"""The 20 held-out failure scenarios (3, 5, 6, 7, 9, 10, 12, 13, 14, 17-20, 22-28).

HELD OUT means the agent's prompt and tools were never tuned against these. Run them once, record the
number, and only then iterate. The MVP scenarios live in scenarios.py.

In P2/P5 each of these becomes a YAML spec run by the `chaos` CLI against the real stack; the evidence
each leaves behind here is what the real injection should produce there.
"""

from __future__ import annotations

from .base import Scenario
from .source import PAYMENT_CLIENT, line_of
from .source_variants import (
    CHECKOUT_REGEX,
    CHECKOUT_SYNC_WEBHOOK,
    INVENTORY_RACY,
    MIGRATION_0042,
    ORDERS_LAZY,
    P_CHECKOUT,
    P_INVENTORY,
    P_MIGRATION,
    P_ORDERS,
    P_PAYMENT_CLIENT,
    P_RECEIPTS,
    P_REFUNDS,
    PAYMENT_CLIENT_RETRY_ONCE,
    PAYMENT_CLIENT_RETRY_STORM,
    RECEIPTS_FD_LEAK,
    REFUNDS,
    make_commit,
)
from .world import MB, LogLine, Trace, World, _span, stack, step

BASE_LAT = {"shop_api": 0.18, "payment_stub": 0.06, "worker": 0.25}
BASE_ERR = {"shop_api": 0.008, "payment_stub": 0.002, "worker": 0.004}
PG = {"db.system": "postgresql"}


# ---- helpers ------------------------------------------------------------------------------------
def _deploy(w: World, svc: str, sha: str, minutes_ago: float, msg: str, files: dict[str, str],
            prev: str = "a41f9c3") -> None:
    w.commits[sha] = make_commit(files)
    w.deployed_sha[svc] = sha
    w.changes.append({"type": "deploy", "service": svc, "git_sha": sha, "previous_sha": prev,
                      "minutes_before_alert": minutes_ago, "by": "ci", "message": msg})


def _lat(w: World, on: float, v: float, svc: str = "shop_api", ramp: float = 1.0) -> None:
    w.set_metric("http_latency_p95_seconds", {"service": svc}, step(on, BASE_LAT[svc], v, ramp))


def _err(w: World, on: float, v: float, svc: str = "shop_api", ramp: float = 0.5, noise: float = 0.08) -> None:
    w.set_metric("http_error_ratio", {"service": svc}, step(on, BASE_ERR[svc], v, ramp), noise)


def _m(w: World, name: str, labels: dict, on: float, before: float, after: float, ramp: float = 1.0,
       noise: float = 0.05) -> None:
    w.set_metric(name, labels, step(on, before, after, ramp), noise)


def _errlog(w: World, svc: str, msg: str, n: int, start: float, exc: str, frames: list[str],
            end: float = 0) -> None:
    w.add_logs(svc, "ERROR", msg, n, start, end, exc_type=exc, frame=frames[-1], stack=stack(frames, msg))


def _pairs(w: World, svc: str, level: str, fmt: str, n: int, start: float, end: float = 0,
           gap_s: float = 0.002) -> None:
    """n pairs of log lines about the same key, a few milliseconds apart."""
    for _ in range(n):
        m = w.rng.uniform(start, end)
        key = w.rng.randint(88000, 89999)
        for k in range(2):
            w.logs.append(LogLine(w.ts(m) + k * gap_s, svc, level, fmt.format(key=key), w.hexid(32)))


def _spans(tr: Trace, prefix: str) -> list:
    return [s for s in tr.spans if s.name.startswith(prefix)]


def _sessions(w: World, app: str, n: int, state: str, query: str, wait: str | None, dur: tuple[float, float],
              pid0: int) -> list[dict]:
    return [{"pid": pid0 + i, "application": app, "state": state, "wait_event": wait, "query": query,
             "duration_s": round(w.rng.uniform(*dur), 2)} for i in range(n)]


# ---- 3: N+1 -------------------------------------------------------------------------------------
def _n_plus_one(w: World) -> None:
    on = -8.0
    _deploy(w, "shop_api", "d4e51a9", 22, "orders: load order items on demand", {P_ORDERS: ORDERS_LAZY})
    _lat(w, on, 1.8)
    _err(w, on, 0.012, ramp=1, noise=0.1)
    _m(w, "db_queries_per_request", {"service": "shop_api"}, on, 4.1, 57)
    _m(w, "container_cpu_cores", {"service": "postgres"}, on, 0.4, 1.3)
    _m(w, "pg_connections", {"state": "active"}, on, 3, 8, noise=0.1)
    w.db["top_queries"][4] = {"query": "SELECT * FROM order_items WHERE order_id = $1", "calls": 412000,
                              "mean_ms": 0.31, "total_ms": 127720, "rows_per_call": 6,
                              "plan": "Index Scan using ix_order_items_order_id on order_items"}
    w.add_logs("shop_api", "WARN", "slow request GET /orders took 1.9s", 35, on, 0)
    w.trace_kinds = {"GET /orders": 0.55, "POST /checkout": 0.2, "GET /products/{id}": 0.25}

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on or tr.root_name != "GET /orders":
            return
        root = tr.spans[0]
        tr.spans = [s for s in tr.spans if not s.name.startswith("SELECT order_items")]
        for _ in range(25):
            tr.spans.append(_span(w, root.span_id, "SELECT order_items WHERE order_id = $1", "postgres", 0,
                                  w.rng.uniform(0.6, 1.8), **PG))
        root.duration_ms = round(w.rng.uniform(1400, 2200), 1)

    w.trace_mods.append(mod)


# ---- 5: deadlock --------------------------------------------------------------------------------
def _deadlock(w: World) -> None:
    on = -7.0
    _deploy(w, "shop_api", "e2a7c40", 300, "refunds: restock inventory when an order is refunded",
            {P_REFUNDS: REFUNDS})
    _err(w, on, 0.09)
    _lat(w, on, 1.4)
    msg = "sqlalchemy.exc.DBAPIError: (asyncpg.exceptions.DeadlockDetectedError) deadlock detected"
    _errlog(w, "shop_api", msg, 62, on, "DBAPIError",
            ["services/shop_api/app/checkout.py:16 in checkout", "services/shop_api/app/inventory.py:7 in reserve_inventory"])
    _errlog(w, "shop_api", msg, 28, on, "DBAPIError",
            ["services/shop_api/app/refunds.py:7 in refund_order", "services/shop_api/app/refunds.py:11 in refund_order"])
    w.db["deadlocks"] = [{
        "minutes_before_alert": round(w.rng.uniform(0.2, 6.5), 1),
        "victim_application": "shop_api",
        "detail": f"Process {5100 + i} waits for ShareLock on transaction {99100 + i * 3}; blocked by process {5200 + i}. "
                  f"Process {5200 + i} waits for ShareLock on transaction {99101 + i * 3}; blocked by process {5100 + i}. "
                  f"Process {5100 + i}: UPDATE inventory SET qty = qty - $1 WHERE sku = $2 AND qty >= $1 "
                  f"Process {5200 + i}: UPDATE orders SET status = 'refunded' WHERE id = $1",
    } for i in range(6)]
    w.db["sessions"] += _sessions(w, "shop_api", 3, "active", "UPDATE inventory SET qty = qty - $1 WHERE sku = $2",
                                  "Lock:transactionid", (0.2, 1.1), 5300)


# ---- 6: connection limit ------------------------------------------------------------------------
def _conn_limit(w: World) -> None:
    on = -6.0
    w.changes.append({"type": "scale", "service": "worker", "detail": "replicas 2 -> 8", "minutes_before_alert": 9,
                      "by": "autoscaler"})
    _err(w, on, 0.22)
    _err(w, on, 0.35, svc="worker")
    _m(w, "pg_connections", {"state": "idle"}, on, 7, 80, ramp=2)
    _m(w, "pg_connections", {"state": "active"}, on, 3, 9, noise=0.1)
    msg = "asyncpg.exceptions.TooManyConnectionsError: sorry, too many clients already"
    for svc, n in [("shop_api", 90), ("worker", 70)]:
        _errlog(w, svc, msg, n, on, "TooManyConnectionsError",
                [f"services/{svc}/app/db.py:18 in get_session", "asyncpg/connect_utils.py:1012 in _connect_addr"])
    sess = (_sessions(w, "worker", 62, "idle", "COMMIT", "Client:ClientRead", (1, 300), 6000)
            + _sessions(w, "shop_api", 21, "idle", "COMMIT", "Client:ClientRead", (1, 120), 6100)
            + _sessions(w, "postgres_exporter", 3, "idle", "SELECT 1", "Client:ClientRead", (1, 15), 6200)
            + _sessions(w, "worker", 6, "active", "SELECT * FROM fulfilment_jobs WHERE status = 'queued' FOR UPDATE SKIP LOCKED LIMIT 10",
                        None, (0.002, 0.02), 6300)
            + _sessions(w, "shop_api", 8, "active", "SELECT * FROM products WHERE sku = ANY($1)", None, (0.001, 0.01), 6400))
    w.db["sessions"] = sess


# ---- 7: disk full -------------------------------------------------------------------------------
def _disk_full(w: World) -> None:
    on = -6.0
    w.changes.append({"type": "job", "service": "worker", "detail": "bulk_import_catalog started",
                      "minutes_before_alert": 12, "by": "ops"})
    _m(w, "container_disk_used_ratio", {"service": "postgres"}, -11, 0.41, 1.0, ramp=5, noise=0.002)
    _err(w, on, 0.35)
    _err(w, on, 0.4, svc="worker")
    msg = ('sqlalchemy.exc.OperationalError: (asyncpg.exceptions.DiskFullError) could not extend file '
           '"base/16384/24598": No space left on device')
    _errlog(w, "shop_api", msg, 120, on, "OperationalError",
            ["services/shop_api/app/orders.py:10 in create_order", "asyncpg/protocol/protocol.pyx:338 in query"])
    _errlog(w, "worker", msg, 40, on, "OperationalError",
            ["services/worker/app/fulfil.py:44 in mark_fulfilled", "asyncpg/protocol/protocol.pyx:338 in query"])


# ---- 9: CPU saturation (catastrophic regex) -----------------------------------------------------
def _cpu_spike(w: World) -> None:
    on = -7.0
    _deploy(w, "shop_api", "f09b3c8", 1200, "checkout: validate coupon format", {P_CHECKOUT: CHECKOUT_REGEX})
    _lat(w, on, 6.5)
    _err(w, on, 0.02, ramp=1, noise=0.1)
    _m(w, "container_cpu_cores", {"service": "shop_api"}, on, 0.3, 1.96, noise=0.02)
    _m(w, "container_cpu_throttled_ratio", {"service": "shop_api"}, on, 0.0, 0.1, noise=0)
    w.add_logs("shop_api", "WARN", "slow request POST /checkout took 8.1s (coupon_code_len=4096)", 30, on, 0)
    w.trace_kinds = {"POST /checkout": 0.6, "GET /orders": 0.2, "GET /products/{id}": 0.2}

    def mod(w: World, tr: Trace, m: float) -> None:
        if m >= on and tr.root_name == "POST /checkout" and w.rng.random() < 0.45:
            tr.spans[0].duration_ms = round(w.rng.uniform(6000, 9500), 1)
            tr.spans[0].attrs["coupon.length"] = w.rng.choice([2048, 4096, 8192])

    w.trace_mods.append(mod)


# ---- 10: blocked event loop ---------------------------------------------------------------------
def _event_loop(w: World) -> None:
    on = -6.0
    _deploy(w, "shop_api", "0a6d2e5", 50, "checkout: notify partner webhook after payment",
            {P_CHECKOUT: CHECKOUT_SYNC_WEBHOOK})
    _lat(w, on, 2.4)
    _err(w, on, 0.03, ramp=1, noise=0.1)
    _m(w, "event_loop_lag_seconds", {"service": "shop_api"}, on, 0.002, 1.9, noise=0.1)
    ln = line_of(CHECKOUT_SYNC_WEBHOOK, "requests.post")
    w.add_logs("shop_api", "WARN",
               "asyncio: Executing <Task pending name='Task-4412' coro=<checkout() running at "
               f"/app/services/shop_api/app/checkout.py:{ln}> took 1.842 seconds", 60, on, 0, with_trace=False)

    def mod(w: World, tr: Trace, m: float) -> None:
        if m >= on:
            tr.spans[0].duration_ms = round(w.rng.uniform(1200, 2600), 1)

    w.trace_mods.append(mod)


# ---- 12: race condition, oversell ---------------------------------------------------------------
def _oversell(w: World) -> None:
    on = -9.0
    _deploy(w, "shop_api", "1b7f9d2", 540, "inventory: log stock level before reserving", {P_INVENTORY: INVENTORY_RACY})
    _m(w, "http_requests_per_second", {"service": "shop_api"}, on, 40, 98)
    _err(w, on, 0.08)
    _m(w, "db_integrity_errors_per_minute", {"service": "shop_api"}, on, 0.2, 14, noise=0.1)
    msg = ('sqlalchemy.exc.IntegrityError: (asyncpg.exceptions.CheckViolationError) new row for relation '
           '"inventory" violates check constraint "inventory_qty_nonneg"')
    ln = line_of(INVENTORY_RACY, "UPDATE inventory")
    _errlog(w, "shop_api", msg, 95, on, "IntegrityError",
            ["services/shop_api/app/checkout.py:16 in checkout", f"services/shop_api/app/inventory.py:{ln} in reserve_inventory"])
    _pairs(w, "shop_api", "INFO", "reserve_inventory read qty=2 sku=SKU-HOT-1", 30, on)
    w.add_logs("worker", "WARN", "reconciliation: oversold sku SKU-HOT-1, stock=-3", 6, on, 0)
    w.db["top_queries"].append({"query": "SELECT qty FROM inventory WHERE sku = $1", "calls": 61000, "mean_ms": 0.5,
                                "total_ms": 30500, "rows_per_call": 1, "plan": "Index Scan using inventory_pkey"})


# ---- 13: race condition, duplicate charge -------------------------------------------------------
def _dup_charge(w: World) -> None:
    on = -6.0
    _deploy(w, "shop_api", "2c8e0a4", 14, "payment: retry once when the charge times out",
            {P_PAYMENT_CLIENT: PAYMENT_CLIENT_RETRY_ONCE})
    _lat(w, on, 2.4)
    _err(w, on, 0.012, ramp=1, noise=0.1)
    _lat(w, on, 1.4, svc="payment_stub")
    _m(w, "http_requests_per_second", {"service": "payment_stub"}, on, 12, 16)
    _m(w, "outbound_requests_per_second", {"service": "shop_api", "target": "payment_stub"}, on, 12, 16)
    _m(w, "outbound_latency_p95_seconds", {"service": "shop_api", "target": "payment_stub"}, on, 0.08, 1.35)
    w.add_logs("shop_api", "WARN", "charge timed out for order 88123 (attempt 1), retrying", 60, on, 0)
    _pairs(w, "payment_stub", "INFO", "POST /charge 200 order_id={key} captured", 24, on, gap_s=1.1)
    for i in range(14):
        w.add_logs("worker", "WARN", f"reconciliation: order {88400 + i * 17} has 2 captured payments", 1, on, 0)
    w.trace_kinds = {"POST /checkout": 0.6, "GET /orders": 0.2, "GET /products/{id}": 0.2}

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on or tr.root_name != "POST /checkout" or w.rng.random() > 0.3:
            return
        root = tr.spans[0]
        pay = _spans(tr, "POST payment_stub")[0]
        first_server = next(s for s in tr.spans if s.parent_id == pay.span_id)
        pay.duration_ms, pay.status = 1000.0, "error"
        pay.attrs.update({"exception.type": "httpx.ReadTimeout", "attempt": 1})
        first_server.duration_ms = round(w.rng.uniform(1300, 1700), 1)  # the stub still finished and captured it
        first_server.attrs["charge.captured"] = True
        retry = _span(w, root.span_id, "POST payment_stub /charge", "shop_api", 0, w.rng.uniform(55, 95),
                      **{"http.url": "http://payment-stub:8080/charge", "peer.service": "payment_stub", "attempt": 2})
        tr.spans.append(retry)
        tr.spans.append(_span(w, retry.span_id, "POST /charge", "payment_stub", 0, retry.duration_ms * 0.7,
                              **{"charge.captured": True}))

    w.trace_mods.append(mod)


# ---- 14: file descriptor leak -------------------------------------------------------------------
def _fd_leak(w: World) -> None:
    on = -2.0
    _deploy(w, "shop_api", "3d9f1b6", 180, "receipts: also write a PDF copy to disk", {P_RECEIPTS: RECEIPTS_FD_LEAK})
    w.set_metric("process_open_fds", {"service": "shop_api"}, lambda m: min(1022, 62 + (m + 15) * 62.5), 0.01)
    _err(w, on, 0.21)
    ln = line_of(RECEIPTS_FD_LEAK, "open(path")
    _errlog(w, "shop_api", "OSError: [Errno 24] Too many open files: '/tmp/receipts/88231.pdf'", 110, on, "OSError",
            ["services/shop_api/app/receipts.py:17 in render_receipt", f"services/shop_api/app/receipts.py:{ln} in write_receipt_pdf"])
    w.add_logs("shop_api", "WARN", "socket.accept() out of file descriptors; sleeping", 25, on, 0, with_trace=False)
    w.container_overrides["shop_api"] = {"open_fds": 1022}
    w.trace_kinds = {"POST /checkout": 0.3, "GET /receipts/{id}": 0.4, "GET /orders": 0.15, "GET /products/{id}": 0.15}


# ---- 17: flaky network --------------------------------------------------------------------------
def _flaky(w: World) -> None:
    on = -6.0
    _err(w, on, 0.30)
    _lat(w, on, 0.9)
    _m(w, "http_requests_per_second", {"service": "payment_stub"}, on, 12, 8.5)
    _errlog(w, "shop_api", "httpx.ReadError: [Errno 104] Connection reset by peer", 110, on, "ReadError",
            ["services/shop_api/app/checkout.py:18 in checkout", f"services/shop_api/app/payment_client.py:{line_of(PAYMENT_CLIENT, 'client.post')} in charge",
             "httpx/_transports/default.py:118 in map_httpcore_exceptions"])
    w.add_logs("shop_api", "WARN", "payment charge failed, returning 502", 40, on, 0)

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on:
            return
        for s in _spans(tr, "POST payment_stub"):
            if w.rng.random() < 0.3:
                s.duration_ms, s.status = round(w.rng.uniform(2, 9), 1), "error"
                s.attrs["exception.type"] = "httpx.ReadError"
                tr.spans = [x for x in tr.spans if x.parent_id != s.span_id]

    w.trace_mods.append(mod)


# ---- 18: retry storm ----------------------------------------------------------------------------
def _retry_storm(w: World) -> None:
    on = -6.0
    _deploy(w, "shop_api", "4eaa2c7", 25, "payment: retry failed charges up to 5 times",
            {P_PAYMENT_CLIENT: PAYMENT_CLIENT_RETRY_STORM})
    _lat(w, on, 8.5)
    _err(w, on, 0.25)
    _m(w, "outbound_requests_per_second", {"service": "shop_api", "target": "payment_stub"}, on, 12, 86)
    _m(w, "outbound_latency_p95_seconds", {"service": "shop_api", "target": "payment_stub"}, on, 0.08, 2.0)
    _m(w, "http_requests_per_second", {"service": "payment_stub"}, on, 12, 86)
    _lat(w, on, 2.6, svc="payment_stub")
    _err(w, on, 0.06, svc="payment_stub")
    _m(w, "container_cpu_cores", {"service": "payment_stub"}, on, 0.05, 1.1)
    for attempt, n in [(1, 120), (2, 90), (3, 60), (4, 40)]:
        w.add_logs("shop_api", "WARN", f"charge attempt {attempt}/5 failed (timeout), retrying immediately", n, on, 0)
    _errlog(w, "shop_api", "RuntimeError: payment unavailable after 5 attempts", 70, on, "RuntimeError",
            ["services/shop_api/app/checkout.py:18 in checkout",
             f"services/shop_api/app/payment_client.py:{line_of(PAYMENT_CLIENT_RETRY_STORM, 'raise RuntimeError')} in charge"])
    w.add_logs("payment_stub", "WARN", "request queue depth 118, shedding load", 30, on, 0)

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on or tr.root_name != "POST /checkout":
            return
        root = tr.spans[0]
        pay = _spans(tr, "POST payment_stub")[0]
        pay.duration_ms, pay.status = 2000.0, "error"
        pay.attrs["attempt"] = 1
        for i in range(2, w.rng.randint(3, 6)):
            tr.spans.append(_span(w, root.span_id, "POST payment_stub /charge", "shop_api", 0, 2000.0,
                                  **{"peer.service": "payment_stub", "attempt": i, "exception.type": "httpx.ReadTimeout"}))
        tr.spans[-1].status = "error"

    w.trace_mods.append(mod)


# ---- 19: cache outage ---------------------------------------------------------------------------
def _cache_down(w: World) -> None:
    on = -6.0
    _lat(w, on, 2.3)
    _err(w, on, 0.13)
    _m(w, "cache_hit_ratio", {"service": "shop_api"}, on, 0.93, 0.0, ramp=0.3, noise=0)
    _m(w, "container_cpu_cores", {"service": "redis"}, on, 0.05, 0, ramp=0.2, noise=0)
    _m(w, "container_memory_rss_bytes", {"service": "redis"}, on, 42 * MB, 0, ramp=0.2, noise=0)
    _m(w, "pg_connections", {"state": "active"}, on, 3, 21, noise=0.1)
    _m(w, "db_pool_wait_seconds", {"service": "shop_api"}, on, 0.002, 0.9)
    _errlog(w, "shop_api", "redis.exceptions.ConnectionError: Error 111 connecting to redis:6379. Connection refused.",
            110, on, "ConnectionError",
            ["services/shop_api/app/products.py:22 in get_product", "services/shop_api/app/cache.py:10 in get_cached",
             "redis/asyncio/connection.py:290 in connect"])
    w.db["top_queries"][3] = {"query": "SELECT * FROM products WHERE sku = $1", "calls": 205000, "mean_ms": 4.8,
                              "total_ms": 984000, "rows_per_call": 1, "plan": "Index Scan using products_pkey"}
    w.container_overrides["redis"] = {"running": False, "status": "exited",
                                      "last_exit": {"reason": "stopped", "exit_code": 0, "minutes_before_alert": 6.0}}
    w.trace_kinds = {"GET /products/{id}": 0.55, "POST /checkout": 0.25, "GET /orders": 0.2}

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on or tr.root_name != "GET /products/{id}":
            return
        for s in _spans(tr, "redis"):
            s.status, s.duration_ms = "error", 1.5
            s.attrs["exception.type"] = "redis.exceptions.ConnectionError"
        tr.spans.append(_span(w, tr.spans[0].span_id, "SELECT * FROM products WHERE sku = $1", "postgres", 0,
                              w.rng.uniform(150, 900), **PG))

    w.trace_mods.append(mod)


# ---- 20: cache stampede -------------------------------------------------------------------------
def _stampede(w: World) -> None:
    on = -4.5
    w.changes.append({"type": "cache", "service": "redis", "detail": "DEL product:SKU-HOT-1 (manual)",
                      "minutes_before_alert": 5, "by": "ops"})
    _lat(w, on, 3.0)
    _err(w, on, 0.07)
    w.set_metric("cache_hit_ratio", {"service": "shop_api"},
                 lambda m: 0.93 if m < on else (0.3 if m < -3 else min(0.93, 0.3 + 0.2 * (m + 3))), 0.02)
    _m(w, "pg_connections", {"state": "active"}, on, 3, 38, noise=0.1)
    _m(w, "db_pool_wait_seconds", {"service": "shop_api"}, on, 0.002, 2.2)
    _m(w, "container_cpu_cores", {"service": "postgres"}, on, 0.4, 1.7)
    w.add_logs("shop_api", "INFO", "cache miss key=product:SKU-HOT-1", 420, -4.5, -3.5)
    w.add_logs("shop_api", "INFO", "cache set key=product:SKU-HOT-1 ttl=5s", 90, -4.5, 0)
    w.add_logs("shop_api", "ERROR",
               "sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 0 reached, connection timed out, timeout 30.00",
               40, -3.8, 0, exc_type="TimeoutError", frame="sqlalchemy/pool/impl.py:157 in _do_get",
               stack=stack(["sqlalchemy/pool/impl.py:157 in _do_get"], "sqlalchemy.exc.TimeoutError: QueuePool limit"))
    w.db["top_queries"][3] = {"query": "SELECT * FROM products WHERE sku = $1", "calls": 186000, "mean_ms": 14.0,
                              "total_ms": 2604000, "rows_per_call": 1, "plan": "Index Scan using products_pkey"}
    w.db["sessions"] += _sessions(w, "shop_api", 30, "active", "SELECT * FROM products WHERE sku = $1", None,
                                  (0.2, 1.4), 7000)
    w.trace_kinds = {"GET /products/{id}": 0.55, "POST /checkout": 0.25, "GET /orders": 0.2}

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on or tr.root_name != "GET /products/{id}":
            return
        for s in _spans(tr, "redis"):
            s.attrs["cache.hit"] = False
        tr.spans.append(_span(w, tr.spans[0].span_id, "SELECT * FROM products WHERE sku = $1", "postgres", 0,
                              w.rng.uniform(300, 900), **PG))

    w.trace_mods.append(mod)


# ---- 22: timeout set too low --------------------------------------------------------------------
def _timeout_low(w: World) -> None:
    on = -8.0
    w.changes += [
        {"type": "config", "service": "shop_api", "key": "PAYMENT_TIMEOUT_MS", "minutes_before_alert": 8.4, "by": "ops"},
        {"type": "restart", "service": "shop_api", "minutes_before_alert": 8.0, "by": "ops"},
    ]
    w.config_current["shop_api"]["PAYMENT_TIMEOUT_MS"] = "50"
    w.container_overrides["shop_api"] = {"started_minutes_before_alert": 8.0}
    _err(w, on, 0.55)
    _lat(w, on, 0.3)
    _errlog(w, "shop_api", "httpx.ReadTimeout: timed out", 150, on, "ReadTimeout",
            ["services/shop_api/app/checkout.py:18 in checkout",
             f"services/shop_api/app/payment_client.py:{line_of(PAYMENT_CLIENT, 'client.post')} in charge"])

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on:
            return
        for s in _spans(tr, "POST payment_stub"):
            if w.rng.random() < 0.9:
                s.duration_ms, s.status = 50.0, "error"
                s.attrs["exception.type"] = "httpx.ReadTimeout"

    w.trace_mods.append(mod)


# ---- 23: bad migration --------------------------------------------------------------------------
def _bad_migration(w: World) -> None:
    on = -6.0
    w.commits["6acc4e9"] = make_commit({P_MIGRATION: MIGRATION_0042})
    w.changes.append({"type": "migration", "service": "shop_api", "name": "0042_rename_orders_total_cents",
                      "git_sha": "6acc4e9", "minutes_before_alert": 6.2, "by": "ci"})
    _err(w, on, 0.31)
    msg = ('sqlalchemy.exc.ProgrammingError: (asyncpg.exceptions.UndefinedColumnError) column "total_cents" of '
           'relation "orders" does not exist')
    _errlog(w, "shop_api", msg, 120, on, "ProgrammingError",
            ["services/shop_api/app/checkout.py:17 in checkout", "services/shop_api/app/orders.py:10 in create_order"])
    _errlog(w, "shop_api", 'sqlalchemy.exc.ProgrammingError: (asyncpg.exceptions.UndefinedColumnError) column "total_cents" does not exist',
            30, on, "ProgrammingError",
            ["services/shop_api/app/receipts.py:10 in render_receipt"])
    w.trace_kinds = {"POST /checkout": 0.5, "GET /receipts/{id}": 0.2, "GET /orders": 0.15, "GET /products/{id}": 0.15}

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on:
            return
        for s in _spans(tr, "INSERT INTO orders") + _spans(tr, "SELECT id, total_cents"):
            s.status = "error"
            s.attrs["exception.type"] = "asyncpg.exceptions.UndefinedColumnError"

    w.trace_mods.append(mod)


# ---- 24: rotated credential ---------------------------------------------------------------------
def _auth_rotated(w: World) -> None:
    on = -6.0
    w.changes.append({"type": "secret", "service": "payment_stub", "detail": "PAYMENT_API_KEY rotated",
                      "minutes_before_alert": 12, "by": "security"})
    w.config_current["payment_stub"]["PAYMENT_API_KEY"] = "pk_live_ROTATED_a1f9"
    _err(w, on, 0.42)
    ln = line_of(PAYMENT_CLIENT, "raise_for_status")
    _errlog(w, "shop_api", "httpx.HTTPStatusError: Client error '401 Unauthorized' for url 'http://payment-stub:8080/charge'",
            150, on, "HTTPStatusError",
            ["services/shop_api/app/checkout.py:18 in checkout", f"services/shop_api/app/payment_client.py:{ln} in charge"])
    w.add_logs("payment_stub", "WARN", "rejected POST /charge: invalid api key (401)", 150, on, 0)

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on:
            return
        for s in _spans(tr, "POST payment_stub"):
            s.status, s.attrs["http.status_code"] = "error", 401
            s.attrs["exception.type"] = "httpx.HTTPStatusError"
        for s in tr.spans:
            if s.service == "payment_stub":
                s.duration_ms, s.status = 2.0, "error"

    w.trace_mods.append(mod)


# ---- 25: feature flag regression ----------------------------------------------------------------
def _flag_regression(w: World) -> None:
    on = -9.0
    w.changes.append({"type": "flag", "service": "shop_api", "flag": "checkout_v2_pricing", "from": False, "to": True,
                      "rollout": "50%", "minutes_before_alert": 9, "by": "product"})
    _lat(w, on, 3.4)
    _err(w, on, 0.012, ramp=1, noise=0.1)
    w.add_logs("shop_api", "INFO", "price_engine.v2 recompute took 2.8s", 80, on, 0)
    w.trace_kinds = {"POST /checkout": 0.6, "GET /orders": 0.2, "GET /products/{id}": 0.2}

    def mod(w: World, tr: Trace, m: float) -> None:
        if m < on or tr.root_name != "POST /checkout":
            return
        on_flag = w.rng.random() < 0.5
        tr.spans[0].attrs["feature.checkout_v2_pricing"] = on_flag
        if on_flag:
            tr.spans.append(_span(w, tr.spans[0].span_id, "price_engine.v2 recompute", "shop_api", 0,
                                  w.rng.uniform(2300, 3300)))

    w.trace_mods.append(mod)


# ---- 26: memory limit too low -------------------------------------------------------------------
def _mem_limit(w: World) -> None:
    on, cycle = -15.0, 1.2
    w.changes.append({"type": "limit_change", "service": "shop_api", "detail": "memory limit 512MB -> 160MB",
                      "minutes_before_alert": 15, "by": "ops"})
    w.set_metric("container_memory_limit_bytes", {"service": "shop_api"}, lambda m: (512 if m < on else 160) * MB, 0)
    w.set_metric("container_memory_rss_bytes", {"service": "shop_api"},
                 lambda m: 190 * MB if m < on else (60 + 100 * (((m - on) % cycle) / cycle)) * MB, 0.005)
    w.set_metric("container_restarts_total", {"service": "shop_api"},
                 lambda m: 0.0 if m < on else float(int((m - on) / cycle) + 1), 0)
    _err(w, on, 0.2, ramp=1)
    w.add_logs("shop_api", "INFO", "Application startup complete.", 12, on, 0, with_trace=False)
    w.add_logs("shop_api", "WARN", "upstream connection reset while reading response (502)", 60, on, 0)
    w.container_overrides["shop_api"] = {"last_exit": {"reason": "OOMKilled", "exit_code": 137,
                                                       "minutes_before_alert": 0.4},
                                         "oom_killed": True, "started_minutes_before_alert": 0.4}


# ---- 27: CPU throttling -------------------------------------------------------------------------
def _cpu_throttle(w: World) -> None:
    on = -14.0
    w.changes.append({"type": "limit_change", "service": "shop_api", "detail": "cpu quota 2.0 -> 0.2 cores",
                      "minutes_before_alert": 14, "by": "ops"})
    _lat(w, on, 2.4)
    _err(w, on, 0.01, ramp=1, noise=0.1)
    _m(w, "container_cpu_cores", {"service": "shop_api"}, on, 0.3, 0.2, noise=0.03)
    _m(w, "container_cpu_throttled_ratio", {"service": "shop_api"}, on, 0.0, 0.88, noise=0.02)
    _m(w, "event_loop_lag_seconds", {"service": "shop_api"}, on, 0.002, 0.35, noise=0.1)
    w.container_overrides["shop_api"] = {"cpu_limit_cores": 0.2}
    w.add_logs("shop_api", "WARN", "slow request GET /orders took 2.6s", 40, on, 0)


# ---- 28: clock skew -----------------------------------------------------------------------------
def _clock_skew(w: World) -> None:
    on = -6.0
    _err(w, on, 0.78, svc="worker")
    _lat(w, on, 0.9, svc="worker")
    _errlog(w, "shop_api", "jwt.exceptions.ImmatureSignatureError: The token is not yet valid (iat)", 130, on,
            "ImmatureSignatureError",
            ["services/shop_api/app/auth.py:31 in verify_internal_token", "jwt/api_jwt.py:168 in _validate_iat"])
    w.add_logs("shop_api", "WARN", "rejected internal token: iat is 598s ahead of server time (leeway 10s)", 130, on, 0)
    _errlog(w, "worker", "httpx.HTTPStatusError: Client error '401 Unauthorized' for url 'http://shop-api:8000/internal/fulfil'",
            120, on, "HTTPStatusError",
            ["services/worker/app/fulfil.py:52 in mark_fulfilled", "services/worker/app/client.py:19 in post_internal"])


HELDOUT: dict[int, Scenario] = {s.id: s for s in [
    Scenario(3, "N+1 queries", "database", "n_plus_one", _n_plus_one),
    Scenario(5, "Deadlock", "database", "deadlock", _deadlock),
    Scenario(6, "Connection limit", "database", "db_connection_limit", _conn_limit),
    Scenario(7, "Database disk full", "database", "disk_full", _disk_full),
    Scenario(9, "CPU spike", "application", "cpu_saturation", _cpu_spike),
    Scenario(10, "Blocked event loop", "application", "event_loop_blocked", _event_loop),
    Scenario(12, "Race condition, oversell", "application", "race_condition", _oversell),
    Scenario(13, "Race condition, duplicate charge", "application", "race_condition", _dup_charge),
    Scenario(14, "File descriptor leak", "application", "resource_leak", _fd_leak),
    Scenario(17, "Flaky network", "dependency", "network_flaky", _flaky),
    Scenario(18, "Retry storm", "dependency", "retry_storm", _retry_storm),
    Scenario(19, "Cache outage", "dependency", "cache_failure", _cache_down),
    Scenario(20, "Cache stampede", "dependency", "cache_stampede", _stampede),
    Scenario(22, "Timeout too low", "config", "config_error", _timeout_low),
    Scenario(23, "Bad migration", "config", "bad_migration", _bad_migration),
    Scenario(24, "Rotated credential", "config", "auth_failure", _auth_rotated),
    Scenario(25, "Feature flag regression", "config", "feature_flag", _flag_regression),
    Scenario(26, "Memory limit too low", "infrastructure", "resource_limit", _mem_limit),
    Scenario(27, "CPU throttling", "infrastructure", "resource_limit", _cpu_throttle),
    Scenario(28, "Clock skew", "infrastructure", "clock_skew", _clock_skew),
]}
