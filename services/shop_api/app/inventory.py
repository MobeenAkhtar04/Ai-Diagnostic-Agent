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
