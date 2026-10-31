"""X.com (Twitter) парсер через yt-dlp (ТЗ §2.1.4, Фаза 3 этап 5).

yt-dlp умеет извлекать медиа из твитов (фото/видео/GIF). Для доступа к X.com
нужны cookies (т.к. X требует логин для просмотра большинства твитов).

Cookie-файл: Netscape format (export из браузера через расширение,
например "Get cookies.txt LOCALLY"). Путь задаётся в settings.xcom_cookies_path.

API:
  - search(query, limit) — не реализован (X.com не имеет публичного поиска по тегам
    через yt-dlp). Вместо этого — extract(tweet_url) → ExternalPost.
  - extract(url) → ExternalPost — извлекает медиа из одного твита.

Особенности:
  - yt-dlp запускается в отдельном потоке (run_in_executor) — он синхронный;
  - --skip-download + --dump-json — получить метаданные без скачивания;
  - источник: твит может содержать несколько фото (album) — берём первое;
  - file_type: .mp4/.webm → video, .jpg/.png → photo, .gif → gif.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from app.config.settings import settings
from app.services.parsers.base import BaseParser, ExternalPost

log = logging.getLogger("parsers.xcom")

_XCOM_URL_PATTERN = "twitter.com/"  # покрывает x.com и twitter.com


class XcomParser(BaseParser):
    """Парсер X.com через yt-dlp."""

    source_type = "xcom"

    async def search(self, query: str, limit: int = 20) -> list[ExternalPost]:
        """Поиск не реализован — X.com не имеет публичного поиска через yt-dlp.

        Используйте extract(url) для парсинга конкретного твита.
        Возвращает пустой список (query интерпретируется как URL если содержит twitter.com).
        """
        # Если query — URL твита, извлекаем его
        if _XCOM_URL_PATTERN in query.lower() or "x.com/" in query.lower():
            post = await self.extract(query)
            return [post] if post else []
        log.warning("XcomParser.search: запрос %r не URL твита — используйте extract(url)", query)
        return []

    async def extract(self, tweet_url: str) -> ExternalPost | None:
        """Извлекает медиа из твита через yt-dlp.

        Возвращает ExternalPost с source_url (прямая ссылка на файл) или None.
        """
        cookies_path = getattr(settings, "xcom_cookies_path", "") or ""
        if cookies_path and not os.path.exists(cookies_path):
            log.warning("XcomParser: cookies_path=%r не существует — пробуем без cookies", cookies_path)
            cookies_path = ""

        # yt-dlp опции: получить JSON метаданных без скачивания
        ydl_opts: dict[str, Any] = {
            "skip_download": True,
            "dumpjson": True,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
        }
        if cookies_path:
            ydl_opts["cookiefile"] = cookies_path

        loop = asyncio.get_event_loop()

        def _extract_sync() -> dict[str, Any] | None:
            try:
                import yt_dlp

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(tweet_url, download=False)
                    return info
            except Exception as e:  # noqa: BLE001
                log.warning("yt-dlp extract failed for %s: %s", tweet_url, e)
                return None

        info = await loop.run_in_executor(None, _extract_sync)
        if not info:
            return None

        return self._parse_info(info, tweet_url)

    def _parse_info(self, info: dict[str, Any], tweet_url: str) -> ExternalPost | None:
        """Преобразует yt-dlp info dict в ExternalPost.

        ВАЖНО: yt-dlp для Twitter видео возвращает m3u8 (HLS playlist) URL как
        info['url']. Прямое скачивание m3u8 даёт текстовый плейлист, а не видео.
        Поэтому мы ищем в formats прямой mp4 (или другой прямой медиа-формат),
        пропуская m3u8/m3u записи.
        """
        tweet_id = str(info.get("id", "") or "")
        if not tweet_id:
            return None

        ext = (info.get("ext") or "").lower()
        source_url = info.get("url") or ""

        # Если основной URL — m3u8 (HLS плейлист), НЕ используем его напрямую.
        # Ищем прямой mp4 в formats.
        is_hls = source_url.endswith(".m3u8") or ext == "m3u8" or "m3u8" in source_url.lower()
        if is_hls:
            source_url = ""

        # Для видео ищем прямой mp4 формат, пропуская m3u8 плейлисты.
        if not source_url and info.get("formats"):
            # Фильтруем: только прямые медиа-форматы (не m3u8 плейлисты)
            direct_formats = [
                f for f in info["formats"]
                if isinstance(f, dict)
                and f.get("url")
                and not f.get("url", "").endswith(".m3u8")
                and f.get("ext", "").lower() != "m3u8"
                and "m3u8" not in f.get("url", "").lower()
            ]
            if direct_formats:
                # Предпочитаем mp4, затем по убыванию битрейта (tbr или vbr)
                mp4_formats = [f for f in direct_formats if f.get("ext", "").lower() == "mp4"]
                candidates = mp4_formats if mp4_formats else direct_formats
                # Сортируем по битрейту (tbr/vbr) убывание — лучший качество первым
                candidates.sort(
                    key=lambda f: float(f.get("tbr") or f.get("vbr") or f.get("abr") or 0),
                    reverse=True,
                )
                best = candidates[0]
                source_url = best.get("url", "") or ""
                ext = (best.get("ext") or ext).lower()
                log.info(
                    "XcomParser: выбран прямой %s формат (tbr=%s) для твита %s",
                    ext, best.get("tbr") or best.get("vbr"), tweet_id,
                )

        if not source_url:
            log.warning("XcomParser: нет прямого URL (только m3u8?) для твита %s", tweet_id)
            return None

        # file_type по расширению
        if ext in ("mp4", "webm", "mov", "avi", "mkv"):
            file_type = "video"
        elif ext == "gif":
            file_type = "gif"
        elif ext in ("jpg", "jpeg", "png", "webp", "bmp"):
            file_type = "photo"
        else:
            file_type = "document"

        preview_url = info.get("thumbnail")
        title = info.get("title") or info.get("description") or ""

        # Теги: из хэштегов в description (если есть)
        tags: list[str] = []
        description = info.get("description") or ""
        if description:
            # Извлекаем #hashtags
            tags = [w.strip("#").lower() for w in description.split() if w.startswith("#")][:10]

        return ExternalPost(
            source_type=self.source_type,
            source_id=tweet_id,
            source_url=source_url,
            file_type=file_type,
            preview_url=preview_url,
            tags=tags,
            rating=None,  # X.com не имеет rating
            referer="https://x.com/",
            caption=title[:1000] if title else None,
        )

    async def close(self) -> None:
        # yt-dlp не держит HTTP session между вызовами
        pass


# Singleton
_xcom_parser: XcomParser | None = None


def get_xcom_parser() -> XcomParser:
    global _xcom_parser
    if _xcom_parser is None:
        _xcom_parser = XcomParser()
    return _xcom_parser


async def close_xcom_parser() -> None:
    global _xcom_parser
    if _xcom_parser is not None:
        await _xcom_parser.close()
    _xcom_parser = None


# JSON helper для тестов (парсинг info dict без yt-dlp)
def parse_info_dict(info: dict[str, Any], tweet_url: str = "") -> ExternalPost | None:
    """Парсит yt-dlp info dict в ExternalPost (для тестов без запуска yt-dlp)."""
    parser = XcomParser()
    return parser._parse_info(info, tweet_url)  # noqa: SLF001
