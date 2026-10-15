"""Тесты тегов архива (_build_tags, _sanitize_tag_id) — выделено из test_regression.py (PR-40).

Покрывает app/services/archive.py:
  - _sanitize_tag_id (минус из chat_id, None, строка, число)
  - _build_tags (source_type telegram_channel, telegram_direct; multiple file_types)
  - Хэштеги не должны содержать минус (Telegram ломает #-...)
"""
from __future__ import annotations

import pytest


def test_archive_tags_sanitize_minus():
    """Теги архива: минус убирается из chat_id (#from_100..., не #from_-100...)."""
    from app.services.archive import _build_tags, _sanitize_tag_id

    # chat_id с минусом
    assert _sanitize_tag_id(-1002435408188) == "1002435408188"
    assert _sanitize_tag_id("-1002435408188") == "1002435408188"
    assert _sanitize_tag_id(None) == ""

    # Полные теги
    post = {
        "source_type": "telegram_channel",
        "source_id": "-1002435408188",
    }
    tags = _build_tags(post, target_channel_id=-1001258194124, all_types=["photo"])
    assert "#from_1002435408188" in tags, f"Тег from сломан: {tags}"
    assert "#to_1001258194124" in tags, f"Тег to сломан: {tags}"
    assert "#pic" in tags, f"Тег pic без #: {tags}"
    assert "#from_-" not in tags, f"Минус не убран: {tags}"


def test_archive_tags_dm_source():
    """Теги архива: источник ЛС → #from_dm."""
    from app.services.archive import _build_tags

    post = {"source_type": "telegram_direct", "source_id": "479924833"}
    tags = _build_tags(post, target_channel_id=-1002257882757, all_types=["photo", "video"])
    assert "#from_dm" in tags
    assert "#to_1002257882757" in tags
    assert "#pic" in tags
    assert "#vid" in tags


def test_archive_tags_no_minus_in_hashtags():
    """Все хэштеги не содержат минус (Telegram ломает #-...)."""
    from app.services.archive import _build_tags

    post = {"source_type": "telegram_channel", "source_id": "-1001686639428"}
    tags = _build_tags(post, target_channel_id=-1002257882757, all_types=["photo"])
    for tag in tags.split():
        assert not tag.startswith("#-"), f"Хэштег с минусом: {tag}"
        assert tag.startswith("#"), f"Тег без #: {tag}"


# ---------------------------------------------------------------------------
# PR-40: новые edge-cases для _sanitize_tag_id
# ---------------------------------------------------------------------------


class TestSanitizeTagId:
    """_sanitize_tag_id — прямые edge-case проверки (раньше только косвенно)."""

    def test_int_zero(self):
        """0 → "0" (строка '0', не пустая)."""
        from app.services.archive import _sanitize_tag_id
        assert _sanitize_tag_id(0) == "0"

    def test_positive_int(self):
        """Положительное int — без минуса, остаётся как есть."""
        from app.services.archive import _sanitize_tag_id
        assert _sanitize_tag_id(12345) == "12345"

    def test_positive_str(self):
        """Положительное число как строка — без изменений."""
        from app.services.archive import _sanitize_tag_id
        assert _sanitize_tag_id("98765") == "98765"

    def test_str_with_leading_minus(self):
        """Строка с минусом — минус убирается."""
        from app.services.archive import _sanitize_tag_id
        assert _sanitize_tag_id("-100") == "100"

    def test_empty_string(self):
        """Пустая строка → пустая строка."""
        from app.services.archive import _sanitize_tag_id
        assert _sanitize_tag_id("") == ""


class TestBuildTagsEdgeCases:
    """Доп. edge-cases _build_tags — все типы файлов."""

    def test_all_file_types(self):
        """Все 4 типа файлов дают правильные теги: pic/vid/gif/doc."""
        from app.services.archive import _build_tags

        post = {"source_type": "telegram_direct", "source_id": "111"}
        tags = _build_tags(
            post,
            target_channel_id=-100,
            all_types=["photo", "video", "gif", "document"],
        )
        tags_list = tags.split()
        assert "#pic" in tags_list
        assert "#vid" in tags_list
        assert "#gif" in tags_list
        assert "#doc" in tags_list

    def test_unknown_source_type_defaults_to_dm_tag(self):
        """Неизвестный source_type — defaults to #from_dm (см. _source_tag).

        Реализация: если st != "telegram_channel", возвращается "#from_dm".
        Это сделано специально: всё, что не из канала, трактуется как «из ЛС».
        """
        from app.services.archive import _build_tags

        post = {"source_type": "unknown_source", "source_id": "abc"}
        tags = _build_tags(post, target_channel_id=-100, all_types=["photo"])
        # to и pic — есть
        assert "#to_100" in tags
        assert "#pic" in tags
        # unknown source_type → #from_dm (default)
        assert "#from_dm" in tags


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
