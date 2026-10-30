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

log = logging.getLogger("scheduler")

_scheduler: AsyncIOScheduler | None = None
# Троттлинг: время последней отправки по каждому queue_id (ТЗ §2.5.2 п.3)
_last_send: dict[int, float] = {}


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
            # PHOTO_INVALID_DIMENSIONS: фото слишком маленькое (<320px) или
            # неправильное соотношение сторон. Fallback: отправить как document.
            if "PHOTO_INVALID_DIMENSIONS" in str(e) and file_type == "photo":
                log.warning(
                    "Пост #%s: PHOTO_INVALID_DIMENSIONS — fallback на send_document",
                    post["id"],
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
    # P2-11: FSInputFile (streaming, не грузит файл в RAM) вместо BufferedInputFile (OOM на 2GB)
    from aiogram.types import FSInputFile

    doc = FSInputFile(file_path, filename=os.path.basename(file_path))
    try:
        if file_type == "photo":
            await bot.send_photo(chat_id=chat_id, photo=doc, caption=caption)
        elif file_type == "video":
            await bot.send_video(chat_id=chat_id, video=doc, caption=caption)
        elif file_type == "gif":
            await bot.send_animation(chat_id=chat_id, animation=doc, caption=caption)
        elif file_type == "audio":
            await bot.send_audio(chat_id=chat_id, audio=doc, caption=caption)
        elif file_type == "voice":
            await bot.send_voice(chat_id=chat_id, voice=doc, caption=caption)
        else:
            await bot.send_document(chat_id=chat_id, document=doc, caption=caption)
    except Exception as e:
        # PHOTO_INVALID_DIMENSIONS: fallback на document
        if "PHOTO_INVALID_DIMENSIONS" in str(e) and file_type == "photo":
            log.warning(
                "Пост #%s: PHOTO_INVALID_DIMENSIONS — fallback на send_document",
                post["id"],
            )
            doc2 = FSInputFile(file_path, filename=os.path.basename(file_path))
            await bot.send_document(chat_id=chat_id, document=doc2, caption=caption)
            log.info("Пост #%s отправлен как document (fallback)", post["id"])
            return
        raise
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
        caption = (p.get("processed_caption") or p.get("original_caption")) if i == 0 else None
        ft = p["file_type"]
        fid = p.get("tg_file_id")
        file_path = p.get("file_path")

        # Определяем источник медиа: tg_file_id (приоритет) или file_path (локальный файл)
        if fid:
            media_obj = fid
        elif file_path:
            media_obj = FSInputFile(file_path)
            log.info("Пост #%s альбома: отправка локального файла %s", p["id"], file_path)
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

    await bot.send_media_group(chat_id=chat_id, media=media)
    log.info("Альбом (%d постов, media_group_id=%s) отправлен в канал %s", len(posts), posts[0].get("media_group_id"), chat_id)


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
        return
    channel = await channels_repo.get_channel(queue["channel_id"])
    if not channel or not channel["is_active"]:
        return

    # Проверка временного окна очереди (блок B)
    if not is_in_time_window(queue):
        # Вне окна. Проверяем, не прошёл ли end_at со стратегией move_to_inbox/delete
        from app.services.queue_time import _now_utc, _parse_iso_utc

        end_at = _parse_iso_utc(queue.get("end_at"))
        if end_at is not None and _now_utc() > end_at:
            # end_at прошёл — обрабатываем стратегию завершения
            await handle_end_strategy(queue)
        return  # вне окна — пропускаем тик

    post = await posts_repo.get_next_pending(queue_id)
    if post is None:
        return  # очередь пуста

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
            # Уведомления (блок C): проверяем остаток очереди
            await _check_queue_notifications(queue, channel)
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
