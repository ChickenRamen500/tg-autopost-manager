"""Приём медиа из ЛС (ТЗ §2.1.1, §4.2.1, §2.2 маршрутизация).

Стратегия file_id (ТЗ §2.1.1 п.4):
- При приёме медиа сохраняем tg_file_id + file_unique_id + размер + mime
- Если пост не требует обработки (was_processed=False) → отправка пойдёт по file_id
- Файл НЕ скачивается при приёме (экономия трафика/диска)
- Лимит приёма для бота: до 2 ГБ (пользователь прислал) — без ограничений

Поддерживаемые типы: photo, video, gif (animation), document.
Форварды: извлекаем медиа, source_type=telegram_channel, source_id=chat_id.
Медиагруппы: каждый файл — отдельный пост (ТЗ: «каждый файл как отдельный пост»).
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.types import (
    Animation,
    Audio,
    BufferedInputFile,  # noqa: F401 — оставлено для будущих фаз
    Document,
    Message,
    PhotoSize,
    Video,
    Voice,
)

from app.repositories import posts as posts_repo
from app.services.routing import route_post

logger = logging.getLogger("handlers.media")

router = Router(name="media")


def _pick_photo(photos: list[PhotoSize]) -> tuple[str, str | None, int | None]:
    """Выбирает максимальный размер фото (ТЗ §2.1.1: «выбирается максимальное»)."""
    best = max(photos, key=lambda p: (p.width or 0) * (p.height or 0))
    return best.file_id, best.file_unique_id, best.file_size


def _extract_media(message: Message) -> dict[str, Any] | None:
    """Извлекает медиа-данные из сообщения в единый словарь.

    Возвращает None, если медиа нет (тогда, возможно, это текст).
    """
    if message.photo:
        file_id, file_unique_id, size = _pick_photo(message.photo)
        return {"file_type": "photo", "tg_file_id": file_id, "file_unique_id": file_unique_id, "file_size": size,
                "mime_type": "image/jpeg"}
    if message.video:
        v: Video = message.video
        return {"file_type": "video", "tg_file_id": v.file_id, "file_unique_id": v.file_unique_id,
                "file_size": v.file_size, "mime_type": v.mime_type}
    if message.animation:
        a: Animation = message.animation
        return {"file_type": "gif", "tg_file_id": a.file_id, "file_unique_id": a.file_unique_id,
                "file_size": a.file_size, "mime_type": a.mime_type}
    if message.document:
        d: Document = message.document
        # Пытаемся определить «настоящий» тип по mime
        mime = (d.mime_type or "").lower()
        if mime.startswith("image/gif") or (d.file_name or "").lower().endswith(".gif"):
            ft = "gif"
        elif mime.startswith("image/"):
            ft = "photo"
        elif mime.startswith("video/"):
            ft = "video"
        else:
            ft = "document"
        return {"file_type": ft, "tg_file_id": d.file_id, "file_unique_id": d.file_unique_id,
                "file_size": d.file_size, "mime_type": d.mime_type}
    if message.audio:
        a: Audio = message.audio  # type: ignore[no-redef]
        return {"file_type": "document", "tg_file_id": a.file_id, "file_unique_id": a.file_unique_id,
                "file_size": a.file_size, "mime_type": a.mime_type}
    if message.voice:
        v: Voice = message.voice  # type: ignore[no-redef]
        return {"file_type": "document", "tg_file_id": v.file_id, "file_unique_id": v.file_unique_id,
                "file_size": v.file_size, "mime_type": v.mime_type}
    return None


def _extract_source(message: Message) -> dict[str, Any]:
    """Метаданные источника (ТЗ §2.2.1).

    aiogram 3.x: forward_origin — union с типами:
      - MessageOriginChannel (пересылка из канала): chat.id, chat.username, chat.title
      - MessageOriginChat (пересылка из чата/группы): chat.id, chat.username, chat.title, author
      - MessageOriginUser (пересылка от пользователя): sender_user.id, sender_user.username
      - MessageOriginHiddenUser (скрытый отправитель): sender_user_name (str)
    """
    fwd = message.forward_origin
    if fwd is not None:
        # MessageOriginChannel / MessageOriginChat — есть атрибут chat
        chat = getattr(fwd, "chat", None)
        if chat is not None:
            chat_id = getattr(chat, "id", None)
            username = getattr(chat, "username", None)
            title = getattr(chat, "title", None)
            source_channel = username or title
            return {
                "source_type": "telegram_channel",
                "source_id": str(chat_id) if chat_id is not None else source_channel,
                "source_channel": f"@{username}" if username else title,
            }
        # MessageOriginUser — пересылка от пользователя (не канал)
        sender = getattr(fwd, "sender_user", None)
        if sender is not None:
            return {
                "source_type": "telegram_user",
                "source_id": str(sender.id),
                "source_channel": f"@{sender.username}" if getattr(sender, "username", None) else None,
            }
        # MessageOriginHiddenUser — скрытый отправитель
        hidden_name = getattr(fwd, "sender_user_name", None)
        if hidden_name:
            return {
                "source_type": "telegram_user",
                "source_id": hidden_name,
                "source_channel": None,
            }
    # Не пересланное сообщение — отправитель из ЛС
    return {
        "source_type": "telegram_direct",
        "source_id": str(message.from_user.id) if message.from_user else None,
        "source_channel": None,
    }


@router.message(F.photo | F.video | F.animation | F.document | F.audio | F.voice)
async def receive_media(message: Message, user: dict | None = None) -> None:
    """Принимает медиа из ЛС, сохраняет пост, применяет маршрутизацию (ТЗ §4.2.1)."""
    media = _extract_media(message)
    if media is None:
        return

    source = _extract_source(message)
    # aiogram 3.x: message.caption — текст подписи (str | None); caption_raw убран в 3.x.
    caption = message.caption or None
    tags: list[str] = []  # теги из captions — Фаза 5 (caption-конвейер)
    # media_group_id: если сообщение часть альбома, Telegram присылает одинаковый ID
    # для всех файлов альбома (ТЗ §2.1.1). Сохраняем для групповой отправки.
    media_group_id = getattr(message, "media_group_id", None)

    # Дедупликация по file_unique_id (ТЗ §2.4.3 — точный уровень)
    if media.get("file_unique_id"):
        existing = await posts_repo.find_by_file_unique_id(media["file_unique_id"])
        if existing:
            await message.answer(
                f"⚠️ Похоже на дубликат: file_unique_id уже есть в системе "
                f"(пост #{existing[0]['id']}, статус: {existing[0]['status']}).\n"
                "Пост всё равно добавлен во «Входящие» как possible_duplicate для ручной проверки."
            )
            # Помечаем как возможный дубликат, очередь не назначаем (входящие)
            post_id = await posts_repo.create_post(
                source_type=source["source_type"],
                source_id=source["source_id"],
                source_channel=source["source_channel"],
                file_type=media["file_type"],
                tg_file_id=media["tg_file_id"],
                file_unique_id=media["file_unique_id"],
                file_size=media["file_size"],
                mime_type=media["mime_type"],
                original_caption=caption,
                tags=tags,
                queue_id=None,
                status="possible_duplicate",
                media_group_id=media_group_id,
            )
            logger.info("Дубликат #%s (file_unique_id) — possible_duplicate", post_id)
            return

    # Маршрутизация (ТЗ §2.2.2)
    queue_id = await route_post(source["source_type"], source["source_id"], tags)

    post_id = await posts_repo.create_post(
        source_type=source["source_type"],
        source_id=source["source_id"],
        source_channel=source["source_channel"],
        file_type=media["file_type"],
        tg_file_id=media["tg_file_id"],
        file_unique_id=media["file_unique_id"],
        file_size=media["file_size"],
        mime_type=media["mime_type"],
        original_caption=caption,
        tags=tags,
        queue_id=queue_id,
        status="pending",
        media_group_id=media_group_id,
    )

    # Альбом: Telegram присылает каждое фото отдельным сообщением с одинаковым media_group_id.
    # Каждый файл = отдельный пост (ТЗ §2.1.1: «каждый файл как отдельный пост или групповой пост»).
    # Сокращаем уведомление, чтобы не спамить при альбомах из N файлов.
    is_album = bool(getattr(message, "media_group_id", None))

    if queue_id is None:
        msg = f"📥 Пост #{post_id} → Входящие"
        if is_album:
            msg += " (альбом — каждый файл отдельным постом)"
        else:
            msg += "\nПравило маршрутизации не найдено. /menu → 🔀 Маршрутизация"
        await message.answer(msg)
    else:
        from app.repositories import queues as queues_repo

        queue = await queues_repo.get_queue(queue_id)
        qname = queue["name"] if queue else f"#{queue_id}"
        pending_count = await queues_repo.count_pending(queue_id)
        await message.answer(
            f"✓ Пост #{post_id} → очередь «{qname}» (позиция ~{pending_count})"
        )
    logger.info("Принят пост #%s: type=%s source=%s queue=%s album=%s", post_id, media["file_type"], source["source_type"], queue_id, is_album)


@router.message(F.text)
async def receive_text(message: Message, user: dict | None = None) -> None:
    """Текст без команды — сохраняем как возможный caption-черновик во Входящие (ТЗ §2.1.1 п.1 'Текст')."""
    if message.text and message.text.startswith("/"):
        return  # команды обрабатываются другими хендлерами
    # Текст сам по себе не создаёт пост (нет медиа), но логируем для будущего
    logger.info("Текст без медиа от user=%s — игнорируется", getattr(message.from_user, "id", None))
