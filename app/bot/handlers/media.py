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

import asyncio
import io
import logging
from typing import Any

from aiogram import F, Router
from aiogram.types import (
    Animation,
    Audio,
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


async def _safe_answer(message: Message, text: str, **kwargs) -> None:
    """Отправляет ответ, ловя TelegramRetryAfter (flood control).

    При RetryAfter — ждём retry_after + 0.5 сек и повторяем один раз.
    При других ошибках — логируем и пропускаем (не роняем хендлер).
    """
    from aiogram.exceptions import TelegramRetryAfter

    try:
        await message.answer(text, **kwargs)
    except TelegramRetryAfter as e:
        retry = float(e.retry_after) + 0.5
        logger.warning("Flood control: ждём %.1f сек, retry", retry)
        await asyncio.sleep(retry)
        try:
            await message.answer(text, **kwargs)
        except Exception:  # noqa: BLE001
            logger.warning("Retry ответа тоже упал — пропускаем")
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось отправить ответ: %s", e)

# Debounce для групповой архивации альбомов в служебный канал.
_album_archive_tasks: dict[str, asyncio.Task] = {}
ALBUM_DEBOUNCE_SEC = 1.5


def _schedule_album_archive(media_group_id: str, target_channel_id: int) -> None:
    """Откладывает групповую архивацию альбома на ALBUM_DEBOUNCE_SEC.

    Telegram присылает альбом как N сообщений с одинаковым media_group_id.
    Каждое сообщение создаёт отдельный пост. Чтобы архивировать альбом как
    группу (send_media_group), ждём пока все посты создадутся, затем
    архивируем одним вызовом.
    """
    old_task = _album_archive_tasks.get(media_group_id)
    if old_task is not None and not old_task.done():
        old_task.cancel()

    async def _do_archive() -> None:
        try:
            await asyncio.sleep(ALBUM_DEBOUNCE_SEC)
        except asyncio.CancelledError:
            return
        _album_archive_tasks.pop(media_group_id, None)
        try:
            from app.db import get_db
            from app.services.archive import archive_album

            db = get_db()
            cur = await db.execute(
                "SELECT * FROM posts WHERE media_group_id = ? ORDER BY created_at ASC, id ASC LIMIT 10",
                (media_group_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
            posts = [dict(r) for r in rows]
            if not posts:
                return
            await archive_album(posts, target_channel_id)
            logger.info("Альбом %s архивирован группой (%d постов)", media_group_id, len(posts))
        except Exception as e:  # noqa: BLE001
            logger.warning("Групповая архивация альбома %s не удалась: %s", media_group_id, e)

    _album_archive_tasks[media_group_id] = asyncio.create_task(_do_archive())


def _pick_photo(photos: list[PhotoSize]) -> tuple[str, str | None, int | None]:
    """Выбирает максимальный размер фото (ТЗ §2.1.1: «выбирается максимальное»)."""
    best = max(photos, key=lambda p: (p.width or 0) * (p.height or 0))
    return best.file_id, best.file_unique_id, best.file_size


def _pick_thumbnail(photos: list[PhotoSize]) -> PhotoSize | None:
    """Выбирает минимальный размер фото для pHash-проверки (только thumbnail).

    pHash устойчив к ресайзу, поэтому thumbnail (~80×80) даёт тот же хеш что и
    оригинал. Скачивание thumbnail —几 KB вместо мегабайт, не нарушает принцип
    «файл не скачивается при приёме» существенно.
    """
    if not photos:
        return None
    return min(photos, key=lambda p: (p.width or 0) * (p.height or 0))


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
        return {"file_type": "audio", "tg_file_id": a.file_id, "file_unique_id": a.file_unique_id,
                "file_size": a.file_size, "mime_type": a.mime_type}
    if message.voice:
        v: Voice = message.voice  # type: ignore[no-redef]
        return {"file_type": "voice", "tg_file_id": v.file_id, "file_unique_id": v.file_unique_id,
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


async def _check_phash_duplicate(message: Message, post_id: int, media: dict[str, Any]) -> list[dict[str, Any]]:
    """Проверяет визуальный дубликат по pHash для фото из ЛС (ТЗ §2.4.3, Фаза 3 этап 2).

    Для photo: скачивает thumbnail (минимальный размер,几 KB), вычисляет pHash,
    сохраняет в БД (set_phash_atomic), ищет дубликаты (find_phash_near).
    Если найден → пост помечается possible_duplicate.

    Возвращает список найденных дубликатов (пустой = дубликатов нет).
    Никогда не падает — возвращает [] при любой ошибке (приём не должен роняться
    из-за дедупликации).

    Для video/gif/document — пропускается (pHash только для изображений;
    для видео нужен ffmpeg-кадр — Фаза 5).
    """
    if media["file_type"] != "photo":
        return []
    if not message.photo:
        return []
    try:
        from app.config.settings import settings

        if not settings.phash_enabled or not settings.phash_check_on_receive:
            return []

        # Thumbnail — самый маленький размер (быстро скачать, pHash устойчив к ресайзу)
        thumb = _pick_thumbnail(message.photo)
        if thumb is None:
            return []

        from app.bot.runtime import get_bot

        bot = get_bot()
        buf = io.BytesIO()
        # bot.download пишет в IO объект (destination=buf)
        await bot.download(thumb.file_id, destination=buf)
        data = buf.getvalue()
        if not data:
            logger.warning("pHash: thumbnail поста #%s скачан пустым — пропуск", post_id)
            return []

        from app.services.dedup.phash import check_visual_duplicate, compute_phash_from_bytes

        phash = compute_phash_from_bytes(data)
        if phash is None:
            return []

        # Атомарно сохраняем pHash в posts + duplicate_hashes
        from app.repositories import duplicates as dup_repo

        await dup_repo.set_phash_atomic(post_id, phash)
        logger.info("pHash поста #%s (при приёме): %s", post_id, phash)

        # Ищем визуальные дубликаты (исключая сам пост — он только что сохранён с этим pHash)
        dupes = await check_visual_duplicate(phash, exclude_post_id=post_id)
        if dupes:
            # Помечаем пост как possible_duplicate для ручной проверки
            await posts_repo.update_status(post_id, "possible_duplicate")
            logger.warning(
                "Пост #%s: визуальный дубликат (pHash=%s, distance≤%d): похожие %s",
                post_id, phash, settings.phash_max_distance,
                [d["post_id"] for d in dupes[:5]],
            )
        return dupes
    except Exception as e:  # noqa: BLE001 — дедупликация не должна ронять приём
        logger.warning("pHash-проверка поста #%s не удалась (пропуск): %s", post_id, e)
        return []


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

    # Дедупликация по file_unique_id (ТЗ §2.4.3 — точный уровень).
    # Реализация: атомарный INSERT ... ON CONFLICT(file_unique_id) DO NOTHING
    # (CR-H-V1 TOCTOU fix) — раньше SELECT-then-INSERT допускал гонку: два
    # одновременных апдейта с одним file_unique_id могли оба пройти SELECT
    # (оба увидели «нет дубликата»), затем оба INSERT'нуть — получали два
    # одинаковых поста. Теперь один INSERT либо вставляет (rowcount=1), либо
    # тихо игнорирует (rowcount=0, конфликт по partial UNIQUE index).
    # Маршрутизацию выполняем ДО вставки (дёшево: один SELECT по routing_rules).
    queue_id = await route_post(source["source_type"], source["source_id"], tags)

    has_file_unique = bool(media.get("file_unique_id"))
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
        on_conflict_file_unique_do_nothing=has_file_unique,
    )

    if has_file_unique and post_id == 0:
        # Конфликт: пост с таким file_unique_id уже существует.
        # Информируем пользователя (со ссылкой на существующий пост),
        # но НЕ создаём дубликат — старый код создавал junk-пост со
        # статусом possible_duplicate, что само по себе нарушало UNIQUE
        # теперь (idx_posts_file_unique_unique в schema.sql).
        existing = await posts_repo.find_by_file_unique_id(media["file_unique_id"])
        existing_id = existing[0]["id"] if existing else "?"
        existing_status = existing[0]["status"] if existing else "?"
        await _safe_answer(message,
            f"⚠️ Дубликат: file_unique_id уже в системе "
            f"(пост #{existing_id}, статус: {existing_status}).\n"
            "Пост не добавлен повторно. Если это новый файл — отправь его ещё раз."
        )
        logger.info("Дубликат отклонён (file_unique_id conflict) — существующий #%s", existing_id)
        return

    # pHash-дедупликация при приёме (ТЗ §2.4.3, Фаза 3 этап 2):
    # Для фото — скачиваем thumbnail (мини-размер,几 KB) и вычисляем pHash.
    # Если найден визуальный дубликат → пост помечается possible_duplicate
    # (вместо pending) для ручной проверки модератором. pHash сохраняется в БД
    # для будущих сравнений. Не роняет приём при ошибке.
    phash_dupe = await _check_phash_duplicate(message, post_id, media)
    if phash_dupe:
        dupe_ids = [d["post_id"] for d in phash_dupe[:3]]
        await _safe_answer(message,
            f"⚠️ Пост #{post_id}: возможный визуальный дубликат (pHash). "
            f"Похожие посты: {dupe_ids}. Пост помечен для проверки.",
            reply_to_message_id=message.message_id,
        )

    # Альбом: Telegram присылает каждое фото отдельным сообщением с одинаковым media_group_id.
    is_album = bool(getattr(message, "media_group_id", None))

    # Архивация в служебный канал (блок D) — от имени бота, через file_id.
    # Для альбомов: debounce 1.5 сек → архивируем группой через send_media_group.
    # Для одиночных медиа: сразу через archive_post.
    target_ch = 0
    if queue_id is not None:
        from app.repositories import channels as channels_repo
        from app.repositories import queues as queues_repo
        q = await queues_repo.get_queue(queue_id)
        if q:
            ch = await channels_repo.get_channel(q["channel_id"])
            if ch:
                target_ch = ch["tg_chat_id"]

    if is_album and media_group_id:
        # Альбом: отложенная групповая архивация
        _schedule_album_archive(media_group_id, target_ch)
    else:
        # Одиночное медиа — архивируем сразу
        try:
            from app.services.archive import archive_post

            post_dict = await posts_repo.get_post(post_id)
            if post_dict:
                await archive_post(post_dict, target_ch, [media["file_type"]])
        except Exception as e:  # noqa: BLE001
            logger.warning("Архивация поста #%s не удалась: %s", post_id, e)

    if queue_id is None:
        # Пост во Входящих. Если источник — ЛС (telegram_direct/telegram_user),
        # показываем интерактивный выбор очереди (блок A): кнопки каналов → очередей.
        from app.bot.keyboards import builder as kb
        from app.repositories import channels as channels_repo

        channels = await channels_repo.list_channels(active_only=True)
        if source["source_type"] in ("telegram_direct", "telegram_user") and channels:
            # Интерактивный выбор: «куда отправить?» с кнопками каналов
            pick_kb = kb.pick_channel_for_post(post_id, channels)
            msg = f"📥 Пост #{post_id} → Входящие. Куда отправить?"
            if is_album:
                msg += " (альбом — каждый файл отдельным постом)"
            await _safe_answer(message, msg, reply_markup=pick_kb, reply_to_message_id=message.message_id)
        else:
            # Для пересылок из каналов (telegram_channel) — правило не найдено,
            # просто информируем (правила создаются через UI маршрутизации)
            msg = f"📥 Пост #{post_id} → Входящие"
            if is_album:
                msg += " (альбом)"
            else:
                msg += "\nПравило маршрутизации не найдено. /menu → 🔀 Маршрутизация"
            await _safe_answer(message, msg, reply_to_message_id=message.message_id)
    else:
        from app.repositories import queues as queues_repo

        queue = await queues_repo.get_queue(queue_id)
        qname = queue["name"] if queue else f"#{queue_id}"
        # Если пост помечен possible_duplicate (pHash-дедуп), он не встанет в
        # отправку автоматически — нужен ручной разбор. Информируем об этом.
        post_after = await posts_repo.get_post(post_id)
        if post_after and post_after["status"] == "possible_duplicate":
            await _safe_answer(message,
                f"⚠️ Пост #{post_id} → очередь «{qname}», НО помечен как возможный дубликат. "
                "Требует проверки (/menu → 📥 Входящие).",
                reply_to_message_id=message.message_id,
            )
        else:
            pending_count = await queues_repo.count_pending(queue_id)
            # Сбрасываем флаг «finished» — в очередь добавлен новый пост (CRITICAL C4)
            from app.services import notifications
            notifications.reset_finished(queue_id)
            await _safe_answer(message,
                f"✓ Пост #{post_id} → очередь «{qname}» (позиция ~{pending_count})",
                reply_to_message_id=message.message_id,
            )
    logger.info("Принят пост #%s: type=%s source=%s queue=%s album=%s", post_id, media["file_type"], source["source_type"], queue_id, is_album)


@router.message(F.text & ~F.text.startswith("/"))
async def receive_text(message: Message, user: dict | None = None) -> None:
    """Текст без команды — сохраняем как возможный caption-черновик во Входящие (ТЗ §2.1.1 п.1 'Текст')."""
    # Текст сам по себе не создаёт пост (нет медиа), но логируем для будущего
    logger.info("Текст без медиа от user=%s — игнорируется", getattr(message.from_user, "id", None))
