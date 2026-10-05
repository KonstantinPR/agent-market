"""Кэш миниатюр фотографий.

Фотографии в библиотеке — 2250x3000, медиана 4 МБ. Вставлять их в PDF «как есть»
нельзя: ``reportlab`` масштабирует картинку только при отрисовке, а в поток
встраивает исходный JPEG, поэтому PDF на 30 товаров весил бы сотни мегабайт.

Здесь каждое фото один раз ужимается до ``thumb_max_px`` по длинной стороне и
кладётся рядом в ``data/thumbs`` под ключом от пути+mtime+размера. Повторные
выгрузки берут готовый файл.

``Image.draft()`` заставляет libjpeg декодировать сразу в уменьшенном размере
(1/2, 1/4, 1/8) — это в разы быстрее полного декодирования 4 МБ.
"""

from __future__ import annotations

import hashlib
import io
import threading
from pathlib import Path

from PIL import Image, ImageOps

from app.config import BASE_DIR, settings

_LOCK = threading.Lock()
_READY = False


def thumbs_dir() -> Path:
    d = Path(settings.thumbs_dir)
    if not d.is_absolute():
        d = BASE_DIR / d
    return d


def _ensure_dir() -> bool:
    global _READY
    if _READY:
        return True
    with _LOCK:
        if _READY:
            return True
        try:
            thumbs_dir().mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        _READY = True
    return True


def cache_key(path: Path, max_px: int, quality: int) -> str:
    """Ключ кэша: путь + mtime + размер + параметры ужатия."""
    try:
        st = path.stat()
        sig = f"{path}|{st.st_mtime_ns}|{st.st_size}|{max_px}|{quality}"
    except OSError:
        sig = f"{path}|0|0|{max_px}|{quality}"
    return hashlib.md5(sig.encode("utf-8", "replace")).hexdigest()


def _render(path: Path, max_px: int, quality: int) -> bytes | None:
    """Открыть, повернуть по EXIF, ужать, отдать JPEG-байтами."""
    try:
        with Image.open(path) as im:
            try:
                im.draft("RGB", (max_px, max_px))
            except Exception:
                pass
            im = ImageOps.exif_transpose(im) or im
            if im.mode not in ("RGB", "L"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                else:
                    im = im.convert("RGB")
            elif im.mode == "L":
                im = im.convert("RGB")
            im.thumbnail((max_px, max_px), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    except Exception:
        return None


def thumb_bytes(
    path: Path | str,
    max_px: int | None = None,
    quality: int | None = None,
) -> bytes | None:
    """Миниатюра фото. ``None`` — файл нечитаем (не роняем выгрузку)."""
    p = Path(path)
    mx = max_px or settings.thumb_max_px
    q = quality or settings.thumb_quality
    if not p.exists():
        return None
    if not _ensure_dir():
        return _render(p, mx, q)

    key = cache_key(p, mx, q)
    target = thumbs_dir() / f"{key}.jpg"
    try:
        if target.exists() and target.stat().st_size > 0:
            return target.read_bytes()
    except OSError:
        return _render(p, mx, q)

    data = _render(p, mx, q)
    if data is None:
        return None
    tmp = target.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(target)
    except OSError:
        pass
    return data


def thumb_size(data: bytes) -> tuple[int, int]:
    """Размеры JPEG-байтов без полной загрузки в память."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return (0, 0)


def clear_cache() -> int:
    """Удалить кэш миниатюр (в т.ч. осиротевшие файлы). Возвращает число удалённых."""
    n = 0
    d = thumbs_dir()
    if not d.is_dir():
        return 0
    for f in d.glob("*.jpg"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n