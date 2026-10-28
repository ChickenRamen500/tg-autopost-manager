"""FastAPI router веб-интерфейса (ТЗ §7, Task 4).

HTML-страницы (Jinja2 + Bootstrap 5) — пути без префикса, корневые:
  GET /            — dashboard
  GET /queues      — список очередей
  GET /queues/{id} — детали очереди с постами
  GET /inbox       — входящие (queue_id IS NULL)
  GET /posts       — все посты с фильтром ?status=
  GET /routing     — правила маршрутизации + форма
  GET /channels    — список каналов

POST-actions (возвращают redirect на referer):
  /web/posts/{id}/assign/{queue_id}   — назначить пост в очередь
  /web/posts/{id}/delete              — удалить пост
  /web/posts/{id}/status              — изменить статус (form: status)
  /web/queues/{id}/toggle             — вкл/выкл очередь
  /web/queues/{id}/delete             — удалить очередь
  /web/routing/create                 — создать правило
  /web/routing/{id}/delete            — удалить правило
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
    except Exception:  # noqa: BLE001 — в тестах worker может быть не создан
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
    return templates.TemplateResponse("pages/dashboard.html", ctx)


@router.get("/queues")
async def page_queues(request: Request, _user: str = Depends(verify_web_user)) -> Any:
    """Список всех очередей."""
    queues_raw = await queues_repo.list_queues(active_only=False)
    queues = []
    for q in queues_raw:
        pending = await queues_repo.count_pending(q["id"])
        queues.append({**q, "pending_count": pending})
    ctx = {
        "request": request,
        "active_page": "queues",
        "queues": queues,
    }
    return templates.TemplateResponse("pages/queues.html", ctx)


@router.get("/queues/{queue_id}")
async def page_queue_detail(
    queue_id: int,
    request: Request,
    _user: str = Depends(verify_web_user),
) -> Any:
    """Детали одной очереди + её посты (limit 100)."""
    q = await queues_repo.get_queue(queue_id)
    if q is None:
        raise HTTPException(status_code=404, detail=f"Очередь #{queue_id} не найдена")
    posts = await posts_repo.list_queue_posts(queue_id, limit=100)
    pending = await queues_repo.count_pending(queue_id)
    ctx = {
        "request": request,
        "active_page": "queues",
        "queue": q,
        "posts": posts,
        "pending_count": pending,
    }
    return templates.TemplateResponse("pages/queue_detail.html", ctx)


@router.get("/inbox")
async def page_inbox(request: Request, _user: str = Depends(verify_web_user)) -> Any:
    """Входящие посты (queue_id IS NULL) + выбор очереди для назначения."""
    inbox_posts = await posts_repo.list_inbox(limit=200)
    queues = await queues_repo.list_queues(active_only=False)
    ctx = {
        "request": request,
        "active_page": "inbox",
        "posts": inbox_posts,
        "queues": queues,
    }
    return templates.TemplateResponse("pages/inbox.html", ctx)


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
    return templates.TemplateResponse("pages/posts.html", ctx)


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
    return templates.TemplateResponse("pages/routing.html", ctx)


@router.get("/channels")
async def page_channels(request: Request, _user: str = Depends(verify_web_user)) -> Any:
    """Список каналов."""
    channels = await channels_repo.list_channels(active_only=False)
    ctx = {
        "request": request,
        "active_page": "channels",
        "channels": channels,
    }
    return templates.TemplateResponse("pages/channels.html", ctx)


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
    """Назначить пост в очередь (queue_id=0 → во Входящие, queue_id=NULL)."""
    target = None if queue_id == 0 else queue_id
    ok = await posts_repo.assign_queue(post_id, target)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Пост #{post_id} не найден")
    log.info("web: пост #%s → очередь %s (user=%s)", post_id, target or "Входящие", _user)
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
