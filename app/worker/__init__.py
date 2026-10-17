"""Worker — обработчик персистентной очереди задач task_queue (ТЗ §4.2.2, Фаза 3).

Жизненный цикл поста (ТЗ §2.3.4):
    new → downloading → pending → sent
                  |       └→ failed
                  └→ duplicate / possible_duplicate

Worker опрашивает task_queue на задачи со статусом 'queued' и обрабатывает их:
  - task_type='download': скачивает медиа (new → downloading → pending).
      * Если у поста есть tg_file_id (Telegram-источник) — скачивать не нужно,
        сразу переводим в pending (стратегия file_id, ТЗ §2.1.1 п.4).
      * Иначе — download_media(source_url) → сохраняем file_path/file_type/file_size.
  - task_type='process': медиа-конвейер (кроп, перекодировка) — Фаза 5, пока stub.
  - task_type='repost': повторная отправка — пока stub.

Retry-стратегия:
  - task_queue.attempts инкрементируется в take_next() при каждом захвате.
  - При retryable-ошибке (сеть/429/5xx/timeout): если attempts < max_attempts →
    requeue (status=queued, attempts сохраняется); иначе → mark_error + mark_failed.
  - При permanent-ошибке (4xx/превышение размера/нет URL): сразу mark_error +
    mark_failed, без ретрая.

Stale-cleanup: периодически (worker_cleanup_interval_seconds) сбрасывает зависшие
in_progress задачи (worker упал / рестарт) обратно в queued — см. reset_stale_in_progress.

Запускается внутри uvicorn-процесса (как и scheduler) — отдельный процесс не нужен,
т.к. SQLite single-writer и task_queue рассчитана на одного потребителя.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config.settings import settings
from app.repositories import (
    posts as posts_repo,
)
from app.repositories import (
    task_queue as tq,
)

log = logging.getLogger("worker")


class Worker:
    """Фоновый обработчик task_queue + опросчик подписок Gelbooru.

    Один экземпляр на процесс (singleton через get_worker()). Запускает три asyncio-задачи:
      - _poll_loop: цикл опроса task_queue (download/process/repost);
      - _cleanup_loop: периодический сброс stale in_progress задач;
      - _subscription_loop: опрос due-подписок gelbooru (Фаза 3 этап 3).

    Безопасен для graceful shutdown: stop() отменяет все задачи и ждёт их завершения.
    """

    def __init__(self) -> None:
        self._poll_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._subscription_task: asyncio.Task | None = None
        self._running = False
        # Счётчики для /health/worker и логирования
        self.processed = 0
        self.errors = 0
        self.requeued = 0
        # Счётчики подписок (Фаза 3 этап 3)
        self.subs_checked = 0
        self.subs_new_posts = 0
        self.subs_notified = 0

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Запускает polling-, cleanup- и subscription-циклы. Idempotent."""
        if self._running:
            log.warning("Worker.start(): уже запущен — пропуск")
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop(), name="worker.poll")
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="worker.cleanup")
        if settings.gelbooru_subs_enabled:
            self._subscription_task = asyncio.create_task(self._subscription_loop(), name="worker.subs")
        else:
            log.info("Worker: подписки Gelbooru отключены (gelbooru_subs_enabled=False)")
        log.info(
            "Worker запущен (poll=%.1fs, max_attempts=%d, stale=%dмин, cleanup=%ds, subs=%ds)",
            settings.worker_poll_interval_seconds,
            settings.worker_max_attempts,
            settings.worker_stale_timeout_minutes,
            settings.worker_cleanup_interval_seconds,
            settings.gelbooru_subs_poll_interval_seconds if settings.gelbooru_subs_enabled else 0,
        )

    async def stop(self) -> None:
        """Останавливает циклы и ждёт их завершения (graceful shutdown)."""
        if not self._running:
            return
        self._running = False
        tasks = [t for t in (self._poll_task, self._cleanup_task, self._subscription_task) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001 — не роняем shutdown
                log.warning("Worker task завершился с ошибкой при stop: %s", e)
        self._poll_task = None
        self._cleanup_task = None
        self._subscription_task = None
        log.info(
            "Worker остановлен (processed=%d, errors=%d, requeued=%d, subs_checked=%d, subs_new=%d, subs_notified=%d)",
            self.processed, self.errors, self.requeued,
            self.subs_checked, self.subs_new_posts, self.subs_notified,
        )

    # ------------------------------------------------------------------
    # Основной polling-цикл
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Цикл: take_next → process → mark_done/error/requeue → sleep → повтор."""
        log.info("Worker poll-loop запущен")
        while self._running:
            try:
                task = await tq.take_next()
                if task is None:
                    # Очередь пуста — спим, чтобы не грузить БД busy-loop'ом
                    await asyncio.sleep(settings.worker_poll_interval_seconds)
                    continue
                await self._process_task(task)
            except asyncio.CancelledError:
                log.info("Worker poll-loop отменён")
                raise
            except Exception as e:  # noqa: BLE001 — не роняем цикл из-за одной задачи
                log.exception("Worker poll-loop: необработанная ошибка: %s", e)
                # Короткая пауза перед продолжением — защита от tight error-loop
                await asyncio.sleep(min(settings.worker_poll_interval_seconds, 5.0))

    async def _process_task(self, task: dict[str, Any]) -> None:
        """Обрабатывает одну захваченную задачу (status уже in_progress)."""
        task_id = task["id"]
        task_type = task["task_type"]
        post_id = task["post_id"]
        attempts = task.get("attempts", 0)
        self.processed += 1

        log.info("Worker: задача #%s (type=%s, post #%s, attempts=%d)", task_id, task_type, post_id, attempts)

        try:
            if task_type == "download":
                await self._handle_download(task)
            elif task_type == "process":
                # Медиа-конвейер (кроп/перекодировка) — Фаза 5. Пока stub.
                log.info("Worker: task_type='process' (post #%s) — не реализован, mark done", post_id)
            elif task_type == "repost":
                # Повторная отправка — будущий этап. Пока stub.
                log.info("Worker: task_type='repost' (post #%s) — не реализован, mark done", post_id)
            else:
                log.warning("Worker: неизвестный task_type=%r (задача #%s) — mark error", task_type, task_id)
                await tq.mark_error(task_id, f"Неизвестный task_type: {task_type}")
                self.errors += 1
                return

            await tq.mark_done(task_id)
        except _PermanentTaskError as e:
            # Неустранимая ошибка — без ретрая
            log.error("Worker: permanent error (задача #%s, post #%s): %s", task_id, post_id, e)
            await tq.mark_error(task_id, str(e))
            await posts_repo.mark_failed(post_id, str(e))
            self.errors += 1
        except _RetryableTaskError as e:
            # Ретраимая ошибка — проверяем лимит попыток
            if attempts >= settings.worker_max_attempts:
                log.error(
                    "Worker: retryable error, лимит попыток исчерпан (задача #%s, post #%s, attempts=%d): %s",
                    task_id, post_id, attempts, e,
                )
                await tq.mark_error(task_id, f"Лимит попыток ({attempts}): {e}")
                await posts_repo.mark_failed(post_id, f"Лимит попыток: {e}")
                self.errors += 1
            else:
                log.warning(
                    "Worker: retryable error, requeue (задача #%s, post #%s, attempts=%d/%d): %s",
                    task_id, post_id, attempts, settings.worker_max_attempts, e,
                )
                await tq.requeue(task_id)
                self.requeued += 1
        except asyncio.CancelledError:
            # Shutdown — задачу НЕ помечаем done/error (она останется in_progress,
            # будет сброшена cleanup'ом при следующем старте). Поднимаем дальше.
            log.info("Worker: задача #%s отменена при shutdown (останется in_progress → stale cleanup)", task_id)
            raise
        except Exception as e:  # noqa: BLE001 — любая неожиданная ошибка = retryable
            # Неизвестная ошибка — трактуем как retryable (безопаснее, чем permanent)
            if attempts >= settings.worker_max_attempts:
                log.exception(
                    "Worker: неожиданная ошибка, лимит попыток (задача #%s, post #%s): %s",
                    task_id, post_id, e,
                )
                await tq.mark_error(task_id, f"Неожиданная ошибка (попыток {attempts}): {e}")
                await posts_repo.mark_failed(post_id, str(e))
                self.errors += 1
            else:
                log.warning(
                    "Worker: неожиданная ошибка, requeue (задача #%s, post #%s, attempts=%d): %s",
                    task_id, post_id, attempts, e,
                )
                await tq.requeue(task_id)
                self.requeued += 1

    # ------------------------------------------------------------------
    # Обработчики по task_type
    # ------------------------------------------------------------------

    async def _handle_download(self, task: dict[str, Any]) -> None:
        """Обрабатывает task_type='download': скачивает медиа для поста.

        Стратегия (ТЗ §2.1.1 п.4, §4.2.2):
          1. Пост уже имеет tg_file_id (Telegram-источник) → скачивать не нужно,
             сразу pending (отправка пойдёт по file_id).
          2. Пост имеет source_url (внешний источник) → download_media().
          3. Нет ни того, ни другого → permanent error.

        Жизненный цикл: new → downloading → pending (success) | failed (error).
        """
        post_id = task["post_id"]
        payload = task.get("payload") or {}

        post = await posts_repo.get_post(post_id)
        if post is None:
            raise _PermanentTaskError(f"Пост #{post_id} не найден (удалён?)")

        # 1. file_id-стратегия: пост из Telegram, скачивать не нужно
        if post.get("tg_file_id") and not post.get("source_url"):
            log.info("Worker: пост #%s уже имеет tg_file_id — скачивание не требуется, → pending", post_id)
            await posts_repo.update_status(post_id, "pending")
            return

        # Если у поста уже есть file_path (повторная обработка / ручная загрузка) —
        # считаем готовым к отправке.
        if post.get("file_path"):
            log.info("Worker: пост #%s уже имеет file_path=%s — → pending", post_id, post["file_path"])
            await posts_repo.update_status(post_id, "pending")
            return

        url = post.get("source_url") or payload.get("media_url") or payload.get("url")
        if not url:
            raise _PermanentTaskError(
                f"Пост #{post_id}: нет tg_file_id, file_path и source_url — скачать невозможно"
            )

        # 2. Скачивание внешнего файла
        await posts_repo.update_status(post_id, "downloading")
        try:
            from app.services.media.downloader import (
                DownloadError,
                PermanentDownloadError,
                download_media,
            )

            result = await download_media(
                url=url,
                post_id=post_id,
                referer=payload.get("referer"),
                user_agent=payload.get("user_agent"),
                extra_headers=payload.get("headers"),
            )
        except PermanentDownloadError as e:
            raise _PermanentTaskError(str(e)) from e
        except DownloadError as e:
            raise _RetryableTaskError(str(e)) from e

        # Сохраняем метаданные файла + переводим в pending (готов к отправке)
        await posts_repo.set_file_info(
            post_id,
            file_path=result["file_path"],
            file_type=result["file_type"],
            file_size=result["file_size"],
            mime_type=result["mime_type"],
        )

        # pHash-дедупликация (ТЗ §2.4.3, Фаза 3 этап 2):
        # После скачивания вычисляем pHash (только для изображений), сохраняем в БД,
        # ищем визуальные дубликаты. Если найден → possible_duplicate (не pending),
        # ручная проверка модератором. Иначе → pending (готов к отправке).
        final_status = "pending"
        try:
            from app.services.dedup.phash import check_visual_duplicate, compute_and_store_phash

            phash = await compute_and_store_phash(post_id, result["file_path"], result["file_type"])
            if phash is not None:
                dupes = await check_visual_duplicate(phash, exclude_post_id=post_id)
                if dupes:
                    dupe_ids = [d["post_id"] for d in dupes[:5]]
                    log.warning(
                        "Worker: пост #%s — визуальный дубликат (pHash=%s, distance≤%d): похожие посты %s",
                        post_id, phash, settings.phash_max_distance, dupe_ids,
                    )
                    final_status = "possible_duplicate"
                else:
                    log.info("Worker: пост #%s — pHash уникален (%s)", post_id, phash)
        except Exception as e:  # noqa: BLE001 — дедупликация не должна блокировать отправку
            log.warning("Worker: pHash-проверка поста #%s не удалась (пропуск, → pending): %s", post_id, e)

        await posts_repo.update_status(post_id, final_status)
        log.info("Worker: пост #%s → %s (file_path=%s)", post_id, final_status, result["file_path"])

        # Архивация в служебный канал (ТЗ §2.4.5, блок D).
        # После скачивания файла — дублируем в служебный канал (если задан).
        # archive_post теперь поддерживает file_path (через FSInputFile).
        try:
            from app.services.archive import archive_post

            # Достаём обновлённый пост с file_path
            post_with_file = await posts_repo.get_post(post_id)
            if post_with_file and post_with_file.get("file_path"):
                # target_channel_id: 0 = нет целевого канала (архивация все равно
                # использует archive_channel из app_settings). Передаём 0.
                await archive_post(post_with_file, 0, [result["file_type"]])
        except Exception as e:  # noqa: BLE001 — архивация не критична
            log.warning("Worker: архивация поста #%s не удалась: %s", post_id, e)

    # ------------------------------------------------------------------
    # Stale-cleanup цикл
    # ------------------------------------------------------------------

    async def _cleanup_loop(self) -> None:
        """Периодически сбрасывает зависшие in_progress задачи в queued."""
        log.info("Worker cleanup-loop запущен (интервал %ds)", settings.worker_cleanup_interval_seconds)
        while self._running:
            try:
                await asyncio.sleep(settings.worker_cleanup_interval_seconds)
                if not self._running:
                    break
                reset = await tq.reset_stale_in_progress(settings.worker_stale_timeout_minutes)
                if reset > 0:
                    log.warning(
                        "Worker cleanup: сброшено %d зависших in_progress задач (stale > %dмин)",
                        reset, settings.worker_stale_timeout_minutes,
                    )
            except asyncio.CancelledError:
                log.info("Worker cleanup-loop отменён")
                raise
            except Exception as e:  # noqa: BLE001 — не роняем cleanup-цикл
                log.exception("Worker cleanup-loop: необработанная ошибка: %s", e)
                await asyncio.sleep(60.0)  # пауза перед повтором

    # ------------------------------------------------------------------
    # Subscription-цикл (Фаза 3 этап 3 — опрос подписок Gelbooru)
    # ------------------------------------------------------------------

    async def _subscription_loop(self) -> None:
        """Периодически опрашивает due-подписки gelbooru (ТЗ §2.1.4).

        Интервал: settings.gelbooru_subs_poll_interval_seconds (по умолчанию 60с).
        Внутри цикла вызывается check_all_due_subscriptions(), которая:
          - берёт подписки с подошедшим check_interval_hours;
          - для каждой: search_posts(tags + blacklist) → новые посты → seen_posts;
          - уведомляет админа по notify_interval_minutes.
        """
        log.info("Worker subscription-loop запущен (интервал %ds)", settings.gelbooru_subs_poll_interval_seconds)
        while self._running:
            try:
                await asyncio.sleep(settings.gelbooru_subs_poll_interval_seconds)
                if not self._running:
                    break
                from app.services.gelbooru_subscriptions import check_all_due_subscriptions

                stats = await check_all_due_subscriptions()
                if stats["checked"] > 0 or stats["new_posts"] > 0:
                    self.subs_checked += stats["checked"]
                    self.subs_new_posts += stats["new_posts"]
                    self.subs_notified += stats["notified"]
                    log.info(
                        "Worker subs: проверено %d подписок, новых %d, уведомлений %d",
                        stats["checked"], stats["new_posts"], stats["notified"],
                    )
            except asyncio.CancelledError:
                log.info("Worker subscription-loop отменён")
                raise
            except Exception as e:  # noqa: BLE001 — не роняем subs-цикл
                log.exception("Worker subscription-loop: необработанная ошибка: %s", e)
                await asyncio.sleep(60.0)


# ---------------------------------------------------------------------------
# Внутренние классы ошибок (для разграничения retry/permanent в _process_task)
# ---------------------------------------------------------------------------


class _PermanentTaskError(Exception):
    """Неустранимая ошибка задачи — mark_error + mark_failed без ретрая."""


class _RetryableTaskError(Exception):
    """Ретраимая ошибка задачи — requeue (с учётом max_attempts)."""


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_worker: Worker | None = None


def get_worker() -> Worker:
    """Возвращает singleton Worker (создаёт при первом вызове)."""
    global _worker
    if _worker is None:
        _worker = Worker()
    return _worker


def reset_worker() -> None:
    """Сбрасывает singleton (для тестов). Останавливает работающий worker если есть."""
    global _worker
    if _worker is not None and _worker.running:
        # В тестах нет running event loop для await stop() — используем синхронную
        # отмену задач. Реальный stop() вызывается в lifespan.
        for t in (_worker._poll_task, _worker._cleanup_task, _worker._subscription_task):  # noqa: SLF001
            if t is not None and not t.done():
                t.cancel()
    _worker = None


def start_worker() -> Worker:
    """Запускает singleton worker (вызывается из main.py lifespan)."""
    w = get_worker()
    w.start()
    return w


async def stop_worker() -> None:
    """Останавливает singleton worker (вызывается из main.py lifespan shutdown)."""
    global _worker
    if _worker is not None:
        await _worker.stop()
    _worker = None


# ---------------------------------------------------------------------------
# Публичный API для тестов: обработать одну задачу синхронно (без loop)
# ---------------------------------------------------------------------------


async def process_one_task() -> bool:
    """Берёт и обрабатывает одну задачу из очереди. Для тестов и ручного запуска.

    Возвращает True если задача была обработана, False если очередь пуста.
    НЕ запускает polling-loop — просто один шаг.
    """
    task = await tq.take_next()
    if task is None:
        return False
    await get_worker()._process_task(task)  # noqa: SLF001 — публичный API для тестов
    return True
