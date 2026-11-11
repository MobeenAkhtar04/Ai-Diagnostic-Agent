import asyncio
import logging
import os
import random

import asyncpg
from fastapi import FastAPI, Header, HTTPException

from services.common import telemetry as t

t.setup_logging()
log = logging.getLogger("payment_stub")
app = FastAPI(title="payment_stub")
t.instrument_http(app)
t.setup_tracing(app)

API_KEY = os.environ["PAYMENT_API_KEY"]
DB_DSN = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
BEHAVIOUR = {"delay_ms": int(os.environ.get("STUB_DELAY_MS", "40")), "error_rate": 0.0, "slow_rate": 0.0,
             "slow_ms": 1500}
_pool: asyncpg.Pool | None = None


@app.on_event("startup")
async def _startup():
    global _pool
    _pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=5, server_settings={"application_name": "payment_stub"})
    asyncio.create_task(t.loop_lag_monitor())
    log.info("Application startup complete.")


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    return t.metrics_response()


@app.post("/admin/behaviour")
async def set_behaviour(body: dict):
    """Chaos hook: delay_ms, slow_rate/slow_ms (some calls slow), error_rate (fraction of 500s)."""
    BEHAVIOUR.update({k: v for k, v in body.items() if k in BEHAVIOUR})
    return BEHAVIOUR


@app.post("/charge")
async def charge(body: dict, x_api_key: str = Header()):
    if x_api_key != API_KEY:
        log.warning("rejected POST /charge: invalid api key (401)")
        raise HTTPException(401, "invalid api key")
    delay = BEHAVIOUR["delay_ms"]
    if random.random() < BEHAVIOUR["slow_rate"]:
        delay = BEHAVIOUR["slow_ms"]
    await asyncio.sleep(delay / 1000)
    if random.random() < BEHAVIOUR["error_rate"]:
        raise HTTPException(500, "processor error")
    async with _pool.acquire() as c:  # type: ignore[union-attr]
        await c.execute("INSERT INTO payments (order_id, amount_cents) VALUES ($1, $2)",
                        body["order_id"], body.get("amount_cents", 0))
    log.info(f"POST /charge 200 order_id={body['order_id']} captured")
    return {"status": "captured", "order_id": body["order_id"]}
