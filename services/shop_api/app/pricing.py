"""Checkout pricing. v2 recomputes tiered and bundle discounts from the full rule table (behind checkout_v2_pricing)."""

import asyncio
import logging
import time

from opentelemetry import trace

log = logging.getLogger("shop_api.pricing")
tracer = trace.get_tracer("shop_api.pricing")

RULES = [(tier, pct) for tier in range(1, 400) for pct in (5, 10, 15)]


def _recompute_v2(items) -> int:
    total = 0
    for item in items:
        best = item.price_cents * item.qty
        # evaluates every rule against every quantity break: O(rules x breaks) per item
        for tier, pct in RULES:
            for qty_break in range(1, 15000):
                if item.qty >= qty_break and tier % qty_break == 0:
                    best = min(best, item.price_cents * item.qty * (100 - pct) // 100)
        total += best
    return total


async def price(items, customer_id: str, v2: bool) -> int:
    if not v2:
        return sum(i.price_cents * i.qty for i in items)
    with tracer.start_as_current_span("price_engine.v2 recompute") as span:
        span.set_attribute("feature.checkout_v2_pricing", True)
        t0 = time.perf_counter()
        total = await asyncio.to_thread(_recompute_v2, items)
        log.info(f"price_engine.v2 recompute took {time.perf_counter() - t0:.1f}s")
        return total
