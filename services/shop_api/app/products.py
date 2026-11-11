from sqlalchemy import text

from .cache import get_cached


async def get_product(session, sku: str) -> dict | None:
    async def load():
        row = (await session.execute(text("SELECT sku, name, price_cents FROM products WHERE sku = :s"),
                                     {"s": sku})).first()
        return dict(row._mapping) if row else None

    return await get_cached(f"product:{sku}", load, ttl_s=30)
