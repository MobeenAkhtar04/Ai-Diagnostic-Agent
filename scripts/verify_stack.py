"""Checks the telemetry pipeline end to end. Run from the host: python -m scripts.verify_stack"""

import json
import re
import sys
import time

import httpx

PROM, LOKI, TEMPO = "http://localhost:9090", "http://localhost:3100", "http://localhost:3200"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")


def prom(q: str) -> list:
    r = httpx.get(f"{PROM}/api/v1/query", params={"query": q}, timeout=10).json()
    return r["data"]["result"] if r.get("status") == "success" else []


def main() -> int:
    print("Prometheus")
    try:
        tg = httpx.get(f"{PROM}/api/v1/targets", timeout=10).json()["data"]["activeTargets"]
        for t in tg:
            check(f"target {t['labels']['job']}", t["health"] == "up", t.get("lastError", ""))
        rules = httpx.get(f"{PROM}/api/v1/rules", timeout=10).json()["data"]["groups"]
        n = sum(len(g["rules"]) for g in rules)
        check("rules loaded", n >= 15, f"{n} rules")
        for m in ("http_requests_per_second", "http_error_ratio", "http_latency_p95_seconds", "cache_hit_ratio",
                  "db_queries_per_request", "db_pool_wait_seconds", "event_loop_lag_seconds", "process_open_fds",
                  "container_memory_rss_bytes", "container_cpu_cores", "pg_connections",
                  "outbound_latency_p95_seconds"):
            r = prom(m)
            check(f"metric {m}", len(r) > 0, f"{len(r)} series")
    except Exception as e:  # noqa: BLE001
        check("prometheus reachable", False, str(e))

    print("Loki")
    trace_id = None
    try:
        end = int((time.time() - 30) * 1e9)  # skip the last 30 s: traces need ~10 s to become queryable
        r = httpx.get(f"{LOKI}/loki/api/v1/query_range", timeout=10, params={
            "query": '{service="shop_api"}', "limit": 200, "start": end - 5 * 60 * 10**9, "end": end}).json()
        vals = [v for s in r["data"]["result"] for v in s["values"]]
        lines = [v[1] for v in vals]
        check("logs arriving from shop_api", len(lines) > 0, f"{len(lines)} lines in 5m")
        for _ts, ln in vals:
            m = re.search(r'"trace_id": "([0-9a-f]{32})"', ln)
            if m:
                trace_id = m.group(1)
                break
        check("a log line carries a trace_id", trace_id is not None, trace_id or "")
    except Exception as e:  # noqa: BLE001
        check("loki reachable", False, str(e))

    print("Tempo")
    try:
        time.sleep(3)
        if trace_id:
            r = httpx.get(f"{TEMPO}/api/traces/{trace_id}", timeout=10)
            spans = r.text.count('"spanId"')
            check("trace from log line found in Tempo", r.status_code == 200 and spans > 0, f"HTTP {r.status_code}, {spans} spans")
        s = httpx.get(f"{TEMPO}/api/search", params={"tags": "service.name=shop_api", "limit": 5}, timeout=10).json()
        check("Tempo search returns traces", len(s.get("traces", [])) > 0, f"{len(s.get('traces', []))} traces")
    except Exception as e:  # noqa: BLE001
        check("tempo reachable", False, str(e))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    print(json.dumps([r[0] for r in failed]) if failed else "all good")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
