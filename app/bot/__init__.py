"""Инициализация Telegram-бота (aiogram 3.x, ТЗ §8).

Создаёт Bot (через Local Bot API Server, ТЗ §3.3) и Dispatcher с:
- AccessMiddleware (RBAC, ТЗ §2.6)
- роутерами handlers: menu (inline-кнопки), media, admin (текстовые команды-совместимость)
- MemoryStorage для FSM (многошаговое добавление канала/очереди/пользователя)
"""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import runtime
from app.bot.handlers import admin, media, menu, start
from app.bot.middleware.access import AccessMiddleware

log = logging.getLogger("bot")

_dp: Dispatcher | None = None


def get_bot() -> Bot:
    """Возвращает singleton Bot (через runtime, разрывает циклический импорт)."""
    return runtime.get_bot()


def get_dispatcher() -> Dispatcher:
    global _dp
    if _dp is not None:
        return _dp

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())

    # Порядок роутеров важен:
    # 1. menu — inline-кнопки + FSM (основной интерфейс)
    # 2. media — приём медиа из ЛС (file_id, маршрутизация)
    # 3. admin — текстовые команды (обратная совместимость)
    # 4. start — /help (fallback)
    dp.include_router(menu.router)
    dp.include_router(media.router)
    dp.include_router(admin.router)
    dp.include_router(start.router)
    _dp = dp
    return dp


async def close_bot() -> None:
    await runtime.close_bot()
