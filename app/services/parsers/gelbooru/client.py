"""Gelbooru API client с rate limiting (мигрировано из gbooru_tg/gelbooru.py, ТЗ §10.1).

Что перенесено как есть:
- Rate limiting 8 req/s (защита от бана API Gelbooru — не связан с лимитами Telegram)
- Методы search_posts / get_post / get_post_tags / get_tags_by_names / fetch_tags_page
- In-memory кеш (cache.py): 10 мин для поиска, 1 час для постов

Что адаптировано (ТЗ §10.3):
- Конфиг читается из app.config.settings (а не из config.py gbooru_tg)
- api_key/user_id опциональны — клиент работает и без них (публичные endpoints)
- Подключение к локальному Bot API Server НЕ нужно: это отдельный HTTP-клиент к gelbooru.com

Сам автопарсинг подписок (polling tag_subscriptions) — Фаза 3 (worker).
Здесь клиент доступен как библиотека для будущих фаз и для populate_tags.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from app.config.settings import settings
from app.services.parsers.gelbooru import cache

logger = logging.getLogger(__name__)

GELBOORU_API_URL = "https://gelbooru.com/index.php"
RATE_LIMIT = 8  # запросов в секунду
RATE_LIMIT_INTERVAL = 1.0 / RATE_LIMIT  # ~125 мс


class GelbooruClient:
    """Асинхронный клиент Gelbooru API с rate limiting."""

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(RATE_LIMIT)
        self._lock = asyncio.Lock()
        self._last_request_time: float = 0.0
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def _rate_limit(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < RATE_LIMIT_INTERVAL:
                await asyncio.sleep(RATE_LIMIT_INTERVAL - elapsed)
            self._last_request_time = time.monotonic()

    async def _request(self, params: dict[str, Any]) -> list | dict | None:
        async with self._semaphore:
            await self._rate_limit()
            session = await self._get_session()

            safe_params = {k: ("***" if k == "api_key" else v) for k, v in params.items()}
            logger.debug("Gelbooru request: %s", safe_params)

            # api_key/user_id опциональны (ТЗ §10: gbooru_tg требовал их, здесь — нет)
            if settings.gelbooru_api_key:
                params["api_key"] = settings.gelbooru_api_key
            if settings.gelbooru_user_id:
                params["user_id"] = settings.gelbooru_user_id
            params["json"] = "1"

            try:
                async with session.get(GELBOORU_API_URL, params=params) as response:
                    body = await response.read()
                    if response.status == 200:
                        logger.info("Gelbooru response: status=%d, size=%d bytes", response.status, len(body))
                        data = await response.json()
                        if isinstance(data, dict) and "@attributes" in data:
                            attrs = data["@attributes"]
                            if isinstance(attrs, dict) and "count" in attrs:
                                logger.debug("Gelbooru API count: %s", attrs["count"])
                        return data
                    if response.status == 404:
                        logger.info("Gelbooru response: status=404")
                        return None
                    logger.warning("Gelbooru API returned status %d", response.status)
                    return None
            except TimeoutError:
                logger.error("Gelbooru API request timed out")
                return None
            except Exception as e:  # noqa: BLE001 — сеть/API, не роняем запрос
                logger.error("Gelbooru API request failed: %s", e)
                return None

    async def search_posts(self, tags: str, pid: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        """Поиск постов. Результат кешируется 10 минут (пустой — 60 сек)."""
        cache_key = f"search:{tags}|{pid}|{limit}"
        cached = cache.get(cache_key)
        if cached is not None:
            logger.info("search_posts: %d posts (CACHE HIT)", len(cached))
            return cached

        params = {"page": "dapi", "s": "post", "q": "index", "tags": tags, "pid": pid, "limit": limit}
        logger.info("search_posts: tags=%r, pid=%d, limit=%d", tags, pid, limit)

        data = await self._request(params)
        posts: list[dict[str, Any]] = []
        if data is None:
            logger.info("search_posts: 0 posts (data=None)")
        elif isinstance(data, list):
            posts = data
        elif isinstance(data, dict):
            if "post" in data:
                raw = data["post"]
                if isinstance(raw, list):
                    posts = raw
                elif isinstance(raw, dict):
                    posts = [raw]
            elif "id" in data:
                posts = [data]

        cache.set(cache_key, posts, ttl=cache.DEFAULT_TTL if posts else 60)
        return posts

    async def get_post(self, post_id: int) -> dict[str, Any] | None:
        """Один пост по ID. Кеш 1 час (посты редко меняются)."""
        cache_key = f"post:{post_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            logger.info("get_post: %d (CACHE HIT)", post_id)
            return cached

        params = {"page": "dapi", "s": "post", "q": "index", "id": post_id, "limit": 1}
        data = await self._request(params)
        post: dict[str, Any] | None = None
        if data is None:
            return None
        if isinstance(data, list) and len(data) > 0:
            post = data[0]
        elif isinstance(data, dict):
            if "post" in data:
                raw = data["post"]
                if isinstance(raw, list) and len(raw) > 0:
                    post = raw[0]
                elif isinstance(raw, dict):
                    post = raw
            elif "id" in data:
                post = data

        if post is not None:
            cache.set(cache_key, post, ttl=cache.POST_TTL)
        return post

    # ---- Tags API (s=tag) — типы тегов и автодополнение ----

    async def get_post_tags(self, post_id: int) -> list[dict[str, Any]]:
        """Все теги поста С типами (artist/character/...). Один API-вызов."""
        cache_key = f"post_tags:{post_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            logger.info("get_post_tags: %d (CACHE HIT, %d tags)", post_id, len(cached))
            return cached

        params = {"page": "dapi", "s": "tag", "q": "index", "tags": f"post_id:{post_id}"}
        data = await self._request(params)
        tags = self._parse_tag_response(data)
        if tags:
            cache.set(cache_key, tags, ttl=cache.POST_TTL)
        return tags

    async def get_tags_by_names(self, names: list[str]) -> list[dict[str, Any]]:
        """Метаданные тегов (type, count) по списку имён. Чанками по 100."""
        clean = [n.strip() for n in names if n and n.strip()]
        if not clean:
            return []
        cache_key = "tag_names:" + "|".join(sorted(clean))
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        all_tags: list[dict[str, Any]] = []
        chunk_size = 100
        for i in range(0, len(clean), chunk_size):
            batch = clean[i : i + chunk_size]
            params = {"page": "dapi", "s": "tag", "q": "index", "names": " ".join(batch)}
            data = await self._request(params)
            all_tags.extend(self._parse_tag_response(data))

        if all_tags:
            cache.set(cache_key, all_tags, ttl=cache.POST_TTL)
        return all_tags

    async def fetch_tags_page(self, pid: int = 0, limit: int = 100, orderby: str = "count") -> tuple[list[dict[str, Any]], int]:
        """Одна страница тегов (max 100). Возвращает (tags, total_count)."""
        params = {"page": "dapi", "s": "tag", "q": "index", "pid": pid, "limit": limit, "orderby": orderby}
        data = await self._request(params)
        tags = self._parse_tag_response(data)
        total = 0
        if isinstance(data, dict) and "@attributes" in data:
            total = data["@attributes"].get("count", 0) or 0
        return tags, total

    @staticmethod
    def _parse_tag_response(data: list | dict | None) -> list[dict[str, Any]]:
        if data is None:
            return []
        if isinstance(data, list):
            return [t for t in data if isinstance(t, dict) and t.get("name")]
        if isinstance(data, dict):
            if "tag" in data:
                raw = data["tag"]
                if isinstance(raw, list):
                    return [t for t in raw if isinstance(t, dict) and t.get("name")]
                if isinstance(raw, dict) and raw.get("name"):
                    return [raw]
            if data.get("name"):
                return [data]
        return []

    async def check_file_alive(self, file_url: str) -> bool:
        """HEAD-запрос: доступен ли файл (Referer/UA обязательны — gelbooru отдаёт 403 без них)."""
        session = await self._get_session()
        try:
            async with session.head(
                file_url,
                allow_redirects=True,
                headers={
                    "Referer": "https://gelbooru.com/",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                },
            ) as response:
                if response.status != 200:
                    return False
                content_type = response.headers.get("Content-Type", "")
                return content_type.startswith(("image/", "video/"))
        except Exception as e:  # noqa: BLE001 — HEAD-проверка, не роняем
            logger.warning("Failed to check file %s: %s", file_url, e)
            return False

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# Глобальный singleton-клиент (как в gbooru_tg)
gelbooru_client = GelbooruClient()
