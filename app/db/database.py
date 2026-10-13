"""Асинхронный доступ к SQLite через aiosqlite (ТЗ §5, §3.1).

Архитектура:
- Одна разделяемая aiosqlite.Connection на весь процесс (SQLite single-writer).
- WAL-режим + foreign_keys=ON (включаются при init_db).
- Схема — app/db/schema.sql (идемпотентна, выполняется при каждом старте).
- Репозитории получают соединение через get_db() либо принимают его аргументом.
"""
from __future__ import annotations

import logging
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
    except aiosqlite.OperationalError as e:
        # Типичный случай: перенос старой БД (например, копия папки со старой
        # машины) — executescript падает на CREATE INDEX по колонке, которой в
        # старой таблице ещё нет ("no such column: media_group_id").
        # SQLite не откатывает уже выполненную часть скрипта, поэтому сначала
        # накатываем миграции (ALTER TABLE ... ADD COLUMN), потом повторяем схему.
        log.warning("Схема применена частично (%s) — запускаю миграции и повтор схемы", e)
        await _run_migrations(_db)
        try:
            await _db.executescript(schema_sql)
        except aiosqlite.OperationalError as e2:
            raise RuntimeError(
                f"Не удалось применить схему БД даже после миграций: {e2}. "
                "Проверьте, что версия schema.sql соответствует вашей базе."
            ) from e2
    else:
        # Обычный путь: схема легла целиком — миграции добирают колонки для
        # таблиц, которые IF NOT EXISTS не тронул (существующие старые БД).
        await _run_migrations(_db)
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
    ]
    for table, column, ddl in migrations:
        if not await _column_exists(db, table, column):
            try:
                await db.execute(ddl)
                log.info("Миграция: добавлена колонка %s.%s", table, column)
            except aiosqlite.OperationalError as e:
                # Кто-то добавил параллельно — не критично
                log.debug("Колонка %s.%s уже существует: %s", table, column, e)


async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    return any(row[1] == column for row in rows)


async def close_db() -> None:
    """Закрывает соединение (вызывается при shutdown)."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
        log.info("Соединение с БД закрыто")


def get_db() -> aiosqlite.Connection:
    """Возвращает singleton-соединение. Бросает RuntimeError, если init_db не вызван."""
    if _db is None:
        raise RuntimeError("БД не инициализирована — вызовите await init_db() при старте приложения")
    return _db


async def bootstrap_admin(tg_id: int, username: str | None) -> bool:
    """Создаёт первого admin из .env (ADMIN_TG_ID), если его ещё нет в БД.

    Решает проблему «курицы и яйца»: команда /add_user требует admin-прав,
    но admin появляется только через БД. Возвращает True, если пользователь
    был добавлен (False — если уже существовал).
    """
    db = get_db()
    cur = await db.execute("SELECT id, role FROM users WHERE tg_id = ?", (tg_id,))
    row = await cur.fetchone()
    await cur.close()
    if row is not None:
        # Уже есть — повышаем до admin, если вдруг был moderator, и активируем
        if row["role"] != "admin":
            await db.execute(
                "UPDATE users SET role = 'admin', is_active = 1, username = COALESCE(?, username) WHERE id = ?",
                (username, row["id"]),
            )
            await db.commit()
            log.info("Существующий пользователь tg_id=%s повышен до admin", tg_id)
        return False

    await db.execute(
        "INSERT INTO users (tg_id, username, role, is_active) VALUES (?, ?, 'admin', 1)",
        (tg_id, username),
    )
    await db.commit()
    log.info("Загружен admin из .env: tg_id=%s username=%s", tg_id, username or "<нет>")
    return True
