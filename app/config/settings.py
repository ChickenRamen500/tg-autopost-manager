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
        return problems


settings = Settings()
