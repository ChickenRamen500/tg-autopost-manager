"""Асинхронный доступ к SQLite через aiosqlite (ТЗ §5, §3.1).

Архитектура:
- Одна разделяемая aiosqlite.Connection на весь процесс (SQLite single-writer).
- WAL-режим + foreign_keys=ON (включаются при init_db).
- Схема — app/db/schema.sql (идемпотентна, выполняется при каждом старте).
- Репозитории получают соединение через get_db() либо принимают его аргументом.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.config.settings import settings

log = logging.getLogger("db")

# Корень проекта: .../app/db/database.py -> project root
BASE_DIR = Path(__file__).resolve().parent.parent.parent
SCHEMA_PATH = BASE_DIR / "app" / "db" / "schema.sql"

_db: aiosqlite.Connection | None = None


async def init_db() -> aiosqlite.Connection:
    """Инициализирует БД: создаёт папку, открывает соединение, применяет схему.

    Безопасно вызывать многократно (CREATE TABLE IF NOT EXISTS).
    Возвращает singleton-соединение для использования репозиториями.
    """
    global _db
    if _db is not None:
        return _db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    _db = await aiosqlite.connect(str(settings.db_path))
    _db.row_factory = aiosqlite.Row

    # busy_timeout: ждать блокировки до 5 сек вместо мгновенного "database is locked"
    # (параллельный доступ CLI-скриптов к БД, которую держит uvicorn-процесс бота)
    await _db.execute("PRAGMA busy_timeout=5000")

    # WAL — параллельное чтение, быстрее запись. На Windows + Docker volume
    # файл -shm может быть залочен другим процессом: тогда PRAGMA падает
    # "unable to open database file". Это не критично — fallback на default journal.
    try:
        await _db.execute("PRAGMA journal_mode=WAL")
    except aiosqlite.OperationalError as e:
        log.warning("Не удалось включить WAL-режим (%s) — работаю на default journal (DELETE).", e)

    await _db.execute("PRAGMA foreign_keys=ON")
    await _db.execute("PRAGMA synchronous=NORMAL")  # баланс надёжности/скорости (§3.1 слабое железо)

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        await _db.executescript(schema_sql)
    except (aiosqlite.OperationalError, aiosqlite.IntegrityError) as e:
        # Типичный случай #1: перенос старой БД (например, копия папки со старой
        # машины) — executescript падает на CREATE INDEX по колонке, которой в
        # старой таблице ещё нет ("no such column: media_group_id") — OperationalError.
        #
        # Типичный случай #2: в существующей БД есть дубликаты posts.file_unique_id
        # (что было возможно до CR-H-V1 фикса — TOCTOU race condition в receive_media).
        # executescript падает на `CREATE UNIQUE INDEX idx_posts_file_unique_unique`
        # с IntegrityError: "UNIQUE constraint failed: posts.file_unique_id".
        #
        # SQLite не откатывает уже выполненную часть скрипта, поэтому сначала
        # накатываем миграции (ALTER TABLE + очистка дубликатов), потом повтор схемы.
        log.warning("Схема применена частично (%s) — запускаю миграции и повтор схемы", e)
        await _run_migrations(_db)
        # Очистка дубликатов file_unique_id — должна выполняться ПОСЛЕ _run_migrations
        # (который добавляет колонку file_unique_id в старые БД), но ДО повторного
        # executescript (который создаёт UNIQUE index).
        await _cleanup_duplicate_file_unique_ids(_db)
        # Миграция CHECK constraint на file_type — для БД, созданных между PR #40 и PR #48
        # (старый CHECK без audio/voice). Иначе INSERT с file_type='audio' падает
        # с IntegrityError: CHECK constraint failed: file_type IN ('photo','video','gif','document')
        await _migrate_posts_file_type_check(_db)
        try:
            await _db.executescript(schema_sql)
        except (aiosqlite.OperationalError, aiosqlite.IntegrityError) as e2:
            raise RuntimeError(
                f"Не удалось применить схему БД даже после миграций и очистки дубликатов: {e2}. "
                "Проверьте, что версия schema.sql соответствует вашей базе."
            ) from e2
    else:
        # Обычный путь: схема легла целиком — миграции добирают колонки для
        # таблиц, которые IF NOT EXISTS не тронул (существующие старые БД).
        await _run_migrations(_db)
        # Миграция CHECK constraint на file_type — даже если executescript прошёл
        # успешно (CREATE TABLE IF NOT EXISTS пропускает существующую таблицу),
        # старый CHECK мог остаться навязанным — проверяем и мигрируем если надо.
        await _migrate_posts_file_type_check(_db)
    await _db.commit()

    log.info("БД инициализирована: %s (схема применена)", settings.db_path)
    return _db


async def _run_migrations(db: aiosqlite.Connection) -> None:
    """Добавляет колонки в существующие таблицы (для БД, созданных до этого фикса).

    SQLite не поддерживает ALTER TABLE ... ADD COLUMN IF NOT EXISTS, поэтому
    проверяем наличие колонки через PRAGMA table_info и добавляем только если нет.
    """
    # Карта: (таблица, колонка, DDL-добавление)
    migrations = [
        ("posts", "media_group_id", "ALTER TABLE posts ADD COLUMN media_group_id TEXT"),
        ("posts", "file_unique_id", "ALTER TABLE posts ADD COLUMN file_unique_id TEXT"),
        ("posts", "tg_file_id", "ALTER TABLE posts ADD COLUMN tg_file_id TEXT"),
        ("posts", "file_size", "ALTER TABLE posts ADD COLUMN file_size INTEGER"),
        ("posts", "mime_type", "ALTER TABLE posts ADD COLUMN mime_type TEXT"),
        ("posts", "was_processed", "ALTER TABLE posts ADD COLUMN was_processed INTEGER NOT NULL DEFAULT 0"),
        ("posts", "send_attempts", "ALTER TABLE posts ADD COLUMN send_attempts INTEGER NOT NULL DEFAULT 0"),
        ("posts", "last_error", "ALTER TABLE posts ADD COLUMN last_error TEXT"),
        ("queues", "group_albums", "ALTER TABLE queues ADD COLUMN group_albums INTEGER NOT NULL DEFAULT 0"),
        ("queues", "crop_video", "ALTER TABLE queues ADD COLUMN crop_video INTEGER NOT NULL DEFAULT 0"),
        ("queues", "signature_template", "ALTER TABLE queues ADD COLUMN signature_template TEXT"),
        ("queues", "shuffle_enabled", "ALTER TABLE queues ADD COLUMN shuffle_enabled INTEGER NOT NULL DEFAULT 1"),
        # Блок B: время работы очереди (start_at, end_at, daily_window, end_strategy)
        ("queues", "start_at", "ALTER TABLE queues ADD COLUMN start_at DATETIME"),
        ("queues", "end_at", "ALTER TABLE queues ADD COLUMN end_at DATETIME"),
        ("queues", "daily_window_start", "ALTER TABLE queues ADD COLUMN daily_window_start TEXT"),
        ("queues", "daily_window_end", "ALTER TABLE queues ADD COLUMN daily_window_end TEXT"),
        # end_strategy имеет DEFAULT + CHECK — ALTER TABLE ADD COLUMN с CHECK поддерживается SQLite ≥3.35
        ("queues", "end_strategy", (
            "ALTER TABLE queues ADD COLUMN end_strategy TEXT NOT NULL DEFAULT 'continue_next_day' "
            "CHECK(end_strategy IN ('continue_next_day', 'continue_after_end', 'move_to_inbox', 'delete'))"
        )),
        ("queues", "strip_caption", "ALTER TABLE queues ADD COLUMN strip_caption INTEGER NOT NULL DEFAULT 0"),
        # Фаза 3: подписки Gelbooru — notify_interval_minutes + last_notified_at
        ("tag_subscriptions", "notify_interval_minutes", "ALTER TABLE tag_subscriptions ADD COLUMN notify_interval_minutes INTEGER NOT NULL DEFAULT 60 CHECK(notify_interval_minutes >= 0)"),
        ("tag_subscriptions", "last_notified_at", "ALTER TABLE tag_subscriptions ADD COLUMN last_notified_at DATETIME"),
        ("tag_subscriptions", "default_queue_id", "ALTER TABLE tag_subscriptions ADD COLUMN default_queue_id INTEGER"),
    ]
    for table, column, ddl in migrations:
        if not await _column_exists(db, table, column):
            try:
                await db.execute(ddl)
                log.info("Миграция: добавлена колонка %s.%s", table, column)
            except aiosqlite.OperationalError as e:
                # Кто-то добавил параллельно — не критично
                log.debug("Колонка %s.%s уже существует: %s", table, column, e)


async def _cleanup_duplicate_file_unique_ids(db: aiosqlite.Connection) -> int:
    """Очищает дубликаты posts.file_unique_id перед созданием UNIQUE index.

    Контекст: CR-H-V1 фикс добавил `CREATE UNIQUE INDEX idx_posts_file_unique_unique
    ON posts(file_unique_id)` в schema.sql, чтобы TOCTOU-дедупликация через
    `INSERT ... ON CONFLICT(file_unique_id) DO NOTHING` работала. Но в существующих
    БД, созданных ДО этого фикса, уже могут быть дубликаты — race condition в
    receive_media позволял создать несколько постов с одним file_unique_id
    (два одновременных сообщения с одним файлом).

    Создание UNIQUE index на колонке с дубликатами падает:
        sqlite3.IntegrityError: UNIQUE constraint failed: posts.file_unique_id

    Эта функция:
      1. Ищет группы дубликатов (file_unique_id с COUNT(*) > 1, IS NOT NULL)
      2. В каждой группе оставляет один пост (MIN(id) — самый старый, скорее
         всего оригинал) с file_unique_id НЕтронутым
      3. У остальных ОБНУЛЯЕТ file_unique_id (NULL в UNIQUE-индексе SQLite
         считаются различными — конфликт снимается) и помечает status='possible_duplicate'
         (кроме уже отправленных — status='sent' сохраняется, чтобы не исказить отчётность)
      4. Физически НЕ удаляет посты — они видны в UI, могут быть обработаны вручную

    Возвращает количество затронутых строк. Безопасно вызывать многократно —
    если дубликатов нет, ничего не делает (идемпотентно).
    """
    # Сначала проверяем что колонка существует (на случай очень старой БД без неё)
    if not await _column_exists(db, "posts", "file_unique_id"):
        return 0  # колонки нет — нечего чистить

    # Найти все группы дубликатов (file_unique_id, count, keep_id)
    cur = await db.execute(
        """
        SELECT file_unique_id, COUNT(*) AS cnt, MIN(id) AS keep_id
        FROM posts
        WHERE file_unique_id IS NOT NULL
        GROUP BY file_unique_id
        HAVING COUNT(*) > 1
        """
    )
    duplicates = await cur.fetchall()
    await cur.close()

    if not duplicates:
        return 0  # дубликатов нет

    total_nulled = 0
    for row in duplicates:
        fuid = row["file_unique_id"]
        keep_id = row["keep_id"]
        # Оставляем пост с MIN(id) (самый старый — скорее всего оригинал),
        # у остальных ОБНУЛЯЕМ file_unique_id (NOT NULL → NULL).
        # Это решает конфликт UNIQUE-констрейнта: остаётся одна строка с данным
        # file_unique_id, остальные имеют NULL (NULL не нарушает UNIQUE в SQLite).
        # Пост не удаляется физически — сохраняется в БД, виден в UI, может быть
        # обработан вручную. Помечаем его как 'possible_duplicate' для видимости.
        cur = await db.execute(
            """
            UPDATE posts
            SET file_unique_id = NULL,
                status = CASE WHEN status = 'sent' THEN status ELSE 'possible_duplicate' END
            WHERE file_unique_id = ?
              AND id != ?
            """,
            (fuid, keep_id),
        )
        nulled = cur.rowcount
        total_nulled += nulled
        await cur.close()
        log.info(
            "Дубликаты file_unique_id=%s: оставлен пост #%s, обнулено file_unique_id у %d дубликатов",
            fuid, keep_id, nulled,
        )

    if total_nulled > 0:
        await db.commit()
        log.info(
            "Очистка дубликатов file_unique_id: обнулено %d постов "
            "(UNIQUE index теперь применится безопасно)",
            total_nulled,
        )
    return total_nulled


async def _migrate_posts_file_type_check(db: aiosqlite.Connection) -> bool:
    """Пересоздаёт таблицу posts если CHECK constraint на file_type не включает audio/voice.

    Контекст: PR #40 добавил CHECK constraint `file_type IN ('photo','video','gif','document')`
    в schema.sql. PR #48 расширил его до `('photo','video','gif','document','audio','voice')`.
    Но CREATE TABLE IF NOT EXISTS в executescript НЕ обновляет существующую таблицу —
    старый CHECK остаётся навязанным, и INSERT с file_type='audio' падает:
        sqlite3.IntegrityError: CHECK constraint failed: file_type IN ('photo','video','gif','document')

    Решение: проверить CHECK через sqlite_master.sql — если не содержит 'audio'/'voice',
    пересоздать таблицу через CREATE+INSERT+DROP+RENAME.

    Безопасность:
    - PRAGMA foreign_keys=OFF на время операции — иначе DROP TABLE триггерит CASCADE
      удаления из duplicate_hashes и task_queue (FK ON DELETE CASCADE)
    - Все данные копируются INSERT INTO posts_new SELECT * FROM posts
    - Все индексы пересоздаются
    - PRAGMA foreign_keys=ON в finally — гарантирует включение обратно

    Возвращает True если миграция выполнена, False если не нужна.
    Идемпотентна: если CHECK уже правильный — ничего не делает.
    """
    # Проверяем нужна ли миграция — читаем CREATE TABLE statement
    cur = await db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='posts'")
    row = await cur.fetchone()
    await cur.close()

    if not row or not row[0]:
        return False  # таблицы нет — init_db создаст с правильным CHECK

    table_sql = row[0]
    # Если в CREATE TABLE уже есть 'audio' И 'voice' в CHECK — миграция не нужна
    if "'audio'" in table_sql and "'voice'" in table_sql:
        return False  # CHECK уже правильный

    log.warning(
        "Миграция: таблица posts имеет старый CHECK на file_type (без audio/voice) — "
        "пересоздаём таблицу для поддержки mp3/voice сообщений"
    )

    # 1. Временно отключаем foreign_keys — иначе DROP TABLEposts триггерит
    #    ON DELETE CASCADE из duplicate_hashes и task_queue → потеря данных
    await db.execute("PRAGMA foreign_keys=OFF")

    try:
        # 2. Создаём новую таблицу с правильным CHECK (копия схемы из schema.sql)
        await db.execute("""
            CREATE TABLE posts_new (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id           INTEGER,
                source_type        TEXT NOT NULL,
                source_id          TEXT,
                source_url         TEXT,
                source_channel     TEXT,
                file_path          TEXT,
                file_type          TEXT NOT NULL CHECK(file_type IN ('photo', 'video', 'gif', 'document', 'audio', 'voice')),
                tg_file_id         TEXT,
                file_unique_id     TEXT,
                file_size          INTEGER,
                mime_type          TEXT,
                was_processed      INTEGER NOT NULL DEFAULT 0,
                original_caption   TEXT,
                processed_caption  TEXT,
                tags               TEXT,
                status             TEXT NOT NULL CHECK(status IN ('new', 'downloading', 'pending', 'sent',
                                                                   'failed', 'duplicate', 'possible_duplicate')),
                phash              TEXT,
                media_group_id     TEXT,
                scheduled_time     DATETIME,
                sent_at            DATETIME,
                send_attempts      INTEGER NOT NULL DEFAULT 0,
                last_error         TEXT,
                created_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (queue_id) REFERENCES queues(id) ON DELETE SET NULL
            )
        """)

        # 3. Копируем все данные (COLLATE-agnostic, просто COPY)
        # ВАЖНО: column order в posts_new должен совпадать с posts (он совпадает — мы
        # создавали новую таблицу с теми же колонками в том же порядке).
        await db.execute("INSERT INTO posts_new SELECT * FROM posts")

        # 4. Дропаем старую таблицу
        await db.execute("DROP TABLE posts")

        # 5. Переименовываем новую в posts
        await db.execute("ALTER TABLE posts_new RENAME TO posts")

        # 6. Пересоздаём все индексы (DROP TABLE их удалил)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_queue_status ON posts(queue_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_file_unique  ON posts(file_unique_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_source       ON posts(source_type, source_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_media_group  ON posts(media_group_id)")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_file_unique_unique ON posts(file_unique_id)")

        await db.commit()
        log.info(
            "Миграция posts завершена: CHECK на file_type обновлён — audio/voice теперь валидны. "
            "Все данные и индексы сохранены."
        )
        return True
    finally:
        # PRAGMA foreign_keys=ON в finally — гарантированно включаем обратно
        await db.execute("PRAGMA foreign_keys=ON")


async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    # Защита от SQL-инъекции через PRAGMA table_info(...) — f-string с именем таблицы.
    # Идентификаторы в SQLite: [A-Za-z_][A-Za-z0-9_]* — проверяем isidentifier().
    assert table.isidentifier() and column.isidentifier(), f"bad identifier: {table!r}.{column!r}"
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    return any(row[1] == column for row in rows)


async def close_db() -> None:
    """Закрывает соединение (вызывается при shutdown).

    Обёрнуто в try/finally: даже если _db.close() упадёт (например, соединение
    уже разрушено), singleton-ссылку _db гарантированно обнуляем — иначе
    последующие init_db() будут думать, что соединение живо.
    """
    global _db
    if _db is not None:
        try:
            await _db.close()
        finally:
            _db = None
            log.info("Соединение с БД закрыто")


def get_db() -> aiosqlite.Connection:
    """Возвращает singleton-соединение. Бросает RuntimeError, если init_db не вызван."""
    if _db is None:
        raise RuntimeError("БД не инициализирована — вызовите await init_db() при старте приложения")
    return _db


@asynccontextmanager
async def transaction():
    """Контекст для многошаговых операций: BEGIN IMMEDIATE ... COMMIT/ROLLBACK.

    Для атомарной записи в несколько таблиц (например, set_phash_atomic:
    posts.phash + duplicate_hashes). BEGIN IMMEDIATE захватывает write-lock
    сразу — параллельные reader'ы увидят согласованное состояние.

    Пример:
        async with transaction() as db:
            await db.execute("UPDATE posts SET ...")
            await db.execute("INSERT INTO duplicate_hashes ...")
    """
    db = get_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def bootstrap_admin(tg_id: int, username: str | None) -> bool:
    """Создаёт первого admin из .env (ADMIN_TG_ID), если его ещё нет в БД.

    Решает проблему «курицы и яйца»: команда /add_user требует admin-прав,
    но admin появляется только через БД. Возвращает True, если пользователь
    был добавлен (False — если уже существовал).

    Атомарная реализация: один INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING
    вместо SELECT-then-INSERT/UPDATE — закрывает race condition между
    concurrent /start-запросами (два одновременных запроса могли дважды
    вставить admin или потерять UPDATE — CR-H-R2).
    """
    db = get_db()
    # RETURNING id вернёт строку, только если данные реально изменились
    # (SQLite RETURNING отражает фактическое состояние после INSERT/UPDATE).
    cur = await db.execute(
        """
        INSERT INTO users (tg_id, username, role, is_active)
        VALUES (?, ?, 'admin', 1)
        ON CONFLICT(tg_id) DO UPDATE SET
            role = 'admin',
            is_active = 1,
            username = COALESCE(excluded.username, users.username)
        WHERE users.role != 'admin' OR users.is_active = 0
        RETURNING id
        """,
        (tg_id, username),
    )
    row = await cur.fetchone()
    await cur.close()
    await db.commit()
    if row is None:
        # Пользователь уже admin и активен — ничего не менялось
        return False
    log.info("Загружен admin из .env: tg_id=%s username=%s", tg_id, username or "<нет>")
    return True
