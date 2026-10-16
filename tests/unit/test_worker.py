"""Тесты worker'а и downloader'а (ТЗ §4.2.2, Фаза 3, этап 1).

Покрытие:
- downloader: чистые функции (_file_type_from_content_type, _referer_for, _extension_for)
- posts.set_file_info: сохранение метаданных скачанного файла
- worker._handle_download:
  * file_id-стратегия (нет source_url → pending без скачивания)
  * уже есть file_path → pending
  * нет ни file_id, ни source_url → permanent error (failed)
  * успешное скачивание (mock) → pending + set_file_info
  * PermanentDownloadError → failed без ретрая
  * DownloadError (retryable) → requeue до max_attempts, затем failed
- worker.process_one_task: один шаг polling'а (пустая очередь → False)
- stale cleanup: reset_stale_in_progress сбрасывает перевисшие задачи

Сетевые вызовы НЕ выполняются — download_media мокается через monkeypatch.
"""
from __future__ import annotations

import asyncio

import pytest

# ===========================================================================
# Downloader: чистые функции (без сети)
# ===========================================================================


def test_file_type_from_content_type():
    from app.services.media.downloader import _file_type_from_content_type

    assert _file_type_from_content_type("image/jpeg", "http://x/a.jpg") == "photo"
    assert _file_type_from_content_type("image/png", "http://x/a.png") == "photo"
    assert _file_type_from_content_type("image/gif", "http://x/a.gif") == "gif"
    assert _file_type_from_content_type("video/mp4", "http://x/a.mp4") == "video"
    assert _file_type_from_content_type("application/octet-stream", "http://x/a.mp4") == "video"
    assert _file_type_from_content_type("application/octet-stream", "http://x/a.jpg") == "photo"
    assert _file_type_from_content_type("application/octet-stream", "http://x/a.bin") == "document"
    assert _file_type_from_content_type("", "http://x/a.gif") == "gif"
    assert _file_type_from_content_type("text/html", "http://x/a") == "document"
    # Content-Type с charset
    assert _file_type_from_content_type("image/jpeg; charset=binary", "http://x/a") == "photo"


def test_file_type_from_content_type_edge():
    from app.services.media.downloader import _file_type_from_content_type

    # Пустой URL и Content-Type → document
    assert _file_type_from_content_type("", "") == "document"
    assert _file_type_from_content_type(None, "") == "document"  # type: ignore[arg-type]


def test_referer_for():
    from app.services.media.downloader import _referer_for

    # Явный referer приоритетнее
    assert _referer_for("https://gelbooru.com/x", "https://custom.com/") == "https://custom.com/"
    # По домену: gelbooru
    assert _referer_for("https://gelbooru.com/index.php", None) == "https://gelbooru.com/"
    # pixiv CDN
    assert _referer_for("https://i.pximg.net/img/a.jpg", None) == "https://www.pixiv.net/"
    # Поддомен
    assert _referer_for("https://img.gelbooru.com/x.jpg", None) == "https://gelbooru.com/"
    # Неизвестный домен → корень этого домена
    assert _referer_for("https://example.com/path", None) == "https://example.com/"
    # Пустой URL → fallback
    assert _referer_for("", None) == "https://example.com/"


def test_extension_for():
    from app.services.media.downloader import _extension_for

    # Из URL
    assert _extension_for("photo", "https://x.com/path/image.jpg", "image/jpeg") == ".jpg"
    assert _extension_for("video", "https://x.com/clip.mp4", "video/mp4") == ".mp4"
    assert _extension_for("gif", "https://x.com/an.gif", "image/gif") == ".gif"
    # URL без расширения → из Content-Type
    assert _extension_for("photo", "https://x.com/noext", "image/jpeg") == ".jpg"
    assert _extension_for("video", "https://x.com/noext", "video/mp4") == ".mp4"
    # Ничего нет → fallback по file_type
    assert _extension_for("photo", "https://x.com/noext", "") == ".jpg"
    assert _extension_for("video", "https://x.com/noext", "") == ".mp4"
    assert _extension_for("gif", "https://x.com/noext", "") == ".gif"
    assert _extension_for("document", "https://x.com/noext", "") == ".bin"


# ===========================================================================
# posts.set_file_info
# ===========================================================================


def test_set_file_info(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo

        pid = await posts_repo.create_post(
            source_type="gelbooru",
            file_type="document",
            source_url="https://gelbooru.com/x",
            status="downloading",
        )
        ok = await posts_repo.set_file_info(
            pid,
            file_path="/data/downloads/post_1.jpg",
            file_type="photo",
            file_size=12345,
            mime_type="image/jpeg",
        )
        assert ok is True
        post = await posts_repo.get_post(pid)
        assert post["file_path"] == "/data/downloads/post_1.jpg"
        assert post["file_type"] == "photo"
        assert post["file_size"] == 12345
        assert post["mime_type"] == "image/jpeg"

    run_with_db(tmp_path, body)


def test_set_file_info_partial_update(tmp_path):
    """set_file_info с None-полями не затрагивает существующие значения (COALESCE)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo

        pid = await posts_repo.create_post(
            source_type="gelbooru",
            file_type="photo",
            file_size=100,
            status="downloading",
        )
        # Обновляем только file_path, остальное сохраняется
        await posts_repo.set_file_info(pid, file_path="/tmp/x.jpg")
        post = await posts_repo.get_post(pid)
        assert post["file_path"] == "/tmp/x.jpg"
        assert post["file_type"] == "photo"  # не изменилось
        assert post["file_size"] == 100  # не изменилось

    run_with_db(tmp_path, body)


# ===========================================================================
# Worker: _handle_download
# ===========================================================================


def test_worker_download_file_id_strategy(tmp_path):
    """Пост с tg_file_id (и без source_url) → pending без скачивания (стратегия file_id)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.worker import process_one_task, reset_worker

        reset_worker()
        pid = await posts_repo.create_post(
            source_type="telegram_direct",
            file_type="photo",
            tg_file_id="AgADBgADabc",
            status="new",
        )
        tid = await tq.enqueue(pid, "download")

        # process_one_task должен обработать и вернуть True
        handled = await process_one_task()
        assert handled is True

        post = await posts_repo.get_post(pid)
        assert post["status"] == "pending"
        task = await _get_task(tid)
        assert task["status"] == "done"
        reset_worker()

    run_with_db(tmp_path, body)


def test_worker_download_already_has_filepath(tmp_path):
    """Пост уже имеет file_path → pending (повторная обработка)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.worker import process_one_task, reset_worker

        reset_worker()
        pid = await posts_repo.create_post(
            source_type="gelbooru",
            file_type="photo",
            source_url="https://gelbooru.com/x.jpg",
            file_path="/data/downloads/post_1.jpg",
            status="new",
        )
        tid = await tq.enqueue(pid, "download")
        handled = await process_one_task()
        assert handled is True
        post = await posts_repo.get_post(pid)
        assert post["status"] == "pending"
        task = await _get_task(tid)
        assert task["status"] == "done"
        reset_worker()

    run_with_db(tmp_path, body)


def test_worker_download_no_url_no_fileid(tmp_path):
    """Пост без tg_file_id, file_path и source_url → permanent error (failed)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.worker import process_one_task, reset_worker

        reset_worker()
        pid = await posts_repo.create_post(
            source_type="chrome_ext",
            file_type="photo",
            status="new",
            # НЕТ source_url, tg_file_id, file_path
        )
        tid = await tq.enqueue(pid, "download")
        handled = await process_one_task()
        assert handled is True
        post = await posts_repo.get_post(pid)
        assert post["status"] == "failed"
        assert post["last_error"] is not None
        task = await _get_task(tid)
        assert task["status"] == "error"
        reset_worker()

    run_with_db(tmp_path, body)


def test_worker_download_success(tmp_path, monkeypatch):
    """Успешное скачивание (mock) → pending + file_path сохранён."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.worker import process_one_task, reset_worker

        reset_worker()

        # Мокаем download_media — возвращаем успех без сети
        async def fake_download(url, post_id, **kwargs):
            return {
                "file_path": f"/data/downloads/post_{post_id}.jpg",
                "file_type": "photo",
                "file_size": 999,
                "mime_type": "image/jpeg",
            }

        # Патчим на уровне модуля downloader, который импортирует worker
        import app.services.media.downloader as dl

        monkeypatch.setattr(dl, "download_media", fake_download)

        pid = await posts_repo.create_post(
            source_type="gelbooru",
            file_type="document",  # должен обновиться на photo из mock
            source_url="https://gelbooru.com/x.jpg",
            status="new",
        )
        tid = await tq.enqueue(pid, "download", {"referer": "https://gelbooru.com/"})

        handled = await process_one_task()
        assert handled is True

        post = await posts_repo.get_post(pid)
        assert post["status"] == "pending"
        assert post["file_path"] == f"/data/downloads/post_{pid}.jpg"
        assert post["file_type"] == "photo"  # обновлён из Content-Type
        assert post["file_size"] == 999
        task = await _get_task(tid)
        assert task["status"] == "done"
        reset_worker()

    run_with_db(tmp_path, body)


def test_worker_download_permanent_error(tmp_path, monkeypatch):
    """PermanentDownloadError → failed + error без ретрая (4xx / размер)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.worker import process_one_task, reset_worker

        reset_worker()

        async def fake_download(url, post_id, **kwargs):
            from app.services.media.downloader import PermanentDownloadError

            raise PermanentDownloadError("HTTP 404")

        import app.services.media.downloader as dl

        monkeypatch.setattr(dl, "download_media", fake_download)

        pid = await posts_repo.create_post(
            source_type="gelbooru",
            file_type="photo",
            source_url="https://gelbooru.com/missing.jpg",
            status="new",
        )
        tid = await tq.enqueue(pid, "download")

        handled = await process_one_task()
        assert handled is True

        post = await posts_repo.get_post(pid)
        assert post["status"] == "failed"
        task = await _get_task(tid)
        assert task["status"] == "error"
        # attempts = 1 (один take_next), но permanent → сразу error без requeue
        assert task["attempts"] == 1
        reset_worker()

    run_with_db(tmp_path, body)


def test_worker_download_retryable_then_requeue(tmp_path, monkeypatch):
    """DownloadError (retryable) при attempts < max → requeue (status=queued)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.worker import process_one_task, reset_worker

        reset_worker()

        async def fake_download(url, post_id, **kwargs):
            from app.services.media.downloader import DownloadError

            raise DownloadError("HTTP 503")

        import app.services.media.downloader as dl

        monkeypatch.setattr(dl, "download_media", fake_download)

        pid = await posts_repo.create_post(
            source_type="gelbooru",
            file_type="photo",
            source_url="https://gelbooru.com/x.jpg",
            status="new",
        )
        tid = await tq.enqueue(pid, "download")

        handled = await process_one_task()
        assert handled is True

        # Должен быть requeued (attempts=1 < max=3)
        task = await _get_task(tid)
        assert task["status"] == "queued"
        assert task["attempts"] == 1
        # Пост НЕ failed (ещё есть попытки)
        post = await posts_repo.get_post(pid)
        assert post["status"] != "failed"
        reset_worker()

    run_with_db(tmp_path, body)


def test_worker_download_retry_exhausted(tmp_path, monkeypatch):
    """DownloadError после max_attempts → failed + error."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.worker import process_one_task, reset_worker

        reset_worker()

        async def fake_download(url, post_id, **kwargs):
            from app.services.media.downloader import DownloadError

            raise DownloadError("HTTP 503")

        import app.services.media.downloader as dl

        monkeypatch.setattr(dl, "download_media", fake_download)

        pid = await posts_repo.create_post(
            source_type="gelbooru",
            file_type="photo",
            source_url="https://gelbooru.com/x.jpg",
            status="new",
        )
        tid = await tq.enqueue(pid, "download")

        # process_one_task 3 раза (max_attempts=3): 1st → requeue, 2nd → requeue, 3rd → error
        await process_one_task()  # attempts=1 → requeue
        task = await _get_task(tid)
        assert task["status"] == "queued"
        assert task["attempts"] == 1

        await process_one_task()  # attempts=2 → requeue
        task = await _get_task(tid)
        assert task["status"] == "queued"
        assert task["attempts"] == 2

        await process_one_task()  # attempts=3 → error (>= max_attempts)
        task = await _get_task(tid)
        assert task["status"] == "error"
        assert task["attempts"] == 3

        post = await posts_repo.get_post(pid)
        assert post["status"] == "failed"
        reset_worker()

    run_with_db(tmp_path, body)


def test_worker_process_one_task_empty_queue(tmp_path):
    """process_one_task на пустой очереди → False (не блокирует)."""
    from conftest import run_with_db

    async def body():
        from app.worker import process_one_task, reset_worker

        reset_worker()
        handled = await process_one_task()
        assert handled is False
        reset_worker()

    run_with_db(tmp_path, body)


def test_worker_process_and_repost_stubs(tmp_path):
    """task_type='process' и 'repost' — stub'ы Фазы 5, mark done без ошибок.

    task_queue.enqueue валидирует task_type по CHECK-констрейнту схемы
    (download/process/repost) — невалидные значения БД не пропустит. Worker
    помечает process/repost как done (реализация — Фаза 5).
    """
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.worker import process_one_task, reset_worker

        reset_worker()
        pid = await posts_repo.create_post(source_type="x", file_type="photo", status="new")
        t1 = await tq.enqueue(pid, "process")
        t2 = await tq.enqueue(pid, "repost")

        assert await process_one_task() is True
        assert await process_one_task() is True

        task1 = await _get_task(t1)
        task2 = await _get_task(t2)
        assert task1["status"] == "done"
        assert task2["status"] == "done"
        reset_worker()

    run_with_db(tmp_path, body)


def test_worker_db_rejects_invalid_task_type(tmp_path):
    """Схема БД CHECK-констрейнт отклоняет невалидный task_type (защита на уровне БД)."""
    from conftest import run_with_db

    async def body():
        import sqlite3

        from app.db import get_db
        from app.repositories import posts as posts_repo

        pid = await posts_repo.create_post(source_type="x", file_type="photo", status="new")
        db = get_db()
        # Прямой INSERT с невалидным task_type должен упасть с IntegrityError
        with pytest.raises(sqlite3.IntegrityError, match="task_type"):
            await db.execute(
                "INSERT INTO task_queue (post_id, task_type, status) VALUES (?, 'bogus', 'queued')",
                (pid,),
            )

    run_with_db(tmp_path, body)


# ===========================================================================
# Worker: stale cleanup
# ===========================================================================


def test_worker_stale_cleanup_resets_in_progress(tmp_path):
    """Зависшая in_progress задача (старше timeout) сбрасывается в queued."""
    from conftest import run_with_db

    async def body():
        from app.db import get_db
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq

        # Создаём пост (FK task_queue.post_id → posts.id)
        pid = await posts_repo.create_post(source_type="x", file_type="photo", status="new")

        # Создаём in_progress задачу с прошёлым created_at (32 минуты назад)
        db = get_db()
        cur = await db.execute(
            """
            INSERT INTO task_queue (post_id, task_type, status, attempts, created_at)
            VALUES (?, 'download', 'in_progress', 1,
                    datetime('now', '-32 minutes'))
            """,
            (pid,),
        )
        tid = cur.lastrowid
        await db.commit()
        await cur.close()

        # reset_stale_in_progress(30) должен сбросить (32 > 30)
        reset = await tq.reset_stale_in_progress(30)
        assert reset == 1
        task = await _get_task(tid)
        assert task["status"] == "queued"

    run_with_db(tmp_path, body)


def test_worker_stale_cleanup_keeps_recent(tmp_path):
    """Свежая in_progress задача НЕ сбрасывается (моложе timeout)."""
    from conftest import run_with_db

    async def body():
        from app.db import get_db
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq

        pid = await posts_repo.create_post(source_type="x", file_type="photo", status="new")

        db = get_db()
        cur = await db.execute(
            """
            INSERT INTO task_queue (post_id, task_type, status, attempts, created_at)
            VALUES (?, 'download', 'in_progress', 1,
                    datetime('now', '-5 minutes'))
            """,
            (pid,),
        )
        tid = cur.lastrowid
        await db.commit()
        await cur.close()

        reset = await tq.reset_stale_in_progress(30)
        assert reset == 0  # 5 < 30 — не stale
        task = await _get_task(tid)
        assert task["status"] == "in_progress"

    run_with_db(tmp_path, body)


# ===========================================================================
# Worker: start/stop lifecycle
# ===========================================================================


def test_worker_start_stop(tmp_path):
    """Worker.start() запускает polling + cleanup задачи, stop() их отменяет."""
    from conftest import run_with_db

    async def body():
        from app.config.settings import settings
        from app.worker import get_worker, reset_worker

        reset_worker()
        # Короткий интервал для теста
        object.__setattr__(settings, "worker_poll_interval_seconds", 0.1)
        object.__setattr__(settings, "worker_cleanup_interval_seconds", 0.2)
        try:
            w = get_worker()
            assert w.running is False
            w.start()
            assert w.running is True
            # Даём циклам поработать чуть-чуть
            await asyncio.sleep(0.25)
            assert w.running is True
            await w.stop()
            assert w.running is False
        finally:
            object.__setattr__(settings, "worker_poll_interval_seconds", 2.0)
            object.__setattr__(settings, "worker_cleanup_interval_seconds", 300)
            reset_worker()

    run_with_db(tmp_path, body)


def test_worker_start_idempotent(tmp_path):
    """Повторный start() безопасен (log warning, без дубля задач)."""
    from conftest import run_with_db

    async def body():
        from app.config.settings import settings
        from app.worker import get_worker, reset_worker

        reset_worker()
        object.__setattr__(settings, "worker_poll_interval_seconds", 0.5)
        try:
            w = get_worker()
            w.start()
            poll1 = w._poll_task  # noqa: SLF001
            w.start()  # повторный — должен пропустить
            poll2 = w._poll_task  # noqa: SLF001
            assert poll1 is poll2  # задача не пересоздалась
            await w.stop()
        finally:
            object.__setattr__(settings, "worker_poll_interval_seconds", 2.0)
            reset_worker()

    run_with_db(tmp_path, body)


# ===========================================================================
# Helper
# ===========================================================================


async def _get_task(task_id: int) -> dict:
    """Достаёт задачу из БД напрямую (для проверок в тестах)."""
    from app.db import get_db

    db = get_db()
    cur = await db.execute("SELECT * FROM task_queue WHERE id = ?", (task_id,))
    row = await cur.fetchone()
    await cur.close()
    return dict(row)
