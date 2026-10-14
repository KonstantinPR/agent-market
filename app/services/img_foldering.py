# -*- coding: utf-8 -*-
"""«Фото для маркетплейса»: раскладка фотографий по папкам WB/Ozon в ZIP.

Порт ``finance/app/modules/img_processor.py::img_foldering`` без файловой
системы: фото берутся из индекса Яндекс.Диска (``app/services/photos.py``),
архив собирается в памяти.

Конвенции имён:

- **WB** — папка ``<nm_id>/`` (nmID из ``nm_articles``), файлы ``<артикул>-<n>.JPG``.
- **OZON** — папка ``<артикул>/``, файлы ``<артикул>.JPG`` (позиция 1),
  ``<артикул>_<n>.JPG`` (n ≥ 2).

``ONLY_NEW`` берёт по одному фото на каждый номер, ``ALL`` — все файлы (совпавшие
имена перенумеровываются). Если суммарный объём > ``max_part_mb`` — «zip из
zip-ов»: папки не дробятся между частями.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.services import photos as photos_service

#: Порог, после которого архив разбивается на части (МБ).
MAX_PART_MB = 500

#: Хвост ``-<цифры>`` или ``_<цифры>`` — позиция фото (для ведущих нулей).
_SUFFIX_RE = re.compile(r"^(.*?)([-_])(\d+)$")


@dataclass
class ArticleEntry:
    """Готовый к записи артикул: папка в архиве и список (имя, путь)."""

    article: str
    folder: str
    files: list[tuple[str, Path]] = field(default_factory=list)


# ------------------------------------------------------------------ утилиты имён


def _split_ext(name: str) -> tuple[str, str]:
    if "." in name:
        stem, _, ext = name.rpartition(".")
        return stem, "." + ext
    return name, ""


def _leading_nulls(name: str) -> str:
    """``<артикул>-2.JPG`` → ``<артикул>-002.JPG`` (позиция до 3 знаков)."""
    stem, ext = _split_ext(name)
    m = _SUFFIX_RE.match(stem)
    if not m:
        return name
    return f"{m.group(1)}{m.group(2)}{m.group(3).zfill(3)}{ext}"


def _dedupe_names(files: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    used: set[str] = set()
    out: list[tuple[str, Path]] = []
    for name, path in files:
        if name not in used:
            used.add(name)
            out.append((name, path))
            continue
        stem, ext = _split_ext(name)
        k = 2
        cand = f"{stem}-{k}{ext}"
        while cand in used:
            k += 1
            cand = f"{stem}-{k}{ext}"
        used.add(cand)
        out.append((cand, path))
    return out


def _swap_positions(files: list[tuple[str, Path]], change_order) -> list[tuple[str, Path]]:
    """Поменять содержимое позиций 1 и N (имена остаются на местах)."""
    if not change_order:
        return files
    try:
        n = int(change_order)
    except (TypeError, ValueError):
        return files
    if n < 2 or len(files) < n:
        return files
    files = list(files)
    first, last = files[0], files[n - 1]
    files[0] = (first[0], last[1])
    files[n - 1] = (last[0], first[1])
    return files


# ------------------------------------------------------------------ план


def _nm_key(article: str) -> str:
    return str(article).strip().upper()


def plan(
    articles: list,
    nm_map: Optional[dict] = None,
    marketplace: str = "WB",
    is_replace: str = "ONLY_NEW",
    order: str = "ASCENDING",
    index=None,
) -> tuple[list[ArticleEntry], dict]:
    """Сопоставить артикулы с фото и именем папки. Возвращает (entries, отчёт)."""
    idx = index if index is not None else photos_service.get_index()
    mp = (marketplace or "WB").strip().upper()
    nm_map = {_nm_key(k): str(v) for k, v in (nm_map or {}).items() if v}
    only_new = (is_replace or "").strip().upper() != "ALL"
    desc = str(order or "").strip().upper().startswith("DESC")

    entries: list[ArticleEntry] = []
    missing: list[str] = []
    no_nm: list[str] = []
    with_photos: list[dict] = []
    total = 0

    for raw in articles or []:
        art = str(raw).strip()
        if not art:
            continue
        total += 1
        ap = idx.get(art, allow_prefix=True)
        if ap is None or not ap.items:
            missing.append(art)
            continue

        items = sorted(ap.items, key=lambda t: (t[0], str(t[1])), reverse=desc)
        if only_new:
            deduped: list[tuple[int, Path]] = []
            seen_nums: set[int] = set()
            for num, path in items:
                if num in seen_nums:
                    continue
                seen_nums.add(num)
                deduped.append((num, path))
            items = deduped

        if mp == "WB":
            folder = nm_map.get(_nm_key(art), "")
            if not folder:
                no_nm.append(art)
                continue
        else:
            folder = art

        files: list[tuple[str, Path]] = []
        for pos, (_num, path) in enumerate(items, start=1):
            ext = path.suffix or ".JPG"
            if mp == "WB":
                name = path.name
            else:
                name = f"{art}{ext}" if pos == 1 else f"{art}_{pos}{ext}"
            files.append((name, path))
        files = _dedupe_names(files)

        entries.append(ArticleEntry(article=art, folder=folder, files=files))
        with_photos.append({"article": art, "folder": folder, "photos": len(files)})

    report = {
        "total": total,
        "found": len(entries),
        "photos": sum(len(e.files) for e in entries),
        "with_photos": with_photos,
        "missing": missing,
        "no_nm": no_nm,
    }
    return entries, report


# ------------------------------------------------------------------ сборка zip


def _read(path: Path) -> bytes:
    return path.read_bytes()


def _total_size(entries: list[ArticleEntry]) -> int:
    total = 0
    for e in entries:
        for _name, path in e.files:
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def build_zip(
    entries: list[ArticleEntry],
    leading_nulls: bool = False,
    change_order=None,
    max_part_mb: int = MAX_PART_MB,
) -> io.BytesIO:
    """Собрать ZIP (или zip из zip-ов при превышении ``max_part_mb``)."""
    prepared: list[tuple[str, list[tuple[str, Path]]]] = []
    for e in entries:
        files = _swap_positions(e.files, change_order)
        if leading_nulls:
            files = [(_leading_nulls(name), path) for name, path in files]
        prepared.append((e.folder, files))

    max_bytes = max_part_mb * 1024 * 1024
    total = _total_size(entries)
    out = io.BytesIO()

    if total <= max_bytes:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for folder, files in prepared:
                for name, path in files:
                    zf.writestr(f"{folder}/{name}", _read(path))
        out.seek(0)
        return out

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as main:
        part_no = 1
        part_buf = io.BytesIO()
        part_zip = zipfile.ZipFile(part_buf, "w", zipfile.ZIP_DEFLATED)
        part_size = 0
        index_lines: list[str] = []
        for folder, files in prepared:
            fsize = sum(p.stat().st_size for _n, p in files if _safe(p))
            if part_size > 0 and part_size + fsize > max_bytes:
                _flush_part(main, part_zip, part_buf, part_no)
                part_no += 1
                part_buf = io.BytesIO()
                part_zip = zipfile.ZipFile(part_buf, "w", zipfile.ZIP_DEFLATED)
                part_size = 0
            for name, path in files:
                part_zip.writestr(f"{folder}/{name}", _read(path))
            part_size += fsize
            index_lines.append(f"{folder} -> images_part_{part_no:03d}.zip")
        _flush_part(main, part_zip, part_buf, part_no)
        main.writestr("README.txt", _readme(max_part_mb, total, part_no, index_lines))
    out.seek(0)
    return out


def _safe(path: Path) -> bool:
    try:
        path.stat()
        return True
    except OSError:
        return False


def _flush_part(main: zipfile.ZipFile, part_zip: zipfile.ZipFile, buf: io.BytesIO, part_no: int) -> None:
    part_zip.close()
    main.writestr(f"images_part_{part_no:03d}.zip", buf.getvalue())


def _readme(max_part_mb: int, total: int, parts: int, lines: list[str]) -> str:
    return (
        "ZIP из ZIP-ов — структура папок сохранена\n"
        "========================================\n"
        f"Частей: {parts}\n"
        f"Объём исходных данных: {total / (1024 * 1024):.2f} МБ\n"
        f"Каждая часть < {max_part_mb} МБ, папки не дробятся\n\n"
        "Распределение папок:\n" + "\n".join(lines) + "\n"
    )
