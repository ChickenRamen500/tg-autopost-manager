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

import asyncio
import logging
import os
import time
from typing import Any

from app.repositories import app_settings as app_settings_repo

log = logging.getLogger("archive")

# --- Throttle для отправки в служебный канал (защита от flood control) ---
#
# Контекст: при массовой отправке постов/альбомов (например, импорт 10 альбомов
# или обработка нескольких постов из очереди подряд) бот шлёт много запросов в
# служебный канал за короткое время. Telegram возвращает 429 Too Many Requests
# с retry_after (часто 5 сек):
#     "Flood control exceeded on method 'SendMediaGroup' in chat -100...
#      Retry in 5 seconds. Original description: Too Many Requests: retry after 5"
#
# Решение: глобальный throttle (минимальная пауза ARCHIVE_SEND_INTERVAL между
# запросами в служебный канал) + retry при TelegramRetryAfter (sleep retry_after
# + 0.5 сек, повтор один раз). Throttle через asyncio.Lock — гарантирует, что
# даже concurrent archive_post/archive_album вызовы идут последовательно.
#
# Лимит Telegram: ~30 сообщений в минуту в один chat → 2 сек между запросами
# с запасом. ARCHIVE_SEND_INTERVAL=1.5 даёт ~40 запросов/мин (с учётом send_media_group
# считается как 1 сообщение — безопасно).

_archive_lock: asyncio.Lock | None = None
_last_archive_send_time: float = 0.0
ARCHIVE_SEND_INTERVAL: float = float(os.getenv("ARCHIVE_SEND_INTERVAL", "1.5"))


def _get_archive_lock() -> asyncio.Lock:
    """Lazy-init Lock (не на module-level — иначе event loop ещё не запущен при импорте)."""
    global _archive_lock
    if _archive_lock is None:
        _archive_lock = asyncio.Lock()
    return _archive_lock


async def _throttled_archive_send(
    send_callable,
    *,
    op_name: str,
    post_id: int | None = None,
) -> Any | None:
    """Отправляет в служебный канал с throttle + retry на TelegramRetryAfter.

    Гарантирует:
    - Минимальная пауза ARCHIVE_SEND_INTERVAL между запросами (даже если concurrent callers)
    - При 429 RetryAfter — sleep retry_after + 0.5 сек, retry 1 раз
    - Любые другие ошибки — логируются, не роняют процесс (архив не критичен)

    Возвращает результат send_callable или None при ошибке.
    """
    from aiogram.exceptions import TelegramRetryAfter

    global _last_archive_send_time
    lock = _get_archive_lock()

    async with lock:
        # Throttle: спим, если с последней отправки прошло меньше ARCHIVE_SEND_INTERVAL
        elapsed = time.monotonic() - _last_archive_send_time
        if elapsed < ARCHIVE_SEND_INTERVAL:
            await asyncio.sleep(ARCHIVE_SEND_INTERVAL - elapsed)

        try:
            result = await send_callable()
        except TelegramRetryAfter as e:
            retry = float(e.retry_after) + 0.5
            log.warning(
                "Flood control на %s (пост #%s): ждём %.1f сек, retry",
                op_name, post_id, retry,
            )
            await asyncio.sleep(retry)
            try:
                result = await send_callable()
            except Exception as e2:  # noqa: BLE001 — архив не критичен
                log.warning("Retry %s тоже упал (пост #%s): %s", op_name, post_id, e2)
                return None
        except Exception as e:  # noqa: BLE001 — архив не критичен
            log.warning("Не удалось выполнить %s (пост #%s): %s", op_name, post_id, e)
            return None

        # Обновляем время последней отправки (даже если была RetryAfter — после
        # успешного retry сервер Telegram уже не считает запрос flood'ом, но
        # всё равно лучше выдержать паузу перед следующим)
        _last_archive_send_time = time.monotonic()
        return result

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

    Поддерживает два режима:
    1. tg_file_id (Telegram-файлы из ЛС) — отправка по file_id.
    2. file_path (скачанные внешние файлы: gelbooru и т.д.) — отправка через FSInputFile.
    """
    archive_channel = await app_settings_repo.get_archive_channel_id()
    if not archive_channel:
        return False  # архив выключен

    tg_file_id = post.get("tg_file_id")
    file_path = post.get("file_path")
    if not tg_file_id and not file_path:
        return False  # нет ни file_id, ни file_path — не архивируем

    types = all_types or [post.get("file_type", "document")]
    tags = _build_tags(post, target_channel_id, types)

    try:
        import os

        from aiogram.types import FSInputFile

        from app.bot.runtime import get_bot

        bot = get_bot()
        file_type = post.get("file_type", "document")
        caption = post.get("original_caption")
        full_caption = f"{tags}\n{caption}" if caption else tags
        if len(full_caption) > 1024:
            full_caption = full_caption[:1021] + "..."

        # Определяем медиа-объект: tg_file_id (приоритет) или FSInputFile из file_path
        media_obj = tg_file_id
        if not media_obj and file_path:
            media_obj = FSInputFile(file_path, filename=os.path.basename(file_path))

        post_id = post.get("id")
        if file_type == "photo":
            await _throttled_archive_send(
                lambda: bot.send_photo(chat_id=archive_channel, photo=media_obj, caption=full_caption),
                op_name="send_photo", post_id=post_id,
            )
        elif file_type == "video":
            await _throttled_archive_send(
                lambda: bot.send_video(chat_id=archive_channel, video=media_obj, caption=full_caption),
                op_name="send_video", post_id=post_id,
            )
        elif file_type == "gif":
            await _throttled_archive_send(
                lambda: bot.send_animation(chat_id=archive_channel, animation=media_obj, caption=full_caption),
                op_name="send_animation", post_id=post_id,
            )
        elif file_type == "audio":
            await _throttled_archive_send(
                lambda: bot.send_audio(chat_id=archive_channel, audio=media_obj, caption=full_caption),
                op_name="send_audio", post_id=post_id,
            )
        elif file_type == "voice":
            await _throttled_archive_send(
                lambda: bot.send_voice(chat_id=archive_channel, voice=media_obj, caption=full_caption),
                op_name="send_voice", post_id=post_id,
            )
        else:
            await _throttled_archive_send(
                lambda: bot.send_document(chat_id=archive_channel, document=media_obj, caption=full_caption),
                op_name="send_document", post_id=post_id,
            )
        log.info("Пост #%s архивирован в канал %s", post_id, archive_channel)
        return True
    except Exception as e:  # noqa: BLE001 — архив не критичен (fallback дляunexpected errors outside send)
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

        # post_id первого поста альбома — для логов
        first_post_id = posts[0].get("id") if posts else None

        result = await _throttled_archive_send(
            lambda: bot.send_media_group(chat_id=archive_channel, media=media),
            op_name="send_media_group", post_id=first_post_id,
        )
        if result is None:
            log.warning("Не удалось архивировать альбом (первый пост #%s) — все retry исчерпаны", first_post_id)
            return False
        log.info("Альбом (%d медиа) архивирован", len(media))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось архивировать альбом: %s", e)
        return False
