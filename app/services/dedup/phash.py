"""Визуальная дедупликация по pHash (ТЗ §2.4.3, Фаза 3 этап 2).

Двухуровневая дедупликация (ТЗ §2.4.3):
  1. Точная — по file_unique_id (Telegram) / source_id. Реализована в posts.py
     (find_by_file_unique_id) и в media.py (ON CONFLICT DO NOTHING).
  2. Визуальная — по pHash (perceptual hash). Этот модуль.

pHash (perceptual hash) — 64-битный хеш изображения, устойчивый к:
  - ресайзу (тот же объект другого размера → тот же хеш);
  - перекомпрессии (JPEG→PNG→JPEG с потерями);
  - небольшим правкам (цветокоррекция, водяной знак).

Расстояние Хэмминга между двумя pHash:
  - 0  → идентичные изображения;
  - ≤5 → «похожие» (рекомендация imagehash для дедупликации);
  - ≤10 → «возможно похожие» (много false positives);
  - >10 → разные изображения.

Этот модуль:
  - compute_phash(file_path | bytes) → hex-строка (16 символов) или None;
  - compute_phash_from_image(PIL.Image) → hex-строка;
  - check_visual_duplicate(phash) → список найденных дубликатов (через duplicates.find_phash_near).

None возвращается если:
  - файл не является изображением (видео, аудио, документ);
  - файл повреждён / не читается PIL;
  - pHash отключён в настройках (phash_enabled=False).

Видео и GIF: pHash вычисляется ТОЛЬКО для изображений (photo). Для видео нужен
извлечение кадра через ffmpeg — это Фаза 5 (media-конвейер). GIF анимированный —
берём первый кадр (PIL это умеет).
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

from app.config.settings import settings

log = logging.getLogger("dedup.phash")

# Расширения файлов, которые PIL может открыть как изображения
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".tif"}


def is_image_file(file_path: str | Path, mime_type: str | None = None) -> bool:
    """Определяет, является ли файл изображением (по расширению или mime).

    Используется worker'ом чтобы решить, вычислять ли pHash после скачивания.
    """
    path_str = str(file_path).lower()
    ext = ""
    if "." in path_str:
        ext = "." + path_str.rsplit(".", 1)[-1]
    if ext in _IMAGE_EXTENSIONS:
        return True
    if mime_type:
        mt = mime_type.split(";")[0].strip().lower()
        if mt.startswith("image/"):
            return True
    return False


def compute_phash_from_image(img: Any) -> str | None:
    """Вычисляет pHash из PIL.Image. Возвращает hex-строку (16 символов) или None.

    imagehash.phash по умолчанию: hash_size=8 → 64 бита → 16 hex символов.
    Это совпадает с ожиданиями duplicates._hamming_hex (hex чётной длины).

    Принимает уже открытый PIL.Image (для тестов и для thumbnail из ЛС).
    """
    if not settings.phash_enabled:
        return None
    try:
        import imagehash

        h = imagehash.phash(img)
        return str(h)
    except Exception as e:  # noqa: BLE001 — PIL может упасть на битом файле
        log.warning("compute_phash_from_image: не удалось вычислить pHash: %s", e)
        return None


def compute_phash(file_path: str | Path) -> str | None:
    """Вычисляет pHash изображения по пути к файлу. Возвращает hex или None.

    None если:
      - pHash отключён (phash_enabled=False);
      - файл не существует / не читается;
      - файл не является изображением (по расширению);
      - PIL не смог открыть/декодировать файл.
    """
    if not settings.phash_enabled:
        return None
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        log.warning("compute_phash: файл не существует: %s", file_path)
        return None
    if not is_image_file(str(path)):
        log.debug("compute_phash: не изображение (пропуск): %s", file_path)
        return None
    try:
        from PIL import Image

        with Image.open(path) as img:
            return compute_phash_from_image(img)
    except Exception as e:  # noqa: BLE001 — PIL ошибки на битых файлах
        log.warning("compute_phash: не удалось открыть %s: %s", file_path, e)
        return None


def compute_phash_from_bytes(data: bytes) -> str | None:
    """Вычисляет pHash из байтов (для thumbnail из ЛС, без сохранения на диск).

    None если pHash отключён или данные не читаются PIL.
    """
    if not settings.phash_enabled:
        return None
    if not data:
        return None
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        return compute_phash_from_image(img)
    except Exception as e:  # noqa: BLE001
        log.warning("compute_phash_from_bytes: не удалось декодировать: %s", e)
        return None


async def check_visual_duplicate(phash: str, exclude_post_id: int | None = None) -> list[dict[str, Any]]:
    """Ищет визуальные дубликаты по pHash (точные + ближние).

    Объединяет два поиска:
      1. duplicates.find_by_hash(phash, 'phash') — ТОЧНОЕ совпадение (distance=0).
         find_phash_near исключает hash_value=phash, поэтому идентичные pHash
         не нашлись бы — нужен отдельный запрос.
      2. duplicates.find_phash_near(phash, max_distance) — ближние (distance 1..max).

    exclude_post_id: ID текущего поста (чтобы не найти самого себя). Передаётся
      из worker/media handler после set_phash_atomic.

    Возвращает список дубликатов (post_id, hash_value, distance), отсортированный
    по возрастанию distance. Пустой список = дубликатов нет.
    """
    if not settings.phash_enabled:
        return []
    from app.repositories import duplicates as dup_repo

    results: dict[int, dict[str, Any]] = {}

    # 1. Точные совпадения (distance=0) — find_by_hash
    exact = await dup_repo.find_by_hash(phash, "phash")
    for d in exact:
        pid = d["post_id"]
        if exclude_post_id is not None and pid == exclude_post_id:
            continue
        results[pid] = {"post_id": pid, "hash_value": phash, "distance": 0}

    # 2. Ближние (distance 1..max) — find_phash_near
    near = await dup_repo.find_phash_near(phash, max_distance=settings.phash_max_distance)
    for d in near:
        pid = d["post_id"]
        if exclude_post_id is not None and pid == exclude_post_id:
            continue
        # Если уже в results с distance=0 — не перезаписываем (точное важнее)
        if pid not in results:
            results[pid] = d

    return sorted(results.values(), key=lambda m: m["distance"])


async def compute_and_store_phash(post_id: int, file_path: str, file_type: str | None = None) -> str | None:
    """Вычисляет pHash файла, атомарно сохраняет в posts+duplicate_hashes, возвращает hex.

    Полный цикл для worker'а после скачивания:
      1. compute_phash(file_path) — если None (не изображение / disabled) → return None;
      2. duplicates.set_phash_atomic(post_id, phash) — posts.phash + duplicate_hashes;
      3. return phash.

    file_type — необязательный, для логирования.
    """
    if not settings.phash_enabled:
        return None
    phash = compute_phash(file_path)
    if phash is None:
        log.debug("compute_and_store_phash: pHash не вычислен для поста #%s (type=%s)", post_id, file_type)
        return None
    from app.repositories import duplicates as dup_repo

    await dup_repo.set_phash_atomic(post_id, phash)
    log.info("pHash поста #%s: %s (type=%s)", post_id, phash, file_type)
    return phash
