#!/usr/bin/env python3
"""Автоочистка папки downloads каждые 24 часа (ТЗ §3.1, Фаза 6).

Удаляет файлы старше 24 часов из data/downloads/.
Запуск: python scripts/cleanup_downloads.py
В Docker: через cron или APScheduler interval job.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("cleanup_downloads")

MAX_AGE_HOURS = 24


def cleanup(downloads_dir: Path | None = None, max_age_hours: int = MAX_AGE_HOURS) -> int:
    """Удаляет файлы старше max_age_hours из downloads_dir. Возвращает кол-во удалённых."""
    if downloads_dir is None:
        downloads_dir = Path(__file__).resolve().parent.parent / "data" / "downloads"

    if not downloads_dir.exists():
        log.info("Папка %s не существует — нечего чистить", downloads_dir)
        return 0

    now = time.time()
    max_age_sec = max_age_hours * 3600
    deleted = 0

    for f in downloads_dir.iterdir():
        if f.name == ".gitkeep":
            continue
        if not f.is_file():
            continue
        try:
            file_age = now - f.stat().st_mtime
            if file_age > max_age_sec:
                f.unlink()
                deleted += 1
                log.info("Удалён: %s (возраст %.1f ч)", f.name, file_age / 3600)
        except OSError as e:
            log.warning("Не удалось удалить %s: %s", f, e)

    log.info("Готово. Удалено файлов: %d", deleted)
    return deleted


if __name__ == "__main__":
    sys.exit(0 if cleanup() >= 0 else 1)
