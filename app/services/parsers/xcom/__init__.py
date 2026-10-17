"""X.com (Twitter) парсер через yt-dlp (ТЗ §2.1.4, Фаза 3 этап 5).

Требует cookies (Netscape format) для доступа к X.com.
"""
from app.services.parsers.xcom.client import (
    XcomParser,
    close_xcom_parser,
    get_xcom_parser,
    parse_info_dict,
)

__all__ = ["XcomParser", "get_xcom_parser", "close_xcom_parser", "parse_info_dict"]
