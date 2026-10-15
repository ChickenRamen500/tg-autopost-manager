#!/usr/bin/env python3
"""Bulk-наполнение tags.db из API Gelbooru (мигрировано из gbooru_tg/populate_tags.py, ТЗ §10.1).

Запуск:
    python scripts/populate_tags.py 50000      # топ 50K популярных тегов
    python scripts/populate_tags.py 100000     # ~3 мин

Теги хранятся в data/tags.db (отдельно от основной БД).

P3-38: добавлен argparse + Ctrl-C handler с try/finally для close().
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.services.parsers.gelbooru import gelbooru_client, tags_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)


async def populate(target: int) -> None:
    try:
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
    finally:
        # P3-38: всегда закрываем ресурсы, даже при Ctrl-C
        try:
            await gelbooru_client.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("gelbooru_client.close: %s", e)
        try:
            await tags_db.close_tags_db()
        except Exception as e:  # noqa: BLE001
            logger.warning("tags_db.close: %s", e)


def main() -> None:
    ap = argparse.ArgumentParser(description="Bulk-наполнение tags.db из Gelbooru API")
    ap.add_argument("target", nargs="?", type=int, default=50000, help="сколько тегов загрузить (по умолчанию 50000)")
    args = ap.parse_args()
    try:
        asyncio.run(populate(args.target))
    except KeyboardInterrupt:
        logger.info("Прервано пользователем (Ctrl-C). Ресурсы закрыты.")


if __name__ == "__main__":
    main()
