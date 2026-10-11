#!/usr/bin/env python3
"""Bulk-наполнение tags.db из API Gelbooru (мигрировано из gbooru_tg/populate_tags.py, ТЗ §10.1).

Запуск:
    python scripts/populate_tags.py 50000      # топ 50K популярных тегов
    python scripts/populate_tags.py 100000     # ~3 мин

Теги хранятся в data/tags.db (отдельно от основной БД).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.services.parsers.gelbooru import gelbooru_client, tags_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)


async def populate(target: int) -> None:
    await tags_db.init_tags_db()
    existing = await tags_db.get_tags_count()
    logger.info("Тегов в БД сейчас: %d", existing)

    pages = (target + 99) // 100
    total = 0

    for pid in range(pages):
        tags, api_total = await gelbooru_client.fetch_tags_page(pid=pid, limit=100, orderby="count")
        if not tags:
            logger.info("Тегов больше нет на странице %d (API вернул пусто)", pid)
            break

        await tags_db.upsert_tags(tags)
        total += len(tags)

        if (pid + 1) % 10 == 0:
            logger.info("Прогресс: %d тегов (страница %d/%d, API сообщает %d всего)", total, pid + 1, pages, api_total)

        if len(tags) < 100:
            logger.info("Достигнут конец результатов на странице %d", pid)
            break

    final_count = await tags_db.get_tags_count()
    logger.info("Готово. За этот запуск добавлено/обновлено %d тегов. В БД теперь %d тегов.", total, final_count)
    await gelbooru_client.close()
    await tags_db.close_tags_db()


def main() -> None:
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 50000
    asyncio.run(populate(target))


if __name__ == "__main__":
    main()
