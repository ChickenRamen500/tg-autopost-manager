"""Репозиторий хэшей дубликатов (ТЗ §5.1 duplicate_hashes, §2.4.3).

Двухуровневый поиск дубликатов:
- точный: по file_unique_id (Telegram) или source_id — реализован в posts.py
- визуальный: по pHash — хранится здесь + в posts.phash
"""
from __future__ import annotations

from typing import Any

from app.db import get_db


async def add_hash(post_id: int, hash_value: str, hash_type: str) -> int:
    if hash_type not in ("md5", "phash"):
        raise ValueError(f"Недопустимый hash_type: {hash_type!r}")
    db = get_db()
    # INSERT OR IGNORE: защита от повторной вставки одинаковых хэшей для
    # одного post_id+hash_type (благодаря UNIQUE(post_id, hash_type) в schema.sql).
    # Возвращает lastrowid (0 при игноре). Без этого — IntegrityError на повторе.
    cur = await db.execute(
        "INSERT OR IGNORE INTO duplicate_hashes (post_id, hash_value, hash_type) VALUES (?, ?, ?)",
        (post_id, hash_value, hash_type),
    )
    await db.commit()
    rid = cur.lastrowid
    await cur.close()
    return rid


async def find_by_hash(hash_value: str, hash_type: str) -> list[dict[str, Any]]:
    """Поиск постов с таким же хэшем (точный дубликат по хэшу)."""
    db = get_db()
    cur = await db.execute(
        """
        SELECT dh.post_id, p.source_type, p.file_type, p.created_at
        FROM duplicate_hashes dh
        JOIN posts p ON p.id = dh.post_id
        WHERE dh.hash_value = ? AND dh.hash_type = ?
        ORDER BY dh.created_at DESC
        """,
        (hash_value, hash_type),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def find_phash_near(phash: str, max_distance: int = 5) -> list[dict[str, Any]]:
    """Визуальный поиск дубликата по pHash с расстоянием Хэмминга ≤ max_distance.

    SQLite не имеет встроенной popcount/Hamming, поэтому фильтруем в Python
    (объём pHash-ов небольшой — быстродействие достаточно для слабого железа).
    """
    db = get_db()
    # LIMIT 10000: защита от OOM на больших БД (CR-M-R5). Hamming-distance
    # фильтрация в Python имеет O(N) по памяти и CPU — для >10k записей
    # нужен SQL-side popcount (встроенной нет, см. INFO в code review).
    cur = await db.execute(
        "SELECT post_id, hash_value FROM duplicate_hashes "
        "WHERE hash_type = 'phash' AND hash_value != ? LIMIT 10000",
        (phash,),
    )
    rows = await cur.fetchall()
    await cur.close()
    matches: list[dict[str, Any]] = []
    for r in rows:
        dist = _hamming_hex(phash, r["hash_value"])
        if 0 <= dist <= max_distance:
            matches.append({"post_id": r["post_id"], "hash_value": r["hash_value"], "distance": dist})
    matches.sort(key=lambda m: m["distance"])
    return matches


def _hamming_hex(a: str, b: str) -> int:
    """Расстояние Хэмминга между двумя hex-строками равной длины."""
    if not a or not b or len(a) != len(b):
        return -1
    try:
        x = int(a, 16) ^ int(b, 16)
    except ValueError:
        return -1
    return x.bit_count()


async def set_phash_atomic(post_id: int, phash: str) -> None:
    """Атомарно устанавливает pHash в posts И duplicate_hashes в одной транзакции.

    Решает CR-H-R4: раньше set_phash (в posts) + add_hash (в duplicates)
    были двумя отдельными commit'ами — crash между ними оставлял
    рассинхрон: posts.phash обновлён, а duplicate_hashes пуст (или наоборот).
    Теперь одна BEGIN IMMEDIATE ... COMMIT/ROLLBACK.

    Idempotent: при повторном вызове для того же post_id старая phash-запись
    в duplicate_hashes заменяется новой (DELETE + INSERT), а не дублируется.
    """
    db = get_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute("UPDATE posts SET phash = ? WHERE id = ?", (phash, post_id))
        # Удаляем старую запись phash для этого поста (для idempotency)
        await db.execute(
            "DELETE FROM duplicate_hashes WHERE post_id = ? AND hash_type = 'phash'",
            (post_id,),
        )
        await db.execute(
            "INSERT INTO duplicate_hashes (post_id, hash_value, hash_type) VALUES (?, ?, 'phash')",
            (post_id, phash),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
