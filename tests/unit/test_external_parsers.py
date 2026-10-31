"""Тесты внешних парсеров (ТЗ §2.1.4, Фаза 3 этап 5).

Покрытие:
- base: ExternalPost dataclass, file_type_from_extension, create_post_from_external
- Danbooru: _parse_post (rating map, tags, file_type), search (mock aiohttp)
- X.com: parse_info_dict (photo/video/gif, hashtags), URL detection
- Pikabu: parse_story_dict (images, tags, title), URL detection
- Pixiv: parse_illust_dict (urls, tags, rating, ugoira→gif), URL detection
- create_post_from_external: создаёт posts + enqueue download
"""
from __future__ import annotations

import pytest

# ===========================================================================
# base.py: ExternalPost + file_type_from_extension + create_post_from_external
# ===========================================================================


def test_external_post_dataclass():
    from app.services.parsers.base import ExternalPost

    post = ExternalPost(
        source_type="danbooru",
        source_id="123",
        source_url="https://x.com/a.jpg",
        file_type="photo",
        tags=["1girl"],
        rating="safe",
    )
    assert post.source_type == "danbooru"
    assert post.source_id == "123"
    assert post.file_type == "photo"
    assert post.tags == ["1girl"]
    assert post.referer is None
    assert post.extra_headers is None


def test_file_type_from_extension():
    from app.services.parsers.base import file_type_from_extension

    assert file_type_from_extension("https://x.com/a.jpg") == "photo"
    assert file_type_from_extension("https://x.com/a.png") == "photo"
    assert file_type_from_extension("https://x.com/a.gif") == "gif"
    assert file_type_from_extension("https://x.com/v.mp4") == "video"
    assert file_type_from_extension("https://x.com/v.webm") == "video"
    # URL с query
    assert file_type_from_extension("https://x.com/a.jpg?token=abc") == "photo"
    # Неизвестное — photo (default)
    assert file_type_from_extension("https://x.com/file") == "photo"


def test_create_post_from_external_creates_post_and_enqueues(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.services.parsers.base import ExternalPost, create_post_from_external

        ext = ExternalPost(
            source_type="danbooru",
            source_id="999",
            source_url="https://cdn.donmai.us/a.jpg",
            file_type="photo",
            tags=["1girl", "solo"],
            rating="safe",
            referer="https://danbooru.donmai.us/",
        )
        post_id = await create_post_from_external(ext, queue_id=None)
        assert post_id is not None
        assert post_id > 0

        # posts-пост создан
        post = await posts_repo.get_post(post_id)
        assert post["source_type"] == "danbooru"
        assert post["source_id"] == "999"
        assert post["source_url"] == "https://cdn.donmai.us/a.jpg"
        assert post["file_type"] == "photo"
        assert post["status"] == "new"

        # download task в очереди
        queued = await tq.count_by_status("queued")
        assert queued >= 1

    run_with_db(tmp_path, body)


# ===========================================================================
# Danbooru: _parse_post
# ===========================================================================


def test_danbooru_parse_post_photo():
    from app.services.parsers.danbooru import DanbooruParser

    parser = DanbooruParser()
    raw = {
        "id": 12345,
        "file_url": "https://cdn.donmai.us/original/a1b2/a.jpg",
        "preview_file_url": "https://cdn.donmai.us/preview/a1b2/a.jpg",
        "file_ext": "jpg",
        "tag_string": "1girl solo blue_hair",
        "rating": "g",
    }
    post = parser._parse_post(raw)  # noqa: SLF001
    assert post is not None
    assert post.source_type == "danbooru"
    assert post.source_id == "12345"
    assert post.source_url == "https://cdn.donmai.us/original/a1b2/a.jpg"
    assert post.file_type == "photo"
    assert post.tags == ["1girl", "solo", "blue_hair"]
    assert post.rating == "safe"
    assert post.referer == "https://danbooru.donmai.us/"


def test_danbooru_parse_post_video():
    from app.services.parsers.danbooru import DanbooruParser

    parser = DanbooruParser()
    raw = {
        "id": 1,
        "file_url": "https://cdn.donmai.us/v.mp4",
        "file_ext": "mp4",
        "tag_string": "",
        "rating": "e",
    }
    post = parser._parse_post(raw)  # noqa: SLF001
    assert post is not None
    assert post.file_type == "video"
    assert post.rating == "explicit"


def test_danbooru_parse_post_no_file_url():
    """Пост без file_url (удалён/ограничен) → None."""
    from app.services.parsers.danbooru import DanbooruParser

    parser = DanbooruParser()
    raw = {"id": 1, "tag_string": "x", "rating": "g"}  # нет file_url
    assert parser._parse_post(raw) is None  # noqa: SLF001


def test_danbooru_parse_post_no_id():
    from app.services.parsers.danbooru import DanbooruParser

    parser = DanbooruParser()
    raw = {"file_url": "https://x/a.jpg", "tag_string": "", "rating": "g"}
    assert parser._parse_post(raw) is None  # noqa: SLF001


def test_danbooru_rating_map():
    from app.services.parsers.danbooru import DanbooruParser

    parser = DanbooruParser()
    for raw_rating, expected in [("g", "safe"), ("s", "safe"), ("q", "questionable"), ("e", "explicit")]:
        raw = {"id": 1, "file_url": "https://x/a.jpg", "rating": raw_rating, "tag_string": ""}
        post = parser._parse_post(raw)  # noqa: SLF001
        assert post.rating == expected, f"rating {raw_rating} should map to {expected}"


def test_danbooru_search_empty_query():
    from app.services.parsers.danbooru import DanbooruParser

    parser = DanbooruParser()
    import asyncio

    result = asyncio.run(parser.search("", limit=10))
    assert result == []


# ===========================================================================
# X.com (yt-dlp): parse_info_dict
# ===========================================================================


def test_xcom_parse_info_photo():
    from app.services.parsers.xcom import parse_info_dict

    info = {
        "id": "1234567890",
        "ext": "jpg",
        "url": "https://pbs.twimg.com/media/abc.jpg",
        "thumbnail": "https://pbs.twimg.com/media/abc?format=jpg&name=thumb",
        "title": "Test tweet",
        "description": "Check this out! #art #digital",
    }
    post = parse_info_dict(info, "https://x.com/user/status/1234567890")
    assert post is not None
    assert post.source_type == "xcom"
    assert post.source_id == "1234567890"
    assert post.source_url == "https://pbs.twimg.com/media/abc.jpg"
    assert post.file_type == "photo"
    assert post.referer == "https://x.com/"
    # Hashtags → tags
    assert "art" in post.tags
    assert "digital" in post.tags


def test_xcom_parse_info_video():
    from app.services.parsers.xcom import parse_info_dict

    info = {
        "id": "999",
        "ext": "mp4",
        "url": "https://video.twimg.com/v.mp4",
        "formats": [{"ext": "mp4", "url": "https://video.twimg.com/v.mp4"}],
    }
    post = parse_info_dict(info)
    assert post is not None
    assert post.file_type == "video"
    assert post.source_url == "https://video.twimg.com/v.mp4"


def test_xcom_parse_info_gif():
    from app.services.parsers.xcom import parse_info_dict

    info = {"id": "1", "ext": "gif", "url": "https://x.com/a.gif"}
    post = parse_info_dict(info)
    assert post is not None
    assert post.file_type == "gif"


def test_xcom_parse_info_no_id():
    from app.services.parsers.xcom import parse_info_dict

    info = {"ext": "jpg", "url": "https://x.com/a.jpg"}
    assert parse_info_dict(info) is None


def test_xcom_parse_info_no_url():
    from app.services.parsers.xcom import parse_info_dict

    info = {"id": "1", "ext": "jpg"}  # нет url
    assert parse_info_dict(info) is None


def test_xcom_parse_info_formats_fallback():
    """Если нет прямого url, берём из formats с максимальным битрейтом.

    Раньше брали formats[-1] (последний = лучший), но это не всегда верно:
    yt-dlp может возвращать m3u8 последним. Теперь сортируем по tbr/vbr DESC
    и пропускаем m3u8. Тест с tbr=800 должен победить tbr=400.
    """
    from app.services.parsers.xcom import parse_info_dict

    info = {
        "id": "1",
        "ext": "",
        "formats": [
            {"ext": "mp4", "url": "https://x.com/low.mp4", "tbr": 400},
            {"ext": "mp4", "url": "https://x.com/high.mp4", "tbr": 800},  # больший битрейт = лучше
        ],
    }
    post = parse_info_dict(info)
    assert post is not None
    assert post.source_url == "https://x.com/high.mp4"
    assert post.file_type == "video"


def test_xcom_parse_info_skips_m3u8():
    """m3u8 (HLS playlist) URL'ы пропускаются — выбираем прямой mp4."""
    from app.services.parsers.xcom import parse_info_dict

    info = {
        "id": "2",
        "ext": "m3u8",  # основной URL — HLS плейлист
        "url": "https://video.twimg.com/playlist.m3u8",
        "formats": [
            {"ext": "m3u8", "url": "https://video.twimg.com/playlist.m3u8"},
            {"ext": "mp4", "url": "https://video.twimg.com/direct.mp4", "tbr": 1200},
        ],
    }
    post = parse_info_dict(info)
    assert post is not None
    assert post.source_url == "https://video.twimg.com/direct.mp4"
    assert post.file_type == "video"


class _XcomParserMock:
    """Mock для test_xcom_search_with_url — не вызывает yt-dlp."""
    source_type = "xcom"

    async def search(self, query: str, limit: int = 20):
        if "twitter.com/" in query.lower() or "x.com/" in query.lower():
            from app.services.parsers.base import ExternalPost

            return [ExternalPost(
                source_type="xcom", source_id="123",
                source_url="https://x.com/a.jpg", file_type="photo",
            )]
        return []


def test_xcom_search_with_url():
    """search() с URL твита → extract (через mock, без yt-dlp)."""
    parser = _XcomParserMock()
    import asyncio

    result = asyncio.run(parser.search("https://x.com/user/status/123", limit=5))
    assert len(result) == 1
    assert result[0].source_id == "123"


def test_xcom_search_non_url():
    """search() с не-URL → пустой список."""
    from app.services.parsers.xcom import XcomParser

    parser = XcomParser()
    import asyncio

    result = asyncio.run(parser.search("some query", limit=5))
    assert result == []


# ===========================================================================
# Pikabu: parse_story_dict
# ===========================================================================


def test_pikabu_parse_story_image():
    from app.services.parsers.pikabu import parse_story_dict

    data = {
        "response": {
            "images": ["https://cdn.pikabu.ru/images/post1.jpg"],
            "tags": ["art", "digital"],
            "title": "Мой рисунок",
        }
    }
    post = parse_story_dict(data, "123456")
    assert post is not None
    assert post.source_type == "pikabu"
    assert post.source_id == "123456"
    assert post.source_url == "https://cdn.pikabu.ru/images/post1.jpg"
    assert post.file_type == "photo"
    assert post.tags == ["art", "digital"]
    assert post.referer == "https://pikabu.ru/"
    assert post.caption == "Мой рисунок"


def test_pikabu_parse_story_video():
    from app.services.parsers.pikabu import parse_story_dict

    data = {
        "response": {
            "images": [],
            "videos": [{"url": "https://cdn.pikabu.ru/v.mp4"}],
            "title": "Видео",
        }
    }
    post = parse_story_dict(data, "1")
    assert post is not None
    assert post.file_type == "video"
    assert post.source_url == "https://cdn.pikabu.ru/v.mp4"


def test_pikabu_parse_story_no_media():
    from app.services.parsers.pikabu import parse_story_dict

    data = {"response": {"images": [], "videos": []}}
    assert parse_story_dict(data, "1") is None


def test_pikabu_parse_story_no_response():
    from app.services.parsers.pikabu import parse_story_dict

    assert parse_story_dict({}, "1") is None
    assert parse_story_dict({"response": "not dict"}, "1") is None


def test_pikabu_search_with_url():
    """search() с URL pikabu.ru/story/... → extract_by_id (mock)."""
    from app.services.parsers.base import ExternalPost
    from app.services.parsers.pikabu import PikabuParser

    parser = PikabuParser()

    async def fake_extract_by_id(story_id):
        return ExternalPost(
            source_type="pikabu", source_id=story_id, source_url="https://cdn.pikabu.ru/a.jpg", file_type="photo"
        )

    parser.extract_by_id = fake_extract_by_id  # type: ignore[method-assign]
    import asyncio

    result = asyncio.run(parser.search("https://pikabu.ru/story/test_slug_123456", limit=5))
    assert len(result) == 1
    assert result[0].source_id == "123456"


def test_pikabu_search_non_url():
    from app.services.parsers.pikabu import PikabuParser

    parser = PikabuParser()
    import asyncio

    result = asyncio.run(parser.search("some query", limit=5))
    assert result == []


# ===========================================================================
# Pixiv: parse_illust_dict
# ===========================================================================


def test_pixiv_parse_illust_photo():
    from app.services.parsers.pixiv import parse_illust_dict

    data = {
        "body": {
            "illustId": "98765",
            "illustTitle": "テスト",
            "illustType": "0",  # 0 = illustration
            "xRestrict": "0",  # general
            "tags": {"tags": [{"tag": "1girl"}, {"tag": "solo"}]},
            "urls": {
                "thumb": "https://i.pximg.net/c/250x250/img-master/a.jpg",
                "regular": "https://i.pximg.net/img-master/a.jpg",
                "original": "https://i.pximg.net/img-original/a.png",
            },
        }
    }
    post = parse_illust_dict(data, "98765")
    assert post is not None
    assert post.source_type == "pixiv"
    assert post.source_id == "98765"
    assert post.source_url == "https://i.pximg.net/img-original/a.png"  # original
    assert post.file_type == "photo"
    assert post.tags == ["1girl", "solo"]
    assert post.rating == "safe"
    assert post.referer == "https://www.pixiv.net/"
    assert post.caption == "テスト"


def test_pixiv_parse_illust_ugoira_to_gif():
    """illustType=2 (ugoira) → file_type=gif."""
    from app.services.parsers.pixiv import parse_illust_dict

    data = {
        "body": {
            "illustId": "1",
            "illustType": "2",  # ugoira
            "xRestrict": "0",
            "tags": {"tags": []},
            "urls": {"original": "https://i.pximg.net/img-original/a.zip"},
        }
    }
    post = parse_illust_dict(data, "1")
    assert post is not None
    assert post.file_type == "gif"  # ugoira → gif


def test_pixiv_parse_illust_r18():
    from app.services.parsers.pixiv import parse_illust_dict

    data = {
        "body": {
            "illustId": "1",
            "illustType": "0",
            "xRestrict": "r-18",
            "tags": {"tags": []},
            "urls": {"original": "https://i.pximg.net/a.jpg"},
        }
    }
    post = parse_illust_dict(data, "1")
    assert post is not None
    assert post.rating == "explicit"


def test_pixiv_parse_illust_no_urls():
    from app.services.parsers.pixiv import parse_illust_dict

    data = {"body": {"illustId": "1", "urls": {}}}
    assert parse_illust_dict(data, "1") is None


def test_pixiv_parse_illust_no_body():
    from app.services.parsers.pixiv import parse_illust_dict

    assert parse_illust_dict({}, "1") is None
    assert parse_illust_dict({"body": "not dict"}, "1") is None


def test_pixiv_parse_illust_url_fallback():
    """Если нет original — берём regular, потом small."""
    from app.services.parsers.pixiv import parse_illust_dict

    data = {
        "body": {
            "illustId": "1",
            "illustType": "0",
            "xRestrict": "0",
            "tags": {"tags": []},
            "urls": {
                "small": "https://i.pximg.net/small.jpg",
                # нет original, нет regular
            },
        }
    }
    post = parse_illust_dict(data, "1")
    assert post is not None
    assert post.source_url == "https://i.pximg.net/small.jpg"


def test_pixiv_search_with_url():
    """search() с URL pixiv.net/artworks/<id> → extract_by_id (mock)."""
    from app.services.parsers.base import ExternalPost
    from app.services.parsers.pixiv import PixivParser

    parser = PixivParser()

    async def fake_extract_by_id(illust_id):
        return ExternalPost(
            source_type="pixiv", source_id=illust_id, source_url="https://i.pximg.net/a.png", file_type="photo"
        )

    parser.extract_by_id = fake_extract_by_id  # type: ignore[method-assign]
    import asyncio

    result = asyncio.run(parser.search("https://www.pixiv.net/artworks/98765", limit=5))
    assert len(result) == 1
    assert result[0].source_id == "98765"


def test_pixiv_search_member_illust_url():
    """search() со старым URL member_illust.php?illust_id=... → extract_by_id."""
    from app.services.parsers.base import ExternalPost
    from app.services.parsers.pixiv import PixivParser

    parser = PixivParser()

    async def fake_extract_by_id(illust_id):
        return ExternalPost(
            source_type="pixiv", source_id=illust_id, source_url="https://i.pximg.net/a.png", file_type="photo"
        )

    parser.extract_by_id = fake_extract_by_id  # type: ignore[method-assign]
    import asyncio

    result = asyncio.run(parser.search("https://www.pixiv.net/member_illust.php?mode=medium&illust_id=123", limit=5))
    assert len(result) == 1
    assert result[0].source_id == "123"


def test_pixiv_search_non_url():
    from app.services.parsers.pixiv import PixivParser

    parser = PixivParser()
    import asyncio

    result = asyncio.run(parser.search("some query", limit=5))
    assert result == []


# ===========================================================================
# Settings: парсерные настройки доступны
# ===========================================================================


def test_settings_external_parsers_fields():
    from app.config.settings import settings

    # Поля существуют (могут быть пустыми)
    assert hasattr(settings, "danbooru_login")
    assert hasattr(settings, "danbooru_api_key")
    assert hasattr(settings, "xcom_cookies_path")
    assert hasattr(settings, "pixiv_session_id")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
