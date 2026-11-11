import json
import os

from redis.asyncio import Redis

from services.common import telemetry as t

redis = Redis.from_url(os.environ["REDIS_URL"])


async def get_cached(key: str, loader, ttl_s: int = 60):
    hit = await redis.get(key)
    if hit is not None:
        t.CACHE_OPS.labels(t.SERVICE, "hit").inc()
        return json.loads(hit)
    t.CACHE_OPS.labels(t.SERVICE, "miss").inc()
    value = await loader()
    await redis.set(key, json.dumps(value), ex=ttl_s)
    return value
