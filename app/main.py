"""Точка входа приложения (ТЗ §4.1, Фаза 2).

FastAPI lifespan:
 1. init_db() — схема БД (ТЗ §5)
 2. bootstrap_admin() — первичный admin из .env (ТЗ §2.6)
 3. init_tags_db() — отдельная БД тегов Gelbooru (ТЗ §10.1)
 4. cache.start_cleanup_task() — фоновая очистка кеша Gelbooru (ТЗ §10.1)
 5. setup_scheduler_async() + scheduler.start() — публикация постов (ТЗ §2.5)
 6. bot polling — приём медиа из ЛС (ТЗ §2.1.1)

Endpoints:
 - /health             — статус приложения + проблемы конфигурации
 - /health/bot-api     — getMe через Local Bot API Server (ТЗ §3.3)
 - /health/scheduler   — список активных job'ов очередей (Фаза 2)
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.bot import get_bot, get_dispatcher
from app.config.settings import settings
from app.db import bootstrap_admin, close_db, init_db
from app.scheduler import get_scheduler, setup_scheduler_async
from app.services.parsers.gelbooru import cache, tags_db
from app.utils.local_bot_api import get_me

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

_bot_polling_task: asyncio.Task | None = None


async def _bot_polling() -> None:
    """Запускает aiogram long-polling в фоне (только если есть BOT_TOKEN)."""
    if not settings.bot_token:
        log.warning("BOT_TOKEN не задан — polling бота пропущен (можно тестировать API/БД без бота)")
        return
    bot = get_bot()
    # Регистрируем bot в runtime, чтобы scheduler/handlers могли его получить
    from app.bot.runtime import set_bot
    set_bot(bot)
    dp = get_dispatcher()
    me = await get_me()
    if me:
        log.info("Бот @%s запущен (polling через Local Bot API)", me.get("username"))
    else:
        log.warning("getMe не прошёл — проверьте logOut (Фаза 1) и доступность local-bot-api:8081")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except asyncio.CancelledError:
        log.info("Bot polling остановлен")
    except Exception:  # не роняем приложение, логируем
        log.exception("Bot polling упал")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Жизненный цикл: инициализация БД, бота, планировщика."""
    global _bot_polling_task
    log.info("Запуск приложения (Фаза 2)…")

    # 1. БД (ТЗ §5)
    await init_db()

    # 2. Первичный admin (ТЗ §2.6)
    if settings.admin_tg_id:
        await bootstrap_admin(settings.admin_tg_id, None)

    # 3. БД тегов Gelbooru (ТЗ §10.1)
    await tags_db.init_tags_db()

    # 4. Кеш Gelbooru + фоновая очистка (ТЗ §10.1)
    cache.init_cache()
    await cache.start_cleanup_task()

    # 5. Планировщик (ТЗ §2.5)
    await setup_scheduler_async()
    sched = get_scheduler()
    sched.start()
    log.info("APScheduler запущен")

    # 6. Bot polling (ТЗ §8)
    _bot_polling_task = asyncio.create_task(_bot_polling())

    try:
        yield
    finally:
        # Shutdown
        log.info("Остановка приложения…")
        if _bot_polling_task:
            _bot_polling_task.cancel()
            try:
                await _bot_polling_task
            except asyncio.CancelledError:
                pass
        sched.shutdown(wait=False)
        await tags_db.close_tags_db()
        await close_db()
        log.info("Приложение остановлено")


app = FastAPI(title="TG Media Queue Bot", version="0.2.0-phase2", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    """Живость приложения + состояние конфигурации."""
    return {
        "status": "ok",
        "phase": 2,
        "bot_api_url": settings.bot_api_url,
        "config_problems": settings.validate(),
    }


@app.get("/health/bot-api")
async def health_bot_api() -> dict:
    """getMe через Local Bot API Server (ТЗ §3.3)."""
    me = await get_me()
    if me:
        return {"status": "ok", "bot": {"username": me.get("username"), "id": me.get("id")}}
    return {"status": "error", "detail": "Local Bot API недоступен или токен отклонён; см. scripts/check_phase1.py"}


@app.get("/health/scheduler")
async def health_scheduler() -> dict:
    """Состояние планировщика: список активных job'ов очередей (Фаза 2)."""
    sched = get_scheduler()
    jobs = []
    for job in sched.get_jobs():
        jobs.append({
            "id": job.id,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
        })
    return {"status": "ok" if sched.running else "stopped", "jobs": jobs}
