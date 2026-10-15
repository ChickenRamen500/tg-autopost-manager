"""Тесты репозиториев (CRUD) и RBAC (ТЗ §5, §2.6)."""
import pytest

from app.repositories.users import check_access


def test_check_access_rbac():
    # ТЗ §2.6: admin >= moderator, неактивный — нет доступа
    admin = {"tg_id": 1, "role": "admin", "is_active": 1}
    moderator = {"tg_id": 2, "role": "moderator", "is_active": 1}
    inactive = {"tg_id": 3, "role": "admin", "is_active": 0}
    none_user = None

    assert check_access(admin, "admin") is True
    assert check_access(admin, "moderator") is True       # admin неявно moderator
    assert check_access(moderator, "moderator") is True
    assert check_access(moderator, "admin") is False       # moderator не admin
    assert check_access(inactive, "moderator") is False    # неактивный
    assert check_access(none_user, "moderator") is False   # гость


def test_check_access_invalid_role():
    bad = {"tg_id": 1, "role": "superuser", "is_active": 1}
    assert check_access(bad, "moderator") is False  # неизвестная роль
    assert check_access({"tg_id": 1, "role": "admin", "is_active": 1}, "superuser") is False


def test_user_crud(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.repositories import users

        tg_id = 111222333
        uid = await users.add_user(tg_id, "alice", role="moderator")
        fetched = await users.get_user_by_tg_id(tg_id)
        assert fetched is not None
        assert fetched["id"] == uid
        assert fetched["role"] == "moderator"
        assert fetched["username"] == "alice"

        await users.update_user_role(tg_id, "admin")
        fetched = await users.get_user_by_tg_id(tg_id)
        assert fetched["role"] == "admin"

        all_users = await users.list_users()
        assert len(all_users) == 1

        ok = await users.remove_user(tg_id)
        assert ok is True
        assert await users.get_user_by_tg_id(tg_id) is None

    run_with_db(tmp_path, body)


def test_add_user_invalid_role(tmp_path):
    from conftest import run_with_db

    async def body():
        from app.repositories import users
        with pytest.raises(ValueError):
            await users.add_user(1, "x", role="superuser")

    run_with_db(tmp_path, body)


def test_post_lifecycle(tmp_path):
    """Жизненный цикл поста (ТЗ §2.3.4): create → pending → sent."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import posts as posts_repo
        from app.repositories import queues as queues_repo

        ch_id = await channels_repo.create_channel(-1000000000010, "Ch")
        qid = await queues_repo.create_queue(ch_id, "Q", interval_minutes=60)

        pid = await posts_repo.create_post(
            source_type="telegram_direct",
            file_type="photo",
            tg_file_id="AgADBAAD123",
            file_unique_id="AQAD123",
            queue_id=qid,
            original_caption="test caption",
            tags=["1girl", "smile"],
        )
        assert pid > 0

        post = await posts_repo.get_next_pending(qid)
        assert post is not None
        assert post["id"] == pid
        assert post["status"] == "pending"
        assert post["tags"] == ["1girl", "smile"]

        await posts_repo.mark_sent(pid)
        assert await posts_repo.count_by_status("sent") == 1
        assert await posts_repo.get_next_pending(qid) is None

    run_with_db(tmp_path, body)


def test_post_file_id_strategy_fields(tmp_path):
    """Поля для стратегии file_id (ТЗ §2.1.1, §2.5.1): tg_file_id, was_processed."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import posts as posts_repo
        from app.repositories import queues as queues_repo

        ch_id = await channels_repo.create_channel(-1000000000011, "Ch2")
        qid = await queues_repo.create_queue(ch_id, "Q2")

        pid = await posts_repo.create_post(
            source_type="telegram_direct",
            file_type="video",
            tg_file_id="vid_file_id_123",
            file_unique_id="vid_unique_123",
            file_size=10 * 1024 * 1024,
            queue_id=qid,
        )
        post = await posts_repo.get_post(pid)
        assert post["tg_file_id"] == "vid_file_id_123"
        assert post["was_processed"] == 0  # False → отправка пойдёт по file_id
        assert post["file_size"] == 10 * 1024 * 1024

    run_with_db(tmp_path, body)


def test_duplicate_detection(tmp_path):
    """Точный дубликат по file_unique_id (ТЗ §2.4.3)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo

        await posts_repo.create_post(
            source_type="telegram_direct", file_type="photo", tg_file_id="f1", file_unique_id="UNIQ_DUP"
        )
        found = await posts_repo.find_by_file_unique_id("UNIQ_DUP")
        assert len(found) == 1
        assert found[0]["tg_file_id"] == "f1"

        assert await posts_repo.find_by_file_unique_id("NOPE") == []

    run_with_db(tmp_path, body)


def test_task_queue_take_next(tmp_path):
    """Персистентная очередь задач (ТЗ §5.1 task_queue, §4.2.2)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo
        from app.repositories import task_queue as tq

        pid = await posts_repo.create_post(source_type="chrome_ext", file_type="photo")
        t1 = await tq.enqueue(pid, "download", {"url": "https://example.com/x.jpg"})
        t2 = await tq.enqueue(pid, "process")

        job = await tq.take_next()
        assert job is not None
        assert job["id"] == t1
        assert job["status"] == "in_progress"
        assert job["payload"] == {"url": "https://example.com/x.jpg"}

        await tq.mark_done(t1)
        job2 = await tq.take_next()
        assert job2["id"] == t2
        await tq.mark_done(t2)
        assert await tq.take_next() is None

    run_with_db(tmp_path, body)
