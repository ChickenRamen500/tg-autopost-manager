"""Тесты подписок Gelbooru (ТЗ §2.1.4, Фаза 3 этап 3).

Покрытие:
- subscriptions: create (с notify_interval), get, set_last_notified, is_due_for_notify
- gelbooru_subs (seen_posts): add (идемпотентность), is_post_seen, is_file_seen_anywhere,
  list_new_posts, count_new_posts, mark_sent, mark_ignored
- gelbooru_subs (blacklist): add (нормализация), remove, list, is_blacklisted,
  get_blacklist_query_suffix
- gelbooru_subscriptions service: _build_query (tags + blacklist), _extract_post_fields,
  check_subscription (mock gelbooru client), check_all_due_subscriptions,
  send_seen_post_to_queue (создание posts + enqueue + mark_sent)
"""
from __future__ import annotations

import asyncio

import pytest

# ===========================================================================
# Helper: создать пользователя + подписку
# ===========================================================================


async def _make_user_and_sub(tmp_path, tags=None, **sub_kwargs):
    """Создаёт пользователя и подписку, возвращает (sub_id, user_id)."""
    from app.repositories import subscriptions as subs_repo
    from app.repositories import users as users_repo

    user_id = await users_repo.add_user(tg_id=111222333, username="tester", role="admin")
    sub_id = await subs_repo.create_subscription(
        user_id=user_id,
        site="gelbooru",
        tags=tags or ["1girl", "solo"],
        **sub_kwargs,
    )
    return sub_id, user_id


# ===========================================================================
# subscriptions.py: create (notify_interval), get, set_last_notified, is_due_for_notify
# ===========================================================================


def test_create_subscription_with_notify_interval(tmp_path):
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path, notify_interval_minutes=30)
        assert sub_id > 0
        from app.repositories import subscriptions as subs_repo

        sub = await subs_repo.get_subscription(sub_id)
        assert sub is not None
        assert sub["notify_interval_minutes"] == 30
        assert sub["site"] == "gelbooru"
        assert sub["tags"] == ["1girl", "solo"]
        assert sub["is_active"] == 1

    run_with_db(tmp_path, body)


def test_create_subscription_default_notify_interval(tmp_path):
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path)
        from app.repositories import subscriptions as subs_repo

        sub = await subs_repo.get_subscription(sub_id)
        assert sub["notify_interval_minutes"] == 60  # default

    run_with_db(tmp_path, body)


def test_create_subscription_invalid_notify_interval(tmp_path):
    from conftest import run_with_db

    async def body():
        with pytest.raises(ValueError, match="notify_interval_minutes"):
            await _make_user_and_sub(tmp_path, notify_interval_minutes=-1)

    run_with_db(tmp_path, body)


def test_is_due_for_notify_never_notified(tmp_path):
    """last_notified_at IS NULL → всегда due (даже при большом interval)."""
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path, notify_interval_minutes=60)
        from app.repositories import subscriptions as subs_repo

        assert await subs_repo.is_due_for_notify(sub_id) is True

    run_with_db(tmp_path, body)


def test_is_due_for_notify_zero_interval(tmp_path):
    """notify_interval_minutes=0 → всегда due (даже после set_last_notified)."""
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path, notify_interval_minutes=0)
        from app.repositories import subscriptions as subs_repo

        await subs_repo.set_last_notified(sub_id)
        assert await subs_repo.is_due_for_notify(sub_id) is True

    run_with_db(tmp_path, body)


def test_is_due_for_notify_recently_notified(tmp_path):
    """last_notified_at только что → не due."""
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path, notify_interval_minutes=60)
        from app.repositories import subscriptions as subs_repo

        await subs_repo.set_last_notified(sub_id)
        assert await subs_repo.is_due_for_notify(sub_id) is False

    run_with_db(tmp_path, body)


def test_set_last_notified(tmp_path):
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path)
        from app.repositories import subscriptions as subs_repo

        ok = await subs_repo.set_last_notified(sub_id)
        assert ok is True
        sub = await subs_repo.get_subscription(sub_id)
        assert sub["last_notified_at"] is not None

    run_with_db(tmp_path, body)


# ===========================================================================
# gelbooru_subs: seen_posts CRUD
# ===========================================================================


def test_add_seen_post_new(tmp_path):
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path)
        from app.repositories import gelbooru_subs as gs_repo

        seen_id = await gs_repo.add_seen_post(
            subscription_id=sub_id,
            post_id=12345,
            file_unique_id="abc123def456",
            source_url="https://img.gelbooru.com/images/sample.jpg",
            preview_url="https://img.gelbooru.com/thumbnails/thumb.jpg",
            tags="1girl solo blue_hair",
            rating="safe",
        )
        assert seen_id is not None
        assert seen_id > 0

    run_with_db(tmp_path, body)


def test_add_seen_post_idempotent(tmp_path):
    """Повторный add_seen_post для (sub_id, post_id) → None (UNIQUE constraint)."""
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path)
        from app.repositories import gelbooru_subs as gs_repo

        seen_id1 = await gs_repo.add_seen_post(sub_id, 12345, file_unique_id="md5_1")
        assert seen_id1 is not None
        # Повтор — тот же (sub_id, post_id)
        seen_id2 = await gs_repo.add_seen_post(sub_id, 12345, file_unique_id="md5_1")
        assert seen_id2 is None  # уже есть

    run_with_db(tmp_path, body)


def test_is_post_seen(tmp_path):
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path)
        from app.repositories import gelbooru_subs as gs_repo

        assert await gs_repo.is_post_seen(sub_id, 12345) is False
        await gs_repo.add_seen_post(sub_id, 12345, file_unique_id="md5_1")
        assert await gs_repo.is_post_seen(sub_id, 12345) is True
        # другой post_id — не seen
        assert await gs_repo.is_post_seen(sub_id, 99999) is False

    run_with_db(tmp_path, body)


def test_is_file_seen_anywhere_cross_subscription(tmp_path):
    """Кросс-подписочная дедупликация по md5 — пост в sub1 блокирует в sub2."""
    from conftest import run_with_db

    async def body():
        from app.repositories import subscriptions as subs_repo
        from app.repositories import users as users_repo

        user_id = await users_repo.add_user(tg_id=111, username="u", role="admin")
        sub1 = await subs_repo.create_subscription(user_id, "gelbooru", ["cat"])

        from app.repositories import gelbooru_subs as gs_repo

        # Добавляем пост с md5=X в sub1
        await gs_repo.add_seen_post(sub1, 100, file_unique_id="shared_md5")
        # Тот же md5 — is_file_seen_anywhere должен вернуть True (найден в sub1)
        assert await gs_repo.is_file_seen_anywhere("shared_md5") is True
        # Другой md5 — False
        assert await gs_repo.is_file_seen_anywhere("other_md5") is False

    run_with_db(tmp_path, body)


def test_list_new_posts(tmp_path):
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path)
        from app.repositories import gelbooru_subs as gs_repo

        await gs_repo.add_seen_post(sub_id, 100, file_unique_id="m1")
        await gs_repo.add_seen_post(sub_id, 101, file_unique_id="m2")
        await gs_repo.add_seen_post(sub_id, 102, file_unique_id="m3")

        new_posts = await gs_repo.list_new_posts(sub_id)
        assert len(new_posts) == 3
        # Порядок по created_at, id — все новые
        assert all(p["status"] == "new" for p in new_posts)

    run_with_db(tmp_path, body)


def test_count_new_posts(tmp_path):
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path)
        from app.repositories import gelbooru_subs as gs_repo

        assert await gs_repo.count_new_posts(sub_id) == 0
        await gs_repo.add_seen_post(sub_id, 100, file_unique_id="m1")
        await gs_repo.add_seen_post(sub_id, 101, file_unique_id="m2")
        assert await gs_repo.count_new_posts(sub_id) == 2

    run_with_db(tmp_path, body)


def test_mark_sent(tmp_path):
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path)
        from app.repositories import gelbooru_subs as gs_repo

        seen_id = await gs_repo.add_seen_post(sub_id, 100, file_unique_id="m1", source_url="https://x/a.jpg")
        ok = await gs_repo.mark_sent(seen_id, post_pk=999)
        assert ok is True
        seen = await gs_repo.get_seen_post(seen_id)
        assert seen["status"] == "sent"
        assert seen["post_pk"] == 999
        assert seen["reviewed_at"] is not None

    run_with_db(tmp_path, body)


def test_mark_ignored(tmp_path):
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path)
        from app.repositories import gelbooru_subs as gs_repo

        seen_id = await gs_repo.add_seen_post(sub_id, 100, file_unique_id="m1")
        ok = await gs_repo.mark_ignored(seen_id)
        assert ok is True
        seen = await gs_repo.get_seen_post(seen_id)
        assert seen["status"] == "ignored"

    run_with_db(tmp_path, body)


def test_mark_sent_only_for_new(tmp_path):
    """mark_sent/mark_ignored работают только для status='new' (WHERE status='new')."""
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path)
        from app.repositories import gelbooru_subs as gs_repo

        seen_id = await gs_repo.add_seen_post(sub_id, 100, file_unique_id="m1")
        await gs_repo.mark_ignored(seen_id)
        # Уже ignored — mark_sent не сработает
        ok = await gs_repo.mark_sent(seen_id, post_pk=999)
        assert ok is False
        seen = await gs_repo.get_seen_post(seen_id)
        assert seen["status"] == "ignored"  # не изменился

    run_with_db(tmp_path, body)


# ===========================================================================
# gelbooru_subs: blacklist CRUD
# ===========================================================================


def test_add_blacklist_tag_normalizes(tmp_path):
    """add_blacklist_tag нормализует: trim, lstrip('-'), lower."""
    from conftest import run_with_db

    async def body():
        from app.repositories import gelbooru_subs as gs_repo

        rid = await gs_repo.add_blacklist_tag("  -Rating_Explicit  ")
        assert rid is not None
        tags = await gs_repo.list_blacklist_tags()
        assert tags == ["rating_explicit"]

    run_with_db(tmp_path, body)


def test_add_blacklist_tag_idempotent(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.repositories import gelbooru_subs as gs_repo

        rid1 = await gs_repo.add_blacklist_tag("1boy")
        assert rid1 is not None
        rid2 = await gs_repo.add_blacklist_tag("1boy")  # дубль
        assert rid2 is None
        tags = await gs_repo.list_blacklist_tags()
        assert tags == ["1boy"]

    run_with_db(tmp_path, body)


def test_add_blacklist_tag_empty_raises(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.repositories import gelbooru_subs as gs_repo

        with pytest.raises(ValueError, match="Пустой blacklist-тег"):
            await gs_repo.add_blacklist_tag("")
        with pytest.raises(ValueError):
            await gs_repo.add_blacklist_tag("---")

    run_with_db(tmp_path, body)


def test_remove_blacklist_tag(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.repositories import gelbooru_subs as gs_repo

        await gs_repo.add_blacklist_tag("loli")
        assert await gs_repo.remove_blacklist_tag("loli") is True
        assert await gs_repo.list_blacklist_tags() == []
        # Повторное удаление — False
        assert await gs_repo.remove_blacklist_tag("loli") is False

    run_with_db(tmp_path, body)


def test_is_blacklisted(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.repositories import gelbooru_subs as gs_repo

        await gs_repo.add_blacklist_tag("rating_explicit")
        assert await gs_repo.is_blacklisted("rating_explicit") is True
        assert await gs_repo.is_blacklisted("RATING_EXPLICIT") is True  # case-insensitive
        assert await gs_repo.is_blacklisted("-rating_explicit") is True  # lstrip('-')
        assert await gs_repo.is_blacklisted("1boy") is False

    run_with_db(tmp_path, body)


def test_get_blacklist_query_suffix(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.repositories import gelbooru_subs as gs_repo

        # Пустой blacklist → пустая строка
        assert await gs_repo.get_blacklist_query_suffix() == ""

        await gs_repo.add_blacklist_tag("rating_explicit")
        await gs_repo.add_blacklist_tag("1boy")
        suffix = await gs_repo.get_blacklist_query_suffix()
        # Порядок сортировки — alphabetical
        assert "-1boy" in suffix
        assert "-rating_explicit" in suffix

    run_with_db(tmp_path, body)


# ===========================================================================
# gelbooru_subscriptions service: чистые функции
# ===========================================================================


def test_build_query_without_blacklist():
    from app.services.gelbooru_subscriptions import _build_query

    assert _build_query(["1girl", "solo"], "") == "1girl solo"
    assert _build_query(["1girl", "-1boy"], "") == "1girl -1boy"


def test_build_query_with_blacklist():
    from app.services.gelbooru_subscriptions import _build_query

    # tags + blacklist suffix
    q = _build_query(["1girl", "solo"], "-rating_explicit -1boy")
    assert "1girl" in q
    assert "solo" in q
    assert "-rating_explicit" in q
    assert "-1boy" in q


def test_build_query_empty_tags():
    from app.services.gelbooru_subscriptions import _build_query

    # Пустые tags + blacklist → только blacklist
    assert _build_query([], "-rating_explicit") == "-rating_explicit"
    assert _build_query([], "") == ""


def test_extract_post_fields():
    from app.services.gelbooru_subscriptions import _extract_post_fields

    raw = {
        "id": 12345,
        "md5": "abc123",
        "file_url": "https://img.gelbooru.com/images/abc.jpg",
        "preview_url": "https://img.gelbooru.com/thumbnails/thumb.jpg",
        "tags": "1girl solo blue_hair",
        "rating": "safe",
    }
    fields = _extract_post_fields(raw)
    assert fields["post_id"] == 12345
    assert fields["file_unique_id"] == "abc123"
    assert fields["source_url"] == "https://img.gelbooru.com/images/abc.jpg"
    assert fields["preview_url"].endswith("thumb.jpg")
    assert fields["tags"] == "1girl solo blue_hair"
    assert fields["rating"] == "safe"


def test_extract_post_fields_missing_fields():
    from app.services.gelbooru_subscriptions import _extract_post_fields

    # Пост без md5/rating — поля None
    raw = {"id": 1, "file_url": "https://x/a.jpg"}
    fields = _extract_post_fields(raw)
    assert fields["post_id"] == 1
    assert fields["file_unique_id"] is None
    assert fields["rating"] is None


def test_file_type_from_url():
    from app.services.gelbooru_subscriptions import _file_type_from_url

    assert _file_type_from_url("https://x.com/a.jpg") == "photo"
    assert _file_type_from_url("https://x.com/a.png") == "photo"
    assert _file_type_from_url("https://x.com/a.gif") == "gif"
    assert _file_type_from_url("https://x.com/v.mp4") == "video"
    assert _file_type_from_url("https://x.com/v.webm") == "video"
    # URL с query params
    assert _file_type_from_url("https://x.com/a.jpg?token=abc") == "photo"
    # Неизвестное расширение → photo (gelbooru преим. изображения)
    assert _file_type_from_url("https://x.com/file") == "photo"


# ===========================================================================
# gelbooru_subscriptions service: check_subscription (mock gelbooru client)
# ===========================================================================


def test_check_subscription_finds_new_posts(tmp_path, monkeypatch):
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path, tags=["1girl"])
        from app.repositories import gelbooru_subs as gs_repo
        from app.repositories import subscriptions as subs_repo
        from app.services.gelbooru_subscriptions import check_subscription

        # Мокаем gelbooru_client.search_posts
        async def fake_search(query, pid=0, limit=50):
            assert "1girl" in query
            return [
                {"id": 100, "md5": "md5_100", "file_url": "https://x/100.jpg", "tags": "1girl", "rating": "safe"},
                {"id": 101, "md5": "md5_101", "file_url": "https://x/101.jpg", "tags": "1girl", "rating": "safe"},
            ]

        from app.services.parsers.gelbooru import client as gclient

        monkeypatch.setattr(gclient.gelbooru_client, "search_posts", fake_search)

        sub = await subs_repo.get_subscription(sub_id)
        new_count = await check_subscription(sub)
        assert new_count == 2

        # seen_posts должны быть добавлены
        assert await gs_repo.count_new_posts(sub_id) == 2
        assert await gs_repo.is_post_seen(sub_id, 100) is True
        assert await gs_repo.is_post_seen(sub_id, 101) is True

    run_with_db(tmp_path, body)


def test_check_subscription_dedup_already_seen(tmp_path, monkeypatch):
    """Посты уже в seen_posts не добавляются повторно."""
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path, tags=["1girl"])
        from app.repositories import gelbooru_subs as gs_repo
        from app.repositories import subscriptions as subs_repo
        from app.services.gelbooru_subscriptions import check_subscription

        # Предварительно добавляем пост 100
        await gs_repo.add_seen_post(sub_id, 100, file_unique_id="md5_100")

        async def fake_search(query, pid=0, limit=50):
            return [
                {"id": 100, "md5": "md5_100", "file_url": "https://x/100.jpg"},  # уже seen
                {"id": 101, "md5": "md5_101", "file_url": "https://x/101.jpg"},  # новый
            ]

        from app.services.parsers.gelbooru import client as gclient

        monkeypatch.setattr(gclient.gelbooru_client, "search_posts", fake_search)

        sub = await subs_repo.get_subscription(sub_id)
        new_count = await check_subscription(sub)
        assert new_count == 1  # только 101 новый

    run_with_db(tmp_path, body)


def test_check_subscription_cross_sub_dedup_by_md5(tmp_path, monkeypatch):
    """Кросс-подписочная дедупликация: пост с md5 из sub1 не добавится в sub2."""
    from conftest import run_with_db

    async def body():
        from app.repositories import gelbooru_subs as gs_repo
        from app.repositories import subscriptions as subs_repo
        from app.repositories import users as users_repo
        from app.services.gelbooru_subscriptions import check_subscription

        user_id = await users_repo.add_user(tg_id=111, username="u", role="admin")
        sub1 = await subs_repo.create_subscription(user_id, "gelbooru", ["cat"])
        sub2 = await subs_repo.create_subscription(user_id, "gelbooru", ["animal"])

        # Добавляем пост с md5=X в sub1
        await gs_repo.add_seen_post(sub1, 100, file_unique_id="shared_md5")

        async def fake_search(query, pid=0, limit=50):
            # sub2 поиск находит тот же пост (md5=shared_md5)
            return [{"id": 100, "md5": "shared_md5", "file_url": "https://x/100.jpg"}]

        from app.services.parsers.gelbooru import client as gclient

        monkeypatch.setattr(gclient.gelbooru_client, "search_posts", fake_search)

        sub2_obj = await subs_repo.get_subscription(sub2)
        new_count = await check_subscription(sub2_obj)
        assert new_count == 0  # кросс-дедуп по md5 заблокировал
        assert await gs_repo.is_post_seen(sub2, 100) is False

    run_with_db(tmp_path, body)


def test_check_subscription_with_blacklist(tmp_path, monkeypatch):
    """Blacklist добавляется к запросу (-tag)."""
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path, tags=["1girl"])
        from app.repositories import gelbooru_subs as gs_repo
        from app.repositories import subscriptions as subs_repo
        from app.services.gelbooru_subscriptions import check_subscription

        # Добавляем blacklist
        await gs_repo.add_blacklist_tag("rating_explicit")

        captured_query = []

        async def fake_search(query, pid=0, limit=50):
            captured_query.append(query)
            return []

        from app.services.parsers.gelbooru import client as gclient

        monkeypatch.setattr(gclient.gelbooru_client, "search_posts", fake_search)

        sub = await subs_repo.get_subscription(sub_id)
        await check_subscription(sub)
        assert len(captured_query) == 1
        assert "1girl" in captured_query[0]
        assert "-rating_explicit" in captured_query[0]

    run_with_db(tmp_path, body)


def test_check_subscription_api_error_returns_zero(tmp_path, monkeypatch):
    """Ошибка gelbooru API → 0 новых, не падает."""
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path, tags=["1girl"])
        from app.repositories import subscriptions as subs_repo
        from app.services.gelbooru_subscriptions import check_subscription

        async def fake_search(query, pid=0, limit=50):
            raise RuntimeError("gelbooru 500")

        from app.services.parsers.gelbooru import client as gclient

        monkeypatch.setattr(gclient.gelbooru_client, "search_posts", fake_search)

        sub = await subs_repo.get_subscription(sub_id)
        new_count = await check_subscription(sub)
        assert new_count == 0

    run_with_db(tmp_path, body)


def test_check_subscription_empty_result(tmp_path, monkeypatch):
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path, tags=["1girl"])
        from app.repositories import gelbooru_subs as gs_repo
        from app.repositories import subscriptions as subs_repo
        from app.services.gelbooru_subscriptions import check_subscription

        async def fake_search(query, pid=0, limit=50):
            return []

        from app.services.parsers.gelbooru import client as gclient

        monkeypatch.setattr(gclient.gelbooru_client, "search_posts", fake_search)

        sub = await subs_repo.get_subscription(sub_id)
        new_count = await check_subscription(sub)
        assert new_count == 0
        assert await gs_repo.count_new_posts(sub_id) == 0

    run_with_db(tmp_path, body)


# ===========================================================================
# gelbooru_subscriptions service: check_all_due_subscriptions
# ===========================================================================


def test_check_all_due_subscriptions_no_due(tmp_path):
    """Нет due-подписок → {checked:0, new_posts:0, notified:0}."""
    from conftest import run_with_db

    async def body():
        from app.services.gelbooru_subscriptions import check_all_due_subscriptions

        # Создаём подписку, но last_checked_at=NULL → она due. Помечаем checked.
        sub_id, _ = await _make_user_and_sub(tmp_path)
        from app.repositories import subscriptions as subs_repo

        await subs_repo.mark_checked(sub_id)
        # Теперь не due (check_interval_hours=6 по умолчанию)
        stats = await check_all_due_subscriptions()
        assert stats == {"checked": 0, "new_posts": 0, "notified": 0}

    run_with_db(tmp_path, body)


def test_check_all_due_subscriptions_marks_checked(tmp_path, monkeypatch):
    """После проверки подписка помечается checked (не due до след. interval)."""
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path, tags=["1girl"], check_interval_hours=6)
        from app.repositories import subscriptions as subs_repo
        from app.services.gelbooru_subscriptions import check_all_due_subscriptions

        async def fake_search(query, pid=0, limit=50):
            return []

        from app.services.parsers.gelbooru import client as gclient

        monkeypatch.setattr(gclient.gelbooru_client, "search_posts", fake_search)

        # Подписка due (last_checked_at=NULL)
        stats = await check_all_due_subscriptions()
        assert stats["checked"] == 1
        # mark_checked установлен
        sub = await subs_repo.get_subscription(sub_id)
        assert sub["last_checked_at"] is not None

    run_with_db(tmp_path, body)


# ===========================================================================
# gelbooru_subscriptions service: send_seen_post_to_queue
# ===========================================================================


def test_send_seen_post_to_queue_creates_post_and_enqueues(tmp_path, monkeypatch):
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path, tags=["1girl"])
        from app.repositories import gelbooru_subs as gs_repo
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.services.gelbooru_subscriptions import send_seen_post_to_queue

        seen_id = await gs_repo.add_seen_post(
            sub_id, 100, file_unique_id="md5_100",
            source_url="https://img.gelbooru.com/images/100.jpg",
            tags="1girl", rating="safe",
        )

        post_pk = await send_seen_post_to_queue(seen_id, queue_id=None)
        assert post_pk is not None
        assert post_pk > 0

        # posts-пост создан
        post = await posts_repo.get_post(post_pk)
        assert post["source_type"] == "gelbooru"
        assert post["source_id"] == "100"
        assert post["source_url"] == "https://img.gelbooru.com/images/100.jpg"
        assert post["status"] == "new"

        # seen_post помечен sent с post_pk
        seen = await gs_repo.get_seen_post(seen_id)
        assert seen["status"] == "sent"
        assert seen["post_pk"] == post_pk

        # download task в очереди
        queued = await tq.count_by_status("queued")
        assert queued >= 1

    run_with_db(tmp_path, body)


def test_send_seen_post_to_queue_already_sent(tmp_path):
    """seen_post уже sent → None (идемпотентность)."""
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path)
        from app.repositories import gelbooru_subs as gs_repo
        from app.services.gelbooru_subscriptions import send_seen_post_to_queue

        seen_id = await gs_repo.add_seen_post(
            sub_id, 100, file_unique_id="m1", source_url="https://x/a.jpg",
        )
        await gs_repo.mark_sent(seen_id, post_pk=999)
        # Повторная отправка
        result = await send_seen_post_to_queue(seen_id)
        assert result is None

    run_with_db(tmp_path, body)


def test_send_seen_post_to_queue_no_source_url(tmp_path):
    """seen_post без source_url → None."""
    from conftest import run_with_db

    async def body():
        sub_id, _ = await _make_user_and_sub(tmp_path)
        from app.repositories import gelbooru_subs as gs_repo
        from app.services.gelbooru_subscriptions import send_seen_post_to_queue

        seen_id = await gs_repo.add_seen_post(sub_id, 100, file_unique_id="m1")
        # source_url не передан → None
        result = await send_seen_post_to_queue(seen_id)
        assert result is None

    run_with_db(tmp_path, body)


def test_send_seen_post_to_queue_nonexistent_seen(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.services.gelbooru_subscriptions import send_seen_post_to_queue

        result = await send_seen_post_to_queue(99999)
        assert result is None

    run_with_db(tmp_path, body)


# ===========================================================================
# Worker: subscription_loop lifecycle (smoke)
# ===========================================================================


def test_worker_start_with_subs_loop(tmp_path):
    """Worker.start() запускает subscription_loop (если gelbooru_subs_enabled)."""
    from conftest import run_with_db

    async def body():
        from app.config.settings import settings
        from app.worker import get_worker, reset_worker

        reset_worker()
        object.__setattr__(settings, "gelbooru_subs_enabled", True)
        object.__setattr__(settings, "gelbooru_subs_poll_interval_seconds", 0.2)
        object.__setattr__(settings, "worker_poll_interval_seconds", 0.5)
        object.__setattr__(settings, "worker_cleanup_interval_seconds", 0.5)
        try:
            w = get_worker()
            w.start()
            assert w.running is True
            assert w._subscription_task is not None  # noqa: SLF001
            await asyncio.sleep(0.3)
            await w.stop()
            assert w.running is False
        finally:
            object.__setattr__(settings, "gelbooru_subs_enabled", True)
            object.__setattr__(settings, "gelbooru_subs_poll_interval_seconds", 60)
            object.__setattr__(settings, "worker_poll_interval_seconds", 2.0)
            object.__setattr__(settings, "worker_cleanup_interval_seconds", 300)
            reset_worker()

    run_with_db(tmp_path, body)


def test_worker_subs_disabled_no_loop(tmp_path):
    """gelbooru_subs_enabled=False → subscription_loop не запускается."""
    from conftest import run_with_db

    async def body():
        from app.config.settings import settings
        from app.worker import get_worker, reset_worker

        reset_worker()
        object.__setattr__(settings, "gelbooru_subs_enabled", False)
        object.__setattr__(settings, "worker_poll_interval_seconds", 0.5)
        object.__setattr__(settings, "worker_cleanup_interval_seconds", 0.5)
        try:
            w = get_worker()
            w.start()
            assert w.running is True
            assert w._subscription_task is None  # noqa: SLF001
            await w.stop()
        finally:
            object.__setattr__(settings, "gelbooru_subs_enabled", True)
            object.__setattr__(settings, "worker_poll_interval_seconds", 2.0)
            object.__setattr__(settings, "worker_cleanup_interval_seconds", 300)
            reset_worker()

    run_with_db(tmp_path, body)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
