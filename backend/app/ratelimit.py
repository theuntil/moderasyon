"""Project bazlı, Redis üzerinde sabit pencereli (fixed window) rate limit.

Her project için saniyelik ve dakikalık sayaç tutulur. Birden fazla API
instance aynı Redis'i kullandığı için limit tüm instance'lar genelinde geçerlidir.
"""
import logging
import time
from dataclasses import dataclass
from uuid import UUID

log = logging.getLogger("ratelimit")


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_s: int


async def check_rate_limit(redis, project_id: UUID, per_second: int, per_minute: int) -> RateLimitResult:
    now = time.time()
    sec_window = int(now)
    min_window = int(now // 60)
    sec_key = f"rl:{project_id}:s:{sec_window}"
    min_key = f"rl:{project_id}:m:{min_window}"

    try:
        pipe = redis.pipeline(transaction=False)
        pipe.incr(sec_key)
        pipe.expire(sec_key, 2)
        pipe.incr(min_key)
        pipe.expire(min_key, 61)
        sec_count, _, min_count, _ = await pipe.execute()
    except Exception:
        # Redis erişilemezse API'yi tamamen durdurmak yerine geçir (fail-open) ve logla.
        # Redis yoksa kuyruk da çalışmayacağı için istekler zaten 'queued' kalır.
        log.exception("rate limit check failed, allowing request")
        return RateLimitResult(True, per_minute, per_minute, 0)

    if sec_count > per_second:
        return RateLimitResult(False, per_second, 0, 1)
    if min_count > per_minute:
        return RateLimitResult(False, per_minute, 0, max(1, 60 - int(now % 60)))
    return RateLimitResult(True, per_minute, max(0, per_minute - min_count), 0)
