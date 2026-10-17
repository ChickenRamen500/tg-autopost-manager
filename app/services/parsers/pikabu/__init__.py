"""Pikabu парсер (ТЗ §2.1.4, Фаза 3 этап 5).

Неофициальный AJAX endpoint: https://pikabu.ru/ajax/page.php?action=get_story
Referer обязателен: https://pikabu.ru/
"""
from app.services.parsers.pikabu.client import (
    PikabuParser,
    close_pikabu_parser,
    get_pikabu_parser,
    parse_story_dict,
)

__all__ = ["PikabuParser", "get_pikabu_parser", "close_pikabu_parser", "parse_story_dict"]
