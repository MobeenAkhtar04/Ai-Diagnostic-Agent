"""Backend interface the tools read through.

SimBackend answers from a simulated World. In P1/P3 write RealBackend with the same methods:
  query_metrics  -> Prometheus /api/v1/query_range
  logs           -> Loki /loki/api/v1/query_range
  traces         -> Tempo /api/search and /api/traces/{id}
  db_*           -> fixed queries on pg_stat_activity, pg_locks, pg_stat_statements (read only role, pg_monitor)
  containers     -> Docker API through a GET-only socket proxy
  changes        -> deploys table
  config         -> env files at the deployed commit
  source / diff  -> git, read only
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Protocol

from ..sim.world import World

METRIC_CATALOG = {
    "http_error_ratio": "5xx responses / all responses, labels: service",
    "http_latency_p95_seconds": "p95 request latency, labels: service",
    "http_requests_per_second": "request rate, labels: service",
    "container_memory_rss_bytes": "resident memory, labels: service (incl. postgres, redis)",
    "container_memory_limit_bytes": "memory limit, labels: service",
    "container_cpu_cores": "CPU usage in cores, labels: service",
    "container_restarts_total": "restart counter, labels: service",
    "pg_connections": "Postgres backends by state, labels: state=active|idle|idle_in_transaction",
    "db_pool_checked_out": "SQLAlchemy pool connections in use, labels: service",
    "db_pool_wait_seconds": "time waiting for a pool connection, labels: service",
    "outbound_latency_p95_seconds": "p95 latency of outbound HTTP calls, labels: service, target",
    "cache_hit_ratio": "Redis cache hit ratio, labels: service",
    "event_loop_lag_seconds": "asyncio event loop scheduling lag, labels: service",
    "process_open_fds": "open file descriptors, labels: service (incl. postgres, redis)",
    "container_cpu_throttled_ratio": "fraction of CPU periods throttled by the cgroup quota, labels: service",
    "container_disk_used_ratio": "disk used / capacity of the service's volume, labels: service",
    "db_queries_per_request": "SQL statements per HTTP request, labels: service",
    "outbound_requests_per_second": "outbound HTTP requests per second, labels: service, target",
    "db_integrity_errors_per_minute": "constraint / integrity violations per minute, labels: service",
}

SECRET_KEYS = re.compile(r"(KEY|SECRET|PASSWORD|TOKEN)", re.I)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M:%S")


def redact(key: str, value: str) -> str:
    if SECRET_KEYS.search(key):
        return "***redacted***"
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", value)


class Backend(Protocol):
    def query_metrics(self, promql: str, minutes: int) -> dict: ...
    def list_alerts(self) -> dict: ...
    def search_logs(self, service: str | None, level: str | None, contains: str | None, minutes: int,
                    limit: int) -> dict: ...
    def top_errors(self, service: str | None, minutes: int, limit: int) -> dict: ...
    def get_stack_trace(self, signature: str) -> dict: ...
    def find_slow_traces(self, service: str, min_duration_ms: float, minutes: int, limit: int) -> dict: ...
    def get_trace(self, trace_id: str) -> dict: ...
    def db_activity(self) -> dict: ...
    def db_locks(self) -> dict: ...
    def db_top_queries(self, order_by: str, limit: int) -> dict: ...
    def container_stats(self, service: str) -> dict: ...
    def recent_changes(self, since_minutes: int) -> dict: ...
    def get_config(self, service: str) -> dict: ...
    def read_source(self, path: str, start_line: int, end_line: int, sha: str | None) -> dict: ...
    def git_diff(self, from_sha: str, to_sha: str, path: str | None) -> dict: ...


class ToolError(Exception):
    pass


class SimBackend:
    def __init__(self, world: World):
        self.w = world

    # ---- metrics -------------------------------------------------------------------------------
    def query_metrics(self, promql: str, minutes: int) -> dict:
        m = re.fullmatch(r"\s*([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{(.*)\})?\s*", promql)
        if not m:
            raise ToolError("this backend supports selectors only: metric_name{label=\"value\",...}. "
                            f"Known metrics: {', '.join(METRIC_CATALOG)}")
        name, body = m.group(1), m.group(3) or ""
        if name not in METRIC_CATALOG:
            raise ToolError(f"unknown metric {name!r}. Known metrics: {', '.join(METRIC_CATALOG)}")
        matchers = []
        for part in filter(None, (p.strip() for p in body.split(","))):
            mm = re.fullmatch(r'(\w+)\s*(=~|=|!=)\s*"([^"]*)"', part)
            if not mm:
                raise ToolError(f"cannot parse label matcher {part!r}")
            matchers.append(mm.groups())
        cutoff = self.w.ts(-minutes)
        out = []
        for s in self.w.series:
            if s.name != name:
                continue
            ok = True
            for k, op, v in matchers:
                lv = s.labels.get(k, "")
                if (op == "=" and lv != v) or (op == "!=" and lv == v) or (op == "=~" and not re.fullmatch(v, lv)):
                    ok = False
            if not ok:
                continue
            pts = [(t, v) for t, v in s.points if t >= cutoff]
            idx = sorted({round(i * (len(pts) - 1) / 11) for i in range(12)})
            vals = [v for _, v in pts]
            out.append({
                "labels": s.labels,
                "points": [[hhmm(pts[i][0]), pts[i][1]] for i in idx],
                "first": vals[0], "last": vals[-1], "min": min(vals), "max": max(vals),
            })
        return {"query": promql, "window_minutes": minutes, "end": iso(self.w.t_alert), "step": "~75s",
                "series": out, "note": None if out else "no series matched"}

    def list_alerts(self) -> dict:
        return {"firing": [{**a, "started_at": iso(a["started_at"])} for a in self.w.alerts]}

    # ---- logs ----------------------------------------------------------------------------------
    def _logs(self, service: str | None, minutes: int):
        cutoff = self.w.ts(-minutes)
        return [lg for lg in self.w.logs if lg.ts >= cutoff and (service is None or lg.service == service)]

    def search_logs(self, service, level, contains, minutes, limit) -> dict:
        rows = [lg for lg in self._logs(service, minutes)
                if (level is None or lg.level == level.upper())
                and (contains is None or contains.lower() in lg.msg.lower())]
        rows = sorted(rows, key=lambda lg: -lg.ts)
        return {"total_matched": len(rows), "lines": [
            {"ts": iso(lg.ts), "service": lg.service, "level": lg.level, "msg": lg.msg, "trace_id": lg.trace_id}
            for lg in rows[:limit]]}

    def top_errors(self, service, minutes, limit) -> dict:
        rows = [lg for lg in self._logs(service, minutes) if lg.level == "ERROR" and lg.signature]
        counts = Counter(lg.signature for lg in rows)
        out = []
        for sig, n in counts.most_common(limit):
            ex = [lg for lg in rows if lg.signature == sig]
            out.append({"signature": sig, "count": n, "service": ex[0].service,
                        "first_seen": iso(ex[0].ts), "first_seen_minutes_before_alert": round(-self.w.rel(ex[0].ts), 1),
                        "last_seen": iso(ex[-1].ts), "example_trace_id": ex[0].trace_id})
        return {"window_minutes": minutes, "total_errors": len(rows), "signatures": out}

    def get_stack_trace(self, signature: str) -> dict:
        ex = [lg for lg in self.w.logs if lg.signature == signature]
        if not ex:
            ex = [lg for lg in self.w.logs if lg.signature and signature.lower() in lg.signature.lower()]
        if not ex:
            raise ToolError("no error matches that signature; call top_errors first")
        return {"signature": ex[0].signature, "occurrences": len(ex), "service": ex[0].service,
                "stack": ex[0].stack, "example_trace_id": ex[0].trace_id}

    # ---- traces --------------------------------------------------------------------------------
    def find_slow_traces(self, service, min_duration_ms, minutes, limit) -> dict:
        cutoff = self.w.ts(-minutes)
        rows = [t for t in self.w.traces if t.ts >= cutoff and t.service == service and t.duration_ms >= min_duration_ms]
        rows.sort(key=lambda t: -t.duration_ms)
        total = sum(1 for t in self.w.traces if t.ts >= cutoff and t.service == service)
        return {"traces_in_window": total, "matched": len(rows), "traces": [
            {"trace_id": t.trace_id, "root": t.root_name, "duration_ms": t.duration_ms, "error": t.error,
             "ts": iso(t.ts)} for t in rows[:limit]]}

    def get_trace(self, trace_id: str) -> dict:
        tr = next((t for t in self.w.traces if t.trace_id == trace_id), None)
        if tr is None:
            raise ToolError("trace not found")
        depth = {tr.spans[0].span_id: 0}
        spans = []
        for s in tr.spans:
            d = depth.get(s.parent_id or "", -1) + 1 if s.parent_id else 0
            depth[s.span_id] = d
            spans.append({"depth": d, "name": s.name, "service": s.service, "start_ms": s.start_ms,
                          "duration_ms": s.duration_ms, "status": s.status, "attrs": s.attrs})
        return {"trace_id": tr.trace_id, "root": tr.root_name, "duration_ms": tr.duration_ms, "ts": iso(tr.ts),
                "spans": spans}

    # ---- database ------------------------------------------------------------------------------
    def db_activity(self) -> dict:
        sess = self.w.db["sessions"]
        by_state = Counter(s["state"] for s in sess)
        by_app: dict[str, Counter] = {}
        for s in sess:
            by_app.setdefault(s["application"], Counter())[s["state"]] += 1
        longest = sorted((s for s in sess if s["state"] != "idle"), key=lambda s: -s["duration_s"])[:8]
        return {"total_connections": len(sess), "max_connections": self.w.db["max_connections"],
                "by_state": dict(by_state), "by_application": {k: dict(v) for k, v in by_app.items()},
                "longest_non_idle": longest}

    def db_locks(self) -> dict:
        return {"blocking_chains": self.w.db["blocking"], "deadlocks_last_15m": self.w.db["deadlocks"]}

    def db_top_queries(self, order_by: str, limit: int) -> dict:
        rows = sorted(self.w.db["top_queries"], key=lambda q: -q[order_by])
        return {"order_by": order_by, "queries": rows[:limit]}

    # ---- infra / changes -----------------------------------------------------------------------
    def container_stats(self, service: str) -> dict:
        if service not in self.w.containers:
            raise ToolError(f"unknown service; known: {', '.join(self.w.containers)}")
        return self.w.containers[service]

    def recent_changes(self, since_minutes: int) -> dict:
        rows = [c for c in self.w.changes if c["minutes_before_alert"] <= since_minutes]
        rows = sorted(rows, key=lambda c: c["minutes_before_alert"])
        return {"since_minutes": since_minutes, "changes": [
            {**c, "at": iso(self.w.ts(-c["minutes_before_alert"]))} for c in rows]}

    def get_config(self, service: str) -> dict:
        if service not in self.w.config_current:
            raise ToolError(f"no config for {service}")
        base, cur = self.w.config_baseline[service], self.w.config_current[service]
        diff = [{"key": k, "baseline": redact(k, base.get(k, "")), "current": redact(k, cur.get(k, ""))}
                for k in sorted(set(base) | set(cur)) if base.get(k) != cur.get(k)]
        return {"service": service, "changed_vs_baseline": diff,
                "current": {k: redact(k, v) for k, v in cur.items()}}

    def read_source(self, path: str, start_line: int, end_line: int, sha: str | None) -> dict:
        svc = "payment_stub" if "payment_stub" in path else "shop_api"
        sha = sha or self.w.deployed_sha[svc]
        files = self.w.commits.get(sha)
        if files is None:
            raise ToolError(f"unknown commit {sha}")
        if path not in files:
            raise ToolError(f"path not found at {sha}; files: {', '.join(sorted(files))}")
        lines = files[path].splitlines()
        sel = lines[start_line - 1:end_line]
        return {"path": path, "sha": sha, "start_line": start_line,
                "lines": [f"{i:4d}  {ln}" for i, ln in enumerate(sel, start=start_line)]}

    def git_diff(self, from_sha: str, to_sha: str, path: str | None) -> dict:
        a, b = self.w.commits.get(from_sha), self.w.commits.get(to_sha)
        if a is None or b is None:
            raise ToolError(f"unknown commit; known: {', '.join(self.w.commits)}")
        out = []
        for p in sorted(set(a) | set(b)):
            if path and p != path:
                continue
            if a.get(p) != b.get(p):
                out.extend(difflib.unified_diff((a.get(p) or "").splitlines(), (b.get(p) or "").splitlines(),
                                                f"a/{p}", f"b/{p}", lineterm=""))
        return {"from": from_sha, "to": to_sha, "diff": "\n".join(out) or "(no changes)"}
