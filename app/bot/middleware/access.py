"""Middleware контроля доступа (RBAC, ТЗ §2.6, §10.1 — база из gbooru_tg/middleware/access.py).

Адаптация:
- Роли: admin / moderator (вместо user/vip/banned/owner из gbooru_tg)
- Источник прав: таблица users новой БД (через app.repositories.users)
- Bootstrap admin: пользователь с tg_id == settings.admin_tg_id при первом /start
  автоматически становится admin (bootstrap_admin), иначе некому добавлять пользователей.
- Гостю (нет в БД) доступ закрыт, но /start разрешён (чтобы bootstrap сработал).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config.settings import settings
from app.db import bootstrap_admin
from app.repositories import users as users_repo

logger = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    """Проверка доступа пользователя по роли перед обработкой события."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Any],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            # Служебные события (channel_post без from_user и т.п.) — пропускаем
            return await handler(event, data)

        tg_id = user.id

        # Bootstrap: первый admin из .env (ADMIN_TG_ID)
        if settings.admin_tg_id and tg_id == settings.admin_tg_id:
            try:
                await bootstrap_admin(tg_id, user.username)
            except Exception as e:  # noqa: BLE001 — не роняем запрос из-за bootstrap
                logger.warning("bootstrap_admin упал: %s", e)

        # Ищем пользователя в БД. try/except — если БД не инициализирована
        # или есть ошибка запроса, логируем и пускаем как гостя (не роняем бота).
        db_user = None
        try:
            db_user = await users_repo.get_user_by_tg_id(tg_id)
        except Exception as e:  # noqa: BLE001 — не роняем запрос
            logger.error("Ошибка поиска пользователя tg_id=%s: %s", tg_id, e)

        if db_user is not None:
            logger.info(
                "RBAC: tg_id=%s найден, role=%s is_active=%s",
                tg_id, db_user.get("role"), db_user.get("is_active"),
            )
        else:
            logger.info("RBAC: tg_id=%s НЕ найден в БД", tg_id)

        if db_user is None or not db_user.get("is_active", 0):
            # Гость/неактивный: /start и /help разрешаем, остальное — отказ
            logger.info("RBAC: tg_id=%s — ДОСТУП ЗАКРЫТ (guest/inactive)", tg_id)
            if isinstance(event, Message):
                text = event.text or ""
                parts = text.split()
                if parts and parts[0] in ("/start", "/help"):
                    data["user"] = None
                    data["user_role"] = None
                    return await handler(event, data)
                await event.answer(
                    "🔒 Доступ закрыт. Обратитесь к администратору бота, чтобы вас добавили "
                    "в систему (роль admin/moderator)."
                )
                return None
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ закрыт.", show_alert=True)
                return None
            return await handler(event, data)

        # Обновим username, если изменился
        if user.username and db_user.get("username") != user.username:
            await users_repo.upsert_user(tg_id, user.username, db_user["role"], is_active=bool(db_user["is_active"]))

        data["user"] = db_user
        data["user_role"] = db_user.get("role")
        return await handler(event, data)
