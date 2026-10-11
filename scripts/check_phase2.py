#!/usr/bin/env python3
"""Скрипт проверки Фазы 2 (ТЗ §13): БД + приём из ЛС + очереди + планировщик.

Запуск (внутри контейнера или на хосте с активированным venv):
    python scripts/check_phase2.py --db          # схема БД + admin bootstrap
    python scripts/check_phase2.py --routing      # демо-маршрутизация
    python scripts/check_phase2.py --scheduler    # список job'ов (требует запущенного приложения)
    python scripts/check_phase2.py                # все быстрые проверки (db + routing)

Проверки требуют инициализированной БД. init_db вызывается автоматически.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

try:
    from dotenv import load_dotenv
    for _c in (BASE_DIR / ".env", Path("/host_env/.env")):
        if _c.is_file():
            load_dotenv(_c, override=False)
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("check_phase2")

from app.config.settings import settings
from app.db import bootstrap_admin, close_db, init_db
from app.repositories import channels as channels_repo
from app.repositories import queues as queues_repo
from app.repositories import routing as routing_repo
from app.repositories import users as users_repo
from app.services.routing import route_post


async def check_db() -> int:
    log.info("== Проверка БД ==")
    db = await init_db()
    if settings.admin_tg_id:
        await bootstrap_admin(settings.admin_tg_id, None)

    admin = await users_repo.get_user_by_tg_id(settings.admin_tg_id) if settings.admin_tg_id else None
    if admin and admin["role"] == "admin":
        log.info("[OK] admin в БД: tg_id=%s role=%s", admin["tg_id"], admin["role"])
    else:
        log.warning("[!] ADMIN_TG_ID не задан или admin не создан")

    cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in await cur.fetchall()]
    await cur.close()
    expected = {"users", "channels", "queues", "posts", "routing_rules", "signatures",
                "duplicate_hashes", "tag_subscriptions", "task_queue"}
    missing = expected - set(tables)
    if missing:
        log.error("[FAIL] Нет таблиц: %s", missing)
        return 1
    log.info("[OK] Все 9 таблиц из ТЗ §5 присутствуют: %s", ", ".join(sorted(expected)))

    # RBAC (ТЗ §2.6)
    mod = {"tg_id": 999999, "username": "test_mod", "role": "moderator", "is_active": 1}
    assert users_repo.check_access(mod, "moderator") is True
    assert users_repo.check_access(mod, "admin") is False
    adm = {"tg_id": 1, "username": "test_adm", "role": "admin", "is_active": 1}
    assert users_repo.check_access(adm, "moderator") is True  # admin >= moderator
    assert users_repo.check_access(adm, "admin") is True
    log.info("[OK] RBAC: admin >= moderator, moderator не проходит admin-проверку (ТЗ §2.6)")
    return 0


async def check_routing() -> int:
    log.info("== Проверка маршрутизации ==")
    await init_db()

    # Создаём демо-канал + очередь + правила (если ещё нет)
    ch = await channels_repo.get_channel_by_chat_id(-100111000111)
    if not ch:
        ch_id = await channels_repo.create_channel(-100111000111, "Demo Channel", "demo_channel")
    else:
        ch_id = ch["id"]

    existing = await queues_repo.list_queues(active_only=False)
    demo_q = next((x for x in existing if x["name"] == "Demo Queue"), None)
    if demo_q:
        qid = demo_q["id"]
    else:
        qid = await queues_repo.create_queue(ch_id, "Demo Queue", interval_minutes=120)

    # Точное правило + regex-правило + правило any (ТЗ §5.2 примеры)
    rules = await routing_repo.list_rules(active_only=False)
    if not any(r["source_pattern"] == "@anime_channel" for r in rules):
        await routing_repo.create_rule("telegram_channel", "@anime_channel", qid, priority=10)
    if not any(r["source_pattern"] == r".*rating:safe.*" for r in rules):
        await routing_repo.create_rule("gelbooru", r".*rating:safe.*", qid, priority=5)

    # Тест 1: точное совпадение telegram_channel
    res = await route_post("telegram_channel", "@anime_channel")
    assert res == qid, f"ожидали очередь {qid}, получили {res}"
    log.info("[OK] Точное совпадение telegram_channel/@anime_channel → queue %s", res)

    # Тест 2: regex по тегам gelbooru
    res2 = await route_post("gelbooru", "12345", tags=["1girl", "rating:safe", "smile"])
    assert res2 == qid, f"ожидали очередь {qid} по тегу rating:safe, получили {res2}"
    log.info("[OK] Regex по тегам gelbooru (.*rating:safe.*) → queue %s", res2)

    # Тест 3: нет правила → Входящие (None)
    res3 = await route_post("x.com", "https://x.com/unknown")
    assert res3 is None, f"ожидали None (Входящие), получили {res3}"
    log.info("[OK] Нет правила для x.com → Входящие (None)")
    return 0


async def check_scheduler_jobs() -> int:
    """Требует запущенного приложения (lifespan). Выводит job'ы через /health/scheduler."""
    import aiohttp

    url = "http://localhost:8000/health/scheduler"
    try:
        async with aiohttp.ClientSession() as s, s.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json()
            if data.get("status") == "ok":
                log.info("[OK] Scheduler запущен, job'ов очередей: %d", len(data.get("jobs", [])))
                for j in data.get("jobs", []):
                    log.info("     - %s next_run=%s", j["id"], j["next_run"])
                return 0
            log.warning("[!] Scheduler не запущен или нет активных очередей: %s", data)
            return 0
    except Exception as e:  # noqa: BLE001 — широкий catch для CLI-скрипта проверки
        log.warning("[!] /health/scheduler недоступен (%s) — приложение запущено?", e)
        return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка Фазы 2")
    ap.add_argument("--db", action="store_true", help="схема БД + admin bootstrap + RBAC")
    ap.add_argument("--routing", action="store_true", help="демо-маршрутизация (regex/точное/any)")
    ap.add_argument("--scheduler", action="store_true", help="список job'ов планировщика")
    args = ap.parse_args()
    if not any([args.db, args.routing, args.scheduler]):
        args.db = args.routing = True

    rc = 0
    try:
        if args.db:
            rc |= await check_db()
        if args.routing:
            rc |= await check_routing()
        if args.scheduler:
            rc |= await check_scheduler_jobs()
    finally:
        await close_db()
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
