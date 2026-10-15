"""Общие фикстуры/хелперы pytest.

- Отключаем автозагрузку внешних pytest-плагинов (в песочнице libtmux-плагин ломает запуск).
  Это означает, что async-тесты НЕ используют pytest-asyncio — вместо этого применяем
  хелпер run_with_db (asyncio.run внутри sync-теста), что совместимо с Фазой 1.
- run_with_db — временную БД в tmp_path для тестов репозиториев (ТЗ §5).

PR-40 (H-T5/code review M-12): временно переопределяем data_dir/db_path в Settings
через object.__setattr__ (Settings — frozen dataclass), а mock.patch.object на нём
не работает (FrozenInstanceError). Поэтому вручную сохраняем и восстанавливаем
оригинальные значения в try/finally.
"""
import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

# setdefault — не перекрываем уже заданные значения (тесты могут переопределять env).
os.environ.setdefault("BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_API_ID", "123")
os.environ.setdefault("TELEGRAM_API_HASH", "abc")
os.environ.setdefault("BOT_API_URL", "http://localhost:8081")


def run_with_db(tmp_path: Path, coro_func: Callable[[], Awaitable[None]]) -> None:
    """Запускает async-функцию с временной БД в tmp_path.

    Создаёт/инициализирует БД, выполняет coro_func, закрывает БД (всегда).
    Каждое выполнение изолировано в своём event loop (asyncio.run).

    PR-40: Settings — frozen dataclass, mock.patch.object на нём падает
    (FrozenInstanceError). Поэтому вручную сохраняем/восстанавливаем
    data_dir/db_path через object.__setattr__ в try/finally — это гарантирует,
    что следующий тест получит исходные пути (важно при параллельном запуске
    и для неведения о состоянии между тестами).
    """

    async def _runner() -> None:
        from app.config.settings import settings
        from app.db import database

        _orig_data_dir = settings.data_dir
        _orig_db_path = settings.db_path
        try:
            object.__setattr__(settings, "data_dir", tmp_path)
            object.__setattr__(settings, "db_path", tmp_path / "test_bot.db")
            await database.init_db()
            try:
                await coro_func()
            finally:
                await database.close_db()
        finally:
            # Восстанавливаем оригинальные пути — иначе singleton Settings «течёт»
            # между тестами (следующий тест обратится к tmp_path прошлого теста).
            object.__setattr__(settings, "data_dir", _orig_data_dir)
            object.__setattr__(settings, "db_path", _orig_db_path)

    asyncio.run(_runner())
