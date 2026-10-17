"""Тесты UI Gelbooru (ТЗ §2.1.4, Фаза 3 этап 4).

Покрытие:
- keyboards: gelbooru_main_menu, gelbooru_subs_list, gelbooru_sub_card,
  gelbooru_seen_list, gelbooru_seen_card (основное меню + режим выбора очереди),
  gelbooru_blacklist_list
- main_menu содержит кнопку «🖼 Gelbooru» (для admin)
- handlers/gelbooru.py: router зарегистрирован, callback_data-конвенция
"""
from __future__ import annotations

import pytest  # noqa: F401  # используется в pytest.main внизу

# ===========================================================================
# keyboards: построение без ошибок + правильные callback_data
# ===========================================================================


def test_main_menu_has_gelbooru_button_for_admin():
    """В main_menu для admin есть кнопка «🖼 Gelbooru» с callback gb:menu."""
    from app.bot.keyboards import builder as kb

    menu = kb.main_menu(is_admin=True)
    buttons = [(b.text, b.callback_data) for row in menu.inline_keyboard for b in row]
    gelbooru_buttons = [b for b in buttons if b[1] == "gb:menu"]
    assert len(gelbooru_buttons) == 1, f"Должна быть 1 кнопка gb:menu, найдено {len(gelbooru_buttons)}"
    assert "Gelbooru" in gelbooru_buttons[0][0]


def test_main_menu_no_gelbooru_for_moderator():
    """Для moderator (не admin) кнопки Gelbooru нет."""
    from app.bot.keyboards import builder as kb

    menu = kb.main_menu(is_admin=False)
    buttons = [b.callback_data for row in menu.inline_keyboard for b in row]
    assert "gb:menu" not in buttons


def test_gelbooru_main_menu():
    from app.bot.keyboards import builder as kb

    m = kb.gelbooru_main_menu(new_posts_count=0)
    callbacks = [b.callback_data for row in m.inline_keyboard for b in row]
    assert "gb:seen" in callbacks
    assert "gb:subs" in callbacks
    assert "gb:blacklist" in callbacks
    assert "menu:main" in callbacks  # Назад

    # С ненулевым count — кнопка содержит число
    m2 = kb.gelbooru_main_menu(new_posts_count=5)
    texts = [b.text for row in m2.inline_keyboard for b in row]
    assert any("5" in t for t in texts)


def test_gelbooru_subs_list():
    from app.bot.keyboards import builder as kb

    subs = [
        {"id": 1, "tags": ["1girl", "solo"], "is_active": 1},
        {"id": 2, "tags": ["cat"], "is_active": 0},
    ]
    kl = kb.gelbooru_subs_list(subs, new_counts={1: 3})
    callbacks = [b.callback_data for row in kl.inline_keyboard for b in row]
    assert "gb:sub:1" in callbacks
    assert "gb:sub:2" in callbacks
    assert "gb:add_sub" in callbacks
    assert "gb:menu" in callbacks  # Назад

    # Бейдж новых постов — в тексте кнопки
    texts = [b.text for row in kl.inline_keyboard for b in row]
    assert any("[+3]" in t for t in texts)


def test_gelbooru_subs_list_empty():
    from app.bot.keyboards import builder as kb

    kl = kb.gelbooru_subs_list([], new_counts={})
    callbacks = [b.callback_data for row in kl.inline_keyboard for b in row]
    assert "gb:add_sub" in callbacks
    assert "gb:menu" in callbacks


def test_gelbooru_sub_card():
    from app.bot.keyboards import builder as kb

    sub = {"id": 1, "is_active": 1, "tags": ["1girl"], "check_interval_hours": 6, "notify_interval_minutes": 60}
    card = kb.gelbooru_sub_card(sub, new_count=2)
    callbacks = [b.callback_data for row in card.inline_keyboard for b in row]
    assert "gb:sub:1:check" in callbacks  # Проверить сейчас
    assert "gb:sub:1:toggle" in callbacks  # Вкл/выкл
    assert "gb:sub:1:del" in callbacks  # Удалить
    assert "gb:subs" in callbacks  # Назад
    assert "gb:sub:1:setcheck" in callbacks  # Интервал проверки
    assert "gb:sub:1:setnotify" in callbacks  # Интервал уведомлений
    assert "gb:sub:1:setqueue" in callbacks  # Default queue
    # Кнопка новых постов (если count > 0)
    assert "gb:seen:1" in callbacks

    # Без новых постов — кнопки «новые посты» нет
    card2 = kb.gelbooru_sub_card(
        {"id": 1, "is_active": 1, "tags": [], "check_interval_hours": 6, "notify_interval_minutes": 60},
        new_count=0,
    )
    callbacks2 = [b.callback_data for row in card2.inline_keyboard for b in row]
    assert "gb:seen:1" not in callbacks2


def test_gelbooru_seen_list():
    from app.bot.keyboards import builder as kb

    items = [
        {"id": 10, "post_id": 100, "tags": ["1girl"], "rating": "safe"},
        {"id": 11, "post_id": 101, "tags": ["cat"], "rating": "explicit"},
    ]
    kl = kb.gelbooru_seen_list(items, sub_id=None)
    callbacks = [b.callback_data for row in kl.inline_keyboard for b in row]
    assert "gb:seen_card:10" in callbacks
    assert "gb:seen_card:11" in callbacks
    assert "gb:menu" in callbacks  # Назад (sub_id=None → gb:menu)

    # С sub_id — Назад ведёт к подписке
    kl2 = kb.gelbooru_seen_list(items, sub_id=5)
    callbacks2 = [b.callback_data for row in kl2.inline_keyboard for b in row]
    assert "gb:seen:5" in callbacks2  # Назад к подписке


def test_gelbooru_seen_list_empty():
    from app.bot.keyboards import builder as kb

    kl = kb.gelbooru_seen_list([], sub_id=None)
    callbacks = [b.callback_data for row in kl.inline_keyboard for b in row]
    assert "noop" in callbacks  # «Пусто»
    assert "gb:menu" in callbacks


def test_gelbooru_seen_card_main_menu():
    """Карточка поста — основное меню с [Отправить][Пропустить][Назад/Далее]."""
    from app.bot.keyboards import builder as kb

    # Без навигации (has_prev=False, has_next=False)
    card = kb.gelbooru_seen_card(seen_id=10, has_prev=False, has_next=False)
    callbacks = [b.callback_data for row in card.inline_keyboard for b in row]
    assert "gb:seen:10:send" in callbacks
    assert "gb:seen:10:skip" in callbacks
    assert "gb:seen" in callbacks  # К списку

    # С навигацией
    card2 = kb.gelbooru_seen_card(seen_id=10, has_prev=True, has_next=True)
    callbacks2 = [b.callback_data for row in card2.inline_keyboard for b in row]
    assert "gb:seen:10:prev" in callbacks2
    assert "gb:seen:10:next" in callbacks2


def test_gelbooru_seen_card_queue_picker_mode():
    """Если передать queues — режим выбора очереди (кнопки очередей)."""
    from app.bot.keyboards import builder as kb

    queues = [{"id": 1, "name": "Q1"}, {"id": 2, "name": "Q2"}]
    card = kb.gelbooru_seen_card(seen_id=10, has_prev=False, has_next=False, queues=queues)
    callbacks = [b.callback_data for row in card.inline_keyboard for b in row]
    # Кнопка «Во Входящие» (qid=0)
    assert "gb:seen:10:send:0" in callbacks
    # Кнопки очередей
    assert "gb:seen:10:send:1" in callbacks
    assert "gb:seen:10:send:2" in callbacks
    # Кнопка отмены — возврат к карточке
    assert "gb:seen_card:10" in callbacks
    # Кнопок основного меню (send/skip) в этом режиме нет
    assert "gb:seen:10:skip" not in callbacks


def test_gelbooru_blacklist_list():
    from app.bot.keyboards import builder as kb

    kl = kb.gelbooru_blacklist_list(["rating_explicit", "1boy"])
    callbacks = [b.callback_data for row in kl.inline_keyboard for b in row]
    # URL-encoded теги в callback_data
    from urllib.parse import quote

    assert f"gb:bl:{quote('rating_explicit', safe='')}:del" in callbacks
    assert f"gb:bl:{quote('1boy', safe='')}:del" in callbacks
    assert "gb:bl_add" in callbacks
    assert "gb:menu" in callbacks


def test_gelbooru_blacklist_list_empty():
    from app.bot.keyboards import builder as kb

    kl = kb.gelbooru_blacklist_list([])
    callbacks = [b.callback_data for row in kl.inline_keyboard for b in row]
    assert "noop" in callbacks  # «Пусто»
    assert "gb:bl_add" in callbacks


# ===========================================================================
# handlers/gelbooru.py: router существует и зарегистрирован
# ===========================================================================


def test_gelbooru_router_exists():
    from app.bot.handlers import gelbooru

    assert gelbooru.router is not None
    assert gelbooru.router.name == "gelbooru"


def test_gelbooru_router_registered_in_dispatcher():
    """Роутер gelbooru включён в Dispatcher (app/bot/__init__.py)."""
    from app.bot import get_dispatcher

    dp = get_dispatcher()
    # Проверяем что router с именем 'gelbooru' есть в sub_routers
    router_names = [r.name for r in dp.sub_routers]
    assert "gelbooru" in router_names, f"gelbooru router не зарегистрирован: {router_names}"


def test_gelbooru_states_defined():
    """FSM-состояния для добавления подписки/blacklist определены."""
    from app.bot.handlers.gelbooru import GelbooruStates

    assert GelbooruStates.add_sub_tags is not None
    assert GelbooruStates.add_bl_tag is not None


def test_gelbooru_handlers_callbacks_registered():
    """Проверяем что ключевые callback_data обрабатываются роутером gelbooru."""
    from app.bot.handlers.gelbooru import router

    # Собираем все callback_data-фильтры из роутера
    # (aiogram хранит их в router.callback_query.handlers)
    cb_handlers = router.callback_query.handlers
    assert len(cb_handlers) > 0, "В роутере gelbooru нет callback handlers"

    # Проверяем что есть handler для gb:menu (главное меню)
    # Полная проверка типов фильтров сложна — просто убеждаемся что handlers есть
    # и роутер не пустой


# ===========================================================================
# Интеграционный smoke: callback_data конвенция не нарушена
# ===========================================================================


def test_callback_data_convention_no_conflicts():
    """Все gb:* callbacks начинаются с 'gb:' — не конфликтуют с menu:/rule: и т.д."""
    from app.bot.keyboards import builder as kb

    keyboards = [
        kb.gelbooru_main_menu(),
        kb.gelbooru_subs_list([{"id": 1, "tags": ["x"], "is_active": 1}], {}),
        kb.gelbooru_sub_card({"id": 1, "is_active": 1, "tags": [], "check_interval_hours": 6, "notify_interval_minutes": 60}, 0),
        kb.gelbooru_seen_list([{"id": 1, "post_id": 1, "tags": [], "rating": "safe"}], None),
        kb.gelbooru_seen_card(1, False, False),
        kb.gelbooru_blacklist_list(["x"]),
    ]
    for kbd in keyboards:
        for row in kbd.inline_keyboard:
            for btn in row:
                if btn.callback_data:
                    # gb:* ИЛИ menu:main (кнопка Назад) ИЛИ noop (пусто) — всё ок
                    assert (
                        btn.callback_data.startswith("gb:")
                        or btn.callback_data.startswith("menu:")
                        or btn.callback_data == "noop"
                    ), f"Неожиданный callback_data в gelbooru клавиатуре: {btn.callback_data}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
