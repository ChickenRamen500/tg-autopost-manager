# Changelog

Все заметные изменения проекта фиксируются в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

## [0.4.63-phase4] — 2026-01

Полный code review + исправления багов + новое уведомление о запуске.

### Added

- **Уведомление о запуске/перезапуске бота** (`app/services/startup_notify.py`):
  через `STARTUP_NOTIFY_DELAY_SECONDS` (по умолчанию 60 сек) после старта бот
  отправляет в служебный канал (с дублированием в ЛС админу) ДВА сообщения:
  1. «🟢 Бот запущен, очереди загружены» — сводка по всем очередям: название,
     целевой канал, интервал, активность и распределение постов по статусам
     (pending/sent/failed/new).
  2. Описание последнего коммита ветки GitHub (что сделано в сборке) —
     берётся из GitHub REST API (`GITHUB_REPO` / `GITHUB_BRANCH`).
  Отключается через `STARTUP_NOTIFY_DELAY_SECONDS=0`. Сбои сети/GitHub не роняют
  приложение (отправляется fallback-сообщение).

- Новые переменные окружения: `STARTUP_NOTIFY_DELAY_SECONDS`, `GITHUB_REPO`,
  `GITHUB_BRANCH` (см. `.env.example`).

- `posts.count_statuses_for_queue(queue_id)` — распределение постов очереди по
  статусам одним SQL-запросом (используется сводкой о запуске).

- `posts.count_album_unready(queue_id, media_group_id)` — счётчик постов альбома
  в статусах `new`/`downloading` (для защиты от split-отправки альбома).

- `posts.clear_file_path(post_id)` — репозиторная обёртка над `UPDATE posts SET
  file_path = NULL` (вместо прямого `db.execute` без закрытия курсора).

### Fixed

#### Scheduler (`app/scheduler/__init__.py`)

- **C1 (CRITICAL)**: `_send_album_group` при отсутствии файла поднимал
  `RuntimeError` вместо `FileMissingError` → альбомы с удалёнными файлами
  никогда не перекачивались через worker и помечались `failed`. Теперь логика
  зеркалит `_send_one_post`: HTTP `source_url` → `FileMissingError` (перекачка),
  локальный/без URL → `RuntimeError`.
- **C2 (CRITICAL)**: fallback альбома «too big for a photo» (посты по одному)
  мог оставить уже доставленные посты в `pending` → дубли при retry. Теперь
  каждый успешно отправленный пост помечается `sent` (через `bulk_mark_sent`)
  по мере отправки, а файлы чистятся.
- **C3 (CRITICAL)**: при перекачке одного поста альбома остальные отправлялись
  без него (split). Теперь scheduler проверяет `count_album_unready` — если
  часть постов альбома ещё в `new`/`downloading`, тик пропускается до полной
  готовности.
- **H4 (HIGH)**: 429 Too Many Requests не учитывался в `max_send_attempts` →
  при persistent flood control пост мог крутиться бесконечно без `failed`.
  Теперь 429 считается попыткой; на последней попытке — `mark_failed` +
  уведомление.
- **H5 (HIGH)**: `_check_queue_notifications` (almost_empty / finished) не
  вызывался после отправки альбома → уведомления не уходили. Теперь вызывается.
- **H7 (HIGH)**: временные пережатые `_tg.jpg` файлы (от
  `compress_photo_for_telegram`) не удалялись → копились в `data/downloads/`.
  Теперь чистятся в `finally` (одиночный пост и альбом).
- **M1**: fallback альбома проверял только `"too big for a photo"`, пропуская
  `PHOTO_INVALID_DIMENSIONS`. Заменено на `_is_photo_too_big_error(e)`.
- **M2**: `_redownload_post` делал прямой `db.execute` без `cur.close()`
  (утечка курсора + обход репозитория). Заменено на `posts.clear_file_path`.
- **L4**: `asyncio.get_event_loop().time()` (deprecated в 3.12) → `time.monotonic()`.

#### Archive (`app/services/archive.py`)

- **H1/H2 (HIGH)**: `_throttled_archive_send` проглатывал все исключения и
  возвращал `None` → photo→document fallback был мёртвым кодом, а `archive_post`
  ложно возвращал `True` и логировал «архивирован» при провале. Теперь
  исключения пробрасываются (caller решает: `archive_post` — fallback на
  document, `archive_album` — `return False`).
- **H3 (HIGH)**: `_do_album_archive_check` помечал группу архивированной ДО
  вызова `archive_album`; при провале (возвращает `False` без исключения) группа
  оставалась в `_archived_groups` навсегда и повторная архивация не выполнялась.
  Теперь marker ставится только при успехе.
- **M5**: `TelegramRetryAfter.retry_after` теперь валидируется (может быть
  `None`/нечисловым) и ограничивается 60 сек.

#### task_queue (`app/repositories/task_queue.py`, `app/db/`)

- **H6 (HIGH)**: stale-проверка использовала `created_at` (время постановки в
  очередь), а не момент захвата задачей worker'ом → задача, лежавшая в очереди
  > `WORKER_STALE_TIMEOUT_MINUTES` до того как worker её взял, объявлялась
  stale сразу после захвата и сбрасывалась (race с активной обработкой).
  Добавлена колонка `started_at` (миграция в `init_db`), `take_next` фиксирует
  её, stale-проверки (`count_stale_in_progress`, `reset_stale_in_progress`)
  используют `COALESCE(started_at, created_at)`.

#### Web (`app/web/router.py`)

- **C2 (security)**: `POST /web/local_files/roots/add` принимал любую
  существующую директорию как корень файлового браузера → любой
  авторизованный пользователь мог добавить `/etc`, `/mnt/c` (весь диск) и
  читать `/etc/passwd`, ключи Docker и т.п. Добавлен denylist
  (`_is_sensitive_root`): блокирует корни дисков целиком и системные
  директории (любой глубины); конкретные папки пользователя разрешены.

### Changed

- Версия синхронизирована: `app/main.py` (FastAPI `version`), `base.html`
  (footer sidebar), `pyproject.toml` → `0.4.63`. Раньше в `main.py` застряла
  `0.4.55`, в pyproject — `0.2.0`.
- Cache-busting статики: `?v=20261119.1` (css/js).

### Migration notes

- Колонка `task_queue.started_at` добавляется автоматически при старте
  (через существующий механизм миграций `init_db` → `_run_migrations`).
  Ручных действий не требуется. Старые in_progress-задачи без `started_at`
  обрабатываются через `COALESCE(started_at, created_at)`.
