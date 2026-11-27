# -*- coding: utf-8 -*-
"""T-101: Упаковочный лист на основе потребности WB и файла остатков.

Источник потребности: ``replenish.replenish_rows(..., view='sizes')`` — берём
WB ``to_sort > 0`` (и опционально Ozon позже; сейчас считаем WB-план).
Источник остатков: ``ОСТАТКИ.xlsx`` в папке ``storage_settings.stock_root``.

Коммит: backup оригинала в ``ОРИГИНАЛ/``, запись упаковочного листа
``00N_YYYY-MM-DD.xlsx``, атомарное списание остатков, манифест в БД.
Откат: только если hash текущего файла остатков совпадает с post-commit hash.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.services import replenish as replenish_service, storage_settings

STOCK_FILE_NAME = "ОСТАТКИ.xlsx"
ORIGINAL_DIR_NAME = "ОРИГИНАЛ"

# Колонки остатков: принимаем распространённые русские/английские варианты.
_STOCK_COLS = {
    "article": ["артикул", "артикул товара", "article", "vendor_code", "код"],
    "size": ["размер", "size", "размер товара"],
    "barcode": ["штрихкод", "баркод", "barcode", "шк", "ean"],
    "qty": ["остаток", "количество", "кол-во", "кол-во шт", "кол-во шт.", "qty", "stock", "остаток шт", "остаток шт.", "количество шт", "количество шт."],
    "name": ["наименование", "название", "name", "товар"],
}

DEFAULT_CABINET_PRIORITY = ["ip_prudnikov_wb", "oo_joinco", "ip_prudnikov"]
DEFAULT_CABINET_WEIGHTS = {
    "ip_prudnikov_wb": 1,
    "oo_joinco": 2,
    "ip_prudnikov": 3,
}


class PackingError(ValueError):
    pass


def _norm_key(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _norm_article(s: Any) -> str:
    return str(s or "").strip().upper()


def _norm_size(s: Any) -> str:
    v = str(s or "").strip()
    if not v or v.lower() in {"nan", "none", "—", "-"}:
        return ""
    return v.upper()


def _norm_barcode(s: Any) -> str:
    v = str(s or "").strip()
    if v.endswith(".0") and v[:-2].isdigit():
        v = v[:-2]
    return v


def _find_col(df: pd.DataFrame, aliases: list[str]) -> Optional[str]:
    norm = {_norm_key(c): c for c in df.columns}
    for a in aliases:
        if a in norm:
            return norm[a]
    return None


def stock_file_path(db: Session) -> Path:
    root = Path(storage_settings.stock_root(db))
    return root / STOCK_FILE_NAME


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_stock_file(path: Path) -> tuple[pd.DataFrame, dict[str, str]]:
    if not path.exists():
        raise PackingError(f"Файл остатков не найден: {path}")
    try:
        df = pd.read_excel(path, sheet_name=0)
    except Exception as e:  # noqa: BLE001
        raise PackingError(f"Не удалось прочитать {path.name}: {e}") from e
    if df.empty:
        raise PackingError(f"Файл остатков пуст: {path.name}")
    cols = {}
    for key, aliases in _STOCK_COLS.items():
        col = _find_col(df, aliases)
        if col:
            cols[key] = col
    if "article" not in cols or "qty" not in cols:
        raise PackingError(
            "В ОСТАТКИ.xlsx не найдены обязательные колонки: Артикул и Остаток/Количество"
        )
    return df, cols


def _stock_index(df: pd.DataFrame, cols: dict[str, str]) -> dict[tuple[str, str], list[int]]:
    idx: dict[tuple[str, str], list[int]] = {}
    art_col = cols["article"]
    size_col = cols.get("size")
    for i, row in df.iterrows():
        art = _norm_article(row.get(art_col))
        size = _norm_size(row.get(size_col)) if size_col else ""
        if not art:
            continue
        idx.setdefault((art, size), []).append(i)
    return idx


def _stock_barcode_index(df: pd.DataFrame, cols: dict[str, str]) -> dict[str, list[int]]:
    bc_col = cols.get("barcode")
    if not bc_col:
        return {}
    idx: dict[str, list[int]] = {}
    for i, row in df.iterrows():
        bc = _norm_barcode(row.get(bc_col))
        if bc:
            idx.setdefault(bc, []).append(i)
    return idx


def _available_qty(df: pd.DataFrame, cols: dict[str, str], rows: list[int]) -> int:
    qty_col = cols["qty"]
    total = 0.0
    for i in rows:
        try:
            total += float(df.at[i, qty_col] or 0)
        except Exception:  # noqa: BLE001
            pass
    return max(0, int(total))


def _match_stock_rows(
    item: dict, sidx: dict[tuple[str, str], list[int]], bcidx: dict[str, list[int]]
) -> list[int]:
    art = _norm_article(item.get("article"))
    size = _norm_size(item.get("size"))
    bc = _norm_barcode(item.get("barcode"))
    # 1) артикул+размер
    rows = sidx.get((art, size), [])
    if rows:
        return rows
    # 2) артикул без размера / пустой размер
    if size:
        rows = sidx.get((art, ""), [])
        if rows:
            return rows
    # 3) штрихкод
    if bc and bc in bcidx:
        return bcidx[bc]
    return []


def _cabinet_label(code: str, cabinets: dict[str, models.Cabinet]) -> str:
    c = cabinets.get(code)
    if c:
        return c.name
    return code


def _resolve_cabinets(db: Session, codes: list[str]) -> dict[str, models.Cabinet]:
    rows = db.execute(select(models.Cabinet).where(models.Cabinet.code.in_(codes))).scalars().all()
    return {c.code: c for c in rows}


def build_draft(
    db: Session,
    *,
    date_from: date,
    date_to: date,
    target_days: int = 30,
    wish: int = 0,
    wish_step: str = "",
    max_per_size: int = 0,
    rehab_units: int = 0,
    article_like: Optional[str] = None,
    cabinet_codes: Optional[list[str]] = None,
    include_ozon: bool = False,
) -> dict:
    cabinet_codes = [c for c in (cabinet_codes or DEFAULT_CABINET_PRIORITY) if c]
    if not cabinet_codes:
        cabinet_codes = list(DEFAULT_CABINET_PRIORITY)
    cabinets = _resolve_cabinets(db, cabinet_codes)

    rep = replenish_service.replenish_rows(
        db, date_from, date_to,
        target_days=target_days, sort="urgency", view="sizes",
        marketplace=None if include_ozon else "wb",
        article_like=article_like,
        wish=wish, wish_step=wish_step,
        max_per_size=max_per_size, rehab_units=rehab_units,
    )
    rows = rep.get("rows") or []

    path = stock_file_path(db)
    stock_df, stock_cols = read_stock_file(path)
    sidx = _stock_index(stock_df, stock_cols)
    bcidx = _stock_barcode_index(stock_df, stock_cols)

    items = []
    for r in rows:
        to_sort = int(r.get("to_sort") or 0)
        ship_oz = int(r.get("ship_ozon") or r.get("oz_ship") or r.get("ship_oz") or 0)
        need = to_sort + (ship_oz if include_ozon else 0)
        if need <= 0:
            continue
        matched = _match_stock_rows(r, sidx, bcidx)
        avail = _available_qty(stock_df, stock_cols, matched)
        pack = min(need, avail)
        alloc = allocate_by_priority(pack, cabinet_codes, DEFAULT_CABINET_WEIGHTS)
        items.append({
            "article": str(r.get("article") or ""),
            "name": str(r.get("name") or ""),
            "size": str(r.get("size") or "—"),
            "barcode": str(r.get("barcode") or ""),
            "need_qty": need,
            "to_sort": to_sort,
            "ship_oz": ship_oz if include_ozon else 0,
            "available_qty": avail,
            "pack_qty": pack,
            "allocation": alloc,
            "stock_rows": matched,
            "status": "ok" if avail >= need else ("partial" if avail > 0 else "missing"),
        })

    return {
        "stock_file": str(path),
        "stock_file_exists": path.exists(),
        "stock_hash": _file_hash(path),
        "date_from": str(date_from),
        "date_to": str(date_to),
        "cabinet_codes": cabinet_codes,
        "cabinets": [{"code": c, "name": _cabinet_label(c, cabinets)} for c in cabinet_codes],
        "items": items,
        "count": len(items),
        "need_total": sum(i["need_qty"] for i in items),
        "pack_total": sum(i["pack_qty"] for i in items),
        "available_total": sum(i["available_qty"] for i in items),
    }


def allocate_by_priority(pack_qty: int, cabinet_codes: list[str], weights: dict[str, int] | None = None) -> dict[str, int]:
    """Распределение при нехватке: по приоритету/весу, остаток по порядку."""
    weights = weights or {}
    codes = [c for c in cabinet_codes if c]
    alloc = {c: 0 for c in codes}
    if pack_qty <= 0 or not codes:
        return alloc
    total_w = sum(max(0, int(weights.get(c, 1))) for c in codes)
    if total_w <= 0:
        total_w = len(codes)
        weights = {c: 1 for c in codes}
    # целая часть по весам
    remain = pack_qty
    fracs = []
    for c in codes:
        w = max(0, int(weights.get(c, 1)))
        exact = pack_qty * w / total_w
        base = int(exact)
        alloc[c] = base
        remain -= base
        fracs.append((exact - base, c))
    # остаток — по наибольшей дробной части, затем по порядку
    for _, c in sorted(fracs, key=lambda x: (-x[0], codes.index(x[1]))):
        if remain <= 0:
            break
        alloc[c] += 1
        remain -= 1
    return alloc


def normalize_allocation(alloc: dict[str, Any], cabinet_codes: list[str], pack_qty: int) -> dict[str, int]:
    out = {c: 0 for c in cabinet_codes}
    for c in cabinet_codes:
        try:
            out[c] = max(0, int(alloc.get(c, 0) or 0))
        except Exception:  # noqa: BLE001
            out[c] = 0
    total = sum(out.values())
    if total <= pack_qty:
        return out
    # обрезаем пропорционально, сохраняя pack_qty
    scale = pack_qty / total if total else 0
    remain = pack_qty
    fracs = []
    for c in cabinet_codes:
        exact = out[c] * scale
        base = int(exact)
        out[c] = base
        remain -= base
        fracs.append((exact - base, c))
    for _, c in sorted(fracs, key=lambda x: (-x[0], cabinet_codes.index(x[1]))):
        if remain <= 0:
            break
        out[c] += 1
        remain -= 1
    return out


def _next_packing_name(folder: Path, day: date) -> str:
    prefix = day.isoformat()
    n = 1
    while True:
        name = f"{n:03d}_{prefix}.xlsx"
        path = folder / name
        if not path.exists():
            return name
        n += 1


def _write_packing_excel(path: Path, draft: dict, items: list[dict], cabinets: list[dict]) -> None:
    header = ["Артикул", "Наименование", "Размер", "Штрихкод", "Нужно", "В наличии", "В упаковку"]
    for c in cabinets:
        header.append(f"В {c.get('name') or c.get('code')}")
    header += ["Статус"]
    rows = []
    for it in items:
        row = [
            it.get("article", ""), it.get("name", ""), it.get("size", ""), it.get("barcode", ""),
            int(it.get("need_qty") or 0), int(it.get("available_qty") or 0), int(it.get("pack_qty") or 0),
        ]
        alloc = it.get("allocation") or {}
        for c in cabinets:
            row.append(int(alloc.get(c.get("code"), 0) or 0))
        row.append(it.get("status", ""))
        rows.append(row)
    df = pd.DataFrame(rows, columns=header)
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Упаковочный лист", index=False)


def _atomic_write_excel(path: Path, df: pd.DataFrame, sheet_name: str = "Остатки") -> str:
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    with pd.ExcelWriter(tmp, engine="openpyxl") as w:
        df.to_excel(w, sheet_name=sheet_name[:31], index=False)
    os.replace(tmp, path)
    return _file_hash(path)


def commit_draft(db: Session, draft: dict) -> dict:
    stock_path = Path(draft.get("stock_file") or stock_file_path(db))
    if not stock_path.exists():
        raise PackingError(f"Файл остатков не найден: {stock_path}")
    before_hash = _file_hash(stock_path)
    expected_hash = str(draft.get("stock_hash") or "")
    if expected_hash and before_hash != expected_hash:
        raise PackingError(
            "Файл остатков изменился после формирования черновика. Сформируйте черновик заново."
        )

    stock_df, stock_cols = read_stock_file(stock_path)
    sidx = _stock_index(stock_df, stock_cols)
    bcidx = _stock_barcode_index(stock_df, stock_cols)
    qty_col = stock_cols["qty"]

    raw_items = draft.get("items") or []
    cabinet_codes = [c.get("code") if isinstance(c, dict) else str(c)
                     for c in (draft.get("cabinets") or draft.get("cabinet_codes") or [])]
    cabinet_codes = [c for c in cabinet_codes if c] or list(DEFAULT_CABINET_PRIORITY)
    cabinets = [{"code": c, "name": (cobj.get("name") if isinstance(cobj, dict) else c)}
                for c, cobj in zip(cabinet_codes, (draft.get("cabinets") or cabinet_codes))]

    prepared = []
    for raw in raw_items:
        need = max(0, int(raw.get("need_qty") or 0))
        avail = max(0, int(raw.get("available_qty") or 0))
        pack = max(0, int(raw.get("pack_qty") or 0))
        pack = min(pack, need, avail)
        alloc = normalize_allocation(raw.get("allocation") or {}, cabinet_codes, pack)
        matched = _match_stock_rows(raw, sidx, bcidx)
        real_avail = _available_qty(stock_df, stock_cols, matched)
        if pack > real_avail:
            raise PackingError(
                f"Недостаточно остатка для {raw.get('article')} {raw.get('size')}: нужно {pack}, есть {real_avail}"
            )
        prepared.append({
            **raw,
            "need_qty": need,
            "available_qty": real_avail,
            "pack_qty": pack,
            "allocation": alloc,
            "stock_rows": matched,
            "status": "ok" if real_avail >= need else ("partial" if real_avail > 0 else "missing"),
        })

    # списание
    for it in prepared:
        remain = int(it["pack_qty"])
        for idx in it["stock_rows"]:
            if remain <= 0:
                break
            cur = float(stock_df.at[idx, qty_col] or 0)
            take = min(cur, float(remain))
            if take <= 0:
                continue
            stock_df.at[idx, qty_col] = cur - take
            remain -= int(take)
        if remain > 0:
            raise PackingError(f"Не удалось списать остаток: {it.get('article')} {it.get('size')}")

    folder = stock_path.parent
    original_dir = folder / ORIGINAL_DIR_NAME
    original_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{stock_path.stem}_{ts}{stock_path.suffix}"
    backup_path = original_dir / backup_name
    shutil.copy2(stock_path, backup_path)

    pack_name = _next_packing_name(folder, datetime.now().date())
    pack_path = folder / pack_name
    _write_packing_excel(pack_path, draft, prepared, cabinets)

    after_hash = _atomic_write_excel(stock_path, stock_df, sheet_name="Остатки")

    pl = models.PackingList(
        status="committed",
        stock_file=str(stock_path),
        backup_file=str(backup_path),
        packing_file=str(pack_path),
        stock_hash_before=before_hash,
        stock_hash_after=after_hash,
        params_json=json.dumps({
            "date_from": draft.get("date_from"), "date_to": draft.get("date_to"),
            "cabinet_codes": cabinet_codes,
        }, ensure_ascii=False),
        meta_json=json.dumps({
            "count": len(prepared), "need_total": sum(i["need_qty"] for i in prepared),
            "pack_total": sum(i["pack_qty"] for i in prepared),
        }, ensure_ascii=False),
    )
    db.add(pl)
    db.flush()
    for it in prepared:
        db.add(models.PackingListItem(
            packing_list_id=pl.id,
            article=str(it.get("article") or ""),
            name=str(it.get("name") or ""),
            size=str(it.get("size") or ""),
            barcode=str(it.get("barcode") or ""),
            need_qty=int(it.get("need_qty") or 0),
            available_qty=int(it.get("available_qty") or 0),
            pack_qty=int(it.get("pack_qty") or 0),
            allocation_json=json.dumps(it.get("allocation") or {}, ensure_ascii=False),
            status=str(it.get("status") or ""),
        ))
    db.commit()
    return {
        "ok": True,
        "id": pl.id,
        "packing_file": str(pack_path),
        "backup_file": str(backup_path),
        "stock_file": str(stock_path),
        "stock_hash_before": before_hash,
        "stock_hash_after": after_hash,
        "count": len(prepared),
        "pack_total": sum(i["pack_qty"] for i in prepared),
    }


def list_packing_lists(db: Session, limit: int = 50) -> list[dict]:
    rows = db.execute(
        select(models.PackingList).order_by(models.PackingList.id.desc()).limit(limit)
    ).scalars().all()
    out = []
    for r in rows:
        meta = {}
        try:
            meta = json.loads(r.meta_json or "{}")
        except Exception:  # noqa: BLE001
            pass
        out.append({
            "id": r.id,
            "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
            "status": r.status,
            "packing_file": r.packing_file,
            "backup_file": r.backup_file,
            "stock_file": r.stock_file,
            "count": meta.get("count", 0),
            "pack_total": meta.get("pack_total", 0),
        })
    return out


def get_packing_list(db: Session, packing_id: int) -> dict:
    r = db.get(models.PackingList, packing_id)
    if not r:
        raise PackingError("Упаковочный лист не найден")
    items = db.execute(
        select(models.PackingListItem).where(models.PackingListItem.packing_list_id == r.id)
    ).scalars().all()
    return {
        "id": r.id,
        "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
        "status": r.status,
        "packing_file": r.packing_file,
        "backup_file": r.backup_file,
        "stock_file": r.stock_file,
        "stock_hash_before": r.stock_hash_before,
        "stock_hash_after": r.stock_hash_after,
        "items": [{
            "article": i.article, "name": i.name, "size": i.size, "barcode": i.barcode,
            "need_qty": i.need_qty, "available_qty": i.available_qty, "pack_qty": i.pack_qty,
            "allocation": json.loads(i.allocation_json or "{}"),
            "status": i.status,
        } for i in items],
    }


def revert_packing_list(db: Session, packing_id: int) -> dict:
    r = db.get(models.PackingList, packing_id)
    if not r:
        raise PackingError("Упаковочный лист не найден")
    if r.status != "committed":
        raise PackingError("Откатить можно только применённый упаковочный лист")
    stock_path = Path(r.stock_file)
    backup_path = Path(r.backup_file)
    if not backup_path.exists():
        raise PackingError(f"Backup не найден: {backup_path}")
    if stock_path.exists():
        cur_hash = _file_hash(stock_path)
        if r.stock_hash_after and cur_hash != r.stock_hash_after:
            raise PackingError(
                "Файл остатков изменился после упаковки. Автоматический откат небезопасен."
            )
    shutil.copy2(backup_path, stock_path)
    r.status = "reverted"
    r.reverted_at = datetime.now()
    db.commit()
    return {"ok": True, "id": r.id, "status": r.status, "stock_file": str(stock_path)}
