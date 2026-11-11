"""MockLLM: a deterministic, rule-based stand-in for the model.

It speaks the same protocol as a real LLM: it reads only the transcript (the incident message and
the tool outputs), requests tools one at a time, and ends with submit_diagnosis citing step IDs.
It never sees the ground-truth label. Use it to build and test the loop, gateway, validator, reports
and eval offline and for free; swap in --llm anthropic|openai|ollama for the real thing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .llm import LLMResponse, ToolCall

OUT_RE = re.compile(r'<tool_output step_id="(s\d+)" tool="(\w+)" trust="untrusted-data">\n(.*?)\n</tool_output>', re.S)


@dataclass
class Obs:
    step_id: str
    tool: str
    args: dict
    data: Any
    ok: bool


class _Need(Exception):
    def __init__(self, name: str, args: dict, thought: str):
        self.name, self.call_args, self.thought = name, args, thought


class _Done(Exception):
    def __init__(self, diagnosis: dict, thought: str):
        self.diagnosis, self.thought = diagnosis, thought


def _parse(messages: list[dict]) -> tuple[dict, int, list[Obs]]:
    first = messages[0]["content"]
    incident = json.loads(re.search(r"<incident>\n(.*?)\n</incident>", first, re.S).group(1))
    pm = re.search(r"db_pool_size:\s*(\d+)", first)
    pool = int(pm.group(1)) if pm else 5
    args_by_id: dict[str, tuple[str, dict]] = {}
    obs: list[Obs] = []
    for m in messages:
        if m["role"] == "assistant":
            for c in m.get("tool_calls", []):
                args_by_id[c["id"]] = (c["name"], c["args"])
        elif m["role"] == "tool":
            for r in m["results"]:
                mm = OUT_RE.search(r["content"])
                if not mm:
                    continue
                try:
                    data = json.loads(mm.group(3).removesuffix(" ...[truncated]"))
                except json.JSONDecodeError:
                    data = None
                ok = isinstance(data, dict) and "error" not in data
                obs.append(Obs(mm.group(1), mm.group(2), args_by_id.get(r["id"], ("", {}))[1], data, ok))
    return incident, pool, obs


def _claim(text: str, *steps: Obs) -> dict:
    return {"text": text, "step_ids": [s.step_id for s in steps]}


def _diag(category: str, conf: float, summary: str, claims: list[dict], alts: list[tuple[str, float]],
          fix: str) -> dict:
    ev = sorted({s for c in claims for s in c["step_ids"]}, key=lambda x: int(x[1:]))
    hyps = [{"category": category, "confidence": conf, "evidence_step_ids": ev, "why": summary}]
    hyps += [{"category": c, "confidence": p, "evidence_step_ids": [], "why": "less consistent with evidence"}
             for c, p in alts]
    return {"category": category, "confidence": conf, "summary": summary, "claims": claims,
            "hypotheses": hyps[:3], "fix": fix}


def _investigate(incident: dict, pool: int, obs: list[Obs]) -> None:
    def get(name: str, thought: str, **args: Any) -> Obs:
        for o in obs:
            if o.tool == name and all(o.args.get(k) == v for k, v in args.items()):
                return o
        raise _Need(name, args, thought)

    svc = incident["service"]
    alerts = get("list_alerts", "Start with what is firing.")
    err = get("query_metrics", f"Is {svc} actually failing requests?", promql=f'http_error_ratio{{service="{svc}"}}')
    lat = get("query_metrics", "And is it slow?", promql=f'http_latency_p95_seconds{{service="{svc}"}}')
    changes = get("recent_changes", "Anything changed recently? Deploys, config, flags.", since_minutes=60)
    errs = get("top_errors", "Which exceptions dominate?", service=svc)
    cont = get("container_stats", f"Check the {svc} container itself.", service=svc)

    err_last = err.data["series"][0]["last"] if err.ok and err.data["series"] else 0.0
    lat_last = lat.data["series"][0]["last"] if lat.ok and lat.data["series"] else 0.0
    sigs = errs.data["signatures"] if errs.ok else []
    dom = [s for s in sigs if s["count"] >= 10]
    ch = changes.data["changes"] if changes.ok else []
    recent = [c for c in ch if c["minutes_before_alert"] <= 15 and c["service"] == svc]
    c = cont.data if cont.ok else {}

    # 1. A dependency refusing connections
    for s in dom:
        if "Connection refused" in s["signature"] or "ConnectError" in s["signature"]:
            target = "payment_stub" if "payment" in s["signature"] else "redis"
            tc = get("container_stats", f"Errors say connection refused; is {target} up?", service=target)
            if tc.ok and not tc.data["running"]:
                raise _Done(_diag(
                    "dependency_down", 0.9,
                    f"{target} is not running, so every {svc} call to it is refused and checkout fails.",
                    [_claim(f"{svc} 5xx ratio rose to {err_last:.0%}", err),
                     _claim(f"{s['count']} x '{s['signature'][:70]}' starting {s['first_seen_minutes_before_alert']} "
                            "min before the alert", errs),
                     _claim(f"{target} container status is {tc.data['status']} (last exit: {tc.data['last_exit']})", tc)],
                    [("network_flaky", 0.06), ("config_error", 0.04)],
                    f"Start {target} (docker compose start {target}); add a circuit breaker and a clear 503 for "
                    "payment unavailability; alert on the dependency's own health check."), "Dependency is down.")

    # 2. Config change right before errors
    cfg = [x for x in recent if x["type"] == "config"]
    if cfg and dom:
        gc = get("get_config", "A config change landed just before the errors. Diff it.", service=svc)
        if gc.ok and gc.data["changed_vs_baseline"]:
            d = gc.data["changed_vs_baseline"][0]
            extra: list[dict] = []
            if "redis" in dom[0]["signature"].lower():
                rc = get("container_stats", "Rule out a real Redis outage.", service="redis")
                extra = [_claim(f"redis container is {rc.data['status']}, so Redis itself is healthy", rc)]
            raise _Done(_diag(
                "config_error", 0.88,
                f"{d['key']} on {svc} was changed from {d['baseline']} to {d['current']} and the service restarted; "
                "it now points at a host that does not exist.",
                [_claim(f"config change to {d['key']} on {svc} {cfg[0]['minutes_before_alert']} min before the alert",
                        changes),
                 _claim(f"{d['key']}: baseline {d['baseline']} -> current {d['current']}", gc),
                 _claim(f"{dom[0]['count']} x '{dom[0]['signature'][:80]}'", errs),
                 _claim(f"5xx ratio at {err_last:.0%}", err), *extra],
                [("cache_failure", 0.08), ("dependency_down", 0.04)],
                f"Restore {d['key']}={d['baseline']} and restart {svc}; validate env at startup (fail fast on "
                "unresolvable hosts); make cache errors fall back to the database."), "Config regression.")

    # 3. Deploy right before a new exception
    deploys = [x for x in recent if x["type"] == "deploy"]
    if deploys and dom and dom[0]["first_seen_minutes_before_alert"] <= deploys[0]["minutes_before_alert"] + 0.5:
        d = deploys[0]
        st = get("get_stack_trace", "New exception right after a deploy. Get the stack.", signature=dom[0]["signature"])
        gd = get("git_diff", "What changed in that deploy?", from_sha=d["previous_sha"], to_sha=d["git_sha"])
        frame = (st.data.get("stack") or "").splitlines()[-2].strip() if st.ok else ""
        raise _Done(_diag(
            "bad_deploy", 0.9,
            f"Deploy {d['git_sha']} changed build_response to index body['shipping_address'] directly; requests "
            "without a shipping address now raise KeyError.",
            [_claim(f"{svc} deployed {d['git_sha']} (from {d['previous_sha']}) {d['minutes_before_alert']} min before "
                    "the alert", changes),
             _claim(f"{dom[0]['count']} x {dom[0]['signature'].split(' @ ')[0]} first seen "
                    f"{dom[0]['first_seen_minutes_before_alert']} min before the alert, after the deploy", errs),
             _claim(f"stack trace ends in {frame}", st),
             _claim("the diff replaces body.get('shipping_address') with body['shipping_address']", gd),
             _claim(f"5xx ratio rose to {err_last:.0%}", err)],
            [("config_error", 0.05), ("race_condition", 0.03)],
            f"Roll back {svc} to {d['previous_sha']}. Patch: restore body.get('shipping_address') or {{}} and "
            "shipping.get(...) with defaults; add a test for checkout without a shipping address."), "Bad deploy.")

    # 4. Memory: OOM kills and restarts
    if c.get("oom_killed") or c.get("restart_count", 0) > 0:
        mem = get("query_metrics", "Container was OOMKilled. Is memory trending up or just capped low?",
                  promql=f'container_memory_rss_bytes{{service="{svc}"}}')
        vals = [p[1] for p in mem.data["series"][0]["points"]] if mem.ok and mem.data["series"] else []
        ups = sum(1 for a, b in zip(vals, vals[1:], strict=False) if b > a * 1.01)
        rising = len(vals) > 3 and ups / (len(vals) - 1) > 0.5
        flags = [x for x in recent if x["type"] == "flag"]
        claims = [_claim(f"{svc} restarted {c['restart_count']} times, last exit {c.get('last_exit')}", cont),
                  _claim(f"RSS {'climbs steadily and resets at each restart' if rising else 'is flat'} "
                         f"(max {max(vals) / 1e6:.0f} MB vs limit {c['mem_limit_mb']} MB)" if vals else "no RSS data",
                         mem)]
        if flags:
            claims.append(_claim(f"flag {flags[0]['flag']} turned on {flags[0]['minutes_before_alert']} min before the "
                                 "alert, when growth began", changes))
        if rising:
            raise _Done(_diag(
                "memory_leak", 0.86,
                f"{svc} memory grows without bound after {flags[0]['flag'] if flags else 'a recent change'} and the "
                "container is repeatedly OOMKilled.", claims, [("resource_limit", 0.1), ("feature_flag", 0.04)],
                (f"Turn off {flags[0]['flag']}. " if flags else "") + "Bound the cache (LRU with max size and TTL) "
                "and key it by customer_id only; add a memory growth alert."), "Memory leak.")
        raise _Done(_diag("resource_limit", 0.7, f"{svc} is OOMKilled at normal usage; the limit is too low.", claims,
                          [("memory_leak", 0.2)], "Raise the memory limit above baseline usage plus headroom."),
                    "Limit too low.")

    # 5. Latency or errors without an obvious exception: follow the slow traces
    if lat_last > 1.0 or err_last > 0.05:
        slow = get("find_slow_traces", "Find the slow requests and see where the time goes.", service=svc,
                   min_duration_ms=1000)
        if slow.ok and slow.data["traces"]:
            tr = get("get_trace", "Open the slowest trace.", trace_id=slow.data["traces"][0]["trace_id"])
            kids = [s for s in tr.data["spans"] if s["depth"] == 1]
            top = max(kids, key=lambda s: s["duration_ms"])
            if top["name"].startswith("POST payment_stub"):
                ob = get("query_metrics", "Time is in the payment call. Check outbound latency.",
                         promql=f'outbound_latency_p95_seconds{{service="{svc}",target="payment_stub"}}')
                server = next((s for s in tr.data["spans"] if s["service"] == "payment_stub"), None)
                claims = [_claim(f"{svc} p95 latency {lat_last:.1f}s, over the 1s SLO", lat),
                          _claim(f"slowest trace spends {top['duration_ms']:.0f} ms of {tr.data['duration_ms']:.0f} ms "
                                 "in POST payment_stub /charge", tr),
                          _claim(f"outbound p95 to payment_stub rose to {ob.data['series'][0]['last']:.1f}s", ob)]
                if server:
                    claims.append(_claim(f"payment_stub's own server span took only {server['duration_ms']:.0f} ms, "
                                         "so the delay is between the services", tr))
                raise _Done(_diag(
                    "dependency_latency", 0.85,
                    "Calls to payment_stub take about 3 s, which pushes checkout p95 over the SLO. The stub's own "
                    "handler is fast, so the delay is on the network path to it.", claims,
                    [("network_flaky", 0.08), ("config_error", 0.04)],
                    "Find and remove the added latency between shop_api and payment_stub (proxy/network); set a "
                    "client timeout near the SLO and fail fast with a circuit breaker."), "Slow dependency.")
            if top["service"] == "postgres" or top["name"] == "sqlalchemy.pool.acquire":
                act = get("db_activity", "Time is in the database or waiting for a pool connection. Check sessions.")
                app = act.data["by_application"].get(svc, {})
                iit = app.get("idle in transaction", 0)
                lockwait = [s for s in act.data["longest_non_idle"] if (s.get("wait_event") or "").startswith("Lock")]
                base_claims = [_claim(f"{svc} p95 latency {lat_last:.1f}s and 5xx ratio {err_last:.0%}", lat, err),
                               _claim(f"slowest trace spends {top['duration_ms']:.0f} ms in {top['name']}", tr)]
                pool_sig = next((s for s in sigs if "QueuePool" in s["signature"]), None)
                if iit >= pool:
                    raise _Done(_diag(
                        "db_pool_exhaustion", 0.88,
                        f"All {pool} pooled connections of {svc} are held by sessions sitting idle in transaction, "
                        "so other requests wait for the pool and time out.",
                        base_claims + [
                            _claim(f"{iit} {svc} sessions are 'idle in transaction' for ~30 s, equal to the pool size "
                                   f"of {pool}", act)]
                        + ([_claim(f"{pool_sig['count']} x QueuePool limit timeouts", errs)] if pool_sig else []),
                        [("lock_contention", 0.07), ("slow_query", 0.05)],
                        "Find the code path that holds a transaction open while doing non-DB work (receipts."
                        "render_receipt sleeps inside session.begin()) and move the wait outside the transaction; set "
                        "idle_in_transaction_session_timeout and a pool timeout below the request timeout."),
                        "Pool exhausted.")
                if lockwait:
                    locks = get("db_locks", "Sessions are waiting on locks. Who is blocking?")
                    chains = locks.data["blocking_chains"] if locks.ok else []
                    if chains:
                        b = chains[0]
                        kq = get("kill_query", "Request (not execute) terminating the blocker; a human approves.",
                                 pid=b["blocker_pid"],
                                 reason=f"{b['blocker_state']} for {b['blocker_xact_age_s']:.0f}s blocking "
                                        f"{len(b['blocked'])} sessions")
                        raise _Done(_diag(
                            "lock_contention", 0.87,
                            f"PID {b['blocker_pid']} ({b['blocker_application']}) has held a row lock on a hot "
                            f"inventory row for {b['blocker_xact_age_s']:.0f}s while idle in transaction; checkout "
                            "UPDATEs queue behind it and hit the lock timeout.",
                            base_claims + [
                                _claim(f"{len(lockwait)} sessions wait on Lock:transactionid", act),
                                _claim(f"blocking chain: pid {b['blocker_pid']} blocks {len(b['blocked'])} sessions on "
                                       f"{b['lock']}", locks),
                                _claim(f"kill_query for pid {b['blocker_pid']} filed as approval "
                                       f"#{kq.data.get('approval_id')}", kq)],
                            [("deadlock", 0.07), ("db_pool_exhaustion", 0.06)],
                            f"Approve terminating pid {b['blocker_pid']}; fix the worker's inventory_sync to commit "
                            "promptly; set idle_in_transaction_session_timeout and lock_timeout."), "Lock contention.")
                tq = get("db_top_queries", "No lock waits. Look for a slow query.", order_by="mean_ms")
                q = tq.data["queries"][0]
                if q["mean_ms"] > 500:
                    raise _Done(_diag(
                        "slow_query", 0.86 if "Seq Scan" in q.get("plan", "") else 0.7,
                        f"'{q['query']}' now averages {q['mean_ms']:.0f} ms with a sequential scan; the index it relied "
                        "on is missing, saturating the DB and the pool.",
                        base_claims + [
                            _claim(f"top query by mean time: {q['query']} at {q['mean_ms']:.0f} ms, plan: {q['plan'][:90]}",
                                   tq),
                            _claim(f"{len(act.data['longest_non_idle'])} long-running active sessions on the same "
                                   "query, not idle in transaction", act)],
                        [("db_pool_exhaustion", 0.08), ("n_plus_one", 0.04)],
                        "Recreate the index: CREATE INDEX CONCURRENTLY ix_orders_customer_id ON orders(customer_id); "
                        "add a migration check that fails CI if the index is missing."), "Slow query.")
        raise _Done(_diag("unknown", 0.3, "Symptoms present but no single cause found.",
                          [_claim(f"p95 {lat_last:.1f}s, 5xx {err_last:.0%}", lat, err)], [],
                          "Escalate to a human."), "Inconclusive.")

    # 6. No user impact
    raise _Done(_diag(
        "none", 0.85,
        f"The {incident['alert_name']} alert is informational; errors and latency on {svc} are within SLO and only "
        "low-volume background errors are present.",
        [_claim(f"alert {incident['alert_name']} is severity {alerts.data['firing'][0]['severity']}", alerts),
         _claim(f"5xx ratio {err_last:.1%}, under the 5% SLO", err),
         _claim(f"p95 latency {lat_last:.2f}s, under the 1s SLO", lat),
         _claim(f"no error signature above 10 occurrences ({len(sigs)} low-volume signatures)", errs),
         _claim(f"{svc} container healthy, {c.get('restart_count', 0)} restarts", cont)],
        [("cpu_saturation", 0.05)],
        "No action. Consider tuning the alert so it does not page."), "No real failure.")


class MockLLM:
    model = "mock-rules-v1"

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        incident, pool, obs = _parse(messages)
        tin = (len(system) + sum(len(json.dumps(m)) for m in messages)) // 4
        n = len(obs) + 1
        try:
            _investigate(incident, pool, obs)
        except _Need as need:
            return LLMResponse(need.thought, [ToolCall(f"call_{n}", need.name, need.call_args)], tin, 40)
        except _Done as done:
            return LLMResponse(done.thought, [ToolCall(f"call_{n}", "submit_diagnosis", done.diagnosis)], tin,
                               len(json.dumps(done.diagnosis)) // 4)
        return LLMResponse("I could not decide.", [], tin, 10)
