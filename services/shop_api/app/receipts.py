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
