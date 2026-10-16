---
Task ID: 2
Agent: Explore (repositories reviewer)
Task: Review all files in app/repositories/

Work Log:
- Read /home/z/my-project/worklog.md (did not exist — created it)
- Read app/db/schema.sql and app/db/database.py to establish schema/FK context
- Read all 11 files in app/repositories/ (__init__, app_settings, channels, duplicates, posts, queues, routing, signatures, subscriptions, task_queue, users)
- Read tests/unit/test_repositories.py to understand contract expectations
- Read conftest.py run_with_db helper
- Read consumers of repositories: app/scheduler/__init__.py, app/services/queue_time.py, app/services/routing/service.py, app/services/archive.py, app/bot/handlers/admin.py & menu.py (grep)
- Grep-traced usage of every repo: confirmed duplicates.py, signatures.py, subscriptions.py, task_queue.py are NOT imported anywhere in app/ (only in tests / docs) — they are "ready for future phases"
- Cross-checked FK behaviour: schema.sql has NO `ON DELETE CASCADE` on any FK (queues→channels, posts→queues, routing_rules→queues, duplicate_hashes→posts, task_queue→posts, tag_subscriptions→users). Default = NO ACTION (RESTRICT) with foreign_keys=ON.
- Compiled per-file findings with file:line citations and code snippets
- Appended Stage Summary

Stage Summary:
- 11 repository files reviewed (~830 LOC of repo code + schema + database.py context).
- ~33 distinct issues found, broken down: HIGH severity = 6 (FK-cascade gaps that crash runtime deletes; ignored return values in queue_time loops; partial-transaction risks on bulk ops; task_queue race/stale-task gaps), MEDIUM = 14 (state-machine gaps, validation gaps, inconsistencies between repos, N+1 loops, dead code), LOW = 13 (boilerplate, naming, docstring drift, perf micro-issues).
- Notable cross-cutting patterns:
  1. No FK ON DELETE CASCADE anywhere → every `remove_*` / `delete_post` is a time-bomb if children exist (IntegrityError propagates, unhandled).
  2. Repetitive `cur = await db.execute(...); await db.commit(); changed = cur.rowcount > 0; await cur.close(); return changed` boilerplate (~20 copies) — ripe for a helper.
  3. `changed = cur.rowcount > 0` is read AFTER `await db.commit()` in every repo — works in aiosqlite today but fragile across driver versions.
  4. Four repos (duplicates, signatures, subscriptions, task_queue) are dead code in the current app — only referenced by tests/docs — so their bugs are latent.
  5. Posts lifecycle in ТЗ §2.3.4 says "new → downloading → pending → sent | failed | duplicate | possible_duplicate" but `posts.update_status` permits ANY→ANY transitions — no state-machine enforcement.
  6. queue_time.handle_end_strategy (consumer of posts repo) increments `moved`/`deleted` counters unconditionally, ignoring the bool returned by assign_queue/delete_post — silently mis-reports and ignores failures.
  7. No SQL injection found — every repo uses `?` parameter binding. The only f-string SQL is `database._column_exists` (PRAGMA table_info({table})) and table is from a hardcoded migration list.
  8. No N+1 in hot paths (send loop is 1 query per tick), but bulk strategies in queue_time loop Python-side (could be a single UPDATE).
  9. Schema indexes cover the common query paths (idx_posts_queue_status, idx_posts_file_unique, idx_routing_active, idx_dup_hash_value, idx_task_queue_status) — only missing idx for tag_subscriptions(is_active, last_checked_at).

---
Task ID: 3
Agent: Explore (services reviewer)
Task: Review all files in app/services/

Work Log:
- Read /home/z/my-project/worklog.md — prior repo reviewer left 9 cross-cutting findings; reused their context on FK-cascade / state-machine / counter bugs
- Listed app/services/ tree; discovered most service files are 0-byte stubs (caption/, dedup/, media/, parsers/__init__.py, parsers/pikabu/, parsers/xcom/, parsers/danbooru/, parsers/pixiv/)
- Read non-stub services: notifications.py, queue_time.py, archive.py, routing/__init__.py + routing/service.py
- Read all 4 gelbooru parser files: __init__.py, cache.py, tags_db.py, client.py
- Peeked at repo contracts these services call: app_settings.py, posts.py, routing.py, queues.py
- Read consumers: scheduler/__init__.py (calls queue_time + notifications), bot/handlers/media.py (calls routing + archive), main.py lifespan (calls tags_db + cache + scheduler), bot/handlers/menu.py (calls queue_time.format_queue_time_info)
- Read schema.sql to confirm queue.end_strategy CHECK constraint, daily_window TEXT format, posts.phash column (still unused by code)
- Read scripts/populate_tags.py + scripts/init_db.py + scripts/check_phase2.py to understand gelbooru client/tags_db lifecycle
- Read requirements.txt: yt-dlp, Pillow, imagehash, ffmpeg-via-subprocess are listed but have NO consuming code in services/ (stubs only)
- Read tests/unit/test_routing.py + test_regression.py (services sections) to confirm intended contracts
- Compiled per-file findings with file:line citations and code snippets

Stage Summary:
- 17 service files reviewed. **8 are 0-byte empty stubs** (caption, dedup, media, parsers/__init__, pikabu, xcom, danbooru, pixiv). Only 9 contain code: notifications.py, queue_time.py, archive.py, routing/{__init__,service}.py, gelbooru/{__init__, cache, tags_db, client}.py — ~575 LOC of real code total.
- **Critical architectural gap**: The task description lists expected review surfaces (ffmpeg subprocess in media/, pHash algorithm in dedup/, yt-dlp in xcom/pikabu, caption pipeline) — none of these exist yet. They are Phase 3–5 stubs. So most "special attention" items are non-reviewable (N/A).
- 38 distinct issues found across the 9 real files. Severity breakdown: HIGH = 6, MEDIUM = 16, LOW = 16.
- HIGH-severity issues:
  1. `queue_time.is_in_time_window` silently ignores `daily_window` if only one of start/end is set (line 91-93) — admin typo leads to queue running 24/7 with no warning.
  2. `gelbooru/client.search_posts` caches empty result for 60s on network failure / 5xx / 429 (line 122) — a 1s network blip causes 60s of "no posts" silently. No retry/backoff on transient failures.
  3. `gelbooru/client._get_session` race condition (line 44-47): no lock — concurrent first-call from `_request` (locked) and `check_file_alive` (unlocked) can orphan a ClientSession (TCP connection leak).
  4. `gelbooru_client` singleton's aiohttp.ClientSession is NEVER closed on app shutdown (lifespan at main.py:113-127 closes polling task + scheduler + tags_db + main db, but not `gelbooru_client.close()`). Same for `cache.start_cleanup_task()` — never cancelled on shutdown.
  5. `gelbooru/client.check_file_alive` (line 220-238) bypasses both `_semaphore` and `_rate_limit` — can spike Gelbooru with >8 req/s and trigger API ban. Also no per-request timeout override (uses session default 10s).
  6. `notifications.notify_queue_almost_empty` / `notify_queue_finished` have NO deduplication/throttle — scheduler calls `_check_queue_notifications` after EVERY send (scheduler/__init__.py:240, 264-283), so an empty queue spams admin every interval forever. (The bare `except Exception` in `_check_queue_notifications` masks subsequent failures but the FIRST path still spams.)
- MEDIUM-severity issues:
  - `routing/_looks_like_regex` (line 49-51): treats any pattern containing `.` `+` `*` as regex. Pattern `@some.channel` is silently regex → `.` matches any char → `@someXchannel` matches. Username routing is a footgun (test_routing.py:6 confirms `.` triggers regex but no test guards against false-positives for usernames).
  - `routing/_matches` uses `re.fullmatch(..., flags=re.IGNORECASE | re.DOTALL)` (line 39) — DOTALL makes `.` match newlines, unexpected for tag matching.
  - `routing/_REGEX_HINTS` (line 46) includes `{` and `}` but NOT `:` or `/` — patterns like URLs (`https://x.com/foo`) are treated as exact match (no regex hint), breaking intended regex use.
  - `queue_time._parse_iso_utc` (line 23-36) silently assumes naive datetime = UTC; if anyone stores local time, scheduling is wrong with no warning.
  - `queue_time.handle_end_strategy` (line 132-147) loops with limit=1000 — if queue has >1000 pending, only first 1000 are moved/deleted, rest are silently orphaned. Also `moved`/`deleted` counters ignore bool return of `assign_queue`/`delete_post` (carried over from repo-layer bug).
  - `archive._source_tag` (line 41-48) returns `#from_dm` for non-Telegram sources (gelbooru, xcom, local_folder). Misleading for archive search: booru posts tagged as if from DM.
  - `archive.archive_album` builds `all_types` via set comprehension (line 111) → non-deterministic ordering of `#pic`/`#vid` in caption tags.
  - `gelbooru/client._request` (line 88) only catches `TimeoutError` explicitly; `aiohttp.ClientError` / `ServerDisconnectedError` / `ContentTypeError` all swallowed by bare `except Exception` (line 91-93) with one log line — no retry, no classification.
  - `gelbooru/client._request` reads body BEFORE checking status (line 74) — wasteful on 404/5xx, and `response.json()` on non-JSON body raises ContentTypeError that's then swallowed.
  - `gelbooru/client._request` safe_params mask is dead code (line 62): `api_key` is added to params AFTER safe_params is computed (line 67), so the `"***" if k == "api_key"` condition never triggers.
  - `gelbooru/tags_db.init_tags_db` (line 52-72) doesn't set `PRAGMA busy_timeout` or `WAL` on tags.db — concurrent populate_tags.py write + app read can fail with "database is locked".
  - `gelbooru/tags_db.upsert_tags` (line 88-113): per-tag `except Exception` swallows errors and continues; if DB is locked, every tag logs a warning → log flooding + final `commit()` may also fail without rollback.
  - `gelbooru/tags_db.init_tags_db` (line 52-56) check-then-assign race: two concurrent calls can both pass `if _conn is not None: return` and connect twice, leaking one connection.
  - `gelbooru/cache.start_cleanup_task` (line 78-91) creates an asyncio.Task that is NEVER stored in a global — main.py:99 calls it but discards the return value → on shutdown the task is never cancelled, raises CancelledError inside `cleanup_loop`'s `asyncio.sleep`, dies silently.
  - `notifications.notify_send_error` (line 75) truncates error to 200 chars — fine, but no structure; multi-line stack trace flattened to one log entry.
  - `archive.archive_post` (line 100) and `archive_album` (line 139) use bare `except Exception` (BLE001) — silent failure paths, the admin gets no signal that archive is broken.
  - `queue_time._now_local()` (line 55-57) calls `ZoneInfo(settings.timezone)` on every invocation — wasteful; should cache the ZoneInfo instance (zoneinfo caches internally but still incurs dict lookup overhead).
  - `gelbooru/client._rate_limit` uses `asyncio.Lock` to serialize ALL requests through one lock — combined with the `Semaphore(8)` (line 39), the semaphore is effectively useless (lock serializes anyway). Design dead-weight.
- LOW-severity issues:
  - `routing/__init__.py` re-exports only `route_post`; helpers `_matches` / `_looks_like_regex` are accessible only via full path — inconsistent with gelbooru package re-export style.
  - `routing/service._REGEX_HINTS` is defined AFTER `_matches` (line 46 vs line 37) — works due to late binding, but reads top-down confusingly.
  - `archive._sanitize_tag_id` (line 38) uses `lstrip("-")` — for chat_id `-100` it returns `100`, OK. For multiple leading dashes (`--100`) returns `100`, edge case.
  - `notifications.notify_admin` (line 28-34 + 37-45) calls `from app.bot.runtime import get_bot` TWICE in same function — wasteful but harmless (Python caches imports).
  - `gelbooru/client.search_posts` cache key includes raw `tags` string (line 97) — `"1girl rating:safe"` and `"rating:safe 1girl"` produce different keys for same logical query (no normalization).
  - `gelbooru/client.get_tags_by_names` cache key `"tag_names:" + "|".join(sorted(clean))` (line 176) — for 10K tags this becomes a multi-KB dict key. No length cap.
  - `gelbooru/cache.py` functions are sync (no `async`) — works for in-event-loop dict access, but the module's `start_cleanup_task` is async → mixed sync/async API style.
  - `gelbooru/cache.stats()` (line 61-65) iterates entire `_cache` to count alive — O(n) on every call; fine for small caches, slow if cache grows.
  - `gelbooru/tags_db.autocomplete` (line 153-165) does `prefix.lower()` but column has no `LOWER()` — for non-ASCII tags (e.g., Japanese) `LIKE` is case-sensitive.
  - `gelbooru/tags_db.get_tag_types` (line 116-134) chunks by 500 but SQLite variable limit is 999 — could safely use 800 to reduce round-trips.
  - `queue_time.format_queue_time_info` (line 154-175) returns "без ограничений (бесконечно)" even when `end_strategy` is non-default — misleading label.
  - `archive.archive_post` sends `caption=full_caption` for `send_photo` etc (line 91-97) — Telegram limits caption to 1024 chars; if tags+caption exceed 1024, API call fails silently (caught at line 100).
  - `gelbooru/client._request` line 86-87: non-200 non-404 status returns None with no body peek — could be 429 Retry-After that we should respect, but we don't (no `retry_after` extraction unlike scheduler).
  - `gelbooru/client._parse_tag_response` (line 203-218) is static but reads like instance method (no `self` use) — could be module function.
  - `services/__init__.py` is a 1-line docstring with no re-exports — inconsistent with routing/__init__.py style.
  - `gelbooru/__init__.py` imports both `cache` and `tags_db` (line 9) — forces tags_db connection to be created lazily; OK but introduces import-time side-effect risk if tags_db grows.
  - Tests cover only routing + queue_time + archive tag helpers (test_regression.py:701-932). No tests for gelbooru client, cache, tags_db, notifications — entire subsystems untested.
- Notable cross-cutting patterns:
  1. **Major stub gap**: 8 of 17 service files are 0 bytes. The task description's "special attention" items (ffmpeg in media/, pHash in dedup/, yt-dlp in xcom/pikabu, caption pipeline) all map to non-existent code. requirements.txt declares yt-dlp, Pillow, imagehash — but no service code consumes them. (Imagehash would be used by dedup; ffmpeg by media; yt-dlp by xcom/pikabu parsers.)
  2. **No HTTP retry/backoff anywhere**: gelbooru/client.py's `_request` returns None on any failure (timeout, 5xx, network), and `search_posts` then caches that None-result as empty list for 60s. No exponential backoff, no Retry-After handling, no circuit breaker. Compare: scheduler/__init__.py:242-259 DOES handle Telegram's `retry_after` — but the gelbooru client doesn't learn from the same pattern.
  3. **Lazy `from X import Y` pattern repeated 8+ times**: archive.py, notifications.py, scheduler.py, media.py all do `from app.bot.runtime import get_bot` INSIDE functions "to break import cycles". This works but obscures dependency graph and adds per-call import-dict-lookup overhead. Acceptable trade-off but inconsistent.
  4. **Bare `except Exception` (BLE001) is the dominant error pattern**: 12 occurrences across services (notifications ×2, archive ×2, gelbooru/client ×3, gelbooru/cache ×1, scheduler ×3). All silently swallow + log. None distinguish transient vs structural failures.
  5. **Singleton-resource lifecycle gap**: tags_db._conn (tags_db.py:45), gelbooru_client._session (client.py:42), cache._cache (cache.py:25), scheduler._last_send (scheduler/__init__.py:38), runtime._bot (bot/runtime.py:20) — all module-level globals with no explicit locking. Within single event loop it's safe, but main.py lifespan never calls `gelbooru_client.close()` (only `close_bot`, `close_db`, `close_tags_db`) — aiohttp ClientSession + underlying TCP connector leak on every container restart.
  6. **Duplication between archive.archive_album and scheduler._send_album_group** (media group assembly logic for InputMediaPhoto/Video/Document): 2 copies, slightly different behavior (archive uses first-post caption; scheduler uses first-post caption too). If album API changes, both must be edited.
  7. **`queue_time` mixes sync and async API**: `is_in_time_window` is sync (line 60), `handle_end_strategy` is async (line 109), `format_queue_time_info` is sync (line 154). Inconsistent for callers (scheduler has to know which is which).
  8. **No SQL injection risk** in services — all DB calls go through repositories which use parameterized queries (per prior reviewer). The only raw-SQL is in handlers/media.py:67 (`SELECT * FROM posts WHERE media_group_id = ?`) — parameterized, fine.
  9. **posts.phash column exists in schema** (schema.sql:80) but **no service computes or stores pHash** — dedup/__init__.py is empty stub, so the column is always NULL. Schema is forward-compatible but unused.
  10. **Consumer-side bug found outside services** (worth flagging to repo reviewer's findings): scheduler/__init__.py:276 calls `posts_repo.count_pending(queue["id"])` — but `posts.py` has NO `count_pending` function (only `count_by_status`). The function exists in `queues.py:110`. This will raise AttributeError at runtime when scheduler tries to send a post and reaches `_check_queue_notifications`. Either repo reviewer missed it or it's a regression introduced after their review. **HIGH runtime crash risk.**

---
Task ID: 5
Agent: Explore (scheduler/api/worker reviewer)
Task: Review app/main.py, app/scheduler/, app/worker/, app/api/, app/utils/

Work Log:
- Read /home/z/my-project/worklog.md — prior Tasks 2 (repos) + 3 (services); reused their cross-cutting findings on FK-cascade, state-machine, gelbooru_client close leak, cache cleanup task leak, posts_repo.count_pending crash risk
- Listed app/ tree; verified the 6 target files exist (4 are 0-byte stubs: app/__init__.py, app/api/__init__.py, app/api/routes/__init__.py, app/worker/__init__.py, app/utils/__init__.py, app/config/__init__.py — 6 stubs total)
- Read app/main.py (164 LOC) — FastAPI lifespan + 3 /health endpoints
- Read app/scheduler/__init__.py (348 LOC) — APScheduler setup + send_post_from_queue + album grouping + retry/throttle + _check_queue_notifications
- Read app/utils/local_bot_api.py (184 LOC) — create_bot + logout_from_cloud + get_me + test_big_upload_download
- Read supporting context: app/config/settings.py, app/bot/runtime.py, app/bot/__init__.py, app/db/database.py, app/db/schema.sql, app/repositories/{posts,queues,channels}.py, app/services/{notifications,queue_time,archive}.py, app/services/parsers/gelbooru/{__init__,cache,client}.py, app/bot/handlers/admin.py, app/bot/middleware/access.py, conftest.py, requirements.txt, pyproject.toml, docker-compose.yml, docker/local-bot-api.md
- Grep-traced: count_pending usage (confirmed `posts_repo.count_pending` in scheduler:276 is a typo — function exists in queues_repo:110 only); reload_scheduler usage (admin.py ×3, menu.py:40, main.py); close_bot/gelbooru_client.close/cache.stop (confirmed lifespan NEVER calls any of them); import graph for circular-import analysis
- Read tests/unit/test_scheduler.py (115 LOC) and tests/unit/test_regression.py:135-179 — confirmed tests do NOT exercise send_post_from_queue tick, so the count_pending AttributeError would not surface in CI
- Verified app/__init__.py, app/api/__init__.py, app/api/routes/__init__.py, app/worker/__init__.py, app/utils/__init__.py, app/config/__init__.py are 0 bytes (wc -c = 0)
- Compiled per-file findings with file:line citations and code snippets

Stage Summary:
- 9 target files reviewed; 6 of them are 0-byte stubs (app/__init__.py, app/api/__init__.py, app/api/routes/__init__.py, app/worker/__init__.py, app/utils/__init__.py, app/config/__init__.py). Only 3 contain code: app/main.py (164 LOC), app/scheduler/__init__.py (348 LOC), app/utils/local_bot_api.py (184 LOC) — ~696 LOC of real code.
- ~52 distinct issues found across the 3 real files + 6 stubs. Severity breakdown: HIGH = 14, MEDIUM = 22, LOW = 16.
- HIGH-severity issues (top):
  1. **scheduler/__init__.py:276** — `posts_repo.count_pending(queue["id"])` calls a function that DOES NOT EXIST on `posts_repo` (it lives in `queues_repo:110`). Called after every successful send inside `_check_queue_notifications`. Bare `except Exception` at line 282 swallows the AttributeError → entire notifications subsystem silently broken. Already flagged by Task 3 reviewer (services) — confirmed here from scheduler side.
  2. **main.py lifespan shutdown (lines 113-127)** — NEVER calls `close_bot()`, `gelbooru_client.close()`, or cancels `cache.start_cleanup_task()`. The bot's AiohttpSession (TCP pool to local-bot-api:8081), the gelbooru_client's aiohttp ClientSession, and the cache cleanup asyncio.Task all leak on every container restart. Functions exist (`bot/runtime.py:40 close_bot`, `gelbooru/client.py:240 close`, `cache.start_cleanup_task` returns the Task) but lifespan drops the return value.
  3. **scheduler/__init__.py:44** — `AsyncIOScheduler(timezone="UTC")` uses default in-memory jobstore (no `jobstores=SQLAlchemyJobStore`). All jobs lost on restart. setup_scheduler_async re-registers from DB, but `next_run_time` resets — missed ticks are dropped, not recovered.
  4. **scheduler/__init__.py:315-324** — `add_job` has no `misfire_grace_time`. APScheduler default = 1 second. With throttle `await asyncio.sleep(1.1s)` + send time + mark_sent + notifications, ticks easily exceed 1s → next tick misfired/skipped. Combined with `coalesce=True`, only one instance runs but the misfired run is LOST.
  5. **scheduler/__init__.py:186** — `await asyncio.sleep(wait)` for throttle BLOCKS the scheduler tick. With AsyncIOScheduler, the tick runs in the event loop; the sleep blocks the loop, so other queue jobs can't fire during the throttle. Also conflates "throttle between sends in same queue" with "tick duration". Should use per-queue `asyncio.Lock` or token bucket, not sleep-in-tick.
  6. **scheduler/__init__.py:206-208** — album mark_sent loop has 10 separate `await posts_repo.mark_sent(p["id"])` calls, each commits separately. If process crashes between mark_sent(P1) and mark_sent(P2), album is partially sent (Telegram received all 10 files) but DB only marks 1 as sent → on restart, get_next_pending returns P2, find_album_posts returns [P2..P10], album is RE-SENT as 9 files → duplicate album with same media_group_id. No transaction wrapping.
  7. **scheduler/__init__.py:216-219, 245-248** — 429 retry_after handling: `getattr(e, "retry_after", None)`. If attribute missing (non-RetryAfter exception, or aiogram version drift), falls through to `await asyncio.sleep(2.0)`. For 429 with retry_after=30, we sleep 2s and immediately retry → another 429 → 2s → ... → exhausts max_attempts=3 in ~6s, then mark_failed. retry_after effectively ignored. Also no permanent-error classification (chat_not_found / bot_blocked 403/400 → should NOT retry, should mark failed immediately per ТЗ §2.5.2 p.5 "permanent fail" — missing).
  8. **scheduler/__init__.py:195** — `find_album_posts(queue_id, mgid, limit=10)`. Telegram's send_media_group has a HARD LIMIT of 10 items. If album has >10 pending posts, only first 10 sent, rest stay pending. Next tick re-fetches them with SAME mgid → second send_media_group with same media_group_id → Telegram rejects ("media group can't have more than 10 items" or "media_group_id already used") → retries → all fail. No chunking.
  9. **scheduler/__init__.py:98** — strategy 2 (local file) reads entire file into memory via `await asyncio.to_thread(_read_bytes)` then `BufferedInputFile(data, ...)`. For 2000 MB Local Bot API limit, this loads 2 GB into RAM. docker-compose.yml:59 caps container at 2 GB → OOM kill. Should use `FSInputFile` (path-based, streamed) instead of `BufferedInputFile` (bytes-based, in-memory).
  10. **scheduler/__init__.py:222-225** — album failure path: marks all posts failed but does NOT call `notify_send_error` (single-post path does at line 257). Inconsistent — admin gets no alert when album fails.
  11. **main.py:133-163** — `/health`, `/health/bot-api`, `/health/scheduler` have NO auth. Exposed: `bot_api_url` (internal docker URL), `config_problems` (leaks which secrets are missing), `jobs` list with `queue_<id>` and next_run_time (leaks schedule cadence). docker-compose.yml:55 exposes port 8000 to host. No SECRET_TOKEN check despite settings.secret_token existing.
  12. **main.py:108** — `_bot_polling_task` global race: lifespan doesn't check `if _bot_polling_task and not _bot_polling_task.done()` before re-creating. On uvicorn --reload or test double-init, old task orphaned.
  13. **app/worker/__init__.py** is 0 bytes — worker is NOT implemented. `task_queue` table (schema.sql:136-147) and `task_queue.py` repo (per Task 2 reviewer — dead code) have no consumer. Phase 3 features (autodownload, yt-dlp, ffmpeg, imagehash dedup) all depend on a worker that doesn't exist.
  14. **app/api/__init__.py, app/api/routes/__init__.py** are both 0 bytes — REST API for Chrome extension (ТЗ §6, §9) is NOT implemented. main.py doesn't import app.api at all. Only 3 inline /health endpoints exist. SECRET_TOKEN validation not implemented anywhere.
- MEDIUM-severity issues (sample):
  - scheduler/__init__.py:140-142 — `_send_album_group` for `len(media)==1` calls `_send_one_post(posts[0], channel)` WITHOUT `queue` parameter → strip_caption / signature_template not applied for single-item "albums".
  - scheduler/__init__.py:182, 204, 228, 237, 261 — `asyncio.get_event_loop().time()` deprecated in 3.10+; should use `asyncio.get_running_loop().time()` or `time.monotonic()`.
  - scheduler/__init__.py:286-300 — `setup_scheduler` (sync) docstring lies: says "Регистрирует interval-job" but only removes old jobs, doesn't register. Dead/misleading code.
  - scheduler/__init__.py:282 — bare `except Exception` in `_check_queue_notifications` hides the count_pending AttributeError bug. Should be narrower.
  - scheduler/__init__.py:99 — `file_path.rsplit("/", 1)[-1]` uses `/` separator → broken on Windows paths (`\`). Use `os.path.basename` or `pathlib.Path.name`.
  - scheduler/__init__.py:92, 117 — lazy `from aiogram.types import ...` inside functions on every send call. Should be top-level.
  - scheduler/__init__.py:8-12 — docstring hardcodes "max 3 попытки" but `max_send_attempts` is configurable via settings. Doc drift.
  - main.py:31 — `logging.basicConfig(level=logging.INFO)` at module import time → overrides uvicorn/loguru config; tests importing app.main get root logger reset.
  - main.py:73-78 — `_bot_polling` has NO retry/reconnect on transient failure. If `dp.start_polling` raises non-CancelledError, task logs and exits silently. Bot is then deaf until container restart. Scheduler keeps running (posts still sent), but no new media from DMs.
  - main.py:92 — `bootstrap_admin(settings.admin_tg_id, None)` passes None as username. Function signature `username: str | None` accepts it. Not a bug (task description flagged it but type is already Optional[str]).
  - local_bot_api.py:40-52 — `create_bot(timeout_s=None)` skips timeout, AiohttpSession defaults to 60s. `runtime.get_bot()` calls `create_bot()` with no timeout → regular sends use 60s. For 2000 MB strategy-2 sends, 60s is too short → TelegramNetworkError("Request timeout error"). Only `test_big_upload_download` passes BIG_UPLOAD_TIMEOUT_S.
  - local_bot_api.py:93 — `make_test_file` uses `secrets.token_bytes(chunk)` per MB. For 300 MB, generates 300 MB of crypto-secure randomness (~15 min on weak hardware). Should use `os.urandom` or `bytearray` (any random bytes work; "non-compressible" doesn't require crypto-secure).
  - local_bot_api.py:61, 71 — `logout_from_cloud` and `get_me` construct URL with bot_token in path. If aiohttp ClientError str includes the URL, token may leak into logs. Mitigated at line 83 (logs `settings.bot_api_url` not full URL), but ClientError.__str__ may still include URL.
  - local_bot_api.py:171 — `resolve_local_file_size` candidate `Path(file_path)` is dead code in Docker (bot container doesn't have /var/lib/telegram-bot-api/...). Only works if running bot + local-bot-api on same host (non-Docker).
  - local_bot_api.py:76 — `get_me` creates new aiohttp.ClientSession per call → new TCP/TLS handshake per /health/bot-api request. Should reuse a session.
  - local_bot_api.py:162 — docstring typo "Перепролируем" → should be "Проецируем".
  - app/config/__init__.py is 0 bytes — `from app.config import settings` doesn't work (must use `from app.config.settings import settings`). Inconsistent with `app/db/__init__.py` which re-exports.
  - app/utils/__init__.py is 0 bytes — no re-exports of `local_bot_api`, `create_bot`, `get_me`. Inconsistent with `app/services/parsers/gelbooru/__init__.py` re-export style.
- LOW-severity issues (sample):
  - Circular-import analysis: NO actual cycle at module import time today. `app.bot.__init__` imports handlers (admin/media/menu/start). `menu.py:40` imports `from app.scheduler import reload_scheduler` at top-level. `admin.py:339, 359, 372` does the SAME import lazily inside functions (inconsistent with menu.py). `scheduler/__init__.py:54, 119` does `from app.bot.runtime import get_bot` lazily inside functions. The cycle the comments describe (bot↔scheduler) is broken by `app.bot.runtime` indirection — runtime.py doesn't import scheduler. So lazy imports in scheduler are defensive but not strictly needed today. The lazy-vs-top-level inconsistency between menu.py and admin.py is a code smell.
  - scheduler/__init__.py:23-31 — multi-line `from app.repositories import (X as Y,)` with trailing comma. Style oddity.
  - scheduler/__init__.py:48 — `queue: dict | None = None` should be `dict[str, Any] | None` for consistency with line 159.
  - main.py:46 — `import traceback` inside `_on_polling_done` function. Should be top-level.
  - main.py:69-70 — `if wh.url:` only checks truthiness, doesn't validate URL scheme/host.
  - main.py:124 — `sched.shutdown(wait=False)` doesn't wait for running ticks. If a tick is mid-send, it's cancelled abruptly. Combined with no transaction wrapping (HIGH #6 above), increases risk of partial-send.
  - local_bot_api.py:37 — `BIG_UPLOAD_TIMEOUT_S` reads env var at module import. Won't pick up runtime changes.
  - local_bot_api.py:152 — `LOCAL_API_MOUNT` hardcoded default `/app/bot-api-data`. Matches docker-compose.yml:46 but assumes container layout.
  - Tests gap: test_scheduler.py (115 LOC) only tests strategy selection logic (file_id vs file_path) and `increment_attempt`/`mark_failed` — never exercises `send_post_from_queue` tick. The `posts_repo.count_pending` AttributeError would NOT be caught by tests. test_regression.py:135-179 tests `reload_scheduler` job registration but not tick execution.
- Notable cross-cutting patterns:
  1. **Singleton-resource lifecycle gap (carried over from Task 3)**: bot.runtime._bot, gelbooru_client._session, cache._cache + cleanup_task, scheduler._scheduler, scheduler._last_send, main._bot_polling_task — all module-level globals. main.py lifespan closes only `_bot_polling_task` (via cancel), `sched` (via shutdown), `tags_db`, and main `db`. **Missing: `close_bot()`, `gelbooru_client.close()`, `cache` cleanup task cancel.** Three resources leak per restart.
  2. **APScheduler misconfiguration pattern**: in-memory jobstore (jobs lost on restart), no `misfire_grace_time` (default 1s too tight), no `job_defaults`, `coalesce=True`+`max_instances=1` set per-job (good) but inconsistent if new jobs added without specifying. Timezone=UTC for scheduler but `settings.timezone=Asia/Yekaterinburg` for `daily_window` — works because IntervalTrigger is TZ-agnostic, but confusing.
  3. **Bare `except Exception` (BLE001) hides bugs**: scheduler/__init__.py:282 swallows the count_pending AttributeError. scheduler/__init__.py:211, 242 swallow Telegram API errors without classifying transient (429, network) vs permanent (chat_not_found, bot_blocked, file_id_invalid). ТЗ §2.5.2 p.5 "permanent fail" is NOT implemented — all errors retry max_attempts times.
  4. **Lazy `from X import Y` inside functions** repeated 8+ times (scheduler ×3, notifications ×2, archive ×2, admin.py ×3) — breaks import cycles but obscures dependency graph and adds per-call overhead. Inconsistent: menu.py:40 does top-level import of `reload_scheduler`, admin.py does it lazily.
  5. **No persistence anywhere**: APScheduler in-memory (jobs lost), MemoryStorage for FSM (user state lost), `_last_send` in-memory (throttle lost on restart — actually OK, throttle resets safely). Only DB is persistent.
  6. **3 stubs in app/api/** (api/__init__.py, api/routes/__init__.py, app/worker/__init__.py) + app/__init__.py + app/utils/__init__.py + app/config/__init__.py = 6 zero-byte files in app/. Worker (Phase 3) and REST API (Phase 4) are not started. The directory structure was scaffolded ahead of phases but never filled.
  7. **Security**: NO auth on /health endpoints. `SECRET_TOKEN` setting exists but is never checked. The /health/scheduler endpoint leaks job IDs and next_run_time — useful for an attacker to infer posting schedule.
  8. **Telegram-specific**: 429 retry_after partially handled (only via `getattr(e, "retry_after", None)` — works for aiogram's RetryAfter but falls through to 2s sleep otherwise). `chat_not_found` / `bot_blocked` (403 Forbidden) NOT classified as permanent — retries 3× then marks failed. No media_group_id collision detection (Telegram rejects reuse of media_group_id). No send_media_group ≤10 chunking (album >10 items breaks). No caption 1024-char truncation. No chat_id type confusion (-100xxx int is fine; aiogram accepts int).

---
Task ID: 6
Agent: Explore (tests/scripts/docker/docs reviewer)
Task: Review tests, scripts, Docker, docs, project structure

Work Log:
- Read /home/z/my-project/worklog.md — reused Task 2 (repositories) and Task 3 (services) findings on FK-cascade gaps, dead-code repos, scheduler count_pending bug, gelbooru client lifecycle leaks
- Listed project tree at /tmp/tg-autopost-manager/ to ground-truth PROJECT_STRUCTURE.md claims
- Read all 7 test files (tests/__init__.py, tests/unit/__init__.py, tests/integration/__init__.py, conftest.py, test_routing.py, test_regression.py [933 lines, read in 3 chunks], test_settings.py, test_scheduler.py, test_repositories.py)
- Read all 6 script files (populate_tags.py, fix_encoding.py, check_phase2.py, cleanup_downloads.py [empty], check_phase1.py, init_db.py)
- Read Docker stack (Dockerfile, docker-compose.yml, docker/local-bot-api.md) + .dockerignore + .gitignore + .gitattributes + .env.example
- Read config (pyproject.toml, requirements.txt) and settings.py for env-var mapping
- Read schema.sql + database.py to verify init_db.py / migrations sync
- Spot-checked consumers: app/main.py (lifespan), app/scheduler/__init__.py (count_pending call), app/services/routing/{__init__,service}.py, app/repositories/{users,posts,queues}.py function existence
- Verified file existence for docs claims: .env.example ✓, .dockerignore ✓, app/web/ ✗ (does NOT exist), chrome-extension/ ✗ (does NOT exist), data/db/ ✓ (empty .gitkeep only — bot.db actually at data/bot.db), .github/ ✗ (no CI), .pre-commit-config.yaml ✗
- Cross-checked schema vs docs: schema.sql has 10 tables (incl. app_settings line 150-154), docs/check_phase2.py:58 list 9 (no app_settings)
- Cross-checked disk path: PHASE1_GUIDE.md:28 says `D:\Media`, docker-compose.yml:51 + QUICKSTART:18 say `E:\Media`
- Cross-checked TZ: Dockerfile:17 `TZ=Europe/Moscow` vs settings.py:58 `timezone="Asia/Yekaterinburg"`
- Compiled per-file findings with file:line citations and code snippets
- Appended Stage Summary

Stage Summary:
- 24 files reviewed (~3000 LOC). ~52 distinct issues found. Severity: HIGH = 12, MEDIUM = 21, LOW = 19.
- **HIGH-severity issues**:
  1. **`tests/unit/test_scheduler.py` tests ZERO scheduler code** — file header (lines 1-5) admits "Полный цикл отправки требует Telegram-бота — тестируем только логику выбора стратегии". Tests at lines 8-33 and 36-60 RE-IMPLEMENT the strategy boolean (`bool(post["tg_file_id"]) and not post["was_processed"]`) inside the test body instead of calling `app/scheduler/__init__.py`. The actual 348-LOC scheduler module (429 retry, throttle, max_attempts→failed, `_check_queue_notifications`) is NEVER imported. Combined with Task 3 reviewer's finding that `scheduler/__init__.py:276` calls non-existent `posts_repo.count_pending` (only `queues_repo.count_pending` exists at queues.py:110) — this AttributeError crash at runtime is completely undetected by the test suite.
  2. **No CI/CD, no lint/type config, no pre-commit** — `.github/` and `.pre-commit-config.yaml` do NOT exist; `pyproject.toml` is 8 lines with only `[tool.pytest.ini_options]`, NO `[tool.ruff]`/`[tool.mypy]`/`[tool.black]`. Yet `DOCS/PROJECT_STRUCTURE.md:74` and `DOCS/1.md:196` both claim "ruff: 0 errors" — unverifiable claim with no config.
  3. **`requirements.txt` has NO version pins** (except `aiogram>=3.4` at line 3) — fastapi, uvicorn, aiosqlite, apscheduler, aiohttp, yt-dlp, Pillow, imagehash, jinja2, python-dotenv are all floating latest. Reproducibility risk; any new release can break the app.
  4. **Dev/test deps missing from `requirements.txt`**: NO `pytest`, NO `pytest-asyncio`, NO `httpx` (for FastAPI TestClient). `DOCS/1.md:194` says `pip install -r requirements.txt` then `pytest tests/unit` — but pytest isn't installed. Fresh setup can't run tests.
  5. **Dockerfile runs as ROOT** (no `USER` directive, `docker/Dockerfile:1-23`). Container compromise = root access to host.
  6. **No healthcheck on `bot` container** (docker-compose.yml:30-61). `local-bot-api` has healthcheck (lines 20-27), but bot doesn't. Bot can have a dead polling task while FastAPI `/health` still returns 200.
  7. **No resource limits on `local-bot-api`** (compose:3-27) — ТЗ §3.2 says "слабое железо AMD Athlon X4 / 8 ГБ RAM" with "строгие лимиты Docker", but only `bot` has `memory: 2G` (line 59). `local-bot-api` is unlimited and could OOM the host. No `cpus` limit on either.
  8. **PHASE1_GUIDE.md:28 vs docker-compose.yml:51 disk-path inconsistency**: PHASE1 says `D:\Media:/app/media`, compose says `E:\Media:/app/media`. Users following PHASE1 guide hit "disk not shared" error.
  9. **Schema vs docs: 10 tables vs claimed 9**: `app/db/schema.sql:150-154` defines `app_settings` (10th table), but `DOCS/PHASE2_GUIDE.md:12`, `scripts/check_phase2.py:58`, `DOCS/QUICKSTART_NEW_MACHINE.md:88`, `DOCS/1.md:152` all say "9 таблиц". `check_phase2.py:58` `expected = {users, channels, queues, posts, routing_rules, signatures, duplicate_hashes, tag_subscriptions, task_queue}` — `app_settings` not in expected set, so the script PASSES even though schema has the extra table.
  10. **README claims HTTP Basic Auth on web UI** (README:56 "Веб-интерфейс: `http://<локальный-IP>:8000` (HTTP Basic Auth)") and QUICKSTART:108 repeats it — but `app/main.py:133-163` has NO authentication on `/health`, `/health/bot-api`, `/health/scheduler`. Misleading security claim.
  11. **`scripts/fix_encoding.py:52` walks `root.rglob("*.py")`** with only `.git` and `node_modules` excluded (line 53) — does NOT exclude `.venv/`, `venv/`, `__pycache__/`, `data/`. If run from project root with a venv, it tries to "fix" encoding of stdlib/installed packages. Could corrupt site-packages.
  12. **`scripts/populate_tags.py` has NO Ctrl-C / SIGTERM handling**: `asyncio.run(populate(target))` (line 58) — on KeyboardInterrupt, `gelbooru_client.close()` (line 52) and `tags_db.close_tags_db()` (line 53) are NOT called. aiohttp ClientSession + aiosqlite Connection leak on every Ctrl-C.
- **MEDIUM-severity issues** (21, summarized):
  - conftest.py:13 sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` via `os.environ.setdefault` AT IMPORT TIME — too late, pytest already loaded plugins. Dead code. Real fix is `pyproject.toml:8 addopts = "-p no:cacheprovider"` + `DOCS/1.md:201` PowerShell env var.
  - conftest.py:15-17 OVERWRITES `TELEGRAM_API_ID/HASH/BOT_API_URL` unconditionally (not `setdefault`) — global side-effect leaks into other tests in same session.
  - conftest.py:31-32 uses `object.__setattr__(settings, "data_dir", tmp_path)` to mutate a frozen dataclass — fragile hack, will break if Settings becomes non-frozen or Python adds stricter frozen enforcement. Same hack in `check_phase2.py:94-95`.
  - conftest.py provides ZERO pytest fixtures (no `tmp_db`, `app`, `bot`, `client`, `tags_db`). Every test re-implements its own async runner via `from conftest import run_with_db`.
  - test_regression.py:12-51 `test_caption_extraction_no_caption_raw_attr` is mis-named — tests only `posts_repo.create_post/get_post` round-trip with `original_caption=None`. Does NOT import `app/bot/handlers/media.py` or test that caption extraction avoids `caption_raw` attr. The docstring claims to test aiogram 3.x compat but the test body doesn't touch aiogram.
  - test_regression.py:182-220 `test_extract_source_*` uses hand-rolled `FakeChat`/`FakeForwardOrigin`/`FakeMessage` classes mirroring aiogram's internal `MessageOriginChannel` shape. Brittle: aiogram rename of `forward_origin.chat.id` breaks the test.
  - test_regression.py:555-646 `test_add_rule_blocks_when_no_queues` + `test_add_rule_starts_flow_when_queues_exist` use `FakeCall.message.edited_text`/`edited_markup` as CLASS attrs — works only because each test redefines `FakeCall` locally; if extracted to a shared fixture, class-level state would leak between tests.
  - test_regression.py:223-249 `test_cmd_cancel_registered_before_fsm_handlers` checks `handler_names[0] == "cmd_cancel"` — registration-order test, brittle to any new high-priority message handler.
  - test_regression.py:1-6 header claims "3. check_phase2.py --routing изолирован от занятой БД" but NO test in the file invokes `check_phase2.py`. The script's isolation is unverified.
  - test_routing.py:5-10 covers `_looks_like_regex` but NO test guards the false-positive case flagged by Task 3 reviewer: pattern `@some.channel` (a username with `.`) triggers regex mode → `.` matches any char → `@someXchannel` matches. Footgun is unverified.
  - test_routing.py has NO test for tag-based routing (`route_post(source_type="gelbooru", tags=[...])` — the path `check_phase2.py:111` exercises). Only `check_phase2.py` covers it, not a real unit test.
  - test_repositories.py covers only users (CRUD + RBAC) + posts (lifecycle + duplicate + file_id fields) + task_queue. NO direct tests for `channels`, `queues` (covered as setup in other files), `routing` (covered in test_regression), `app_settings`, `duplicates`, `signatures`, `subscriptions` repos. Massive gap given 11 repo files exist.
  - test_repositories.py NO test for FK-cascade behavior — `delete_post` with children (duplicate_hashes, task_queue rows) would raise IntegrityError, no test catches this (per Task 2 reviewer).
  - test_settings.py:5-8 sets env vars at module level BEFORE importing `Settings` — but `Settings` is also imported at module level by other modules. If test_settings runs after a test that imported `settings` singleton with different env, the singleton is already constructed. Test isolation issue.
  - test_settings.py:14-22 NO test for env-var edge cases: `TELEGRAM_API_ID=abc` (raises ValueError at settings.py:28 `int(...)`), `MAX_SEND_ATTEMPTS=0`, `BOT_API_URL` with trailing slash.
  - docker-compose.yml:9-10 `TELEGRAM_API_ID=${TELEGRAM_API_ID}` — no `:-default` and no `:?required` fail-fast. If .env is empty, local-bot-api starts with `--api-id=` (empty) and crashes with cryptic error.
  - docker-compose.yml:35-36 `depends_on: - local-bot-api` — no `condition: service_healthy`. Bot starts before local-bot-api is healthy, retries `getMe` in a tight loop.
  - docker-compose.yml:18-19 `ports: - "8081:8081"` — exposes internal-only local-bot-api to host 0.0.0.0. README:72 claims "Веб-интерфейс не проброшен во внешнюю сеть" but 8000 IS exposed to 0.0.0.0. Port 8081 should be `expose:` not `ports:`.
  - docker-compose.yml:51 `E:\Media:/app/media` — hardcoded Windows path in compose file. Not portable to Linux/Mac. PHASE1:28 says `D:\Media` — inconsistent.
  - docker-compose.yml:56-61 NO `cpus`, NO `memswap_limit`, NO `pids_limit`, NO `read_only`, NO `security_opt: no-new-privileges`, NO `cap_drop: ALL`. "Strict Docker limits" claim is overstated.
  - Dockerfile:17 `ENV PYTHONUNBUFFERED=1 TZ=Europe/Moscow` vs `settings.py:58 timezone="Asia/Yekaterinburg"` — TZ inconsistency. daily_window calculations use Moscow (Dockerfile TZ) but settings.py comment says "TZ пользователя Asia/Yekaterinburg".
  - Dockerfile:21-22 stale comment "Полноценный вход (FastAPI + aiogram polling) появится на Фазе 2" — but Phase 2 is implemented (per docs); comment is outdated.
  - scripts/init_db.py:58 `from app.db import close_db` imported INSIDE main() — inconsistent with line 28 top-level `from app.db import bootstrap_admin, init_db`. Same import style issue in check_phase2.py:28-39 vs line 122 `import shutil` inside finally.
  - scripts/check_phase2.py:142-144 `except Exception as e: ... return 0` — `check_scheduler_jobs` swallows ALL errors and returns 0 (success). If app is down, script reports success. Misleading for CI.
  - scripts/check_phase1.py:104 `if "too big" in msg or "timeout" in msg.lower()` — fragile string matching. If Telegram changes error message, partial-success path won't trigger. Returns 0 on timeout (line 110) — CI treats timeout as pass.
- **LOW-severity issues** (19, summarized):
  - tests/__init__.py, tests/unit/__init__.py, tests/integration/__init__.py — all 0 bytes. `tests/integration/` has ONLY `__init__.py` (no integration tests). PROJECT_STRUCTURE.md doesn't note this.
  - tests/unit/test_routing.py:2 `from app.services.routing.service import _looks_like_regex, _matches` — uses full path because `routing/__init__.py` only re-exports `route_post`. Testing private functions is brittle (refactor breaks tests).
  - test_regression.py uses `from conftest import run_with_db` — non-standard pattern (importing conftest as regular module). Standard is `@pytest.fixture`. Works only because pytest adds rootdir to sys.path.
  - test_regression.py:1-6 header mentions "3. check_phase2.py --routing изолирован от занятой БД" — but the file doesn't test the script. Misleading docstring.
  - test_scheduler.py:79 `max_attempts = settings.max_send_attempts` — uses the singleton built at import time with `MAX_SEND_ATTEMPTS=3` default. Doesn't test override via env. Accidentally passes.
  - conftest.py:20 type annotation `Callable[[], Awaitable[None]]` — but body functions take no args and return None. OK but no `tmp_path` plumbing to the coro (coro relies on module-level `settings` mutation). Inconsistent fixture pattern.
  - scripts/init_db.py:40 `await bootstrap_admin(settings.admin_tg_id, None)` — passes `None` for username, so admin row has `username=NULL`. PHASE2_GUIDE claims admin username is logged — actually logged as `<нет>`.
  - scripts/populate_tags.py:57 `target = int(sys.argv[1])` — no argparse, no `--help`, no friendly error on non-integer input.
  - scripts/populate_tags.py:34-44 NO resume capability — Ctrl-C at page 200/500 restarts from 0. `upsert_tags` is idempotent (correct but wasteful).
  - scripts/check_phase1.py:41-46 `try/except ImportError` on `dotenv` — but `python-dotenv` is in requirements.txt. Dead code path.
  - scripts/check_phase2.py:25-29 same dead `try/except ImportError` on dotenv.
  - scripts/cleanup_downloads.py — 0-byte empty file. PROJECT_STRUCTURE.md:47 lists "scripts/ (автоочистка downloads)" as Phase 6 feature but file is empty stub.
  - docker/local-bot-api.md:15,24 uses `curl` (not `curl.exe`) — on Windows PowerShell, `curl` is alias for `Invoke-WebRequest`, breaks. PHASE1/QUICKSTART warn about this; local-bot-api.md doesn't.
  - docker-compose.yml:5 `container_name: tg-lba` — pinned for `docker logs tg-lba`. Bot container has no `container_name` — Docker generates `tg-autopost-manager-bot-1`. 1.md:88 hardcodes `run-...-bot-1` guess.
  - README.md:67 "API_Reference.md, Chrome_Extension_Guide.md, Deployment.md — будут добавлены на Фазах 3–4 (пока не существуют)" — honest about non-existence. Good.
  - DOCS/my_telegram_org_troubleshooting.md:13 "Short name | (оставить пустым)" — but my.telegram.org REQUIRES Short name. Potentially wrong advice.
  - DOCS/my_telegram_org_troubleshooting.md:78 `curl -s http://localhost:8081/health` — local-bot-api returns 403 on `/` (per compose healthcheck comment). `/health` endpoint likely doesn't exist on bot-api server. Wrong endpoint.
  - DOCS/PROJECT_STRUCTURE.md:74 "22 юнит-теста проходят" — actual count is ~50 tests (5 routing + 30 regression + 4 settings + 4 scheduler + 7 repositories). Stale.
  - DOCS/PULL_REQUEST.md:20 "Volume `D:\Media` требует File Sharing" — same D:/E: inconsistency.
  - DOCS/1.md heavily duplicates PHASE1_GUIDE.md and QUICKSTART_NEW_MACHINE.md content. Three docs cover the same "setup from scratch" workflow with overlapping steps.
  - .gitignore:13 `data/db/*.db` — but bot.db is at `data/bot.db` (top-level), NOT `data/db/`. The .gitignore pattern doesn't match the actual DB location. `*.db` at line 26 catches it, but `data/db/` directory is dead structure (only .gitkeep, never used).
  - schema.sql:80 `phash TEXT` column declared but NO migration in `database.py:93-117` `_run_migrations` adds it. So old DBs migrated to current schema are missing `phash`. Only fresh DBs have it. (Task 3 reviewer noted no service code computes phash — column is unused, so this is latent.)
- **Notable cross-cutting patterns**:
  1. **Test-the-implementation-not-the-contract**: test_scheduler.py re-implements the file_id-strategy boolean IN the test body (`bool(post["tg_file_id"]) and not post["was_processed"]`) instead of calling `app/scheduler/_send_one_post`. If scheduler logic changes, tests still pass. Same anti-pattern in test_regression.py:856-872 `format_queue_time_info` test — checks for substring presence, doesn't verify against actual queue state machine.
  2. **`run_with_db` async-in-sync pattern**: every async test wraps its body in `def body(): ... asyncio.run(_runner())`. No `@pytest.mark.asyncio`, no `pytest_asyncio` fixture, no `pytest-asyncio` in requirements. Works but non-standard; prevents use of `pytest_asyncio.fixture` for session-scoped async setup (e.g., a single tags_db init shared across tests).
  3. **Frozen-dataclass mutation hack**: `object.__setattr__(settings, ...)` appears in conftest.py:31-32, check_phase2.py:94-95. Documented as "frozen dataclass — via object.__setattr__" but no comment explaining WHY settings is frozen (it shouldn't be — it's runtime-mutated in tests). Either unfreeze Settings or add a proper test-settings factory.
  4. **Docs-vs-reality drift on frontend**: README:31 + PROJECT_STRUCTURE:32-34 describe `app/web/templates/pages/` and `app/web/static/{css,js,img}/` — none exist. `app/web/` directory itself is missing. README:33 describes Chrome Extension — `chrome-extension/` directory missing. README:18-19 describes pHash dedup + ffmpeg crop — `app/services/dedup/` and `app/services/media/` are 0-byte stubs (per Task 3 reviewer). Docs present Phase 5 stubs as current features.
  5. **9-vs-10 tables myth**: carried across PHASE2_GUIDE.md, check_phase2.py, QUICKSTART, 1.md. The 10th table `app_settings` (schema.sql:150) was added but docs/check script never updated. check_phase2.py:58 `expected = {...}` set doesn't include `app_settings`, so the script PASSES even though the table exists in schema — false positive on schema completeness.
  6. **No HTTP mocking library**: gelbooru/client.py has 200+ LOC of HTTP code with rate-limiting, caching, retry — entirely untested. No `aioresponses`/`responses`/`httpx.MockTransport` in requirements. Combined with no `pytest-asyncio`, async HTTP testing is structurally impossible without major refactoring.
  7. **Docker "strict limits" myth**: README:20 + PROJECT_STRUCTURE.md:57 claim "строгие лимиты Docker" for "слабое железо AMD Athlon X4 / 8GB RAM". Reality: only `bot` has `memory: 2G` (no CPU limit), `local-bot-api` has zero limits, no `memswap_limit`, no `pids_limit`, no `cpus`. Two containers on an 8GB host with one capped at 2G and the other uncapped can still OOM.
  8. **`cleanup_downloads.py` empty stub**: 0 bytes, but PROJECT_STRUCTURE.md:47 implies it's a feature ("автоочистка каждые 24 ч"). Test suite doesn't check script existence. Users following docs will run `python scripts/cleanup_downloads.py` and get nothing.
  9. **Settings singleton built at import time** (settings.py:89 `settings = Settings()`) — means importing `app.config.settings` triggers env read IMMEDIATELY. Test files that `import app.config.settings` at module level (test_settings.py:10) construct the singleton with whatever env is set at that moment. Test isolation depends on test file ordering. conftest.py:14-17 tries to paper over this by setting env vars at import time, but Python's import order is not guaranteed across test files.
  10. **D: vs E: disk inconsistency** (PHASE1_GUIDE:28 vs compose:51 + QUICKSTART:18) — likely a disk drive change on the dev machine that was updated in compose but not in PHASE1_GUIDE. Doc-vs-doc drift.

---
Task ID: 4
Agent: Explore (bot layer reviewer)
Task: Review all files in app/bot/

Work Log:
- Read /home/z/my-project/worklog.md to absorb prior findings (Task 2 repos: 33 issues incl. FK-cascade time-bombs; Task 3 services: 38 issues incl. scheduler:276 calling posts_repo.count_pending which doesn't exist)
- Listed app/bot/ tree: __init__.py, runtime.py, middleware/{__init__,access}.py, handlers/{__init__,start,media,menu,admin}.py, keyboards/{__init__,builder}.py — 11 files
- Read all 11 bot-layer files (menu.py is 1268 LOC — read in two chunks via awk)
- Read supporting contracts to verify each handler call: repositories/{users,queues,posts,channels,routing,app_settings,subscriptions}.py, db/{__init__,database}.py, db/schema.sql, services/archive.py, services/queue_time.py (referenced), scheduler/__init__.py, main.py, utils/local_bot_api.py, config/settings.py, requirements.txt
- Confirmed aiogram>=3.4 in requirements.txt (not installed in sandbox; analysis based on documented aiogram 3.x router propagation semantics)
- Traced router include order from app/bot/__init__.py:43-46: menu → media → admin → start
- Traced callback_data routes from keyboards/builder.py → handlers/menu.py callback handlers; identified 1 missing handler (menu:channel:<id> outside FSM state)
- Traced FSM state flow (AddStates) across menu.py: 14 states, 12 FSM message/callback handlers
- Cross-checked RBAC enforcement in every admin.py command and menu.py callback
- Verified FK constraints in schema.sql: queues→channels, posts→queues, routing_rules→queues, tag_subscriptions→users, duplicate_hashes→posts, task_queue→posts — all NO ACTION (RESTRICT), no ON DELETE CASCADE (confirms Task 2 finding → IntegrityError time-bombs in /remove_* commands)
- Compiled per-file findings with file:line citations and code snippets
- Appended Stage Summary

Stage Summary:
- 11 bot-layer files reviewed (~1850 LOC total; menu.py alone is 1268 LOC).
- ~42 distinct issues found. Severity breakdown: CRITICAL = 3, HIGH = 10, MEDIUM = 16, LOW = 13.
- CRITICAL findings:
  1. **Dispatcher router include order breaks ALL admin commands** (app/bot/__init__.py:43-46). media.router is included BEFORE admin.router. media's `receive_text` handler (handlers/media.py:306-312) has filter `F.text` (catches ANY text incl. commands) and `return`s early for "/"-prefixed text WITHOUT calling `message.answer`. In aiogram 3.x, once a handler matches and executes (even returning None), the dispatcher STOPS checking subsequent routers. Result: `/list_queues`, `/queue`, `/add_user`, `/add_channel`, `/add_queue`, `/remove_*`, `/help`, `/stats`, `/list_inbox`, `/list_channels`, `/list_users`, `/subscribe`, `/list_subscriptions`, `/reload_scheduler`, `/add_to_queue`, `/set_role` — ALL silently swallowed. Only `/cancel` (exact-match in menu), `/menu`, `/start` (Command filter in menu) reach their handlers. Bot is non-functional for admin CLI. Fix: move `admin.router` and `start.router` BEFORE `media.router`, OR change `receive_text` filter to `F.text & ~F.text.startswith("/")`.
  2. **No handler for `menu:channel:<id>` outside add_queue FSM state** (keyboards/builder.py:140-143 builds `menu:channel:<id>` buttons in channels_list; handlers/menu.py:547 `add_queue_pick_channel` requires `StateFilter(AddStates.add_queue_pick_channel)`). Admin clicking a channel in the channels list (not during add_queue flow) → no handler matches → infinite spinner on button (~10s Telegram client timeout).
  3. **`cb_post` fallback path missing `call.answer()`** (handlers/menu.py:376-381). When user clicks a post button in inbox/queue list (callback `menu:post:<id>` with no action suffix), `_render_post_card` edits the message but `call.answer()` is never called → button shows infinite spinner until Telegram client timeout. Common path (every post click from list).
- HIGH findings:
  - `cmd_remove_from_queue` (admin.py:74-88) has NO `user_role` RBAC check — moderators can pull any post out of any queue. Every other mutating command checks.
  - `cmd_cancel` (menu.py:77) uses `F.text == "/cancel"` instead of `Command("cancel")` — misses `/cancel@botname` (group form) and `/cancel extra` (with args).
  - `cmd_add_queue` (admin.py:322-332) parses `args = (command.args or "").split()` then `name = args[1]` — multi-word queue names silently truncated to first word (`/add_queue 5 Моя Очередь 60` → name="Моя", drops "Очередь").
  - All `**bold**` Markdown in 30+ messages across start.py/admin.py/menu.py sent WITHOUT `parse_mode` (no default on Bot/Dispatcher in aiogram 3.x). Users see literal `**` asterisks. Fix: set `default=DefaultBotProperties(parse_mode=ParseMode.HTML)` on Bot, or pass `parse_mode="Markdown"` per call.
  - `cmd_remove_channel` / `cmd_remove_queue` / `cmd_remove_user` (admin.py:303,347,216) — repo `remove_*` functions don't catch IntegrityError; with FK constraints (queues→channels, posts→queues, tag_subscriptions→users) and NO ON DELETE CASCADE (Task 2 finding), deleting a parent with children raises unhandled IntegrityError → bot logs error, user gets no response.
  - `cb_post` action=="toq" (menu.py:290) ignores `assign_queue` return value (bool) — always answers "✓ Пост #X → очередь" even if assignment silently failed (race: post deleted between get_post and assign_queue).
  - `cmd_remove_user` (admin.py:233) ignores `remove_user` return value — always says "✓ @{username} удалён."
  - `add_user_tg_id` (menu.py:1041-1066) extracts tg_id from `message.forward_origin`, but `fsm_non_text_in_state` (menu.py:122, filter `StateFilter(AddStates), ~F.text`) intercepts ALL non-text messages in ANY FSM state BEFORE `add_user_tg_id` (registered later). Forwarding a photo (no `text` attr) → caught by `fsm_non_text_in_state` → "Я жду текстовый ответ". The forward_origin extraction path is dead code for non-text forwards. The handler's own filter `@router.message(AddStates.add_user_tg_id)` (no F.text) is bypassed.
  - `_create_queue_from_state` (menu.py:747-793) accesses `data["channel_id"]`, `data["name"]`, `data["interval"]` without `.get()` — KeyError if state corrupted. No try/except → `call.answer()` never called → infinite spinner.
  - `cb_create_rule_from_post` (menu.py:215) calls `routing_repo.create_rule(...)` without try/except — DB error leaves `call.answer()` uncalled → infinite spinner.
  - All `cb_*` handlers in menu.py call `call.message.edit_text(...)` without try/except — if user deleted the bot's menu message (common — admin clears chat), `edit_text` raises `TelegramBadRequest("message to edit not found")`, propagates, no `call.answer()`, user sees infinite spinner.
  - No rate limiting on guest messages — `AccessMiddleware` (middleware/access.py:53) does `users_repo.get_user_by_tg_id` (DB SELECT) on EVERY message from unknown tg_id. A spammer can DoS the DB. Also every event from bootstrap admin triggers `bootstrap_admin` (SELECT + maybe UPDATE).
- MEDIUM findings (16): `_parse_dates` end_at off-by-59-seconds (23:59:00 not 23:59:59); `add_queue_daily_window` no hour/minute range validation ("99:00" stored, breaks scheduler later); no signature_template length validation (long sig + caption > 1024 chars → Telegram API error → post failed); no FSM state timeout (state persists indefinitely); no FSM state clearing on exception (handler raises mid-flow → state stuck); intermediate FSM steps (add_queue_name, add_queue_interval, add_queue_start_end, add_queue_daily_window, add_queue_signature, add_rule_pattern, add_rule_pick_queue, add_archive_channel, add_threshold, add_user_tg_id) don't re-check `user_role` — demoted admin can complete flow started while still admin; `cb_post`/`cb_user_actions`/`cb_rule_actions`/`cb_add_queue_for_channel`/`cb_create_rule_from_post`/`add_queue_pick_channel` all do `int(parts[N])` without try/except → malformed callback data raises ValueError, no `call.answer()`, infinite spinner; `cb_rule_actions` (menu.py:945 `F.data.startswith("menu:rule:")`) catches `menu:rule:stype:X` and `menu:rule:queue:X` clicks OUTSIDE the add_rule FSM state (because no state filter) → `int("stype")` / `int("queue")` crashes; `_album_archive_tasks` (media.py:39) leaks dict entries for tasks cancelled during `asyncio.sleep` (CancelledError branch returns before `pop`); `archive_post` called with `target_ch=0` for inbox posts (media.py:266) → archive tag `#to_0` meaningless; `kb.pick_channel_for_post` "Оставить во Входящих" button uses `noop` callback (builder.py:276) — doesn't navigate back to inbox; `cmd_list_queues` (admin.py:47) shows `@<channel_name>` when channel has no username (misleading); `cmd_add_channel` (admin.py:263) no validation of chat_id format (should start with -100); `AccessMiddleware` line 70 `text.split()[0]` raises IndexError on whitespace-only text; `receive_text` (media.py:307) silently ignores plain text in non-FSM state — no feedback to user; `cmd_subscribe` (admin.py:432) allows `site="danbooru"` but danbooru parser is 0-byte stub (Task 3 finding) → subscription never fires.
- LOW findings (13): `BufferedInputFile` dead import (media.py:23); `fsm_non_text_in_state` doesn't allow forwarded messages; `_render_post_card` (menu.py:393) truncates caption to 100 chars without ellipsis; `cb_post` is 150 LOC monolith handling 7 actions (should split); `_parse_dates` uses `%y` 2-digit year (ambiguous); `_create_queue_from_state` (menu.py:770) convoluted `getattr`/`hasattr` for answer method; duplicated `if user_role != "admin": ...` boilerplate ~20× across admin.py/menu.py (decorator candidate); duplicated `args = (command.args or "").split()` + `int(args[0])` + ValueError boilerplate ~6×; missing docstrings on 10 admin.py commands; mixed `post["status"]` vs `post.get("queue_id")` in builder.py; `cmd_set_role` (admin.py:179) accepts `username = args[0].lstrip("@")` — `/set_role admin` (forgot @) → username="admin"; `kb.main_menu` `adjust(2,1,2,2)` layout has 9 slots for 8 admin buttons (orphan slot); `AccessMiddleware` logs at INFO on EVERY event (log flooding for active bot).
- Notable cross-cutting patterns:
  1. **Router include order is THE critical bug** — `media.router` before `admin.router` + media's `F.text` catch-all that `return`s early for commands = 100% of admin/start commands silently dropped. This single design choice makes the bot's CLI non-functional. Only menu's exact-match/command filters for /cancel, /menu, /start survive.
  2. **No `parse_mode` anywhere** — neither on the Bot (no `default=DefaultBotProperties(...)` in `create_bot`, local_bot_api.py:40-52) nor on the Dispatcher nor per-message. All `**bold**` syntax in 30+ strings renders as literal asterisks. Adding `parse_mode="HTML"` later WITHOUT escaping user-controlled text (captions, usernames, channel names) in `f-string` builders (admin.py:47, 299; menu.py:393; builder.py post_card) would create HTML injection — latent risk.
  3. **Bare `int(parts[N])` without try/except** in 7 callback handlers — every callback data parser is a ValueError time-bomb on malformed/legacy callbacks. The pattern needs a helper `_safe_int(call.data, idx, on_error=call.answer(...))`.
  4. **Missing `call.answer()` on fallback/error paths** — 5 callback handlers have paths where `call.answer()` is never called (cb_post fallback, _create_queue_from_state error, cb_create_rule_from_post error, all edit_text on deleted message). Users see ~10s spinner.
  5. **No HTML escaping** today (safe because no parse_mode), but the codebase is one `parse_mode="HTML"` away from HTML injection via `q['name']`, `c['name']`, `post['original_caption']`, `user.username` — all user-controlled and all interpolated into message text via f-strings. Recommend: when adding parse_mode, ALSO add `html.escape(...)` calls or switch to MarkdownV2 with proper escaping.
  6. **FSM state hygiene**: state is cleared on cancel (cmd_cancel) and on successful completion, but NOT on exception (handler raises mid-flow → state stuck) and NOT on timeout (state persists forever in MemoryStorage until bot restart). Intermediate steps don't re-check role (only final step does for some flows).
  7. **Cross-layer bug confirmed from Task 3**: scheduler/__init__.py:276 calls `posts_repo.count_pending(queue["id"])` but `posts.py` has NO `count_pending` (it's in `queues.py:110`). This crashes the scheduler's `_check_queue_notifications` after EVERY successful send. The `except Exception` at scheduler:282 swallows it, so the bot limps on but notifications are silently broken. The bot layer doesn't cause this but is affected (admin's `cb_queue` at menu.py:158 also calls `queues_repo.count_pending(qid)` — that one IS correct, uses queues_repo).
  8. **No SQL injection in bot layer** — all DB calls go through repositories which use parameterized queries (per Task 2). The only raw-SQL is in media.py:66-67 (`SELECT * FROM posts WHERE media_group_id = ?`) — parameterized, fine. But it bypasses the `posts` repository abstraction (should use `posts_repo.find_album_posts` which exists at posts.py:112).
  9. **FK-cascade time-bombs hit the bot layer** (Task 2 finding manifests here): `cmd_remove_channel`, `cmd_remove_queue`, `cmd_remove_user` all call repo `remove_*` which raise IntegrityError on parent-with-children deletes. None of the handlers catch this. Admin who tries to delete a channel that has queues → silent failure, no user feedback.

---
Task ID: R5
Agent: Explore (scheduler/repos/services re-verification)
Task: Re-verify bugs after PRs #31, #32, #33 + find new bugs

Work Log:
- Read /home/z/my-project/worklog.md — reused prior reviewers' findings (Tasks 2/3/5/6/7) on FK-cascade gaps, scheduler count_pending bug, gelbooru client lifecycle leaks, cache cleanup task leak, queue_time counter issues, notifications dedup gap
- Re-read ALL 21 in-scope files (some shifted line numbers since prior reviews; code unchanged in most places)
- Traced each of the 44 previously-found bugs to its current code location; verified status (FIXED / PARTIALLY FIXED / NOT FIXED / REGRESSION)
- Grep-confirmed: count_pending now uses queues_repo (line 280); gelbooru_client.close() added to lifespan (line 124-128); cache.start_cleanup_task return captured (line 99) and cancelled on shutdown (line 130-131); close_bot() added (line 134-137); FK ON DELETE CASCADE added in schema.sql (lines 57, 89, 101, 119, 132, 146); notifications throttle added (lines 50-93); _source_tag still mislabels non-Telegram sources; _REGEX_HINTS unchanged
- Grep-traced new SQL: only `database._column_exists` (PRAGMA table_info) and `tags_db.get_tag_types` (placeholders pattern) — both safe, no new f-string SQL with user input
- Grep-traced close_bot usage: only in main.py:134 — no double-close elsewhere
- Grep-traced tests: NO tests added for any of the PR fixes (close_bot, gelbooru_client.close, cache cleanup cancel, count_pending, FK CASCADE, notifications throttle)
- Cross-checked migration path: `_run_migrations` (database.py:86-126) only does ADD COLUMN; no table recreation for FK changes — existing DBs retain RESTRICT FKs
- Verified `_cache_cleanup_task` cancellation in main.py:130-131 is fire-and-forget (NOT awaited) — CancelledError becomes "Task exception was never retrieved" warning
- Verified notifications `_last_notify` dict is SHARED between `notify_queue_almost_empty` and `notify_queue_finished` — almost_empty suppresses finished
- Verified `posts.queue_id` FK uses CASCADE (line 89) but spec required SET NULL — wrong fix
- Compiled per-bug re-verification report (see below)
- Appended Stage Summary

Stage Summary:
- FIXED: 5 (#1 scheduler count_pending, #8 _send_album_group queue param, #11 album notify_send_error, #33 cache cleanup task capture+cancel, #42 lifespan closes gelbooru_client/close_bot/cancels cache)
- PARTIALLY FIXED: 4 (#15 FK CASCADE added for fresh DBs only — migration gap for existing DBs + posts.queue_id CASCADE is wrong spec (should be SET NULL); #37 daily_window partial config now logged; #40 notifications throttle added but shared dict suppresses finished; #2 partial — see below)
- NOT FIXED: 35 (#2 APScheduler jobstores/misfire_grace_time, #3 permanent-error classification, #4 album mark_sent no transaction, #5 album >10 chunking, #6 BufferedInputFile OOM, #7 429 retry_after fragile, #9 album strip_caption/signature_template, #10 caption 1024 truncation, #12 _last_send dead code, #13 throttle blocks tick, #14 asyncio.get_event_loop().time() deprecated, #16 update_status state machine, #17 update_status sent resets attempts, #18 increment_attempt 0 for missing, #19 find_album_posts pending-only, #20 _row_to_post JSON swallow, #21 list_queues LEFT JOIN WHERE, #22 add_user silent reactivate, #23 users.username no UNIQUE, #24 app_settings.set str(value), #25 task_queue take_next no claimed_at, #26 task_queue.requeue no reset attempts, #27 _get_session race, #28 check_file_alive bypasses rate limit, #29 search_posts caches empty 60s on failure, #30 _request reads body before status, #31 safe_params dead code, #34 cache no max-size, #35 tags_db no busy_timeout/WAL, #36 handle_end_strategy limit=1000 + bool return, #38 _source_tag mislabels, #39 archive_album vs _send_album_group duplicate, #41 _REGEX_HINTS URL chars, #43 /health no auth, #44 _bot_polling no retry)
- REGRESSION: 2 (NEW-1 posts.queue_id CASCADE deletes posts instead of moving to inbox per spec; NEW-2 cache cleanup task cancellation not awaited → CancelledError never retrieved)
- NEW BUGS: 3 (NEW-3: notifications._last_notify shared dict — almost_empty suppresses finished notification within 5-min window; NEW-4: notify_queue_finished still spams every 5 min when queue persistently empty; NEW-5: cache cleanup task cancellation is fire-and-forget, not awaited, race with process exit + "Task exception was never retrieved" warning)
- Key new findings:
  - PR #31 fixed the 3 lifespan shutdown leaks (#42) but the cache cleanup cancellation is incomplete (fire-and-forget, not awaited).
  - PR #31 fixed the count_pending bug (#1) correctly — `queues_repo.count_pending` is used, `queues_repo` is imported at the top of scheduler/__init__.py.
  - PR #31 added FK CASCADE to schema.sql but used CASCADE for `posts.queue_id` which the spec said should be SET NULL — posts get deleted instead of moved to inbox when their queue is deleted. Also no migration path for existing DBs (RESTRICT stays until table recreation).
  - PR #33 added notifications throttle but used a SHARED `_last_notify` dict for both almost_empty and finished — almost_empty sets the timestamp, finished within 5 min is suppressed. Also finished still fires every 5 min for persistently-empty queues (not a one-shot transition).
  - PR #32 (LOW priority) — no visible changes in scope of files re-reviewed; possibly touched other files (handlers, etc.) outside this scope.
  - 0 tests added for any of the 3 PR fixes — all 5 "FIXED" statuses are based on code inspection only, not behavioral verification.

---
Task ID: R4
Agent: Explore (bot layer re-verification)
Task: Re-verify bot-layer bugs after PRs #31, #32, #33 + find new bugs

Work Log:
- Read /home/z/my-project/worklog.md to absorb prior findings (Task 2 repos: 33 issues; Task 3 services: 38 issues; Task 4 bot layer: 42 issues incl. explicit warning that adding parse_mode="HTML" without html.escape() would create HTML injection — cross-cutting finding #2 and #5).
- Re-read ALL 12 in-scope files (they changed across the 3 PRs): app/bot/{__init__,runtime}.py, app/bot/middleware/{__init__,access}.py, app/bot/handlers/{__init__,start,media,menu,admin}.py, app/bot/keyboards/{__init__,builder}.py, app/utils/local_bot_api.py. menu.py is now 1339 LOC (was 1268).
- Read PR commit messages and stat: PR #31 (a8022f2, HIGH): 9 files, 96+29 — claimed fixes for router order, cb_post fallback, cmd_cancel, FK cascade, scheduler count_pending, etc. PR #32 (24ece69, MEDIUM): 8 files, 145+62 — claimed _safe_int, cmd_remove_from_queue RBAC, parse_mode=HTML + <b> conversion, cb_channel_info, _parse_dates second=59, Dockerfile TZ, requirements dev deps. PR #33 (1206579, LOW): 8 files, 21+18 — .gitignore, dead import, caption ellipsis, main_menu adjust, access.py text.split()[0], doc updates.
- Grep-confirmed `import html` / `html.escape` is NOT present anywhere in the repo (the parse_mode=HTML regression is unmitigated).
- Grep-confirmed 31 `call.message.edit_text(...)` calls in menu.py — none wrapped in try/except for TelegramBadRequest (Bug 5 not fixed).
- Verified each of the 11 `_safe_int(...)` call sites cover all callback int parsing; verified `_safe_int` does NOT catch IndexError on parts[N] bounds — still vulnerable to malformed callbacks missing required fields.
- Verified `_create_queue_from_state` (menu.py:838-841) still uses bare `data["channel_id"]`/`data["name"]`/`data["interval"]` — no .get(), no try/except (Bug 7 not fixed).
- Verified `cb_create_rule_from_post` (menu.py:234-247) still calls `routing_repo.create_rule(...)` without try/except (Bug 8 not fixed).
- Verified `cmd_add_to_queue` (admin.py:398-429) still has NO user_role RBAC check despite receiving the param (Bug 10 partially fixed — only cmd_remove_from_queue got the check).
- Verified `add_user_tg_id` (menu.py:1112-1136) forward-handling for non-text is still dead code: fsm_non_text_in_state (line 137, filter StateFilter(AddStates) + ~F.text) intercepts non-text messages in ANY AddStates substate BEFORE add_user_tg_id runs.
- Verified `_parse_dates` (menu.py:678) now has `second=59` (Bug 12 fixed).
- Verified `cmd_cancel` (menu.py:92) now uses `Command("cancel")` (Bug 6 fixed).
- Verified `cb_channel_info` (menu.py:445-485) is registered WITHOUT StateFilter and branches on state inside (Bug 3 fixed). Old add_queue_pick_channel handler removed (line 620 comment).
- Verified router include order in app/bot/__init__.py:45-48 is now `menu → admin → media → start` (Bug 1 fixed). Additionally media.py:305 filter is `F.text & ~F.text.startswith("/")` (double fix).
- Verified `cb_post` (menu.py:251-407) all 7 action branches + main fall-through call `call.answer()` (Bug 2 fixed).
- Verified `_safe_int` helper (menu.py:76-84) uses `asyncio.ensure_future(call.answer(...))` inside a sync function — works in async context but is a code-smell (should be async); minor NEW bug: doesn't catch IndexError if caller passes parts[N] where N is out of bounds.
- Verified `add_queue_daily_window` (menu.py:719) regex `(\d{1,2}):(\d{2})` still accepts `99:00` (Bug 13 not fixed).
- Verified `add_queue_signature` (menu.py:825-833) has no length validation (Bug 14 not fixed).
- Verified `cmd_add_queue` (admin.py:336-358) still uses `name = args[1]` — multi-word names still truncated (Bug 15 not fixed).
- Verified `cmd_add_channel` (admin.py:260-294) still has no -100 prefix validation (Bug 16 not fixed).
- Verified middleware/access.py:70 fix `if text and text.split()[0] in (...)` does NOT actually fix the whitespace-only IndexError: for `text = "   "`, `text` is truthy (non-empty str), `text.split()` returns `[]`, `[][0]` raises IndexError. PR #33's "fix" only re-arranged the conditional (Bug 17b not fixed).
- Verified middleware/access.py:43-47 still runs `bootstrap_admin` on EVERY event from admin (Bug 17c not fixed).
- Verified middleware/access.py:52-55 still does DB hit per event, no rate limit/cache (Bug 17a not fixed).
- Verified .env.example now contains `ADMIN_TG_ID=` (line 34) with documentation.
- EXHAUSTIVE parse_mode=HTML regression audit: enumerated all 30+ f-string sites interpolating user-controlled text (queue names `q['name']`/`qname`, channel names `c['name']`/`ch_name`/`channel['name']`/`existing['name']`, usernames `user.get('username')`/`@{user.get('username')}`, captions `post['original_caption']`, routing patterns `rule['source_pattern']`/`source_id`/`data['source_pattern']`) into message bodies sent via `message.answer()`/`call.message.edit_text()`. ZERO of these wrap their interpolations in `html.escape(...)`. Button-label text (keyboards/builder.py) is NOT HTML-parsed by Telegram so keyboard labels are safe. Confirmed CRITICAL REGRESSION.
- Verified the intentional `<b>...</b>` formatting now renders correctly (parse_mode=HTML is the global default via DefaultBotProperties in local_bot_api.py:58).
- Compiled per-bug re-verification table with status + code citations.
- Appended Stage Summary.

Stage Summary:
- FIXED = 7 (Bugs 1, 2, 3, 4, 6, 12; plus cmd_remove_from_queue RBAC half of Bug 10; plus .env.example ADMIN_TG_ID)
- PARTIALLY FIXED = 2 (Bug 10 — only cmd_remove_from_queue got RBAC, cmd_add_to_queue still missing; Bug 17b — PR #33 only re-arranged the conditional, IndexError still possible on whitespace-only text)
- NOT FIXED = 9 (Bugs 5, 7, 8, 9, 11, 13, 14, 15, 16, 17a, 17c — see detailed table)
- REGRESSION = 1 CRITICAL (Bug 18 — parse_mode=HTML added globally, ZERO html.escape on user-controlled text in 30+ message bodies: admin-named queues/channels/usernames, forwarded post captions, and admin-entered routing patterns can inject `<b>`, `<a href>`, `<i>` etc. tags that Telegram will RENDER. Admin naming a queue `<b>test</b>` or `<a href="t.me">click</a>` will get rendered as bold/clickable. The previous reviewer's Task 4 cross-cutting finding #5 explicitly warned about this exact regression risk.)
- NEW minor bugs introduced:
  1. `_safe_int` helper (menu.py:76-84) uses `asyncio.ensure_future(call.answer(...))` from inside a sync function — works but is fragile (would raise RuntimeError if event loop not running, e.g., in tests). Should be made `async`.
  2. `_safe_int` does NOT catch IndexError — if caller does `_safe_int(parts[3], ...)` but `parts` has < 4 elements (malformed callback like `"rule:create:"`), the `parts[3]` access raises IndexError BEFORE _safe_int is entered — handler crashes, no call.answer → 10s spinner. All 11 call sites have this latent issue. (The keyboard always sends well-formed callbacks so this is low-priority in practice.)
- Key new finding: the PR #32 (M3) commit message itself notes "Все **text** заменены на <b>text</b>" but never mentions adding html.escape. The previous reviewer's explicit warning in worklog.md Task 4 ("Recommend: when adding parse_mode, ALSO add html.escape(...) calls") was ignored. This is the most important finding — HTML injection is now live and exploitable by any admin/moderator who can create queues/channels/users/rules or any user who can forward media with a malicious caption.

---
Task ID: R6
Agent: Explore (tests/scripts/docker/docs re-verification)
Task: Re-verify bugs after PRs #31, #32, #33 + find new bugs

Work Log:
- Read /home/z/my-project/worklog.md to absorb previous Task 6 (tests/scripts/docker/docs) findings: 26 bugs catalogued
- Listed /tmp/tg-autopost-manager tree; confirmed no NEW files added by PRs #31/#32/#33 (no new tests/, no new scripts/, no new docker/, no new docs/)
- Ran `git log --oneline -30` and `git diff --stat` for each PR to map exact file changes per PR:
  - PR #31 (HIGH): 9 files changed — app/bot/{__init__,handlers/admin,handlers/media,handlers/menu}.py, app/db/schema.sql, app/main.py, app/scheduler/__init__.py, app/services/{notifications,queue_time}.py — NONE in tests/scripts/docker/docs scope except via cross-cutting fix to check_phase2.py indirectly (no, check_phase2 wasn't touched by PR #31)
  - PR #32 (MEDIUM): 8 files — app/bot/handlers/{admin,menu,start}.py, app/services/notifications.py, app/utils/local_bot_api.py, docker/Dockerfile, requirements.txt, scripts/check_phase2.py
  - PR #33 (LOW): 8 files — .gitignore, DOCS/PHASE2_GUIDE.md, app/bot/handlers/media.py, app/bot/handlers/menu.py, app/bot/keyboards/builder.py, app/bot/middleware/access.py, scripts/check_phase2.py, scripts/fix_encoding.py
- Re-read ALL in-scope files: tests/{__init__,unit/__init__,integration/__init__}.py, conftest.py, tests/unit/{test_routing,test_regression,test_settings,test_scheduler,test_repositories}.py, scripts/{populate_tags,fix_encoding,check_phase2,cleanup_downloads,check_phase1,init_db}.py, docker-compose.yml, docker/{Dockerfile,local-bot-api.md}, pyproject.toml, requirements.txt, .env.example, .gitignore, README.md, DOCS/{PHASE1_GUIDE,PHASE2_GUIDE,QUICKSTART_NEW_MACHINE,PROJECT_STRUCTURE,PULL_REQUEST,my_telegram_org_troubleshooting,1}.md
- Verified actual `def test_` count via Grep: test_regression=39, test_routing=6, test_scheduler=4, test_settings=4, test_repositories=8 — TOTAL 61 (matches PR #33 PHASE2_GUIDE.md claim)
- Verified PR #31 actual fixes (out of my scope but cross-cutting): router include order (media moved AFTER admin), 6× ON DELETE CASCADE in schema.sql, scheduler/__init__.py:277 posts_repo.count_pending → queues_repo.count_pending (was HIGH bug from Task 3/5), album failure now calls notify_send_error, file_path.rsplit("/",1)→os.path.basename, notifications.py added throttle dict, main.py lifespan now closes gelbooru_client + cache cleanup task + close_bot
- Verified PR #32 actual fixes: Dockerfile TZ=Europe/Moscow→Asia/Yekaterinburg, requirements.txt added 3 commented dev deps (# pytest, # pytest-asyncio, # ruff), check_phase2.py:58-59 added app_settings to expected set
- Verified PR #33 actual fixes: .gitignore removed data/db/*.db dead pattern + added unified *.db/*.db-shm/*.db-wal, scripts/fix_encoding.py:54 exclusion list (.venv, venv, __pycache__, data), scripts/check_phase2.py:64 print "9 таблиц"→"10 таблиц", DOCS/PHASE2_GUIDE.md partial count updates (9→10 in 4 places, 58→61 in 5 places — but L368 "22 теста" NOT touched, L12 enumeration STILL lists 9 names without app_settings, L420/L473/L490 wrongly say "30 в test_regression.py" when actual is 39)
- Confirmed git diff shows ZERO changes to: tests/* (5 test files + conftest.py + 3 __init__.py), scripts/{populate_tags,init_db,cleanup_downloads,check_phase1}.py, docker-compose.yml, .env.example, .gitattributes, pyproject.toml, README.md, DOCS/{PHASE1_GUIDE,QUICKSTART_NEW_MACHINE,PROJECT_STRUCTURE,PULL_REQUEST,my_telegram_org_troubleshooting,1}.md
- Cross-checked .env.example:17 `TZ=Europe/Moscow` vs docker/Dockerfile:17 `TZ=Asia/Yekaterinburg` — NEW REGRESSION (PR #32 fixed Dockerfile but missed .env.example; compose env_file loads .env which overrides Dockerfile ENV)
- Cross-checked PHASE2_GUIDE.md:368 `22 теста` vs PHASE2_GUIDE.md:312 `61 passed` — INTERNAL INCONSISTENCY within same file (PR #33 missed L368)
- Cross-checked PHASE2_GUIDE.md:12 "10 таблиц: users, channels, ..., task_queue" — enumeration has only 9 names (no app_settings) while count says 10
- Cross-checked PHASE2_GUIDE.md:420/473/490 "61 passed (30 в test_regression.py)" — actual count is 39 (PR #33 guessed wrong number)
- Cross-checked PROJECT_STRUCTURE.md:74 STILL says "22 юнит-теста проходят, ruff: 0 ошибок" — NOT updated by PR #33 (commit message claim is FALSE; PROJECT_STRUCTURE.md was not in PR #33 diff)
- Cross-checked 1.md:90/152 STILL says "9 таблиц", 1.md:195 STILL says "22 passed" — NOT updated by PR #33
- Cross-checked pyproject.toml STILL has no [tool.ruff]/[tool.mypy]/[tool.black]/[tool.coverage] — but 7 doc references claim "ruff: 0 ошибок" / "All checks passed!" ( unverifiable claim)
- Cross-checked data/db/ directory STILL exists with only .gitkeep (dead structure — bot.db lives at data/bot.db not data/db/)
- Compiled per-bug re-verification report with status (FIXED / PARTIALLY FIXED / NOT FIXED / REGRESSION)
- Appended Stage Summary

Stage Summary:
- 26 previous bugs re-verified. Status: FIXED = 4 (bugs #7, #11, parts of #13/#18/#20/#22/#26), PARTIALLY FIXED = 6 (#5, #13, #18, #20, #22, #26), NOT FIXED = 16 (#1, #2, #3, #4, #6, #8, #9, #10, #12, #14, #15, #16, #17, #19, #21, #23, #24, #25 — slight count overflow because some bugs have multiple sub-parts each counted), REGRESSION = 0 (no bug from previous review made worse), NEW BUGS = 6
- Key new findings (NEW BUGS introduced by the 3 PRs):
  1. NEW REGRESSION (PR #32): `.env.example:17 TZ=Europe/Moscow` NOT updated when Dockerfile TZ was changed to `Asia/Yekaterinburg`. `docker-compose.yml:47-48 env_file: - .env` loads user's `.env`, overriding Dockerfile ENV → PR #32's TZ fix is undone in practice. Plus `.env.example` is now internally inconsistent with `Dockerfile`.
  2. NEW INCONSISTENCY (PR #33): `PHASE2_GUIDE.md:368` STILL says `tests/unit/ # 22 теста (routing, repositories, scheduler, settings)` while L312/L318/L420/L455/L473/L490 were updated to `61 passed`. Same file, two contradictory test counts (22 vs 61). PR #33 commit message claims "58 → 61 passed" but missed L368.
  3. NEW INCONSISTENCY (PR #33): `PHASE2_GUIDE.md:12` changed `9 таблиц` → `10 таблиц` but the enumeration STILL lists 9 names (no `app_settings` added to the list). Same line says "10" then lists 9. PR #33 commit message claims `10 таблиц (app_settings)` but the list wasn't actually updated.
  4. NEW INCONSISTENCY (PR #33): `PHASE2_GUIDE.md:420/473/490` changed `25 в test_regression.py` → `30 в test_regression.py` — but the ACTUAL count in test_regression.py is 39 (verified via `rg -c "^def test_" test_regression.py` = 39). PR #33's guess was wrong.
  5. NEW INCONSISTENCY (PR #32+PR #33): `scripts/check_phase2.py:64` now prints `[OK] Все 10 таблиц из ТЗ §5 присутствуют: <10 names sorted>` (with `app_settings`), but `DOCS/1.md:152-153` STILL shows expected output `[OK] Все 9 таблиц из ТЗ §5 присутствуют: channels, duplicate_hashes, posts, queues, routing_rules, signatures, tag_subscriptions, task_queue, users` (only 9 names, no app_settings, and says "9 таблиц"). The 1.md "expected output" block is now out of sync with the actual script output.
  6. NEW INCONSISTENCY (PR #33): `PROJECT_STRUCTURE.md:74` STILL says `22 юнит-теста проходят, ruff: 0 ошибок` while `PHASE2_GUIDE.md:312` now says `61 passed`. Same project, two different test counts (22 vs 61) in two docs. PR #33 commit message claims it fixed this but PROJECT_STRUCTURE.md was NOT in the PR #33 diff.
- Other ongoing gaps not fixed by any of the 3 PRs (still standing from Task 6 review): test_scheduler.py still re-implements strategy boolean in test body (L30, L57), conftest.py:13 PYTEST_DISABLE_PLUGIN_AUTOLOAD dead code (set too late), conftest.py:31-32 object.__setattr__ hack, test_regression.py:12-51 mis-named test, test_settings.py:5-8 module-level env mutation, no tests for gelbooru/{client,cache,tags_db}.py / notifications.py / archive send path / /health endpoints, scripts/populate_tags.py no argparse + no Ctrl-C handler, scripts/cleanup_downloads.py 0 bytes, scripts/init_db.py:40 still passes None username to bootstrap_admin (saves admin with username=NULL), scripts/check_phase2.py:142-144 except Exception: return 0 (added noqa comment but kept the bug), scripts/check_phase1.py:104 fragile "too big"/"timeout" string match returns 0 on timeout, Dockerfile still runs as root + no multi-stage + no HEALTHCHECK, docker-compose.yml still no resource limits on local-bot-api + no cpus on bot + no memswap/pids/cap_drop/security_opt/init/read_only + depends_on without condition: service_healthy + port 8081 exposed via ports: instead of expose: + no :-default or :?required on TELEGRAM_API_ID/HASH + redundant environment+env_file + hardcoded E:\Media Windows path, requirements.txt still no version pins (only aiogram>=3.4), pyproject.toml still no [tool.ruff]/[tool.mypy]/[tool.black]/[tool.coverage], PHASE1_GUIDE:28 D:\Media vs docker-compose:51 E:\Media, README still claims HTTP Basic Auth + Jinja2+Bootstrap frontend + Chrome Extension + pHash dedup + ffmpeg crop (none implemented, none marked as future), PROJECT_STRUCTURE.md still lists app/web/ + chrome-extension/ + app/api/ + app/worker/ + services/{caption,dedup,media}/ that don't exist or are 0-byte stubs, my_telegram_org_troubleshooting.md:13 "Short name (оставить пустым)" + L78 `/health` endpoint on local-bot-api (403 on /), data/db/ dead directory with .gitkeep (bot.db at data/bot.db)
- Notable cross-cutting observation: PR #33's commit message lists 8 fixes but only 6 actually landed as described. The "9 → 10 таблиц (app_settings)" fix updated the COUNT (in 4 of 5 places) but never added `app_settings` to the L12 enumeration. The "58 → 61 passed" fix updated 5 lines but missed L368 (still says 22). And the per-file count "30 в test_regression.py" is wrong (actual 39). This pattern — commit-message claims vs actual diff — suggests the PR was assembled without re-running the test suite or re-grepping the docs.

---
Task ID: phase3-stage1
Agent: Phase 3 implementer
Task: Фаза 3, Этап 1 — Worker Core (polling loop task_queue + скачивание медиа)

Work Log:
- Изучил кодовую базу Фаз 1-2: schema.sql (10 таблиц), repositories (task_queue с take_next/UPDATE...RETURNING, posts, duplicates, subscriptions), scheduler (APScheduler внутри uvicorn), main.py lifespan, gelbooru client, settings.py, conftest.py (run_with_db helper), pyproject.toml [tool.ruff]
- Создал feature-ветку phase3/stage1-worker от main
- Добавил 5 worker-настроек в app/config/settings.py: WORKER_POLL_INTERVAL_SECONDS (2.0), WORKER_MAX_ATTEMPTS (3), WORKER_STALE_TIMEOUT_MINUTES (30), WORKER_DOWNLOAD_TIMEOUT_SECONDS (120), WORKER_CLEANUP_INTERVAL_SECONDS (300) + валидация в Settings.validate()
- Создал app/services/media/downloader.py (новый): download_media() с Referer/UA по домену (gelbooru/pximg/...), Content-Type → file_type (photo/video/gif/document), лимит 2000 МБ, stream-запись чанками 8КБ, PermanentDownloadError (4xx/размер) vs DownloadError (429/5xx/timeout — ретраится), cleanup_old_downloads() для автоочистки
- Добавил posts.set_file_info() в app/repositories/posts.py — COALESCE-обновление file_path/file_type/file_size/mime_type после скачивания
- Реализовал app/worker/__init__.py (был 0 байт stub): класс Worker с _poll_loop (take_next → _process_task → mark_done/error/requeue) и _cleanup_loop (reset_stale_in_progress), обработка _handle_download (file_id-стратегия / уже есть file_path / скачивание / permanent vs retryable), singleton get_worker/start_worker/stop_worker, process_one_task() для тестов
- Интегрировал в app/main.py lifespan: start_worker() после scheduler, stop_worker() ПЕРВЫМ в shutdown (до scheduler/БД), новый endpoint /health/worker (running + счётчики + статистика task_queue), версия → 0.3.0-phase3-stage1
- Обновил .env.example блоком Фазы 3 с комментариями
- Создал tests/unit/test_worker.py: 20 тестов — downloader чистые функции (file_type/referer/extension), set_file_info, _handle_download (file_id/file_path/no-url/success/permanent/retryable+requeue/retry-exhausted), process/repost stubs, DB CHECK на task_type, stale cleanup (reset + keep recent), start/stop lifecycle, idempotent start
- ruff: мои файлы — 0 ошибок (исправил pre-existing UP041 asyncio.TimeoutError в main.py; baseline 34 → 33 errors, все remaining в файлах которые я не трогал)
- pytest: 179 passed (20 новых + 159 существующих), 0 failed

Stage Summary:
- Worker Core Фазы 3 реализован: polling loop task_queue (queued → in_progress → done/error), скачивание медиа (new → downloading → pending | failed), retry с max_attempts, requeue stale, интеграция в lifespan
- Архитектурное решение: worker внутри uvicorn-процесса (как scheduler), НЕ отдельный процесс — SQLite single-writer, task_queue рассчитана на одного потребителя, отдельный процесс добавил бы сложность без выгоды
- Стратегия file_id сохранена: посты с tg_file_id НЕ скачиваются (Фаза 2), worker только скачивает внешние source_url (gelbooru/pikabu/...)
- Endpoint /health/worker для наблюдаемости (Docker healthcheck / отладка)
- 20 новых тестов покрывают: чистые функции downloader, lifecycle постов (file_id/file_path/no-url/success/permanent/retryable/retry-exhausted), DB-констрейнты, stale cleanup, start/stop
- Готов фундамент для этапов 2-5: pHash (этап 2 будет вызываться из _handle_download после скачивания), Gelbooru подписки (этап 3 будет создавать posts status=new + enqueue download), внешние парсеры (этап 5 будут создавать posts + enqueue download)

---
Task ID: phase3-stage2
Agent: Phase 3 implementer
Task: Фаза 3, Этап 2 — pHash дедупликация (визуальный поиск дубликатов)

Work Log:
- Изучил PR #51 (этап 1, слит в main): worker с _handle_download, downloader.py, task_queue репозиторий
- Изучил duplicates.py: find_phash_near (Hamming distance), set_phash_atomic, _hamming_hex; обнаружил что find_phash_near исключает ТОЧНОЕ совпадение (hash_value != phash) — значит идентичные pHash не находятся
- Создал ветку phase3/stage2-phash-dedup от свежего main (commit 4f735fd)
- Добавил 3 phash-настройки в app/config/settings.py: PHASH_ENABLED (1), PHASH_MAX_DISTANCE (5), PHASH_CHECK_ON_RECEIVE (1) + валидация (max_distance 0..32)
- Создал app/services/dedup/phash.py (новый): is_image_file (расширение/mime), compute_phash_from_image (PIL→imagehash.phash→16 hex), compute_phash (из файла), compute_phash_from_bytes (из байтов для thumbnail), compute_and_store_phash (полный цикл: вычислить + set_phash_atomic), check_visual_duplicate (объединяет find_by_hash для distance=0 + find_phash_near для distance 1..max, исключая exclude_post_id)
- Ключевое архитектурное решение: check_visual_duplicate объединяет ДВА поиска — точный (find_by_hash, distance=0) и ближний (find_phash_near, distance 1..max). find_phash_near один не находит идентичные pHash (исключает hash_value=phash), поэтому нужен отдельный точный поиск
- Интегрировал pHash в worker._handle_download: после скачивания → compute_and_store_phash → check_visual_duplicate(exclude_post_id=post_id) → если дубликат найден → possible_duplicate (не pending), иначе pending. Обёрнуто в try/except — pHash-ошибка не блокирует отправку
- Интегрировал pHash в media.py handler (приём из ЛС): для photo — _pick_thumbnail (минимальный размер) → bot.download в BytesIO → compute_phash_from_bytes → set_phash_atomic → check_visual_duplicate → possible_duplicate. Финальное сообщение корректируется: если possible_duplicate, предупреждаем "требует проверки" вместо "позиция ~N"
- Обновил .env.example блоком Фазы 3 этап 2 с комментариями
- Создал tests/unit/test_phash.py: 29 тестов — is_image_file (extension/mime/negative), compute_phash_from_image (hex/disabled), compute_phash_from_bytes (real/empty/invalid), compute_phash (file/nonexistent/non-image/disabled/consistent/resize-invariant), compute_and_store_phash (success/non-image/disabled), check_visual_duplicate (exact/near/no-match/max_distance/excludes_self/disabled), worker (duplicate_detected/unique/disabled/video-no-phash), duplicates integration (atomic+find_near/idempotent)
- ruff: мои файлы — 0 ошибок (baseline 33 → 33, не добавил новых)
- pytest: 208 passed (29 новых + 179 существующих), 0 failed

Stage Summary:
- pHash дедупликация реализована на двух уровнях:
  1. Worker (после скачивания внешних файлов) — полный цикл: download → compute_phash → set_phash_atomic → check_visual_duplicate → possible_duplicate/pending
  2. Media handler (приём photo из ЛС) — thumbnail скачивается в BytesIO, pHash вычисляется, при дубликате → possible_duplicate вместо pending
- Дедупликация теперь ДВУХУРОВНЕВАЯ (ТЗ §2.4.3): точная (file_unique_id, Фаза 2) + визуальная (pHash, Фаза 3 этап 2)
- pHash устойчив к ресайзу/перекомпрессии — проверено тестом (круг 256→128, distance ≤ 10)
- find_phash_near ограничение (исключает точное совпадение) обойдено через объединение с find_by_hash в check_visual_duplicate
- Настройки: PHASH_ENABLED, PHASH_MAX_DISTANCE (≤5 = дубликат), PHASH_CHECK_ON_RECEIVE (можно выключить thumbnail-скачивание при приёме)
- Видео/GIF: pHash не применяется (нужен ffmpeg-кадр — Фаза 5). Для них работает только file_unique_id дедупликация
- 29 новых тестов покрывают: чистые функции, lifecycle (file/bytes/disabled/non-image), интеграцию с БД (atomic, find_near, idempotent), worker-интеграцию (duplicate detected/unique/disabled/video)
- Готов фундамент для этапа 3 (Gelbooru подписки): worker уже умеет скачивать + pHash-дедуп, подписки будут создавать posts status=new + enqueue download
