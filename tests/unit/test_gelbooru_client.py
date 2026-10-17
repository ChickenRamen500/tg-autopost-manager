"""Тесты GelbooruClient._parse_tag_response + cache (без HTTP).

PR-40 (H-T3): app/services/parsers/gelbooru/{client,cache}.py раньше не имели тестов —
coverage = 0%. Покрываем:
  - 6 веток _parse_tag_response (None / list / dict с "post"-list / dict с "post"-dict /
    dict с "id" / прочий dict).
  - cache.get/set/delete/clear_all/stats/cleanup_expired с mock-временем.

Никакой реальной сети — только статический разбор JSON-структур и TTL-логика.
"""
from __future__ import annotations

from unittest.mock import patch

from app.services.parsers.gelbooru import cache
from app.services.parsers.gelbooru.client import GelbooruClient

# ---------------------------------------------------------------------------
# _parse_tag_response — 6 веток
# ---------------------------------------------------------------------------


class TestParseTagResponse:
    """GelbooruClient._parse_tag_response — статический разбор ответа s=tag API."""

    def test_none_returns_empty(self):
        """Ветка 1: data is None → [] (404 или network error)."""
        assert GelbooruClient._parse_tag_response(None) == []

    def test_list_returns_filtered(self):
        """Ветка 2: data is list → фильтруем только dict'ы с name."""
        data = [
            {"name": "tag1", "type": 0},
            {"name": "tag2", "type": 1},
            {"name": "", "type": 0},  # без name — отбрасывается
            {"type": 0},  # нет name — отбрасывается
            "not_a_dict",  # не dict — отбрасывается
            42,
            None,
        ]
        result = GelbooruClient._parse_tag_response(data)
        assert len(result) == 2
        assert result[0]["name"] == "tag1"
        assert result[1]["name"] == "tag2"

    def test_empty_list_returns_empty(self):
        """Пустой list → []."""
        assert GelbooruClient._parse_tag_response([]) == []

    def test_dict_with_post_key_list(self):
        """Ветка 3: data is dict с "tag" key → list (фильтрованный)."""
        data = {
            "@attributes": {"count": 2},
            "tag": [
                {"name": "artist:x"},
                {"name": "char:y"},
                {"type": 0},  # нет name — отбрасывается
            ],
        }
        result = GelbooruClient._parse_tag_response(data)
        assert len(result) == 2
        assert result[0]["name"] == "artist:x"

    def test_dict_with_post_key_single_dict(self):
        """Ветка 4: data is dict с "tag" key → одиночный dict (один тег в ответе)."""
        data = {
            "@attributes": {"count": 1},
            "tag": {"name": "single_tag", "type": 1},
        }
        result = GelbooruClient._parse_tag_response(data)
        assert len(result) == 1
        assert result[0]["name"] == "single_tag"

    def test_dict_with_post_key_single_dict_without_name(self):
        """Ветка 4 (negative): dict с "tag" key но без name → []."""
        data = {"tag": {"type": 0}}  # нет name
        assert GelbooruClient._parse_tag_response(data) == []

    def test_dict_with_id_key(self):
        """Ветка 5: data is dict с "id" key (single post response для tag API)."""
        data = {"name": "by_id_tag", "id": 12345, "type": 0}
        result = GelbooruClient._parse_tag_response(data)
        assert len(result) == 1
        assert result[0]["name"] == "by_id_tag"

    def test_dict_without_post_or_id_returns_empty(self):
        """Ветка 6: data is dict без "tag"/"name"/"id" → []."""
        data = {"@attributes": {"count": 0}}
        assert GelbooruClient._parse_tag_response(data) == []

    def test_dict_without_post_but_with_name(self):
        """Ветка 6 positive: data is dict без "tag", но с name → [data]."""
        data = {"name": "lone_tag", "count": 100}
        result = GelbooruClient._parse_tag_response(data)
        assert len(result) == 1
        assert result[0]["name"] == "lone_tag"

    def test_dict_with_tag_key_empty_list(self):
        """dict с "tag": [] → []."""
        data = {"tag": []}
        assert GelbooruClient._parse_tag_response(data) == []

    def test_dict_with_tag_key_string(self):
        """dict с "tag": "string" — не dict, не list → []."""
        data = {"tag": "unexpected_string"}
        assert GelbooruClient._parse_tag_response(data) == []


# ---------------------------------------------------------------------------
# Cache — get/set/delete/clear_all/stats/cleanup_expired
# ---------------------------------------------------------------------------


class TestGelbooruCache:
    """In-memory TTL-кеш Gelbooru API."""

    def setup_method(self):
        """Чистим cache перед каждым тестом."""
        cache.clear_all()

    def teardown_method(self):
        cache.clear_all()

    def test_set_and_get(self):
        """set + get возвращает значение до истечения TTL."""
        cache.set("k1", {"posts": [1, 2, 3]}, ttl=60)
        assert cache.get("k1") == {"posts": [1, 2, 3]}

    def test_get_missing_key_returns_none(self):
        """get для несуществующего ключа → None."""
        assert cache.get("nope") is None

    def test_get_expired_returns_none(self):
        """Если запись истекла по TTL — get возвращает None и удаляет запись."""
        # TTL=0 — моментально истекает (time.monotonic() >= expire_at всегда true).
        # Замораживаем время: set с TTL=10, потом "продвигаем" время на 11 сек.
        with patch("app.services.parsers.gelbooru.cache.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            cache.set("k2", "value", ttl=10)  # expire_at = 110
            # Запись ещё жива (now=100 < 110)
            mock_time.monotonic.return_value = 100.0
            assert cache.get("k2") == "value"
            # Теперь время уходит в будущее (now=120 > 110) — истекла
            mock_time.monotonic.return_value = 120.0
            assert cache.get("k2") is None

    def test_delete_existing_key_returns_true(self):
        """delete для существующего ключа → True, ключ удаляется."""
        cache.set("k3", "v3", ttl=60)
        assert cache.delete("k3") is True
        assert cache.get("k3") is None

    def test_delete_missing_key_returns_false(self):
        """delete для отсутствующего ключа → False (не падает)."""
        assert cache.delete("nope") is False

    def test_clear_all_removes_everything(self):
        """clear_all — очищает весь кеш, возвращает количество удалённых."""
        cache.set("a", 1, ttl=60)
        cache.set("b", 2, ttl=60)
        cache.set("c", 3, ttl=60)
        removed = cache.clear_all()
        assert removed == 3
        assert cache.get("a") is None
        assert cache.get("b") is None
        assert cache.get("c") is None

    def test_clear_all_empty_returns_zero(self):
        """clear_all на пустом кеше → 0 (не логирует)."""
        assert cache.clear_all() == 0

    def test_stats_counts_alive_and_expired(self):
        """stats возвращает total/alive/expired — отдельно живые и истёкшие."""
        with patch("app.services.parsers.gelbooru.cache.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            cache.set("alive1", "v1", ttl=100)  # истечёт при 200
            cache.set("alive2", "v2", ttl=100)
            cache.set("expired1", "v3", ttl=10)  # истечёт при 110
            cache.set("expired2", "v4", ttl=10)

            mock_time.monotonic.return_value = 150.0  # 110 < 150, 200 > 150
            stats = cache.stats()
            assert stats["total"] == 4
            assert stats["alive"] == 2
            assert stats["expired"] == 2

    def test_cleanup_expired_removes_only_expired(self):
        """cleanup_expired удаляет только истёкшие записи, возвращает количество."""
        with patch("app.services.parsers.gelbooru.cache.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            cache.set("keep1", "v1", ttl=100)  # истечёт при 200
            cache.set("keep2", "v2", ttl=100)
            cache.set("drop1", "v3", ttl=10)  # истечёт при 110
            cache.set("drop2", "v4", ttl=10)

            mock_time.monotonic.return_value = 150.0
            removed = cache.cleanup_expired()
            assert removed == 2
            # Живые остались
            assert cache.get("keep1") == "v1"
            assert cache.get("keep2") == "v2"
            # Истёкшие удалены
            assert cache.get("drop1") is None
            assert cache.get("drop2") is None

    def test_cleanup_expired_nothing_to_clean(self):
        """cleanup_expired на пустом кеше → 0."""
        assert cache.cleanup_expired() == 0

    def test_init_cache_clears_all(self):
        """init_cache — псевдоним clear_all + лог."""
        cache.set("x", "y", ttl=60)
        cache.init_cache()
        assert cache.get("x") is None

    def test_clear_all_cache_backward_compat(self):
        """clear_all_cache — обратная совместимость со старым именем из gbooru_tg."""
        cache.set("z", "y", ttl=60)
        assert cache.clear_all_cache() == 1
        assert cache.get("z") is None

    def test_set_overwrites_existing_key(self):
        """set на существующий ключ — перезаписывает значение."""
        cache.set("k", "v1", ttl=60)
        cache.set("k", "v2", ttl=60)
        assert cache.get("k") == "v2"


# ---------------------------------------------------------------------------
# GelbooruClient constructor + close (без сети)
# ---------------------------------------------------------------------------


class TestGelbooruClientLifecycle:
    """GelbooruClient: __init__, close — без HTTP-запросов."""

    def test_init_creates_client_without_session(self):
        """__init__ — создаёт client, но session лениво."""
        client = GelbooruClient()
        assert client._session is None
        assert client._last_request_time == 0.0

    def test_close_null_session_when_no_session(self):
        """close() без активной сессии — не падает, _session остаётся None."""
        import asyncio

        client = GelbooruClient()
        # session не создавался
        asyncio.run(client.close())
        # PR-40 LOW-фикс: close() должен обнулять _session
        assert client._session is None

    def test_close_idempotent_multiple_calls(self):
        """close() можно вызывать несколько раз — не падает."""
        import asyncio

        client = GelbooruClient()
        asyncio.run(client.close())
        asyncio.run(client.close())
        asyncio.run(client.close())
        assert client._session is None


# ---------------------------------------------------------------------------
# Фильтрация тегов с мусором (доп. сценарии _parse_tag_response)
# ---------------------------------------------------------------------------


class TestParseTagResponseEdgeCases:
    """Доп. edge-cases _parse_tag_response — защита от мусора в ответе API."""

    def test_dict_with_tag_key_null(self):
        """data["tag"] = None → [] (фильтр not isinstance(raw, list|dict))."""
        data = {"tag": None}
        # raw = None — не list, не dict с name → []
        assert GelbooruClient._parse_tag_response(data) == []

    def test_dict_with_tag_key_number(self):
        """data["tag"] = 42 → [] (фильтр not isinstance(raw, list|dict))."""
        data = {"tag": 42}
        assert GelbooruClient._parse_tag_response(data) == []

    def test_string_input_returns_empty(self):
        """data is string (не dict, не list, не None) → []."""
        assert GelbooruClient._parse_tag_response("unexpected") == []  # type: ignore[arg-type]

    def test_int_input_returns_empty(self):
        """data is int → []."""
        assert GelbooruClient._parse_tag_response(42) == []  # type: ignore[arg-type]
