"""Pixiv парсер (ТЗ §2.1.4, Фаза 3 этап 5).

Pixiv не имеет публичного API без аутентификации. Для доступа к изображениям
используем прямые URL вида https://i.pximg.net/img-original/img/.../...png,
но они требуют Referer: https://www.pixiv.net/ (без Referer — 403).

API: https://www.pixiv.net/ajax/illust/<id> — JSON метаданные поста.
Аутентификация: опционально (PHPSESSID cookie), без неё доступны только
публичные посты.

Особенности:
  - URL поста: https://www.pixiv.net/artworks/<id>;
  - i.pximg.net требует Referer: https://www.pixiv.net/;
  - file_type: png/jpg → photo, gif → gif, ugoira → gif (zip-анимация);
  - rating: 'g' (general), 'r' (r-18) — мапим в safe/explicit.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

from app.services.parsers.base import BaseParser, ExternalPost, file_type_from_extension

log = logging.getLogger("parsers.pixiv")

PIXIV_AJAX_URL = "https://www.pixiv.net/ajax/illust"
PIXIV_REFERER = "https://www.pixiv.net/"
_PIXIV_URL_RE = re.compile(r"pixiv\.net/(?:artworks/|member_illust\.php.*illust_id=)(\d+)", re.IGNORECASE)

_RATING_MAP = {"g": "safe", "r-18": "explicit", "r-18g": "explicit"}

_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": PIXIV_REFERER,
    "x-user-id": "",
}


class PixivParser(BaseParser):
    """Парсер Pixiv через AJAX endpoint."""

    source_type = "pixiv"

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = dict(_DEFAULT_HEADERS)
            # Опциональная аутентификация через PHPSESSID
            pixiv_session_id = getattr(_get_settings(), "pixiv_session_id", "") or ""
            if pixiv_session_id:
                headers["Cookie"] = f"PHPSESSID={pixiv_session_id}"
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20, connect=10, sock_read=15),
                headers=headers,
            )
        return self._session

    async def search(self, query: str, limit: int = 20) -> list[ExternalPost]:
        """Поиск не реализован — Pixiv поиск требует аутентификации.

        Используйте extract(url) для парсинга конкретного поста.
        Если query — URL pixiv.net/artworks/<id>, извлекаем его.
        """
        match = _PIXIV_URL_RE.search(query)
        if match:
            illust_id = match.group(1)
            post = await self.extract_by_id(illust_id)
            return [post] if post else []
        log.warning("PixivParser.search: запрос %r не URL поста — используйте extract(url)", query)
        return []

    async def extract(self, url: str) -> ExternalPost | None:
        """Извлекает медиа из поста Pixiv по URL."""
        match = _PIXIV_URL_RE.search(url)
        if not match:
            log.warning("PixivParser.extract: URL %r не распознан", url)
            return None
        illust_id = match.group(1)
        return await self.extract_by_id(illust_id)

    async def extract_by_id(self, illust_id: str) -> ExternalPost | None:
        """Извлекает медиа из поста Pixiv по illust_id через AJAX endpoint."""
        url = f"{PIXIV_AJAX_URL}/{illust_id}"
        params = {"lang": "en"}
        try:
            session = await self._get_session()
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    log.warning("Pixiv API returned status %d for illust_id=%s", response.status, illust_id)
                    return None
                data = await response.json(content_type=None)
        except TimeoutError:
            log.warning("Pixiv API timeout for illust_id=%s", illust_id)
            return None
        except aiohttp.ClientError as e:
            log.warning("Pixiv API network error: %s", e)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("Pixiv API error: %s", e)
            return None

        return self._parse_illust(data, illust_id)

    def _parse_illust(self, data: dict[str, Any], illust_id: str) -> ExternalPost | None:
        """Парсит ответ Pixiv AJAX в ExternalPost.

        Структура: {"body": {"illustId", "illustTitle", "tags", "urls": {...}, "xRestrict", ...}}
        urls: {"thumb", "small", "regular", "original"}
        """
        body = data.get("body")
        if not isinstance(body, dict):
            return None

        urls = body.get("urls") or {}
        if not isinstance(urls, dict):
            return None

        # Оригинальный URL — лучшее качество
        source_url = urls.get("original") or urls.get("regular") or urls.get("small")
        if not source_url:
            log.warning("PixivParser: нет source_url для illust %s", illust_id)
            return None

        file_type = file_type_from_extension(source_url)
        # ugoira (zip-анимация pixiv) → gif
        illust_type = str(body.get("illustType", "0"))
        if illust_type == "2":  # ugoira
            file_type = "gif"

        preview_url = urls.get("thumb") or urls.get("small")

        # Теги
        tags_raw = body.get("tags") or {}
        if isinstance(tags_raw, dict):
            tags_list = tags_raw.get("tags") or []
        else:
            tags_list = tags_raw if isinstance(tags_raw, list) else []
        tags = []
        for t in tags_list:
            if isinstance(t, dict):
                tag = t.get("tag") or ""
                if tag:
                    tags.append(tag.lower())
            elif isinstance(t, str):
                tags.append(t.lower())
        tags = tags[:15]

        # Rating
        x_restrict = str(body.get("xRestrict", "0"))
        rating = _RATING_MAP.get(x_restrict.lower(), None)
        if rating is None and x_restrict == "0":
            rating = "safe"

        title = body.get("illustTitle") or ""

        return ExternalPost(
            source_type=self.source_type,
            source_id=illust_id,
            source_url=source_url,
            file_type=file_type,
            preview_url=preview_url,
            tags=tags,
            rating=rating,
            referer=PIXIV_REFERER,
            caption=title[:1000] if title else None,
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None


def _get_settings():
    """Ленивый импорт settings (разрывает циклический импорт при необходимости)."""
    from app.config.settings import settings

    return settings


# Singleton
_pixiv_parser: PixivParser | None = None


def get_pixiv_parser() -> PixivParser:
    global _pixiv_parser
    if _pixiv_parser is None:
        _pixiv_parser = PixivParser()
    return _pixiv_parser


async def close_pixiv_parser() -> None:
    global _pixiv_parser
    if _pixiv_parser is not None:
        await _pixiv_parser.close()
    _pixiv_parser = None


# Helper для тестов (парсинг illust dict без HTTP)
def parse_illust_dict(data: dict[str, Any], illust_id: str) -> ExternalPost | None:
    """Парсит ответ Pixiv AJAX в ExternalPost (для тестов без HTTP)."""
    parser = PixivParser()
    return parser._parse_illust(data, illust_id)  # noqa: SLF001
