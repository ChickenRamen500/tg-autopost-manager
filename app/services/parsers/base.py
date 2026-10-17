"""Базовый интерфейс внешних парсеров (ТЗ §2.1.4, Фаза 3 этап 5).

Все парсеры (Danbooru, X.com, Pikabu, Pixiv) реализуют общий интерфейс:
  - search(query, limit) → list[ExternalPost] — поиск постов по запросу;
  - create_post_from_external(post, queue_id) → int — создаёт posts-пост
    status='new' + enqueue download task (worker скачает + pHash-дедуп).

Архитектура: парсеры НЕ скачивают файлы сами. Они возвращают source_url,
а скачивание делегируется worker'у (этап 1) через task_queue. Это переиспользует
инфраструктуру: retry, max attempts, stale cleanup, pHash-дедупликация (этап 2),
file_size/mime определения из downloader.py.

ExternalPost — унифицированный формат независимо от источника:
  source_type: 'danbooru' | 'xcom' | 'pikabu' | 'pixiv'
  source_id: ID поста на источнике (str)
  source_url: прямой URL файла для скачивания
  preview_url: URL preview/thumbnail (для UI)
  file_type: photo/video/gif/document
  tags: list[str]
  rating: safe/questionable/explicit/None
  referer: Referer для HTTP-запроса (pixiv/gelbooru требуют)
  extra_headers: dict доп. заголовков (Cookie для xcom и т.п.)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("parsers.base")


@dataclass
class ExternalPost:
    """Унифицированный пост из внешнего источника."""

    source_type: str  # 'danbooru' | 'xcom' | 'pikabu' | 'pixiv'
    source_id: str
    source_url: str  # прямой URL файла
    file_type: str = "photo"  # photo/video/gif/document
    preview_url: str | None = None
    tags: list[str] = field(default_factory=list)
    rating: str | None = None
    referer: str | None = None
    extra_headers: dict[str, str] | None = None
    # Оригинальный caption (если есть) — сохраняется в posts.original_caption
    caption: str | None = None


class BaseParser:
    """Базовый класс парсера. Подклассы реализуют search().

    Конкретные парсеры (DanbooruParser, XcomParser, ...) переопределяют:
      - search(query, limit) → list[ExternalPost]
      - close() — закрыть HTTP session (если есть)
    """

    source_type: str = "base"

    async def search(self, query: str, limit: int = 20) -> list[ExternalPost]:
        """Поиск постов по запросу. Возвращает список ExternalPost.

        query — поисковый запрос (теги для danbooru, URL для xcom, и т.п.).
        limit — максимальное количество постов.
        """
        raise NotImplementedError

    async def close(self) -> None:
        """Закрыть ресурсы (HTTP session). По умолчанию — ничего."""
        pass


async def create_post_from_external(
    post: ExternalPost,
    queue_id: int | None = None,
) -> int | None:
    """Создаёт posts-пост из ExternalPost + enqueue download task.

    Жизненный цикл:
      1. posts.create_post(source_type, source_id, source_url, file_type, status='new')
      2. task_queue.enqueue(post_id, 'download', payload={referer, headers})
      3. Worker (этап 1) скачает файл → set_file_info → pHash-дедуп (этап 2) → pending

    Возвращает post_id или None при ошибке.
    """
    from app.repositories import posts as posts_repo
    from app.repositories import task_queue as tq

    try:
        post_id = await posts_repo.create_post(
            source_type=post.source_type,
            source_id=post.source_id,
            source_url=post.source_url,
            file_type=post.file_type,
            tags=post.tags,
            original_caption=post.caption,
            queue_id=queue_id,
            status="new",
        )
    except Exception as e:  # noqa: BLE001
        log.exception("create_post_from_external: не удалось создать пост: %s", e)
        return None

    # enqueue download — worker скачает и применит pHash-дедуп
    payload: dict[str, Any] = {"site": post.source_type}
    if post.referer:
        payload["referer"] = post.referer
    if post.extra_headers:
        payload["headers"] = post.extra_headers

    try:
        await tq.enqueue(post_id, "download", payload)
    except Exception as e:  # noqa: BLE001
        log.exception("create_post_from_external: не удалось enqueue: %s", e)
        # Пост создан, но без task — зависнет в 'new'. Возвращаем post_id,
        # caller может решить что делать (например, удалить пост).

    log.info(
        "create_post_from_external: %s post #%s → posts #%s (queue=%s, type=%s)",
        post.source_type, post.source_id, post_id, queue_id, post.file_type,
    )
    return post_id


def file_type_from_extension(url: str) -> str:
    """Определяет file_type по расширению URL (общий хелпер для парсеров)."""
    url_lower = url.lower().split("?")[0]
    if url_lower.endswith(".gif"):
        return "gif"
    if url_lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp")):
        return "photo"
    if url_lower.endswith((".mp4", ".webm", ".mov", ".avi", ".mkv")):
        return "video"
    return "photo"  # default для парсеров изображений
