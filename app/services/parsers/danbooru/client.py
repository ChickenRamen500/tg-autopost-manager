"""Danbooru парсер (ТЗ §2.1.4, Фаза 3 этап 5).

API: https://danbooru.donmai.us/posts.json?tags=<query>&limit=<n>
Аутентификация: опционально (login + api_key), без неё — rate limit 1 req/s.

Возвращает посты с полями: id, file_url, large_file_url, preview_file_url,
file_ext, tag_string, rating, source, media_asset_id, ...

Особенности:
  - file_url может быть пустым для удалённых/ограниченных постов → пропускаем;
  - rating: 'g' (general), 's' (sensitive), 'q' (questionable), 'e' (explicit);
  - tags: tag_string (строка через пробел);
  - Referer для скачивания: https://danbooru.donmai.us/
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from app.config.settings import settings
from app.services.parsers.base import BaseParser, ExternalPost, file_type_from_extension

log = logging.getLogger("parsers.danbooru")

DANBOORU_API_URL = "https://danbooru.donmai.us/posts.json"
DANBOORU_REFERER = "https://danbooru.donmai.us/"

_RATING_MAP = {
    "g": "safe",
    "s": "safe",
    "q": "questionable",
    "e": "explicit",
}

_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; tg-autopost-manager/1.0)",
    "Accept": "application/json",
    "Referer": DANBOORU_REFERER,
}


class DanbooruParser(BaseParser):
    """Асинхронный клиент Danbooru API."""

    source_type = "danbooru"

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20, connect=10, sock_read=15),
                headers=_DEFAULT_HEADERS,
            )
        return self._session

    async def search(self, query: str, limit: int = 20) -> list[ExternalPost]:
        """Поиск постов по тегам. query — теги через пробел (danbooru syntax).

        Возвращает список ExternalPost с source_url для скачивания.
        """
        if not query.strip():
            return []

        params: dict[str, Any] = {
            "tags": query,
            "limit": min(limit, 200),  # danbooru max 200
        }
        # Опциональная аутентификация (если задана в настройках)
        danbooru_login = getattr(settings, "danbooru_login", "") or ""
        danbooru_api_key = getattr(settings, "danbooru_api_key", "") or ""
        if danbooru_login:
            params["login"] = danbooru_login
        if danbooru_api_key:
            params["api_key"] = danbooru_api_key

        try:
            session = await self._get_session()
            async with session.get(DANBOORU_API_URL, params=params) as response:
                if response.status != 200:
                    log.warning("Danbooru API returned status %d for query=%r", response.status, query)
                    return []
                data = await response.json(content_type=None)
        except TimeoutError:
            log.warning("Danbooru API timeout for query=%r", query)
            return []
        except aiohttp.ClientError as e:
            log.warning("Danbooru API network error: %s", e)
            return []
        except Exception as e:  # noqa: BLE001
            log.warning("Danbooru API error: %s", e)
            return []

        if not isinstance(data, list):
            log.warning("Danbooru API unexpected response type: %s", type(data).__name__)
            return []

        posts: list[ExternalPost] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            ext_post = self._parse_post(raw)
            if ext_post is not None:
                posts.append(ext_post)

        log.info("Danbooru search query=%r: найдено %d постов", query, len(posts))
        return posts

    def _parse_post(self, raw: dict[str, Any]) -> ExternalPost | None:
        """Преобразует ответ danbooru в ExternalPost. None если пост невалидный."""
        post_id = raw.get("id")
        # file_url может быть пустым (удалён/ограничен) — пропускаем
        file_url = raw.get("file_url") or raw.get("large_file_url")
        if not post_id or not file_url:
            return None

        file_ext = raw.get("file_ext", "")
        file_type = file_type_from_extension(file_url) if file_ext else file_type_from_extension(file_url)

        tags_str = raw.get("tag_string", "") or ""
        tags = [t for t in tags_str.split() if t][:20]  # лимит 20 тегов

        rating_raw = raw.get("rating", "")
        rating = _RATING_MAP.get(rating_raw, None) if rating_raw else None

        preview_url = raw.get("preview_file_url") or raw.get("large_file_url")

        return ExternalPost(
            source_type=self.source_type,
            source_id=str(post_id),
            source_url=file_url,
            file_type=file_type,
            preview_url=preview_url,
            tags=tags,
            rating=rating,
            referer=DANBOORU_REFERER,
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None


# Singleton
_danbooru_parser: DanbooruParser | None = None


def get_danbooru_parser() -> DanbooruParser:
    global _danbooru_parser
    if _danbooru_parser is None:
        _danbooru_parser = DanbooruParser()
    return _danbooru_parser


async def close_danbooru_parser() -> None:
    global _danbooru_parser
    if _danbooru_parser is not None:
        await _danbooru_parser.close()
    _danbooru_parser = None
