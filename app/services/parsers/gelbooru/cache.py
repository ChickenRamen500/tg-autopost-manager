"""In-memory TTL-кеш для ответов Gelbooru API (мигрировано из gbooru_tg/cache.py, ТЗ §10.1).

Снижает нагрузку на API Gelbooru (8 req/s) при повторных операциях:
  * search — кеш 10 мин (DEFAULT_TTL)
  * single post — кеш 1 час (POST_TTL)

Фреймворк-независим, без внешних зависимостей. Запуск фоновой очистки —
start_cleanup_task() (вызывается при старте приложения).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# TTL в секундах
DEFAULT_TTL = 10 * 60          # 10 минут — результаты поиска
POST_TTL = 60 * 60             # 1 час — одиночный пост (редко меняется)
CLEANUP_INTERVAL = 10 * 60     # чистка истёкших записей каждые 10 минут
MAX_CACHE_SIZE = 2000          # лимит записей в кеше (FIFO-eviction)

# key -> (value, expire_at_monotonic)
_cache: dict[str, tuple[Any, float]] = {}


def init_cache() -> None:
    """Инициализация кеша (симметрия запуска)."""
    _cache.clear()
    logger.info("In-memory API cache initialized")


def get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    value, expire_at = entry
    if time.monotonic() > expire_at:
        _cache.pop(key, None)
        return None
    return value


def set(key: str, value: Any, ttl: int = DEFAULT_TTL) -> None:
    # Простейшее FIFO-eviction — защита от неограниченного роста кеша.
    # При достижении лимита выбрасываем самую старую (первую) запись.
    if len(_cache) >= MAX_CACHE_SIZE and key not in _cache:
        try:
            _cache.pop(next(iter(_cache)))
        except (StopIteration, KeyError):
            pass
    _cache[key] = (value, time.monotonic() + ttl)


def delete(key: str) -> bool:
    return _cache.pop(key, None) is not None


def clear_all() -> int:
    removed = len(_cache)
    _cache.clear()
    if removed > 0:
        logger.info("Cleared %d cached entries", removed)
    return removed


def stats() -> dict[str, int]:
    now = time.monotonic()
    alive = sum(1 for _, expire_at in _cache.values() if now <= expire_at)
    expired = len(_cache) - alive
    return {"total": len(_cache), "alive": alive, "expired": expired}


def cleanup_expired() -> int:
    now = time.monotonic()
    expired_keys = [k for k, (_, expire_at) in _cache.items() if now > expire_at]
    for k in expired_keys:
        _cache.pop(k, None)
    if expired_keys:
        logger.info("Cleaned up %d expired cache entries", len(expired_keys))
    return len(expired_keys)


def start_cleanup_task() -> asyncio.Task:
    """Фоновая задача периодической очистки истёкших записей.

    Sync-функция: создаёт asyncio.Task и возвращает его без await,
    чтобы можно было использовать как `task = cache.start_cleanup_task()`.
    """

    async def cleanup_loop():
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            try:
                cleanup_expired()
            except Exception as e:  # noqa: BLE001 — никогда не роняем цикл
                logger.error("Cache cleanup error: %s", e)

    task = asyncio.create_task(cleanup_loop())
    logger.info("Started cache cleanup task (interval=%ds)", CLEANUP_INTERVAL)
    return task


# Обратная совместимость со старым именем из gbooru_tg
def clear_all_cache() -> int:
    return clear_all()
