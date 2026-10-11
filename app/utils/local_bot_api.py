"""Инфраструктура Local Bot API Server (ТЗ §3.3).

Содержит:
- create_bot() — подключение aiogram к локальному серверу через TelegramAPIServer.from_base
                  (ключевой момент: без --local-сервера действуют лимиты 50 МБ upload / 20 МБ getFile);
- logout_from_cloud() — разовая миграция бота с облачного API (curl logOut из ТЗ),
                       без неё локальный сервер вернёт 401/409;
- get_me() / healthcheck() — проверка связности с локальным сервером;
- make_test_file() / test_big_upload_download() — проверка из п.4 Фазы 1:
                       отправка файла ~300 МБ и getFile файла > 20 МБ.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path

import aiohttp
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from app.config.settings import settings

log = logging.getLogger("local_bot_api")

UPLOAD_LIMIT_CLOUD_MB = 50      # облачный Bot API
DOWNLOAD_LIMIT_CLOUD_MB = 20    # getFile через облако
UPLOAD_LIMIT_LOCAL_MB = 2000    # local-bot-api с --local (ТЗ §3.3)


def create_bot() -> Bot:
    """Bot, подключённый к Local Bot API Server (код из ТЗ §3.3)."""
    session = AiohttpSession(
        api=TelegramAPIServer.from_base(settings.bot_api_url)
    )
    return Bot(token=settings.bot_token, session=session)


async def logout_from_cloud() -> bool:
    """Разлогинивает бота с облачного Bot API (обязательно ОДИН РАЗ перед работой через локальный сервер).

    curl https://api.telegram.org/bot<TOKEN>/logOut  →  {"ok":true,"result":true}
    Повторный вызов безопасен (идемпотентен).
    """
    url = f"https://api.telegram.org/bot{settings.bot_token}/logOut"
    async with aiohttp.ClientSession() as s, s.post(url) as r:
        data = await r.json()
        ok = bool(data.get("ok"))
        log.info("logOut облачного API: %s", data)
        return ok


async def get_me() -> dict | None:
    """GET /getMe на локальном сервере — базовый healthcheck."""
    url = f"{settings.bot_api_url}/bot{settings.bot_token}/getMe"
    try:
        async with aiohttp.ClientSession() as s, s.get(url) as r:
            data = await r.json()
            if data.get("ok"):
                return data["result"]
            log.error("getMe вернул ошибку: %s", data)
            return None
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.error("Local Bot API недоступен по %s: %s", settings.bot_api_url, e)
        return None


def make_test_file(size_mb: int, dest_dir: Path) -> Path:
    """Создаёт случайный файл заданного размера (не сжимается Telegram как документ)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"test_{size_mb}mb_{secrets.token_hex(4)}.bin"
    chunk = 1024 * 1024
    with open(path, "wb") as f:
        for _ in range(size_mb):
            f.write(secrets.token_bytes(chunk))
    return path


async def test_big_upload_download(chat_id: int, size_mb: int = 300) -> bool:
    """Проверка п.4 Фазы 1 ТЗ: загрузка документа ~300 МБ (>лимит облака 50 МБ)
    и скачивание его обратно через getFile (>лимит облака 20 МБ).

    Работает только если local-bot-api запущен с --local, иначе Telegram
    отклонит загрузку file_too_big.
    """
    if not chat_id:
        raise ValueError("chat_id не задан (TEST_CHAT_ID в .env)")

    tmp = make_test_file(size_mb, settings.downloads_dir)
    ok = False
    try:
        bot = create_bot()
        async with bot:
            doc = await bot.send_document(chat_id, document=tmp.open("rb"),
                                          caption=f"Фаза 1: тест загрузки {size_mb} МБ")
            log.info("Отправлено: file_unique_id=%s", doc.document.file_unique_id)
            file = await bot.get_file(doc.document.file_id)
            # На сервере с --local file.file_path — абсолютный локальный путь уже скачанного файла
            log.info("getFile: file_path=%s", file.file_path)
            await bot.send_message(chat_id, "✅ Фаза 1: upload>50МБ и getFile>20МБ прошли — --local работает.")
            ok = True
    finally:
        tmp.unlink(missing_ok=True)  # временные файлы удаляем сразу (ТЗ §3.1)
    return ok
