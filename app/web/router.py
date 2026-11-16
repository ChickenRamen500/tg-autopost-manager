"""FastAPI router веб-интерфейса (ТЗ §7, Task 4 + Task 8-9).

HTML-страницы (Jinja2 + Bootstrap 5) — пути без префикса, корневые:
  GET /            — dashboard
  GET /queues      — список очередей + форма создания
  GET /queues/{id} — детали очереди с постами + форма редактирования
  GET /inbox       — входящие (queue_id IS NULL)
  GET /posts       — все посты с фильтром ?status=
  GET /routing     — правила маршрутизации + форма
  GET /channels    — список каналов + форма создания + inline-редактирование

POST-actions (возвращают redirect на referer):
  /web/posts/{id}/assign/{queue_id}      — назначить пост в очередь
  /web/posts/{id}/delete                 — удалить пост
  /web/posts/{id}/status                 — изменить статус (form: status)
  /web/queues/create                     — создать очередь (Task 8)
  /web/queues/{id}/edit                   — обновить очередь (Task 8)
  /web/queues/{id}/toggle                — вкл/выкл очередь
  /web/queues/{id}/delete                — удалить очередь
  /web/routing/create                    — создать правило
  /web/routing/{id}/delete               — удалить правило
  /web/channels/create                   — создать канал (Task 8)
  /web/channels/{id}/edit                — обновить канал (Task 8)
  /web/channels/{id}/delete              — удалить канал (Task 8)

JSON endpoints файлового браузера (local_files):
  GET  /web/local_files/list             — список файлов/папок (+флаг sent)
  POST /web/local_files/add              — создать посты из файлов
  GET  /web/local_files/preview          — превью файла
  GET  /web/local_files/serve            — файл по абсолютному пути
  POST /web/local_files/mkdir            — создать подпапку в текущей директории
  POST /web/local_files/move_sent        — переместить отправленные в <root>/sent/
  GET/POST /web/local_files/roots[...]   — управление корневыми папками
  POST /web/local_files/roots/resolve    — переводчик путей: Windows-путь → контейнер
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.repositories import (
    channels as channels_repo,
)
from app.repositories import (
    posts as posts_repo,
)
from app.repositories import (
    queues as queues_repo,
)
from app.repositories import (
    routing as routing_repo,
)
from app.repositories import (
    task_queue as tq_repo,
)
from app.web.deps import verify_web_user

log = logging.getLogger("web")

router = APIRouter(prefix="", tags=["web"])

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Статусы постов для UI (ТЗ §2.3.4)
# ---------------------------------------------------------------------------
POST_STATUSES = ("new", "downloading", "pending", "sent", "failed", "duplicate", "possible_duplicate")


def _redirect_back(request: Request) -> RedirectResponse:
    """После POST — возврат на referer (или на главную)."""
    back = request.headers.get("referer", "/")
    return RedirectResponse(url=back, status_code=303)


def _checkbox(value: str | None) -> bool:
    """HTML checkbox отправляет 'on' если отмечен, и отсутствует если нет.

    FastAPI Form(default=None) получит None при отсутствии поля.
    """
    return value is not None and value.lower() not in ("", "off", "false", "0")


async def _reload_scheduler_safe() -> None:
    """Перерегистрирует job'ы scheduler'а после изменения очередей.

    Без этого новые очереди, созданные через веб-интерфейс, не получают
    APScheduler job → посты в них никогда не отправляются (scheduler просто
    не знает об очереди). Бот вызывает reload_scheduler в /add_queue,
    но веб-роуты раньше этого не делали.
    """
    try:
        from app.scheduler import reload_scheduler
        n = await reload_scheduler()
        log.info("web: scheduler перерегистрирован (%d очередей)", n)
    except Exception as e:  # noqa: BLE001  # noqa: BLE001
        log.warning("web: reload_scheduler не удался: %s", e)


# ---------------------------------------------------------------------------
# HTML страницы
# ---------------------------------------------------------------------------


@router.get("/")
async def page_dashboard(request: Request, _user: str = Depends(verify_web_user)) -> Any:
    """Dashboard: статистика постов, очереди, worker, последние входящие."""
    counts = {s: await posts_repo.count_by_status(s) for s in POST_STATUSES}
    total_posts = await posts_repo.total_count()

    queues_raw = await queues_repo.list_queues(active_only=False)
    queues = []
    for q in queues_raw:
        pending = await queues_repo.count_pending(q["id"])
        queues.append({**q, "pending_count": pending})

    inbox = await posts_repo.list_inbox(limit=5)

    # Worker stats (как в /health/worker)
    worker_stats = {
        "queued": await tq_repo.count_by_status("queued"),
        "in_progress": await tq_repo.count_by_status("in_progress"),
        "done": await tq_repo.count_by_status("done"),
        "error": await tq_repo.count_by_status("error"),
    }
    worker_running = False
    try:
        from app.worker import get_worker

        worker_running = get_worker().running
    except Exception:  # noqa: BLE001  # noqa: BLE001 — в тестах worker может быть не создан
        pass

    ctx = {
        "request": request,
        "active_page": "dashboard",
        "total_posts": total_posts,
        "counts": counts,
        "queues": queues,
        "inbox": inbox,
        "worker_stats": worker_stats,
        "worker_running": worker_running,
    }
    return templates.TemplateResponse(request, "pages/dashboard.html", ctx)


@router.get("/queues")
async def page_queues(request: Request, _user: str = Depends(verify_web_user)) -> Any:
    """Список всех очередей + форма создания."""
    queues_raw = await queues_repo.list_queues(active_only=False)
    queues = []
    for q in queues_raw:
        pending = await queues_repo.count_pending(q["id"])
        queues.append({**q, "pending_count": pending})
    channels = await channels_repo.list_channels(active_only=False)
    ctx = {
        "request": request,
        "active_page": "queues",
        "queues": queues,
        "channels": channels,
    }
    return templates.TemplateResponse(request, "pages/queues.html", ctx)


@router.get("/queues/{queue_id}")
async def page_queue_detail(
    queue_id: int,
    request: Request,
    _user: str = Depends(verify_web_user),
) -> Any:
    """Детали одной очереди + её посты (limit 100) + форма редактирования."""
    q = await queues_repo.get_queue(queue_id)
    if q is None:
        raise HTTPException(status_code=404, detail=f"Очередь #{queue_id} не найдена")
    posts = await posts_repo.list_queue_posts(queue_id, limit=100)
    pending = await queues_repo.count_pending(queue_id)
    channels = await channels_repo.list_channels(active_only=False)
    ctx = {
        "request": request,
        "active_page": "queues",
        "queue": q,
        "posts": posts,
        "pending_count": pending,
        "channels": channels,
    }
    return templates.TemplateResponse(request, "pages/queue_detail.html", ctx)


@router.get("/inbox")
async def page_inbox(request: Request, _user: str = Depends(verify_web_user)) -> Any:
    """Входящие посты (queue_id IS NULL) + выбор очереди для назначения.

    Посты с одинаковым media_group_id группируются в одну строку с указанием
    количества картинок (например «4 фото»). При назначении такой группы
    в очередь — назначаются ВСЕ посты группы одновременно.
    """
    groups = await posts_repo.list_inbox_grouped(limit=200)
    queues = await queues_repo.list_queues(active_only=False)
    ctx = {
        "request": request,
        "active_page": "inbox",
        "groups": groups,
        "queues": queues,
    }
    return templates.TemplateResponse(request, "pages/inbox.html", ctx)


@router.get("/sent")
async def page_sent(request: Request, _user: str = Depends(verify_web_user)) -> Any:
    """Архив отправленных постов (status='sent').

    Сюда попадают посты из активных очередей после успешной отправки в канал.
    При удалении очереди sent посты удаляются (remove_queue), а не засоряют Inbox.
    """
    sent_posts = await posts_repo.list_sent(limit=200)
    queues = await queues_repo.list_queues(active_only=False)
    queues_by_id = {q["id"]: q for q in queues}
    for p in sent_posts:
        p["queue_obj"] = queues_by_id.get(p["queue_id"]) if p["queue_id"] else None
    ctx = {
        "request": request,
        "active_page": "sent",
        "posts": sent_posts,
        "total": len(sent_posts),
    }
    return templates.TemplateResponse(request, "pages/sent.html", ctx)


@router.get("/posts")
async def page_posts(
    request: Request,
    status: str | None = None,
    _user: str = Depends(verify_web_user),
) -> Any:
    """Все посты с опциональным фильтром по статусу."""
    if status and status not in POST_STATUSES:
        status = None
    queues = await queues_repo.list_queues(active_only=False)
    queues_by_id = {q["id"]: q for q in queues}

    if status:
        # list_queue_posts фильтрует по одной очереди, нам нужен глобальный список.
        # Делаем простой запрос через list_inbox (нет) — берём все очереди и inbox.
        # Чтобы не плодить SQL в репозитории (требование: использовать repo-функции),
        # собираем посты по всем очередям + inbox.
        all_posts = []
        for q in queues:
            qs = await posts_repo.list_queue_posts(q["id"], status=status, limit=500)
            for p in qs:
                p["queue_obj"] = queues_by_id.get(p["queue_id"])
            all_posts.extend(qs)
        # Входящие тоже могут иметь любой статус (например, possible_duplicate).
        inbox_posts = await posts_repo.list_inbox(limit=500)
        for p in inbox_posts:
            if p["status"] == status:
                p["queue_obj"] = None
                all_posts.append(p)
    else:
        all_posts = []
        for q in queues:
            qs = await posts_repo.list_queue_posts(q["id"], limit=500)
            for p in qs:
                p["queue_obj"] = queues_by_id.get(p["queue_id"])
            all_posts.extend(qs)
        inbox_posts = await posts_repo.list_inbox(limit=500)
        for p in inbox_posts:
            p["queue_obj"] = None
            all_posts.append(p)

    # Ограничиваем итог выдачи
    all_posts = all_posts[:200]

    ctx = {
        "request": request,
        "active_page": "posts",
        "posts": all_posts,
        "statuses": POST_STATUSES,
        "current_status": status,
    }
    return templates.TemplateResponse(request, "pages/posts.html", ctx)


@router.get("/routing")
async def page_routing(request: Request, _user: str = Depends(verify_web_user)) -> Any:
    """Правила маршрутизации + форма создания."""
    rules = await routing_repo.list_rules(active_only=False)
    queues = await queues_repo.list_queues(active_only=False)
    queues_by_id = {q["id"]: q for q in queues}
    for r in rules:
        r["target_queue_obj"] = queues_by_id.get(r["target_queue_id"])
    ctx = {
        "request": request,
        "active_page": "routing",
        "rules": rules,
        "queues": queues,
        "source_types": [
            ("any", "Любой"),
            ("gelbooru", "Gelbooru"),
            ("danbooru", "Danbooru"),
            ("pixiv", "Pixiv"),
            ("pikabu", "Pikabu"),
            ("xcom", "X.com"),
            ("telegram_channel", "Telegram-канал"),
        ],
    }
    return templates.TemplateResponse(request, "pages/routing.html", ctx)


@router.get("/channels")
async def page_channels(request: Request, _user: str = Depends(verify_web_user)) -> Any:
    """Список каналов."""
    channels = await channels_repo.list_channels(active_only=False)
    ctx = {
        "request": request,
        "active_page": "channels",
        "channels": channels,
    }
    return templates.TemplateResponse(request, "pages/channels.html", ctx)


@router.get("/local_files")
async def page_local_files(request: Request, _user: str = Depends(verify_web_user)) -> Any:
    """Браузер локальных файлов — просмотр медиа, добавление в очереди."""
    queues = await queues_repo.list_queues(active_only=False)
    ctx = {
        "request": request,
        "active_page": "local_files",
        "queues": queues,
    }
    return templates.TemplateResponse(request, "pages/local_files.html", ctx)


# --- JSON endpoints для AJAX из local_files страницы ---


async def _get_allowed_roots() -> list[Path]:
    """Список корневых директорий, доступных для просмотра.

    Источники корней (в порядке приоритета):
    1. settings.media_dir (основная папка)
    2. settings.extra_media_dirs (через запятую в .env)
    3. app_settings: ключ 'web_local_roots' (динамически через веб-интерфейс)
    """
    from app.config.settings import settings
    from app.repositories import app_settings as app_settings_repo

    roots: list[Path] = []
    # 1. Основная media_dir
    roots.append(Path(settings.media_dir).resolve())
    # 2. EXTRA_MEDIA_DIRS из .env
    extra = getattr(settings, "extra_media_dirs", "") or ""
    if extra:
        for part in extra.split(","):
            part = part.strip()
            if part:
                roots.append(Path(part).resolve())
    # 3. Динамические пути из БД (app_settings)
    try:
        dynamic = await app_settings_repo.get("web_local_roots") or ""
        if dynamic:
            for part in dynamic.split("\n"):
                part = part.strip()
                if part:
                    roots.append(Path(part).resolve())
    except Exception:  # noqa: BLE001  # noqa: BLE001 — БД может быть недоступна при инициализации
        pass
    return list(dict.fromkeys(roots))  # dedup preserving order


def _safe_join(base: Path, subpath: str) -> Path | None:
    """Безопасно объединяет base с subpath, предотвращая path traversal."""
    base_resolved = base if base.is_absolute() else base.resolve()
    if not subpath:
        return base_resolved
    parts = [p for p in subpath.replace("\\", "/").split("/") if p and p != ".."]
    result = base_resolved.joinpath(*parts) if parts else base_resolved
    resolved = result.resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        return None
    return resolved


_MEDIA_EXTS_WEB = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif",
                   ".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}


def _guess_ft(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in (".gif", ".gifv"):
        return "gif"
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif"):
        return "photo"
    if ext in (".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v"):
        return "video"
    return "document"


@router.get("/web/local_files/list")
async def web_local_files_list(
    request: Request,
    path: str = "",
    root: str = "",
    sort: str = "name",
    _user: str = Depends(verify_web_user),
) -> dict[str, Any]:
    """JSON: список файлов и поддиректорий с поддержкой сортировки.

    Параметры:
      path — относительный путь внутри корня
      root — индекс корня (0 = media_dir, 1+ = extra)
      sort — сортировка: name (по умолч.), size, type, modified

    Каждый файл содержит флаг "sent": true, если существует пост со
    статусом 'sent', чей file_path совпадает с этим файлом. Фильтрация
    (скрытие) выполняется на клиенте — так тумблер работает без перезапроса,
    а кнопка «Переместить отправленные» знает актуальное количество.
    """
    roots = await _get_allowed_roots()
    if not roots:
        raise HTTPException(status_code=500, detail="Корневые директории не настроены")

    try:
        root_idx = int(root) if root else 0
    except ValueError:
        root_idx = 0
    if root_idx < 0 or root_idx >= len(roots):
        raise HTTPException(status_code=400, detail=f"Неверный индекс корня: {root}")

    base = roots[root_idx]
    target = _safe_join(base, path)
    if target is None:
        raise HTTPException(status_code=400, detail="Недопустимый путь")

    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Директория не найдена: {path}")

    # Пути файлов отправленных постов — для пометки "sent" в браузере.
    sent_paths: set[str] = set()
    try:
        sent_paths = {str(Path(p).resolve()) for p in await posts_repo.get_sent_file_paths()}
    except Exception:  # noqa: BLE001 — БД недоступна: браузер должен работать без пометок
        log.warning("web: не удалось получить sent-пути для local_files list", exc_info=True)

    directories = []
    files = []
    try:
        for entry in target.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                directories.append({"name": entry.name, "modified": entry.stat().st_mtime})
            elif entry.is_file():
                ext = entry.suffix.lower()
                if ext in _MEDIA_EXTS_WEB:
                    stat = entry.stat()
                    try:
                        rel = str(entry.resolve().relative_to(base))
                    except ValueError:
                        rel = entry.name
                    abs_resolved = base.joinpath(rel) if rel != entry.name else entry.resolve()
                    is_sent = (
                        str(abs_resolved) in sent_paths or str(entry.resolve()) in sent_paths
                    )
                    files.append({
                        "name": entry.name,
                        "size": stat.st_size,
                        "ext": ext,
                        "file_type": _guess_ft(entry.name),
                        "relative_path": rel,
                        "modified": stat.st_mtime,
                        "sent": is_sent,
                    })
    except PermissionError:
        raise HTTPException(status_code=403, detail="Нет доступа к директории") from None

    # Сортировка
    reverse = sort.startswith("-")
    sort_key = sort.lstrip("-")
    if sort_key == "size":
        files.sort(key=lambda f: f["size"], reverse=reverse)
        directories.sort(key=lambda d: d["name"].lower(), reverse=reverse)
    elif sort_key == "type":
        files.sort(key=lambda f: (f["file_type"], f["name"].lower()), reverse=reverse)
        directories.sort(key=lambda d: d["name"].lower(), reverse=reverse)
    elif sort_key == "modified":
        files.sort(key=lambda f: f["modified"], reverse=not reverse)  # newest first by default
        directories.sort(key=lambda d: d["modified"], reverse=not reverse)
    else:  # name (default)
        files.sort(key=lambda f: f["name"].lower(), reverse=reverse)
        directories.sort(key=lambda d: d["name"].lower(), reverse=reverse)

    parent_path = ""
    if path:
        parts = path.rstrip("/").split("/")
        parent_path = "/".join(parts[:-1]) if len(parts) > 1 else ""

    roots_info = [{"index": i, "path": str(r), "name": str(r)} for i, r in enumerate(roots)]

    return {
        "path": path,
        "parent": parent_path,
        "root": root_idx,
        "roots": roots_info,
        "directories": directories,
        "files": files,
    }


@router.post("/web/local_files/add")
async def web_local_files_add(
    request: Request,
    _user: str = Depends(verify_web_user),
) -> Any:
    """JSON: создаёт посты из локальных файлов.

    Тело:
      { "files": ["sub/img1.jpg","sub/img2.jpg"], "root": 0, "queue_id": 1,
        "as_album": false, "title": "caption", "tags": [] }

    Если as_album=true — все файлы получают одинаковый media_group_id
    (scheduler отправит их одним сообщением через send_media_group,
    если у очереди group_albums=True).
    """
    body = await request.json()
    files_list = body.get("files") or []
    if not files_list:
        raise HTTPException(status_code=400, detail="files обязателен (массив путей)")

    roots = await _get_allowed_roots()
    if not roots:
        raise HTTPException(status_code=500, detail="Корневые директории не настроены")

    root_idx = body.get("root", 0)
    try:
        root_idx = int(root_idx)
    except (ValueError, TypeError):
        root_idx = 0
    if root_idx < 0 or root_idx >= len(roots):
        raise HTTPException(status_code=400, detail=f"Неверный индекс корня: {root_idx}")
    base = roots[root_idx]

    queue_id = body.get("queue_id")
    title = body.get("title") or None
    tags = body.get("tags") or []
    as_album = bool(body.get("as_album", False))

    # media_group_id для альбома
    media_group_id = None
    if as_album and len(files_list) > 1:
        import time
        media_group_id = f"web:{int(time.time())}"

    queue_name = "Входящие"
    if queue_id is not None:
        q = await queues_repo.get_queue(queue_id)
        if q is None:
            raise HTTPException(status_code=404, detail=f"Очередь #{queue_id} не найдена")
        queue_name = q["name"]

    created_ids = []
    errors = []
    for file_rel in files_list:
        file_rel = (file_rel or "").strip()
        if not file_rel:
            continue
        abs_path = _safe_join(base, file_rel)
        if abs_path is None or not abs_path.is_file():
            errors.append(f"Не найден: {file_rel}")
            continue
        ext = abs_path.suffix.lower()
        if ext not in _MEDIA_EXTS_WEB:
            errors.append(f"Неподдерживаемый тип: {file_rel}")
            continue
        try:
            file_type = _guess_ft(abs_path.name)
            file_size = abs_path.stat().st_size
            post_id = await posts_repo.create_post(
                source_type="local_file",
                file_type=file_type,
                queue_id=queue_id,
                source_id=str(abs_path),
                source_url=str(abs_path),
                file_path=str(abs_path),
                file_size=file_size,
                original_caption=title,
                tags=tags,
                status="pending",
                media_group_id=media_group_id,
            )
            created_ids.append(post_id)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{file_rel}: {e}")

    log.info("web: local_files → %d постов (album=%s, queue='%s', user=%s)",
             len(created_ids), as_album, queue_name, _user)
    return {
        "success": len(created_ids) > 0,
        "created": len(created_ids),
        "post_ids": created_ids,
        "errors": errors,
        "media_group_id": media_group_id,
        "queue_name": queue_name,
    }


@router.get("/web/local_files/preview")
async def web_local_files_preview(
    request: Request,
    path: str = "",
    root: str = "",
    _user: str = Depends(verify_web_user),
) -> Any:
    """Отдаёт файл для предпросмотра (изображение/видео)."""
    from fastapi.responses import FileResponse

    roots = await _get_allowed_roots()
    if not roots:
        raise HTTPException(status_code=500, detail="Корневые директории не настроены")

    try:
        root_idx = int(root) if root else 0
    except ValueError:
        root_idx = 0
    if root_idx < 0 or root_idx >= len(roots):
        raise HTTPException(status_code=400, detail=f"Неверный индекс корня: {root}")

    base = roots[root_idx]
    abs_path = _safe_join(base, path)
    if abs_path is None or not abs_path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    return FileResponse(str(abs_path))


@router.get("/web/local_files/serve")
async def web_local_files_serve(
    request: Request,
    path: str = "",
    _user: str = Depends(verify_web_user),
) -> Any:
    """Отдаёт файл по абсолютному пути (для ссылок в posts/queue_detail).

    Ищет файл во ВСЕХ корнях. Если найден — отдаёт через FileResponse.
    Это решает проблему: посты хранят абсолютный путь /app/data/media/...,
    а веб-ссылки ведут на /web/local_files/serve?path=/app/data/media/...
    """
    from fastapi.responses import FileResponse

    if not path:
        raise HTTPException(status_code=400, detail="path обязателен")

    # Проверяем, что путь находится внутри одного из корней
    roots = await _get_allowed_roots()
    try:
        target = Path(path).resolve()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Неверный путь") from None

    allowed = False
    for root in roots:
        try:
            target.relative_to(root)
            allowed = True
            break
        except ValueError:
            continue

    if not allowed:
        raise HTTPException(status_code=403, detail="Файл вне разрешённых директорий")

    if not target.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    return FileResponse(str(target))


@router.post("/web/local_files/mkdir")
async def web_local_files_mkdir(
    request: Request,
    _user: str = Depends(verify_web_user),
) -> dict[str, Any]:
    """JSON: создаёт подпапку в текущей директории браузера файлов.

    Тело: { "root": 0, "path": "sub/dir", "name": "Новая папка" }
    path — текущая директория относительно корня ("" = корень).
    name — имя новой папки (без слешей, без "..", не начинается с ".").
    """
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name обязателен")
    if "/" in name or "\\" in name or name == ".." or name.startswith("."):
        raise HTTPException(
            status_code=400,
            detail="Недопустимое имя папки: слеши, '..' и точки в начале запрещены",
        )

    roots = await _get_allowed_roots()
    if not roots:
        raise HTTPException(status_code=500, detail="Корневые директории не настроены")

    root_idx = body.get("root", 0)
    try:
        root_idx = int(root_idx)
    except (ValueError, TypeError):
        root_idx = 0
    if root_idx < 0 or root_idx >= len(roots):
        raise HTTPException(status_code=400, detail=f"Неверный индекс корня: {root_idx}")

    base = roots[root_idx]
    parent = _safe_join(base, body.get("path") or "")
    if parent is None or not parent.is_dir():
        raise HTTPException(status_code=404, detail="Родительская директория не найдена")

    new_dir = _safe_join(parent, name)
    if new_dir is None:
        raise HTTPException(status_code=400, detail="Недопустимый путь")
    if new_dir.exists():
        raise HTTPException(status_code=409, detail=f"Уже существует: {new_dir.name}")

    try:
        new_dir.mkdir(parents=True)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Не удалось создать папку: {e}") from None

    log.info("web: создана папка %s (user=%s)", new_dir, _user)
    return {"success": True, "name": name, "path": str(new_dir)}


@router.post("/web/local_files/move_sent")
async def web_local_files_move_sent(
    request: Request,
    _user: str = Depends(verify_web_user),
) -> dict[str, Any]:
    """JSON: перемещает отправленные файлы в папку <root>/sent/.

    Тело: { "root": 0, "files": ["sub/img1.jpg", "img2.mp4"] }

    Правила:
    - Перемещаются только файлы, у которых есть пост со статусом 'sent'
      с совпадающим file_path (защита от случайного перемещения файлов
      из очереди).
    - Структура подпапок сохраняется: <root>/a/b/img.jpg → <root>/sent/a/b/img.jpg.
    - Конфликты имён решаются суффиксом _1, _2, ...
    - Все посты, ссылающиеся на файл, перепривязываются на новый путь
      (posts.file_path), чтобы scheduler не потерял файл.

    Возвращает { moved, errors, repointed }.
    """
    body = await request.json()
    files_list = body.get("files") or []
    if not files_list:
        raise HTTPException(status_code=400, detail="files обязателен (массив путей)")

    roots = await _get_allowed_roots()
    if not roots:
        raise HTTPException(status_code=500, detail="Корневые директории не настроены")

    root_idx = body.get("root", 0)
    try:
        root_idx = int(root_idx)
    except (ValueError, TypeError):
        root_idx = 0
    if root_idx < 0 or root_idx >= len(roots):
        raise HTTPException(status_code=400, detail=f"Неверный индекс корня: {root_idx}")
    base = roots[root_idx]

    # Актуальные sent-пути — сверяем каждый файл перед перемещением
    sent_paths: set[str] = set()
    try:
        sent_paths = {str(Path(p).resolve()) for p in await posts_repo.get_sent_file_paths()}
    except Exception:  # noqa: BLE001
        log.warning("web: не удалось получить sent-пути для move_sent", exc_info=True)

    sent_root = _safe_join(base, "sent")
    if sent_root is None:
        raise HTTPException(status_code=400, detail="Недопустимый путь для папки sent")

    moved = 0
    repointed_total = 0
    errors: list[str] = []
    for file_rel in files_list:
        file_rel = (file_rel or "").strip()
        if not file_rel:
            continue
        src = _safe_join(base, file_rel)
        if src is None or not src.is_file():
            errors.append(f"Не найден: {file_rel}")
            continue

        try:
            rel_norm = src.relative_to(base)
        except ValueError:
            errors.append(f"Вне корня: {file_rel}")
            continue

        # Уже в sent/ — пропускаем без ошибки (идемпотентность повторных кликов)
        if rel_norm.parts[0] == "sent":
            continue

        if str(src.resolve()) not in sent_paths:
            errors.append(f"Нет отправленного поста для: {file_rel}")
            continue

        # Целевая папка: <root>/sent/<относительный путь родителя>
        dest_dir = _safe_join(sent_root, str(rel_norm.parent)) if str(rel_norm.parent) != "." else sent_root
        if dest_dir is None:
            errors.append(f"Недопустимый целевой путь: {file_rel}")
            continue
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            errors.append(f"{file_rel}: {e}")
            continue

        # Разрешение конфликтов имён: img.jpg → img_1.jpg, img_2.jpg ...
        dest = dest_dir / src.name
        if dest.exists():
            stem, suffix = src.stem, src.suffix
            for i in range(1, 1000):
                candidate = dest_dir / f"{stem}_{i}{suffix}"
                if not candidate.exists():
                    dest = candidate
                    break
            else:
                errors.append(f"Не удалось подобрать имя для: {file_rel}")
                continue

        try:
            src.rename(dest)
        except OSError as e:
            errors.append(f"{file_rel}: {e}")
            continue

        moved += 1
        # Перепривязываем посты на новый путь (старая строка и resolved-вариант)
        old_abs = str(src)
        old_resolved = str(src.resolve())
        new_abs = str(dest)
        repointed_total += await posts_repo.repoint_file_path(old_abs, new_abs)
        if old_resolved != old_abs:
            repointed_total += await posts_repo.repoint_file_path(old_resolved, new_abs)

    log.info(
        "web: move_sent → перемещено %d файлов, перепривязано %d постов, ошибок %d (user=%s)",
        moved, repointed_total, len(errors), _user,
    )
    return {"success": moved > 0, "moved": moved, "repointed": repointed_total, "errors": errors}


# --- Управление корневыми директориями (динамически через веб) ---


@router.get("/web/local_files/roots")
async def web_local_files_roots(
    request: Request,
    _user: str = Depends(verify_web_user),
) -> dict[str, Any]:
    """Список всех корневых директорий (для UI переключателя)."""
    roots = await _get_allowed_roots()
    return {"roots": [{"index": i, "path": str(r), "name": str(r)} for i, r in enumerate(roots)]}


@router.post("/web/local_files/roots/resolve")
async def web_local_files_roots_resolve(
    request: Request,
    _user: str = Depends(verify_web_user),
) -> dict[str, Any]:
    """Переводчик путей: Windows-путь → путь(и) внутри контейнера.

    Тело: { "path": "N:\\media_saver_bot\\saved_files" }

    Пользователь знает только пути своего ПК (сетевые диски M:/N: могут
    указывать на C:/D: сервера) — бот ищет папку ПО ХВОСТУ пути среди всех
    примонтированных директорий (настроенные корни + точки монтирования +
    well-known /mnt, /host_mnt, ...). Буква диска и способ монтирования
    не имеют значения.

    Возвращает:
      { "windows_path": ..., "exists_as_is": bool,
        "matches": [{ "container_path", "root" }, ...] }
    """
    from app.services.path_resolver import resolve_windows_path

    body = await request.json()
    raw = (body.get("path") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="path обязателен")

    # Путь уже существует в контейнере как есть (обычный Linux-путь) —
    # поиск не нужен, единственное совпадение — сам путь.
    try:
        p = Path(raw)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Неверный путь") from None
    if p.is_dir():
        return {
            "windows_path": raw,
            "exists_as_is": True,
            "matches": [{"container_path": str(p.resolve()), "root": str(p.resolve())}],
        }

    roots = await _get_allowed_roots()
    matches = await resolve_windows_path(raw, roots)
    log.info("web: resolve path %r → %d совпадений (user=%s)", raw, len(matches), _user)
    return {"windows_path": raw, "exists_as_is": False, "matches": matches}


@router.post("/web/local_files/roots/add")
async def web_local_files_roots_add(
    request: Request,
    _user: str = Depends(verify_web_user),
) -> Any:
    """Добавляет новую корневую директорию (динамически).

    Тело: { "path": "/mnt/saved_files" } или { "path": "N:\\media_saver_bot\\saved_files" }

    Если путь не существует в контейнере как есть — запускается «переводчик»:
    папка ищется по хвосту пути среди всех примонтированных директорий.
    Найдено ровно одно совпадение → добавляем его. Несколько → возвращаем
    needs_choice=true со списком кандидатов (UI предложит выбрать).
    Сохраняется в app_settings (ключ 'web_local_roots'), newline-separated.
    """
    from app.repositories import app_settings as app_settings_repo
    from app.services.path_resolver import resolve_windows_path

    body = await request.json()
    new_path = (body.get("path") or "").strip()
    if not new_path:
        raise HTTPException(status_code=400, detail="path обязателен")

    try:
        p = Path(new_path)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Неверный путь") from None

    # --- Переводчик путей (Windows-путь → путь контейнера) ---
    if not p.is_dir():
        roots_cfg = await _get_allowed_roots()
        matches = await resolve_windows_path(new_path, roots_cfg)
        if len(matches) == 1:
            p = Path(matches[0]["container_path"])
            log.info("web: путь %r найден по хвосту: %s", new_path, p)
        elif len(matches) > 1:
            log.info("web: путь %r неоднозначен (%d кандидатов) — выбор пользователя", new_path, len(matches))
            return {
                "success": False,
                "needs_choice": True,
                "windows_path": new_path,
                "matches": matches,
            }
        else:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Директория «{new_path}» не найдена ни в одной примонтированной папке. "
                    f"Проверьте, что диск/папка смонтирована в Docker (volumes в docker-compose.yml), "
                    f"или укажите путь внутри контейнера."
                ),
            ) from None

    if not p.is_dir():
        raise HTTPException(status_code=404, detail=f"Директория не существует: {p}")

    resolved = str(p.resolve())

    # Читаем текущие пути из БД
    current = app_settings_repo.get_setting("web_local_roots") or ""
    paths = [line.strip() for line in current.split("\n") if line.strip()]
    if resolved not in paths:
        paths.append(resolved)
        app_settings_repo.set_setting("web_local_roots", "\n".join(paths))
        log.info("web: добавлен корень local_files: %s (user=%s)", resolved, _user)
    return {"success": True, "roots": _get_allowed_roots_list()}


@router.post("/web/local_files/roots/remove")
async def web_local_files_roots_remove(
    request: Request,
    _user: str = Depends(verify_web_user),
) -> Any:
    """Удаляет корневую директорию (динамически).

    Тело: { "path": "/mnt/saved_files" }
    """
    from app.repositories import app_settings as app_settings_repo

    body = await request.json()
    rm_path = (body.get("path") or "").strip()
    if not rm_path:
        raise HTTPException(status_code=400, detail="path обязателен")

    try:
        p = Path(rm_path).resolve()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Неверный путь") from None

    current = app_settings_repo.get_setting("web_local_roots") or ""
    paths = [line.strip() for line in current.split("\n") if line.strip()]
    if str(p) in paths:
        paths.remove(str(p))
        app_settings_repo.set_setting("web_local_roots", "\n".join(paths))
        log.info("web: удалён корень local_files: %s (user=%s)", p, _user)
    return {"success": True, "roots": _get_allowed_roots_list()}


async def _get_allowed_roots_list() -> list[dict]:
    """Возвращает список корней в формате для JSON."""
    return [{"index": i, "path": str(r), "name": str(r)} for i, r in enumerate(await _get_allowed_roots())]


# ---------------------------------------------------------------------------
# POST-actions (redirect after submit)
# ---------------------------------------------------------------------------


@router.post("/web/posts/{post_id}/assign/{queue_id}")
async def action_assign_post(
    post_id: int,
    queue_id: int,
    request: Request,
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    """Назначить пост в очередь (queue_id=0 → во Входящие, queue_id=NULL).

    Если у поста есть media_group_id — назначаем ВСЕ посты с тем же
    media_group_id (чтобы альбом не дробился по разным очередям).
    """
    post = await posts_repo.get_post(post_id)
    if post is None:
        raise HTTPException(status_code=404, detail=f"Пост #{post_id} не найден")

    target = None if queue_id == 0 else queue_id
    mgid = post.get("media_group_id")
    if mgid:
        # Группа: назначаем все посты с этим media_group_id
        group_posts = await posts_repo.find_posts_by_media_group_id(mgid)
        count = 0
        for p in group_posts:
            await posts_repo.assign_queue(p["id"], target)
            count += 1
        log.info(
            "web: группа %s (%d постов) → очередь %s (user=%s)",
            mgid, count, target or "Входящие", _user,
        )
    else:
        # Одиночный пост
        await posts_repo.assign_queue(post_id, target)
        log.info("web: пост #%s → очередь %s (user=%s)", post_id, target or "Входящие", _user)
    return _redirect_back(request)


@router.post("/web/group/{media_group_id}/assign/{queue_id}")
async def action_assign_group(
    media_group_id: str,
    queue_id: int,
    request: Request,
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    """Назначить ВСЕ посты альбома (по media_group_id) в очередь."""
    target = None if queue_id == 0 else queue_id
    group_posts = await posts_repo.find_posts_by_media_group_id(media_group_id)
    if not group_posts:
        raise HTTPException(status_code=404, detail=f"Группа {media_group_id} не найдена")
    count = 0
    for p in group_posts:
        await posts_repo.assign_queue(p["id"], target)
        count += 1
    log.info(
        "web: группа %s (%d постов) → очередь %s (user=%s)",
        media_group_id, count, target or "Входящие", _user,
    )
    return _redirect_back(request)


@router.post("/web/group/{media_group_id}/delete")
async def action_delete_group(
    media_group_id: str,
    request: Request,
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    """Удалить ВСЕ посты альбома (по media_group_id)."""
    group_posts = await posts_repo.find_posts_by_media_group_id(media_group_id)
    if not group_posts:
        raise HTTPException(status_code=404, detail=f"Группа {media_group_id} не найдена")
    count = 0
    for p in group_posts:
        await posts_repo.delete_post(p["id"])
        count += 1
    log.info("web: удалена группа %s (%d постов) (user=%s)", media_group_id, count, _user)
    return _redirect_back(request)


@router.post("/web/posts/{post_id}/delete")
async def action_delete_post(
    post_id: int,
    request: Request,
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    ok = await posts_repo.delete_post(post_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Пост #{post_id} не найден")
    log.info("web: удалён пост #%s (user=%s)", post_id, _user)
    return _redirect_back(request)


@router.post("/web/posts/{post_id}/status")
async def action_change_status(
    post_id: int,
    request: Request,
    status: str = Form(...),
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    if status not in POST_STATUSES:
        raise HTTPException(status_code=400, detail=f"Недопустимый статус: {status!r}")
    ok = await posts_repo.update_status(post_id, status)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Пост #{post_id} не найден")
    log.info("web: пост #%s → статус %s (user=%s)", post_id, status, _user)
    return _redirect_back(request)


@router.post("/web/queues/{queue_id}/toggle")
async def action_toggle_queue(
    queue_id: int,
    request: Request,
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    q = await queues_repo.get_queue(queue_id)
    if q is None:
        raise HTTPException(status_code=404, detail=f"Очередь #{queue_id} не найдена")
    new_state = not bool(q["is_active"])
    await queues_repo.set_active(queue_id, new_state)
    log.info("web: очередь #%s is_active=%s (user=%s)", queue_id, new_state, _user)
    await _reload_scheduler_safe()
    return _redirect_back(request)


@router.post("/web/queues/{queue_id}/delete")
async def action_delete_queue(
    queue_id: int,
    request: Request,
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    ok = await queues_repo.remove_queue(queue_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Очередь #{queue_id} не найдена")
    log.info("web: удалена очередь #%s (user=%s)", queue_id, _user)
    await _reload_scheduler_safe()
    # Удаление очереди — возвращаем на /queues, а не на referer (там удалённой очереди уже нет)
    return RedirectResponse(url="/queues", status_code=303)


@router.post("/web/routing/create")
async def action_create_rule(
    request: Request,
    source_type: str = Form(...),
    source_pattern: str = Form(...),
    target_queue_id: int = Form(...),
    priority: int = Form(0),
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    if not source_pattern.strip():
        raise HTTPException(status_code=400, detail="source_pattern не может быть пустым")
    # Проверяем что target_queue существует
    q = await queues_repo.get_queue(target_queue_id)
    if q is None:
        raise HTTPException(status_code=404, detail=f"Очередь #{target_queue_id} не найдена")
    await routing_repo.create_rule(
        source_type=source_type.strip(),
        source_pattern=source_pattern.strip(),
        target_queue_id=target_queue_id,
        priority=priority,
        is_active=True,
    )
    log.info(
        "web: создано правило source=%s pattern=%r → queue #%s (user=%s)",
        source_type, source_pattern, target_queue_id, _user,
    )
    return _redirect_back(request)


@router.post("/web/routing/{rule_id}/delete")
async def action_delete_rule(
    rule_id: int,
    request: Request,
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    ok = await routing_repo.remove_rule(rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Правило #{rule_id} не найдено")
    log.info("web: удалено правило #%s (user=%s)", rule_id, _user)
    return _redirect_back(request)


# ---------------------------------------------------------------------------
# Queue CRUD (Task 8) — create / edit
# ---------------------------------------------------------------------------


@router.post("/web/queues/create")
async def action_create_queue(
    request: Request,
    name: str = Form(...),
    channel_id: int = Form(...),
    interval_minutes: int = Form(60),
    is_active: str | None = Form(None),
    crop_video: str | None = Form(None),
    shuffle_enabled: str | None = Form(None),
    group_albums: str | None = Form(None),
    strip_caption: str | None = Form(None),
    signature_template: str | None = Form(None),
    start_at: str | None = Form(None),
    end_at: str | None = Form(None),
    daily_window_start: str | None = Form(None),
    daily_window_end: str | None = Form(None),
    end_strategy: str | None = Form(None),
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Имя очереди не может быть пустым")
    if interval_minutes < 1:
        raise HTTPException(
            status_code=400,
            detail=f"interval_minutes должен быть >= 1, получено {interval_minutes}",
        )
    # Проверяем что канал существует
    ch = await channels_repo.get_channel(channel_id)
    if ch is None:
        raise HTTPException(
            status_code=404, detail=f"Канал #{channel_id} не найден"
        )
    qid = await queues_repo.create_queue(
        channel_id=channel_id,
        name=name,
        interval_minutes=interval_minutes,
        is_active=_checkbox(is_active),
        crop_video=_checkbox(crop_video),
        shuffle_enabled=_checkbox(shuffle_enabled),
        group_albums=_checkbox(group_albums),
        strip_caption=_checkbox(strip_caption),
        signature_template=(signature_template or None),
        start_at=(start_at or None),
        end_at=(end_at or None),
        daily_window_start=(daily_window_start or None),
        daily_window_end=(daily_window_end or None),
        end_strategy=(end_strategy or "continue_next_day"),
    )
    log.info(
        "web: создана очередь #%s name=%r channel_id=%s interval=%s (user=%s)",
        qid, name, channel_id, interval_minutes, _user,
    )
    await _reload_scheduler_safe()
    return RedirectResponse(url=f"/queues/{qid}", status_code=303)


@router.post("/web/queues/{queue_id}/edit")
async def action_edit_queue(
    queue_id: int,
    request: Request,
    name: str = Form(...),
    channel_id: int = Form(...),
    interval_minutes: int = Form(60),
    is_active: str | None = Form(None),
    crop_video: str | None = Form(None),
    shuffle_enabled: str | None = Form(None),
    group_albums: str | None = Form(None),
    strip_caption: str | None = Form(None),
    signature_template: str | None = Form(None),
    start_at: str | None = Form(None),
    end_at: str | None = Form(None),
    daily_window_start: str | None = Form(None),
    daily_window_end: str | None = Form(None),
    end_strategy: str | None = Form(None),
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    q = await queues_repo.get_queue(queue_id)
    if q is None:
        raise HTTPException(status_code=404, detail=f"Очередь #{queue_id} не найдена")
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Имя очереди не может быть пустым")
    if interval_minutes < 1:
        raise HTTPException(
            status_code=400,
            detail=f"interval_minutes должен быть >= 1, получено {interval_minutes}",
        )
    ch = await channels_repo.get_channel(channel_id)
    if ch is None:
        raise HTTPException(
            status_code=404, detail=f"Канал #{channel_id} не найден"
        )
    await queues_repo.update_queue(
        queue_id,
        name=name,
        channel_id=channel_id,
        interval_minutes=interval_minutes,
        is_active=_checkbox(is_active),
        crop_video=_checkbox(crop_video),
        shuffle_enabled=_checkbox(shuffle_enabled),
        group_albums=_checkbox(group_albums),
        strip_caption=_checkbox(strip_caption),
        signature_template=(signature_template or None),
        start_at=(start_at or None),
        end_at=(end_at or None),
        daily_window_start=(daily_window_start or None),
        daily_window_end=(daily_window_end or None),
        end_strategy=(end_strategy or "continue_next_day"),
    )
    log.info(
        "web: обновлена очередь #%s name=%r channel_id=%s interval=%s (user=%s)",
        queue_id, name, channel_id, interval_minutes, _user,
    )
    await _reload_scheduler_safe()
    return RedirectResponse(url=f"/queues/{queue_id}", status_code=303)


# ---------------------------------------------------------------------------
# Channel CRUD (Task 8) — create / edit / delete
# ---------------------------------------------------------------------------


@router.post("/web/channels/create")
async def action_create_channel(
    request: Request,
    name: str = Form(...),
    tg_chat_id: int = Form(...),
    username: str = Form(""),
    is_active: str | None = Form(None),
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Имя канала не может быть пустым")
    username = (username or "").strip()
    try:
        cid = await channels_repo.create_channel(
            tg_chat_id=tg_chat_id,
            name=name,
            username=username or None,
            is_active=_checkbox(is_active),
        )
    except channels_repo.DuplicateChannelError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Канал с tg_chat_id={e.tg_chat_id} уже существует",
        ) from e
    log.info(
        "web: создан канал #%s name=%r tg_chat_id=%s (user=%s)",
        cid, name, tg_chat_id, _user,
    )
    return RedirectResponse(url="/channels", status_code=303)


@router.post("/web/channels/{channel_id}/edit")
async def action_edit_channel(
    channel_id: int,
    request: Request,
    name: str = Form(...),
    tg_chat_id: int = Form(...),
    username: str = Form(""),
    is_active: str | None = Form(None),
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    ch = await channels_repo.get_channel(channel_id)
    if ch is None:
        raise HTTPException(
            status_code=404, detail=f"Канал #{channel_id} не найден"
        )
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Имя канала не может быть пустым")
    username = (username or "").strip()
    try:
        await channels_repo.update_channel(
            channel_id,
            name=name,
            tg_chat_id=tg_chat_id,
            username=username or "",
            is_active=_checkbox(is_active),
        )
    except channels_repo.DuplicateChannelError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Канал с tg_chat_id={e.tg_chat_id} уже существует",
        ) from e
    log.info(
        "web: обновлён канал #%s name=%r tg_chat_id=%s (user=%s)",
        channel_id, name, tg_chat_id, _user,
    )
    return _redirect_back(request)


@router.post("/web/channels/{channel_id}/delete")
async def action_delete_channel(
    channel_id: int,
    request: Request,
    _user: str = Depends(verify_web_user),
) -> RedirectResponse:
    ok = await channels_repo.remove_channel(channel_id)
    if not ok:
        raise HTTPException(
            status_code=404, detail=f"Канал #{channel_id} не найден"
        )
    log.info("web: удалён канал #%s (user=%s)", channel_id, _user)
    # Удаление канала каскадно сносит очереди (FK ON DELETE CASCADE) —
    # возвращаемся на /channels (referere может быть на удалённой странице канала).
    return RedirectResponse(url="/channels", status_code=303)
