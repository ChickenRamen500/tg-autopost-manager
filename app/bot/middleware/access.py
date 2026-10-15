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

# Кеш tg_id, для которых bootstrap_admin уже выполнил INSERT/UPDATE в этом
# процессе. Избегает вызова bootstrap_admin на каждый апдейт (CR-H-R6) —
# раньше /start и любое сообщение от ADMIN_TG_ID вызывали SELECT+UPDATE.
# Сет не персистится между рестартами — это OK, т.к. после первого старта
# admin уже есть в БД (идемпотентность INSERT ON CONFLICT вернёт False).
_bootstrap_done: set[int] = set()


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

        # Bootstrap: первый admin из .env (ADMIN_TG_ID).
        # Вызываем ТОЛЬКО для /start команды (а не на каждый апдейт) и только
        # если tg_id не в кеше _bootstrap_done — иначе лишний SELECT на каждое
        # сообщение от admin'а (CR-H-R6).
        if settings.admin_tg_id and tg_id == settings.admin_tg_id and tg_id not in _bootstrap_done:
            # Только для /start@botname и bare /start
            if isinstance(event, Message):
                raw_text = event.text or ""
                parts = raw_text.split()
                cmd = parts[0].split("@")[0] if parts else ""
                if cmd in ("/start", "/help"):
                    try:
                        await bootstrap_admin(tg_id, user.username)
                    except Exception as e:  # noqa: BLE001 — не роняем запрос из-за bootstrap
                        logger.warning("bootstrap_admin упал: %s", e)
                    finally:
                        # Добавляем в кеш даже при ошибке — чтобы не зацикливаться
                        # на одной команде (один лог-ворминг на /start, дальше тишина).
                        _bootstrap_done.add(tg_id)

        # Ищем пользователя в БД. try/except — если БД не инициализирована
        # или есть ошибка запроса, логируем и пускаем как гостя (не роняем бота).
        db_user = None
        try:
            db_user = await users_repo.get_user_by_tg_id(tg_id)
        except Exception as e:  # noqa: BLE001 — не роняем запрос
            logger.error("Ошибка поиска пользователя tg_id=%s: %s", tg_id, e)

        if db_user is not None:
            logger.debug(
                "RBAC: tg_id=%s найден, role=%s is_active=%s",
                tg_id, db_user.get("role"), db_user.get("is_active"),
            )
        else:
            logger.debug("RBAC: tg_id=%s НЕ найден в БД", tg_id)

        if db_user is None or not db_user.get("is_active", 0):
            # Гость/неактивный: /start и /help разрешаем, остальное — отказ
            logger.info("RBAC: tg_id=%s — ДОСТУП ЗАКРЫТ (guest/inactive)", tg_id)
            if isinstance(event, Message):
                msg_text = event.text or ""
                parts = msg_text.split()
                # Поддержка /start@botname (CR-M-2): parts[0].split("@")[0]
                cmd = parts[0].split("@")[0] if parts else ""
                if cmd in ("/start", "/help"):
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

        # Обновим username, если изменился. Используем update_username (а не
        # upsert_user) — НЕ трогаем role/is_active (CR-H-R7): раньше вызов
        # upsert_user с db_user["role"] мог молча понизить admin, если в БД
        # между этим вызовом и SELECT'ом сменили роль.
        if user.username and db_user.get("username") != user.username:
            await users_repo.update_username(tg_id, user.username)

        data["user"] = db_user
        data["user_role"] = db_user.get("role")
        return await handler(event, data)
