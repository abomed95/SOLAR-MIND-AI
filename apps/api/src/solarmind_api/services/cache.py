"""Read-through cache for expensive, tenant-scoped analytics (Redis).

Rules that keep it safe:

* every key starts with the organization id: one tenant can never be served
  another tenant's entry, even through a bug in the caller's key;
* authorization runs *before* the cache is consulted (route dependencies),
  so a cached entry is never a way around a permission check;
* anything that changes the result (plan features, parameters) is part of
  the key;
* Redis failures are not errors: the value is computed and served uncached.

Freshness is bounded by the TTL only. Dashboards show ``generated_at``, and
the ``X-Cache`` response header tells whether the value came from the cache.
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.encoders import jsonable_encoder
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

VERSION = "v1"  # bump when the shape of a cached payload changes


def key(org: uuid.UUID, name: str, *parts: object) -> str:
    return ":".join(["cache", VERSION, str(org), name, *(str(p) for p in parts)])


async def get_or_compute(
    redis: Redis, cache_key: str, ttl_s: int, compute: Callable[[], Awaitable[Any]]
) -> tuple[Any, bool]:
    """Return ``(value, hit)``. The value is JSON-compatible either way, so a
    hit and a miss produce byte-identical responses."""
    if ttl_s <= 0:
        return jsonable_encoder(await compute()), False
    try:
        raw = await redis.get(cache_key)
    except RedisError:
        logger.warning("cache unavailable", extra={"op": "get"})
        raw = None
    if raw is not None:
        return json.loads(raw), True
    value = jsonable_encoder(await compute())
    try:
        await redis.set(cache_key, json.dumps(value), ex=ttl_s)
    except RedisError:
        logger.warning("cache unavailable", extra={"op": "set"})
    return value, False


async def invalidate_org(redis: Redis, org: uuid.UUID) -> None:
    """Drop every cached entry of one organization (e.g. after a site edit)."""
    try:
        async for k in redis.scan_iter(match=key(org, "*"), count=200):
            await redis.delete(k)
    except RedisError:
        logger.warning("cache unavailable", extra={"op": "invalidate"})
