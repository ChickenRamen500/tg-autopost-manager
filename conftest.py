"""Общие фикстуры/хелперы pytest.

- Отключаем автозагрузку внешних pytest-плагинов (в песочнице libtmux-плагин ломает запуск).
  Это означает, что async-тесты НЕ используют pytest-asyncio — вместо этого применяем
  хелпер run_with_db (asyncio.run внутри sync-теста), что совместимо с Фазой 1.
- run_with_db — временную БД в tmp_path для тестов репозиториев (ТЗ §5).
"""
import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
os.environ.setdefault("BOT_TOKEN", "")
os.environ["TELEGRAM_API_ID"] = "123"
os.environ["TELEGRAM_API_HASH"] = "abc"
os.environ["BOT_API_URL"] = "http://localhost:8081"


def run_with_db(tmp_path: Path, coro_func: Callable[[], Awaitable[None]]) -> None:
    """Запускает async-функцию с временной БД в tmp_path.

    Создаёт/инициализирует БД, выполняет coro_func, закрывает БД (всегда).
    Каждое выполнение изолировано в своём event loop (asyncio.run).
    """

    async def _runner() -> None:
        from app.config.settings import settings
        from app.db import database

        object.__setattr__(settings, "data_dir", tmp_path)
        object.__setattr__(settings, "db_path", tmp_path / "test_bot.db")
        await database.init_db()
        try:
            await coro_func()
        finally:
            await database.close_db()

    asyncio.run(_runner())
