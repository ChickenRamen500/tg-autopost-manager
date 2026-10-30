"""REST API для Chrome-расширения (ТЗ §6.1.3, §9 — Фаза 4).

Endpoints:
  POST /api/quick_add         — режим A: только ссылка, скачивает бот (worker, task_queue)
  POST /api/quick_add_upload  — режим B: браузер скачал файл (cookies) → multipart upload
  GET  /api/queues            — список очередей (popup расширения показывает выбор)
  GET  /api/post_status/{id}  — статус поста (расширение может опционально опрашивать)

Авторизация (ТЗ §6.1): секретный токен в заголовке ``X-Secret-Token`` ИЛИ в поле
``secret_token`` тела запроса (расширение по ТЗ §9.5 кладёт токен в body/formdata,
поэтому проверка написана внутри хендлеров, а не через Depends(verify_token)).

Если SECRET_TOKEN не задан в .env — авторизация отключена (локальная разработка),
как и на /health* endpoints (app/main.py::verify_token).

Жизненный цикл постов из расширения:
  quick_add (режим A)     → posts.status='new' + task_queue(download) → worker качает
                            (с Referer из payload) → pHash-дедуп → 'pending'/'possible_duplicate'
  quick_add_upload (реж.) → файл сохраняется в settings.media_dir, pHash считается
                            синхронно, дубликат → 'possible_duplicate' (в Входящие),
                            иначе сразу 'pending'.
Маршрутизация по правилам (ТЗ §2.2) применяется к обоим режимам; если правило не
найдено — пост попадает в «Входящие» (queue_id=NULL).
"""
from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.config.settings import settings
from app.repositories import posts as posts_repo
from app.repositories import queues as queues_repo
from app.repositories import task_queue as tq_repo
from app.services.routing.service import route_post

log = logging.getLogger("api.quick_add")

router = APIRouter(prefix="/api", tags=["extension"])

# Разрешённые расширения для quick_add_upload (ТЗ §6.1.3: медиа из браузера).
_ALLOWED_UPLOAD_EXTS = {
    # images
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif",
    # video
    ".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v",
}


class QuickAddRequest(BaseModel):
    """Тело POST /api/quick_add (ТЗ §6.1.3)."""

    url: str = Field(..., description="URL страницы-источника (page URL)")
    media_url: str | None = Field(None, description="Прямая ссылка на файл (если известна)")
    referer: str | None = Field(None, description="Referer для скачивания (pixiv/gelbooru требуют)")
    title: str | None = None
    tags: list[str] = Field(default_factory=list)
    site: str | None = Field(None, description="Подсказка источника: gelbooru/danbooru/pixiv/...")
    queue_id: int | None = Field(None, description="Явная очередь (иначе — маршрутизация/Входящие)")
    secret_token: str | None = Field(None, description="SECRET_TOKEN (или заголовок X-Secret-Token)")
    # media_group_id — для постов с 2+ медиа (x.com: твит с 4 картинками).
    # Все медиа одного поста получают одинаковый ID; если у очереди group_albums=True,
    # scheduler отправит их одним сообщением через send_media_group (ТЗ §2.1.1).
    media_group_id: str | None = Field(None, description="ID альбома (для send_media_group)")


def _check_auth(provided_token: str | None, request: Request) -> None:
    """Проверка секретного токена (constant-time, как verify_token в main.py)."""
    if not settings.secret_token:
        return  # auth отключён (SECRET_TOKEN не задан — локальная разработка)
    header_token = request.headers.get("x-secret-token")
    candidate = provided_token or header_token
    if not candidate or not secrets.compare_digest(candidate, settings.secret_token):
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing secret token")


def _guess_file_type(url: str) -> str:
    """Предположительный file_type по расширению URL (для поста со status='new')."""
    path = urlparse(url or "").path.lower()
    ext = path[path.rfind("."):] if "." in path else ""
    if ext in (".gif", ".gifv"):
        return "gif"
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif"):
        return "photo"
    if ext in (".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v"):
        return "video"
    return "document"


def _normalize_site(site: str | None, page_url: str) -> str:
    """source_type поста: подсказка сайта или hostname (gelbooru.com → gelbooru)."""
    if site:
        return site.strip().lower()
    host = (urlparse(page_url).hostname or "").lower()
    for key in ("gelbooru", "danbooru", "pixiv", "pikabu", "x.com", "twitter"):
        if key in host:
            return key.replace(".com", "") if key != "x.com" else "xcom"
    return "web"


async def _resolve_target_queue(
    source_type: str, source_id: str | None, tags: list[str], explicit_queue_id: int | None,
) -> tuple[int | None, str]:
    """Определяет очередь: явная > правила маршрутизации > Входящие (None).

    Возвращает (queue_id, human-readable название).
    """
    if explicit_queue_id is not None:
        q = await queues_repo.get_queue(explicit_queue_id)
        if q is None:
            raise HTTPException(status_code=404, detail=f"Очередь #{explicit_queue_id} не найдена")
        return q["id"], q["name"]

    routed_qid = await route_post(source_type, source_id, tags)
    if routed_qid is not None:
        q = await queues_repo.get_queue(routed_qid)
        return routed_qid, (q["name"] if q else f"#{routed_qid}")
    return None, "Входящие"


@router.post("/quick_add")
async def quick_add(body: QuickAddRequest, request: Request) -> dict[str, Any]:
    """Режим A (ТЗ §9.5): расширение присылает только ссылки — качает бот.

    Создаёт post(status='new') и задачу download в task_queue; worker Фазы 3
    скачает файл с Referer/UA, посчитает pHash и переведёт в pending.
    """
    _check_auth(body.secret_token, request)

    parsed = urlparse(body.url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Поле 'url' должно быть http(s)-ссылкой")

    media_url = (body.media_url or "").strip() or body.url
    source_type = _normalize_site(body.site, body.url)
    queue_id, queue_name = await _resolve_target_queue(source_type, body.url, body.tags, body.queue_id)

    # Дедупликация по source_id (ТЗ §2.4.2: точный уровень для внешних источников)
    existing = await posts_repo.find_by_source(source_type, media_url)
    if existing:
        p = existing[0]
        return {
            "success": True,
            "duplicate": True,
            "post_id": p["id"],
            "queue_id": p["queue_id"],
            "status": p["status"],
            "message": f"Пост уже в базе (#{p['id']}, статус {p['status']})",
        }

    post_id = await posts_repo.create_post(
        source_type=source_type,
        file_type=_guess_file_type(media_url),
        queue_id=queue_id,
        source_id=media_url,
        source_url=media_url,
        source_channel=body.site or parsed.hostname,
        original_caption=body.title,
        tags=body.tags,
        status="new",
        media_group_id=body.media_group_id,
    )
    await tq_repo.enqueue(
        post_id,
        "download",
        {"media_url": media_url, "page_url": body.url, "referer": body.referer, "site": source_type},
    )
    log.info("quick_add: пост #%s (%s) → очередь '%s'", post_id, source_type, queue_name)
    return {
        "success": True,
        "post_id": post_id,
        "queue_id": queue_id,
        "queue_name": queue_name,
        "message": f"Пост #{post_id} добавлен в очередь '{queue_name}' (ожидает скачивания)",
    }


@router.post("/quick_add_upload")
async def quick_add_upload(
    request: Request,
    file: UploadFile = File(...),
    url: str = Form(""),
    title: str = Form(""),
    tags: str = Form("[]"),
    queue_id: str | None = Form(None),
    secret_token: str | None = Form(None),
    media_group_id: str | None = Form(None),
) -> dict[str, Any]:
    """Режим B (ТЗ §6.1.3, §9.5): расширение скачало файл браузером (cookies x.com)
    и прислало его через multipart/form-data.

    Файл сохраняется в settings.media_dir, pHash считается сразу (файл на диске),
    дедупликация по file_unique_id (md5) и визуальному порогу.
    """
    _check_auth(secret_token, request)

    # Лимит размера (ТЗ §6.1.3: настраивается, по умолчанию 200 МБ)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Файл больше лимита {settings.max_upload_mb} МБ — отправьте ссылку поста боту (yt-dlp)",
        )
    if not data:
        raise HTTPException(status_code=400, detail="Пустой файл")

    # Расширение + имя
    orig_name = file.filename or "upload.bin"
    ext = Path(orig_name).suffix.lower()
    if ext not in _ALLOWED_UPLOAD_EXTS:
        # x.com отдаёт файлы без нормального расширения — пробуем по content_type
        ct = (file.content_type or "").split(";")[0].strip().lower()
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
               "image/gif": ".gif", "video/mp4": ".mp4", "video/webm": ".webm"}.get(ct, ".bin")
        if ext == ".bin":
            raise HTTPException(status_code=400, detail=f"Неподдерживаемый тип файла: {file.content_type}")

    # md5 как стабильный file_unique_id для внешних файлов (exact-дедуп уровня 1)
    import hashlib

    digest = hashlib.md5(data).hexdigest()
    file_unique_id = f"md5:{digest}"
    existing = await posts_repo.find_by_file_unique_id(file_unique_id)
    if existing:
        p = existing[0]
        return {
            "success": True,
            "duplicate": True,
            "post_id": p["id"],
            "queue_id": p["queue_id"],
            "status": p["status"],
            "message": f"Такой файл уже в базе (#{p['id']}, статус {p['status']})",
        }

    try:
        tag_list = json.loads(tags) if tags else []
        if not isinstance(tag_list, list):
            tag_list = []
    except (json.JSONDecodeError, TypeError):
        tag_list = []

    source_type = _normalize_site(None, url) if url else "extension"
    queue_id_int: int | None = None
    if queue_id:
        try:
            queue_id_int = int(queue_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="queue_id должен быть числом") from None
    target_qid, queue_name = await _resolve_target_queue(source_type, url or None, tag_list, queue_id_int)

    # Сохраняем файл в media_dir под id-именем (пост ещё не создан — используем временное
    # уникальное имя из md5: путь всё равно хранится в posts.file_path).
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"ext_{digest[:16]}{ext}"
    file_path = settings.media_dir / stored_name
    file_path.write_bytes(data)

    from app.services.dedup.phash import check_visual_duplicate, compute_and_store_phash

    guessed_type = _guess_file_type(f"file{ext}")
    post_id = await posts_repo.create_post(
        source_type=source_type,
        file_type=guessed_type,
        queue_id=target_qid,
        source_id=url or None,
        source_url=url or None,
        file_path=str(file_path),
        file_unique_id=file_unique_id,
        file_size=len(data),
        mime_type=file.content_type,
        original_caption=title or None,
        tags=tag_list,
        status="pending",
        media_group_id=media_group_id,
    )

    # pHash для изображений: визуальный дедуп (ТЗ §2.4.3)
    final_status = "pending"
    phash = await compute_and_store_phash(post_id, str(file_path), guessed_type)
    if phash is not None:
        dupes = await check_visual_duplicate(phash, exclude_post_id=post_id)
        if dupes:
            final_status = "possible_duplicate"
            await posts_repo.update_status(post_id, final_status)
            await posts_repo.assign_queue(post_id, None)  # спорные — в Входящие на модерацию
            log.warning(
                "quick_add_upload: пост #%s — возможный визуальный дубликат (%s)",
                post_id, [d["post_id"] for d in dupes[:5]],
            )

    message = (
        f"Пост #{post_id} — возможен дубликат, проверьте во «Входящих»"
        if final_status == "possible_duplicate"
        else f"Пост #{post_id} добавлен в очередь '{queue_name}'"
    )
    log.info("quick_add_upload: %s (%d байт)", message, len(data))
    return {
        "success": True,
        "post_id": post_id,
        "queue_id": None if final_status == "possible_duplicate" else target_qid,
        "queue_name": "Входящие" if final_status == "possible_duplicate" else queue_name,
        "status": final_status,
        "message": message,
    }


@router.get("/queues")
async def api_queues(request: Request, secret_token: str | None = None) -> dict[str, Any]:
    """Список активных очередей для popup расширения (?secret_token=... или заголовок)."""
    _check_auth(secret_token, request)
    qs = await queues_repo.list_queues(active_only=False)
    out = []
    for q in qs:
        pending = await queues_repo.count_pending(q["id"])
        out.append({
            "id": q["id"],
            "name": q["name"],
            "channel_name": q.get("channel_name"),
            "interval_minutes": q["interval_minutes"],
            "is_active": bool(q["is_active"]),
            "pending": pending,
        })
    return {"queues": out}


@router.get("/post_status/{post_id}")
async def api_post_status(post_id: int, request: Request, secret_token: str | None = None) -> dict[str, Any]:
    """Статус поста по id (для подтверждения расширения)."""
    _check_auth(secret_token, request)
    p = await posts_repo.get_post(post_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Пост не найден")
    return {
        "post_id": p["id"],
        "status": p["status"],
        "queue_id": p["queue_id"],
        "source_type": p["source_type"],
        "created_at": p["created_at"],
    }
