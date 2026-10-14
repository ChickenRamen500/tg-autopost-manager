"""Настройки приложения (ТЗ §3.2, §3.3).

Все значения читаются из переменных окружения / .env-файла.
Пример наполнения — в `.env.example`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта: .../app/config/settings.py -> project root
BASE_DIR = Path(__file__).resolve().parent.parent.parent

load_dotenv(BASE_DIR / ".env")


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # --- Telegram ---
    bot_token: str = field(default_factory=lambda: _get("BOT_TOKEN"))
    telegram_api_id: int = field(default_factory=lambda: int(_get("TELEGRAM_API_ID", "0") or 0))
    telegram_api_hash: str = field(default_factory=lambda: _get("TELEGRAM_API_HASH"))

    # --- Local Bot API Server (ТЗ §3.3) ---
    # Внутри docker-сети: http://local-bot-api:8081; на хосте для тестов: http://localhost:8081
    bot_api_url: str = field(default_factory=lambda: _get("BOT_API_URL", "http://local-bot-api:8081"))

    # --- Безопасность (REST API + Chrome-расширение, ТЗ §6, §9) ---
    secret_token: str = field(default_factory=lambda: _get("SECRET_TOKEN"))

    # --- База данных (ТЗ §5) ---
    database_url: str = field(default_factory=lambda: _get("DATABASE_URL", "sqlite:///./data/bot.db"))

    # --- Фаза 2: первичный admin (bootstrap RBAC, ТЗ §2.6) ---
    # tg_id владельца-админа. На первом /start он автоматически становится admin в БД,
    # иначе нельзя добавить пользователей (команда /add_user требует прав admin).
    admin_tg_id: int = field(default_factory=lambda: int(_get("ADMIN_TG_ID", "0") or 0))

    # --- Фаза 2: миграция кода gbooru_tg (ТЗ §10) ---
    # Опциональные ключи Gelbooru API (нужны для автопарсинга подписок — Фаза 3).
    # На Фазе 2 GelbooruClient переносится как библиотека, ключи не обязательны для импорта.
    gelbooru_api_key: str = field(default_factory=lambda: _get("GELBOORU_API_KEY", ""))
    gelbooru_user_id: str = field(default_factory=lambda: _get("GELBOORU_USER_ID", ""))

    # --- Фаза 2: планировщик (ТЗ §2.5) ---
    # Минимальная пауза между отправками в одну очередь (защита от 429, ТЗ §2.5.2 п.3)
    send_throttle_seconds: float = field(default_factory=lambda: float(_get("SEND_THROTTLE_SECONDS", "1.1") or 1.1))
    # Лимит попыток отправки поста до перевода в failed (ТЗ §2.5.2 п.4)
    max_send_attempts: int = field(default_factory=lambda: int(_get("MAX_SEND_ATTEMPTS", "3") or 3))
    # Часовой пояс для daily_window очередей (Asia/Yekaterinburg — TZ пользователя)
    timezone: str = field(default_factory=lambda: _get("TIMEZONE", "Asia/Yekaterinburg"))

    # --- Веб-интерфейс (ТЗ §7) ---
    web_port: int = field(default_factory=lambda: int(_get("WEB_PORT", "8000") or 8000))

    # --- Пути ---
    data_dir: Path = BASE_DIR / "data"
    db_path: Path = data_dir / "bot.db"
    downloads_dir: Path = data_dir / "downloads"   # автоочистка каждые 24 ч (ТЗ §3.1)
    media_dir: Path = Path(_get("MEDIA_DIR", str(data_dir / "media")))  # в Docker: /app/media

    def validate(self) -> list[str]:
        """Возвращает список критических проблем конфигурации."""
        problems = []
        if not self.bot_token:
            problems.append("BOT_TOKEN не задан — бот не сможет работать ни с облачным, ни с локальным API")
        if not self.telegram_api_id or not self.telegram_api_hash:
            problems.append(
                "TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы — local-bot-api не поднимется с флагом --local "
                "(получить на https://my.telegram.org, ТЗ §3.3)"
            )
        if not self.secret_token:
            problems.append("SECRET_TOKEN не задан — REST API и Chrome-расширение будут недоступны (Фаза 4)")
        if not self.admin_tg_id:
            problems.append(
                "ADMIN_TG_ID не задан — никто не сможет стать admin при первом /start "
                "(управление пользователями/каналами/очередями будет недоступно, ТЗ §2.6)"
            )
        return problems


settings = Settings()
