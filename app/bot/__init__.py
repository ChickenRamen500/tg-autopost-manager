"""Инициализация Telegram-бота (aiogram 3.x, ТЗ §8).

Создаёт Bot (через Local Bot API Server, ТЗ §3.3) и Dispatcher с:
- AccessMiddleware (RBAC, ТЗ §2.6)
- роутерами handlers: menu (inline-кнопки), media, admin (текстовые команды-совместимость)
- MemoryStorage для FSM (многошаговое добавление канала/очереди/пользователя)
"""
from __future__ import annotations

import asyncio
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

    # Порядок роутеров важен (CRITICAL из code review):
    # 1. menu — inline-кнопки + FSM (основной интерфейс) + /cancel /menu /start
    # 2. admin — текстовые команды (/list_queues, /add_user и т.д.) — ДО media,
    #    т.к. media.receive_text ловит F.text (любой текст) и return для команд,
    #    что молча проглатывало бы все admin-команды
    # 3. media — приём медиа из ЛС (file_id, маршрутизация)
    # 4. start — /help (fallback)
    dp.include_router(menu.router)
    dp.include_router(admin.router)
    dp.include_router(media.router)
    dp.include_router(start.router)

    # Глобальный error-handler: не роняем dispatcher из-за необработанной ошибки в хендлере.
    # Логируем и пытаемся сообщить пользователю.
    #
    # ВАЖНО: aiogram 3.x dp.errors() handler signature — (event: ErrorEvent).
    # ErrorEvent содержит поля `update` и `exception`. НЕ передаём `update`
    # отдельным параметром (старая сигнатура on_error(event, update) падала с
    # TypeError: missing 1 required positional argument: 'update' при обработке
    # исключения в handler'е — маскируя первичную ошибку).
    try:
        from aiogram.exceptions import TelegramRetryAfter
        from aiogram.types import ErrorEvent

        @dp.errors()
        async def on_error(event: ErrorEvent) -> None:  # type: ignore[unused-ignore]
            log.exception("Unhandled exception in handler: %s", event.exception)
            try:
                update = event.update
                if update.callback_query:
                    try:
                        await update.callback_query.answer(
                            "⚠️ Внутренняя ошибка. Попробуй позже.",
                            show_alert=True,
                        )
                    except TelegramRetryAfter as e:
                        await asyncio.sleep(e.retry_after)
                        await update.callback_query.answer(
                            "⚠️ Внутренняя ошибка. Попробуй позже.",
                            show_alert=True,
                        )
                elif update.message:
                    try:
                        await update.message.answer("⚠️ Произошла ошибка. /menu для возврата.")
                    except TelegramRetryAfter as e:
                        await asyncio.sleep(e.retry_after)
                        await update.message.answer("⚠️ Произошла ошибка. /menu для возврата.")
            except Exception:  # noqa: BLE001 — не роняем dispatcher из-за ошибки в error-handler
                pass
    except ImportError:
        log.warning("aiogram ErrorEvent недоступен — глобальный error-handler не зарегистрирован")

    _dp = dp
    return dp


async def close_bot() -> None:
    await runtime.close_bot()
