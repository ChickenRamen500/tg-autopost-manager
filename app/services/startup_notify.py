"""Уведомление о запуске/перезапуске бота (блок вне ТЗ — доработка UX/observability).

Через ``STARTUP_NOTIFY_DELAY_SECONDS`` (по умолчанию 60) после старта приложения
отправляет в служебный канал (archive channel) ДВА сообщения:

1. «Бот запущен, очереди загружены» — сводка по всем очередям: название, канал,
   интервал, активность и распределение постов по статусам (pending/sent/failed/new).
2. Описание последнего коммита GitHub (branch=main) — что было сделано в этой
   сборке. Источник — GitHub REST API ``/repos/{owner}/{repo}/commits/{branch}``.
   Берётся тело сообщения коммита (commit.message).

Оба сообщения дублируются в ЛС админу (как и прочие уведомления через
notify_admin) — на случай если служебный канал не открывают.

Все сбои (нет archive-канала, бот недоступен, GitHub недоступен, нет интернета)
только логируются — модуль никогда не роняет приложение. Если GitHub-коммит
недоступен — второе сообщение всё равно отправляется с пояснением причины.

Функции ``build_startup_summary`` и ``fetch_latest_commit`` вынесены отдельно
и НЕ делают отправку — это позволяет тестировать их без моков Telegram.
"""
from __future__ import annotations

import html
import logging
from typing import Any

from app.config.settings import settings
from app.repositories import app_settings as app_settings_repo
from app.repositories import queues as queues_repo

log = logging.getLogger("startup_notify")

# Статусы, которые показываем в сводке (порядок важен для восприятия).
_SUMMARY_STATUSES = ("pending", "new", "sent", "failed", "duplicate")


async def build_startup_summary() -> str:
    """Формирует HTML-текст сводки по очередям (без отправки).

    Используется как для основного уведомления, так и для тестов.
    """
    queues = await queues_repo.list_queues(active_only=False)
    active = [q for q in queues if q.get("is_active")]
    lines: list[str] = [
        "<b>🟢 Бот запущен, очереди загружены</b>",
        f"📋 Очередей: <b>{len(queues)}</b> (активных: <b>{len(active)}</b>)",
    ]
    if not queues:
        lines.append("\n<i>Очереди не созданы.</i>")
        return "\n".join(lines)

    from app.repositories import posts as posts_repo

    lines.append("")  # пустая строка-разделитель
    for idx, q in enumerate(queues, start=1):
        name = html.escape(str(q.get("name") or f"#{q['id']}"))
        channel_name = q.get("channel_name") or f"#{q['channel_id']}"
        channel_str = html.escape(str(channel_name))
        # username канала (если есть) — для удобства
        ch_username = q.get("channel_username")
        if ch_username:
            channel_str += f" @{html.escape(str(ch_username))}"
        interval = q.get("interval_minutes")
        is_active = bool(q.get("is_active"))
        status_icon = "✅ активна" if is_active else "⏸ на паузе"

        # Распределение постов по статусам
        counts = await posts_repo.count_statuses_for_queue(q["id"])
        parts: list[str] = []
        for st in _SUMMARY_STATUSES:
            cnt = counts.get(st, 0)
            if cnt:
                parts.append(f"{st}={cnt}")
        # posts без очереди (inbox) сюда не входят — это только посты очереди
        total = sum(counts.values())
        posts_str = ", ".join(parts) if parts else "пусто"

        lines.append(
            f"{idx}. <b>{name}</b> → {channel_str}\n"
            f"   каждые {interval} мин · {status_icon} · постов: {total} ({posts_str})"
        )
    return "\n".join(lines)


async def fetch_latest_commit() -> dict[str, Any] | None:
    """Получает последний коммит ветки из GitHub REST API.

    Возвращает dict с ключами: message, sha, author, date — или None при ошибке
    (нет сети, репозиторий недоступен, и т.п.). Без auth (rate limit 60/час —
    достаточно для одного запроса при старте).
    """
    import aiohttp

    repo = settings.github_repo.strip().strip("/")
    branch = settings.github_branch.strip() or "main"
    url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "tg-media-queue-bot/startup-notify",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    log.warning("startup_notify: GitHub API вернул %s для %s", resp.status, url)
                    return None
                data = await resp.json()
        commit = data.get("commit") or {}
        return {
            "message": (commit.get("message") or "").strip(),
            "sha": (data.get("sha") or "")[:7],
            "author": (commit.get("author") or {}).get("name") or (data.get("author") or {}).get("login") or "",
            "date": (commit.get("author") or {}).get("date") or "",
            "html_url": (data.get("html_url") or ""),
        }
    except Exception as e:  # noqa: BLE001 — сеть/GitHub опциональны
        log.warning("startup_notify: не удалось получить коммит из GitHub: %s", e)
        return None


def format_commit_message(commit: dict[str, Any] | None) -> str:
    """Формирует HTML-текст второго сообщения (описание коммита)."""
    if not commit:
        return (
            "<b>📝 Последний коммит</b>\n\n"
            "<i>Не удалось получить описание коммита из GitHub "
            "(нет сети / репозиторий недоступен / rate limit).</i>"
        )
    message = commit.get("message") or "(пустое сообщение)"
    # Если коммит многострочный — берём только заголовок (первую строки),
    # остальное отбрасываем, чтобы сообщение было компактным. Полное описание
    # доступно по html_url.
    # Но пользователь просил «описание с коммита» — оставим целиком, обрезав до
    # лимита Telegram (4096 с запасом).
    if len(message) > 3000:
        message = message[:2997] + "..."
    sha = html.escape(commit.get("sha") or "")
    author = html.escape(commit.get("author") or "")
    date_raw = commit.get("date") or ""
    date_str = html.escape(date_raw.replace("T", " ").replace("Z", " UTC") if date_raw else "")
    url = html.escape(commit.get("html_url") or "")

    lines = [
        "<b>📝 Последний коммит (что сделано в этой сборке)</b>",
        "",
        f"<pre>{html.escape(message)}</pre>",
    ]
    meta_parts: list[str] = []
    if sha:
        meta_parts.append(f"SHA: <code>{sha}</code>")
    if author:
        meta_parts.append(f"Автор: {author}")
    if date_str:
        meta_parts.append(f"Дата: {date_str}")
    if meta_parts:
        lines.append(" · ".join(meta_parts))
    if url:
        lines.append(f"\n🔗 {url}")
    return "\n".join(lines)


async def _send_to_channels(text: str) -> None:
    """Отправляет текст в служебный канал + дублирует в ЛС админу.

    Не падает при ошибках отправки (уведомления не критичны). Если служебный
    канал не задан — отправляет только в ЛС админу.
    """
    if not settings.admin_tg_id and not settings.bot_token:
        return
    try:
        from app.bot.runtime import get_bot

        bot = get_bot()
        # 1. Служебный канал (если задан)
        archive_channel = await app_settings_repo.get_archive_channel_id()
        if archive_channel:
            try:
                await bot.send_message(archive_channel, text)
            except Exception as e:  # noqa: BLE001
                log.warning("startup_notify: не удалось отправить в служебный канал: %s", e)
        # 2. ЛС админу (дублируем, чтобы админ точно заметил)
        if settings.admin_tg_id:
            try:
                await bot.send_message(settings.admin_tg_id, text)
            except Exception as e:  # noqa: BLE001
                log.warning("startup_notify: не удалось отправить админу в ЛС: %s", e)
    except Exception as e:  # noqa: BLE001 — бот может быть ещё не готов
        log.warning("startup_notify: отправка недоступна: %s", e)


async def notify_startup() -> None:
    """Главная функция: собирает сводку + коммит и отправляет ДВА сообщения.

    Вызывается из main.py lifespan с задержкой STARTUP_NOTIFY_DELAY_SECONDS.
    Ошибки не всплывают (никогда не роняет приложение).
    """
    try:
        log.info("startup_notify: подготовка сводки о запуске…")
        # 1. Сводка по очередям
        summary = await build_startup_summary()
        await _send_to_channels(summary)
        # 2. Описание последнего коммита
        commit = await fetch_latest_commit()
        commit_text = format_commit_message(commit)
        await _send_to_channels(commit_text)
        log.info("startup_notify: уведомления о запуске отправлены")
    except Exception as e:  # noqa: BLE001
        log.warning("startup_notify: сбой отправки уведомления о запуске: %s", e)
