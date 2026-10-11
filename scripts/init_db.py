#!/usr/bin/env python3
"""Инициализация БД Фазы 2 (ТЗ §5, §2.6, §10.1).

Создаёт:
- основную БД data/bot.db со всеми таблицами (§5.1)
- БД тегов Gelbooru data/tags.db (§10.1)
- первого admin из .env (ADMIN_TG_ID, §2.6)

Запуск:
    python scripts/init_db.py            # внутри контейнера: docker compose exec bot python scripts/init_db.py
    python scripts/init_db.py --check    # проверить схему без изменений
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("init_db")

from app.config.settings import settings
from app.db import bootstrap_admin, init_db
from app.services.parsers.gelbooru import tags_db


async def main(check_only: bool = False) -> int:
    log.info("DATABASE_URL = %s", settings.database_url)
    log.info("db_path = %s", settings.db_path)

    db = await init_db()
    await tags_db.init_tags_db()

    if not check_only and settings.admin_tg_id:
        added = await bootstrap_admin(settings.admin_tg_id, None)
        if added:
            log.info("Создан admin: tg_id=%s", settings.admin_tg_id)
        else:
            log.info("Admin tg_id=%s уже существует в БД", settings.admin_tg_id)
    elif not settings.admin_tg_id:
        log.warning("ADMIN_TG_ID не задан — admin не создан. Заполните .env (ТЗ §2.6).")

    # Проверка: покажем список таблиц
    cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in await cur.fetchall()]
    await cur.close()
    log.info("Таблицы в bot.db: %s", ", ".join(tables))

    tags_count = await tags_db.get_tags_count()
    log.info("Тегов в tags.db: %d (заполняется scripts/populate_tags.py)", tags_count)

    await tags_db.close_tags_db()
    from app.db import close_db
    await close_db()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Инициализация БД Фазы 2")
    ap.add_argument("--check", action="store_true", help="только проверить схему, без bootstrap admin")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(check_only=args.check)))
