"""Тесты веб-страниц «Посты»/«Архив» (пагинация) и эндпоинта preview постов.

Регрессии из баг-репортов пользователя:
  - миниатюры не работали для скачанных файлов (data/downloads вне разрешённых
    корней local_files/serve → 403) — теперь выделенный /web/posts/{id}/preview;
  - «Посты»/«Архив» показывали максимум 200 постов — теперь пагинация по 50.
"""

from conftest import run_with_db


def test_repo_pagination_list_all_count_all(tmp_path):
    """list_all/count_all: пагинация по всем постам (ORDER BY id DESC)."""
    async def body():
        from app.repositories import posts as posts_repo

        for i in range(7):
            await posts_repo.create_post(
                source_type="xcom",
                file_type="photo",
                source_url=f"https://example.com/{i}.jpg",
                status="pending" if i % 2 else "new",
            )

        total = await posts_repo.count_all()
        assert total == 7
        assert await posts_repo.count_all("pending") == 3
        assert await posts_repo.count_all("new") == 4

        page1 = await posts_repo.list_all(limit=5, offset=0)
        assert len(page1) == 5
        page2 = await posts_repo.list_all(limit=5, offset=5)
        assert len(page2) == 2
        # Свежие сверху: id идут по убыванию
        ids = [p["id"] for p in page1]
        assert ids == sorted(ids, reverse=True)
        # Пересечение страниц пустое
        assert not {p["id"] for p in page1} & {p["id"] for p in page2}
        # Фильтр по статусу
        pending = await posts_repo.list_all(status="pending")
        assert len(pending) == 3
        assert all(p["status"] == "pending" for p in pending)

    run_with_db(tmp_path, body)


def test_repo_pagination_list_sent_offset_count(tmp_path):
    """list_sent(offset)/count_sent: пагинация архива."""
    async def body():
        from app.repositories import posts as posts_repo

        ids = []
        for i in range(6):
            pid = await posts_repo.create_post(
                source_type="gelbooru", file_type="photo",
                source_url=f"https://img3.gelbooru.com/images/{i}.jpg",
            )
            ids.append(pid)
            await posts_repo.update_status(pid, "sent")

        assert await posts_repo.count_sent() == 6
        page1 = await posts_repo.list_sent(limit=4, offset=0)
        page2 = await posts_repo.list_sent(limit=4, offset=4)
        assert len(page1) == 4 and len(page2) == 2
        assert not {p["id"] for p in page1} & {p["id"] for p in page2}

    run_with_db(tmp_path, body)


def test_post_preview_endpoint_serves_local_file(tmp_path):
    """GET /web/posts/{id}/preview: файл на диске (в т.ч. data/downloads) → 200."""
    async def body():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.repositories import posts as posts_repo
        from app.web.router import router

        media_file = tmp_path / "downloads" / "post_999.jpg"
        media_file.parent.mkdir(parents=True, exist_ok=True)
        media_file.write_bytes(b"JPEGDATA")

        pid = await posts_repo.create_post(
            source_type="gelbooru", file_type="photo",
            source_url="https://img3.gelbooru.com/images/a.jpg",
            file_path=str(media_file),
            status="pending",
        )

        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            resp = client.get(f"/web/posts/{pid}/preview")
            assert resp.status_code == 200
            assert resp.content == b"JPEGDATA"

            # Несуществующий пост → 404
            assert client.get("/web/posts/999999/preview").status_code == 404

    run_with_db(tmp_path, body)


def test_post_preview_endpoint_redirects_to_direct_source(tmp_path):
    """Файл удалён (после отправки), но source_url — прямая ссылка → redirect."""
    async def body():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.repositories import posts as posts_repo
        from app.web.router import router

        pid = await posts_repo.create_post(
            source_type="gelbooru", file_type="photo",
            source_url="https://img3.gelbooru.com/images/a/b/abcd.jpg",
            file_path="/app/data/downloads/deleted_after_send.jpg",  # файл не существует
            status="sent",
        )

        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            resp = client.get(f"/web/posts/{pid}/preview", follow_redirects=False)
            assert resp.status_code == 302
            assert resp.headers["location"] == "https://img3.gelbooru.com/images/a/b/abcd.jpg"

    run_with_db(tmp_path, body)


def test_post_preview_endpoint_404_without_media(tmp_path):
    """Нет файла и source_url — HTML страница (твит) → 404 (никаких сломанных ⚠)."""
    async def body():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.repositories import posts as posts_repo
        from app.web.router import router

        pid = await posts_repo.create_post(
            source_type="xcom", file_type="video",
            source_url="https://x.com/user/status/123",
            file_path=None,
            status="new",
        )

        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            resp = client.get(f"/web/posts/{pid}/preview")
            assert resp.status_code == 404

    run_with_db(tmp_path, body)


def test_posts_page_pagination(tmp_path):
    """GET /posts?page=N: по 50 на страницу, страницы > 1 доступны (больше 200 постов)."""
    async def body():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.repositories import posts as posts_repo
        from app.web.router import router

        for i in range(55):
            await posts_repo.create_post(
                source_type="xcom", file_type="photo",
                source_url=f"https://pbs.twimg.com/media/{i}.jpg?name=orig",
            )

        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            r1 = client.get("/posts")
            assert r1.status_code == 200
            assert "стр. 1 из 2" in r1.text
            assert r1.text.count("/web/posts/") > 0
            # На странице 50 строк постов (проверяем по чекбоксам delete-форм)
            assert 'data-confirm="Удалить пост #' in r1.text

            r2 = client.get("/posts?page=2")
            assert r2.status_code == 200
            assert "стр. 2 из 2" in r2.text

            # За границей — зажим на последнюю страницу
            r99 = client.get("/posts?page=99")
            assert r99.status_code == 200
            assert "стр. 2 из 2" in r99.text

            # Фильтр статуса сохраняется в пагинации
            rs = client.get("/posts?status=pending")
            assert rs.status_code == 200
            assert "status=pending&amp;page=2" in rs.text or "status=pending&page=2" in rs.text

    run_with_db(tmp_path, body)


def test_sent_page_pagination(tmp_path):
    """GET /sent?page=N: пагинация архива + data-preview на строках."""
    async def body():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.repositories import posts as posts_repo
        from app.web.router import router

        for i in range(60):
            pid = await posts_repo.create_post(
                source_type="gelbooru", file_type="photo",
                source_url=f"https://img3.gelbooru.com/images/{i}/x.jpg",
            )
            await posts_repo.update_status(pid, "sent")

        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            r1 = client.get("/sent")
            assert r1.status_code == 200
            assert "стр. 1 из 2" in r1.text
            assert "60 шт." in r1.text
            # preview-эндпоинт в data-preview (файла нет — source_url прямая ссылка)
            assert "/web/posts/" in r1.text and "/preview" in r1.text
            assert "tgq-preview-zone" in r1.text

            r2 = client.get("/sent?page=2")
            assert r2.status_code == 200
            assert "стр. 2 из 2" in r2.text

    run_with_db(tmp_path, body)


def test_dashboard_includes_previews(tmp_path):
    """Дашборд: последние входящие содержат data-preview и preview-зоны."""
    async def body():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.repositories import posts as posts_repo
        from app.web.router import router

        await posts_repo.create_post(
            source_type="gelbooru", file_type="photo",
            source_url="https://img3.gelbooru.com/images/c/d/cdef.gif",
        )

        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            resp = client.get("/")
            assert resp.status_code == 200
            assert "data-preview=" in resp.text
            assert "tgq-preview-zone" in resp.text
            assert "/web/posts/" in resp.text and "/preview" in resp.text

    run_with_db(tmp_path, body)
