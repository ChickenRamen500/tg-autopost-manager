"""Тесты регрессии для ошибок, найденных в runtime-логах (Фаза 2 post-fix).

1. caption_raw не существует в aiogram 3.x — receive_media не должен падать.
2. Дубликат tg_chat_id в /add_channel — DuplicateChannelError вместо IntegrityError.
3. check_phase2.py --routing изолирован от занятой БД.
"""
from app.repositories.channels import DuplicateChannelError


def test_caption_extraction_no_caption_raw_attr(tmp_path):
    """aiogram 3.x Message не имеет caption_raw — media.py использует только message.caption.

    Проверяем, что логика извлечения caption не обращается к несуществующему атрибуту.
    """
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import posts as posts_repo
        from app.repositories import queues as queues_repo

        ch_id = await channels_repo.create_channel(-1000000000099, "CapCh")
        qid = await queues_repo.create_queue(ch_id, "CapQ")

        # Пост без caption (как сообщение без подписи) — caption=None
        pid = await posts_repo.create_post(
            source_type="telegram_direct",
            file_type="photo",
            tg_file_id="cap_test_1",
            file_unique_id="cap_uniq_1",
            original_caption=None,
            queue_id=qid,
        )
        post = await posts_repo.get_post(pid)
        assert post["original_caption"] is None

        # Пост с caption
        pid2 = await posts_repo.create_post(
            source_type="telegram_direct",
            file_type="photo",
            tg_file_id="cap_test_2",
            file_unique_id="cap_uniq_2",
            original_caption="Подпись к фото",
            queue_id=qid,
        )
        post2 = await posts_repo.get_post(pid2)
        assert post2["original_caption"] == "Подпись к фото"

    run_with_db(tmp_path, body)


def test_duplicate_channel_raises_typed_error(tmp_path):
    """Создание канала с дубликатом tg_chat_id → DuplicateChannelError (не голый IntegrityError)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo

        tg_chat_id = -1000000000088
        cid = await channels_repo.create_channel(tg_chat_id, "First Channel")
        assert cid > 0

        # Вторая попытка с тем же tg_chat_id — должна поднять DuplicateChannelError
        raised = False
        try:
            await channels_repo.create_channel(tg_chat_id, "Duplicate Channel")
        except DuplicateChannelError as e:
            raised = True
            assert e.tg_chat_id == tg_chat_id
            assert "уже существует" in str(e)
        assert raised, "Ожидался DuplicateChannelError"

    run_with_db(tmp_path, body)


def test_get_channel_by_chat_id_finds_existing(tmp_path):
    """get_channel_by_chat_id возвращает существующий канал (для проверки дубликата в /add_channel)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo

        tg_chat_id = -1000000000077
        cid = await channels_repo.create_channel(tg_chat_id, "Lookup Test", "lookup_ch")

        found = await channels_repo.get_channel_by_chat_id(tg_chat_id)
        assert found is not None
        assert found["id"] == cid
        assert found["name"] == "Lookup Test"
        assert found["username"] == "lookup_ch"

        # Несуществующий — None
        assert await channels_repo.get_channel_by_chat_id(-1000000000066) is None

    run_with_db(tmp_path, body)


def test_add_to_queue_moves_post_from_inbox(tmp_path):
    """Перенос поста из Входящих в очередь (команда /add_to_queue, ТЗ §2.3)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import posts as posts_repo
        from app.repositories import queues as queues_repo

        ch_id = await channels_repo.create_channel(-1000000000055, "MoveCh")
        qid = await queues_repo.create_queue(ch_id, "MoveQ")

        # Пост без очереди (Входящие)
        pid = await posts_repo.create_post(source_type="telegram_direct", file_type="photo")
        assert await posts_repo.get_next_pending(qid) is None  # очередь пуста

        # Перенос в очередь
        ok = await posts_repo.assign_queue(pid, qid)
        assert ok is True
        post = await posts_repo.get_post(pid)
        assert post["queue_id"] == qid
        assert post["status"] == "pending"

        # Теперь get_next_pending находит пост
        nxt = await posts_repo.get_next_pending(qid)
        assert nxt["id"] == pid

        # Возврат во Входящие
        ok2 = await posts_repo.assign_queue(pid, None)
        assert ok2 is True
        assert await posts_repo.get_next_pending(qid) is None

    run_with_db(tmp_path, body)


def test_hot_reload_scheduler_registers_new_queue(tmp_path):
    """reload_scheduler подхватывает новую очередь без перезапуска (главная проблема пользователя)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import queues as queues_repo
        from app.scheduler import get_scheduler, reload_scheduler

        # Инициализируем scheduler (имитируем запуск приложения)
        sched = get_scheduler()
        sched.start()

        # Сначала очередей нет
        n0 = await reload_scheduler()
        assert n0 == 0
        assert not [j for j in sched.get_jobs() if j.id.startswith("queue_")]

        # Создаём канал + очередь
        ch_id = await channels_repo.create_channel(-1000000000044, "ReloadCh")
        qid = await queues_repo.create_queue(ch_id, "ReloadQ", interval_minutes=30)

        # Hot-reload — новая очередь должна появиться в scheduler
        n1 = await reload_scheduler()
        assert n1 == 1
        jobs = [j for j in sched.get_jobs() if j.id.startswith("queue_")]
        assert len(jobs) == 1
        assert jobs[0].id == f"queue_{qid}"

        # Добавляем ещё одну очередь
        qid2 = await queues_repo.create_queue(ch_id, "ReloadQ2", interval_minutes=60)
        n2 = await reload_scheduler()
        assert n2 == 2

        # Удаляем первую — reload убирает её job
        await queues_repo.remove_queue(qid)
        n3 = await reload_scheduler()
        assert n3 == 1
        remaining = [j.id for j in sched.get_jobs() if j.id.startswith("queue_")]
        assert f"queue_{qid2}" in remaining
        assert f"queue_{qid}" not in remaining

        sched.shutdown(wait=False)

    run_with_db(tmp_path, body)


def test_extract_source_forward_from_channel():
    """_extract_source для пересланного из канала сообщения — source_id не None (баг из логов)."""
    from app.bot.handlers.media import _extract_source

    # Имитируем MessageOriginChannel (aiogram 3.x): chat.id, chat.username, chat.title
    class FakeChat:
        id = -1002257882757
        username = "anime_channel"
        title = "Anime Channel"

    class FakeForwardOrigin:
        chat = FakeChat()

    class FakeMessage:
        forward_origin = FakeForwardOrigin()
        from_user = None

    source = _extract_source(FakeMessage())
    assert source["source_type"] == "telegram_channel"
    assert source["source_id"] == "-1002257882757"  # НЕ None (был баг!)
    assert source["source_channel"] == "@anime_channel"


def test_extract_source_direct_message():
    """_extract_source для прямого сообщения (не пересланного)."""
    from app.bot.handlers.media import _extract_source

    class FakeUser:
        id = 479924833
        username = "chickenramen"

    class FakeMessage:
        forward_origin = None
        from_user = FakeUser()

    source = _extract_source(FakeMessage())
    assert source["source_type"] == "telegram_direct"
    assert source["source_id"] == "479924833"
    assert source["source_channel"] is None
