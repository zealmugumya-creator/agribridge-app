# Copyright (C) 2026 Mugumya Zeal. All rights reserved.

"""Redis-backed sliding-window rate limiter (mandate: replace in-process limiter).

Why: the existing `_rl_buckets` dict in app.py is per-worker — with 2+ gunicorn
workers each has its own dict, so a client can make N*workers requests before
being throttled. This module uses Redis so the counter is shared across all workers.

Falls back to the in-process limiter when Redis is unavailable, so the service
never crashes because of a missing cache.

Usage in app.py:
    from services.rate_limit import RateLimiter
    limiter = RateLimiter(redis_url=os.environ.get('REDIS_URL'))

    @app.route('/api/some-endpoint')
    @limiter.limit(max_req=30, window=60)
    def some_endpoint():
        ...
"""
from __future__ import annotations

import time
from collections import defaultdict
from functools import wraps
from typing import Any

from flask import jsonify, request

from services.logging import get_logger

log = get_logger(__name__)


class RateLimiter:
    def __init__(self, redis_url: str | None = None):
        self._redis: Any = None
        self._fallback: dict = defaultdict(list)

        if redis_url:
            try:
                import redis  # type: ignore
                self._redis = redis.from_url(redis_url, decode_responses=True,
                                             socket_timeout=1, socket_connect_timeout=1)
                self._redis.ping()
                log.info("rate_limit.redis_connected")
            except Exception as e:
                log.warning("rate_limit.redis_unavailable", error=str(e),
                            note="Falling back to in-process limiter")
                self._redis = None

    def _client_key(self, route_name: str) -> str:
        fwd = request.headers.get('X-Forwarded-For', '')
        ip = fwd.split(',')[0].strip() if fwd else (request.remote_addr or 'unknown')
        return f"rl:{route_name}:{ip}"

    def _check_redis(self, key: str, max_req: int, window: int) -> bool:
        """Returns True if the request is allowed. Uses Redis ZADD/ZREMRANGEBYSCORE."""
        import redis as redis_lib  # noqa: PLC0415
        now = time.time()
        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, 0, now - window)
        pipe.zadd(key, {str(now): now})
        pipe.zcard(key)
        pipe.expire(key, window + 1)
        try:
            results = pipe.execute()
            count = results[2]
            return count <= max_req
        except redis_lib.RedisError:
            return True  # fail open on Redis errors

    def _check_local(self, key: str, max_req: int, window: int) -> bool:
        now = time.time()
        bucket = self._fallback[key]
        cutoff = now - window
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= max_req:
            return False
        bucket.append(now)
        return True

    def limit(self, max_req: int = 30, window: int = 60):
        """Decorator: @limiter.limit(max_req=20, window=60)"""
        def deco(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                key = self._client_key(fn.__name__)
                allowed = (
                    self._check_redis(key, max_req, window)
                    if self._redis else
                    self._check_local(key, max_req, window)
                )
                if not allowed:
                    return jsonify({'error': 'Too many requests. Please slow down.'}), 429
                return fn(*args, **kwargs)
            return wrapper
        return deco
