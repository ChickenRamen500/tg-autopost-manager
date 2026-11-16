#!/usr/bin/env python3
"""Одноразовая очистка папки медиа (data/media) по данным БД.

Файлы, скачанные/загруженные ботом до включения AUTO_DELETE_SENT_FILES,
накапливаются в data/media (в Docker: /app/media). Скрипт сопоставляет каждый
файл на диске с постами в БД (posts.file_path) и решает его судьбу:

  УДАЛЯТЬ:
    - «отправленные»   — все посты файла в статусе sent (уже в TG; архивация
                         выполняется при приёме, повторная отправка идёт по
                         tg_file_id или перекачкой по source_url);
    - «сироты»         — файл не ссылается ни на один пост в БД (остатки
                         удалённых постов / старых загрузок);
    - «failed»         — все посты файла в статусе failed (перекачаются по
                         source_url при requeue) — только с флагом --purge-failed.

  СОХРАНЯТЬ (никогда не удалять):
    - файлы локальных постов пользователя (source_type='local_file') — это
      собственные файлы, управляются вручную через файловый браузер;
    - файлы активных постов (new / downloading / pending /
      possible_duplicate / duplicate) — они ещё будут отправляться;
    - содержимое подпапки sent/ (пользователь перемещает файлы туда вручную,
      скрипт её не трогает — см. --include-sent-dir).

По умолчанию работает в режиме DRY-RUN (только показывает, что будет удалено).
Для реального удаления: python scripts/cleanup_media.py --apply

Запуск внутри контейнера (рекомендуется):
    docker compose exec bot python scripts/cleanup_media.py          # dry-run
    docker compose exec bot python scripts/cleanup_media.py --apply  # удалить

Запуск на хосте (Windows): путь к БД и папке можно указать явно:
    python scripts/cleanup_media.py --db C:\\tg-autopost-manager\\data\\bot.db ^
        --dir C:\\tg-autopost-manager\\data\\media --apply
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Статусы, при которых файл ещё может понадобиться
KEEP_STATUSES = {"new", "downloading", "pending", "possible_duplicate", "duplicate"}
# Статусы, при которых файл можно удалять (при отсутствии активных ссылок)
DELETEABLE_STATUSES = {"sent", "failed"}

MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif",
    ".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".mp3", ".ogg", ".opus",
}


def _norm(path: str | Path) -> str:
    """Нормализация пути для сопоставления: absolute + forward slashes + lower."""
    p = str(path).strip().replace("\\", "/")
    try:
        p = str(Path(p).resolve())
    except OSError:
        p = os.path.abspath(p)
    return p.replace("\\", "/").lower()


def _load_dotenv_silent() -> None:
    """Подхватывает .env из корня репозитория (для запуска на хосте вне Docker)."""
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:  # noqa: BLE001 — dotenv не установлен: работаем без него
        pass


def _fallback_db_path() -> Path:
    env = os.getenv("DATABASE_URL", "")
    if env.startswith("sqlite:///"):
        return Path(env[len("sqlite:///"):])
    return Path(__file__).resolve().parent.parent / "data" / "bot.db"


def _fallback_media_dir() -> Path:
    # Приоритет: MEDIA_DIR из окружения (задана в .env контейнера) → data/media
    env = os.getenv("MEDIA_DIR", "")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "media"


def load_posts(db_path: Path) -> list[dict]:
    """Читает posts (id, file_path, status, source_type, source_url) из sqlite."""
    if not db_path.exists():
        raise SystemExit(f"БД не найдена: {db_path} (укажите --db)")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, file_path, status, source_type, source_url "
            "FROM posts WHERE file_path IS NOT NULL AND file_path != ''"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def scan_files(media_dir: Path, include_sent_dir: bool) -> list[Path]:
    """Все медиа-файлы в media_dir (рекурсивно, без скрытых)."""
    if not media_dir.is_dir():
        raise SystemExit(f"Папка медиа не найдена: {media_dir} (укажите --dir)")
    files: list[Path] = []
    for f in media_dir.rglob("*"):
        if not f.is_file():
            continue
        if f.name.startswith("."):
            continue
        if not include_sent_dir and "sent" in f.relative_to(media_dir).parts:
            continue
        files.append(f)
    return files


def build_media_prefixes(posts: list[dict], media_dir: Path) -> list[str]:
    """Извлекает из путей постов префиксы корневой папки медиа.

    Посты хранят абсолютные пути контейнера, которые менялись между версиями
    (/app/media/... и /app/data/media/...), а на хосте пути другие. Префикс —
    родитель-предок, чьё имя совпадает с именем media_dir (обычно 'media').
    Сортировка по убыванию длины: longest-prefix wins при обрезке.
    """
    base = media_dir.name.lower()
    prefixes: set[str] = {"/app/media/", "/app/data/media/"}
    for p in posts:
        pp = _norm(p.get("file_path") or "")
        if not pp:
            continue
        parts = pp.split("/")
        for i in range(1, len(parts)):
            anc = "/".join(parts[:i])
            if anc == base or anc.endswith("/" + base):
                prefixes.add(anc + "/")
    return sorted(prefixes, key=len, reverse=True)


def match_refs(file: Path, media_dir: Path, posts: list[dict], prefixes: list[str]) -> list[dict]:
    """Посты, ссылающиеся на этот файл.

    Сопоставление устойчиво к разнице путей хост/контейнер:
    1. точное совпадение нормализованных путей (local_file и актуальные пути);
    2. совпадение относительного пути после обрезки известного префикса медиа
       (/app/media/img.jpg и /app/data/media/img.jpg → img.jpg == img.jpg);
    3. fallback: конец пути поста совпадает с путём файла относительно
       media_dir (только для файлов в подпапках — иначе sub/img.jpg
       ошибочно сматчится с img.jpg в корне).
    """
    file_key = _norm(file)
    try:
        rel = file.relative_to(media_dir).as_posix().lower()
    except ValueError:
        rel = file.name.lower()

    refs: list[dict] = []
    for p in posts:
        pp = _norm(p.get("file_path") or "")
        if not pp:
            continue
        if pp == file_key:
            refs.append(p)
            continue
        rel_post = None
        for pref in prefixes:
            if pp.startswith(pref):
                rel_post = pp[len(pref):]
                break
        if rel_post is not None and rel_post == rel:
            refs.append(p)
            continue
        if "/" in rel and pp.endswith("/" + rel):
            refs.append(p)
    return refs


def classify(refs: list[dict]) -> str:
    """Категория файла по его постам (см. docstring модуля)."""
    if not refs:
        return "orphan"
    statuses = {r["status"] for r in refs}
    if any(r.get("source_type") == "local_file" for r in refs):
        return "local"
    if statuses & KEEP_STATUSES:
        return "active"
    if "failed" in statuses:
        return "failed"
    if statuses <= DELETEABLE_STATUSES:
        return "sent"
    return "active"  # незнакомая комбинация — безопаснее сохранить


CATEGORY_TITLES = {
    "sent": "ОТПРАВЛЕННЫЕ (все посты sent) — будут удалены",
    "orphan": "СИРОТЫ (нет постов в БД) — будут удалены",
    "failed": "FAILED (все посты failed) — удалятся только с --purge-failed",
    "local": "ЛОКАЛЬНЫЕ ФАЙЛЫ (local_file) — сохраняются",
    "active": "АКТИВНЫЕ (new/downloading/pending/дубль) — сохраняются",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Очистка data/media по данным БД (по умолчанию dry-run).",
    )
    parser.add_argument("--apply", action="store_true", help="реально удалить файлы (по умолчанию только показать)")
    parser.add_argument("--db", type=Path, default=None, help="путь к bot.db (по умолчанию DATABASE_URL или data/bot.db)")
    parser.add_argument("--dir", type=Path, default=None, dest="media_dir", help="папка медиа (по умолчанию MEDIA_DIR или data/media)")
    parser.add_argument("--purge-failed", action="store_true", help="также удалить файлы, у которых все посты failed")
    parser.add_argument("--keep-orphans", action="store_true", help="не удалять файлы без ссылок в БД")
    parser.add_argument("--include-sent-dir", action="store_true", help="обрабатывать и подпапку sent/ (по умолчанию пропускается)")
    parser.add_argument("--limit-list", type=int, default=40, help="сколько путей показывать в каждой категории (по умолчанию 40)")
    args = parser.parse_args()

    _load_dotenv_silent()
    db_path = args.db or _fallback_db_path()
    media_dir = args.media_dir or _fallback_media_dir()

    print(f"БД:         {db_path}")
    print(f"Папка медиа: {media_dir}")
    mode = "РЕАЛЬНОЕ УДАЛЕНИЕ (--apply)" if args.apply else "DRY-RUN (ничего не удаляется, добавьте --apply)"
    print(f"Режим:      {mode}")
    print()

    posts = load_posts(db_path)
    files = scan_files(media_dir, args.include_sent_dir)
    print(f"Постов в БД с файлами: {len(posts)}; файлов на диске: {len(files)}")
    print()

    prefixes = build_media_prefixes(posts, media_dir)
    by_cat: dict[str, list[Path]] = {c: [] for c in CATEGORY_TITLES}
    skipped_ext: list[Path] = []
    for f in files:
        if f.suffix.lower() not in MEDIA_EXTS:
            skipped_ext.append(f)
            continue
        refs = match_refs(f, media_dir, posts, prefixes)
        by_cat[classify(refs)].append(f)

    to_delete: list[Path] = list(by_cat["sent"])
    if not args.keep_orphans:
        to_delete += list(by_cat["orphan"])
    if args.purge_failed:
        to_delete += list(by_cat["failed"])

    freed = sum(f.stat().st_size for f in to_delete if f.exists())

    for cat, title in CATEGORY_TITLES.items():
        items = by_cat[cat]
        size = sum(f.stat().st_size for f in items if f.exists())
        print(f"--- {title}: {len(items)} шт., {size / 1048576:.1f} МБ")
        for f in items[: args.limit_list]:
            try:
                print(f"    {f.relative_to(media_dir)}")
            except ValueError:
                print(f"    {f}")
        if len(items) > args.limit_list:
            print(f"    ... и ещё {len(items) - args.limit_list}")
    if skipped_ext:
        print(f"--- Не-медиа файлы (пропущены): {len(skipped_ext)} шт.")

    print()
    if not to_delete:
        print("Удалять нечего — папка чистая.")
        return 0

    print(f"ИТОГО к удалению: {len(to_delete)} файлов, {freed / 1048576:.1f} МБ")
    if not args.apply:
        print("\nЭто dry-run. Для удаления запустите с --apply:")
        print(f"    python {Path(sys.argv[0]).name} --apply")
        return 0

    deleted = 0
    errors = 0
    for f in to_delete:
        try:
            f.unlink()
            deleted += 1
        except OSError as e:
            errors += 1
            print(f"  ОШИБКА: {f}: {e}")
    print(f"\nУдалено: {deleted}; ошибок: {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
