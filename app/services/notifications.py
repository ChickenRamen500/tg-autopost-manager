"""Сервис уведомлений (блок C — полировка Фазы 2).

Отправляет уведомления админу в ЛС:
- «Скоро закончится» (pending ≤ threshold)
- «Очередь закончилась» (pending == 0 после отправки)
- Ошибка отправки поста (failed)

Также дублирует в служебный канал (если задан, блок D).
"""
from __future__ import annotations

import logging

from app.config.settings import settings
from app.repositories import app_settings as app_settings_repo

log = logging.getLogger("notifications")


async def notify_admin(text: str) -> None:
    """Отправляет уведомление админу в ЛС + дублирует в служебный канал.

    Не падает при ошибках отправки (уведомления не критичны).
    """
    # 1. В ЛС админу
    if not settings.admin_tg_id:
        return
    try:
        from app.bot.runtime import get_bot

        bot = get_bot()
        await bot.send_message(settings.admin_tg_id, text)
    except Exception as e:  # noqa: BLE001 — не роняем планировщик из-за уведомления
        log.warning("Не удалось отправить уведомление админу: %s", e)

    # 2. В служебный канал (если задан)
    archive_channel = await app_settings_repo.get_archive_channel_id()
    if archive_channel:
        try:
            from app.bot.runtime import get_bot

            bot = get_bot()
            await bot.send_message(archive_channel, f"🔔 {text}")
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось продублировать уведомление в служебный канал: %s", e)


import time

# Throttle: не отправлять уведомление чаще чем раз в N минут для одной очереди
_NOTIFY_THROTTLE_SEC = 300  # 5 минут
_last_notify: dict[int, float] = {}  # queue_id → monotonic timestamp


async def notify_queue_almost_empty(queue_id: int, queue_name: str, channel_name: str, remaining: int) -> None:
    """«Скоро закончится» — pending постов ≤ threshold.

    Throttle: не чаще раза в 5 минут для одной очереди (чтобы не спамить).
    """
    threshold = await app_settings_repo.get_notify_threshold()
    if remaining > threshold:
        return
    # Throttle
    now = time.monotonic()
    last = _last_notify.get(queue_id, 0.0)
    if now - last < _NOTIFY_THROTTLE_SEC:
        return  # уже отправляли недавно
    _last_notify[queue_id] = now

    text = (
        f"⏰ **Очередь скоро закончится**\n"
        f"Очередь #{queue_id} «{queue_name}» в канале «{channel_name}»\n"
        f"Осталось постов: {remaining}"
    )
    await notify_admin(text)


async def notify_queue_finished(queue_id: int, queue_name: str, channel_name: str) -> None:
    """«Очередь закончилась» — последний пост отправлен.

    Throttle: отправляем только ОДИН раз (флаг в _last_notify).
    """
    now = time.monotonic()
    # Если уже отправляли «finished» — не повторяем
    if _last_notify.get(queue_id, 0.0) > 0 and now - _last_notify.get(queue_id, 0.0) < _NOTIFY_THROTTLE_SEC:
        return
    _last_notify[queue_id] = now

    text = (
        f"✅ **Очередь закончилась**\n"
        f"Очередь #{queue_id} «{queue_name}» в канале «{channel_name}» — все посты отправлены."
    )
    await notify_admin(text)


async def notify_send_error(queue_id: int, queue_name: str, post_id: int, error: str) -> None:
    """Ошибка отправки поста (failed после max_attempts)."""
    text = (
        f"❌ **Ошибка отправки поста**\n"
        f"Очередь #{queue_id} «{queue_name}», пост #{post_id}\n"
        f"Ошибка: {error[:200]}"
    )
    await notify_admin(text)
