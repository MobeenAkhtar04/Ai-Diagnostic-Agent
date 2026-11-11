"""RealBackend: the same tool surface as SimBackend, answered by the real Docker Compose stack.

  query_metrics / list_alerts     -> Prometheus HTTP API
  search_logs / top_errors / ...  -> Loki HTTP API (LogQL)
  traces                          -> Tempo HTTP API (TraceQL search + trace by id)
  db_*                            -> Postgres through a read only role (pg_monitor): pg_stat_activity,
                                     pg_blocking_pids, pg_stat_statements; deadlock details from the postgres container log
  container_stats / get_config    -> Docker API through a GET-only socket proxy
  recent_changes                  -> the `deploys` table
  read_source / git_diff          -> `git show` / `git diff` on the target repo (read only)

Nothing here writes. Output shapes match SimBackend so the agent, gateway and validator are unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .backend import METRIC_CATALOG, ToolError, hhmm, iso, redact

REAL_EXTRA_METRICS = {
    "container_running": "1 if the container is running, labels: service",
    "container_oom_killed": "1 if the last exit was an OOM kill, labels: service",
    "pg_lock_waiters": "Postgres sessions waiting on a lock",
    "pg_deadlocks_total": "deadlocks detected since start",
}
ALLOWED_METRICS = {**METRIC_CATALOG, **REAL_EXTRA_METRICS}
SERVICES = ("shop_api", "payment_stub", "worker", "postgres", "redis")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _hex(v: str) -> str:
    """Tempo returns ids as hex or base64 depending on the endpoint and version."""
    if re.fullmatch(r"[0-9a-fA-F]+", v or ""):
        return v.lower()
    try:
        return base64.b64decode(v).hex()
    except Exception:  # noqa: BLE001
        return v or ""


def _attr_value(v: dict):
    for k in ("stringValue", "intValue", "boolValue", "doubleValue"):
        if k in v:
            return v[k]
    return str(v)


def _signature(msg: str, stack: str | None) -> str:
    frame = "?"
    if stack:
        frames = re.findall(r'File "([^"]+)", line (\d+), in (\w+)', stack)
        app = [f for f in frames if "services/" in f[0]] or frames
        if app:
            path, line, func = app[-1]
            path = path[path.index("services/"):] if "services/" in path else path
            frame = f"{path}:{line} in {func}"
    return f"{msg[:110]} @ {frame}"


class RealBackend:
    def __init__(self, repo_dir: str | Path | None = None, t_ref: float | None = None):
        self.prom = _env("PROM_URL", "http://localhost:9090").rstrip("/")
        self.loki = _env("LOKI_URL", "http://localhost:3100").rstrip("/")
        self.tempo = _env("TEMPO_URL", "http://localhost:3200").rstrip("/")
        self.pg_dsn = _env("DIAG_PG_DSN", "postgresql://diag_ro:diag@localhost:5432/shop")
        self.docker_host = _env("DOCKER_HOST_PROXY", "tcp://localhost:2375")
        self.repo = Path(repo_dir or _env("TARGET_REPO", ".")).resolve()
        self.compose_file = self.repo / "docker-compose.yml"
        self.project = _env("COMPOSE_PROJECT", "shop")
        self.t_ref = t_ref or time.time()
        nb = os.environ.get("DIAG_NOT_BEFORE")
        self.not_before: float | None = float(nb) if nb else None
        self.http = httpx.Client(timeout=8)

    # ---- helpers ---------------------------------------------------------------------------------
    def _start(self, end: float, minutes: float) -> float:
        """Window start in epoch seconds, never before the fence (when the eval harness sets one)."""
        start = end - minutes * 60
        return max(start, self.not_before) if self.not_before else start

    def _minutes(self, minutes: int) -> int:
        if not self.not_before:
            return minutes
        return max(1, min(minutes, int((time.time() - self.not_before) // 60) + 1))

    def _get(self, url: str, _timeout: float | None = None, **params) -> dict:
        try:
            r = self.http.get(url, params=params, **({"timeout": _timeout} if _timeout else {}))
        except httpx.HTTPError as e:
            raise ToolError(f"backend unreachable: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise ToolError(f"backend returned HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def _rel_min(self, ts: float) -> float:
        return round((self.t_ref - ts) / 60, 1)

    def _pg(self, sql: str, *args) -> list[dict]:
        import asyncpg

        async def run():
            conn = await asyncpg.connect(self.pg_dsn, timeout=4, server_settings={"application_name": "diag_agent"})
            try:
                await conn.execute("SET statement_timeout = '3s'")
                return [dict(r) for r in await conn.fetch(sql, *args)]
            finally:
                await conn.close()

        try:
            return asyncio.run(run())
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"postgres query failed: {type(e).__name__}: {str(e)[:150]}") from e

    def _docker(self):
        import docker

        try:
            return docker.DockerClient(base_url=self.docker_host, timeout=6)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"docker API unreachable: {type(e).__name__}") from e

    def _container(self, client, service: str):
        found = client.containers.list(all=True, filters={"label": [f"com.docker.compose.service={service}",
                                                                   f"com.docker.compose.project={self.project}"]})
        if not found:
            raise ToolError(f"no container for service {service!r}; known: {', '.join(SERVICES)}")
        return found[0]

    # ---- metrics ---------------------------------------------------------------------------------
    def query_metrics(self, promql: str, minutes: int) -> dict:
        m = re.fullmatch(r"\s*([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{(.*)\})?\s*", promql)
        if not m:
            raise ToolError("this backend supports selectors only: metric_name{label=\"value\",...}. "
                            f"Known metrics: {', '.join(ALLOWED_METRICS)}")
        name = m.group(1)
        if name not in ALLOWED_METRICS:
            raise ToolError(f"unknown metric {name!r}. Known metrics: {', '.join(ALLOWED_METRICS)}")
        for part in filter(None, (p.strip() for p in (m.group(3) or "").split(","))):
            if not re.fullmatch(r'\w+\s*(=~|=|!=)\s*"[^"]*"', part):
                raise ToolError(f"cannot parse label matcher {part!r}")
        end = time.time()
        step = max(5, int(minutes * 60 / 60))
        data = self._get(f"{self.prom}/api/v1/query_range", query=promql, start=self._start(end, minutes), end=end,
                         step=step)
        out = []
        for s in data["data"]["result"]:
            pts = [(float(t), float(v)) for t, v in s["values"] if v not in ("NaN", "+Inf", "-Inf")]
            if not pts:
                continue
            idx = sorted({round(i * (len(pts) - 1) / 11) for i in range(12)})
            vals = [v for _, v in pts]
            labels = {k: v for k, v in s["metric"].items() if k not in ("__name__", "job", "instance")}
            out.append({"labels": labels, "points": [[hhmm(pts[i][0]), round(pts[i][1], 4)] for i in idx],
                        "first": vals[0], "last": vals[-1], "min": min(vals), "max": max(vals)})
        return {"query": promql, "window_minutes": minutes, "end": iso(end), "step": f"~{step}s", "series": out,
                "note": None if out else "no series matched"}

    def list_alerts(self) -> dict:
        data = self._get(f"{self.prom}/api/v1/alerts")["data"]["alerts"]
        firing = []
        for a in data:
            if a["state"] != "firing":
                continue
            active = datetime.fromisoformat(a["activeAt"].replace("Z", "+00:00")).timestamp()
            lbl = a["labels"]
            firing.append({"name": lbl["alertname"], "severity": lbl.get("severity", "warning"),
                           "labels": {k: v for k, v in lbl.items() if k not in ("alertname", "severity")},
                           "summary": a["annotations"].get("summary", ""), "started_at": iso(active)})
        return {"firing": firing}

    # ---- logs ------------------------------------------------------------------------------------
    def _loki_lines(self, service: str | None, level: str | None, contains: str | None, minutes: int,
                    limit: int) -> list[dict]:
        sel = f'service="{service}"' if service else 'service=~".+"'
        if level:
            sel += f',level="{level.lower() if level != "WARN" else "warning"}"'
        q = "{" + sel + "}"
        if contains:
            q += " |~ `(?i)" + re.escape(contains).replace("`", "") + "`"
        end = time.time_ns()
        start = int(self._start(end / 1e9, minutes) * 1e9)
        # Loki occasionally stalls for >10 s on the first big read while the shop is failing. Fail fast, then
        # retry once with a smaller read; top_errors reports the sample size actually used.
        attempts = ((limit, 4.0), (min(limit, 100), 7.0))
        for i, (attempt_limit, attempt_timeout) in enumerate(attempts):
            self._loki_limit_used = attempt_limit
            try:
                data = self._get(f"{self.loki}/loki/api/v1/query_range", query=q, limit=attempt_limit,
                                 direction="backward", start=start, end=end, _timeout=attempt_timeout)
                break
            except ToolError as e:
                if i == len(attempts) - 1 or "ReadTimeout" not in str(e):
                    raise
        rows = []
        for stream in data["data"]["result"]:
            for ts, line in stream["values"]:
                try:
                    obj = json.loads(line)
                except ValueError:
                    obj = {"msg": line, "level": "INFO"}
                obj.setdefault("service", stream["stream"].get("service"))
                obj["_ts"] = int(ts) / 1e9
                rows.append(obj)
        rows.sort(key=lambda r: -r["_ts"])
        return rows

    @staticmethod
    def _lvl(obj: dict) -> str:
        return {"WARNING": "WARN"}.get(obj.get("level", "INFO"), obj.get("level", "INFO"))

    def search_logs(self, service, level, contains, minutes, limit) -> dict:
        rows = self._loki_lines(service, level, contains, minutes, limit)
        return {"total_matched": len(rows), "lines": [
            {"ts": iso(r["_ts"]), "service": r["service"], "level": self._lvl(r), "msg": r.get("msg", "")[:400],
             "trace_id": r.get("trace_id")} for r in rows[:limit]]}

    SAMPLE = 300

    def _count_errors(self, service: str | None, minutes: int) -> int | None:
        """Exact number of exception log lines, counted by Loki (cheap), not by pulling the lines."""
        sel = f'service="{service}"' if service else 'service=~".+"'
        q = f'sum(count_over_time({{{sel},level="error"}} |= `exc_type` [{self._minutes(minutes)}m]))'
        try:
            r = self._get(f"{self.loki}/loki/api/v1/query", query=q, _timeout=2.0)["data"]["result"]
            return int(float(r[0]["value"][1])) if r else 0
        except (ToolError, KeyError, ValueError, IndexError):
            return None

    def top_errors(self, service, minutes, limit) -> dict:
        rows = [r for r in self._loki_lines(service, "ERROR", "exc_type", minutes, self.SAMPLE) if r.get("exc_type")]
        by_sig: dict[str, list[dict]] = {}
        for r in rows:
            by_sig.setdefault(_signature(r.get("msg", ""), r.get("stack")), []).append(r)
        counts = Counter({k: len(v) for k, v in by_sig.items()})
        out = []
        for sig, n in counts.most_common(limit):
            ex = sorted(by_sig[sig], key=lambda r: r["_ts"])
            out.append({"signature": sig, "count": n, "service": ex[0]["service"], "first_seen": iso(ex[0]["_ts"]),
                        "first_seen_minutes_before_alert": self._rel_min(ex[0]["_ts"]),
                        "last_seen": iso(ex[-1]["_ts"]), "example_trace_id": ex[0].get("trace_id")})
        total = self._count_errors(service, minutes)
        res = {"window_minutes": minutes, "total_errors": total if total is not None else len(rows), "signatures": out}
        used = getattr(self, "_loki_limit_used", self.SAMPLE)
        if len(rows) >= used:
            res["note"] = f"signature counts come from the newest {used} error lines; total_errors is exact"
        return res

    def get_stack_trace(self, signature: str) -> dict:
        rows = [r for r in self._loki_lines(None, "ERROR", "exc_type", 60, self.SAMPLE) if r.get("exc_type")]
        for needle in (lambda s: s == signature, lambda s: signature.lower() in s.lower()):
            ex = [r for r in rows if needle(_signature(r.get("msg", ""), r.get("stack")))]
            if ex:
                return {"signature": _signature(ex[0].get("msg", ""), ex[0].get("stack")), "occurrences": len(ex),
                        "service": ex[0]["service"], "stack": (ex[0].get("stack") or "")[-3000:],
                        "example_trace_id": ex[0].get("trace_id")}
        raise ToolError("no error matches that signature; call top_errors first")

    # ---- traces ----------------------------------------------------------------------------------
    def find_slow_traces(self, service, min_duration_ms, minutes, limit) -> dict:
        end = int(time.time())
        base = {"start": int(self._start(end, minutes)), "end": end}
        q = f'{{ resource.service.name = "{service}" && duration >= {int(min_duration_ms)}ms }}'
        slow = self._get(f"{self.tempo}/api/search", q=q, limit=limit * 3, **base).get("traces", [])
        allq = self._get(f"{self.tempo}/api/search", q=f'{{ resource.service.name = "{service}" }}', limit=500,
                         **base).get("traces", [])
        slow.sort(key=lambda t: -float(t.get("durationMs", 0)))
        errq = f'{{ resource.service.name = "{service}" && status = error && duration >= {int(min_duration_ms)}ms }}'
        try:
            err_ids = {t["traceID"] for t in self._get(f"{self.tempo}/api/search", q=errq, limit=200, **base).get("traces", [])}
        except ToolError:
            err_ids = set()
        traces = []
        for t in slow[:limit]:
            traces.append({"trace_id": t["traceID"].zfill(32), "root": t.get("rootTraceName", "?"),
                           "duration_ms": float(t.get("durationMs", 0)), "error": t["traceID"] in err_ids,
                           "ts": iso(int(t["startTimeUnixNano"]) / 1e9)})
        return {"traces_in_window": len(allq), "matched": len(slow), "traces": traces}

    def get_trace(self, trace_id: str) -> dict:
        try:
            r = self.http.get(f"{self.tempo}/api/traces/{trace_id}")
        except httpx.HTTPError as e:
            raise ToolError(f"backend unreachable: {type(e).__name__}") from e
        if r.status_code == 404:
            raise ToolError("trace not found (traces become queryable about 10 s after the request; "
                            "older ids may have expired)")
        if r.status_code >= 400:
            raise ToolError(f"backend returned HTTP {r.status_code}")
        spans = []
        for batch in r.json().get("batches", []):
            svc = next((_attr_value(a["value"]) for a in batch.get("resource", {}).get("attributes", [])
                        if a["key"] == "service.name"), "?")
            for ss in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
                for sp in ss.get("spans", []):
                    spans.append({
                        "id": _hex(sp["spanId"]), "parent": _hex(sp.get("parentSpanId", "")), "name": sp["name"],
                        "service": svc, "start": int(sp["startTimeUnixNano"]), "end": int(sp["endTimeUnixNano"]),
                        "status": "error" if sp.get("status", {}).get("code") in (2, "STATUS_CODE_ERROR") else "ok",
                        "attrs": {a["key"]: _attr_value(a["value"]) for a in sp.get("attributes", [])
                                  if not a["key"].startswith(("net.", "thread."))}})
        if not spans:
            raise ToolError("trace not found")
        spans.sort(key=lambda s: s["start"])
        ids = {s["id"] for s in spans}
        root = next((s for s in spans if not s["parent"] or s["parent"] not in ids), spans[0])
        depth = {root["id"]: 0}
        out = []
        for s in spans:
            d = depth.get(s["parent"], -1) + 1 if s["parent"] in depth else (0 if s is root else 1)
            depth[s["id"]] = d
            out.append({"depth": d, "name": s["name"], "service": s["service"],
                        "start_ms": round((s["start"] - root["start"]) / 1e6, 1),
                        "duration_ms": round((s["end"] - s["start"]) / 1e6, 1), "status": s["status"],
                        "attrs": s["attrs"]})
        return {"trace_id": trace_id, "root": root["name"], "duration_ms": round((root["end"] - root["start"]) / 1e6, 1),
                "ts": iso(root["start"] / 1e9), "spans": out[:60]}

    # ---- database --------------------------------------------------------------------------------
    def db_activity(self) -> dict:
        rows = self._pg(
            "SELECT pid, coalesce(nullif(application_name,''),'unknown') application, state, wait_event_type || ':' || wait_event wait_event, "
            "left(query, 200) query, extract(epoch FROM now() - coalesce(query_start, state_change)) duration_s "
            "FROM pg_stat_activity WHERE datname = 'shop' AND pid <> pg_backend_pid() AND backend_type = 'client backend'")
        by_state = Counter((r["state"] or "other").replace(" ", "_") for r in rows)
        by_app: dict[str, Counter] = {}
        for r in rows:
            by_app.setdefault(r["application"], Counter())[(r["state"] or "other").replace(" ", "_")] += 1
        longest = sorted((r for r in rows if r["state"] not in ("idle", None)), key=lambda r: -(r["duration_s"] or 0))[:8]
        mx = self._pg("SELECT setting::int AS m FROM pg_settings WHERE name = 'max_connections'")[0]["m"]
        return {"total_connections": len(rows), "max_connections": mx, "by_state": dict(by_state),
                "by_application": {k: dict(v) for k, v in by_app.items()},
                "longest_non_idle": [{**r, "duration_s": round(float(r["duration_s"] or 0), 2)} for r in longest]}

    def db_locks(self) -> dict:
        rows = self._pg(
            "SELECT b.pid blocker_pid, coalesce(nullif(b.application_name,''),'unknown') blocker_application, "
            "b.state blocker_state, extract(epoch FROM now() - b.xact_start) blocker_xact_age_s, left(b.query,200) blocker_query, "
            "a.pid blocked_pid, coalesce(nullif(a.application_name,''),'unknown') blocked_app, "
            "extract(epoch FROM now() - a.query_start) waiting_s, left(a.query,200) blocked_query "
            "FROM pg_stat_activity a CROSS JOIN LATERAL unnest(pg_blocking_pids(a.pid)) bp(pid) "
            "JOIN pg_stat_activity b ON b.pid = bp.pid WHERE a.datname = 'shop'")
        chains: dict[int, dict] = {}
        for r in rows:
            c = chains.setdefault(r["blocker_pid"], {
                "blocker_pid": r["blocker_pid"], "blocker_application": r["blocker_application"],
                "blocker_state": r["blocker_state"], "blocker_xact_age_s": round(float(r["blocker_xact_age_s"] or 0), 1),
                "blocker_query": r["blocker_query"], "blocked": []})
            c["blocked"].append({"pid": r["blocked_pid"], "application": r["blocked_app"],
                                 "waiting_s": round(float(r["waiting_s"] or 0), 1), "query": r["blocked_query"]})
        return {"blocking_chains": list(chains.values()), "deadlocks_last_15m": self._deadlocks()}

    def _deadlocks(self) -> list[dict]:
        try:
            client = self._docker()
            raw = self._container(client, "postgres").logs(since=int(self._start(time.time(), 15)), timestamps=False).decode(
                "utf-8", "replace")
        except Exception:  # noqa: BLE001
            return []
        entries: list[list[str]] = []
        for ln in raw.splitlines():
            if re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+ \w+ \[\d+\]", ln):
                entries.append([ln])
            elif entries:
                entries[-1].append(ln.strip())
        out = []
        for e in entries:
            if "deadlock detected" in e[0]:
                stamp = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", e[0])
                ts = datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp() if stamp else self.t_ref
                out.append({"minutes_before_alert": self._rel_min(ts), "victim_application": "see detail",
                            "detail": " ".join(e[1:])[:700]})
        return out[-8:]

    def db_top_queries(self, order_by: str, limit: int) -> dict:
        col = {"mean_ms": "mean_exec_time", "total_ms": "total_exec_time", "calls": "calls"}[order_by]
        rows = self._pg(
            f"SELECT left(query, 300) query, calls, round(mean_exec_time::numeric, 3)::float mean_ms, "
            f"round(total_exec_time::numeric, 1)::float total_ms, (rows / greatest(calls, 1))::int rows_per_call "
            f"FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = 'shop') "
            f"AND query NOT ILIKE '%pg_stat_%' AND query NOT ILIKE '%generate_series%' AND query NOT ILIKE '%pg_settings%' AND query !~* '^(SET|BEGIN|COMMIT|ROLLBACK)' "
            f"ORDER BY {col} DESC LIMIT $1", limit)
        for r in rows:
            r["plan"] = self._explain(r["query"])
        return {"order_by": order_by, "queries": rows}

    @staticmethod
    def _bind_placeholders(query: str) -> str:
        """pg_stat_statements stores $1-style placeholders, which EXPLAIN cannot take over a prepared statement.
        Substitute typed stand-in constants so the planner still chooses a realistic plan."""
        def typed(m: re.Match) -> str:
            t = m.group(1).lower()
            if t in ("varchar", "text", "char", "bpchar"):
                return f"'x'::{t}"
            if t in ("integer", "int", "int4", "bigint", "int8", "smallint", "int2", "numeric"):
                return f"50::{t}"
            return f"NULL::{t}"
        out = re.sub(r"\$\d+::(\w+)", typed, query)
        return re.sub(r"\$\d+", "50", out)

    def _explain(self, query: str) -> str:
        if not re.match(r"(?is)^\s*select\b", query):
            return "(plan not available)"
        try:
            plan = self._pg(f"EXPLAIN (COSTS OFF) {self._bind_placeholders(query).rstrip(';')}")
            lines = [list(p.values())[0] for p in plan]
            return " -> ".join(ln.strip().removeprefix("->").strip() for ln in lines[:4])
        except ToolError:
            return "(plan not available)"

    # ---- containers, changes, config -------------------------------------------------------------
    def container_stats(self, service: str) -> dict:
        if service not in SERVICES:
            raise ToolError(f"unknown service; known: {', '.join(SERVICES)}")
        client = self._docker()
        c = self._container(client, service)
        c.reload()
        st = c.attrs["State"]
        host = c.attrs["HostConfig"]
        snap = {"service": service, "running": bool(st.get("Running")), "status": st.get("Status"),
                "cpu_cores": 0.0, "cpu_limit_cores": (host.get("NanoCpus") or 0) / 1e9 or None,
                "cpu_throttled_pct": 0.0, "mem_rss_mb": 0.0, "mem_limit_mb": round((host.get("Memory") or 0) / 1e6, 1),
                "disk_used_pct": 0.0, "open_fds": None, "fd_limit": 1024, "restart_count": c.attrs.get("RestartCount", 0),
                "last_exit": None, "oom_killed": bool(st.get("OOMKilled")),
                "started_minutes_before_alert": self._rel_min(self._parse_docker_ts(st.get("StartedAt")))}
        if st.get("Running"):
            s = c.stats(stream=False)
            mem = s.get("memory_stats", {})
            snap["mem_rss_mb"] = round(mem.get("stats", {}).get("anon", mem.get("usage", 0)) / 1e6, 1)
            cpu, pre = s.get("cpu_stats", {}), s.get("precpu_stats", {})
            dt = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
            ds = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
            snap["cpu_cores"] = round(dt / ds * (cpu.get("online_cpus") or 1), 2) if ds > 0 else 0.0
            th = cpu.get("throttling_data", {})
            snap["cpu_throttled_pct"] = round(100 * th.get("throttled_periods", 0) / max(1, th.get("periods", 1)), 1)
        fin_ts = self._parse_docker_ts(st["FinishedAt"]) if st.get("FinishedAt") and not st["FinishedAt"].startswith("0001") else None
        if fin_ts is not None and not (self.not_before and fin_ts < self.not_before):
            reason = "OOMKilled" if st.get("OOMKilled") else ("stopped" if st.get("ExitCode") == 0 else "crashed")
            snap["last_exit"] = {"reason": reason, "exit_code": st.get("ExitCode"),
                                 "minutes_before_alert": self._rel_min(fin_ts)}
        snap["recent_events"] = self._container_events(client, service)
        for metric, key, scale in (("process_open_fds", "open_fds", 1), ("container_disk_used_ratio", "disk_used_pct", 100)):
            try:
                res = self._get(f"{self.prom}/api/v1/query", query=f'{metric}{{service="{service}"}}')["data"]["result"]
                if res:
                    snap[key] = round(float(res[0]["value"][1]) * scale, 1) if scale != 1 else int(float(res[0]["value"][1]))
            except ToolError:
                pass
        return snap

    def _container_events(self, client, service: str) -> list[dict] | str:
        """Docker lifecycle events for the service (oom, die with exit code, kill with signal, start, restart)."""
        now = time.time()
        try:
            evs = client.events(since=int(self._start(now, 30)), until=int(now), decode=True, filters={
                "type": "container", "label": [f"com.docker.compose.service={service}",
                                               f"com.docker.compose.project={self.project}"]})
            out = []
            for e in evs:
                action = (e.get("Action") or e.get("status") or "").split(":")[0]
                if action not in ("oom", "die", "kill", "start", "restart", "stop"):
                    continue
                attrs = (e.get("Actor") or {}).get("Attributes") or {}
                ev = {"action": action, "minutes_before_alert": self._rel_min(float(e.get("time", now)))}
                if action == "die":
                    ev["exit_code"] = attrs.get("exitCode")
                if action == "kill":
                    ev["signal"] = attrs.get("signal")
                out.append(ev)
            return out[-12:]
        except Exception as e:  # noqa: BLE001
            return f"events unavailable: {type(e).__name__}"

    @staticmethod
    def _parse_docker_ts(s: str | None) -> float:
        if not s or s.startswith("0001"):
            return time.time()
        s = re.sub(r"(\.\d{6})\d*Z$", r"\1+00:00", s)
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

    def recent_changes(self, since_minutes: int) -> dict:
        rows = self._pg("SELECT kind, service, git_sha, previous_sha, detail, \"by\", extract(epoch FROM at) ts "
                        "FROM deploys WHERE at > now() - make_interval(mins => $1) ORDER BY at DESC", since_minutes)
        changes = []
        for r in rows:
            c = {"type": r["kind"], "service": r["service"], "by": r["by"],
                 "minutes_before_alert": self._rel_min(float(r["ts"])), "at": iso(float(r["ts"]))}
            for k in ("git_sha", "previous_sha", "detail"):
                if r[k]:
                    c[k] = r[k]
            changes.append(c)
        changes.sort(key=lambda c: c["minutes_before_alert"])
        return {"since_minutes": since_minutes, "changes": changes}

    def _baseline_env(self, service: str) -> dict[str, str]:
        import yaml

        spec = yaml.safe_load(self.compose_file.read_text())["services"].get(service) or {}
        env = spec.get("environment") or {}
        if isinstance(env, list):
            env = dict(e.split("=", 1) for e in env)
        # compose interpolation: ${VAR:-default} is the baseline value when VAR is not set
        return {k: re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", str(v)) for k, v in env.items()}

    def get_config(self, service: str) -> dict:
        client = self._docker()
        c = self._container(client, service)
        cur = dict(e.split("=", 1) for e in c.attrs["Config"]["Env"] if "=" in e and not e.startswith(("PATH=", "PYTHON", "LANG=", "GPG_KEY=")))
        try:
            base = self._baseline_env(service)
        except (OSError, KeyError):
            base = {}
        cur = {k: v for k, v in cur.items() if k in base or not base}
        diff = [{"key": k, "baseline": redact(k, base.get(k, "")), "current": redact(k, cur.get(k, ""))}
                for k in sorted(set(base) | set(cur)) if base.get(k) != cur.get(k)]
        return {"service": service, "changed_vs_baseline": diff, "current": {k: redact(k, v) for k, v in cur.items()}}

    # ---- source ----------------------------------------------------------------------------------
    def _git(self, *args: str) -> str:
        try:
            p = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ToolError(f"git unavailable: {type(e).__name__}") from e
        if p.returncode != 0:
            raise ToolError(p.stderr.strip().splitlines()[-1][:200] if p.stderr.strip() else "git failed")
        return p.stdout

    def _deployed_sha(self, path: str) -> str:
        svc = "payment_stub" if "payment_stub" in path else "worker" if "/worker/" in path else "shop_api"
        rows = self._pg("SELECT git_sha FROM deploys WHERE service = $1 AND git_sha IS NOT NULL AND kind = 'deploy' "
                        "ORDER BY at DESC LIMIT 1", svc)
        return rows[0]["git_sha"] if rows else "HEAD"

    def read_source(self, path: str, start_line: int, end_line: int, sha: str | None) -> dict:
        sha = sha or self._deployed_sha(path)
        try:
            text = self._git("show", f"{sha}:{path}")
        except ToolError as e:
            files = self._git("ls-tree", "-r", "--name-only", sha, "services/").split() if sha else []
            raise ToolError(f"path not found at {sha}; files: {', '.join(f for f in files if f.endswith('.py'))[:600]}") from e
        sel = text.splitlines()[start_line - 1:end_line]
        return {"path": path, "sha": sha, "start_line": start_line,
                "lines": [f"{i:4d}  {ln}" for i, ln in enumerate(sel, start=start_line)]}

    def git_diff(self, from_sha: str, to_sha: str, path: str | None) -> dict:
        args = ["diff", "--no-color", from_sha, to_sha] + (["--", path] if path else ["--", "services/"])
        return {"from": from_sha, "to": to_sha, "diff": self._git(*args)[:12000] or "(no changes)"}
