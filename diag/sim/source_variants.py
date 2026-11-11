"""Code variants used by the held-out scenarios: small, realistic changes to files in the fake repo."""

from __future__ import annotations

from .source import CHECKOUT_BASE, COMMITS, ORDERS, RECEIPTS

P_CHECKOUT = "services/shop_api/app/checkout.py"
P_INVENTORY = "services/shop_api/app/inventory.py"
P_ORDERS = "services/shop_api/app/orders.py"
P_PAYMENT_CLIENT = "services/shop_api/app/payment_client.py"
P_RECEIPTS = "services/shop_api/app/receipts.py"
P_REFUNDS = "services/shop_api/app/refunds.py"
P_MIGRATION = "services/shop_api/migrations/0042_rename_orders_total_cents.py"


def make_commit(changes: dict[str, str]) -> dict[str, str]:
    """A commit is the base tree plus the given file changes."""
    return {**COMMITS["a41f9c3"], **changes}


ORDERS_LAZY = ORDERS.replace(
    "    rows = await session.execute(\n"
    "        select(Order).options(selectinload(Order.items)).where(Order.customer_id == customer_id).limit(50)\n"
    "    )\n"
    "    return rows.scalars().all()",
    "    rows = await session.execute(select(Order).where(Order.customer_id == customer_id).limit(50))\n"
    "    return rows.scalars().all()  # order.items is now loaded lazily, one query per order",
).replace("from sqlalchemy.orm import selectinload\n", "")

REFUNDS = '''\
from sqlalchemy import text


async def refund_order(session, order_id: int):
    async with session.begin():
        await session.execute(text("UPDATE orders SET status = 'refunded' WHERE id = :id"), {"id": order_id})
        rows = await session.execute(text("SELECT sku, qty FROM order_items WHERE order_id = :id"), {"id": order_id})
        for sku, qty in rows:
            await session.execute(text("UPDATE inventory SET qty = qty + :n WHERE sku = :sku"), {"n": qty, "sku": sku})
'''

CHECKOUT_REGEX = (
    CHECKOUT_BASE.replace(
        "from fastapi import APIRouter, Depends",
        "import re\n\nfrom fastapi import APIRouter, Depends, HTTPException",
    ).replace(
        "    async with session.begin():\n        items",
        "    if not validate_coupon(req.coupon_code):\n"
        '        raise HTTPException(422, "invalid coupon")\n'
        "    async with session.begin():\n        items",
    )
    + '''

COUPON_RE = re.compile(r"^([A-Z0-9]+)+-?$")


def validate_coupon(code: str | None) -> bool:
    return code is None or bool(COUPON_RE.match(code))
'''
)

CHECKOUT_SYNC_WEBHOOK = (
    CHECKOUT_BASE.replace(
        "from fastapi import APIRouter, Depends",
        "import os\n\nimport requests\nfrom fastapi import APIRouter, Depends",
    )
    .replace(
        "router = APIRouter()\n",
        'router = APIRouter()\nWEBHOOK_URL = os.environ.get("ORDER_WEBHOOK_URL", "https://hooks.partner.example/orders")\n',
    )
    .replace(
        "    payment = await charge(order_id=order.id, amount_cents=order.total_cents)\n",
        "    payment = await charge(order_id=order.id, amount_cents=order.total_cents)\n"
        '    requests.post(WEBHOOK_URL, json={"order_id": order.id}, timeout=10)  # notify partner\n',
    )
)

INVENTORY_RACY = '''\
import logging

from sqlalchemy import text

log = logging.getLogger(__name__)


async def reserve_inventory(session, items):
    reserved = []
    for item in items:
        row = (await session.execute(text("SELECT qty FROM inventory WHERE sku = :sku"), {"sku": item.sku})).one()
        log.info("reserve_inventory read qty=%s sku=%s", row.qty, item.sku)
        if row.qty >= item.qty:
            await session.execute(
                text("UPDATE inventory SET qty = :q WHERE sku = :sku"), {"q": row.qty - item.qty, "sku": item.sku}
            )
        reserved.append(item)
    return reserved
'''

PAYMENT_CLIENT_RETRY_ONCE = '''\
import logging
import os

import httpx

log = logging.getLogger(__name__)
PAYMENT_URL = os.environ["PAYMENT_URL"]


async def charge(order_id: int, amount_cents: int) -> dict:
    async with httpx.AsyncClient(base_url=PAYMENT_URL, timeout=1.0) as client:
        for attempt in (1, 2):
            try:
                r = await client.post(
                    "/charge",
                    json={"order_id": order_id, "amount_cents": amount_cents},
                    headers={"X-Api-Key": os.environ["PAYMENT_API_KEY"]},
                )
                r.raise_for_status()
                return r.json()
            except httpx.TimeoutException:
                log.warning("charge timed out for order %s (attempt %s), retrying", order_id, attempt)
        raise RuntimeError("payment unavailable")
'''

PAYMENT_CLIENT_RETRY_STORM = '''\
import logging
import os

import httpx

log = logging.getLogger(__name__)
PAYMENT_URL = os.environ["PAYMENT_URL"]


async def charge(order_id: int, amount_cents: int) -> dict:
    async with httpx.AsyncClient(base_url=PAYMENT_URL, timeout=2.0) as client:
        for attempt in range(1, 6):  # up to 5 attempts
            try:
                r = await client.post(
                    "/charge",
                    json={"order_id": order_id, "amount_cents": amount_cents},
                    headers={"X-Api-Key": os.environ["PAYMENT_API_KEY"]},
                )
                r.raise_for_status()
                return r.json()
            except httpx.TimeoutException:
                log.warning("charge attempt %s/5 failed (timeout), retrying immediately", attempt)
        raise RuntimeError("payment unavailable after 5 attempts")
'''

RECEIPTS_FD_LEAK = (
    RECEIPTS.replace(
        '        return {"order_id": row.id, "total_cents": row.total_cents}',
        '        return {"order_id": row.id, "total_cents": row.total_cents, "pdf": write_receipt_pdf(row.id, row)}',
    )
    + '''

def write_receipt_pdf(order_id: int, row) -> str:
    path = f"/tmp/receipts/{order_id}.pdf"
    f = open(path, "wb")
    f.write(render_pdf(row))
    return path
'''
)

MIGRATION_0042 = '''\
from alembic import op


def upgrade():
    op.alter_column("orders", "total_cents", new_column_name="total_amount_cents")


def downgrade():
    op.alter_column("orders", "total_amount_cents", new_column_name="total_cents")
'''
