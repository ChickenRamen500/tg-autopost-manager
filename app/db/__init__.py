"""Пакет доступа к данным (ТЗ §5).

Экспортирует:
- init_db / close_db / get_db  — управление соединением aiosqlite
- bootstrap_admin              — первичная инициализация admin из .env
"""
from app.db.database import bootstrap_admin, close_db, get_db, init_db, transaction

__all__ = ["bootstrap_admin", "close_db", "get_db", "init_db", "transaction"]
