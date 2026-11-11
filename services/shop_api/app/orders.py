from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .models import Order, OrderItem


async def create_order(session, customer_id, items, total_cents: int | None = None):
    if total_cents is None:
        total_cents = sum(i.price_cents * i.qty for i in items)
    order = Order(customer_id=customer_id, total_cents=total_cents)
    session.add(order)
    await session.flush()
    for i in items:
        session.add(OrderItem(order_id=order.id, sku=i.sku, qty=i.qty, price_cents=i.price_cents))
    await session.flush()
    return order


async def list_orders(session, customer_id: str):
    # relies on ix_orders_customer_id; orders has ~1M rows
    rows = await session.execute(
        select(Order).options(selectinload(Order.items)).where(Order.customer_id == customer_id).limit(50)
    )
    return rows.scalars().all()
