import asyncio
import logging
import os

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from services.common import telemetry as t

from . import auth, checkout, flags, orders, products, receipts, recs
from .db import engine, get_session

t.setup_logging()
log = logging.getLogger("shop_api")
app = FastAPI(title="shop_api")
t.instrument_http(app)
t.setup_tracing(app, engine)
app.include_router(checkout.router)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(t.loop_lag_monitor())
    log.info("Application startup complete.")


@app.exception_handler(Exception)
async def _unhandled(request, exc):
    from fastapi.responses import JSONResponse

    if isinstance(exc, IntegrityError):
        t.DB_INTEGRITY.labels(t.SERVICE).inc()
    log.error(f"{type(exc).__module__}.{type(exc).__name__}: {exc}".split("\n")[0][:300], exc_info=exc)
    return JSONResponse({"error": "internal"}, status_code=500)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    return t.metrics_response()


@app.get("/products/{sku}")
async def product(sku: str, session: AsyncSession = Depends(get_session)):
    p = await products.get_product(session, sku)
    if not p:
        raise HTTPException(404, "no such product")
    return p


@app.get("/orders")
async def list_orders(customer_id: str, session: AsyncSession = Depends(get_session)):
    rows = await orders.list_orders(session, customer_id)
    return [{"id": o.id, "total_cents": o.total_cents, "items": len(o.items)} for o in rows]


@app.get("/receipts/{order_id}")
async def receipt(order_id: int, session: AsyncSession = Depends(get_session)):
    return await receipts.render_receipt(session, order_id)


@app.get("/recs/{customer_id}")
async def recommendations(customer_id: str, session: AsyncSession = Depends(get_session)):
    return {"ids": await recs.recommendations(customer_id, session)}


@app.post("/internal/fulfil")
async def fulfil(body: dict, authorization: str = Header(), session: AsyncSession = Depends(get_session)):
    try:
        auth.verify_internal_token(authorization.removeprefix("Bearer "))
    except jwt.PyJWTError as e:
        log.warning(f"rejected internal token: {e}")
        log.error(f"{type(e).__module__}.{type(e).__name__}: {e}", exc_info=e)
        raise HTTPException(401, "bad token") from e
    async with session.begin():
        await session.execute(text("UPDATE orders SET status = 'fulfilled' WHERE id = :i"), {"i": body["order_id"]})
    return {"ok": True}


@app.get("/admin/flags")
async def show_flags():
    return {"enable_recs_cache": flags.enabled("enable_recs_cache"), "sha": os.environ.get("GIT_SHA", "dev")}
