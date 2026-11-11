"""Shared observability: Prometheus metrics, JSON logs (stdout + Loki), OpenTelemetry traces (Tempo).

Everything is optional and driven by env vars so the services also run bare in tests:
  OTLP_ENDPOINT   e.g. http://tempo:4318   (traces)
  LOKI_URL        e.g. http://loki:3100    (logs)
Prometheus scrapes GET /metrics on every service.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
import traceback

import httpx
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE = os.environ.get("SERVICE_NAME", "unknown")
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

HTTP_REQS = Counter("http_server_requests_total", "HTTP requests", ["service", "route", "status"])
HTTP_DUR = Histogram("http_server_duration_seconds", "HTTP latency", ["service", "route"], buckets=BUCKETS)
OUT_REQS = Counter("outbound_requests_total", "Outbound HTTP calls", ["service", "target", "status"])
OUT_DUR = Histogram("outbound_duration_seconds", "Outbound HTTP latency", ["service", "target"], buckets=BUCKETS)
DB_QUERIES = Counter("db_queries_total", "SQL statements executed", ["service"])
DB_INTEGRITY = Counter("db_integrity_errors_total", "Integrity / constraint violations", ["service"])
POOL_OUT = Gauge("db_pool_checked_out", "Pool connections in use", ["service"])
POOL_WAIT = Histogram("db_pool_wait_duration_seconds", "Wait for a pool connection", ["service"], buckets=BUCKETS)
CACHE_OPS = Counter("cache_operations_total", "Cache lookups", ["service", "result"])
LOOP_LAG = Gauge("event_loop_lag_seconds", "asyncio scheduling lag", ["service"])
OPEN_FDS = Gauge("process_open_fds_current", "Open file descriptors", ["service"])
RSS = Gauge("process_rss_bytes", "Resident memory", ["service"])


def metrics_response():
    from fastapi import Response

    _sample_process()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _sample_process() -> None:
    try:
        OPEN_FDS.labels(SERVICE).set(len(os.listdir("/proc/self/fd")))
        with open("/proc/self/statm") as f:
            RSS.labels(SERVICE).set(int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE"))
    except OSError:
        pass


async def loop_lag_monitor(interval: float = 0.25) -> None:
    """Measures how late the event loop wakes us up: the signature of a blocked loop."""
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        LOOP_LAG.labels(SERVICE).set(max(0.0, time.perf_counter() - t0 - interval))


# ---- logs ------------------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        out = {"ts": record.created, "level": record.levelname, "service": SERVICE, "msg": record.getMessage(),
               "logger": record.name,
               "trace_id": format(ctx.trace_id, "032x") if ctx and ctx.trace_id else None}
        if record.exc_info:
            et, ev, tb = record.exc_info
            out["exc_type"] = f"{et.__module__}.{et.__name__}" if et else None
            out["stack"] = "".join(traceback.format_exception(et, ev, tb))
        return json.dumps(out)


class LokiHandler(logging.Handler):
    """Batches formatted log lines and pushes them to Loki from a background thread."""

    def __init__(self, url: str):
        super().__init__()
        self.url = url.rstrip("/") + "/loki/api/v1/push"
        self.q: queue.Queue = queue.Queue(maxsize=10000)
        threading.Thread(target=self._run, daemon=True).start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait((time.time_ns(), record.levelname.lower(), self.format(record)))
        except queue.Full:
            pass

    def _run(self) -> None:
        client = httpx.Client(timeout=5)
        while True:
            batch = [self.q.get()]
            time.sleep(0.5)
            while len(batch) < 500:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            streams: dict[str, list] = {}
            for ts, lvl, line in batch:
                streams.setdefault(lvl, []).append([str(ts), line])
            body = {"streams": [{"stream": {"service": SERVICE, "level": lvl}, "values": v} for lvl, v in streams.items()]}
            try:
                client.post(self.url, json=body)
            except httpx.HTTPError:
                pass


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fmt = JsonFormatter()
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if os.environ.get("LOKI_URL"):
        lh = LokiHandler(os.environ["LOKI_URL"])
        lh.setFormatter(fmt)
        root.addHandler(lh)
    logging.getLogger("uvicorn.access").disabled = True


# ---- traces ------------------------------------------------------------------------------------
def setup_tracing(app=None, engine=None) -> None:
    endpoint = os.environ.get("OTLP_ENDPOINT")
    if not endpoint:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,health")
    if engine is not None:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()
    except Exception:  # noqa: BLE001
        pass


def instrument_http(app) -> None:
    """Middleware that records per-route latency and status for the RED metrics."""

    @app.middleware("http")
    async def _mw(request, call_next):
        t0 = time.perf_counter()
        status = 500
        try:
            resp = await call_next(request)
            status = resp.status_code
            return resp
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", request.url.path)
            if path not in ("/metrics", "/health") and not path.startswith("/admin"):
                dur = time.perf_counter() - t0
                HTTP_DUR.labels(SERVICE, path).observe(dur)
                HTTP_REQS.labels(SERVICE, path, f"{status // 100}xx").inc()
                logging.getLogger("access").info(f"{request.method} {path} {status} {dur * 1000:.0f}ms")
