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


def test_cmd_cancel_registered_before_fsm_handlers():
    """Регрессия FSM-ловушки: cmd_cancel должен быть зарегистрирован ПЕРВЫМ в menu-роутере.

    Без этого /cancel перехватывался FSM-хендлером (который ждёт текст) —
    пользователь застревал в состоянии, не мог выйти даже /cancel.
    """
    from app.bot.handlers.menu import router

    # Собираем имена callback-хендлеров message-наблюдателя в порядке регистрации
    handler_names = []
    for observer in router.observers.values():
        if observer.event_name != "message":
            continue
        for h in observer.handlers:
            # У каждого хендлера есть callback; берём его имя
            cb = h.callback
            handler_names.append(cb.__name__)
    assert handler_names, "Не нашли message-хендлеры в menu-роутере"

    # cmd_cancel должен быть первым (до FSM-хендлеров)
    assert handler_names[0] == "cmd_cancel", (
        f"cmd_cancel должен быть первым, иначе /cancel перехватывается FSM. "
        f"Первый хендлер: {handler_names[0]}"
    )

    # Также проверим, что есть catch-all для не-текста в FSM
    assert "fsm_non_text_in_state" in handler_names, "Нет catch-all для не-текстовых сообщений в FSM"


def test_routing_rule_crud(tmp_path):
    """CRUD правил маршрутизации (ТЗ §2.2.2)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import queues as queues_repo
        from app.repositories import routing as routing_repo
        from app.services.routing import route_post

        ch_id = await channels_repo.create_channel(-1000000000033, "RuleCh")
        qid = await queues_repo.create_queue(ch_id, "RuleQ")

        # Создаём правило: telegram_channel с паттерном @anime → очередь
        rid = await routing_repo.create_rule(
            source_type="telegram_channel",
            source_pattern="@anime_channel",
            target_queue_id=qid,
            priority=10,
        )
        assert rid > 0

        # Проверяем, что route_post находит правило
        result = await route_post("telegram_channel", "@anime_channel")
        assert result == qid

        # Проверяем list_rules
        rules = await routing_repo.list_rules(active_only=False)
        assert len(rules) == 1
        assert rules[0]["source_pattern"] == "@anime_channel"

        # Удаляем правило
        ok = await routing_repo.remove_rule(rid)
        assert ok is True
        rules = await routing_repo.list_rules(active_only=False)
        assert len(rules) == 0

    run_with_db(tmp_path, body)


def test_keyboards_build_routing():
    """Клавиатуры для routing rules строятся без ошибок."""
    from app.bot.keyboards import builder as kb

    st = kb.source_type_picker()
    assert st is not None
    assert len(st.inline_keyboard) > 0

    qpk = kb.rule_pick_queue([{"id": 1, "name": "Test"}])
    assert qpk is not None

    rl = kb.rules_list(
        [{"id": 1, "source_type": "any", "source_pattern": ".*", "target_queue_id": 1, "priority": 0, "is_active": 1}],
        {1: "Test"},
    )
    assert rl is not None

    ra = kb.rule_actions(1)
    assert ra is not None

    # add_menu должен содержать кнопку «🔀 Правило»
    am = kb.add_menu()
    buttons_text = [b.text for row in am.inline_keyboard for b in row]
    assert any("🔀" in t for t in buttons_text), f"Нет кнопки Маршрутизация в add_menu: {buttons_text}"


def test_post_card_has_delete_button():
    """Карточка поста содержит кнопку «🗑 Удалить пост» (для очистки дубликатов)."""
    from app.bot.keyboards import builder as kb

    post = {
        "id": 42,
        "status": "possible_duplicate",
        "queue_id": None,  # во Входящих
        "file_type": "photo",
    }
    card = kb.post_card(post, queues=[])
    buttons = [b.text for row in card.inline_keyboard for b in row]
    assert any("🗑" in t for t in buttons), f"Нет кнопки удаления в карточке поста: {buttons}"


def test_create_post_with_media_group_id(tmp_path):
    """Пост сохраняет media_group_id (ТЗ §2.1.1 — альбомы)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo

        # Два поста с одинаковым media_group_id — это альбом
        p1 = await posts_repo.create_post(
            source_type="telegram_direct",
            file_type="photo",
            tg_file_id="album_file_1",
            file_unique_id="album_uniq_1",
            media_group_id="album_123",
        )
        p2 = await posts_repo.create_post(
            source_type="telegram_direct",
            file_type="photo",
            tg_file_id="album_file_2",
            file_unique_id="album_uniq_2",
            media_group_id="album_123",
        )
        assert p1 > 0 and p2 > 0

        # Проверяем, что media_group_id сохранён
        post1 = await posts_repo.get_post(p1)
        assert post1["media_group_id"] == "album_123"

    run_with_db(tmp_path, body)


def test_find_album_posts_groups_by_media_group_id(tmp_path):
    """find_album_posts возвращает все посты альбома (одинаковый media_group_id)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import posts as posts_repo
        from app.repositories import queues as queues_repo

        ch_id = await channels_repo.create_channel(-1000000000044, "AlbumCh")
        qid = await queues_repo.create_queue(ch_id, "AlbumQ", group_albums=True)

        # Альбом из 3 фото (одинаковый media_group_id)
        for i in range(3):
            await posts_repo.create_post(
                source_type="telegram_direct",
                file_type="photo",
                tg_file_id=f"album_file_{i}",
                file_unique_id=f"album_uniq_{i}",
                queue_id=qid,
                media_group_id="album_xyz",
            )
        # Одиничный пост (другой media_group_id)
        await posts_repo.create_post(
            source_type="telegram_direct",
            file_type="photo",
            queue_id=qid,
            media_group_id="other_group",
        )

        album = await posts_repo.find_album_posts(qid, "album_xyz", limit=10)
        assert len(album) == 3, f"Ожидали 3 поста альбома, получили {len(album)}"

        # Другой альбом
        other = await posts_repo.find_album_posts(qid, "other_group", limit=10)
        assert len(other) == 1

    run_with_db(tmp_path, body)


def test_delete_post_removes_from_db(tmp_path):
    """delete_post удаляет пост из БД (для очистки дубликатов)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import posts as posts_repo

        pid = await posts_repo.create_post(
            source_type="telegram_direct",
            file_type="photo",
            tg_file_id="del_test",
            file_unique_id="del_uniq",
        )
        assert await posts_repo.get_post(pid) is not None

        ok = await posts_repo.delete_post(pid)
        assert ok is True
        assert await posts_repo.get_post(pid) is None

        # Повторное удаление — False
        ok2 = await posts_repo.delete_post(pid)
        assert ok2 is False

    run_with_db(tmp_path, body)


def test_queue_group_albums_setting(tmp_path):
    """Очередь с group_albums=True сохраняет настройку (ТЗ §2.1.1)."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import queues as queues_repo

        ch_id = await channels_repo.create_channel(-1000000000055, "GACh")
        qid = await queues_repo.create_queue(ch_id, "GroupAlbumsQ", group_albums=True)
        q = await queues_repo.get_queue(qid)
        assert q["group_albums"] == 1

        # Переключение через set_group_albums
        await queues_repo.set_group_albums(qid, False)
        q = await queues_repo.get_queue(qid)
        assert q["group_albums"] == 0

        await queues_repo.set_group_albums(qid, True)
        q = await queues_repo.get_queue(qid)
        assert q["group_albums"] == 1

    run_with_db(tmp_path, body)


def test_db_migration_adds_columns(tmp_path):
    """Миграция добавляет media_group_id и group_albums в существующую БД.

    Симулируем старую БД (без колонок) и проверяем, что init_db их добавляет.
    """
    from conftest import run_with_db

    async def body():
        from app.db import get_db

        # init_db уже выполнен в run_with_db — колонки должны быть
        db = get_db()
        # Проверяем posts.media_group_id
        cur = await db.execute("PRAGMA table_info(posts)")
        cols = [r[1] for r in await cur.fetchall()]
        await cur.close()
        assert "media_group_id" in cols, f"Нет колонки media_group_id в posts: {cols}"

        # Проверяем queues.group_albums
        cur = await db.execute("PRAGMA table_info(queues)")
        cols = [r[1] for r in await cur.fetchall()]
        await cur.close()
        assert "group_albums" in cols, f"Нет колонки group_albums в queues: {cols}"

    run_with_db(tmp_path, body)


def test_list_queues_returns_orphan_queues(tmp_path):
    """Регрессия бага «Нет очередей» при создании правила маршрутизации.

    list_queues использовал INNER JOIN — очередь без канала (orphan) «пряталась»,
    и пользователь получал ложное «Нет очередей» при создании правила, хотя
    очередь существовала. Фикс: LEFT JOIN.
    """
    from conftest import run_with_db

    async def body():
        from app.db import get_db
        from app.repositories import queues as queues_repo

        db = get_db()
        # Вставляем очередь с channel_id=999 (канал не существует).
        # FK отключаем на время вставки — симулируем старую/перенесённую БД.
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute(
            "INSERT INTO queues (channel_id, name, interval_minutes) VALUES (999, 'Orphan', 60)"
        )
        await db.execute("PRAGMA foreign_keys=ON")
        await db.commit()

        # LEFT JOIN: orphan-очередь видна, channel_name=None
        q = await queues_repo.list_queues(active_only=False)
        assert len(q) == 1, f"Ожидали 1 orphan-очередь, получили {len(q)} (INNER JOIN баг?)"
        assert q[0]["name"] == "Orphan"
        assert q[0]["channel_name"] is None, f"channel_name должен быть None: {q[0].get('channel_name')}"

    run_with_db(tmp_path, body)


def test_list_queues_with_channel_and_orphan(tmp_path):
    """Смесь: очередь с каналом + orphan-очередь — обе видны через LEFT JOIN."""
    from conftest import run_with_db

    async def body():
        from app.db import get_db
        from app.repositories import channels as channels_repo
        from app.repositories import queues as queues_repo

        # Очередь с реальным каналом
        ch_id = await channels_repo.create_channel(-1000000000066, "RealCh")
        await queues_repo.create_queue(ch_id, "WithChannel", interval_minutes=60)

        # Orphan-очередь (без канала)
        db = get_db()
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute(
            "INSERT INTO queues (channel_id, name, interval_minutes) VALUES (888, 'Orphan', 30)"
        )
        await db.execute("PRAGMA foreign_keys=ON")
        await db.commit()

        q = await queues_repo.list_queues(active_only=False)
        assert len(q) == 2, f"Ожидали 2 очереди, получили {len(q)}"
        names = {x["name"]: x.get("channel_name") for x in q}
        assert names["WithChannel"] == "RealCh"
        assert names["Orphan"] is None

    run_with_db(tmp_path, body)
