"""Детектор NSFW-заглушки Пикабу (Фаза 4, v0.4.61).

Проблема: для 18+ постов CDN пикабу (cs*.pikabu.ru) отдаёт КАРТИНКИ только
с cookies авторизованного пользователя, у которого включён показ 18+. Анонимный
запрос (а наш сервер качает mode A анонимно) получает вместо контента
сгенерированную заглушку — однокадровый GIF с текстом «NSFW / Изображение может
быть небезопасным для просмотра и доступно только АВТОРИЗОВАННЫМ пользователям»,
отрисованный в размерах реальной картинки. URL при этом выглядит как обычное
медиа (https://cs2.pikabu.ru/s/2026/11/19/11/<hash>.gif), поэтому молча
скачивается и уходит в очередь/канал как «успешный» пост.

Видео пикабу CDN не защищает — заглушки встречаются только среди photo/gif.

Как выглядит заглушка (замеры по 3 реальным образцам, 2026-11-20):
  600x576   (278 КБ):  белых 98.1%, красных 0.32%
  1640x1920 (2.8 МБ):  белых 99.8%, красных 0.03%
  700x1393  (631 КБ):  белых 99.3%, красных 0.09%
То есть: GIF, РОВНО один кадр (анимация = реальный контент), фон почти
полностью белый, есть красные пиксели надписи «NSFW» (r>140, g<90, b<90).

Правило детектора (все условия обязательны):
  1. файл декодируется PIL как GIF;
  2. n_frames == 1 (анимированные GIF — реальный контент, не трогаем);
  3. доля почти-белых пикселей (min(R,G,B) > 225) > 90%;
  4. доля красных пикселей (R>140, G<90, B<90) > 0.005%.

Условия 3-4 проверяются на уменьшенной копии (максимум 800px по большей
стороне) — быстро и достаточно: белая заливка сохраняется, а надпись NSFW
даже на копии 800px даёт >0.02% красных пикселей (замер на образце 1640x1920).

Ложные срабатывания практически исключены: правило применяется ТОЛЬКО к
постам с source_type='pikabu' и file_type в ('photo', 'gif') (вызывает
worker / quick_add_upload). Статичная GIF-картинка пикабу, белая на >90%
и с красными пикселями — реальный контент, который пострадал бы от правила,
— на практике не встречается, а цена ошибки (пост → failed с понятным
сообщением, вместо заглушки в канале) несопоставимо ниже.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("media.nsfw_stub")

# Пороги (см. докстринг — замеры по реальным образцам заглушки).
_WHITE_RATIO_MIN = 0.90
_RED_RATIO_MIN = 0.00005  # 0.005%
_ANALYSIS_MAX_DIM = 800


def is_pikabu_nsfw_stub(file_path: str | Path) -> bool:
    """True, если файл — однокадровый белый GIF с красной надписью NSFW.

    Любая ошибка (нет файла, не картинка, битый файл) → False: детектор
    не должен ломать загрузку обычного контента.
    """
    path = Path(file_path)
    if not path.is_file():
        return False
    try:
        from PIL import Image

        with Image.open(path) as img:
            # Анимированный GIF — реальный контент, не заглушка.
            frames = getattr(img, "n_frames", 1)
            if frames and frames > 1:
                return False
            is_gif = (img.format or "").upper() == "GIF"
            img = img.convert("RGB")
            if max(img.size) > _ANALYSIS_MAX_DIM:
                img.thumbnail((_ANALYSIS_MAX_DIM, _ANALYSIS_MAX_DIM), Image.BILINEAR)
            pixels = list(img.getdata())
    except Exception as e:  # noqa: BLE001 — любая ошибка декодирования → не заглушка
        log.debug("nsfw_stub: %s не декодируется (%s) — пропускаю проверку", path, e)
        return False

    if not is_gif or not pixels:
        return False

    total = len(pixels)
    white = sum(1 for r, g, b in pixels if min(r, g, b) > 225)
    red = sum(1 for r, g, b in pixels if r > 140 and g < 90 and b < 90)
    white_ratio = white / total
    red_ratio = red / total
    verdict = white_ratio > _WHITE_RATIO_MIN and red_ratio > _RED_RATIO_MIN
    if verdict:
        log.warning(
            "nsfw_stub: %s — похоже на заглушку пикабу (white=%.1f%%, red=%.3f%%)",
            path.name, white_ratio * 100, red_ratio * 100,
        )
    return verdict
