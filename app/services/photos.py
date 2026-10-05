"""Индекс фотографий товаров с диска (Яндекс.Диск).

Фотографии лежат в глубоко вложенной структуре, имена — ``<артикул>-<номер>.JPG``,
но реальность содержит мусор: двойные расширения (``X-2.JPG.JPG``), копии Windows
(``X-8 — копия.JPG``), дубли ``(2)``, файлы вообще без номера (``.JPG`` с кодом
SKU) и один артикул сразу в нескольких папках, включая архив.

Поэтому индекс строится один раз рекурсивным обходом (~0.4 с на 60k файлов),
нормализует имена, ключи приводит к верхнему регистру (артикулы в БД и на диске
различаются регистром) и кэшируется в памяти на TTL.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings

#: Расширения, которые умеет читать Pillow (webp нужен тоже — reportlab его не берёт).
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}

#: Имя папки архива: фото из неё — запасной вариант.
ARCHIVE_DIR = "АРХИВ"

#: ``<артикул>-<номер>`` — хвост из цифр.
_NUM_RE = re.compile(r"^(.*?)-(\d+)$")

#: Хвостовой мусор: `` — копия``, `` - copy``, ``(2)``, `` (12)``.
_JUNK_TAIL_RE = re.compile(
    r"(?:\s*[-—–]?\s*(?:копия|copy|\(\s*\d+\s*\))\s*)+$", re.IGNORECASE
)

#: Хвостовые расширения, слипшиеся с именем (``X-2.JPG.JPG``).
_EXT_TAIL_RE = re.compile(r"(?:\.(?:jpe?g|png|webp))+$", re.IGNORECASE)

#: Время жизни индекса в памяти, сек.
INDEX_TTL = 600.0

#: Пробовать ли укоротить артикул из БД, если точного нет.
#: В БД встречаются ``wlp-tm130005-1506-m2``, а фото лежат как
#: ``WLP-TM130005-1506`` — хвост-вариант (цвет/рынок) в имя фото не попал.
PREFIX_FALLBACK = True
#: Короче этого не укорачиваем — иначе рискуем попасть в чужой артикул.
MIN_PREFIX_LEN = 6


def clean_stem(stem: str) -> str:
    """Убрать из имени хвост-мусор: ``X-8 — копия`` -> ``X-8``, ``X.JPG.JPG`` -> ``X``."""
    s = _JUNK_TAIL_RE.sub("", stem)
    s = _EXT_TAIL_RE.sub("", s)
    return s.strip()


def split_photo(stem: str) -> tuple[str, int]:
    """Разобрать имя файла на ``(артикул, номер)``.

    ``WLP-117116-13-1`` -> ``("WLP-117116-13", 1)`` — артикул сам содержит дефисы,
    поэтому номер берём только с хвоста.
    Файлы без номера (``NO8B7861.JPG``) считаем первой фотографией.
    """
    s = clean_stem(stem)
    m = _NUM_RE.match(s)
    if m and m.group(1).strip():
        return m.group(1).strip(), int(m.group(2))
    return s, 1


def folder_rank(path: Path, root: Path) -> int:
    """0 — активная папка, 1 — архив. Приоритет при выборе источника фото."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return 0
    for part in rel.parts[:-1]:
        if part.strip().upper() == ARCHIVE_DIR.upper():
            return 1
    return 0


@dataclass
class ArticlePhotos:
    """Фотографии одного артикула, уже отобранные и отсортированные."""

    article: str
    folder: Path
    rank: int
    items: list[tuple[int, Path]] = field(default_factory=list)

    def paths(self, count: int) -> list[Path]:
        """Первые ``count`` фото (после сортировки по номеру)."""
        if count <= 0:
            return []
        return [p for _, p in self.items[:count]]


class PhotoIndex:
    """Рекурсивный индекс ``артикул -> фотографии`` с кэшем в памяти."""

    def __init__(self, root: Path | None = None, ttl: float = INDEX_TTL) -> None:
        self.root = Path(root) if root else Path(
            os.environ.get("PHOTOS_ROOT")
            or getattr(settings, "photos_root", "")
            or r"C:\YandexDisk\ФОТОГРАФИИ"
        )
        self.ttl = ttl
        self._by_key: dict[str, ArticlePhotos] = {}
        self._built_at: float = 0.0
        self._lock = threading.Lock()
        self.stats: dict = {}

    # -- построение -----------------------------------------------------

    def _collect(self) -> tuple[dict[str, ArticlePhotos], dict]:
        # артикул -> (rank, папка) -> [(num, path)]; ключ папки — lowercase
        # для группировки, но сам путь храним в исходном регистре.
        groups: dict[str, dict[tuple[int, str], tuple[Path, list[tuple[int, Path]]]]] = {}
        files = 0
        photos = 0
        junk = 0
        if self.root.is_dir():
            for dirpath, _dirnames, filenames in os.walk(self.root):
                for fn in filenames:
                    stem, ext = os.path.splitext(fn)
                    if ext.lower() not in PHOTO_EXT:
                        continue
                    files += 1
                    path = Path(dirpath) / fn
                    article, num = split_photo(stem)
                    if not article:
                        junk += 1
                        continue
                    if clean_stem(stem) != stem or not _NUM_RE.match(clean_stem(stem)):
                        junk += 1
                    photos += 1
                    rank = folder_rank(path, self.root)
                    key = (rank, str(path.parent).lower())
                    slot = groups.setdefault(article.upper(), {}).setdefault(
                        key, (path.parent, [])
                    )
                    slot[1].append((num, path))

        index: dict[str, ArticlePhotos] = {}
        dup_articles = 0
        for key, folders in groups.items():
            if len(folders) > 1:
                dup_articles += 1
            # активная папка > архив; внутри — больше фото; затем имя папки
            best_key = min(folders, key=lambda k: (k[0], -len(folders[k][1]), k[1]))
            folder, items = folders[best_key]
            index[key] = ArticlePhotos(
                article=key,
                folder=folder,
                rank=best_key[0],
                items=sorted(items, key=lambda t: (t[0], str(t[1]))),
            )
        return index, {
            "files": files,
            "photos": photos,
            "articles": len(index),
            "articles_multi_folder": dup_articles,
            "junk_names": junk,
            "root_exists": self.root.is_dir(),
        }

    def _get(self) -> dict[str, ArticlePhotos]:
        with self._lock:
            if self._by_key and (time.monotonic() - self._built_at) < self.ttl:
                return self._by_key
            index, stats = self._collect()
            self._by_key = index
            self.stats = stats
            self._built_at = time.monotonic()
            return index

    def refresh(self) -> dict:
        """Принудительно перестроить индекс (после правок в папках)."""
        with self._lock:
            self._built_at = 0.0
        self._get()
        return self.stats

    def invalidate(self) -> None:
        with self._lock:
            self._by_key = {}
            self._built_at = 0.0

    # -- поиск ----------------------------------------------------------

    def _by_prefix(self, key: str, index: dict[str, ArticlePhotos]) -> ArticlePhotos | None:
        """Отбросить хвостовые сегменты артикула: ``A-B-C-12`` -> ``A-B-C``.

        Ищем именно существующий ключ индекса, а не «фото, начинающиеся с
        префикса», поэтому результат всегда однозначен.
        """
        segs = key.split("-")
        for cut in range(1, len(segs)):
            cand = "-".join(segs[: len(segs) - cut])
            if len(cand) < MIN_PREFIX_LEN:
                break
            hit = index.get(cand)
            if hit is not None:
                return hit
        return None

    def find(self, article: str, count: int = 1, allow_prefix: bool = True) -> list[Path]:
        """Первые ``count`` фото артикула. Пустой список — фото не найдены."""
        if not article:
            return []
        key = str(article).strip().upper()
        index = self._get()
        entry = index.get(key)
        if entry is None and allow_prefix and PREFIX_FALLBACK:
            entry = self._by_prefix(key, index)
        if not entry:
            return []
        return entry.paths(count)

    def get(self, article: str, allow_prefix: bool = False) -> ArticlePhotos | None:
        if not article:
            return None
        key = str(article).strip().upper()
        index = self._get()
        entry = index.get(key)
        if entry is None and allow_prefix and PREFIX_FALLBACK:
            entry = self._by_prefix(key, index)
        return entry

    def articles(self) -> list[str]:
        return sorted(self._get())


_index: PhotoIndex | None = None
_index_lock = threading.Lock()


def get_index() -> PhotoIndex:
    """Общий индекс процесса (ленивый, потокобезопасный)."""
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = PhotoIndex()
    return _index


def set_index(index: PhotoIndex | None) -> None:
    """Подменить индекс (тесты)."""
    global _index
    with _index_lock:
        _index = index