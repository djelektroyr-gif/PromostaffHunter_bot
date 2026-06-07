"""FSM storage: Redis (prod) или MemoryStorage (fallback)."""

from __future__ import annotations

import logging
import os

from aiogram.fsm.storage.memory import MemoryStorage

logger = logging.getLogger(__name__)


def create_fsm_storage():
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return MemoryStorage()
    try:
        from aiogram.fsm.storage.redis import RedisStorage
        from redis.asyncio import Redis

        storage = RedisStorage(redis=Redis.from_url(redis_url))
        logger.info("FSM storage: Redis")
        return storage
    except Exception as e:
        logger.warning("Redis FSM недоступен (%s), MemoryStorage", e)
        return MemoryStorage()
