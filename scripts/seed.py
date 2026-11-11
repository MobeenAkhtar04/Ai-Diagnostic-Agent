"""Seed the shop: products, inventory and a large orders table (SQL generate_series, fast).

python -m scripts.seed --orders 1000000
"""

import argparse
import asyncio
import os

import asyncpg

DSN = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")


async def main(orders: int, products: int, customers: int) -> None:
    c = await asyncpg.connect(DSN)
    await c.execute("TRUNCATE order_items, orders, inventory, products, fulfilment_jobs, payments RESTART IDENTITY CASCADE")
    await c.execute(
        "INSERT INTO products SELECT 'SKU-' || g, 'Product ' || g, 500 + (g * 37) % 9500 FROM generate_series(1, $1) g",
        products)
    await c.execute("INSERT INTO inventory SELECT sku, 1000000 FROM products")
    await c.execute("INSERT INTO products VALUES ('SKU-HOT-1', 'Hot item', 2500)")
    await c.execute("INSERT INTO inventory VALUES ('SKU-HOT-1', 1000000)")
    await c.execute(
        "INSERT INTO orders (customer_id, total_cents, status, created_at) "
        "SELECT 'cust-' || (g % $2), 1000 + (g * 13) % 20000, 'fulfilled', now() - (g || ' seconds')::interval "
        "FROM generate_series(1, $1) g", orders, customers)
    await c.execute(
        "INSERT INTO order_items (order_id, product_id, sku, qty, price_cents) "
        "SELECT o.id, 1 + (o.id % $2), 'SKU-' || (1 + (o.id % $2)), 1 + (o.id % 3), 1000 "
        "FROM orders o, generate_series(1, 2) s WHERE o.id % 20 = 0 OR o.id > $1 - 200000", orders, products)
    await c.execute("ANALYZE")
    await c.execute("SELECT pg_stat_statements_reset()")
    n = await c.fetchval("SELECT count(*) FROM orders")
    print(f"seeded {n} orders, {products} products")
    await c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", type=int, default=1_000_000)
    ap.add_argument("--products", type=int, default=200)
    ap.add_argument("--customers", type=int, default=5000)
    a = ap.parse_args()
    asyncio.run(main(a.orders, a.products, a.customers))
