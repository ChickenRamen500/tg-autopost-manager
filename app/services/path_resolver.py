"""Переводчик путей: Windows-путь → путь внутри контейнера.

Пользователь знает только Windows-пути своего ПК/сервера
(N:\\media_saver_bot\\saved_files), а бот работает с путями контейнера
(/host_mnt/D/media_saver_bot/saved_files, /mnt/saved_files, ...). Буквы дисков
не совпадают (сетевые диски M:/N: на ПК пользователя могут указывать на C:/D:
сервера), поэтому путь ищется ПО ХВОСТУ: диск, буква и способ монтирования
не имеют значения.

Алгоритм (resolve_windows_path):
1. Из Windows-пути извлекаются компоненты без буквы диска:
   N:\\media_saver_bot\\saved_files → ['media_saver_bot', 'saved_files'].
2. Строится список корней для поиска:
   - настроенные корни файлового браузера (media_dir + EXTRA_MEDIA_DIRS +
     web_local_roots);
   - авто-обнаруженные точки монтирования: top-level директории «/», у которых
     os.path.ismount() == True (bind-маунты Docker Desktop: /host_mnt/C и т.п.);
   - well-known директории: /mnt, /media, /host_mnt, /host, /data, /srv,
     /run/desktop/mnt/host + их подкаталоги первого уровня.
3. Быстрый путь: для каждого корня проверить существование <root>/<components>.
4. Если не нашлось — ограниченный BFS по каждому корню (лимиты глубины и числа
   посещённых директорий): совпадение = хвост относительного пути корня
   совпадает с компонентами Windows-пути (регистронезависимо — Windows
   нечувствителен к регистру).

Все функции чисто синхронные; async-обёртка resolve_windows_path выполняет
поиск в отдельном потоке (asyncio.to_thread), чтобы не блокировать event loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("path_resolver")

# Well-known директории-кандидаты на «корень примонтированных дисков»
WELL_KNOWN_ROOTS = (
    "/mnt",
    "/media",
    "/host_mnt",           # Docker Desktop (Windows/macOS): bind-маунты хоста
    "/host",
    "/data",
    "/srv",
    "/volumes",
    "/run/desktop/mnt/host",  # Docker Desktop WSL2 backend
)

# Виртуальные/системные директории, которые не нужно обходить
SKIP_DIRS = {"/proc", "/sys", "/dev", "/boot", "/etc", "/usr", "/lib", "/lib64",
             "/bin", "/sbin", "/var/lib/docker", "/run/secrets"}

# Лимиты BFS — поиск не должен вешать event loop на целых дисках
MAX_DEPTH = 12           # глубина относительно корня поиска
MAX_VISITS_PER_ROOT = 15000  # максимум посещённых директорий на корень

# Регексп drive letter: C:\ → пусто; UNC \\server\share → обрабатываем как parts
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def split_windows_path(win_path: str) -> list[str] | None:
    """Windows-путь → компоненты без диска.

    N:\\media_saver_bot\\saved_files → ['media_saver_bot', 'saved_files']
    \\\\NAS\\share\\folder           → ['nas', 'share', 'folder']
    /mnt/saved_files                 → ['mnt', 'saved_files']

    None — из пути не извлекается ни одного компонента.
    """
    s = (win_path or "").strip().replace("\\", "/")
    # UNC: //server/share → убираем ведущие слеши, server становится компонентом
    s = re.sub(r"^/+", "", s)
    s = _DRIVE_RE.sub("", s)
    parts = [p for p in s.split("/") if p and p not in (".", "..")]
    return parts or None


def discover_search_roots(configured: list[Path] | None = None) -> list[Path]:
    """Строит список корней для поиска папки.

    Источники: настроенные корни браузера, top-level точки монтирования «/»,
    well-known директории и их подкаталоги первого уровня.
    """
    roots: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        try:
            rp = str(p.resolve())
        except OSError:
            rp = str(p)
        if rp in seen or rp in SKIP_DIRS or rp == "/":
            return
        if not p.is_dir():
            return
        seen.add(rp)
        roots.append(p)

    for c in configured or []:
        _add(Path(c))

    # Top-level точки монтирования (/host_mnt/D, /mnt/c, ...)
    try:
        for entry in os.scandir("/"):
            if entry.name.startswith("."):
                continue
            p = Path(entry.path)
            if entry.is_dir(follow_symlinks=False) and os.path.ismount(entry.path):
                _add(p)
    except OSError:
        pass

    # Well-known корни + их подкаталоги первого уровня (обычно там диски:
    # /host_mnt/C, /host_mnt/D, /mnt/wsl, /mnt/c ...)
    for wk in WELL_KNOWN_ROOTS:
        wkp = Path(wk)
        _add(wkp)
        try:
            for entry in os.scandir(wk):
                if entry.is_dir(follow_symlinks=False):
                    _add(Path(entry.path))
        except OSError:
            pass

    return roots


def _is_virtual(p: Path) -> bool:
    """Системные директории, которые не нужно обходить."""
    try:
        rp = str(p.resolve())
    except OSError:
        return True
    for skip in SKIP_DIRS:
        if rp == skip or rp.startswith(skip + "/"):
            return True
    return False


def search_tree_for_tail(root: Path, parts: list[str]) -> list[Path]:
    """BFS от root: ищет директории, чей путь от корня заканчивается на parts.

    Регистронезависимо, с лимитами MAX_DEPTH/MAX_VISITS_PER_ROOT.
    """
    target = [p.lower() for p in parts]
    n = len(target)
    matches: list[Path] = []
    visited = 0
    from collections import deque

    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    while queue and visited < MAX_VISITS_PER_ROOT:
        node, depth = queue.popleft()
        visited += 1
        try:
            rel_parts = [x.lower() for x in node.relative_to(root).parts]
        except ValueError:
            rel_parts = []
        # root сам может быть искомой папкой (например, root = saved_files)
        if rel_parts and len(rel_parts) >= n and rel_parts[-n:] == target:
            matches.append(node)
            continue  # внутрь совпавшей папки не углубляемся
        if depth >= MAX_DEPTH:
            continue
        try:
            children = list(os.scandir(node))
        except OSError:
            continue
        for ch in children:
            if ch.name.startswith("."):
                continue
            if not ch.is_dir(follow_symlinks=False):
                continue
            queue.append((Path(ch.path), depth + 1))
    if visited >= MAX_VISITS_PER_ROOT:
        log.warning("path_resolver: лимит обхода %s исчерпан (%d визитов)", root, visited)
    return matches


def resolve_windows_path_sync(
    win_path: str, configured_roots: list[Path] | None = None
) -> list[dict[str, str]]:
    """Синхронная версия: Windows-путь → список совпадений внутри контейнера.

    Возвращает [{"container_path": ..., "root": ...}] — уникальные,
    в порядке: точные сборки (быстрый путь) → результаты BFS.
    """
    parts = split_windows_path(win_path)
    if not parts:
        return []

    roots = discover_search_roots(configured_roots)
    if not roots:
        return []

    matches: dict[str, str] = {}  # container_path → root label

    # 1. Быстрый путь: <root>/<parts> существует
    for r in roots:
        candidate = r.joinpath(*parts)
        if candidate.is_dir():
            try:
                key = str(candidate.resolve())
            except OSError:
                key = str(candidate)
            if key not in matches:
                matches[key] = str(r)

    # 2. BFS по хвосту пути (если быстрый путь не нашёл или нужны все варианты)
    if not matches:
        for r in roots:
            if _is_virtual(r):
                continue
            for m in search_tree_for_tail(r, parts):
                try:
                    key = str(m.resolve())
                except OSError:
                    key = str(m)
                if key not in matches:
                    matches[key] = str(r)

    return [{"container_path": k, "root": v} for k, v in matches.items()]


async def resolve_windows_path(
    win_path: str, configured_roots: list[Path] | None = None
) -> list[dict[str, str]]:
    """Async-обёртка: поиск выполняется в отдельном потоке (не блокирует loop)."""
    return await asyncio.to_thread(resolve_windows_path_sync, win_path, configured_roots)
