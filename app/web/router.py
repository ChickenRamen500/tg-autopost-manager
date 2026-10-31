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
    """Входящие посты (queue_id IS NULL) + выбор очереди для назначения."""
    inbox_posts = await posts_repo.list_inbox(limit=200)
    queues = await queues_repo.list_queues(active_only=False)
    ctx = {
        "request": request,
        "active_page": "inbox",
        "posts": inbox_posts,
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
    )
    log.info(
        "web: создана очередь #%s name=%r channel_id=%s interval=%s (user=%s)",
        qid, name, channel_id, interval_minutes, _user,
    )
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
    )
    log.info(
        "web: обновлена очередь #%s name=%r channel_id=%s interval=%s (user=%s)",
        queue_id, name, channel_id, interval_minutes, _user,
    )
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
