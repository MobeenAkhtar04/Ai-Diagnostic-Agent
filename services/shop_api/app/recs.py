import json
import time

from sqlalchemy import text

from . import flags

_CACHE: dict[str, dict] = {}
_CLIENTS = ("web", "ios", "android", "email", "amp", "widget", "kiosk", "tv", "watch", "api")


def _render(ids: list[int], client: str) -> str:
    return json.dumps([{"id": i, "client": client, "reason": "similar to your recent orders"} for i in ids])


async def recommendations(customer_id: str, session) -> list[int]:
    if flags.enabled("enable_recs_cache"):
        key = f"{customer_id}:{time.time_ns()}"
        ids = await _compute(customer_id, session)
        _CACHE[key] = {"ids": ids, "rendered": {c: _render(ids, c) for c in _CLIENTS}}
        return ids
    return await _compute(customer_id, session)


async def _compute(customer_id: str, session) -> list[int]:
    rows = await session.execute(
        text(
            "SELECT oi.id FROM order_items oi JOIN orders o ON o.id = oi.order_id "
            "WHERE o.customer_id = :c ORDER BY oi.id DESC LIMIT 500"
        ),
        {"c": customer_id},
    )
    return [r[0] for r in rows]
