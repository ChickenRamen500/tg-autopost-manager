"""Медиа-конвертация через ffmpeg (ТЗ §2.1.4 — отправка «максимального качества»).

Задачи модуля:
  1. convert_video_to_mp4() — конвертация webm/mkv/mov/avi/... → mp4 (H.264/AAC).
     Gelbooru и ряд других источников отдают видео в webm — Telegram плохо
     играет webm (и как video, и как animation), пользователь просил mp4.
     Конвертация выполняется ПОСЛЕ скачивания в worker'е (и в quick_add_upload),
     до pHash/постановки в очередь отправки.
  2. compress_photo_for_telegram() — пережатие фото > 10 МБ в JPEG.
     Telegram не принимает photo больше 10485760 байт («file of size ... is too
     big for a photo») — пережимаем в JPEG (q~4, длинная сторона ≤ 4096);
     если не помогло — scheduler отправляет как document.

ffmpeg ставится в docker-образ (docker/Dockerfile: apt-get install ffmpeg).
В среде без ffmpeg функции возвращают None — вызывающий код тихо оставляет
оригинальный файл (деградация качества, но не ошибка).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("media.transcode")

# Telegram Bot API: максимальный размер фото для send_photo (1024*1024*10).
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024

# Контейнеры, которые имеет смысл конвертировать в mp4. mp4/m3u8 не трогаем;
# gif — animation (отдаётся как есть).
VIDEO_EXTS_TO_CONVERT = {".webm", ".mkv", ".mov", ".avi", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts"}

# Таймаут ffmpeg — большие видео (до 2 ГБ) на слабом железе (§3.1) могут
# конвертироваться долго; 30 минут достаточно и не подвешивает worker.
FFMPEG_TIMEOUT_SECONDS = 1800


def ffmpeg_available() -> bool:
    """Есть ли ffmpeg в PATH (в docker-образе — да; в минимальной тестовой среде — нет)."""
    return shutil.which("ffmpeg") is not None


def _ext_of(path: str | Path) -> str:
    return os.path.splitext(str(path))[1].lower()


async def _run_ffmpeg(args: list[str], *, timeout: int = FFMPEG_TIMEOUT_SECONDS) -> bool:
    """Запускает ffmpeg, возвращает True при коде 0. Не бросает исключений."""
    if not ffmpeg_available():
        return False
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, NotImplementedError) as e:  # no exec / no event loop policy
        log.warning("ffmpeg не запущен: %s", e)
        return False
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        log.warning("ffmpeg превысил таймаут %d сек — отмена", timeout)
        return False
    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", "replace").strip()[-500:]
        log.warning("ffmpeg завершился с кодом %s: %s", proc.returncode, err)
        return False
    return True


async def convert_video_to_mp4(file_path: str | Path, *, timeout: int = FFMPEG_TIMEOUT_SECONDS) -> str | None:
    """Конвертирует видео в mp4 (H.264 + AAC, faststart) для корректной отправки в Telegram.

    Args:
        file_path: путь к исходному видео (обычно .webm из gelbooru).
        timeout: максимальная длительность конвертации.

    Returns:
        Путь к сконвертированному .mp4 (исходник удалён) — либо None,
        если конвертация не нужна/не удалась (остаётся исходный файл).
    """
    src = Path(file_path)
    if not src.is_file():
        return None
    ext = _ext_of(src)
    if ext not in VIDEO_EXTS_TO_CONVERT:
        return None  # mp4/gif и прочее — не трогаем
    if not ffmpeg_available():
        log.info("transcode: ffmpeg недоступен — оставляю %s как есть", src.name)
        return None

    dest = src.with_suffix(".mp4")
    ok = await _run_ffmpeg(
        [
            "-i", str(src),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(dest),
        ],
        timeout=timeout,
    )
    if not ok or not dest.is_file() or dest.stat().st_size == 0:
        # Неудачная конвертация: убираем битый выход, оставляем исходник.
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        log.warning("transcode: конвертация %s → mp4 не удалась — оставляю исходник", src.name)
        return None

    try:
        src.unlink(missing_ok=True)
    except OSError as e:
        log.warning("transcode: не удалось удалить исходник %s: %s", src, e)
    log.info(
        "transcode: %s → %s (%d → %d байт)",
        src.name, dest.name, src.stat().st_size if src.exists() else -1, dest.stat().st_size,
    )
    return str(dest)


async def compress_photo_for_telegram(file_path: str | Path) -> str | None:
    """Пережимает фото в JPEG ≤ лимита Telegram (10 МБ) для send_photo.

    Вызывается scheduler'ом перед отправкой photo с размером > 10 МБ
    (ошибка «file of size ... is too big for a photo; the maximum size is
    10485760 bytes»). JPEG q=4 + длинная сторона ≤ 4096px обычно даёт
    1-3 МБ даже с 50 МБ PNG.

    Returns:
        Путь к пережатому временному .jpg (рядом с оригиналом, имя
        <orig>_tg.jpg) — либо None (ffmpeg нет / пережатие не удалось /
        результат всё ещё > лимита; тогда вызывающий отправляет document).
        Оригинальный файл НЕ трогаем.
    """
    src = Path(file_path)
    if not src.is_file():
        return None
    if not ffmpeg_available():
        return None
    if os.path.getsize(src) <= TELEGRAM_PHOTO_MAX_BYTES:
        return None  # уже в лимите — ничего не делаем

    dest = src.with_name(src.stem + "_tg.jpg")
    ok = await _run_ffmpeg(
        [
            "-i", str(src),
            "-vf", "scale='min(4096,iw)':'-2'",
            "-q:v", "4",
            str(dest),
        ],
        timeout=600,
    )
    if not ok or not dest.is_file() or dest.stat().st_size == 0:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        log.warning("transcode: пережатие %s не удалось", src.name)
        return None
    if dest.stat().st_size > TELEGRAM_PHOTO_MAX_BYTES:
        log.warning(
            "transcode: %s после пережатия %d байт — всё ещё > лимита Telegram",
            src.name, dest.stat().st_size,
        )
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    log.info(
        "transcode: фото пережато %s → %s (%d → %d байт)",
        src.name, dest.name, os.path.getsize(src), dest.stat().st_size,
    )
    return str(dest)
