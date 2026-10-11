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
    cur = await db.execute(
        "INSERT INTO duplicate_hashes (post_id, hash_value, hash_type) VALUES (?, ?, ?)",
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
    cur = await db.execute(
        "SELECT post_id, hash_value FROM duplicate_hashes WHERE hash_type = 'phash' AND hash_value != ?",
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
