import os
import time

import httpx

from services.common import telemetry as t

PAYMENT_URL = os.environ["PAYMENT_URL"]
TIMEOUT_S = float(os.environ.get("PAYMENT_TIMEOUT_MS", "5000")) / 1000


async def charge(order_id: int, amount_cents: int) -> dict:
    t0 = time.perf_counter()
    status = "error"
    try:
        async with httpx.AsyncClient(base_url=PAYMENT_URL, timeout=TIMEOUT_S) as client:
            r = await client.post(
                "/charge",
                json={"order_id": order_id, "amount_cents": amount_cents},
                headers={"X-Api-Key": os.environ["PAYMENT_API_KEY"]},
            )
            status = f"{r.status_code // 100}xx"
            r.raise_for_status()
            return r.json()
    finally:
        t.OUT_DUR.labels(t.SERVICE, "payment_stub").observe(time.perf_counter() - t0)
        t.OUT_REQS.labels(t.SERVICE, "payment_stub", status).inc()
