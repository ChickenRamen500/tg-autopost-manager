"""Планировщик публикации постов (ТЗ §2.5).

APScheduler (AsyncIOScheduler): для каждой активной очереди — interval job.
Алгоритм отправки (ТЗ §2.5.1):
  1. Если есть tg_file_id и не was_processed → отправляем по file_id (без чтения файла с диска)
  2. Иначе → локальный файл через Local Bot API Server (лимит 2000 МБ)

Обработка ошибок (ТЗ §2.5.2):
  - 429 Too Many Requests: ждём retry_after, повторяем
  - троттлинг 1.1 сек между отправками в одну очередь
  - max 3 попытки → статус failed
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config.settings import settings
from app.repositories import (
    channels as channels_repo,
)
from app.repositories import (
    posts as posts_repo,
)
from app.repositories import (
    queues as queues_repo,
)
from app.services.queue_time import handle_end_strategy, is_in_time_window
from app.utils.telegram_files import extract_file_id_from_message

log = logging.getLogger("scheduler")

_scheduler: AsyncIOScheduler | None = None
# Троттлинг: время последней отправки по каждому queue_id (ТЗ §2.5.2 п.3)
_last_send: dict[int, float] = {}

# Telegram не принимает send_photo больше 10 МБ:
# «Bad Request: file of size ... is too big for a photo; the maximum size is 10485760 bytes».
# Такие фото пережимаются в JPEG (ffmpeg) или уходят как document (fallback).
_TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024


def _is_photo_too_big_error(e: Exception) -> bool:
    """True если Telegram отклонил фото из-за размера (> 10 МБ)."""
    s = str(e)
    return "too big for a photo" in s or "PHOTO_INVALID_DIMENSIONS" in s

# --- Диагностика «молчаливых» пропусков тиков (баг «очередь не отправляет») ---
# Раньше все ранние return в send_post_from_queue были немыми: при зависшей
# очереди в логе не было НИЧЕГО (только apscheduler «executed successfully») —
# причину невозможно установить post factum. Теперь каждый пропуск с ожидающими
# постами логируется INFO при СМЕНЕ состояния и повторно раз в
# _SKIP_REMIND_SECONDS, пока состояние не изменится. Пустая очередь (нет постов)
# — норма, без логов.
_skip_state: dict[int, tuple[str, str]] = {}
_last_skip_log: dict[int, float] = {}
_SKIP_REMIND_SECONDS = 1800.0  # напоминание о зависшей очереди раз в 30 минут


def _log_skip(queue_id: int, reason: str, message: str) -> None:
    """Логирует причину пропуска тика очереди (INFO, без спама: по смене состояния)."""
    state = (reason, message)
    now = asyncio.get_event_loop().time()
    if _skip_state.get(queue_id) == state and (
        now - _last_skip_log.get(queue_id, 0.0)
    ) < _SKIP_REMIND_SECONDS:
        return
    _skip_state[queue_id] = state
    _last_skip_log[queue_id] = now
    log.info("%s", message)


def _clear_skip(queue_id: int) -> None:
    """Сбрасывает состояние пропуска (очередь снова отправляет или опустела):
    следующий пропуск залогируется сразу, без ожидания напоминания."""
    _skip_state.pop(queue_id, None)


class FileMissingError(Exception):
    """Файл поста не найден на диске, но можно перекачать через worker.

    Поднимается в _send_one_post / _send_album_group когда file_path указывает
    на несуществующий файл. Если у поста есть source_url — worker может
    перекачать. Scheduler ловит это исключение и enqueues download task.
    """

    def __init__(self, post_id: int, file_path: str, source_url: str, message: str) -> None:
        super().__init__(message)
        self.post_id = post_id
        self.file_path = file_path
        self.source_url = source_url


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(
            timezone="UTC",
            job_defaults={
                "misfire_grace_time": 300,  # P1-5: 5 минут вместо дефолт 1 сек
                "coalesce": True,
                "max_instances": 1,
            },
        )
    return _scheduler


def reset_scheduler() -> None:
    """Сбрасывает singleton scheduler (для тестов).

    PR-40 (H-T6): без этого scheduler «течёт» между тестами — первый тест,
    вызвавший sched.start(), оставляет scheduler в состоянии running, и следующий
    тест, создающий новый scheduler через get_scheduler(), получает тот же
    инстанс с запущенными job'ами предыдущего теста.

    Использовать в начале теста, затрагивающего scheduler:
        from app.scheduler import reset_scheduler
        reset_scheduler()  # чистый singleton
        sched = get_scheduler()
        sched.start()
        # ...
        sched.shutdown(wait=False)
    """
    global _scheduler
    if _scheduler is not None:
        try:
            if _scheduler.running:
                _scheduler.shutdown(wait=False)
        except Exception as e:  # noqa: BLE001 — тесты не должны падать на teardown
            log.warning("reset_scheduler: shutdown error: %s", e)
        _scheduler = None
        # Также чистим троттлинг-словарь — иначе между тестами сохраняются last_send.
        _last_send.clear()


async def _capture_sent_file_id(post_id: int, message: Any) -> None:
    """Сохраняет file_id из результата отправки в posts.tg_file_id (если был пуст).

    У постов из расширения/парсеров/локальных файлов исходного tg_file_id нет —
    каждая отправка перечитывала файл с диска и падала FileMissingError,
    если файл удалён (cleanup, пересборка контейнера). После первой успешной
    отправки Telegram возвращает Message с file_id: сохраняем его, и все
    повторные отправки идут по file_id (стратегия 1) без файла на диске.

    Ошибки захвата логируются и не влияют на результат отправки.
    """
    fid = extract_file_id_from_message(message)
    if not fid:
        return
    try:
        saved = await posts_repo.set_tg_file_id(post_id, fid)
        if saved:
            log.info(
                "Пост #%s: захвачен tg_file_id после отправки — "
                "повторные отправки пойдут по file_id без файла на диске",
                post_id,
            )
    except Exception:  # noqa: BLE001 — захват file_id не критичен для отправки
        log.warning("Пост #%s: не удалось сохранить захваченный file_id", post_id, exc_info=True)


async def _auto_delete_sent_files(post_ids: list[int]) -> None:
    """Удаляет файлы постов с диска после успешной отправки в TG (автоочистка).

    Логика (AUTO_DELETE_SENT_FILES, по умолчанию включено): файл, скачанный из
    внешнего источника (расширение/парсеры), больше не нужен на диске после
    первой успешной отправки:
      - повторные отправки идут по захваченному tg_file_id (стратегия 1);
      - если файл удалён и file_id не захвачен — FileMissingError → перекачка
        через worker по source_url (HTTP).

    НЕ удаляются:
      - локальные файлы пользователя (source_type='local_file') — это его
        собственные файлы, управляются вручную через файловый браузер;
      - посты без HTTP source_url (перекачать нельзя — файл единственная копия);
      - файлы, нужные другим неотправленным постам (new/downloading/pending
        с тем же file_path).

    file_path в БД НЕ обнуляется: debounce-проверка архивации альбомов и
    пометка «отправлено» в файловом браузере опираются на него, а попытка
    отправки по несуществующему пути корректно обрабатывается через
    FileMissingError. Ошибки удаления не влияют на результат отправки.
    """
    if not settings.auto_delete_sent_files:
        return
    for pid in post_ids:
        try:
            post = await posts_repo.get_post(pid)
            if not post:
                continue
            if (post.get("source_type") or "") == "local_file":
                continue
            file_path = post.get("file_path")
            if not file_path:
                continue
            if not str(post.get("source_url") or "").startswith("http"):
                continue
            others = await posts_repo.count_unsent_by_file_path(file_path, exclude_post_id=pid)
            if others > 0:
                log.info(
                    "Пост #%s: файл %s нужен ещё %d неотправленным постам — не удаляем",
                    pid, file_path, others,
                )
                continue
            os.remove(file_path)
            log.info(
                "Пост #%s: файл %s удалён после успешной отправки (автоочистка)",
                pid, file_path,
            )
        except FileNotFoundError:
            pass  # файл уже удалён (например, предыдущим постом того же альбома)
        except Exception as e:  # noqa: BLE001 — очистка не критична для отправки
            log.warning("Пост #%s: не удалось удалить файл после отправки: %s", pid, e)


async def _send_one_post(post: dict[str, Any], channel: dict[str, Any], queue: dict | None = None) -> None:
    """Отправляет один пост в канал по стратегии file_id → файл (ТЗ §2.5.1).

    queue — словарь очереди (для strip_caption / signature_template). Если None — настройки не применяются.
    Поднимает исключение при ошибке API (вызывающий код считает попытки).
    """
    from app.bot.runtime import get_bot  # ленивый импорт — разрывает цикл bot↔scheduler

    bot = get_bot()
    chat_id = channel["tg_chat_id"]
    caption = post.get("processed_caption") or post.get("original_caption")

    # Настройки очереди: strip_caption (убирать подпись), signature_template (добавить свою)
    if queue:
        if queue.get("strip_caption"):
            caption = None
        sig = queue.get("signature_template")
        if sig:
            caption = f"{sig}\n{caption}" if caption else sig
    # P2-13: Telegram limit caption 1024 chars
    if caption and len(caption) > 1024:
        caption = caption[:1021] + "..."

    file_type = post["file_type"]
    tg_file_id = post.get("tg_file_id")
    was_processed = bool(post.get("was_processed", 0))
    file_path = post.get("file_path")

    if tg_file_id and not was_processed:
        # Стратегия 1: отправка по file_id — без чтения файла, без лимита размера
        try:
            if file_type == "photo":
                await bot.send_photo(chat_id=chat_id, photo=tg_file_id, caption=caption)
            elif file_type == "video":
                await bot.send_video(chat_id=chat_id, video=tg_file_id, caption=caption)
            elif file_type == "gif":
                await bot.send_animation(chat_id=chat_id, animation=tg_file_id, caption=caption)
            elif file_type == "audio":
                await bot.send_audio(chat_id=chat_id, audio=tg_file_id, caption=caption)
            elif file_type == "voice":
                await bot.send_voice(chat_id=chat_id, voice=tg_file_id, caption=caption)
            else:
                await bot.send_document(chat_id=chat_id, document=tg_file_id, caption=caption)
            log.info("Пост #%s отправлен по file_id (type=%s)", post["id"], file_type)
            return
        except Exception as e:
            # PHOTO_INVALID_DIMENSIONS / too big for a photo: фото слишком
            # маленькое (<320px), неправильное соотношение сторон или > 10 МБ.
            # Fallback: отправить как document.
            if file_type == "photo" and _is_photo_too_big_error(e):
                log.warning(
                    "Пост #%s: %s — fallback на send_document (file_id)",
                    post["id"], type(e).__name__,
                )
                await bot.send_document(chat_id=chat_id, document=tg_file_id, caption=caption)
                log.info("Пост #%s отправлен как document (fallback)", post["id"])
                return
            raise  # пробрасываем для retry-логики

    # Стратегия 2: локальный файл через Local Bot API Server (до 2 ГБ)
    if not file_path:
        raise RuntimeError(
            f"Пост #{post['id']}: нет ни tg_file_id, ни file_path — отправить невозможно"
        )

    # Проверка существования файла. Файл мог быть удалён (cleanup, пересборка
    # контейнера, изменение MEDIA_DIR, ручная очистка data/downloads).
    if not os.path.exists(file_path):
        source_url = post.get("source_url") or ""
        source_type = post.get("source_type") or ""

        # Для локальных файлов (source_type='local_file') source_url = путь на диске.
        # Перекачка бессмысленна (это не HTTP URL) → сразу mark_failed.
        if source_type == "local_file" or source_url.startswith("/") or source_url.startswith("\\"):
            raise RuntimeError(
                f"Пост #{post['id']}: файл не найден на диске: {file_path}. "
                f"Локальный файл удалён или перемещён. Удалите пост или "
                f"переотправьте файл через браузер локальных файлов."
            )

        # Для внешних источников (x.com, gelbooru и т.п.) — source_url это HTTP URL,
        # можно перекачать через worker.
        if source_url.startswith("http"):
            raise FileMissingError(
                post_id=post["id"],
                file_path=file_path,
                source_url=source_url,
                message=(
                    f"Пост #{post['id']}: файл не найден на диске: {file_path}. "
                    f"Перекачиваем через worker (source_url={source_url})."
                ),
            )

        # Нет source_url или не HTTP — перекачать нельзя
        raise RuntimeError(
            f"Пост #{post['id']}: файл не найден на диске: {file_path}. "
            f"source_url отсутствует или не HTTP — перекачать нельзя. "
            f"Удалите пост или переотправьте через расширение."
        )

    # P2-11: FSInputFile (streaming, не грузит файл в RAM) вместо BufferedInputFile (OOM на 2GB)
    from aiogram.types import FSInputFile

    # Telegram не принимает photo > 10 МБ. Если файл больше лимита — сначала
    # пробуем пережать в JPEG (ffmpeg); не получилось → уйдёт как document
    # (fallback в except ниже и подмена file_type здесь).
    send_file_type = file_type
    send_path = file_path
    if file_type == "photo" and os.path.getsize(file_path) > _TELEGRAM_PHOTO_MAX_BYTES:
        try:
            from app.services.media.transcode import compress_photo_for_telegram

            compressed = await compress_photo_for_telegram(file_path)
        except Exception as e:  # noqa: BLE001 — пережатие не должно ронять отправку
            log.warning("Пост #%s: пережатие фото не удалось: %s", post["id"], e)
            compressed = None
        if compressed:
            send_path = compressed
            log.info(
                "Пост #%s: фото > 10 МБ пережато в %s (%d байт) — отправляю как photo",
                post["id"], send_path, os.path.getsize(send_path),
            )
        else:
            log.info(
                "Пост #%s: фото %d байт > лимита Telegram 10 МБ, пережать не удалось — отправлю как document",
                post["id"], os.path.getsize(file_path),
            )
            send_file_type = "document"

    doc = FSInputFile(send_path, filename=os.path.basename(send_path))
    try:
        if send_file_type == "photo":
            result = await bot.send_photo(chat_id=chat_id, photo=doc, caption=caption)
        elif send_file_type == "video":
            result = await bot.send_video(chat_id=chat_id, video=doc, caption=caption)
        elif send_file_type == "gif":
            result = await bot.send_animation(chat_id=chat_id, animation=doc, caption=caption)
        elif send_file_type == "audio":
            result = await bot.send_audio(chat_id=chat_id, audio=doc, caption=caption)
        elif send_file_type == "voice":
            result = await bot.send_voice(chat_id=chat_id, voice=doc, caption=caption)
        else:
            result = await bot.send_document(chat_id=chat_id, document=doc, caption=caption)
    except Exception as e:
        # PHOTO_INVALID_DIMENSIONS / too big for a photo: fallback на document
        if file_type == "photo" and _is_photo_too_big_error(e):
            log.warning(
                "Пост #%s: %s — fallback на send_document",
                post["id"], type(e).__name__,
            )
            doc2 = FSInputFile(file_path, filename=os.path.basename(file_path))
            result = await bot.send_document(chat_id=chat_id, document=doc2, caption=caption)
            await _capture_sent_file_id(post["id"], result)
            log.info("Пост #%s отправлен как document (fallback)", post["id"])
            return
        raise
    # Захват file_id из результата отправки: повторные отправки пойдут по file_id
    await _capture_sent_file_id(post["id"], result)
    log.info("Пост #%s отправлен локальным файлом %s", post["id"], file_path)


async def _send_album_group(posts: list[dict[str, Any]], channel: dict[str, Any], queue: dict | None = None) -> None:
    """Отправляет альбом (несколько постов) одним сообщением через send_media_group (ТЗ §2.1.1).

    Все посты должны иметь одинаковый media_group_id и быть в одной очереди.
    Caption берётся из первого поста (Telegram показывает подпись только для первого).

    Поддерживает два источника медиа:
      1. tg_file_id — пост из Telegram (уже загружен, отправляем по file_id).
      2. file_path — пост из расширения/парсера (локальный файл на диске,
         отправляем через FSInputFile/attach://). Это работает с Local Bot API.
    """
    from aiogram.types import (
        FSInputFile,
        InputMediaAudio,
        InputMediaDocument,
        InputMediaPhoto,
        InputMediaVideo,
    )

    from app.bot.runtime import get_bot

    bot = get_bot()
    chat_id = channel["tg_chat_id"]

    media: list[InputMediaPhoto | InputMediaVideo | InputMediaDocument | InputMediaAudio] = []
    for i, p in enumerate(posts):
        # Caption только для первого элемента альбома (ограничение Telegram)
        raw_caption = (p.get("processed_caption") or p.get("original_caption")) if i == 0 else None

        # Настройки очереди: strip_caption (убирать подпись), signature_template (добавить свою)
        # Та же логика что в _send_one_post — иначе альбомы получали подпись без учёта настроек.
        if i == 0 and queue:
            if queue.get("strip_caption"):
                raw_caption = None
            sig = queue.get("signature_template")
            if sig:
                raw_caption = f"{sig}\n{raw_caption}" if raw_caption else sig
        # P2-13: Telegram limit caption 1024 chars (для album — 1024, не 4096)
        if raw_caption and len(raw_caption) > 1024:
            raw_caption = raw_caption[:1021] + "..."
        caption = raw_caption

        ft = p["file_type"]
        fid = p.get("tg_file_id")
        file_path = p.get("file_path")

        # Определяем источник медиа: tg_file_id (приоритет) или file_path (локальный файл)
        if fid:
            media_obj = fid
        elif file_path:
            # Проверка существования файла (см. комментарий в _send_one_post)
            if not os.path.exists(file_path):
                raise RuntimeError(
                    f"Пост #{p['id']}: файл не найден на диске: {file_path}. "
                    f"Файл удалён или контейнер пересоздан. "
                    f"Удалите пост или переотправьте через расширение."
                )
            send_path = file_path
            # Фото > 10 МБ Telegram не принимает даже в альбомах: пережимаем
            # в JPEG; не получилось — отправляем элемент как document.
            if ft == "photo" and os.path.getsize(file_path) > _TELEGRAM_PHOTO_MAX_BYTES:
                try:
                    from app.services.media.transcode import compress_photo_for_telegram

                    compressed = await compress_photo_for_telegram(file_path)
                except Exception as e:  # noqa: BLE001
                    log.warning("Пост #%s альбома: пережатие фото не удалось: %s", p["id"], e)
                    compressed = None
                if compressed:
                    send_path = compressed
                    log.info("Пост #%s альбома: фото > 10 МБ пережато в %s", p["id"], send_path)
                else:
                    log.info("Пост #%s альбома: фото > 10 МБ — элемент уйдёт как document", p["id"])
                    ft = "document"
            media_obj = FSInputFile(send_path)
            log.info("Пост #%s альбома: отправка локального файла %s", p["id"], send_path)
        else:
            log.warning("Пост #%s альбома без tg_file_id и file_path — пропускаем", p["id"])
            raise RuntimeError(f"Пост #{p['id']}: альбом требует tg_file_id или file_path для всех файлов")

        if ft == "photo":
            media.append(InputMediaPhoto(media=media_obj, caption=caption))
        elif ft == "video":
            media.append(InputMediaVideo(media=media_obj, caption=caption))
        elif ft == "audio":
            media.append(InputMediaAudio(media=media_obj, caption=caption))
        else:
            media.append(InputMediaDocument(media=media_obj, caption=caption))

    if len(media) == 1:
        await _send_one_post(posts[0], channel, queue)
        return

    try:
        result = await bot.send_media_group(chat_id=chat_id, media=media)
    except Exception as e:
        # Страховка: даже после пережатия Telegram может отклонить альбом
        # («too big for a photo»). Отправляем каждый пост отдельно —
        # _send_one_post сам решает: photo / пережатое photo / document.
        if "too big for a photo" in str(e):
            log.warning(
                "Альбом (media_group_id=%s) отклонён целиком (%s) — отправляю посты по одному",
                posts[0].get("media_group_id"), type(e).__name__,
            )
            for p in posts:
                await _send_one_post(p, channel, queue)
            return
        raise
    # Захват file_id для каждого поста альбома (message[i] ↔ media[i] ↔ posts[i])
    if isinstance(result, (list, tuple)):
        for i, msg in enumerate(result):
            if i < len(posts):
                await _capture_sent_file_id(posts[i]["id"], msg)
    else:
        await _capture_sent_file_id(posts[0]["id"], result)
    log.info("Альбом (%d постов, media_group_id=%s) отправлен в канал %s", len(posts), posts[0].get("media_group_id"), chat_id)


async def _redownload_post(post_id: int, queue_id: int, queue_name: str) -> None:
    """Перекачивает файл поста через worker (когда файл удалён с диска).

    Вызывается когда scheduler при попытке отправки обнаруживает, что file_path
    указывает на несуществующий файл, но у поста есть source_url.

    Действия:
    1. Проверяем, нет ли уже queued download-задачи для этого поста
       (защита от дублирования — если уже в очереди, ждём).
    2. Обнуляем file_path (worker не должен считать файл готовым).
    3. Меняем статус на 'new' (worker ищет new → downloading → pending).
    4. Enqueue download-задачу с payload из source_url + referer из post.
    5. Пост останется в очереди, но не будет отправлен пока worker не скачает.
       Worker переведёт в pending → scheduler отправит на следующем тике.
    """
    from app.repositories import task_queue as tq_repo

    # 1. Проверяем, нет ли уже queued download-задачи
    if await tq_repo.has_queued_task(post_id, "download"):
        log.info(
            "Пост #%s: файл не найден, но download-задача уже в очереди — ждём worker",
            post_id,
        )
        return

    # Достаём пост (нужен source_url для payload)
    post = await posts_repo.get_post(post_id)
    if not post:
        log.warning("Пост #%s не найден (удалён?) — перекачка отменена", post_id)
        return

    source_url = post.get("source_url")
    if not source_url:
        log.error(
            "Пост #%s: файл не найден, но source_url отсутствует — перекачать нельзя",
            post_id,
        )
        await posts_repo.mark_failed(
            post_id,
            "Файл не найден на диске и source_url отсутствует — перекачать нельзя",
        )
        return

    # 2. Обнуляем file_path (worker не должен считать файл готовым)
    db = posts_repo.get_db()
    await db.execute("UPDATE posts SET file_path = NULL WHERE id = ?", (post_id,))
    await db.commit()

    # 3. Меняем статус на 'new' (worker ищет new для скачивания)
    await posts_repo.update_status(post_id, "new")

    # 4. Enqueue download-задачу с payload (referer, site — для yt-dlp path)
    payload: dict[str, Any] = {
        "media_url": source_url,
        "url": source_url,
        "referer": post.get("source_channel") or None,
        "site": post.get("source_type") or None,
    }
    await tq_repo.enqueue(post_id, "download", payload)

    log.warning(
        "Пост #%s: файл не найден на диске → перекачиваем через worker "
        "(queue '%s' #%s, source_url=%s). Пост вернётся в pending после скачивания.",
        post_id, queue_name, queue_id, source_url,
    )


async def send_post_from_queue(queue_id: int) -> None:
    """Отправляет следующий pending-пост из очереди (ТЗ §2.5.1).

    Если у очереди включена групповая отправка альбомов (group_albums=True) и у поста
    есть media_group_id, собирает все посты альбома и отправляет через send_media_group.

    Проверяет временное окно очереди (блок B): start_at/end_at/daily_window.
    Если вне окна — пропускает тик. Если end_at прошёл — выполняет end_strategy.

    Троттлинг: минимум settings.send_throttle_seconds между отправками в одну очередь.
    """
    queue = await queues_repo.get_queue(queue_id)
    if not queue or not queue["is_active"]:
        pending = await queues_repo.count_pending(queue_id)
        if pending:
            _log_skip(
                queue_id, "queue_inactive",
                f"Очередь #{queue_id}: {pending} пост(ов) ждут, но очередь неактивна "
                f"(is_active=0) — отправка на паузе. Включите очередь в разделе «Очереди».",
            )
        return
    channel = await channels_repo.get_channel(queue["channel_id"])
    if not channel or not channel["is_active"]:
        pending = await queues_repo.count_pending(queue_id)
        if pending:
            _log_skip(
                queue_id, "channel_inactive",
                f"Очередь #{queue_id} «{queue['name']}»: {pending} пост(ов) ждут, но канал "
                f"#{queue['channel_id']} не найден или неактивен (is_active=0) — отправка "
                f"заблокирована. Проверьте канал в разделе «Каналы».",
            )
        return

    # Проверка временного окна очереди (блок B)
    if not is_in_time_window(queue):
        # Вне окна. Проверяем, не прошёл ли end_at со стратегией move_to_inbox/delete
        from app.services.queue_time import _now_utc, _parse_iso_utc, format_queue_time_info

        end_at = _parse_iso_utc(queue.get("end_at"))
        if end_at is not None and _now_utc() > end_at:
            # end_at прошёл — обрабатываем стратегию завершения
            await handle_end_strategy(queue)
        pending = await queues_repo.count_pending(queue_id)
        if pending:
            _log_skip(
                queue_id, "outside_window",
                f"Очередь #{queue_id} «{queue['name']}»: {pending} пост(ов) ждут отправки, "
                f"но очередь вне временного окна — {format_queue_time_info(queue)}. "
                f"Проверьте настройки очереди (start_at/end_at/ежедневное окно).",
            )
        else:
            _clear_skip(queue_id)
        return  # вне окна — пропускаем тик

    post = await posts_repo.get_next_pending(queue_id)
    if post is None:
        _clear_skip(queue_id)  # очередь пуста — норма, без логов
        return  # очередь пуста
    _clear_skip(queue_id)  # пост найден — все проверки пройдены: сбрасываем состояние пропуска

    # Троттлинг (ТЗ §2.5.2 п.3) — защита от 429 при коротком интервале очереди
    now = asyncio.get_event_loop().time()
    last = _last_send.get(queue_id, 0.0)
    wait = settings.send_throttle_seconds - (now - last)
    if wait > 0:
        await asyncio.sleep(wait)

    # Групповая отправка альбомов (ТЗ §2.1.1, §2.5.1):
    # если у очереди group_albums=True и у поста есть media_group_id,
    # собираем все pending-посты с тем же media_group_id и отправляем одним сообщением.
    group_albums = bool(queue.get("group_albums", 0))
    mgid = post.get("media_group_id")
    album_posts: list[dict[str, Any]] = []
    if group_albums and mgid:
        album_posts = await posts_repo.find_album_posts(queue_id, mgid, limit=10)
        if len(album_posts) > 1:
            # Отправляем весь альбом одним вызовом
            sent_ids: list[int] = []
            max_attempts = settings.max_send_attempts
            last_error: str | None = None
            for _attempt in range(1, max_attempts + 1):
                try:
                    await _send_album_group(album_posts, channel, queue)
                    _last_send[queue_id] = asyncio.get_event_loop().time()
                    # Атомарно помечаем ВСЕ посты альбома как sent (P1-9: одна транзакция)
                    post_ids = [p["id"] for p in album_posts]
                    await posts_repo.bulk_mark_sent(post_ids)
                    sent_ids = post_ids
                    log.info("Альбом отправлен: посты %s", sent_ids)
                    # Автоочистка: скачанные файлы больше не нужны на диске
                    await _auto_delete_sent_files(post_ids)
                    return
                except FileMissingError as e:
                    # Один из постов альбома потерял файл → перекачиваем.
                    # Пост вернётся в pending после скачивания, альбом отправится
                    # на следующем тике scheduler'а (когда все посты снова pending).
                    await _redownload_post(e.post_id, queue_id, queue["name"])
                    return
                except Exception as e:  # noqa: BLE001 — любая ошибка API/сети
                    last_error = str(e)
                    # Инкрементим попытки для всех постов альбома
                    for p in album_posts:
                        await posts_repo.increment_attempt(p["id"])
                    retry_after = getattr(e, "retry_after", None)
                    if retry_after:
                        log.warning("429 rate limit на альбом %s: ждём %s сек", mgid, retry_after)
                        await asyncio.sleep(float(retry_after))
                        continue
                    log.warning("Ошибка отправки альбома %s (попытка %d/%d): %s", mgid, _attempt, max_attempts, e)
                    if _attempt >= max_attempts:
                        for p in album_posts:
                            await posts_repo.mark_failed(p["id"], last_error)
                        log.error("Альбом %s переведён в failed после %d попыток", mgid, max_attempts)
                        # Уведомление об ошибке (блок C) — для альбомов тоже
                        from app.services.notifications import notify_send_error
                        await notify_send_error(queue_id, queue["name"], album_posts[0]["id"], last_error or "")
                        return
                    await asyncio.sleep(2.0)
            _last_send[queue_id] = asyncio.get_event_loop().time()
            return
        # Альбом из 1 поста или не найден — отправляем как обычный пост

    max_attempts = settings.max_send_attempts
    last_error: str | None = None
    for _attempt in range(1, max_attempts + 1):
        try:
            await _send_one_post(post, channel, queue)
            _last_send[queue_id] = asyncio.get_event_loop().time()
            await posts_repo.mark_sent(post["id"])
            # Автоочистка: скачанный файл больше не нужен на диске
            await _auto_delete_sent_files([post["id"]])
            # Уведомления (блок C): проверяем остаток очереди
            await _check_queue_notifications(queue, channel)
            return
        except FileMissingError as e:
            # Файл удалён, но есть source_url → перекачиваем через worker.
            # Не ретраим отправку (бесполезно до скачивания), не mark_failed.
            await _redownload_post(e.post_id, queue_id, queue["name"])
            return
        except Exception as e:  # noqa: BLE001 — любая ошибка API/сети
            last_error = str(e)
            # P2-6: permanent-error classification (ТЗ §2.5.2 п.5)
            # Forbidden (bot kicked/blocked) и BadRequest (chat not found, entity_too_large)
            # — НЕ retry, сразу mark_failed
            error_str = str(e).lower()
            is_permanent = any(keyword in error_str for keyword in [
                "forbidden", "chat not found", "chat_id_invalid",
                "entity_too_large", "bot was blocked", "not enough rights",
                # Файл удалён, перекачать нельзя
                "source_url отсутствует",
                "локальный файл удалён",
            ])
            if is_permanent:
                await posts_repo.mark_failed(post["id"], last_error)
                log.error("Пост #%s: permanent error → failed без retry: %s", post["id"], last_error)
                from app.services.notifications import notify_send_error
                await notify_send_error(queue_id, queue["name"], post["id"], last_error)
                return
            # 429 Too Many Requests — retry_after
            retry_after = getattr(e, "retry_after", None)
            if retry_after:
                log.warning("429 rate limit на пост #%s: ждём %s сек", post["id"], retry_after)
                await asyncio.sleep(float(retry_after))
                continue
            attempts_done = await posts_repo.increment_attempt(post["id"])
            log.warning("Ошибка отправки поста #%s (попытка %d/%d): %s", post["id"], attempts_done, max_attempts, e)
            if attempts_done >= max_attempts:
                await posts_repo.mark_failed(post["id"], last_error)
                log.error("Пост #%s переведён в failed после %d попыток: %s", post["id"], max_attempts, last_error)
                # Уведомление об ошибке (блок C)
                from app.services.notifications import notify_send_error

                await notify_send_error(queue_id, queue["name"], post["id"], last_error)
                return
            await asyncio.sleep(2.0)

    _last_send[queue_id] = asyncio.get_event_loop().time()


async def _check_queue_notifications(queue: dict, channel: dict) -> None:
    """Проверяет остаток очереди и шлёт уведомления (блок C).

    - «Скоро закончится»: pending ≤ threshold (по умолчанию 5)
    - «Закончилась»: pending == 0 (последний пост отправлен)
    """
    try:
        from app.services.notifications import (
            notify_queue_almost_empty,
            notify_queue_finished,
        )

        pending = await queues_repo.count_pending(queue["id"])
        channel_name = channel.get("name") or f"#{channel['id']}"
        if pending == 0:
            await notify_queue_finished(queue["id"], queue["name"], channel_name)
        else:
            await notify_queue_almost_empty(queue["id"], queue["name"], channel_name, pending)
    except Exception as e:  # noqa: BLE001 — уведомления не критичны
        log.warning("Не удалось отправить уведомление по очереди: %s", e)


def setup_scheduler(scheduler: AsyncIOScheduler | None = None) -> AsyncIOScheduler:
    """Регистрирует interval-job для каждой активной очереди (ТЗ §2.5.1).

    Вызывается при старте приложения (main.py lifespan). Не запускает scheduler —
    запуск делает вызывающий код (.start()).
    """
    sched = scheduler or get_scheduler()
    # Снимаем старые job'ы очередей (на случай перерегистрации)
    for job in list(sched.get_jobs()):
        if job.id.startswith("queue_"):
            sched.remove_job(job.id)

    # Регистрируем асинхронно — репозитории требуют event loop, который есть только
    # после запуска приложения. Поэтому setup_scheduler_async вызывается из lifespan.
    return sched


async def setup_scheduler_async() -> AsyncIOScheduler:
    """Асинхронная регистрация job'ов активных очередей (после init_db).

    Безопасно вызывать многократно — снимает старые job'ы очередей и регистрирует заново.
    Используется и при старте приложения, и для hot-reload после /add_queue, /remove_queue.
    """
    sched = get_scheduler()
    setup_scheduler(sched)  # чистим старые job'ы очередей

    queues = await queues_repo.list_active_queues()
    for q in queues:
        trigger = IntervalTrigger(minutes=q["interval_minutes"])
        sched.add_job(
            send_post_from_queue,
            trigger=trigger,
            args=[q["id"]],
            id=f"queue_{q['id']}",
            replace_existing=True,
            # coalesce — если пропустили несколько интервалов, выполним один раз
            coalesce=True,
            max_instances=1,
        )
        log.info(
            "Зарегистрирована очередь #%s «%s» (каждые %d мин → канал #%s)",
            q["id"], q["name"], q["interval_minutes"], q["channel_id"],
        )
    if not queues:
        log.info("Активных очередей нет — scheduler пустой")
    return sched


async def reload_scheduler() -> int:
    """Hot-reload: перерегистрирует job'ы всех активных очередей без перезапуска контейнера.

    Возвращает количество зарегистрированных очередей. Вызывается:
    - командой /reload_scheduler (admin);
    - автоматически после /add_queue, /remove_queue.
    """
    sched = get_scheduler()
    if not sched.running:
        log.warning("reload_scheduler: scheduler не запущен — пропуск")
        return 0
    await setup_scheduler_async()
    queue_jobs = [j for j in sched.get_jobs() if j.id.startswith("queue_")]
    return len(queue_jobs)
