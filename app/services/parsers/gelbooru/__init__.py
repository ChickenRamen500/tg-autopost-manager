"""Парсер Gelbooru (ТЗ §2.1.4, §10.1 — миграция из gbooru_tg).

Экспорты:
- gelbooru_client — singleton GelbooruClient
- cache           — in-memory TTL кеш
- tags_db         — отдельная БД метаданных тегов (aiosqlite)
- populate_tags   — bulk-наполнение tags.db из API Gelbooru (скрипт populate_tags.py)
"""
from app.services.parsers.gelbooru import cache, tags_db
from app.services.parsers.gelbooru.client import GelbooruClient, gelbooru_client

__all__ = ["GelbooruClient", "cache", "gelbooru_client", "tags_db"]
