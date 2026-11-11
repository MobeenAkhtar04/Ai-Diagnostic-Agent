"""A tiny fake monorepo at three commits, so read_source and git_diff have real code to show.

Commit order: a41f9c3 (base) -> 9e0d114 (payment_stub tweak) -> b7c1e2a (checkout bug, scenario 11).
"""

from __future__ import annotations

CHECKOUT_BASE = '''\
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .inventory import reserve_inventory
from .orders import create_order
from .payment_client import charge
from .schemas import CheckoutRequest

router = APIRouter()


@router.post("/checkout")
async def checkout(req: CheckoutRequest, session: AsyncSession = Depends(get_session)):
    async with session.begin():
        items = await reserve_inventory(session, req.items)
        order = await create_order(session, req.customer_id, items)
    payment = await charge(order_id=order.id, amount_cents=order.total_cents)
    return build_response(order, payment, req.model_dump())


def build_response(order, payment, body: dict) -> dict:
    shipping = body.get("shipping_address") or {}
    return {
        "order_id": order.id,
        "status": payment["status"],
        "ship_to": shipping.get("city", "pickup"),
    }
'''

CHECKOUT_BAD = CHECKOUT_BASE.replace(
    '''    shipping = body.get("shipping_address") or {}
    return {
        "order_id": order.id,
        "status": payment["status"],
        "ship_to": shipping.get("city", "pickup"),
    }''',
    '''    # show estimated delivery on the confirmation page
    shipping = body["shipping_address"]
    return {
        "order_id": order.id,
        "status": payment["status"],
        "ship_to": shipping["city"],
        "eta_days": estimate_eta(shipping["postal_code"]),
    }


def estimate_eta(postal_code: str) -> int:
    return 2 if postal_code.startswith(("M", "L")) else 5''',
)

INVENTORY = '''\
from sqlalchemy import text


async def reserve_inventory(session, items):
    reserved = []
    for item in items:
        await session.execute(
            text("UPDATE inventory SET qty = qty - :n WHERE sku = :sku AND qty >= :n"),
            {"n": item.qty, "sku": item.sku},
        )
        reserved.append(item)
    return reserved
'''

ORDERS = '''\
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .models import Order


async def create_order(session, customer_id, items):
    order = Order(customer_id=customer_id, total_cents=sum(i.price_cents * i.qty for i in items))
    session.add(order)
    await session.flush()
    return order


async def list_orders(session, customer_id: str):
    # relies on ix_orders_customer_id; orders has ~1M rows
    rows = await session.execute(
        select(Order).options(selectinload(Order.items)).where(Order.customer_id == customer_id).limit(50)
    )
    return rows.scalars().all()
'''

RECS = '''\
import time

from . import flags

_CACHE: dict[str, list[int]] = {}


async def recommendations(customer_id: str, session) -> list[int]:
    if flags.enabled("enable_recs_cache"):
        key = f"{customer_id}:{time.time_ns()}"
        _CACHE[key] = await _compute(customer_id, session)
        return _CACHE[key]
    return await _compute(customer_id, session)


async def _compute(customer_id: str, session) -> list[int]:
    rows = await session.execute(
        "SELECT product_id FROM order_items oi JOIN orders o ON o.id = oi.order_id "
        "WHERE o.customer_id = :c ORDER BY oi.id DESC LIMIT 500",
        {"c": customer_id},
    )
    return [r[0] for r in rows]
'''

PAYMENT_CLIENT = '''\
import os

import httpx

PAYMENT_URL = os.environ["PAYMENT_URL"]
TIMEOUT_S = float(os.environ.get("PAYMENT_TIMEOUT_MS", "5000")) / 1000


async def charge(order_id: int, amount_cents: int) -> dict:
    async with httpx.AsyncClient(base_url=PAYMENT_URL, timeout=TIMEOUT_S) as client:
        r = await client.post(
            "/charge",
            json={"order_id": order_id, "amount_cents": amount_cents},
            headers={"X-Api-Key": os.environ["PAYMENT_API_KEY"]},
        )
        r.raise_for_status()
        return r.json()
'''

CACHE = '''\
import json
import os

from redis.asyncio import Redis

redis = Redis.from_url(os.environ["REDIS_URL"])


async def get_cached(key: str, loader, ttl_s: int = 60):
    hit = await redis.get(key)
    if hit is not None:
        return json.loads(hit)
    value = await loader()
    await redis.set(key, json.dumps(value), ex=ttl_s)
    return value
'''

RECEIPTS = '''\
import asyncio
import os

from sqlalchemy import text


async def render_receipt(session, order_id: int) -> dict:
    async with session.begin():
        row = (await session.execute(
            text("SELECT id, total_cents FROM orders WHERE id = :id"), {"id": order_id}
        )).one()
        await asyncio.sleep(float(os.environ.get("RECEIPT_RENDER_DELAY_S", "0")))
        return {"order_id": row.id, "total_cents": row.total_cents}
'''

PAYMENT_STUB_BASE = '''\
from fastapi import FastAPI, Header, HTTPException

app = FastAPI()
API_KEY = __import__("os").environ["PAYMENT_API_KEY"]


@app.post("/charge")
async def charge(body: dict, x_api_key: str = Header()):
    if x_api_key != API_KEY:
        raise HTTPException(401, "invalid api key")
    return {"status": "captured", "order_id": body["order_id"]}
'''

PAYMENT_STUB_NEW = PAYMENT_STUB_BASE.replace(
    'return {"status": "captured", "order_id": body["order_id"]}',
    'return {"status": "captured", "order_id": body["order_id"], "processor": "stub-v2"}',
)

_BASE = {
    "services/shop_api/app/checkout.py": CHECKOUT_BASE,
    "services/shop_api/app/inventory.py": INVENTORY,
    "services/shop_api/app/orders.py": ORDERS,
    "services/shop_api/app/recs.py": RECS,
    "services/shop_api/app/payment_client.py": PAYMENT_CLIENT,
    "services/shop_api/app/cache.py": CACHE,
    "services/shop_api/app/receipts.py": RECEIPTS,
    "services/payment_stub/app/main.py": PAYMENT_STUB_BASE,
}

COMMITS: dict[str, dict[str, str]] = {
    "a41f9c3": dict(_BASE),
    "9e0d114": {**_BASE, "services/payment_stub/app/main.py": PAYMENT_STUB_NEW},
    "b7c1e2a": {**_BASE, "services/payment_stub/app/main.py": PAYMENT_STUB_NEW,
                "services/shop_api/app/checkout.py": CHECKOUT_BAD},
}

COMMIT_MESSAGES = {
    "a41f9c3": "orders: paginate order history",
    "9e0d114": "payment_stub: tag processor version in response",
    "b7c1e2a": "checkout: show delivery ETA on confirmation",
}


def line_of(content: str, needle: str) -> int:
    for i, line in enumerate(content.splitlines(), start=1):
        if needle in line:
            return i
    return 1
