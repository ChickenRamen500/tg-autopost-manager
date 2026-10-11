"""Тесты маршрутизации (ТЗ §2.2.2)."""
from app.services.routing.service import _looks_like_regex, _matches


def test_looks_like_regex():
    assert _looks_like_regex(r"@anime_channel") is False  # нет спецсимволов
    assert _looks_like_regex(r".*rating:safe.*") is True
    assert _looks_like_regex(r"D:\\Media\\Videos") is True  # содержит \
    assert _looks_like_regex("plain_text") is False
    assert _looks_like_regex("1girl+smile") is True  # +


def test_matches_exact():
    # Паттерн без спецсимволов → точное совпадение (с учётом регистра)
    assert _matches("@anime_channel", "@anime_channel") is True
    assert _matches("@anime_channel", "@manga_channel") is False
    assert _matches("@anime_channel", None) is False
    assert _matches("", "@anime_channel") is False  # пустой паттерн


def test_matches_regex():
    # Regex-паттерн — full-match, регистронезависимый
    assert _matches(r".*rating:safe.*", "1girl rating:safe smile") is True
    assert _matches(r".*rating:safe.*", "1girl rating:explicit") is False
    assert _matches(r"D:\\Media\\Videos.*", "D:\\Media\\Videos\\New\\clip.mp4") is True
    # Паттерн со спецсимволом [ ] → regex; IGNORECASE: U матчит u
    assert _matches(r"GELBOOR[U]", "gelbooru") is True


def test_matches_invalid_regex_fallback():
    # Невалидный regex → трактуем как точное совпадение (не роняем маршрутизацию)
    assert _matches("[invalid(", "1girl") is False
    assert _matches("[invalid(", "[invalid(") is True


def test_route_post_exact_and_any(tmp_path):
    """Полный цикл маршрутизации: точное правило + any → очередь / Входящие."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import queues as queues_repo
        from app.repositories import routing as routing_repo
        from app.services.routing import route_post

        ch_id = await channels_repo.create_channel(-1000000000001, "Test Channel", "test_ch")
        q_anime = await queues_repo.create_queue(ch_id, "Anime Queue", interval_minutes=120)
        q_inbox = await queues_repo.create_queue(ch_id, "Inbox", interval_minutes=1440)

        await routing_repo.create_rule("telegram_channel", "@anime_channel", q_anime, priority=10)
        await routing_repo.create_rule("any", r".*", q_inbox, priority=0)

        # Точное совпадение → очередь Anime
        res = await route_post("telegram_channel", "@anime_channel")
        assert res == q_anime

        # Нет точного правила → срабатывает any → Входящие
        res = await route_post("telegram_channel", "@unknown_channel")
        assert res == q_inbox

        # Удалим any-правило → теперь None (Входящие без queue_id)
        rules = await routing_repo.list_rules(active_only=False)
        any_rule = next(r for r in rules if r["source_type"] == "any")
        await routing_repo.remove_rule(any_rule["id"])
        res = await route_post("telegram_channel", "@unknown_channel")
        assert res is None

    run_with_db(tmp_path, body)


def test_route_post_priority(tmp_path):
    """Правило с бóльшим приоритетом побеждает при совпадении (ТЗ §2.2.2 п.3)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import queues as queues_repo
        from app.repositories import routing as routing_repo
        from app.services.routing import route_post

        ch_id = await channels_repo.create_channel(-1000000000002, "Test Channel 2")
        q_high = await queues_repo.create_queue(ch_id, "High Priority Queue")
        q_low = await queues_repo.create_queue(ch_id, "Low Priority Queue")

        # Оба правила матчат "@anime_channel", но приоритеты разные
        await routing_repo.create_rule("telegram_channel", "@anime_channel", q_low, priority=1)
        await routing_repo.create_rule("telegram_channel", "@anime_channel", q_high, priority=10)

        res = await route_post("telegram_channel", "@anime_channel")
        assert res == q_high  # приоритет выше → побеждает

    run_with_db(tmp_path, body)
