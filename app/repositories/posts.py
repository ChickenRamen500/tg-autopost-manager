"""Репозиторий постов (ТЗ §5.1 posts, §2.1.1, §2.3.4 статусы).

Жизненный цикл поста (ТЗ §2.3.4):
  new -> downloading -> pending -> sent
                          |  └-> failed
                          └-> duplicate / possible_duplicate
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.db import get_db


def _tags_to_json(tags: list[str] | None) -> str | None:
    if tags is None:
        return None
    return json.dumps(tags, ensure_ascii=False)


def _row_to_post(row: Any) -> dict[str, Any]:
    d = dict(row)
    # JSON-теги распарсим в список (для удобства в handlers/services)
    raw_tags = d.get("tags")
    if raw_tags:
        try:
            d["tags"] = json.loads(raw_tags)
        except (json.JSONDecodeError, TypeError):
            d["tags"] = []
    else:
        d["tags"] = []
    return d


async def create_post(
    *,
    source_type: str,
    file_type: str,
    queue_id: int | None = None,
    source_id: str | None = None,
    source_url: str | None = None,
    source_channel: str | None = None,
    file_path: str | None = None,
    tg_file_id: str | None = None,
    file_unique_id: str | None = None,
    file_size: int | None = None,
    mime_type: str | None = None,
    original_caption: str | None = None,
    processed_caption: str | None = None,
    tags: list[str] | None = None,
    status: str = "pending",
    was_processed: bool = False,
    media_group_id: str | None = None,
    scheduled_time: str | None = None,
    on_conflict_file_unique_do_nothing: bool = False,
) -> int:
    """Создаёт пост. По умолчанию статус 'pending' (готов к публикации).

    Для внешних источников (Chrome-расширение, парсеры) используйте status='new'
    — worker Фазы 3 скачает файл и переведёт в pending.

    scheduled_time — ISO-строка или NULL. NULL = отправить сразу по позиции
    очереди; иначе пост будет ждать до scheduled_time (см. get_next_pending).

    on_conflict_file_unique_do_nothing=True — добавить ON CONFLICT(file_unique_id)
    DO NOTHING. Используется в receive_media для атомарной дедупликации (CR-H-V1
    TOCTOU fix): вместо SELECT-then-INSERT (гонка между двумя одновременными
    апдейтами с одним file_unique_id) делаем один INSERT. Возвращает 0, если
    конфликт (пост уже существует с этим file_unique_id), иначе id новой строки.
    Требует partial UNIQUE index idx_posts_file_unique_unique в schema.sql.
    """
    if status not in ("new", "downloading", "pending", "sent", "failed", "duplicate", "possible_duplicate"):
        raise ValueError(f"Недопустимый статус поста: {status!r}")
    if file_type not in ("photo", "video", "gif", "document", "audio", "voice"):
        raise ValueError(f"Недопустимый file_type: {file_type!r}")

    db = get_db()
    sql = """
        INSERT INTO posts (
            queue_id, source_type, source_id, source_url, source_channel,
            file_path, file_type, tg_file_id, file_unique_id, file_size, mime_type,
            was_processed, original_caption, processed_caption, tags, status, scheduled_time, media_group_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    if on_conflict_file_unique_do_nothing:
        # Conflict target: UNIQUE index idx_posts_file_unique_unique.
        # SQLite матчит индекс по имени колонки file_unique_id.
        # ВАЖНО: при конфликте lastrowid НЕ обнуляется — он остаётся равным
        # последнему успешному INSERT'у на этом соединении (sqlite3-особенность).
        # Поэтому используем RETURNING id — он вернёт строку ТОЛЬКО если вставка
        # реально произошла, и None при тихом игноре (ON CONFLICT DO NOTHING).
        sql += " ON CONFLICT(file_unique_id) DO NOTHING RETURNING id"
        cur = await db.execute(
            sql,
            (
                queue_id,
                source_type,
                source_id,
                source_url,
                source_channel,
                file_path,
                file_type,
                tg_file_id,
                file_unique_id,
                file_size,
                mime_type,
                1 if was_processed else 0,
                original_caption,
                processed_caption,
                _tags_to_json(tags),
                status,
                scheduled_time,
                media_group_id,
            ),
        )
        row = await cur.fetchone()
        await cur.close()
        await db.commit()
        # None → конфликт (file_unique_id уже занят). Возвращаем 0 — сигнал caller'у.
        return row["id"] if row is not None else 0
    cur = await db.execute(
        sql,
        (
            queue_id,
            source_type,
            source_id,
            source_url,
            source_channel,
            file_path,
            file_type,
            tg_file_id,
            file_unique_id,
            file_size,
            mime_type,
            1 if was_processed else 0,
            original_caption,
            processed_caption,
            _tags_to_json(tags),
            status,
            scheduled_time,
            media_group_id,
        ),
    )
    await db.commit()
    rid = cur.lastrowid
    await cur.close()
    return rid


async def delete_post(post_id: int) -> bool:
    """Удаляет пост (для ручной очистки дубликатов из Входящих)."""
    db = get_db()
    cur = await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def find_album_posts(queue_id: int, media_group_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Все pending-посты альбома (с одинаковым media_group_id) в очереди.

    Для групповой отправки через send_media_group (ТЗ §2.1.1).
    Возвращает посты в порядке создания.
    """
    db = get_db()
    cur = await db.execute(
        """
        SELECT * FROM posts
        WHERE queue_id = ? AND media_group_id = ? AND status = 'pending'
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """,
        (queue_id, media_group_id, limit),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_post(r) for r in rows]


async def get_post(post_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
    row = await cur.fetchone()
    await cur.close()
    return _row_to_post(row) if row else None


async def get_next_pending(queue_id: int) -> dict[str, Any] | None:
    """Следующий пост к отправке из очереди (ТЗ §2.5.1).

    Порядок: по scheduled_time asc (NULLs first), затем по created_at asc.
    При shuffle_enabled очереди перемешивание будет в планировщике (Фаза 5).

    Фильтрация будущих scheduled_time (CR-H-R3): посты с scheduled_time в
    будущем НЕ отдаются планировщиком — иначе можно было «проскочить»
    отложенный пост и отправить следующий раньше времени. NULL считается
    доступным сейчас (отправлять по позиции очереди).
    """
    db = get_db()
    cur = await db.execute(
        """
        SELECT * FROM posts
        WHERE queue_id = ? AND status = 'pending'
          AND (scheduled_time IS NULL OR scheduled_time <= datetime('now'))
        ORDER BY scheduled_time IS NOT NULL, scheduled_time ASC, created_at ASC
        LIMIT 1
        """,
        (queue_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return _row_to_post(row) if row else None


async def list_queue_posts(queue_id: int, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    db = get_db()
    if status:
        cur = await db.execute(
            "SELECT * FROM posts WHERE queue_id = ? AND status = ? ORDER BY created_at DESC LIMIT ?",
            (queue_id, status, limit),
        )
    else:
        cur = await db.execute(
            "SELECT * FROM posts WHERE queue_id = ? ORDER BY created_at DESC LIMIT ?",
            (queue_id, limit),
        )
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_post(r) for r in rows]


async def list_inbox(limit: int = 100) -> list[dict[str, Any]]:
    """«Входящие» — посты без очереди (queue_id IS NULL), ручная сортировка (ТЗ §2.2.2).

    ВАЖНО: excludes sent posts — они уже отправлены и не требуют модерации.
    Sent посты из удалённых очередей удаляются в remove_queue(), но если
    какой-то sent пост остался без очереди (edge case), он сюда не попадёт —
    используйте list_sent() для просмотра отправленных.
    """
    db = get_db()
    cur = await db.execute(
        "SELECT * FROM posts WHERE queue_id IS NULL AND status != 'sent' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_post(r) for r in rows]


async def list_inbox_grouped(limit: int = 100) -> list[dict[str, Any]]:
    """«Входящие» с группировкой по media_group_id.

    Возвращает список «групп» — каждая группа это словарь:
      {
        "media_group_id": str | None,  # None для одиночных постов
        "posts": [post1, post2, ...],  # посты в группе (для одиночного — 1 элемент)
        "count": int,                  # количество постов
        "first": post,                 # первый пост (для отображения в UI)
        "file_type": str,              # тип первого поста
        "source_type": str,            # источник первого поста
        "created_at": str,            # created_at первого поста
      }

    Логика:
    - Посты с одинаковым media_group_id (IS NOT NULL) объединяются в одну группу.
    - Посты с media_group_id=NULL — каждая своя группа (count=1).
    - Группы сортируются по created_at DESC.
    """
    posts = await list_inbox(limit=limit)
    groups: dict[str, dict[str, Any]] = {}
    singles: list[dict[str, Any]] = []
    for p in posts:
        mgid = p.get("media_group_id")
        if mgid:
            if mgid not in groups:
                groups[mgid] = {
                    "media_group_id": mgid,
                    "posts": [],
                    "count": 0,
                    "first": p,
                    "file_type": p["file_type"],
                    "source_type": p["source_type"],
                    "created_at": p["created_at"],
                }
            groups[mgid]["posts"].append(p)
            groups[mgid]["count"] += 1
        else:
            singles.append({
                "media_group_id": None,
                "posts": [p],
                "count": 1,
                "first": p,
                "file_type": p["file_type"],
                "source_type": p["source_type"],
                "created_at": p["created_at"],
            })
    # Объединяем группы и одиночные, сортируем по created_at DESC
    all_groups = list(groups.values()) + singles
    all_groups.sort(key=lambda g: g["created_at"], reverse=True)
    return all_groups


async def find_posts_by_media_group_id(media_group_id: str) -> list[dict[str, Any]]:
    """Все посты с данным media_group_id (для назначения группы в очередь разом)."""
    db = get_db()
    cur = await db.execute(
        "SELECT * FROM posts WHERE media_group_id = ? ORDER BY id ASC",
        (media_group_id,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_post(r) for r in rows]


async def list_sent(limit: int = 200, queue_id: int | None = None) -> list[dict[str, Any]]:
    """Список отправленных постов (status='sent') — для страницы «Архив/Sent».

    Если queue_id задан — фильтрует по очереди; иначе все sent посты.
    Сортировка: sent_at DESC (сначала самые свежие отправленные).
    """
    db = get_db()
    if queue_id is not None:
        cur = await db.execute(
            "SELECT * FROM posts WHERE status = 'sent' AND queue_id = ? "
            "ORDER BY sent_at DESC, created_at DESC LIMIT ?",
            (queue_id, limit),
        )
    else:
        cur = await db.execute(
            "SELECT * FROM posts WHERE status = 'sent' "
            "ORDER BY sent_at DESC, created_at DESC LIMIT ?",
            (limit,),
        )
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_post(r) for r in rows]


async def update_status(post_id: int, status: str, error: str | None = None) -> bool:
    if status not in ("new", "downloading", "pending", "sent", "failed", "duplicate", "possible_duplicate"):
        raise ValueError(f"Недопустимый статус поста: {status!r}")
    db = get_db()
    if status == "sent":
        cur = await db.execute(
            "UPDATE posts SET status = 'sent', sent_at = CURRENT_TIMESTAMP, last_error = NULL WHERE id = ?",
            (post_id,),
        )
    else:
        cur = await db.execute(
            "UPDATE posts SET status = ?, last_error = ? WHERE id = ?",
            (status, error, post_id),
        )
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def mark_sent(post_id: int) -> bool:
    return await update_status(post_id, "sent")


async def bulk_mark_sent(post_ids: list[int]) -> int:
    """Атомарно помечает несколько постов как sent (одна транзакция).

    Возвращает количество обновлённых постов. Решает P1-9 (code review):
    раньше каждый mark_sent был отдельным commit → crash между = partial-send дубликаты.
    """
    if not post_ids:
        return 0
    db = get_db()
    placeholders = ",".join("?" * len(post_ids))
    cur = await db.execute(
        f"UPDATE posts SET status = 'sent', sent_at = CURRENT_TIMESTAMP, last_error = NULL WHERE id IN ({placeholders})",
        post_ids,
    )
    await db.commit()
    changed = cur.rowcount
    await cur.close()
    return changed


async def mark_failed(post_id: int, error: str) -> bool:
    return await update_status(post_id, "failed", error=error)


async def increment_attempt(post_id: int) -> int:
    """Увеличивает счётчик попыток отправки. Возвращает новое значение (ТЗ §2.5.2 п.4)."""
    db = get_db()
    cur = await db.execute(
        "UPDATE posts SET send_attempts = send_attempts + 1 WHERE id = ? RETURNING send_attempts",
        (post_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    await db.commit()
    return row["send_attempts"] if row else 0


async def assign_queue(post_id: int, queue_id: int | None) -> bool:
    """Назначить/перенести пост в очередь (или во «Входящие» при queue_id=None)."""
    db = get_db()
    cur = await db.execute("UPDATE posts SET queue_id = ? WHERE id = ?", (queue_id, post_id))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def set_file_info(
    post_id: int,
    *,
    file_path: str | None = None,
    file_type: str | None = None,
    file_size: int | None = None,
    mime_type: str | None = None,
) -> bool:
    """Сохраняет метаданные скачанного файла в posts (ТЗ §4.2.2, Фаза 3).

    Вызывается worker'ом после успешного download_media(). file_type может
    обновляться (Content-Type точнее, чем предположение при создании поста).
    Переданные None-поля не затрагиваются (COALESCE).
    """
    db = get_db()
    cur = await db.execute(
        """
        UPDATE posts SET
            file_path  = COALESCE(?, file_path),
            file_type  = COALESCE(?, file_type),
            file_size  = COALESCE(?, file_size),
            mime_type  = COALESCE(?, mime_type)
        WHERE id = ?
        """,
        (file_path, file_type, file_size, mime_type, post_id),
    )
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


# Шаблон hex-строки: только 0-9a-f, чётной длины (байты). Длина произвольная,
# т.к. pHash-алгоритмы дают разную битность (64 бита = 16 hex, 128 бит = 32 hex и т.д.).
_PHEX_RE = re.compile(r"^(?:[0-9a-fA-F]{2})+$")


async def set_phash(post_id: int, phash: str) -> bool:
    """Сохраняет pHash в posts.phash. Возвращает True, если строка обновилась.

    ВАЛИДАЦИЯ (CR-L-R7): phash должен быть hex-строкой чётной длины
    (0-9a-f), иначе ValueError. Защищает _hamming_hex от мусора в БД.
    """
    if not phash or not _PHEX_RE.match(phash):
        raise ValueError(f"Недопустимый phash (нужен hex чётной длины): {phash!r}")
    db = get_db()
    cur = await db.execute("UPDATE posts SET phash = ? WHERE id = ?", (phash, post_id))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def find_by_file_unique_id(file_unique_id: str) -> list[dict[str, Any]]:
    """Точный поиск дубликата по file_unique_id Telegram (ТЗ §2.4.3)."""
    db = get_db()
    cur = await db.execute(
        "SELECT * FROM posts WHERE file_unique_id = ? ORDER BY created_at DESC LIMIT 5",
        (file_unique_id,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_post(r) for r in rows]


async def find_by_source(source_type: str, source_id: str) -> list[dict[str, Any]]:
    """Поиск постов по точному источнику (ТЗ §2.4.2 — дедуп внешних источников).

    Используется API расширения (quick_add): один и тот же media_url не должен
    создавать повторные посты/задачи скачивания.
    """
    db = get_db()
    cur = await db.execute(
        "SELECT * FROM posts WHERE source_type = ? AND source_id = ? ORDER BY created_at DESC LIMIT 5",
        (source_type, source_id),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_post(r) for r in rows]


async def count_by_status(status: str) -> int:
    db = get_db()
    cur = await db.execute("SELECT COUNT(*) AS c FROM posts WHERE status = ?", (status,))
    row = await cur.fetchone()
    await cur.close()
    return row["c"] if row else 0


async def total_count() -> int:
    db = get_db()
    cur = await db.execute("SELECT COUNT(*) AS c FROM posts")
    row = await cur.fetchone()
    await cur.close()
    return row["c"] if row else 0


# ---------------------------------------------------------------------------
# Bulk-операции (для очистки очереди / переноса постов пачкой)
# ---------------------------------------------------------------------------

async def bulk_assign_queue(queue_id: int | None, target_queue_id: int | None) -> int:
    """Переносит ВСЕ посты из очереди queue_id в target_queue_id одним UPDATE.

    queue_id=None не поддерживается (бессмысленно — inbox не «очередь»).
    target_queue_id=None → перенос во «Входящие». Возвращает количество строк.
    """
    db = get_db()
    cur = await db.execute(
        "UPDATE posts SET queue_id = ? WHERE queue_id = ?",
        (target_queue_id, queue_id),
    )
    await db.commit()
    changed = cur.rowcount
    await cur.close()
    return changed


async def bulk_delete_by_queue(queue_id: int) -> int:
    """Удаляет ВСЕ посты очереди одним DELETE. Возвращает количество удалённых."""
    db = get_db()
    cur = await db.execute("DELETE FROM posts WHERE queue_id = ?", (queue_id,))
    await db.commit()
    changed = cur.rowcount
    await cur.close()
    return changed


async def bulk_increment_attempt(post_ids: list[int]) -> None:
    """Инкремент send_attempts сразу для списка постов одним запросом.

    Чанкование по 500 — защита от SQLite-лимита на количество параметров
    (SQLITE_MAX_VARIABLE_NUMBER по умолчанию 999).
    """
    if not post_ids:
        return
    db = get_db()
    CHUNK = 500
    for i in range(0, len(post_ids), CHUNK):
        chunk = post_ids[i:i + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        cur = await db.execute(
            f"UPDATE posts SET send_attempts = send_attempts + 1 WHERE id IN ({placeholders})",
            chunk,
        )
        await cur.close()
    await db.commit()
