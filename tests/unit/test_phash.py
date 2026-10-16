"""Тесты pHash-дедупликации (ТЗ §2.4.3, Фаза 3 этап 2).

Покрытие:
- phash.is_image_file: расширение + mime
- phash.compute_phash_from_image: возвращает hex-строку, None при disabled
- phash.compute_phash: из файла (реальное изображение через PIL), None на не-изображении
- phash.compute_phash_from_bytes: из байтов
- phash.compute_and_store_phash: полный цикл (вычислить + сохранить в БД)
- phash.check_visual_duplicate: поиск через find_phash_near
- worker: после скачивания pHash вычисляется → pending (уникален) / possible_duplicate (дубликат)
- worker: pHash disabled → пропускается, сразу pending
- worker: видео → pHash не вычисляется (не изображение), pending
- duplicates.set_phash_atomic + find_phash_near: интеграционный цикл
"""
from __future__ import annotations

import io

import pytest

# ===========================================================================
# Helper: создаём тестовые изображения через PIL
# ===========================================================================


def _make_image_bytes(color: tuple[int, int, int] = (255, 0, 0), size: tuple[int, int] = (64, 64)) -> bytes:
    """Создаёт PNG-изображение в байтах (для compute_phash_from_bytes)."""
    from PIL import Image

    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_image_file(tmp_path, name: str = "test.png", color: tuple[int, int, int] = (255, 0, 0)) -> str:
    """Создаёт PNG-файл на диске (для compute_phash)."""
    data = _make_image_bytes(color)
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


# ===========================================================================
# is_image_file
# ===========================================================================


def test_is_image_file_by_extension():
    from app.services.dedup.phash import is_image_file

    assert is_image_file("photo.jpg") is True
    assert is_image_file("photo.jpeg") is True
    assert is_image_file("photo.png") is True
    assert is_image_file("anim.gif") is True
    assert is_image_file("photo.webp") is True
    assert is_image_file("PHOTO.JPG") is True  # case-insensitive


def test_is_image_file_by_mime():
    from app.services.dedup.phash import is_image_file

    assert is_image_file("noext", mime_type="image/jpeg") is True
    assert is_image_file("noext", mime_type="image/png") is True
    assert is_image_file("file.bin", mime_type="application/octet-stream") is False
    assert is_image_file("file.mp4", mime_type="video/mp4") is False


def test_is_image_file_negative():
    from app.services.dedup.phash import is_image_file

    assert is_image_file("video.mp4") is False
    assert is_image_file("audio.mp3") is False
    assert is_image_file("doc.pdf") is False
    assert is_image_file("noext") is False
    assert is_image_file("") is False


# ===========================================================================
# compute_phash_from_image / compute_phash / compute_phash_from_bytes
# ===========================================================================


def test_compute_phash_from_image_returns_hex():
    from PIL import Image

    from app.services.dedup.phash import compute_phash_from_image

    img = Image.new("RGB", (64, 64), color=(128, 64, 200))
    h = compute_phash_from_image(img)
    assert h is not None
    # 64-битный pHash = 16 hex символов
    assert len(h) == 16
    # Все символы — hex
    int(h, 16)  # не падает = валидный hex


def test_compute_phash_from_image_when_disabled():
    from PIL import Image

    from app.config.settings import settings
    from app.services.dedup.phash import compute_phash_from_image

    orig = settings.phash_enabled
    object.__setattr__(settings, "phash_enabled", False)
    try:
        img = Image.new("RGB", (64, 64))
        assert compute_phash_from_image(img) is None
    finally:
        object.__setattr__(settings, "phash_enabled", orig)


def test_compute_phash_from_bytes_real_image():
    from app.services.dedup.phash import compute_phash_from_bytes

    data = _make_image_bytes(color=(100, 50, 25))
    h = compute_phash_from_bytes(data)
    assert h is not None
    assert len(h) == 16


def test_compute_phash_from_bytes_empty():
    from app.services.dedup.phash import compute_phash_from_bytes

    assert compute_phash_from_bytes(b"") is None


def test_compute_phash_from_bytes_invalid():
    from app.services.dedup.phash import compute_phash_from_bytes

    # Не изображение (мусорные байты)
    assert compute_phash_from_bytes(b"not an image at all") is None


def test_compute_phash_from_file(tmp_path):
    from app.services.dedup.phash import compute_phash

    path = _make_image_file(tmp_path, "test.png", color=(200, 100, 50))
    h = compute_phash(path)
    assert h is not None
    assert len(h) == 16


def test_compute_phash_nonexistent_file(tmp_path):
    from app.services.dedup.phash import compute_phash

    assert compute_phash(str(tmp_path / "missing.png")) is None


def test_compute_phash_non_image_file(tmp_path):
    from app.services.dedup.phash import compute_phash

    # Текстовый файл с расширением .txt
    p = tmp_path / "notes.txt"
    p.write_text("hello world")
    assert compute_phash(str(p)) is None


def test_compute_phash_disabled(tmp_path):
    from app.config.settings import settings
    from app.services.dedup.phash import compute_phash

    path = _make_image_file(tmp_path)
    orig = settings.phash_enabled
    object.__setattr__(settings, "phash_enabled", False)
    try:
        assert compute_phash(path) is None
    finally:
        object.__setattr__(settings, "phash_enabled", orig)


def test_compute_phash_consistent_for_same_image(tmp_path):
    """Одинаковое изображение → одинаковый pHash (детерминизм)."""
    from app.services.dedup.phash import compute_phash

    path1 = _make_image_file(tmp_path, "img1.png", color=(123, 45, 67))
    path2 = _make_image_file(tmp_path, "img2.png", color=(123, 45, 67))
    h1 = compute_phash(path1)
    h2 = compute_phash(path2)
    assert h1 is not None and h2 is not None
    assert h1 == h2


def test_compute_phash_resize_invariant(tmp_path):
    """pHash устойчив к ресайзу: то же изображение другого размера → близкий хеш (distance ≤ max).

    pHash (perceptual hash) устойчив к ресайзу — это его ключевое свойство для
    дедупликации. Используем изображение с чёткими особенностями (белый круг на
    чёрном фоне), которое даёт стабильный pHash при ресайзе.
    """
    from PIL import Image, ImageDraw

    from app.repositories.duplicates import _hamming_hex
    from app.services.dedup.phash import compute_phash_from_image

    # Создаём изображение с чёткой особенностью (круг) — pHash устойчив к ресайзу
    img1 = Image.new("RGB", (256, 256), color=(0, 0, 0))
    draw1 = ImageDraw.Draw(img1)
    draw1.ellipse([64, 64, 192, 192], fill=(255, 255, 255))

    img2 = img1.resize((128, 128))  # ресайз в 2 раза меньше

    h1 = compute_phash_from_image(img1)
    h2 = compute_phash_from_image(img2)
    assert h1 is not None and h2 is not None

    # Distance должен быть маленьким (pHash устойчив к ресайзу)
    # max_distance по умолчанию = 5, но для ресайза возможны небольшие отличия
    # из-за DCT на разных размерах. Допускаем до 10 (всё ещё «похожие»).
    dist = _hamming_hex(h1, h2)
    assert 0 <= dist <= 10, f"pHash distance {dist} > 10 для ресайза того же изображения"


# ===========================================================================
# compute_and_store_phash (полный цикл с БД)
# ===========================================================================


def test_compute_and_store_phash_success(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.repositories import duplicates as dup_repo
        from app.repositories import posts as posts_repo
        from app.services.dedup.phash import compute_and_store_phash

        # Создаём пост
        pid = await posts_repo.create_post(
            source_type="gelbooru", file_type="photo", source_url="https://x/a.jpg", status="downloading"
        )
        # Создаём файл-изображение
        path = _make_image_file(tmp_path, "post.png")
        phash = await compute_and_store_phash(pid, path, "photo")
        assert phash is not None
        assert len(phash) == 16

        # pHash должен сохраниться в posts И duplicate_hashes
        post = await posts_repo.get_post(pid)
        assert post["phash"] == phash
        # duplicate_hashes должна содержать phash-запись
        dupes = await dup_repo.find_by_hash(phash, "phash")
        assert any(d["post_id"] == pid for d in dupes)

    run_with_db(tmp_path, body)


def test_compute_and_store_phash_non_image(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo
        from app.services.dedup.phash import compute_and_store_phash

        pid = await posts_repo.create_post(source_type="x", file_type="video", status="downloading")
        # Текстовый файл (не изображение)
        p = tmp_path / "video.mp4"
        p.write_text("fake video")
        phash = await compute_and_store_phash(pid, str(p), "video")
        assert phash is None

    run_with_db(tmp_path, body)


def test_compute_and_store_phash_disabled(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.config.settings import settings
        from app.repositories import posts as posts_repo
        from app.services.dedup.phash import compute_and_store_phash

        orig = settings.phash_enabled
        object.__setattr__(settings, "phash_enabled", False)
        try:
            pid = await posts_repo.create_post(source_type="x", file_type="photo", status="downloading")
            path = _make_image_file(tmp_path, "img.png")
            assert await compute_and_store_phash(pid, path, "photo") is None
        finally:
            object.__setattr__(settings, "phash_enabled", orig)

    run_with_db(tmp_path, body)


# ===========================================================================
# check_visual_duplicate (поиск через find_phash_near)
# ===========================================================================


def test_check_visual_duplicate_finds_exact(tmp_path):
    """Точное совпадение pHash (distance=0) — find_by_hash находит (find_phash_near исключает)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import duplicates as dup_repo
        from app.repositories import posts as posts_repo
        from app.services.dedup.phash import check_visual_duplicate

        # Существующий пост с pHash X
        pid1 = await posts_repo.create_post(source_type="x", file_type="photo", status="sent")
        await dup_repo.set_phash_atomic(pid1, "ffff0000ffff0000")

        # Поиск с тем же pHash, исключая pid1 (имитируем новый пост pid2 с этим pHash)
        pid2 = await posts_repo.create_post(source_type="x", file_type="photo", status="new")
        dupes = await check_visual_duplicate("ffff0000ffff0000", exclude_post_id=pid2)
        # Должен найти pid1 (distance=0) — точное совпадение через find_by_hash
        assert len(dupes) >= 1
        assert any(d["post_id"] == pid1 and d["distance"] == 0 for d in dupes)

    run_with_db(tmp_path, body)


def test_check_visual_duplicate_finds_near(tmp_path):
    """Ближнее совпадение (distance=1) — find_phash_near находит."""
    from conftest import run_with_db

    async def body():
        from app.repositories import duplicates as dup_repo
        from app.repositories import posts as posts_repo
        from app.services.dedup.phash import check_visual_duplicate

        # Пост с pHash X
        pid1 = await posts_repo.create_post(source_type="x", file_type="photo", status="sent")
        await dup_repo.set_phash_atomic(pid1, "ffff0000ffff0001")  # 1 бит от ffff0000ffff0000

        # Поиск с pHash, отличающимся на 1 бит → distance=1 ≤ 5 → дубликат
        dupes = await check_visual_duplicate("ffff0000ffff0000", exclude_post_id=999)
        assert len(dupes) >= 1
        assert any(d["post_id"] == pid1 for d in dupes)
        assert dupes[0]["distance"] == 1

    run_with_db(tmp_path, body)


def test_check_visual_duplicate_no_match(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.repositories import duplicates as dup_repo
        from app.repositories import posts as posts_repo
        from app.services.dedup.phash import check_visual_duplicate

        pid1 = await posts_repo.create_post(source_type="x", file_type="photo", status="sent")
        await dup_repo.set_phash_atomic(pid1, "0000000000000000")

        # Полностью другой pHash (distance=64) → не дубликат при max_distance=5
        dupes = await check_visual_duplicate("ffffffffffffffff", exclude_post_id=999)
        assert len(dupes) == 0

    run_with_db(tmp_path, body)


def test_check_visual_duplicate_respects_max_distance(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.config.settings import settings
        from app.repositories import duplicates as dup_repo
        from app.repositories import posts as posts_repo
        from app.services.dedup.phash import check_visual_duplicate

        # pHash с distance=4 от эталона (4 бита отличаются: 0x0F = 4 бита)
        pid1 = await posts_repo.create_post(source_type="x", file_type="photo", status="sent")
        await dup_repo.set_phash_atomic(pid1, "000000000000000f")  # 4 бита set

        orig = settings.phash_max_distance
        try:
            # max_distance=1 → не дубликат (distance=4 > 1)
            object.__setattr__(settings, "phash_max_distance", 1)
            dupes = await check_visual_duplicate("0000000000000000", exclude_post_id=999)
            assert len(dupes) == 0

            # max_distance=5 → дубликат (distance=4 ≤ 5)
            object.__setattr__(settings, "phash_max_distance", 5)
            dupes = await check_visual_duplicate("0000000000000000", exclude_post_id=999)
            assert len(dupes) >= 1
        finally:
            object.__setattr__(settings, "phash_max_distance", orig)

    run_with_db(tmp_path, body)


def test_check_visual_duplicate_excludes_self(tmp_path):
    """exclude_post_id исключает сам пост (не находит себя)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import duplicates as dup_repo
        from app.repositories import posts as posts_repo
        from app.services.dedup.phash import check_visual_duplicate

        pid1 = await posts_repo.create_post(source_type="x", file_type="photo", status="sent")
        await dup_repo.set_phash_atomic(pid1, "aabbccdd11223344")

        # Поиск с тем же pHash, исключая pid1 → не должен найти себя
        dupes = await check_visual_duplicate("aabbccdd11223344", exclude_post_id=pid1)
        assert len(dupes) == 0

        # Поиск БЕЗ exclude → найдёт сам себя (distance=0)
        dupes = await check_visual_duplicate("aabbccdd11223344")
        assert any(d["post_id"] == pid1 for d in dupes)

    run_with_db(tmp_path, body)


def test_check_visual_duplicate_disabled(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.config.settings import settings
        from app.repositories import duplicates as dup_repo
        from app.repositories import posts as posts_repo
        from app.services.dedup.phash import check_visual_duplicate

        orig = settings.phash_enabled
        object.__setattr__(settings, "phash_enabled", False)
        try:
            pid1 = await posts_repo.create_post(source_type="x", file_type="photo", status="sent")
            await dup_repo.set_phash_atomic(pid1, "ffff0000ffff0000")
            dupes = await check_visual_duplicate("ffff0000ffff0000")
            assert dupes == []
        finally:
            object.__setattr__(settings, "phash_enabled", orig)

    run_with_db(tmp_path, body)


# ===========================================================================
# Worker: pHash-интеграция в _handle_download
# ===========================================================================


def test_worker_download_phash_duplicate_detected(tmp_path, monkeypatch):
    """После скачивания pHash вычисляется; если найден дубликат → possible_duplicate."""
    from conftest import run_with_db

    async def body():
        from app.repositories import duplicates as dup_repo
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.services.dedup import phash as phash_mod
        from app.worker import process_one_task, reset_worker

        reset_worker()

        # Существующий пост с известным pHash (будет найден как дубликат)
        existing_pid = await posts_repo.create_post(source_type="x", file_type="photo", status="sent")
        await dup_repo.set_phash_atomic(existing_pid, "aabbccdd11223344")

        # Мокаем download_media — возвращаем путь к реальному PNG (нужно для set_file_info)
        img_path = tmp_path / "downloaded.png"
        img_path.write_bytes(_make_image_bytes())

        async def fake_download(url, post_id, **kwargs):
            return {"file_path": str(img_path), "file_type": "photo", "file_size": 999, "mime_type": "image/png"}

        import app.services.media.downloader as dl

        monkeypatch.setattr(dl, "download_media", fake_download)

        # Мокаем compute_and_store_phash целиком — возвращает pHash existing
        async def fake_compute_and_store(post_id, file_path, file_type=None):
            from app.repositories import duplicates as dup_repo_inner

            phash = "aabbccdd11223344"
            await dup_repo_inner.set_phash_atomic(post_id, phash)
            return phash

        monkeypatch.setattr(phash_mod, "compute_and_store_phash", fake_compute_and_store)

        pid = await posts_repo.create_post(
            source_type="gelbooru",
            file_type="photo",
            source_url="https://gelbooru.com/x.jpg",
            status="new",
        )
        await tq.enqueue(pid, "download")

        handled = await process_one_task()
        assert handled is True

        post = await posts_repo.get_post(pid)
        # Должен быть possible_duplicate (найден existing с тем же pHash, distance=0)
        assert post["status"] == "possible_duplicate"
        assert post["phash"] == "aabbccdd11223344"
        reset_worker()

    run_with_db(tmp_path, body)


def test_worker_download_phash_unique(tmp_path, monkeypatch):
    """После скачивания pHash уникален → pending."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.worker import process_one_task, reset_worker

        reset_worker()

        img_path = tmp_path / "downloaded.png"
        img_path.write_bytes(_make_image_bytes())

        async def fake_download(url, post_id, **kwargs):
            return {"file_path": str(img_path), "file_type": "photo", "file_size": 999, "mime_type": "image/png"}

        import app.services.media.downloader as dl

        monkeypatch.setattr(dl, "download_media", fake_download)

        pid = await posts_repo.create_post(
            source_type="gelbooru",
            file_type="photo",
            source_url="https://gelbooru.com/x.jpg",
            status="new",
        )
        await tq.enqueue(pid, "download")

        handled = await process_one_task()
        assert handled is True

        post = await posts_repo.get_post(pid)
        # Уникальный pHash → pending
        assert post["status"] == "pending"
        assert post["phash"] is not None  # pHash сохранён
        assert len(post["phash"]) == 16
        reset_worker()

    run_with_db(tmp_path, body)


def test_worker_download_phash_disabled(tmp_path, monkeypatch):
    """pHash disabled → после скачивания сразу pending, pHash не вычисляется."""
    from conftest import run_with_db

    async def body():
        from app.config.settings import settings
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.worker import process_one_task, reset_worker

        reset_worker()
        orig = settings.phash_enabled
        object.__setattr__(settings, "phash_enabled", False)
        try:
            img_path = tmp_path / "downloaded.png"
            img_path.write_bytes(_make_image_bytes())

            async def fake_download(url, post_id, **kwargs):
                return {
                    "file_path": str(img_path), "file_type": "photo",
                    "file_size": 999, "mime_type": "image/png",
                }

            import app.services.media.downloader as dl

            monkeypatch.setattr(dl, "download_media", fake_download)

            pid = await posts_repo.create_post(
                source_type="gelbooru", file_type="photo",
                source_url="https://x/a.jpg", status="new",
            )
            await tq.enqueue(pid, "download")
            handled = await process_one_task()
            assert handled is True

            post = await posts_repo.get_post(pid)
            assert post["status"] == "pending"
            assert post["phash"] is None  # pHash не вычислялся
        finally:
            object.__setattr__(settings, "phash_enabled", orig)
            reset_worker()

    run_with_db(tmp_path, body)


def test_worker_download_video_no_phash(tmp_path, monkeypatch):
    """Видео → pHash не вычисляется (не изображение), сразу pending."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq
        from app.worker import process_one_task, reset_worker

        reset_worker()

        # Файл .mp4 (не изображение)
        video_path = tmp_path / "clip.mp4"
        video_path.write_bytes(b"fake video content")

        async def fake_download(url, post_id, **kwargs):
            return {
                "file_path": str(video_path), "file_type": "video",
                "file_size": 999, "mime_type": "video/mp4",
            }

        import app.services.media.downloader as dl

        monkeypatch.setattr(dl, "download_media", fake_download)

        pid = await posts_repo.create_post(
            source_type="gelbooru", file_type="video",
            source_url="https://x/v.mp4", status="new",
        )
        await tq.enqueue(pid, "download")
        handled = await process_one_task()
        assert handled is True

        post = await posts_repo.get_post(pid)
        assert post["status"] == "pending"
        assert post["phash"] is None  # видео — pHash не применим
        reset_worker()

    run_with_db(tmp_path, body)


# ===========================================================================
# duplicates.set_phash_atomic + find_phash_near: интеграционный цикл
# ===========================================================================


def test_phash_atomic_and_find_near_integration(tmp_path):
    """set_phash_atomic сохраняет в posts+duplicate_hashes; find_phash_near находит."""
    from conftest import run_with_db

    async def body():
        from app.repositories import duplicates as dup_repo
        from app.repositories import posts as posts_repo

        # Два поста с похожими pHash (distance=1)
        pid1 = await posts_repo.create_post(source_type="x", file_type="photo", status="sent")
        pid2 = await posts_repo.create_post(source_type="x", file_type="photo", status="sent")
        await dup_repo.set_phash_atomic(pid1, "0000000000000000")
        await dup_repo.set_phash_atomic(pid2, "0000000000000001")  # 1 бит отличается

        # Поиск от pid1 → должен найти pid2 (distance=1 ≤ 5)
        dupes = await dup_repo.find_phash_near("0000000000000000", max_distance=5)
        assert any(d["post_id"] == pid2 and d["distance"] == 1 for d in dupes)

        # Поиск от pid2 → должен найти pid1
        dupes = await dup_repo.find_phash_near("0000000000000001", max_distance=5)
        assert any(d["post_id"] == pid1 and d["distance"] == 1 for d in dupes)

    run_with_db(tmp_path, body)


def test_set_phash_atomic_idempotent(tmp_path):
    """Повторный set_phash_atomic для того же post_id заменяет, не дублирует."""
    from conftest import run_with_db

    async def body():
        from app.db import get_db
        from app.repositories import duplicates as dup_repo
        from app.repositories import posts as posts_repo

        pid = await posts_repo.create_post(source_type="x", file_type="photo", status="sent")
        await dup_repo.set_phash_atomic(pid, "aaaaaaaaaaaaaaaa")
        await dup_repo.set_phash_atomic(pid, "bbbbbbbbbbbbbbbb")  # замена

        # В duplicate_hashes должна быть ОДНА запись phash для pid
        db = get_db()
        cur = await db.execute(
            "SELECT COUNT(*) AS c FROM duplicate_hashes WHERE post_id = ? AND hash_type = 'phash'",
            (pid,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row["c"] == 1

        # В posts.phash — новое значение
        post = await posts_repo.get_post(pid)
        assert post["phash"] == "bbbbbbbbbbbbbbbb"

    run_with_db(tmp_path, body)


if __name__ == "__main__":
    # Позволяет запустить тест напрямую для отладки
    pytest.main([__file__, "-v"])
