"""Инициализация Telegram-бота (aiogram 3.x, ТЗ §8).

Создаёт Bot (через Local Bot API Server, ТЗ §3.3) и Dispatcher с:
- AccessMiddleware (RBAC, ТЗ §2.6)
- роутерами handlers: start, media, admin
"""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher

from app.bot.handlers import admin, media, start
from app.bot.middleware.access import AccessMiddleware
from app.utils.local_bot_api import create_bot

log = logging.getLogger("bot")

_bot: Bot | None = None
_dp: Dispatcher | None = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        # create_bot подключает сессию к Local Bot API Server (ТЗ §3.3)
        _bot = create_bot()
    return _bot


def get_dispatcher() -> Dispatcher:
    global _dp
    if _dp is not None:
        return _dp

    dp = Dispatcher()
    # RBAC: проверка доступа по роли перед каждым хендлером (ТЗ §2.6)
    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())

    # Порядок важен: media ловит контентные сообщения, start — команды /start /help,
    # admin — остальные команды. Регистрируем admin первым, чтобы команды перехватывались
    # раньше общего media-хендлера (у media фильтр по content_types, не команды —
    # но для надёжности ставим admin + start первыми).
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(media.router)
    _dp = dp
    return dp


async def close_bot() -> None:
    global _bot
    if _bot is not None:
        await _bot.session.close()
        _bot = None
