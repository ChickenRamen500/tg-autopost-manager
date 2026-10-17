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
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),   -- BOOLEAN: 0/1
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Целевые каналы для постинга
CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_chat_id  INTEGER UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    username    TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Очереди постов
CREATE TABLE IF NOT EXISTS queues (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id         INTEGER NOT NULL,
    name               TEXT NOT NULL,
    interval_minutes   INTEGER NOT NULL DEFAULT 60 CHECK(interval_minutes > 0),
    is_active          INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
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
    -- Блок B/C: настройки отправки (toggle в UI создания очереди)
    strip_caption      INTEGER NOT NULL DEFAULT 0,  -- удалять оригинальную подпись поста при отправке
    created_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
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
    file_type          TEXT NOT NULL CHECK(file_type IN ('photo', 'video', 'gif', 'document', 'audio', 'voice')),
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
    FOREIGN KEY (queue_id) REFERENCES queues(id) ON DELETE SET NULL
);

-- Правила маршрутизации
CREATE TABLE IF NOT EXISTS routing_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type     TEXT NOT NULL,
    source_pattern  TEXT NOT NULL,
    target_queue_id INTEGER NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 0,
    is_active        INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (target_queue_id) REFERENCES queues(id) ON DELETE CASCADE
);

-- Подписи для постов
CREATE TABLE IF NOT EXISTS signatures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Хэши для поиска дубликатов
CREATE TABLE IF NOT EXISTS duplicate_hashes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL,
    hash_value  TEXT NOT NULL,
    hash_type   TEXT NOT NULL CHECK(hash_type IN ('md5', 'phash')),
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
    CONSTRAINT uq_dup_post_hash UNIQUE (post_id, hash_type)
);

-- Подписки на теги (Gelbooru/Danbooru)
CREATE TABLE IF NOT EXISTS tag_subscriptions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                  INTEGER NOT NULL,
    site                     TEXT NOT NULL CHECK(site IN ('gelbooru', 'danbooru')),
    tags                     TEXT NOT NULL,                -- JSON массив тегов
    check_interval_hours     INTEGER NOT NULL DEFAULT 6 CHECK(check_interval_hours > 0),
    -- Фаза 3: интервал уведомлений админа о новых постах (минуты).
    -- 0 = уведомлять при каждом чеке; иначе — не чаще раза в N минут
    -- (throttle, чтобы не спамить при частом check_interval).
    notify_interval_minutes  INTEGER NOT NULL DEFAULT 60 CHECK(notify_interval_minutes >= 0),
    -- Фаза 3 этап 4: очередь по умолчанию для отправки постов из подписки.
    -- При нажатии «Отправить» в карточке поста используется эта очередь
    -- (не нужно каждый раз выбирать). NULL = всегда спрашивать (старое поведение).
    default_queue_id         INTEGER,
    last_checked_at          DATETIME,
    last_notified_at         DATETIME,                       -- когда последний раз шло уведомление
    is_active                INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at               DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (default_queue_id) REFERENCES queues(id) ON DELETE SET NULL
);

-- Просмотренные посты Gelbooru (Фаза 3, ТЗ §2.1.4)
-- Отслеживает, какие посты уже найдены по подписке, и их статус:
--   new      — найден, ждёт решения админа (отправить/пропустить)
--   sent     — отправлен в очередь (пост создан в posts)
--   ignored  — пропущен админом (больше не показывается)
CREATE TABLE IF NOT EXISTS gelbooru_seen_posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    post_id         INTEGER NOT NULL,                    -- ID поста на gelbooru.com
    file_unique_id  TEXT,                                  -- md5 из gelbooru (для дедупа)
    source_url      TEXT,                                  -- file_url для скачивания
    preview_url     TEXT,                                  -- для карточки в UI
    tags            TEXT,                                  -- JSON массив тегов поста
    rating          TEXT,                                  -- safe/questionable/explicit
    status          TEXT NOT NULL DEFAULT 'new'
                    CHECK(status IN ('new', 'sent', 'ignored')),
    post_pk         INTEGER,                               -- ID поста в нашей БД (после «Отправить»)
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    reviewed_at     DATETIME,                              -- когда admin нажал Отправить/Пропустить
    FOREIGN KEY (subscription_id) REFERENCES tag_subscriptions(id) ON DELETE CASCADE,
    CONSTRAINT uq_seen_sub_post UNIQUE (subscription_id, post_id)
);

-- Глобальный blacklist тегов Gelbooru (Фаза 3, ТЗ §2.1.4)
-- Теги, которые исключаются из ВСЕХ подписок (добавляются как -tag к запросу).
-- Например: 'rating_explicit', '1boy', 'loli' — чтобы вырезать нежелательный контент
-- на глобальном уровне, не дублируя -tag в каждой подписке.
CREATE TABLE IF NOT EXISTS gelbooru_blacklist_tags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tag             TEXT NOT NULL UNIQUE,                 -- тег без минуса (например 'rating_explicit')
    created_by_user INTEGER,                              -- кто добавил (NULL = системный)
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by_user) REFERENCES users(id) ON DELETE SET NULL
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
    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
);

-- Настройки приложения (блок D — служебный канал, пороги уведомлений, и т.д.)
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
-- Фаза 3: индексы для gelbooru_seen_posts (поиск новых постов по подписке)
CREATE INDEX IF NOT EXISTS idx_seen_sub_status     ON gelbooru_seen_posts(subscription_id, status);
CREATE INDEX IF NOT EXISTS idx_seen_file_unique    ON gelbooru_seen_posts(file_unique_id);
CREATE INDEX IF NOT EXISTS idx_seen_post_id        ON gelbooru_seen_posts(post_id);
-- idx_users_tg_id удалён: UNIQUE-индекс на users(tg_id) уже создаётся автоматически
-- (избыточный индекс без выигрыша — CR-LOW-3).
-- Unique index для TOCTOU-дедупликации по file_unique_id (CR-H-V1).
-- В SQLite NULL-значения в UNIQUE-индексе считаются различными (NULL != NULL),
-- поэтому partial-WHERE не нужен: несколько постов с file_unique_id=NULL
-- могут сосуществовать, а ненулевые значения должны быть уникальны.
-- Non-partial index, чтобы ON CONFLICT(file_unique_id) DO NOTHING матчило его
-- без явного WHERE в conflict target (как требуют partial-индексы).
CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_file_unique_unique
    ON posts(file_unique_id);
