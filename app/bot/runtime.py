"""Runtime-ссылка на Bot — разрывает циклический импорт.

app.bot импортирует handlers (menu/media/admin), которые импортируют app.scheduler,
который импортирует app.bot (для get_bot). Чтобы разорвать цикл, Bot хранится здесь.

get_bot() лениво создаёт singleton через create_bot() при первом вызове —
это позволяет избежать проблемы «порядка инициализации» (когда set_bot
вызывается после get_bot и падает с RuntimeError).
"""
from __future__ import annotations

import logging

from aiogram import Bot

from app.utils.local_bot_api import create_bot

log = logging.getLogger("bot.runtime")

_bot: Bot | None = None


def set_bot(bot: Bot) -> None:
    """Явная установка Bot (для тестов/кастомной конфигурации)."""
    global _bot
    _bot = bot


def get_bot() -> Bot:
    """Возвращает singleton Bot. При первом вызове создаёт через create_bot()
    (подключение к Local Bot API Server, ТЗ §3.3).
    """
    global _bot
    if _bot is None:
        _bot = create_bot()
        log.info("Bot создан (singleton, подключение к Local Bot API)")
    return _bot


async def close_bot() -> None:
    global _bot
    # Отменяем все pending album-archive tasks (cleanup при shutdown)
    try:
        from app.bot.handlers.media import _album_archive_tasks

        for task in list(_album_archive_tasks.values()):
            if not task.done():
                task.cancel()
        _album_archive_tasks.clear()
    except ImportError:
        pass  # media-хендлеры не импортированы (например, в тестах)
    if _bot is not None:
        await _bot.session.close()
        _bot = None
