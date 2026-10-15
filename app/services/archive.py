"""Сервис служебного канала (блок D — архив всех медиа).

При приёме медиа ботом — дублирует в служебный канал (от имени бота, через file_id).
Теги в подписи: #from_<source> #to_<channel> #<type>

Примеры:
- Фото из канала: #from_1001686639428 #to_1002257882757 #pic
- Альбом из ЛС: #from_dm #to_1002257882757 #pic #vid

Важно: chat_id каналов начинается с -100..., но в тегах минус убираем
(#from_1002435408188, не #from_-1002435408188) — иначе Telegram ломает хэштег.
"""
from __future__ import annotations

import logging
from typing import Any

from app.repositories import app_settings as app_settings_repo

log = logging.getLogger("archive")

# Маппинг типов файлов в теги
TYPE_TAGS = {
    "photo": "pic",
    "video": "vid",
    "gif": "gif",
    "document": "doc",
    "audio": "aud",
    "voice": "voice",
}


def _sanitize_tag_id(value: str | int | None) -> str:
    """Убирает минус из chat_id для тегов (Telegram ломает #from_-100...).

    -1002435408188 → 1002435408188
    """
    if value is None:
        return ""
    return str(value).lstrip("-")


def _source_tag(post: dict[str, Any], target_channel_id: int) -> str:
    """Формирует тег источника: #from_<chat_id> или #from_dm."""
    st = post.get("source_type", "")
    sid = post.get("source_id")
    if st in ("telegram_channel",) and sid:
        return f"#from_{_sanitize_tag_id(sid)}"
    # telegram_direct, telegram_user и всё из ЛС — #from_dm
    return "#from_dm"


def _build_tags(post: dict[str, Any], target_channel_id: int, all_types: list[str]) -> str:
    """Собирает строку тегов для подписи архивного сообщения."""
    tags = [
        _source_tag(post, target_channel_id),
        f"#to_{_sanitize_tag_id(target_channel_id)}",
    ]
    for ft in all_types:
        tag = TYPE_TAGS.get(ft, ft)
        hashtag = f"#{tag}"
        if hashtag not in tags:
            tags.append(hashtag)
    return " ".join(tags)


async def archive_post(post: dict[str, Any], target_channel_id: int, all_types: list[str] | None = None) -> bool:
    """Дублирует пост в служебный канал с тегами.

    all_types — список типов файлов (для альбома: ['photo', 'video']).
    Возвращает True если отправлено, False если архив выключен/ошибка.
    """
    archive_channel = await app_settings_repo.get_archive_channel_id()
    if not archive_channel:
        return False  # архив выключен

    tg_file_id = post.get("tg_file_id")
    if not tg_file_id:
        return False  # локальный файл — не архивируем (Фаза 5)

    types = all_types or [post.get("file_type", "document")]
    tags = _build_tags(post, target_channel_id, types)

    try:
        from app.bot.runtime import get_bot

        bot = get_bot()
        file_type = post.get("file_type", "document")
        caption = post.get("original_caption")
        full_caption = f"{tags}\n{caption}" if caption else tags
        if len(full_caption) > 1024:
            full_caption = full_caption[:1021] + "..."

        if file_type == "photo":
            await bot.send_photo(chat_id=archive_channel, photo=tg_file_id, caption=full_caption)
        elif file_type == "video":
            await bot.send_video(chat_id=archive_channel, video=tg_file_id, caption=full_caption)
        elif file_type == "gif":
            await bot.send_animation(chat_id=archive_channel, animation=tg_file_id, caption=full_caption)
        elif file_type == "audio":
            await bot.send_audio(chat_id=archive_channel, audio=tg_file_id, caption=full_caption)
        elif file_type == "voice":
            await bot.send_voice(chat_id=archive_channel, voice=tg_file_id, caption=full_caption)
        else:
            await bot.send_document(chat_id=archive_channel, document=tg_file_id, caption=full_caption)
        log.info("Пост #%s архивирован в канал %s", post.get("id"), archive_channel)
        return True
    except Exception as e:  # noqa: BLE001 — архив не критичен
        log.warning("Не удалось архивировать пост #%s: %s", post.get("id"), e)
        return False


async def archive_album(posts: list[dict[str, Any]], target_channel_id: int) -> bool:
    """Дублирует альбом (несколько постов) в служебный канал через send_media_group."""
    archive_channel = await app_settings_repo.get_archive_channel_id()
    if not archive_channel:
        return False

    all_types = list(dict.fromkeys(p.get("file_type", "document") for p in posts))
    tags = _build_tags(posts[0] if posts else {}, target_channel_id, all_types)

    try:
        from aiogram.types import InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo

        from app.bot.runtime import get_bot

        bot = get_bot()
        media: list[InputMediaPhoto | InputMediaVideo | InputMediaDocument | InputMediaAudio] = []
        sent_idx = 0
        for p in posts:
            fid = p.get("tg_file_id")
            if not fid:
                log.warning("Пост #%s альбома без tg_file_id — пропущен в архиве", p.get("id"))
                continue
            ft = p.get("file_type", "document")
            cap = tags if sent_idx == 0 else None
            sent_idx += 1
            if ft == "photo":
                media.append(InputMediaPhoto(media=fid, caption=cap))
            elif ft == "video":
                media.append(InputMediaVideo(media=fid, caption=cap))
            elif ft == "audio":
                media.append(InputMediaAudio(media=fid, caption=cap))
            else:
                media.append(InputMediaDocument(media=fid, caption=cap))

        if not media:
            return False
        # Обрезаем подпись первого элемента до 1024 (лимит Telegram для album caption)
        if media and media[0].caption and len(media[0].caption) > 1024:
            media[0].caption = media[0].caption[:1021] + "..."
        await bot.send_media_group(chat_id=archive_channel, media=media)
        log.info("Альбом (%d медиа) архивирован", len(media))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось архивировать альбом: %s", e)
        return False
