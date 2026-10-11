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
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.bot import get_bot
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

log = logging.getLogger("scheduler")

_scheduler: AsyncIOScheduler | None = None
# Троттлинг: время последней отправки по каждому queue_id (ТЗ §2.5.2 п.3)
_last_send: dict[int, float] = {}


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


async def _send_one_post(post: dict[str, Any], channel: dict[str, Any]) -> None:
    """Отправляет один пост в канал по стратегии file_id → файл (ТЗ §2.5.1).

    Поднимает исключение при ошибке API (вызывающий код считает попытки).
    """
    bot = get_bot()
    chat_id = channel["tg_chat_id"]
    caption = post.get("processed_caption") or post.get("original_caption")

    file_type = post["file_type"]
    tg_file_id = post.get("tg_file_id")
    was_processed = bool(post.get("was_processed", 0))
    file_path = post.get("file_path")

    if tg_file_id and not was_processed:
        # Стратегия 1: отправка по file_id — без чтения файла, без лимита размера
        if file_type == "photo":
            await bot.send_photo(chat_id=chat_id, photo=tg_file_id, caption=caption)
        elif file_type == "video":
            await bot.send_video(chat_id=chat_id, video=tg_file_id, caption=caption)
        elif file_type == "gif":
            await bot.send_animation(chat_id=chat_id, animation=tg_file_id, caption=caption)
        else:
            await bot.send_document(chat_id=chat_id, document=tg_file_id, caption=caption)
        log.info("Пост #%s отправлен по file_id (type=%s)", post["id"], file_type)
        return

    # Стратегия 2: локальный файл через Local Bot API Server (до 2 ГБ)
    if not file_path:
        raise RuntimeError(
            f"Пост #{post['id']}: нет ни tg_file_id, ни file_path — отправить невозможно"
        )
    # Чтение файла в потоке, чтобы не блокировать event loop (ТЗ §3.1: слабое железо)
    from aiogram.types import BufferedInputFile

    def _read_bytes() -> bytes:
        with open(file_path, "rb") as fh:  # выполняется в потоке через to_thread
            return fh.read()

    data = await asyncio.to_thread(_read_bytes)
    doc = BufferedInputFile(data, filename=file_path.rsplit("/", 1)[-1])
    if file_type == "photo":
        await bot.send_photo(chat_id=chat_id, photo=doc, caption=caption)
    elif file_type == "video":
        await bot.send_video(chat_id=chat_id, video=doc, caption=caption)
    elif file_type == "gif":
        await bot.send_animation(chat_id=chat_id, animation=doc, caption=caption)
    else:
        await bot.send_document(chat_id=chat_id, document=doc, caption=caption)
    log.info("Пост #%s отправлен локальным файлом %s", post["id"], file_path)


async def send_post_from_queue(queue_id: int) -> None:
    """Отправляет следующий pending-пост из очереди (ТЗ §2.5.1).

    Троттлинг: минимум settings.send_throttle_seconds между отправками в одну очередь.
    """
    queue = await queues_repo.get_queue(queue_id)
    if not queue or not queue["is_active"]:
        return
    channel = await channels_repo.get_channel(queue["channel_id"])
    if not channel or not channel["is_active"]:
        return

    post = await posts_repo.get_next_pending(queue_id)
    if post is None:
        return  # очередь пуста

    # Троттлинг (ТЗ §2.5.2 п.3) — защита от 429 при коротком интервале очереди
    now = asyncio.get_event_loop().time()
    last = _last_send.get(queue_id, 0.0)
    wait = settings.send_throttle_seconds - (now - last)
    if wait > 0:
        await asyncio.sleep(wait)

    max_attempts = settings.max_send_attempts
    last_error: str | None = None
    for _attempt in range(1, max_attempts + 1):
        try:
            await _send_one_post(post, channel)
            _last_send[queue_id] = asyncio.get_event_loop().time()
            await posts_repo.mark_sent(post["id"])
            return
        except Exception as e:  # noqa: BLE001 — любая ошибка API/сети
            last_error = str(e)
            attempts_done = await posts_repo.increment_attempt(post["id"])
            # 429 Too Many Requests — ждём retry_after и повторяем (ТЗ §2.5.2 п.1)
            retry_after = getattr(e, "retry_after", None)
            if retry_after:
                log.warning("429 rate limit на пост #%s: ждём %s сек", post["id"], retry_after)
                await asyncio.sleep(float(retry_after))
                continue
            # Сетевая ошибка — пост остаётся pending, повтор в этом же цикле
            log.warning("Ошибка отправки поста #%s (попытка %d/%d): %s", post["id"], attempts_done, max_attempts, e)
            if attempts_done >= max_attempts:
                await posts_repo.mark_failed(post["id"], last_error)
                log.error(
                    "Пост #%s переведён в failed после %d попыток: %s", post["id"], max_attempts, last_error
                )
                # TODO Фаза 6: уведомление админа
                return
            await asyncio.sleep(2.0)

    _last_send[queue_id] = asyncio.get_event_loop().time()


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
