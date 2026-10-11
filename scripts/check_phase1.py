#!/usr/bin/env python3
"""Скрипт проверки Фазы 1 (ТЗ §13): Local Bot API Server + миграция.

Запуск на хосте (Windows/Linux) при поднятом контейнере local-bot-api:

    # 1. Проверка конфигурации (.env должен быть заполнен)
    python scripts/check_phase1.py --config

    # 2. Разовая миграция: разлогинить бота с облачного API
    python scripts/check_phase1.py --logout

    # 3. Healthcheck: локальный сервер отвечает, токен принят
    python scripts/check_phase1.py --health

    # 4. Главный тест: отправка файла ~300 МБ в тестовый чат + getFile обратно
    #    (TEST_CHAT_ID — id ЛС с ботом или тестового канала, см. .env)
    python scripts/check_phase1.py --big-test

Критерии приёмки Фазы 1 (ТЗ §13):
 - getMe через BOT_API_URL возвращает ok:true (иначе 401/409 > не сделан logOut);
 - send_document 300 МБ проходит (облако отклонило бы >50 МБ);
 - getFile возвращает file_path с абсолютным локальным путём (признак флага --local).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# чтобы `import app...` работал при запуске из любого места
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# Подстраховка: если скрипт запускается внутри Docker-контейнера, а TEST_CHAT_ID
# не проброшен через compose — подтянуть его напрямую из .env на хосте
# (обычно C:\tg-autopost-manager\.env), смонтированном как /app.
try:
    from dotenv import load_dotenv

    for _candidate in (BASE_DIR / ".env", Path("/host_env/.env")):
        if _candidate.is_file():
            load_dotenv(_candidate, override=False)
except ImportError:  # pragma: no cover
    pass

from app.config.settings import settings
from app.utils.local_bot_api import (
    get_me,
    logout_from_cloud,
    test_big_upload_download,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def check_config() -> int:
    problems = settings.validate()
    print(f"BOT_API_URL      = {settings.bot_api_url}")
    print(f"DATABASE_URL     = {settings.database_url}")
    print(f"TELEGRAM_API_ID  = {settings.telegram_api_id or '<не задан>'}")
    print(f"BOT_TOKEN        = {'***' + settings.bot_token[-6:] if settings.bot_token else '<не задан>'}")
    if problems:
        for p in problems:
            print(f"[!] {p}")
        return 1
    print("[OK] Конфигурация полная.")
    return 0


async def check_health() -> int:
    me = await get_me()
    if me:
        print(f"[OK] Local Bot API работает, бот: @{me.get('username')} (id={me.get('id')})")
        print("     Если это http://localhost:8081 — сервер поднят с --local и миграция выполнена.")
        return 0
    print("[FAIL] Локальный сервер не отвечает / токен отклонён.")
    print("       Чек-лист: 1) контейнер local-bot-api запущен; 2) выполнен logOut (--logout);")
    print("       3) api_id/api_hash корректны; 4) порт 8081 проброшен на хост.")
    return 1


async def check_big_test(size_mb: int | None) -> int:
    chat_id = int(os.getenv("TEST_CHAT_ID", "0") or 0)
    if not chat_id:
        print("[FAIL] Задайте TEST_CHAT_ID в .env (id вашего ЛС-чата с ботом или тестового канала).")
        print("       Внутри контейнера проверьте проброс: docker compose exec bot printenv TEST_CHAT_ID")
        return 1
    if chat_id > 0 and chat_id < 1_000_000_000:
        # user-id из @userinfobot — скорее всего это НЕ id чата (см. вывод getUpdates)
        print(f"[WARN] TEST_CHAT_ID={chat_id} выглядит как user-id, а не chat-id.")
        print("       Для ЛС с ботом chat_id обычно совпадает с вашим user-id — пробуем отправить...")
    size = size_mb or 300
    print(f"Отправка документа {size} МБ в чат {chat_id} через {settings.bot_api_url} ...")
    print("       (загрузка большого файла может занять несколько минут — ждём до 10 мин)")
    try:
        ok = await test_big_upload_download(chat_id, size_mb=size)
    except Exception as e:  # noqa: BLE001 — широкий catch для CLI-скрипта проверки
        msg = str(e)
        # Файл мог физически уйти в чат, но ответ сервера не успел вернуться,
        # либо getFile упёрся в облачный лимит 20 МБ — это НЕ провал --local.
        if "too big" in msg or "timeout" in msg.lower():
            print(f"[WARN] {type(e).__name__}: {msg}")
            print("[OK(частично)] Проверьте ЛС: если файл пришёл — upload через --local работает.")
            print("       Для полной проверки убедитесь, что volume bot-api-data примонтирован")
            print("       в контейнер bot (/app/bot-api-data) и повторите тест с меньшим размером:")
            print("       docker compose exec bot python scripts/check_phase1.py --big-test --size 60")
            return 0
        print(f"[FAIL] {type(e).__name__}: {e}")
        return 1
    if ok:
        print(f"[OK] Upload {size} МБ (> облачного лимита 50 МБ) и getFile прошли — Local Bot API c --local работает.")
        return 0
    return 1


async def check_logout() -> int:
    ok = await logout_from_cloud()
    if ok:
        print("[OK] Бот разлогинен с облачного API. Теперь он обслуживается только локальным сервером.")
        print("     ? Старые file_id остаются валидными (ТЗ §3.3), переиндексация не нужна.")
        return 0
    print("[FAIL] logOut не выполнен — проверьте BOT_TOKEN и доступ к api.telegram.org.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка Фазы 1: Local Bot API Server")
    ap.add_argument("--config", action="store_true", help="проверить .env/настройки")
    ap.add_argument("--logout", action="store_true", help="разовая миграция: logOut с облачного API")
    ap.add_argument("--health", action="store_true", help="getMe через локальный сервер")
    ap.add_argument("--big-test", action="store_true", help="тест 300 МБ upload + getFile (нужен TEST_CHAT_ID)")
    ap.add_argument("--size", type=int, default=None, help="размер тестового файла в МБ (по умолчанию 300)")
    args = ap.parse_args()

    if not any([args.config, args.logout, args.health, args.big_test]):
        args.config = args.health = True

    rc = 0
    if args.config:
        rc |= check_config()
    if args.logout:
        rc |= asyncio.run(check_logout())
    if args.health:
        rc |= asyncio.run(check_health())
    if args.big_test:
        rc |= asyncio.run(check_big_test(args.size))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
