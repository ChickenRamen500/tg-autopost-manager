"""Сервис маршрутизации (ТЗ §2.2.2).

Алгоритм маршрутизации (ТЗ):
 1. Получить метаданные источника поста
 2. Загрузить все активные правила маршрутизации
 3. Отсортировать по приоритету (от высшего к низшему)
 4. Для каждого правила:
    a. Проверить соответствие source_type ('any' подходит любому)
    b. Проверить соответствие source_pattern (regex ИЛИ точное совпадение)
    c. Совпадение → применить правило, вернуть target_queue_id
 5. Если ни одно правило не подошло → None (пост идёт во «Входящие»)
"""
from __future__ import annotations

import functools
import logging
import re

from app.repositories import routing as routing_repo

log = logging.getLogger("routing")


@functools.lru_cache(maxsize=256)
def _compile_pattern(pattern: str, flags: int) -> re.Pattern:
    """Компилирует и кеширует regex-паттерн (защита от повторной компиляции в hot path)."""
    return re.compile(pattern, flags)


def _matches(pattern: str, value: str | None) -> bool:
    """Проверка совпадения source_pattern со значением.

    Правила определения режима (ТЗ §2.2.2 — regex или точное совпадение):
    - Пустой pattern → не матчит ничего (защита от случайного правила «всё»)
    - Паттерн, выглядящий как regex (содержит спецсимволы . * + ? [ ] ( ) | ^ $ \\ { })
      → трактуется как regex (full match)
    - Иначе → точное совпадение строк

    Защита от ReDoS: ограничиваем длину значения (паттерны из репозитория могут быть
    скомпрометированы и вызвать катастрофический backtracking).
    """
    if not pattern:
        return False
    if value is None:
        return False
    if len(value) > 500:  # защита от ReDoS — слишком длинное значение не матчим
        return False

    if _looks_like_regex(pattern):
        try:
            flags = re.IGNORECASE | re.DOTALL
            return _compile_pattern(pattern, flags).fullmatch(value) is not None
        except re.error as e:
            log.warning("Невалидный regex-паттерн %r: %s — трактуем как точное совпадение", pattern, e)
            return pattern == value
    return pattern == value


_REGEX_HINTS = set(".+*?[](){}|^$\\")


def _looks_like_regex(pattern: str) -> bool:
    """Эвристика: если в паттерне есть спецсимволы regex — считаем его regex."""
    return any(ch in _REGEX_HINTS for ch in pattern)


async def route_post(source_type: str, source_id: str | None, tags: list[str] | None = None) -> int | None:
    """Определяет целевую очередь для поста.

    Возвращает target_queue_id или None (пост → «Входящие», queue_id=NULL).

    source_type  — тип источника (telegram_channel, local_folder, gelbooru, ...).
    source_id    — идентификатор источника (id канала, путь, id поста, URL).
    tags         — теги поста (опционально; для правил по тегам вида '.*rating:safe.*').

    Алгоритм: правила уже отсортированы по priority DESC в репозитории;
    берём первое совпавшее (ТЗ §2.2.2 п.4d «выйти из цикла»).
    """
    rules = await routing_repo.list_rules(active_only=True)
    tag_str = " ".join(tags) if tags else ""

    # Разделяем кандидаты для матчинга: для telegram-источников матчем source_id,
    # для booru/прочих — только теги. Это исключает неоднозначность, когда для
    # booru-поста числовой source_id случайно матчит правило, написанное для канала.
    if source_type in ("telegram_channel", "telegram_user"):
        candidates = [str(source_id)] if source_id else []
    else:
        candidates = [tag_str] if tag_str else []

    for rule in rules:
        rtype = rule["source_type"]
        if rtype != "any" and rtype != source_type:
            continue

        # Для booru часто source_id — числовой id поста, а правило — по тегам:
        # проверяем паттерн против каждой строки-кандидата
        matched = any(_matches(rule["source_pattern"], c) for c in candidates) if candidates else False

        if matched:
            log.info(
                "Маршрутизация: source=%s id=%s → queue_id=%s (rule #%s, priority=%s)",
                source_type, source_id, rule["target_queue_id"], rule["id"], rule["priority"],
            )
            return rule["target_queue_id"]

    log.info("Маршрутизация: правил не найдено для source=%s id=%s → Входящие", source_type, source_id)
    return None
