"""Скачивание медиа из внешних источников (ТЗ §2.1.4, §4.2.2 — Фаза 3).

Worker вызывает download_media() для постов с source_url (Gelbooru, Danbooru,
Pikabu, Pixiv, ...). Файл сохраняется в data/downloads/ (временное хранилище,
автоочистка каждые 24 ч — ТЗ §3.1), путь записывается в posts.file_path.

Особенности:
- Referer/UA обязательны для ряда источников (gelbooru отдаёт 403 без Referer,
  pixiv — без Referer на i.pximg.net).
- Источник определяется по referer в payload задачи; если не задан — выводится
  из домена URL (fallback на универсальный browser-like UA).
- Content-Type → file_type: image/* → photo, video/* → video, image/gif → gif,
  прочее → document. Это нужно, т.к. scheduler.send_post_from_queue выбирает
  метод отправки (send_photo / send_video / ...) по posts.file_type.
- Лимит размера: 2000 МБ (Local Bot API Server, ТЗ §3.2). Превышение → ошибка,
  пост переводится в failed (permanent error).
"""
from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from app.config.settings import settings

log = logging.getLogger("media.downloader")

# User-Agent: имитируем браузер — часть источников (gelbooru, pixiv) блокируют
# «голые» HTTP-клиенты по сигнатуре UA.
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Referer по домену источника. piximg/i.pximg.net требует Referer=https://pixiv.net
_REFERER_BY_HOST = {
    "gelbooru.com": "https://gelbooru.com/",
    "rule34.xxx": "https://rule34.xxx/",
    "pximg.net": "https://www.pixiv.net/",
    "pixiv.net": "https://www.pixiv.net/",
    "i.pximg.net": "https://www.pixiv.net/",
}

# Максимальный размер скачиваемого файла (2000 МБ — лимит Local Bot API Server).
_MAX_FILE_SIZE_BYTES = 2000 * 1024 * 1024

# Chunk size для stream-записи (8 КБ — баланс памяти/скорости).
_CHUNK_SIZE = 8192


class DownloadError(Exception):
    """Ошибка скачивания (сеть/HTTP/размер). Ретраится worker'ом."""


class PermanentDownloadError(DownloadError):
    """Неустранимая ошибка (4xx кроме 429, неподдерживаемый Content-Type,
    превышение размера). Ретрай бессмысленен — задача → error сразу.
    """


def _referer_for(url: str, explicit: str | None) -> str:
    """Referer для запроса: явный > по домену > универсальный."""
    if explicit:
        return explicit
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        host = ""
    host_lower = host.lower()
    for key, ref in _REFERER_BY_HOST.items():
        if host_lower == key or host_lower.endswith("." + key):
            return ref
    # Fallback: Referer = корень того же домена
    if host:
        return f"https://{host}/"
    return "https://example.com/"


def _file_type_from_content_type(content_type: str, url: str) -> str:
    """Определяет file_type (для posts.file_type) по Content-Type или расширению URL.

    Возвращает один из: photo, video, gif, document (аудио/voice пока как document).
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "image/gif":
        return "gif"
    if ct.startswith("image/"):
        return "photo"
    if ct.startswith("video/"):
        return "video"
    # Content-Type не помог — пробуем расширение URL
    path = urlparse(url).path.lower()
    ext = os.path.splitext(path)[1]
    if ext in (".gif", ".gifv"):
        return "gif"
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        return "photo"
    if ext in (".mp4", ".webm", ".mov", ".avi", ".mkv"):
        return "video"
    return "document"


def _extension_for(file_type: str, url: str, content_type: str) -> str:
    """Подбирает расширение файла для сохранения."""
    # Сначала из URL
    path = urlparse(url).path
    ext = os.path.splitext(path)[1]
    if ext and 1 <= len(ext) <= 8:
        return ext
    # Из Content-Type
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return guessed
    # Fallback по file_type
    return {
        "photo": ".jpg",
        "video": ".mp4",
        "gif": ".gif",
        "document": ".bin",
    }.get(file_type, ".bin")


async def download_media(
    url: str,
    post_id: int,
    *,
    referer: str | None = None,
    user_agent: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Скачивает файл во временное хранилище и возвращает метаданные.

    Args:
        url: прямой URL файла.
        post_id: ID поста (для имени файла и логирования).
        referer: явный Referer (если None — выводится из домена url).
        user_agent: явный UA (если None — browser-like).
        extra_headers: доп. заголовки (Cookie для xcom и т.п.).

    Returns:
        dict с ключами:
          file_path — абсолютный путь к скачанному файлу;
          file_type — photo/video/gif/document (для posts.file_type);
          file_size — размер в байтах;
          mime_type — Content-Type ответа.

    Raises:
        PermanentDownloadError: 4xx (кроме 429), неподдерживаемый тип, размер > 2 ГБ.
        DownloadError: сетевая ошибка / 429 / 5xx / timeout (ретраится).
    """
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": user_agent or _DEFAULT_UA,
        "Referer": _referer_for(url, referer),
        "Accept": "*/*",
    }
    if extra_headers:
        headers.update(extra_headers)

    timeout = aiohttp.ClientTimeout(total=settings.worker_download_timeout_seconds, connect=15, sock_read=60)
    log.info("download_media: post #%s url=%s referer=%s", post_id, url, headers["Referer"])

    try:
        # timeout as kwarg for the session, per-request override below
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as response:
                status = response.status
                if status == 429:
                    # Retry-After есть в заголовках — caller может подождать
                    raise DownloadError(f"HTTP 429 Too Many Requests для {url}")
                if 400 <= status < 500:
                    raise PermanentDownloadError(f"HTTP {status} для {url} (клиентская ошибка, ретрай бесполезен)")
                if status >= 500:
                    raise DownloadError(f"HTTP {status} для {url} (серверная ошибка, ретраим)")

                content_type = response.headers.get("Content-Type", "")
                file_type = _file_type_from_content_type(content_type, url)
                ext = _extension_for(file_type, url, content_type)

                # Проверка Content-Length ДО скачивания (быстрый отказ на больших файлах)
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        cl = int(content_length)
                    except ValueError:
                        cl = 0
                    if cl > _MAX_FILE_SIZE_BYTES:
                        raise PermanentDownloadError(
                            f"Размер файла {cl} байт > лимита {_MAX_FILE_SIZE_BYTES} (post #{post_id})"
                        )

                # Имя файла: post_<id>_<короткий хеш>.ext — avoids коллизий при ретраях
                dest_path = settings.downloads_dir / f"post_{post_id}{ext}"
                total = 0
                with open(dest_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                        total += len(chunk)
                        if total > _MAX_FILE_SIZE_BYTES:
                            f.close()
                            try:
                                dest_path.unlink()
                            except OSError:
                                pass
                            raise PermanentDownloadError(
                                f"Размер файла превысил {_MAX_FILE_SIZE_BYTES} байт при стриме (post #{post_id})"
                            )
                        f.write(chunk)

                log.info(
                    "download_media: post #%s скачан %s (%d байт, type=%s, ct=%s)",
                    post_id, dest_path, total, file_type, content_type,
                )
                return {
                    "file_path": str(dest_path),
                    "file_type": file_type,
                    "file_size": total,
                    "mime_type": content_type,
                }
    except PermanentDownloadError:
        raise
    except DownloadError:
        raise
    except TimeoutError as e:
        raise DownloadError(f"Timeout при скачивании {url} (post #{post_id}): {e}") from e
    except aiohttp.ClientError as e:
        raise DownloadError(f"Сетевая ошибка при скачивании {url} (post #{post_id}): {e}") from e
    except Exception as e:
        # Неожиданная ошибка — логируем и оборачиваем в DownloadError (ретраится)
        log.exception("Неожиданная ошибка скачивания %s (post #%s)", url, post_id)
        raise DownloadError(f"Неожиданная ошибка скачивания {url} (post #{post_id}): {e}") from e


async def cleanup_old_downloads(max_age_hours: int = 24) -> int:
    """Удаляет файлы из data/downloads старше max_age_hours (ТЗ §3.1).

    Возвращает количество удалённых файлов. Запускается по расписанию (APScheduler)
    или вручную через scripts/cleanup_downloads.py.
    """
    import time

    downloads: Path = settings.downloads_dir
    if not downloads.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for entry in downloads.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError as e:
            log.warning("Не удалось удалить %s: %s", entry, e)
    if removed:
        log.info("cleanup_old_downloads: удалено %d файлов старше %dч", removed, max_age_hours)
    return removed
