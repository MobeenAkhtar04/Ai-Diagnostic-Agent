"""Order fulfilment worker: claims queued jobs, calls shop_api /internal/fulfil, reconciles duplicate payments."""

import asyncio
import logging
import os
import time

import asyncpg
import httpx
import jwt
from fastapi import FastAPI
from prometheus_client import Counter

from services.common import telemetry as t

t.setup_logging()
log = logging.getLogger("worker")
app = FastAPI(title="worker")
t.setup_tracing(app)

DB_DSN = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
SHOP_URL = os.environ["SHOP_API_URL"]
SECRET = os.environ.get("INTERNAL_JWT_SECRET", "dev-internal-secret")
POLL_S = float(os.environ.get("WORKER_POLL_S", "0.5"))
JOBS = Counter("worker_jobs_total", "Fulfilment jobs processed", ["service", "result"])


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    return t.metrics_response()


def make_token() -> str:
    now = int(time.time())
    return jwt.encode({"iat": now, "exp": now + 60, "sub": "worker"}, SECRET, algorithm="HS256")


async def process(conn, client: httpx.AsyncClient) -> bool:
    async with conn.transaction():
        job = await conn.fetchrow(
            "SELECT * FROM fulfilment_jobs WHERE status = 'queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
        )
        if not job:
            return False
        try:
            r = await client.post(f"{SHOP_URL}/internal/fulfil", json={"order_id": job["order_id"]},
                                  headers={"Authorization": f"Bearer {make_token()}"})
            r.raise_for_status()
            await conn.execute("UPDATE fulfilment_jobs SET status = 'done' WHERE id = $1", job["id"])
            JOBS.labels("worker", "ok").inc()
        except httpx.HTTPError as e:
            log.error(f"{type(e).__module__}.{type(e).__name__}: {e}", exc_info=e)
            await conn.execute("UPDATE fulfilment_jobs SET attempts = attempts + 1, "
                               "status = CASE WHEN attempts >= 20 THEN 'failed' ELSE 'queued' END WHERE id = $1",
                               job["id"])
            JOBS.labels("worker", "error").inc()
    return True


async def reconcile(conn) -> None:
    rows = await conn.fetch(
        "SELECT order_id, count(*) c FROM payments WHERE created_at > now() - interval '5 minutes' "
        "GROUP BY order_id HAVING count(*) > 1 LIMIT 20"
    )
    for r in rows:
        log.warning(f"reconciliation: order {r['order_id']} has {r['c']} captured payments")
    oversold = await conn.fetch("SELECT sku, qty FROM inventory WHERE qty < 0 LIMIT 5")
    for r in oversold:
        log.warning(f"reconciliation: oversold sku {r['sku']}, stock={r['qty']}")


async def run() -> None:
    conn = await asyncpg.connect(DB_DSN, server_settings={"application_name": "worker"})
    last_reconcile = 0.0
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                worked = await process(conn, client)
                if time.time() - last_reconcile > 30:
                    await reconcile(conn)
                    last_reconcile = time.time()
                if not worked:
                    await asyncio.sleep(POLL_S)
            except (asyncpg.PostgresError, OSError) as e:
                log.error(f"{type(e).__module__}.{type(e).__name__}: {e}", exc_info=e)
                await asyncio.sleep(2)
                try:
                    conn = await asyncpg.connect(DB_DSN, server_settings={"application_name": "worker"})
                except (asyncpg.PostgresError, OSError):
                    pass


@app.on_event("startup")
async def _startup():
    asyncio.create_task(run())
    asyncio.create_task(t.loop_lag_monitor())
    log.info("Application startup complete.")
