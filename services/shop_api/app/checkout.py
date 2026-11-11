from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from . import flags
from .db import get_session
from .inventory import reserve_inventory
from .orders import create_order
from .payment_client import charge
from .pricing import price
from .schemas import CheckoutRequest

router = APIRouter()


@router.post("/checkout")
async def checkout(req: CheckoutRequest, session: AsyncSession = Depends(get_session)):
    total = await price(req.items, req.customer_id, flags.in_rollout("checkout_v2_pricing", req.customer_id))
    async with session.begin():
        items = await reserve_inventory(session, req.items)
        order = await create_order(session, req.customer_id, items, total)
    payment = await charge(order_id=order.id, amount_cents=order.total_cents)
    return build_response(order, payment, req.model_dump())


def build_response(order, payment, body: dict) -> dict:
    shipping = body.get("shipping_address") or {}
    return {
        "order_id": order.id,
        "status": payment["status"],
        "ship_to": shipping.get("city", "pickup"),
    }
