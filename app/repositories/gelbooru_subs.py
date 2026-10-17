"""Репозиторий просмотренных постов Gelbooru и глобального blacklist'а тегов.

ТЗ §2.1.4 (Фаза 3 этап 3):
  - gelbooru_seen_posts: отслеживает найденные посты по подпискам (new/sent/ignored)
  - gelbooru_blacklist_tags: глобальные исключающие теги (-tag добавляется ко всем запросам)

Жизненный цикл seen_post:
  new → sent    (админ нажал «Отправить» → пост создан в posts, worker скачает)
  new → ignored (админ нажал «Пропустить» → больше не показывается)
"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_db


def _tags_to_json(tags: list[str] | str | None) -> str | None:
    """Gelbooru возвращает tags как строку через пробел; мы храним JSON-массивом."""
    if tags is None:
        return None
    if isinstance(tags, str):
        # из gelbooru — строка через пробел
        tags_list = [t for t in tags.split() if t]
    else:
        tags_list = list(tags)
    return json.dumps(tags_list, ensure_ascii=False) if tags_list else None


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ===========================================================================
# gelbooru_seen_posts CRUD
# ===========================================================================


async def add_seen_post(
    subscription_id: int,
    post_id: int,
    *,
    file_unique_id: str | None = None,
    source_url: str | None = None,
    preview_url: str | None = None,
    tags: list[str] | str | None = None,
    rating: str | None = None,
) -> int | None:
    """Добавляет пост в seen_posts со статусом 'new'.

    Если пост уже есть для этой подписки (UNIQUE(subscription_id, post_id)) —
    возвращаем None (идемпотентно, не дублируем). Иначе возвращаем id новой строки.

    file_unique_id — md5 из gelbooru (поле 'md5' в ответе API). Используется для
    кросс-подписочной дедупликации (один пост может быть найден по разным тегам).
    """
    db = get_db()
    cur = await db.execute(
        """
        INSERT OR IGNORE INTO gelbooru_seen_posts
            (subscription_id, post_id, file_unique_id, source_url, preview_url, tags, rating, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'new')
        """,
        (subscription_id, post_id, file_unique_id, source_url, preview_url, _tags_to_json(tags), rating),
    )
    await db.commit()
    # cur.rowcount: 1 = вставлено, 0 = IGNORE (конфликт UNIQUE(subscription_id, post_id))
    inserted = cur.rowcount > 0
    rid = cur.lastrowid
    await cur.close()
    # Возвращаем id новой строки, либо None если был конфликт (уже существует)
    return rid if inserted else None


async def is_post_seen(subscription_id: int, post_id: int) -> bool:
    """True если пост уже есть в seen_posts для этой подписки (любой статус)."""
    db = get_db()
    cur = await db.execute(
        "SELECT 1 FROM gelbooru_seen_posts WHERE subscription_id = ? AND post_id = ?",
        (subscription_id, post_id),
    )
    row = await cur.fetchone()
    await cur.close()
    return row is not None


async def is_file_seen_anywhere(file_unique_id: str) -> bool:
    """True если файл (по md5/file_unique_id) уже найден в ЛЮБОЙ подписке.

    Кросс-подписочная дедупликация: один пост gelbooru может попасть в несколько
    подписок (пересечение тегов). Чтобы не показывать его дважды, проверяем md5.
    """
    if not file_unique_id:
        return False
    db = get_db()
    cur = await db.execute(
        "SELECT 1 FROM gelbooru_seen_posts WHERE file_unique_id = ? LIMIT 1",
        (file_unique_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return row is not None


async def list_new_posts(subscription_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Список новых (status='new') постов подписки — для UI карточек [Назад/Отправить/Пропустить/Далее]."""
    db = get_db()
    cur = await db.execute(
        "SELECT * FROM gelbooru_seen_posts WHERE subscription_id = ? AND status = 'new' "
        "ORDER BY created_at ASC, id ASC LIMIT ?",
        (subscription_id, limit),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_seen(r) for r in rows]


async def list_new_posts_all(limit: int = 100) -> list[dict[str, Any]]:
    """Все новые посты по всем подпискам (для общего списка «новые посты Gelbooru»)."""
    db = get_db()
    cur = await db.execute(
        "SELECT * FROM gelbooru_seen_posts WHERE status = 'new' "
        "ORDER BY created_at ASC, id ASC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_seen(r) for r in rows]


async def count_new_posts(subscription_id: int) -> int:
    """Количество новых постов подписки (для уведомления «N новых постов»)."""
    db = get_db()
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM gelbooru_seen_posts WHERE subscription_id = ? AND status = 'new'",
        (subscription_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return row["c"] if row else 0


async def count_new_posts_all() -> int:
    """Всего новых постов по всем подпискам."""
    db = get_db()
    cur = await db.execute("SELECT COUNT(*) AS c FROM gelbooru_seen_posts WHERE status = 'new'")
    row = await cur.fetchone()
    await cur.close()
    return row["c"] if row else 0


async def get_seen_post(seen_id: int) -> dict[str, Any] | None:
    """Возвращает запись seen_post по id (для UI карточки)."""
    db = get_db()
    cur = await db.execute("SELECT * FROM gelbooru_seen_posts WHERE id = ?", (seen_id,))
    row = await cur.fetchone()
    await cur.close()
    return _row_to_seen(row) if row else None


async def mark_sent(seen_id: int, post_pk: int) -> bool:
    """Помечает seen_post как 'sent' и связывает с созданным постом в нашей БД.

    Вызывается после того как админ нажал «Отправить» → создан posts-пост →
    worker скачает и отправит в очередь.
    """
    db = get_db()
    cur = await db.execute(
        "UPDATE gelbooru_seen_posts SET status = 'sent', post_pk = ?, reviewed_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND status = 'new'",
        (post_pk, seen_id),
    )
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def mark_ignored(seen_id: int) -> bool:
    """Помечает seen_post как 'ignored' (админ нажал «Пропустить»)."""
    db = get_db()
    cur = await db.execute(
        "UPDATE gelbooru_seen_posts SET status = 'ignored', reviewed_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND status = 'new'",
        (seen_id,),
    )
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def find_by_post_and_subscription(subscription_id: int, post_id: int) -> dict[str, Any] | None:
    """Найти seen_post по (subscription_id, post_id) — для дедупликации при поиске."""
    db = get_db()
    cur = await db.execute(
        "SELECT * FROM gelbooru_seen_posts WHERE subscription_id = ? AND post_id = ?",
        (subscription_id, post_id),
    )
    row = await cur.fetchone()
    await cur.close()
    return _row_to_seen(row) if row else None


def _row_to_seen(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["tags"] = _parse_tags(d.get("tags"))
    return d


# ===========================================================================
# gelbooru_blacklist_tags CRUD
# ===========================================================================


async def add_blacklist_tag(tag: str, created_by_user: int | None = None) -> int | None:
    """Добавляет тег в глобальный blacklist.

    tag — БЕЗ минуса (например 'rating_explicit'). При поиске к запросу будет
    добавлено '-rating_explicit'. Возвращает id новой строки или None если уже есть.
    """
    clean = tag.strip().lstrip("-").lower()
    if not clean:
        raise ValueError("Пустой blacklist-тег")
    db = get_db()
    cur = await db.execute(
        "INSERT OR IGNORE INTO gelbooru_blacklist_tags (tag, created_by_user) VALUES (?, ?)",
        (clean, created_by_user),
    )
    await db.commit()
    inserted = cur.rowcount > 0
    rid = cur.lastrowid
    await cur.close()
    return rid if inserted else None


async def remove_blacklist_tag(tag: str) -> bool:
    """Удаляет тег из blacklist (по точному имени, case-insensitive)."""
    clean = tag.strip().lstrip("-").lower()
    db = get_db()
    cur = await db.execute("DELETE FROM gelbooru_blacklist_tags WHERE tag = ?", (clean,))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def list_blacklist_tags() -> list[str]:
    """Возвращает список blacklist-тегов (без минусов, отсортированный)."""
    db = get_db()
    cur = await db.execute("SELECT tag FROM gelbooru_blacklist_tags ORDER BY tag")
    rows = await cur.fetchall()
    await cur.close()
    return [r["tag"] for r in rows]


async def is_blacklisted(tag: str) -> bool:
    """Проверяет, в blacklist ли тег (case-insensitive)."""
    clean = tag.strip().lstrip("-").lower()
    if not clean:
        return False
    db = get_db()
    cur = await db.execute("SELECT 1 FROM gelbooru_blacklist_tags WHERE tag = ?", (clean,))
    row = await cur.fetchone()
    await cur.close()
    return row is not None


async def get_blacklist_query_suffix() -> str:
    """Возвращает строку '-tag1 -tag2 ...' для добавления к поисковому запросу gelbooru.

    Используется worker'ом при опросе подписок: к tags подписки добавляются
    все blacklist-теги с минусом. Если blacklist пуст — возвращает ''.
    """
    tags = await list_blacklist_tags()
    return " ".join(f"-{t}" for t in tags)
