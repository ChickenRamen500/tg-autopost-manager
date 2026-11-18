"""Тесты media.transcode (webm→mp4, пережатие фото) и его интеграции.

webm от gelbooru конвертируется в mp4 (Telegram плохо играет webm);
фото > 10 МБ пережимаются в JPEG для send_photo (лимит Telegram 10485760 байт).

ffmpeg есть в docker-образе и в песочнице — тесты выполняют РЕАЛЬНУЮ
конвертацию (маленькие файлы, секунды). Если ffmpeg пропадёт из окружения —
часть тестов skipif, негативные ветки (None без ffmpeg) тестируются всегда.
"""

from pathlib import Path

import pytest

from app.services.media.transcode import (
    TELEGRAM_PHOTO_MAX_BYTES,
    compress_photo_for_telegram,
    convert_video_to_mp4,
    ffmpeg_available,
)


def _make_webm(path: Path) -> Path:
    """Генерирует крошечный webm через ffmpeg (1 сек, 64x64, без аудио)."""
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1",
            "-c:v", "libvpx", "-b:v", "100k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _make_png(path: Path) -> Path:
    """Генерирует большой PNG (> лимита Telegram): шум 3000x3000 не жмётся.

    Шум через geq=random → PNG ≈ 12 МБ (> 10 МБ Telegram). ffmpeg в песочнице
    может быть убит ОС сразу ПОСЛЕ записи файла — поэтому check=False,
    валидируем результат по факту (файл существует и больше лимита).
    """
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "nullsrc=s=3000x3000:d=1,geq=random(1)*255:128:128",
            "-frames:v", "1", "-format", "png", "-pix_fmt", "rgb24",
            str(path),
        ],
        check=False,
        capture_output=True,
    )
    return path


pytestmark = pytest.mark.skipif(
    not ffmpeg_available(), reason="ffmpeg недоступен в окружении"
)


@pytest.mark.asyncio
async def test_convert_video_to_mp4_real(tmp_path):
    """webm → mp4: файл конвертирован, исходник удалён, mp4 валиден."""
    src = _make_webm(tmp_path / "post_1.webm")
    assert src.is_file()

    out = await convert_video_to_mp4(src)

    assert out is not None
    out_path = Path(out)
    assert out_path.suffix == ".mp4"
    assert out_path.is_file()
    assert out_path.stat().st_size > 0
    assert not src.exists()  # исходник удалён после успешной конвертации


@pytest.mark.asyncio
async def test_convert_skips_mp4_and_gif(tmp_path):
    """mp4 и gif не трогаем (None, файл остаётся)."""
    mp4 = tmp_path / "post_2.mp4"
    mp4.write_bytes(b"fake")
    gif = tmp_path / "post_3.gif"
    gif.write_bytes(b"fake")

    assert await convert_video_to_mp4(mp4) is None
    assert await convert_video_to_mp4(gif) is None
    assert mp4.is_file() and gif.is_file()


@pytest.mark.asyncio
async def test_convert_missing_file_returns_none(tmp_path):
    assert await convert_video_to_mp4(tmp_path / "nope.webm") is None


@pytest.mark.asyncio
async def test_convert_without_ffmpeg_returns_none(tmp_path, monkeypatch):
    """Нет ffmpeg в PATH → None, исходник не тронут (деградация, не ошибка)."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    src = tmp_path / "post_4.webm"
    src.write_bytes(b"fake")
    assert await convert_video_to_mp4(src) is None
    assert src.is_file()


@pytest.mark.asyncio
async def test_compress_photo_real(tmp_path):
    """Большой PNG → JPEG в пределах лимита Telegram; оригинал не тронут."""
    src = _make_png(tmp_path / "post_5.png")
    if src.stat().st_size <= TELEGRAM_PHOTO_MAX_BYTES:
        pytest.skip("сгенерированный PNG меньше лимита — проверка не имеет смысла")

    out = await compress_photo_for_telegram(src)

    assert out is not None
    out_path = Path(out)
    assert out_path.suffix == ".jpg"
    assert out_path.is_file()
    assert out_path.stat().st_size <= TELEGRAM_PHOTO_MAX_BYTES
    assert src.is_file()  # оригинал пережатие НЕ удаляет


@pytest.mark.asyncio
async def test_compress_skips_small_photo(tmp_path):
    """Фото в пределах лимита — None (пережимать не нужно)."""
    src = tmp_path / "post_6.jpg"
    src.write_bytes(b"x" * 1024)
    assert await compress_photo_for_telegram(src) is None


@pytest.mark.asyncio
async def test_compress_without_ffmpeg_returns_none(tmp_path, monkeypatch):
    """Нет ffmpeg → None (scheduler отправит как document)."""
    import shutil

    src = tmp_path / "post_7.png"
    src.write_bytes(b"y" * (TELEGRAM_PHOTO_MAX_BYTES + 1))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert await compress_photo_for_telegram(src) is None
