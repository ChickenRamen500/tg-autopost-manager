"""Сервис опроса подписок Gelbooru (ТЗ §2.1.4, Фаза 3 этап 3).

Бизнес-логика автопарсинга подписок:
  1. Для каждой due-подписки (list_due_subscriptions):
     a. Собираем поисковый запрос: tags подписки + глобальный blacklist (-tag).
     b. GelbooruClient.search_posts(query, limit).
     c. Для каждого найденного поста:
        - кросс-подписочная дедупликация по md5 (is_file_seen_anywhere)
        - внутриподписочная дедупликация (is_post_seen)
        - если новый → add_seen_post(status='new')
     d. mark_checked(subscription_id)
  2. Если есть новые посты И подошло время (is_due_for_notify):
     → notify_admin «N новых постов по подписке <tags>» + set_last_notified

Игнор-теги (ТЗ §2.1.4): теги с минусом в строке подписки (например '-rating_explicit')
передаются в gelbooru API как есть — gelbooru сам исключает их из результатов.
Глобальный blacklist добавляется дополнительно через get_blacklist_query_suffix.

Отправка поста в очередь (UI этап 4 → mark_sent + создание posts-поста):
  send_seen_post_to_queue(seen_id, queue_id) → создаёт posts-пост status='new'
  + enqueue download → mark_sent(seen_id, post_pk).
"""
from __future__ import annotations

import html
import logging
from typing import Any

from app.config.settings import settings
from app.repositories import (
    gelbooru_subs as gs_repo,
)
from app.repositories import (
    subscriptions as subs_repo,
)

log = logging.getLogger("gelbooru.subs")


def _build_query(tags: list[str], blacklist_suffix: str) -> str:
    """Собирает поисковый запрос gelbooru из тегов подписки + blacklist.

    tags могут содержать '-tag' (игнор-теги в самой подписке) — они передаются как есть.
    blacklist_suffix — строка '-tag1 -tag2' из глобального blacklist.
    """
    query = " ".join(tags).strip()
    if blacklist_suffix:
        query = f"{query} {blacklist_suffix}".strip()
    return query


def _extract_post_fields(post: dict[str, Any]) -> dict[str, Any]:
    """Извлекает нужные поля из ответа gelbooru API в формат seen_posts.

    Gelbooru возвращает: id, file_url, preview_url, sample_url, file_ext, md5,
    width, height, tags (строка через пробел), rating, source, ...
    """
    return {
        "post_id": int(post.get("id", 0)),
        "file_unique_id": post.get("md5"),  # md5 — стабильный идентификатор файла
        "source_url": post.get("file_url"),
        "preview_url": post.get("preview_url"),
        "tags": post.get("tags", ""),
        "rating": post.get("rating"),
    }


async def check_subscription(subscription: dict[str, Any]) -> int:
    """Опрашивает gelbooru по подписке, добавляет новые посты в seen_posts.

    Возвращает количество НОВЫХ постов (добавленных в этом чеке).
    Не падает при ошибках API — возвращает 0 и логирует warning.

    subscription — dict из subs_repo.get_subscription / list_due_subscriptions
    (с распарсенными tags).
    """
    sub_id = subscription["id"]
    tags = subscription.get("tags") or []
    if not tags:
        log.warning("check_subscription: подписка #%s без тегов — пропуск", sub_id)
        return 0

    # Глобальный blacklist (-tag1 -tag2 ...)
    blacklist_suffix = await gs_repo.get_blacklist_query_suffix()
    query = _build_query(tags, blacklist_suffix)

    log.info("check_subscription: #%s query=%r", sub_id, query)

    # Поиск через gelbooru client (rate limit + cache встроены)
    try:
        from app.services.parsers.gelbooru.client import gelbooru_client

        posts = await gelbooru_client.search_posts(query, pid=0, limit=settings.gelbooru_search_limit)
    except Exception as e:  # noqa: BLE001 — не роняем worker из-за одной подписки
        log.warning("check_subscription: gelbooru API ошибка для #%s: %s", sub_id, e)
        return 0

    if not posts:
        log.info("check_subscription: #%s — 0 постов найдено", sub_id)
        return 0

    new_count = 0
    for raw_post in posts:
        try:
            fields = _extract_post_fields(raw_post)
        except (ValueError, TypeError) as e:
            log.warning("check_subscription: пост без id/query=%r: %s", query, e)
            continue

        if not fields["post_id"]:
            continue

        # Кросс-подписочная дедупликация по md5 (один пост может быть в нескольких подписках)
        if fields["file_unique_id"] and await gs_repo.is_file_seen_anywhere(fields["file_unique_id"]):
            continue

        # Внутриподписочная дедупликация по post_id
        if await gs_repo.is_post_seen(sub_id, fields["post_id"]):
            continue

        # Добавляем как новый
        added = await gs_repo.add_seen_post(
            subscription_id=sub_id,
            post_id=fields["post_id"],
            file_unique_id=fields["file_unique_id"],
            source_url=fields["source_url"],
            preview_url=fields["preview_url"],
            tags=fields["tags"],
            rating=fields["rating"],
        )
        if added is not None:
            new_count += 1

    log.info("check_subscription: #%s — найдено %d, новых %d", sub_id, len(posts), new_count)
    return new_count


async def check_all_due_subscriptions() -> dict[str, int]:
    """Опрашивает все due-подписки. Возвращает статистику {checked, new_posts, notified}.

    Вызывается worker'ом по расписанию. Не падает на одной подписке — продолжает остальные.
    """
    due = await subs_repo.list_due_subscriptions()
    if not due:
        return {"checked": 0, "new_posts": 0, "notified": 0}

    total_new = 0
    notified = 0
    for sub in due:
        try:
            new_count = await check_subscription(sub)
            total_new += new_count
            # mark_checked ВНЕ зависимости от new_count (чтобы не опрашивать снова до interval)
            await subs_repo.mark_checked(sub["id"])

            # Уведомление админу (с учётом notify_interval)
            if new_count > 0:
                if await _maybe_notify(sub, new_count):
                    notified += 1
        except Exception as e:  # noqa: BLE001 — не роняем цикл из-за одной подписки
            log.exception("check_all_due_subscriptions: подписка #%s упала: %s", sub.get("id"), e)

    log.info(
        "check_all_due_subscriptions: проверено %d подписок, новых постов %d, уведомлений %d",
        len(due), total_new, notified,
    )
    return {"checked": len(due), "new_posts": total_new, "notified": notified}


async def _maybe_notify(subscription: dict[str, Any], new_count: int) -> bool:
    """Отправляет уведомление админу если подошло время (is_due_for_notify).

    Возвращает True если уведомление отправлено.
    """
    sub_id = subscription["id"]
    if not await subs_repo.is_due_for_notify(sub_id):
        return False

    tags_str = " ".join(subscription.get("tags") or [])
    total_new = await gs_repo.count_new_posts(sub_id)

    text = (
        f"🔔 <b>Новые посты Gelbooru</b>\n"
        f"Подписка #{sub_id} «{html.escape(tags_str)}»\n"
        f"Найдено {new_count} новых (всего непросмотренных: {total_new})\n"
        f"Просмотр: /menu → Gelbooru"
    )
    try:
        from app.services.notifications import notify_admin

        await notify_admin(text)
    except Exception as e:  # noqa: BLE001 — уведомления не критичны
        log.warning("_maybe_notify: не удалось отправить уведомление: %s", e)
        return False

    await subs_repo.set_last_notified(sub_id)
    return True


# ---------------------------------------------------------------------------
# Отправка seen_post в очередь (UI этап 4 вызывает это при кнопке «Отправить»)
# ---------------------------------------------------------------------------


async def send_seen_post_to_queue(seen_id: int, queue_id: int | None = None) -> int | None:
    """Создаёт posts-пост из seen_post и ставит task_queue download.

    Жизненный цикл:
      1. Достаём seen_post (status='new').
      2. Создаём posts-пост: source_type='gelbooru', source_id=post_id, source_url=file_url,
         file_type (по file_ext), status='new', queue_id (None = Входящие).
      3. enqueue(post_id, 'download', payload={referer, ...}).
      4. mark_sent(seen_id, post_pk).

    Возвращает post_pk (ID созданного posts-поста) или None при ошибке.
    """
    from app.repositories import posts as posts_repo
    from app.repositories import task_queue as tq

    seen = await gs_repo.get_seen_post(seen_id)
    if seen is None:
        log.warning("send_seen_post_to_queue: seen_post #%s не найден", seen_id)
        return None
    if seen["status"] != "new":
        log.warning("send_seen_post_to_queue: seen_post #%s уже обработан (status=%s)", seen_id, seen["status"])
        return None

    if not seen.get("source_url"):
        log.warning("send_seen_post_to_queue: seen_post #%s без source_url", seen_id)
        return None

    # Определяем file_type по расширению source_url или tags
    file_type = _file_type_from_url(seen.get("source_url") or "")

    try:
        post_pk = await posts_repo.create_post(
            source_type="gelbooru",
            source_id=str(seen["post_id"]),
            source_url=seen["source_url"],
            file_type=file_type,
            tags=seen.get("tags") or [],
            queue_id=queue_id,
            status="new",
        )
    except Exception as e:  # noqa: BLE001
        log.exception("send_seen_post_to_queue: не удалось создать пост: %s", e)
        return None

    # enqueue download — worker скачает и применит pHash-дедуп
    payload = {
        "referer": "https://gelbooru.com/",
        "site": "gelbooru",
    }
    try:
        await tq.enqueue(post_pk, "download", payload)
    except Exception as e:  # noqa: BLE001
        log.exception("send_seen_post_to_queue: не удалось enqueue: %s", e)
        # Пост создан, но без task — он зависнет в 'new'. Логируем, но seen_post помечаем sent
        # (иначе при retry будет дубликат posts-поста)

    # Помечаем seen_post как sent
    await gs_repo.mark_sent(seen_id, post_pk)
    log.info(
        "send_seen_post_to_queue: seen #%s → post #%s (queue=%s, type=%s)",
        seen_id, post_pk, queue_id, file_type,
    )
    return post_pk


def _file_type_from_url(url: str) -> str:
    """Определяет file_type по расширению URL gelbooru.

    gelbooru file_ext: jpg/png/gif/webm/mp4/webp. GIF может быть как анимация.
    """
    url_lower = url.lower().split("?")[0]
    if url_lower.endswith(".gif"):
        return "gif"
    if url_lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp")):
        return "photo"
    if url_lower.endswith((".mp4", ".webm", ".mov", ".avi", ".mkv")):
        return "video"
    # По умолчанию — photo (gelbooru преимущественно изображения)
    return "photo"
