"""Отдельная БД метаданных тегов Gelbooru (мигрировано из gbooru_tg/tags_db.py, ТЗ §10.1).

Хранится в data/tags.db — отдельно от основной bot.db, чтобы сброс основной БД
не терял информацию о типах тегов.

Адаптация (ТЗ §10.3):
- sync sqlite3 -> aiosqlite (стек проекта асинхронный)
- путь -> data/tags.db (через settings.data_dir)

Типы тегов из Gelbooru API (поле "type" в ответах s=tag):
    0 = general
    1 = artist
    3 = copyright
    4 = character
    5 = circle / meta
    6 = meta (deprecated/other)

Маппим в 5 отображаемых категорий: artist, character, copyright, meta, general.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from app.config.settings import settings

logger = logging.getLogger(__name__)

# Gelbooru type -> отображаемая категория
TYPE_MAP: dict[int, str] = {
    0: "general",
    1: "artist",
    3: "copyright",
    4: "character",
    5: "meta",
    6: "meta",
}

# Порядок отображения (важные — первыми)
CATEGORY_ORDER = ["artist", "copyright", "character", "meta", "general"]

_conn: aiosqlite.Connection | None = None


def _db_path() -> Path:
    return settings.data_dir / "tags.db"


async def init_tags_db() -> None:
    """Создаёт БД тегов, если её нет. Вызывается при старте приложения."""
    global _conn
    if _conn is not None:
        return
    # Синхронный mkdir обернём в поток, чтобы не блокировать event loop.
    await asyncio.to_thread(settings.data_dir.mkdir, parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(str(_db_path()))
    _conn.row_factory = aiosqlite.Row
    # P2-16: WAL + busy_timeout для конкурентного доступа (populate_tags.py write + app read)
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.execute("PRAGMA busy_timeout=5000")
    await _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tags (
            name       TEXT PRIMARY KEY,
            type       INTEGER NOT NULL DEFAULT 0,
            count      INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
        """
    )
    await _conn.commit()
    logger.info("Tags database ready at %s", _db_path())


async def close_tags_db() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def _get_conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("tags_db не инициализирована — вызовите await init_tags_db() при старте")
    return _conn


async def upsert_tags(tags: list[dict[str, Any]]) -> int:
    """Bulk insert/update списка тегов ({name, type, count}). Возвращает число обработанных.

    Использует executemany — один round-trip вместо N INSERT-запросов.
    """
    if not tags:
        return 0
    db = _get_conn()
    rows: list[tuple[str, int, int]] = []
    for tag in tags:
        name = (tag.get("name") or "").strip()
        if not name:
            continue
        tag_type = int(tag.get("type", 0) or 0)
        count = int(tag.get("count", 0) or 0)
        rows.append((name, tag_type, count))
    if not rows:
        return 0
    try:
        await db.executemany(
            "INSERT INTO tags (name, type, count, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(name) DO UPDATE SET "
            "type = excluded.type, count = excluded.count, "
            "updated_at = datetime('now')",
            rows,
        )
        await db.commit()
        return len(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to executemany upsert %d tags: %s", len(rows), e)
        return 0


async def get_tag_types(names: list[str]) -> dict[str, int]:
    """Пакетный лукап типов тегов из локальной БД. Возвращает {name: type}."""
    if not names:
        return {}
    unique = list({n.strip() for n in names if n and n.strip()})
    if not unique:
        return {}
    db = _get_conn()
    result: dict[str, int] = {}
    chunk_size = 500  # лимит переменных SQLite ~999
    for i in range(0, len(unique), chunk_size):
        batch = unique[i : i + chunk_size]
        placeholders = ",".join("?" * len(batch))
        cur = await db.execute(f"SELECT name, type FROM tags WHERE name IN ({placeholders})", batch)
        rows = await cur.fetchall()
        await cur.close()
        for row in rows:
            result[row["name"]] = row["type"]
    return result


async def categorize_tags(names: list[str]) -> dict[str, list[str]]:
    """Разбивает теги по отображаемым категориям (использует локальную БД)."""
    categories: dict[str, list[str]] = {c: [] for c in CATEGORY_ORDER}
    if not names:
        return categories
    types = await get_tag_types(names)
    for name in names:
        name = name.strip()
        if not name:
            continue
        tag_type = types.get(name)
        category = TYPE_MAP.get(tag_type, "general") if tag_type is not None else "general"
        categories.setdefault(category, []).append(name)
    return categories


async def autocomplete(prefix: str, limit: int = 10) -> list[dict[str, Any]]:
    """Теги, начинающиеся с prefix, отсортированные по популярности. Локальный запрос, 0 API-вызовов."""
    prefix = prefix.strip().lower()
    if not prefix:
        return []
    # Экранируем специальные символы LIKE (% и _) — иначе пользовательский ввод
    # вида '%' или '_' ломает семантику поиска или матчит лишнее.
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    db = _get_conn()
    cur = await db.execute(
        "SELECT name, type, count FROM tags WHERE name LIKE ? ESCAPE '\\' ORDER BY count DESC, name ASC LIMIT ?",
        (escaped + "%", limit),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [{"name": r["name"], "type": r["type"], "count": r["count"]} for r in rows]


async def get_tags_count() -> int:
    db = _get_conn()
    cur = await db.execute("SELECT COUNT(*) AS c FROM tags")
    row = await cur.fetchone()
    await cur.close()
    return row["c"] if row else 0
