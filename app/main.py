"""Точка входа приложения (ТЗ §4.1, Фаза 2).

FastAPI lifespan:
 1. init_db() — схема БД (ТЗ §5)
 2. bootstrap_admin() — первичный admin из .env (ТЗ §2.6)
 3. init_tags_db() — отдельная БД тегов Gelbooru (ТЗ §10.1)
 4. cache.start_cleanup_task() — фоновая очистка кеша Gelbooru (ТЗ §10.1)
 5. setup_scheduler_async() + scheduler.start() — публикация постов (ТЗ §2.5)
 6. bot polling — приём медиа из ЛС (ТЗ §2.1.1)

Endpoints:
 - /health             — liveness без auth (для Docker HEALTHCHECK / load balancer);
                          возвращает только {"status":"ok"}, без раскрытия конфигурации.
 - /health/bot-api     — getMe через Local Bot API Server (ТЗ §3.3), auth: X-Secret-Token.
 - /health/scheduler   — список активных job'ов очередей (Фаза 2), auth: X-Secret-Token.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import traceback
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException

from app.bot import get_bot, get_dispatcher
from app.config.settings import settings
from app.db import bootstrap_admin, close_db, init_db
from app.repositories import task_queue as tq_repo
from app.scheduler import get_scheduler, setup_scheduler_async
from app.services.parsers.gelbooru import cache, tags_db
from app.utils.local_bot_api import get_me
from app.worker import start_worker, stop_worker

# НЕ используем logging.basicConfig — он перетирает handlers uvicorn/gunicorn.
# Только поднимаем уровень корневого логгера, оставляя handlers изо всех мест.
logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger("main")

_bot_polling_task: asyncio.Task | None = None


def _on_polling_done(task: asyncio.Task) -> None:
    """Логирует фатальный сбой polling (без него задача падает молча)."""
    if task.cancelled():
        log.info("Bot polling задача отменена (shutdown)")
        return
    exc = task.exception()
    if exc is not None:
        log.error("Bot polling задача упала с необработанным исключением: %r", exc)
        log.error("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


async def _bot_polling() -> None:
    """Запускает aiogram long-polling в фоне (только если есть BOT_TOKEN)."""
    if not settings.bot_token:
        log.warning("BOT_TOKEN не задан — polling бота пропущен (можно тестировать API/БД без бота)")
        return
    # get_bot() лениво создаёт singleton через runtime (подключение к Local Bot API)
    bot = get_bot()
    dp = get_dispatcher()
    me = await get_me()
    if me:
        log.info("Бот @%s запущен (polling через Local Bot API)", me.get("username"))
    else:
        log.warning("getMe не прошёл — проверьте logOut (Фаза 1) и доступность local-bot-api:8081")
    # Смена хоста: на старом сервере polling мог оставить зарегистрированный
    # webhook (или «зависший» long-polling-слот) — тогда getUpdates возвращает
    # пустой список и бот молчит. Снимаем webhook перед стартом, если он есть.
    try:
        wh = await bot.get_webhook_info()
        if wh.url:
            log.warning("Обнаружен webhook %s — удаляем перед polling", wh.url)
            await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:  # noqa: BLE001 — не роняем старт из-за этой проверки
        log.warning("get_webhook_info/delete_webhook не прошли: %r (продолжаем)", e)
    # P2-30: retry loop с exponential backoff для non-CancelledError
    retry_delay = 5.0
    max_retry_delay = 300.0  # 5 минут макс
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        except asyncio.CancelledError:
            log.info("Bot polling остановлен")
            return
        except Exception:
            log.exception("Bot polling упал — retry через %.0f сек", retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_retry_delay)
            continue
        else:
            # start_polling завершился нормально (shutdown)
            return


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Жизненный цикл: инициализация БД, бота, планировщика."""
    global _bot_polling_task
    log.info("Запуск приложения (Фаза 2)…")

    if not settings.secret_token:
        log.warning(
            "SECRET_TOKEN не задан — /health/bot-api и /health/scheduler открыты без auth. "
            "В production обязательно задайте SECRET_TOKEN (см. .env.example)."
        )

    # 1. БД (ТЗ §5)
    await init_db()

    # 2. Первичный admin (ТЗ §2.6)
    if settings.admin_tg_id:
        await bootstrap_admin(settings.admin_tg_id, None)

    # 3. БД тегов Gelbooru (ТЗ §10.1)
    await tags_db.init_tags_db()

    # 4. Кеш Gelbooru + фоновая очистка (ТЗ §10.1)
    cache.init_cache()
    _cache_cleanup_task = cache.start_cleanup_task()

    # 5. Планировщик (ТЗ §2.5)
    await setup_scheduler_async()
    sched = get_scheduler()
    sched.start()
    log.info("APScheduler запущен")

    # 6. Worker — обработка task_queue (ТЗ §4.2.2, Фаза 3)
    # Запускается ПОСЛЕ scheduler, т.к. worker переводит посты new→pending, а
    # scheduler уже должен быть готов их отправлять. Worker внутри uvicorn-процесса
    # (как и scheduler) — отдельный процесс не нужен (SQLite single-writer).
    start_worker()

    # 7. Bot polling (ТЗ §8)
    _bot_polling_task = asyncio.create_task(_bot_polling())
    _bot_polling_task.add_done_callback(_on_polling_done)

    try:
        yield
    finally:
        # Shutdown — закрываем ВСЕ ресурсы (code review CRITICAL #1)
        log.info("Остановка приложения…")
        # Worker останавливаем ПЕРВЫМ — чтобы не было гонки между переводом постов
        # в pending и закрытием БД/scheduler. Worker.cancel → in_progress задачи
        # останутся (сбросятся cleanup'ом при следующем старте).
        try:
            await stop_worker()
        except Exception as e:  # noqa: BLE001
            log.warning("stop_worker: %s", e)
        if _bot_polling_task:
            _bot_polling_task.cancel()
            try:
                await _bot_polling_task
            except asyncio.CancelledError:
                pass
        # CRITICAL: sched.shutdown(wait=True) даёт running jobs корректно завершиться,
        # но bounded timeout 30с — иначе зависший job блокирует close_db и контейнер
        # падает по SIGKILL с потерей данных.
        #
        # AsyncIOScheduler.shutdown(wait=True) — СИНХРОННЫЙ блокирующий вызов
        # (APScheduler 3.x не возвращает coroutine). В async-контексте заворачиваем
        # его в run_in_executor, чтобы не заморозить event loop, и ограничиваем 30с.
        try:
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: sched.shutdown(wait=True)),
                timeout=30,
            )
        except TimeoutError:
            log.warning(
                "APScheduler shutdown превысил 30с — принудительно завершаем (running jobs могут потерять состояние)"
            )
        except Exception as e:  # noqa: BLE001
            log.warning("APScheduler shutdown завершился с ошибкой: %s", e)
        # Закрываем gelbooru client (aiohttp ClientSession)
        try:
            from app.services.parsers.gelbooru import gelbooru_client
            await gelbooru_client.close()
        except Exception as e:  # noqa: BLE001
            log.warning("gelbooru_client.close: %s", e)
        # Закрываем внешние парсеры (Фаза 3 этап 5 — aiohttp ClientSessions)
        try:
            from app.services.parsers.danbooru import close_danbooru_parser
            from app.services.parsers.pikabu import close_pikabu_parser
            from app.services.parsers.pixiv import close_pixiv_parser
            from app.services.parsers.xcom import close_xcom_parser

            await close_danbooru_parser()
            await close_xcom_parser()
            await close_pikabu_parser()
            await close_pixiv_parser()
        except Exception as e:  # noqa: BLE001
            log.warning("external parsers close: %s", e)
        # Отменяем cache cleanup task
        if _cache_cleanup_task and not _cache_cleanup_task.done():
            _cache_cleanup_task.cancel()
            try:
                await _cache_cleanup_task
            except asyncio.CancelledError:
                pass
        # Закрываем bot (AiohttpSession)
        try:
            from app.bot import close_bot
            await close_bot()
        except Exception as e:  # noqa: BLE001
            log.warning("close_bot: %s", e)
        await tags_db.close_tags_db()
        await close_db()
        log.info("Приложение остановлено")


app = FastAPI(title="TG Media Queue Bot", version="0.3.0-phase3-stage5", lifespan=lifespan)


# P2-29: Простая авторизация для защищённых endpoints через SECRET_TOKEN.
# Используем secrets.compare_digest — постоянное по времени сравнение,
# чтобы исключить timing-атаку на SECRET_TOKEN (code review HIGH #1).
async def verify_token(x_secret_token: str | None = Header(None)) -> None:
    """Проверяет SECRET_TOKEN в заголовке X-Secret-Token.

    - Если SECRET_TOKEN не задан в конфигурации — endpoint открыт (для локальной разработки).
    - Если задан и не совпал — 401 Unauthorized (семантически правильно для auth: 403
      подразумевает аутентифицированного пользователя без прав, здесь же токен именно
      credential).
    """
    if settings.secret_token:
        if not x_secret_token or not secrets.compare_digest(x_secret_token, settings.secret_token):
            raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing X-Secret-Token")


@app.get("/health")
async def health() -> dict:
    """Liveness probe БЕЗ auth — для Docker HEALTHCHECK и load balancer.

    Не раскрывает конфигурацию (bot_api_url, config_problems) — этого достаточно,
    чтобы сказать «контейнер жив», и не более. Подробности — в /health/bot-api
    и /health/scheduler (за X-Secret-Token).
    """
    return {"status": "ok"}


@app.get("/health/bot-api")
async def health_bot_api(_token: None = Depends(verify_token)) -> dict:
    """getMe через Local Bot API Server (ТЗ §3.3)."""
    me = await get_me()
    if me:
        return {"status": "ok", "bot": {"username": me.get("username"), "id": me.get("id")}}
    return {"status": "error", "detail": "Local Bot API недоступен или токен отклонён; см. scripts/check_phase1.py"}


@app.get("/health/scheduler")
async def health_scheduler(_token: None = Depends(verify_token)) -> dict:
    """Состояние планировщика: список активных job'ов очередей (Фаза 2)."""
    sched = get_scheduler()
    jobs = []
    for job in sched.get_jobs():
        jobs.append({
            "id": job.id,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
        })
    return {"status": "ok" if sched.running else "stopped", "jobs": jobs}


@app.get("/health/worker")
async def health_worker(_token: None = Depends(verify_token)) -> dict:
    """Состояние worker'а (Фаза 3): running + счётчики + статистика task_queue."""
    from app.worker import get_worker

    w = get_worker()
    stats = {
        "queued": await tq_repo.count_by_status("queued"),
        "in_progress": await tq_repo.count_by_status("in_progress"),
        "done": await tq_repo.count_by_status("done"),
        "error": await tq_repo.count_by_status("error"),
    }
    # Статистика подписок Gelbooru (Фаза 3 этап 3)
    gelbooru_stats: dict[str, int] = {}
    try:
        from app.repositories import gelbooru_subs as gs_repo

        gelbooru_stats = {
            "new_posts": await gs_repo.count_new_posts_all(),
        }
    except Exception:  # noqa: BLE001 — статистика опциональна
        pass
    return {
        "status": "ok" if w.running else "stopped",
        "processed": w.processed,
        "errors": w.errors,
        "requeued": w.requeued,
        "subs_checked": w.subs_checked,
        "subs_new_posts": w.subs_new_posts,
        "subs_notified": w.subs_notified,
        "task_queue": stats,
        "gelbooru": gelbooru_stats,
    }
