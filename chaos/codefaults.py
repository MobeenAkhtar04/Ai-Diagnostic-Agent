"""Held-out batch B: faults that are real code deploys (a git commit, an image rebuild, a redeploy).

#3 N+1 queries, #9 CPU hog, #10 blocked event loop, #14 file descriptor leak. The agent's prompt was never tuned on these.
"""

from __future__ import annotations

import httpx

from .deploy import ship
from .util import STUB, ChaosError, compose, recreate

ORDERS = "services/shop_api/app/orders.py"
CHECKOUT = "services/shop_api/app/checkout.py"
RECEIPTS = "services/shop_api/app/receipts.py"


def _sub(text: str, old: str, new: str, path: str) -> str:
    if text.count(old) != 1:
        raise ChaosError(f"{path} no longer has the expected code ({old.strip()[:50]!r}); update chaos/codefaults.py")
    return text.replace(old, new)


# ---- 3 N+1: show each order's items, product names and payment status ----------------------------------------------
def patch_n_plus_one(text: str) -> str:
    text = _sub(text, "from sqlalchemy import select\nfrom sqlalchemy.orm import selectinload\n",
                "from sqlalchemy import select, text\nfrom sqlalchemy.orm.attributes import set_committed_value\n", ORDERS)
    head = text[: text.index("async def list_orders")]
    return head + '''async def list_orders(session, customer_id: str):
    # relies on ix_orders_customer_id; orders has ~1M rows
    rows = await session.execute(select(Order).where(Order.customer_id == customer_id).limit(50))
    result = rows.scalars().all()
    for order in result:
        items = (await session.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
        for item in items:
            await session.execute(text("SELECT name FROM products WHERE sku = :s"), {"s": item.sku})
        await session.execute(text("SELECT count(*) FROM payments WHERE order_id = :i"), {"i": order.id})
        set_committed_value(order, "items", items)
    return result
'''


def inj_n_plus_one() -> dict:
    return ship(ORDERS, patch_n_plus_one, "orders: include product names and payment status per order")


# ---- 9 CPU hog: a "fraud check" regex with catastrophic backtracking, run in worker processes ----------------------
def patch_cpu_hog(text: str) -> str:
    text = _sub(text, "from fastapi import APIRouter, Depends\n",
                "import asyncio\nimport re\nfrom concurrent.futures import ProcessPoolExecutor\n\n"
                "from fastapi import APIRouter, Depends\n", CHECKOUT)
    text = _sub(text, "router = APIRouter()\n", '''router = APIRouter()
_POOL = ProcessPoolExecutor(max_workers=2)


def _risk_score(customer_id: str) -> int:
    """Rough fraud heuristic on the customer id."""
    probe = "a" * 23 + "!"
    return 0 if re.match(r"^(a+)+$", probe) else len(customer_id)
''', CHECKOUT)
    return _sub(text, "    total = await price(",
                "    await asyncio.get_running_loop().run_in_executor(_POOL, _risk_score, req.customer_id)\n"
                "    total = await price(", CHECKOUT)


def inj_cpu_hog() -> dict:
    return ship(CHECKOUT, patch_cpu_hog, "checkout: add fraud heuristic before pricing")


# ---- 10 blocked event loop: a synchronous partner notification inside the async handler -----------------------------
def patch_event_loop(text: str) -> str:
    text = _sub(text, "from fastapi import APIRouter, Depends\n",
                "import os\n\nimport httpx\nfrom fastapi import APIRouter, Depends\n", CHECKOUT)
    return _sub(text, "    payment = await charge(",
                '    httpx.post(f"{os.environ[\'PAYMENT_URL\']}/partner/notify", json={"order_id": order.id}, timeout=5)\n'
                "    payment = await charge(", CHECKOUT)


def _ensure_stub_notify() -> None:
    def ok() -> bool:
        try:
            return httpx.post(f"{STUB}/partner/notify", json={"order_id": 0}, timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    if ok():
        return
    compose("build", "payment_stub", timeout=600)
    recreate("payment_stub")
    if not ok():
        raise ChaosError("payment_stub has no /partner/notify endpoint even after a rebuild")


def inj_event_loop() -> dict:
    _ensure_stub_notify()
    return ship(CHECKOUT, patch_event_loop, "checkout: notify partner webhook after creating the order")


# ---- 14 FD leak: receipts keep an audit file handle open for every request ---------------------------------------------
def patch_fd_leak(text: str) -> str:
    text = _sub(text, "async def render_receipt", "_AUDIT_FILES: list = []\n\n\nasync def render_receipt", RECEIPTS)
    return _sub(text, '        return {"order_id": row.id, "total_cents": row.total_cents}',
                '        audit = open(f"/tmp/receipt-{row.id}.log", "a")  # noqa: SIM115\n'
                '        audit.write(f"rendered {row.id}\\n")\n'
                "        _AUDIT_FILES.append(audit)  # handed to the batch flusher\n"
                '        return {"order_id": row.id, "total_cents": row.total_cents}', RECEIPTS)


def inj_fd_leak() -> dict:
    return ship(RECEIPTS, patch_fd_leak, "receipts: write an audit line per rendered receipt")
