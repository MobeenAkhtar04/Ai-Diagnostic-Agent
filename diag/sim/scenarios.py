"""The 10 MVP scenarios (1, 2, 4, 8, 11, 15, 16, 21, 29, 30). Each mutates a baseline World.

In P2 these become YAML specs run by the `chaos` CLI against the real stack; the evidence each one
leaves behind here is what the real injection should produce there.
"""

from __future__ import annotations

from .base import Scenario
from .source import CHECKOUT_BAD, line_of
from .world import MB, Trace, World, const, new_world, stack, step


def _pool_exhaustion(w: World) -> None:
    on = -5.0
    w.set_metric("http_latency_p95_seconds", {"service": "shop_api"}, step(on, 0.18, 6.2))
    w.set_metric("http_error_ratio", {"service": "shop_api"}, step(on, 0.008, 0.21), 0.08)
    w.set_metric("db_pool_checked_out", {"service": "shop_api"}, step(on - 0.5, 2, 5, 0.5), 0)
    w.set_metric("db_pool_wait_seconds", {"service": "shop_api"}, step(on, 0.002, 29.5), 0.03)
    w.set_metric("pg_connections", {"state": "idle_in_transaction"}, step(on - 0.5, 0, 5, 0.5), 0)
    w.set_metric("pg_connections", {"state": "active"}, step(on, 3, 1), 0.2)
    msg = ("sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 0 reached, connection timed out, "
           "timeout 30.00")
    fr = "sqlalchemy/pool/impl.py:157 in _do_get"
    w.add_logs("shop_api", "ERROR", msg, 140, on, 0, exc_type="TimeoutError", frame=fr,
               stack=stack(["services/shop_api/app/db.py:18 in get_session",
                            "sqlalchemy/ext/asyncio/session.py:612 in connection", fr], msg))
    w.add_logs("shop_api", "WARN", "slow request POST /checkout took 30.1s", 25, on, 0)
    w.db["sessions"] = [s for s in w.db["sessions"] if s["application"] != "shop_api"] + [
        {"pid": 4100 + i, "application": "shop_api", "state": "idle in transaction",
         "wait_event": "Client:ClientRead", "query": "SELECT id, total_cents FROM orders WHERE id = $1",
         "duration_s": round(w.rng.uniform(24, 31), 1), "xact_age_s": round(w.rng.uniform(24, 31), 1)}
        for i in range(5)
    ]
    w.trace_kinds = {"POST /checkout": 0.5, "GET /orders": 0.2, "GET /receipts/{id}": 0.1, "GET /products/{id}": 0.2}

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on or tr.root_name == "GET /products/{id}":
            return
        for s in tr.spans:
            if s.name == "sqlalchemy.pool.acquire":
                s.duration_ms = round(w.rng.uniform(5000, 30000), 1)
                if s.duration_ms > 29000:
                    s.status = "error"
                    s.attrs["exception.type"] = "sqlalchemy.exc.TimeoutError"

    w.trace_mods.append(mod)


def _missing_index(w: World) -> None:
    on = -6.0
    w.set_metric("http_latency_p95_seconds", {"service": "shop_api"}, step(on, 0.18, 2.8))
    w.set_metric("http_error_ratio", {"service": "shop_api"}, step(on, 0.008, 0.028), 0.1)
    w.set_metric("pg_connections", {"state": "active"}, step(on, 3, 9), 0.1)
    w.set_metric("db_pool_checked_out", {"service": "shop_api"}, step(on, 2, 5), 0)
    w.set_metric("db_pool_wait_seconds", {"service": "shop_api"}, step(on, 0.002, 1.4))
    w.set_metric("container_cpu_cores", {"service": "postgres"}, step(on, 0.4, 1.85), 0.05)
    msg = ("sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 0 reached, connection timed out, "
           "timeout 30.00")
    w.add_logs("shop_api", "ERROR", msg, 8, on + 2, 0, exc_type="TimeoutError", frame="sqlalchemy/pool/impl.py:157 in _do_get",
               stack=stack(["sqlalchemy/pool/impl.py:157 in _do_get"], msg))
    w.add_logs("shop_api", "WARN", "slow request GET /orders took 2.9s", 30, on, 0)
    w.db["top_queries"][0] = {
        "query": "SELECT * FROM orders WHERE customer_id = $1 LIMIT $2", "calls": 5200, "mean_ms": 1843.0,
        "total_ms": 9583600, "rows_per_call": 12, "shared_blks_read": 8_400_000,
        "plan": "Seq Scan on orders  (cost=0.00..21870.00 rows=12) Filter: (customer_id = $1) Rows Removed by Filter: 999988",
    }
    w.db["sessions"] = [s for s in w.db["sessions"] if s["state"] != "active"] + [
        {"pid": 4200 + i, "application": "shop_api", "state": "active", "wait_event": "IO:DataFileRead",
         "query": "SELECT * FROM orders WHERE customer_id = $1 LIMIT $2", "duration_s": round(w.rng.uniform(0.8, 2.6), 2)}
        for i in range(8)
    ]
    w.trace_kinds = {"POST /checkout": 0.3, "GET /orders": 0.5, "GET /products/{id}": 0.2}

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on:
            return
        for s in tr.spans:
            if s.name.startswith("SELECT orders WHERE customer_id"):
                s.duration_ms = round(w.rng.uniform(1500, 2700), 1)
            if s.name == "sqlalchemy.pool.acquire":
                s.duration_ms = round(w.rng.uniform(5, 400), 1)

    w.trace_mods.append(mod)


def _lock_contention(w: World) -> None:
    on = -6.0
    w.set_metric("http_latency_p95_seconds", {"service": "shop_api"}, step(on, 0.18, 4.6))
    w.set_metric("http_error_ratio", {"service": "shop_api"}, step(on, 0.008, 0.09), 0.08)
    w.set_metric("pg_connections", {"state": "active"}, step(on, 3, 7), 0.1)
    w.set_metric("pg_connections", {"state": "idle_in_transaction"}, step(on, 0, 1), 0)
    w.set_metric("db_pool_checked_out", {"service": "shop_api"}, step(on, 2, 5), 0)
    w.set_metric("db_pool_wait_seconds", {"service": "shop_api"}, step(on, 0.002, 3.1))
    msg = ("sqlalchemy.exc.DBAPIError: (asyncpg.exceptions.LockNotAvailableError) canceling statement due to "
           "lock timeout")
    fr = "services/shop_api/app/inventory.py:7 in reserve_inventory"
    w.add_logs("shop_api", "ERROR", msg, 70, on, 0, exc_type="DBAPIError", frame=fr,
               stack=stack(["services/shop_api/app/checkout.py:16 in checkout", fr,
                            "asyncpg/connection.py:1709 in _execute"], msg))
    pmsg = ("sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 0 reached, connection timed out, "
            "timeout 30.00")
    w.add_logs("shop_api", "ERROR", pmsg, 12, on + 1, 0, exc_type="TimeoutError",
               frame="sqlalchemy/pool/impl.py:157 in _do_get", stack=stack(["sqlalchemy/pool/impl.py:157 in _do_get"], pmsg))
    blocker = {"pid": 4411, "application": "worker", "state": "idle in transaction", "wait_event": "Client:ClientRead",
               "query": "UPDATE inventory SET qty = qty WHERE sku = 'SKU-HOT-1'", "duration_s": 371.0,
               "xact_age_s": 372.4}
    waiters = [{"pid": 4500 + i, "application": "shop_api" if i < 5 else "worker", "state": "active",
                "wait_event": "Lock:transactionid", "query": "UPDATE inventory SET qty = qty - $1 WHERE sku = $2 AND qty >= $1",
                "duration_s": round(w.rng.uniform(1.5, 5.0), 1)} for i in range(6)]
    w.db["sessions"] = [s for s in w.db["sessions"] if s["state"] == "idle"] + [blocker] + waiters
    w.db["blocking"] = [{
        "blocker_pid": 4411, "blocker_application": "worker", "blocker_state": "idle in transaction",
        "blocker_xact_age_s": 372.4, "blocker_query": blocker["query"],
        "lock": "transactionid (row lock on inventory sku='SKU-HOT-1')",
        "blocked": [{"pid": x["pid"], "application": x["application"], "waiting_s": x["duration_s"],
                     "query": x["query"]} for x in waiters],
    }]

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on or tr.root_name != "POST /checkout":
            return
        for s in tr.spans:
            if s.name.startswith("UPDATE inventory"):
                s.duration_ms = round(w.rng.uniform(2500, 5000), 1)
                if s.duration_ms > 4600:
                    s.status = "error"
                    s.attrs["exception.type"] = "asyncpg.exceptions.LockNotAvailableError"

    w.trace_mods.append(mod)


def _memory_leak(w: World) -> None:
    flag_at = -14.0
    k = 60.0  # MB per minute
    cycle = (512 - 190) / k
    ooms = [flag_at + cycle, flag_at + 2 * cycle]

    def rss(m: float) -> float:
        if m < flag_at:
            return 190 * MB
        start = flag_at
        for o in ooms:
            if m >= o:
                start = o
        return (190 + (m - start) * k) * MB

    def restarts(m: float) -> float:
        return float(sum(1 for o in ooms if m >= o))

    def err(m: float) -> float:
        return 0.3 if any(o <= m < o + 0.5 for o in ooms) else 0.009

    w.set_metric("container_memory_rss_bytes", {"service": "shop_api"}, rss, 0.005)
    w.set_metric("container_restarts_total", {"service": "shop_api"}, restarts, 0)
    w.set_metric("http_error_ratio", {"service": "shop_api"}, err, 0.1)
    w.set_metric("http_latency_p95_seconds", {"service": "shop_api"}, step(flag_at, 0.18, 0.31, 6))
    w.changes.append({"type": "flag", "service": "shop_api", "flag": "enable_recs_cache", "from": False, "to": True,
                      "minutes_before_alert": 14, "by": "chaos"})
    for o in ooms:
        w.add_logs("shop_api", "INFO", "Application startup complete.", 1, o + 0.1, o + 0.2, with_trace=False)
        w.add_logs("shop_api", "WARN", "upstream connection reset while reading response (502)", 9, o, o + 0.4)
    w.container_overrides["shop_api"] = {"last_exit": {"reason": "OOMKilled", "exit_code": 137,
                                                       "minutes_before_alert": round(-ooms[-1], 1)},
                                         "oom_killed": True, "started_minutes_before_alert": round(-ooms[-1], 1)}


def _bad_deploy(w: World) -> None:
    on = -4.0
    w.changes.append({"type": "deploy", "service": "shop_api", "git_sha": "b7c1e2a", "previous_sha": "a41f9c3",
                      "minutes_before_alert": 4, "by": "ci", "message": "checkout: show delivery ETA on confirmation"})
    w.deployed_sha["shop_api"] = "b7c1e2a"
    w.set_metric("http_error_ratio", {"service": "shop_api"}, step(on, 0.008, 0.24, 0.5), 0.08)
    ln = line_of(CHECKOUT_BAD, 'shipping = body["shipping_address"]')
    fr = f"services/shop_api/app/checkout.py:{ln} in build_response"
    w.add_logs("shop_api", "ERROR", "KeyError: 'shipping_address'", 150, on, 0, exc_type="KeyError", frame=fr,
               stack=stack(["starlette/routing.py:74 in app", "services/shop_api/app/checkout.py:19 in checkout", fr],
                           "KeyError: 'shipping_address'"))
    w.container_overrides["shop_api"] = {"started_minutes_before_alert": 4}

    def mod(w: World, tr: Trace, m: float) -> None:
        if m >= on and tr.root_name == "POST /checkout" and w.rng.random() < 0.55:
            tr.spans[0].status = "error"
            tr.spans[0].attrs.update({"exception.type": "KeyError", "exception.message": "'shipping_address'"})
            tr.spans[0].attrs["http.status_code"] = 500

    w.trace_mods.append(mod)


def _payment_latency(w: World) -> None:
    on = -5.0
    w.set_metric("outbound_latency_p95_seconds", {"service": "shop_api", "target": "payment_stub"}, step(on, 0.08, 3.1))
    w.set_metric("http_latency_p95_seconds", {"service": "shop_api"}, step(on, 0.18, 3.25))
    w.set_metric("http_error_ratio", {"service": "shop_api"}, step(on, 0.008, 0.018), 0.1)
    w.add_logs("shop_api", "WARN", "slow request POST /checkout took 3.3s", 20, on, 0)

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on:
            return
        for s in tr.spans:
            if s.name.startswith("POST payment_stub"):
                s.duration_ms = round(w.rng.uniform(3000, 3250), 1)  # toxiproxy delay; server span stays fast

    w.trace_mods.append(mod)


def _payment_down(w: World) -> None:
    on = -4.0
    w.set_metric("http_error_ratio", {"service": "shop_api"}, step(on, 0.008, 0.33, 0.3), 0.06)
    w.set_metric("http_requests_per_second", {"service": "payment_stub"}, step(on, 12, 0, 0.2), 0)
    w.set_metric("container_cpu_cores", {"service": "payment_stub"}, step(on, 0.05, 0, 0.2), 0)
    w.set_metric("container_memory_rss_bytes", {"service": "payment_stub"}, step(on, 80 * MB, 0, 0.2), 0)
    msg = "httpx.ConnectError: [Errno 111] Connection refused"
    fr = "services/shop_api/app/payment_client.py:11 in charge"
    w.add_logs("shop_api", "ERROR", msg, 160, on, 0, exc_type="ConnectError", frame=fr,
               stack=stack(["services/shop_api/app/checkout.py:18 in checkout", fr,
                            "httpx/_transports/default.py:118 in map_httpcore_exceptions"], msg))
    w.logs = [log for log in w.logs if not (log.service == "payment_stub" and log.ts >= w.ts(on))]
    w.container_overrides["payment_stub"] = {"running": False, "status": "exited",
                                             "last_exit": {"reason": "stopped", "exit_code": 0,
                                                           "minutes_before_alert": 4.0}}

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on:
            return
        new = []
        for s in tr.spans:
            if s.service == "payment_stub":
                continue
            if s.name.startswith("POST payment_stub"):
                s.duration_ms, s.status = 2.1, "error"
                s.attrs["exception.type"] = "httpx.ConnectError"
            new.append(s)
        tr.spans = new

    w.trace_mods.append(mod)


def _broken_env(w: World) -> None:
    on = -5.0
    w.changes += [
        {"type": "config", "service": "shop_api", "key": "REDIS_URL", "minutes_before_alert": 5.3, "by": "ops"},
        {"type": "restart", "service": "shop_api", "minutes_before_alert": 5.0, "by": "ops"},
    ]
    w.config_current["shop_api"]["REDIS_URL"] = "redis://redis-cache:6380/0"
    w.set_metric("cache_hit_ratio", {"service": "shop_api"}, step(on, 0.93, 0.0, 0.2), 0)
    w.set_metric("http_error_ratio", {"service": "shop_api"}, step(on, 0.008, 0.27, 0.3), 0.08)
    msg = "redis.exceptions.ConnectionError: Error -2 connecting to redis-cache:6380. Name or service not known."
    fr = "services/shop_api/app/cache.py:10 in get_cached"
    w.add_logs("shop_api", "ERROR", msg, 130, on, 0, exc_type="ConnectionError", frame=fr,
               stack=stack(["services/shop_api/app/products.py:22 in get_product", fr,
                            "redis/asyncio/connection.py:290 in connect"], msg))
    w.container_overrides["shop_api"] = {"started_minutes_before_alert": 5.0}

    def mod(w: World, tr: Trace, m: float) -> None:
        if m >= on:
            for s in tr.spans:
                if s.name.startswith("redis"):
                    s.status = "error"
                    s.attrs["exception.type"] = "redis.exceptions.ConnectionError"

    w.trace_mods.append(mod)


def _healthy_spike(w: World) -> None:
    on = -6.0
    w.set_metric("http_requests_per_second", {"service": "shop_api"}, step(on, 40, 121, 1.5))
    w.set_metric("http_latency_p95_seconds", {"service": "shop_api"}, step(on, 0.18, 0.34, 1.5))
    w.set_metric("http_error_ratio", {"service": "shop_api"}, const(0.009), 0.15)
    w.set_metric("container_cpu_cores", {"service": "shop_api"}, step(on, 0.3, 0.85, 1.5))
    w.set_metric("pg_connections", {"state": "active"}, step(on, 3, 5), 0.2)
    w.add_logs("shop_api", "WARN", "Coupon validation failed: code expired", 10, on, 0)


def _noise_only(w: World) -> None:
    on = -5.0
    w.add_logs("shop_api", "WARN", "DeprecationWarning: `orm_mode` is deprecated, use `from_attributes`", 60, on, 0,
               with_trace=False)
    w.add_logs("shop_api", "WARN", "cache warmup skipped for key prefix stats:", 30, on, 0, with_trace=False)
    w.add_logs("worker", "WARN", "Retrying fulfilment job (attempt 2/5)", 15, on, 0)


SCENARIOS: dict[int, Scenario] = {s.id: s for s in [
    Scenario(1, "Pool exhaustion", "database", "db_pool_exhaustion", _pool_exhaustion),
    Scenario(2, "Missing index", "database", "slow_query", _missing_index),
    Scenario(4, "Lock contention", "database", "lock_contention", _lock_contention),
    Scenario(8, "Memory leak", "application", "memory_leak", _memory_leak),
    Scenario(11, "Bad deploy", "application", "bad_deploy", _bad_deploy),
    Scenario(15, "Payment latency", "dependency", "dependency_latency", _payment_latency),
    Scenario(16, "Payment down", "dependency", "dependency_down", _payment_down),
    Scenario(21, "Broken env var", "config", "config_error", _broken_env),
    Scenario(29, "Healthy traffic spike", "control", "none", _healthy_spike),
    Scenario(30, "Noise only", "control", "none", _noise_only),
]}


MVP_IDS: tuple[int, ...] = tuple(SCENARIOS)

from .scenarios_heldout import HELDOUT  # noqa: E402

HELDOUT_IDS: tuple[int, ...] = tuple(sorted(HELDOUT))
SCENARIOS.update(HELDOUT)
SCENARIOS = dict(sorted(SCENARIOS.items()))


def build_world(scenario_id: int, seed: int = 0) -> World:
    sc = SCENARIOS[scenario_id]
    w = new_world(sc.id, sc.name, sc.label, seed)
    sc.build(w)
    w.materialize()
    from ..detect import evaluate_alerts

    w.alerts = evaluate_alerts(w)
    return w
