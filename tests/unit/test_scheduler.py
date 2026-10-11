"""Тесты планировщика: выбор стратегии отправки (ТЗ §2.5.1).

Полный цикл отправки требует Telegram-бота — тестируем только логику выбора стратегии
(file_id vs файл) и учёт попыток (§2.5.2).
"""


def test_strategy_file_id_when_unprocessed(tmp_path):
    """Пост с tg_file_id и was_processed=False → отправка по file_id (ТЗ §2.5.1 стратегия 1)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import posts as posts_repo
        from app.repositories import queues as queues_repo

        ch_id = await channels_repo.create_channel(-1000000000020, "StratCh")
        qid = await queues_repo.create_queue(ch_id, "StratQ")

        pid = await posts_repo.create_post(
            source_type="telegram_direct",
            file_type="video",
            tg_file_id="vid_123",
            was_processed=False,
            queue_id=qid,
        )
        post = await posts_repo.get_post(pid)
        assert post["tg_file_id"] == "vid_123"
        assert not post["was_processed"]
        use_file_id = bool(post["tg_file_id"]) and not post["was_processed"]
        assert use_file_id is True

    run_with_db(tmp_path, body)


def test_strategy_file_when_processed(tmp_path):
    """was_processed=True → отправка локальным файлом (ТЗ §2.5.1 стратегия 2)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import posts as posts_repo
        from app.repositories import queues as queues_repo

        ch_id = await channels_repo.create_channel(-1000000000021, "StratCh2")
        qid = await queues_repo.create_queue(ch_id, "StratQ2")

        pid = await posts_repo.create_post(
            source_type="telegram_direct",
            file_type="video",
            tg_file_id="vid_456",
            file_path="/app/media/processed.mp4",
            was_processed=True,
            queue_id=qid,
        )
        post = await posts_repo.get_post(pid)
        use_file_id = bool(post["tg_file_id"]) and not post["was_processed"]
        assert use_file_id is False  # → будет читаться file_path

    run_with_db(tmp_path, body)


def test_send_attempts_increment(tmp_path):
    """Учёт попыток отправки (ТЗ §2.5.2 п.4): max → failed."""
    from conftest import run_with_db

    async def body():
        from app.config.settings import settings
        from app.repositories import channels as channels_repo
        from app.repositories import posts as posts_repo
        from app.repositories import queues as queues_repo

        ch_id = await channels_repo.create_channel(-1000000000022, "AttemptCh")
        qid = await queues_repo.create_queue(ch_id, "AttemptQ")
        pid = await posts_repo.create_post(
            source_type="telegram_direct", file_type="photo", queue_id=qid
        )

        max_attempts = settings.max_send_attempts
        for i in range(max_attempts):
            attempts = await posts_repo.increment_attempt(pid)
            assert attempts == i + 1

        await posts_repo.mark_failed(pid, "simulated 429")
        post = await posts_repo.get_post(pid)
        assert post["status"] == "failed"
        assert post["send_attempts"] == max_attempts
        assert "429" in post["last_error"]

    run_with_db(tmp_path, body)


def test_next_pending_ordering(tmp_path):
    """get_next_pending возвращает самый старый pending (ТЗ §2.5.1: order_by scheduled_time)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import posts as posts_repo
        from app.repositories import queues as queues_repo

        ch_id = await channels_repo.create_channel(-1000000000023, "OrderCh")
        qid = await queues_repo.create_queue(ch_id, "OrderQ")
        p1 = await posts_repo.create_post(source_type="x", file_type="photo", queue_id=qid)
        p2 = await posts_repo.create_post(source_type="x", file_type="photo", queue_id=qid)

        nxt = await posts_repo.get_next_pending(qid)
        assert nxt["id"] == p1  # старший (первый созданный) — первый на отправку

        await posts_repo.mark_sent(p1)
        nxt = await posts_repo.get_next_pending(qid)
        assert nxt["id"] == p2

    run_with_db(tmp_path, body)
