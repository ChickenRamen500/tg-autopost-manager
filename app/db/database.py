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
    # WAL — параллельное чтение, быстрее запись; foreign_keys — каскадная целостность
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA foreign_keys=ON")
    await _db.execute("PRAGMA synchronous=NORMAL")  # баланс надёжности/скорости (§3.1 слабое железо)

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    await _db.executescript(schema_sql)
    await _db.commit()

    log.info("БД инициализирована: %s (схема применена)", settings.db_path)
    return _db


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
