-- ==========================================================================
-- Схема базы данных TG Media Queue Bot (ТЗ §5.1)
-- БД: SQLite (aiosqlite), WAL-режим, foreign_keys=ON
-- Идемпотентна: безопасно выполняется через executescript() при каждом старте.
-- ==========================================================================

-- Пользователи системы (ТЗ §5.1, §2.6 RBAC)
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER UNIQUE NOT NULL,
    username    TEXT,
    role        TEXT NOT NULL CHECK(role IN ('admin', 'moderator')),
    is_active   INTEGER NOT NULL DEFAULT 1,   -- BOOLEAN: 0/1
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Целевые каналы для постинга
CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_chat_id  INTEGER UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    username    TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Очереди постов
CREATE TABLE IF NOT EXISTS queues (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id         INTEGER NOT NULL,
    name               TEXT NOT NULL,
    interval_minutes   INTEGER NOT NULL DEFAULT 60,
    is_active          INTEGER NOT NULL DEFAULT 1,
    crop_video         INTEGER NOT NULL DEFAULT 0,
    signature_template TEXT,
    shuffle_enabled    INTEGER NOT NULL DEFAULT 1,
    -- Группировать альбомы при отправке (ТЗ §2.1.1: «каждый файл как отдельный пост или групповой пост»):
    -- TRUE → посты с одинаковым media_group_id отправляются одним сообщением через send_media_group
    group_albums       INTEGER NOT NULL DEFAULT 0,
    -- Время работы очереди (блок B — полировка Фазы 2):
    -- start_at/end_at: абсолютные даты начала/конца (NULL = сразу/бесконечно)
    -- daily_window_start/end: ежедневное окно в формате "HH:MM" (NULL = 00:00..23:59)
    -- end_strategy: что делать с неотправленными постами при end_at:
    --   'continue_next_day' (по умолчанию) — продолжить в следующем daily_window
    --   'continue_after_end' — игнорировать end_at, шлём по интервалу
    --   'move_to_inbox' — все pending → Входящие (queue_id=NULL)
    --   'delete' — удалить pending посты
    start_at           DATETIME,
    end_at             DATETIME,
    daily_window_start TEXT,   -- "HH:MM" (локальное время, TZ Asia/Yekaterinburg)
    daily_window_end   TEXT,   -- "HH:MM"
    end_strategy       TEXT NOT NULL DEFAULT 'continue_next_day'
                              CHECK(end_strategy IN ('continue_next_day', 'continue_after_end', 'move_to_inbox', 'delete')),
    created_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (channel_id) REFERENCES channels(id)
);

-- Посты в очередях (ТЗ §5.1 — обновлённая схема с поддержкой file_id и жизненного цикла)
CREATE TABLE IF NOT EXISTS posts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id           INTEGER,                       -- NULL = «Входящие» (inbox)
    source_type        TEXT NOT NULL,
    source_id         TEXT,
    source_url         TEXT,
    source_channel     TEXT,
    file_path          TEXT,                           -- NULL, если пост только по file_id
    file_type          TEXT NOT NULL CHECK(file_type IN ('photo', 'video', 'gif', 'document')),
    tg_file_id         TEXT,                           -- file_id Telegram (для телеграм-источников)
    file_unique_id     TEXT,                           -- стабильный id файла Telegram (дедуп)
    file_size          INTEGER,                        -- размер в байтах (логирование, лимиты)
    mime_type           TEXT,
    was_processed      INTEGER NOT NULL DEFAULT 0,     -- проходил ли обработку (кроп и т.п.)
    original_caption   TEXT,
    processed_caption  TEXT,
    tags               TEXT,                           -- JSON массив
    status             TEXT NOT NULL CHECK(status IN ('new', 'downloading', 'pending', 'sent',
                                                       'failed', 'duplicate', 'possible_duplicate')),
    phash              TEXT,                           -- perceptual hash для поиска дубликатов
    -- ID группы альбома (ТЗ §2.1.1): посты с одинаковым media_group_id — один альбом
    media_group_id     TEXT,
    scheduled_time     DATETIME,
    sent_at            DATETIME,
    -- Расширение Фазы 2 для реализации §2.5.2 (учёт попыток отправки):
    send_attempts      INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT,
    created_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (queue_id) REFERENCES queues(id)
);

-- Правила маршрутизации
CREATE TABLE IF NOT EXISTS routing_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type     TEXT NOT NULL,
    source_pattern  TEXT NOT NULL,
    target_queue_id INTEGER NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 0,
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (target_queue_id) REFERENCES queues(id)
);

-- Подписи для постов
CREATE TABLE IF NOT EXISTS signatures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Хэши для поиска дубликатов
CREATE TABLE IF NOT EXISTS duplicate_hashes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL,
    hash_value  TEXT NOT NULL,
    hash_type   TEXT NOT NULL CHECK(hash_type IN ('md5', 'phash')),
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (post_id) REFERENCES posts(id)
);

-- Подписки на теги (Gelbooru/Danbooru)
CREATE TABLE IF NOT EXISTS tag_subscriptions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER NOT NULL,
    site                 TEXT NOT NULL CHECK(site IN ('gelbooru', 'danbooru')),
    tags                 TEXT NOT NULL,                -- JSON массив тегов
    check_interval_hours INTEGER NOT NULL DEFAULT 6,
    last_checked_at      DATETIME,
    is_active            INTEGER NOT NULL DEFAULT 1,
    created_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Очередь задач (task_queue) — персистентная очередь для worker'а (Фаза 3)
CREATE TABLE IF NOT EXISTS task_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL,
    task_type  TEXT NOT NULL CHECK(task_type IN ('download', 'process', 'repost')),
    payload    TEXT,                                    -- JSON: media_url, referer, site и т.д.
    attempts   INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'queued'
               CHECK(status IN ('queued', 'in_progress', 'done', 'error')),
    error      TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (post_id) REFERENCES posts(id)
);

-- ==========================================================================
-- Индексы (производительность частых запросов)
-- ==========================================================================
CREATE INDEX IF NOT EXISTS idx_posts_queue_status ON posts(queue_id, status);
CREATE INDEX IF NOT EXISTS idx_posts_file_unique  ON posts(file_unique_id);
CREATE INDEX IF NOT EXISTS idx_posts_source       ON posts(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_posts_media_group  ON posts(media_group_id);
CREATE INDEX IF NOT EXISTS idx_routing_active      ON routing_rules(is_active, priority);
CREATE INDEX IF NOT EXISTS idx_dup_hash_value      ON duplicate_hashes(hash_type, hash_value);
CREATE INDEX IF NOT EXISTS idx_task_queue_status   ON task_queue(status);
CREATE INDEX IF NOT EXISTS idx_users_tg_id          ON users(tg_id);
