"""Работа с file_id отправленных Telegram-сообщений (общий код).

Захват file_id после успешной отправки/архивации — ключевой механизм:
у постов из расширения, парсеров и локальных файлов исходного tg_file_id нет,
и каждая отправка перечитывала файл с диска. Telegram возвращает Message
с file_id после первой загрузки — сохраняем его в posts.tg_file_id, после чего
scheduler отправляет по file_id (стратегия 1) без файла на диске.
"""
from __future__ import annotations

from typing import Any

from aiogram.types import Message


def extract_file_id_from_message(message: Any) -> str | None:
    """Извлекает file_id из Message (или из первого элемента list[Message]).

    Поддерживает все типы медиа: photo (берётся наибольший размер),
    video, animation (gif), audio, voice, document, sticker.

    Возвращает None, если структура неожиданная (например, моки в тестах) —
    вызов всегда безопасен и никогда не бросает исключений.
    """
    try:
        if isinstance(message, (list, tuple)):
            message = message[0] if message else None
        if not isinstance(message, Message):
            return None
        if message.photo:
            return message.photo[-1].file_id  # наибольший размер
        for attr in ("video", "animation", "audio", "voice", "document", "sticker"):
            obj = getattr(message, attr, None)
            if obj is not None:
                return obj.file_id
    except Exception:  # noqa: BLE001 — захват file_id не должен ломать отправку
        return None
    return None
