"""Тесты клавиатур (builder.py) — выделено из test_regression.py (PR-40).

Покрывает app/bot/keyboards/builder.py:
  - source_type_picker, rule_pick_queue, rules_list, rule_actions
  - post_card (с кнопкой удаления)
  - need_queue_menu (кнопка «Создать очередь»)
  - pick_channel_for_post, pick_queue_for_post
  - ROUTING_SOURCE_TYPES (без telegram_direct / telegram_user)
"""
from __future__ import annotations

import pytest


def test_keyboards_build_routing():
    """Клавиатуры для routing rules строятся без ошибок."""
    from app.bot.keyboards import builder as kb

    st = kb.source_type_picker()
    assert st is not None
    assert len(st.inline_keyboard) > 0

    qpk = kb.rule_pick_queue([{"id": 1, "name": "Test"}])
    assert qpk is not None

    rl = kb.rules_list(
        [{"id": 1, "source_type": "any", "source_pattern": ".*", "target_queue_id": 1, "priority": 0, "is_active": 1}],
        {1: "Test"},
    )
    assert rl is not None

    ra = kb.rule_actions(1)
    assert ra is not None

    # add_menu должен содержать кнопку «🔀 Правило»
    am = kb.add_menu()
    buttons_text = [b.text for row in am.inline_keyboard for b in row]
    assert any("🔀" in t for t in buttons_text), f"Нет кнопки Маршрутизация в add_menu: {buttons_text}"


def test_post_card_has_delete_button():
    """Карточка поста содержит кнопку «🗑 Удалить пост» (для очистки дубликатов)."""
    from app.bot.keyboards import builder as kb

    post = {
        "id": 42,
        "status": "possible_duplicate",
        "queue_id": None,  # во Входящих
        "file_type": "photo",
    }
    card = kb.post_card(post, queues=[])
    buttons = [b.text for row in card.inline_keyboard for b in row]
    assert any("🗑" in t for t in buttons), f"Нет кнопки удаления в карточке поста: {buttons}"


def test_keyboards_need_queue_menu():
    """Клавиатура «Нет очередей» для правил маршрутизации содержит кнопку создания очереди."""
    from app.bot.keyboards import builder as kb

    menu = kb.need_queue_menu()
    buttons = [b.text for row in menu.inline_keyboard for b in row]
    assert any("Создать очередь" in t for t in buttons), f"Нет кнопки создания очереди: {buttons}"
    assert any("Назад" in t for t in buttons), f"Нет кнопки Назад: {buttons}"


def test_pick_channel_for_post_keyboard():
    """Клавиатура выбора канала для поста (блок A — интерактивный выбор)."""
    from app.bot.keyboards import builder as kb

    channels = [
        {"id": 1, "name": "AnimeCh", "username": "anime"},
        {"id": 2, "name": "MemeCh", "username": None},
    ]
    markup = kb.pick_channel_for_post(42, channels)
    buttons = [b.text for row in markup.inline_keyboard for b in row]
    assert any("AnimeCh" in t for t in buttons)
    assert any("MemeCh" in t for t in buttons)
    # Должна быть кнопка создания канала
    assert any("Создать канал" in t for t in buttons)
    # callback_data ведёт на pickch
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert any("menu:post:42:pickch:1" == c for c in callbacks), f"Нет callback pickch: {callbacks}"


def test_pick_queue_for_post_keyboard():
    """Клавиатура выбора очереди канала для поста."""
    from app.bot.keyboards import builder as kb

    queues = [
        {"id": 1, "name": "AnimeQ", "interval_minutes": 60},
        {"id": 2, "name": "MemeQ", "interval_minutes": 30},
    ]
    markup = kb.pick_queue_for_post(42, channel_id=1, queues=queues)
    buttons = [b.text for row in markup.inline_keyboard for b in row]
    assert any("AnimeQ" in t for t in buttons)
    assert any("MemeQ" in t for t in buttons)
    # Должна быть кнопка создания очереди и возврата к каналам
    assert any("Создать очередь" in t for t in buttons)
    assert any("К каналам" in t for t in buttons)
    # callback_data для toq (перемещение поста)
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert any("menu:post:42:toq:1" == c for c in callbacks), f"Нет callback toq: {callbacks}"
    # callback для создания очереди с возвратом к посту
    assert any("menu:addq_forch:1:42" == c for c in callbacks), f"Нет callback addq_forch: {callbacks}"


def test_routing_source_types_no_direct_user():
    """Из UI создания правил убраны telegram_direct и telegram_user (блок A)."""
    from app.bot.keyboards import builder as kb

    source_types = [v for v, _ in kb.ROUTING_SOURCE_TYPES]
    assert "telegram_channel" in source_types
    assert "telegram_direct" not in source_types, "telegram_direct должен быть убран из правил"
    assert "telegram_user" not in source_types, "telegram_user должен быть убран из правил"
    assert "any" in source_types


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
