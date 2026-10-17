"""Pikabu парсер (ТЗ §2.1.4, Фаза 3 этап 5).

Pikabu не имеет официального API — парсим HTML страницы поста или используем
неофициальный JSON endpoint: https://pikabu.ru/ajax/page.php?action=get_story&story_id=<id>

Для каждого поста извлекаем медиа (фото/видео/GIF) из story.images / story.videos.

Особенности:
  - URL поста: https://pikabu.ru/story/<slug>_<id>;
  - media_type: image / video / gif;
  - Прямые URL изображений: https://cdn.pikabu.ru/images/...;
  - Referer обязателен: https://pikabu.ru/.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

from app.services.parsers.base import BaseParser, ExternalPost, file_type_from_extension

log = logging.getLogger("parsers.pikabu")

PIKABU_STORY_API = "https://pikabu.ru/ajax/page.php"
PIKABU_REFERER = "https://pikabu.ru/"
_PIKABU_URL_RE = re.compile(r"pikabu\.ru/story/.+?_(\d+)", re.IGNORECASE)

_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": PIKABU_REFERER,
}


class PikabuParser(BaseParser):
    """Парсер Pikabu через неофициальный AJAX endpoint."""

    source_type = "pikabu"

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
        """Поиск не реализован — Pikabu не имеет удобного поиска по тегам через API.

        Используйте extract(url) для парсинга конкретного поста.
        Если query — URL pikabu.ru/story/..., извлекаем его.
        """
        match = _PIKABU_URL_RE.search(query)
        if match:
            story_id = match.group(1)
            post = await self.extract_by_id(story_id)
            return [post] if post else []
        log.warning("PikabuParser.search: запрос %r не URL поста — используйте extract(url)", query)
        return []

    async def extract(self, url: str) -> ExternalPost | None:
        """Извлекает медиа из поста Pikabu по URL."""
        match = _PIKABU_URL_RE.search(url)
        if not match:
            log.warning("PikabuParser.extract: URL %r не распознан", url)
            return None
        story_id = match.group(1)
        return await self.extract_by_id(story_id)

    async def extract_by_id(self, story_id: str) -> ExternalPost | None:
        """Извлекает медиа из поста Pikabu по story_id через AJAX endpoint."""
        params = {"action": "get_story", "story_id": story_id}
        try:
            session = await self._get_session()
            async with session.get(PIKABU_STORY_API, params=params) as response:
                if response.status != 200:
                    log.warning("Pikabu API returned status %d for story_id=%s", response.status, story_id)
                    return None
                data = await response.json(content_type=None)
        except TimeoutError:
            log.warning("Pikabu API timeout for story_id=%s", story_id)
            return None
        except aiohttp.ClientError as e:
            log.warning("Pikabu API network error: %s", e)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("Pikabu API error: %s", e)
            return None

        return self._parse_story(data, story_id)

    def _parse_story(self, data: dict[str, Any], story_id: str) -> ExternalPost | None:
        """Парсит ответ Pikabu AJAX в ExternalPost.

        Структура ответа: {"response": {...story...}} или {story: {...}}
        story содержит: images (list URL), videos (list), tags (list), title, ...
        """
        story = data.get("response") or data.get("story") or data
        if not isinstance(story, dict):
            return None

        # Извлекаем изображения (берём первое)
        images = story.get("images") or story.get("image_urls") or []
        if isinstance(images, list) and images:
            source_url = images[0] if isinstance(images[0], str) else (images[0].get("url") if isinstance(images[0], dict) else None)
        else:
            source_url = None

        # Если нет изображений — пробуем видео
        if not source_url:
            videos = story.get("videos") or []
            if isinstance(videos, list) and videos:
                v = videos[0]
                source_url = v.get("url") if isinstance(v, dict) else (v if isinstance(v, str) else None)

        if not source_url:
            log.warning("PikabuParser: нет медиа в story %s", story_id)
            return None

        file_type = file_type_from_extension(source_url)

        tags_raw = story.get("tags") or []
        if isinstance(tags_raw, list):
            tags = [str(t).lower() for t in tags_raw if t][:15]
        else:
            tags = []

        title = story.get("title") or ""
        preview_url = source_url  # pikabu не отдает отдельный preview

        return ExternalPost(
            source_type=self.source_type,
            source_id=story_id,
            source_url=source_url,
            file_type=file_type,
            preview_url=preview_url,
            tags=tags,
            rating=None,  # pikabu не имеет rating
            referer=PIKABU_REFERER,
            caption=title[:1000] if title else None,
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None


# Singleton
_pikabu_parser: PikabuParser | None = None


def get_pikabu_parser() -> PikabuParser:
    global _pikabu_parser
    if _pikabu_parser is None:
        _pikabu_parser = PikabuParser()
    return _pikabu_parser


async def close_pikabu_parser() -> None:
    global _pikabu_parser
    if _pikabu_parser is not None:
        await _pikabu_parser.close()
    _pikabu_parser = None


# Helper для тестов (парсинг story dict без HTTP)
def parse_story_dict(data: dict[str, Any], story_id: str) -> ExternalPost | None:
    """Парсит ответ Pikabu в ExternalPost (для тестов без HTTP)."""
    parser = PikabuParser()
    return parser._parse_story(data, story_id)  # noqa: SLF001
