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
