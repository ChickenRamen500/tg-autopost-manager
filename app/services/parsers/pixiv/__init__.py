"""Pixiv парсер (ТЗ §2.1.4, Фаза 3 этап 5).

AJAX endpoint: https://www.pixiv.net/ajax/illust/<id>
Referer: https://www.pixiv.net/ — обязателен для i.pximg.net.
Опциональная аутентификация через PHPSESSID.
"""
from app.services.parsers.pixiv.client import (
    PixivParser,
    close_pixiv_parser,
    get_pixiv_parser,
    parse_illust_dict,
)

__all__ = ["PixivParser", "get_pixiv_parser", "close_pixiv_parser", "parse_illust_dict"]
