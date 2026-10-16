"""Безопасные обёртки для Telegram API вызовов.

aiogram 3.x TelegramBadRequest с "message is not modified" — типичная проблема
при edit_text/edit_reply_markup: если новый контент идентичен текущему сообщению,
Telegram возвращает 400. Это не ошибка, а нормальная ситуация (идемпотентный update).

Также обрабатываем TelegramRetryAfter (flood control) — sleep + retry.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

log = logging.getLogger(__name__)


async def safe_edit_text(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    **kwargs: Any,
) -> bool:
    """Делает message.edit_text, ловя 'message is not modified' и TelegramRetryAfter.

    Возвращает True если edit прошёл успешно, False если:
    - 'message is not modified' (контент идентичен — это OK, не ошибка)
    - другая TelegramBadRequest (например, message too long, parse error)
    - TelegramRetryAfter — пробуем 1 раз, если опять упало — False

    Используется во всех handlers где edit_text вызывается с динамическим контентом
    (например, перерисовка карточки поста после action).

    parse_mode наследуется от Bot config (DefaultBotProperties) — НЕ передаём
    явно, иначе mock-объекты в тестах ломаются на неожиданном kwarg.
    Дополнительные kwargs (parse_mode, disable_web_page_preview и т.д.) можно
    передать через **kwargs — они будут прокинуты в edit_text.
    """
    call_kwargs: dict[str, Any] = {"text": text}
    if reply_markup is not None:
        call_kwargs["reply_markup"] = reply_markup
    call_kwargs.update(kwargs)

    try:
        await message.edit_text(**call_kwargs)
        return True
    except TelegramRetryAfter as e:
        retry = float(e.retry_after) + 0.5
        log.warning("Flood control на edit_text: ждём %.1f сек, retry", retry)
        await asyncio.sleep(retry)
        try:
            await message.edit_text(**call_kwargs)
            return True
        except TelegramBadRequest as e2:
            if "not modified" in str(e2).lower():
                return False  # идемпотентный update — не ошибка
            log.warning("edit_text упал после retry: %s", e2)
            return False
        except Exception as e3:  # noqa: BLE001
            log.warning("edit_text retry упал: %s", e3)
            return False
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            # Контент идентичен — это OK (например, нажали retry когда статус уже pending).
            # Молча возвращаем False — handler не должен падать.
            return False
        log.warning("edit_text TelegramBadRequest: %s", e)
        return False
    except Exception as e:  # noqa: BLE001 — edit не должен ронять handler
        log.warning("edit_text упал: %s", e)
        return False


async def safe_answer(
    call: CallbackQuery,
    text: str,
    *,
    show_alert: bool = False,
) -> bool:
    """Делает call.answer, ловя TelegramRetryAfter (flood control).

    Возвращает True если answer прошёл, False если упал.
    """
    try:
        await call.answer(text, show_alert=show_alert)
        return True
    except TelegramRetryAfter as e:
        retry = float(e.retry_after) + 0.5
        log.warning("Flood control на call.answer: ждём %.1f сек, retry", retry)
        await asyncio.sleep(retry)
        try:
            await call.answer(text, show_alert=show_alert)
            return True
        except Exception as e2:  # noqa: BLE001
            log.warning("call.answer retry упал: %s", e2)
            return False
    except Exception as e:  # noqa: BLE001
        log.warning("call.answer упал: %s", e)
        return False
