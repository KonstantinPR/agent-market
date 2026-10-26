# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
import pandas as pd
from sqlalchemy.orm import Session

from app import models
from app.services import cabinets as cabinet_service
from app.services import warehouse as warehouse_service


NAME_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?:\s*-\s*(.*))?\s*$")


@dataclass
class FolderInfo:
    folder_name: str
    num: str
    date: Optional[date]
    note: str
    status: str  # ok|bad_name|skip_archive|no_file|exists
    file_path: Optional[Path]
    file_name: Optional[str]
    warning: Optional[str]
    source_ref: str


def _default_settings_for(cab: models.Cabinet) -> Dict[str, Any]:
    code = (cab.code or "").lower()
    root = ""
    if "join" in code:
        roots = [
            r"C:\YandexDisk\СЕТЕВЫЕ МАГАЗИНЫ\WILDBERRIES\ДЖОИНС\ОТПРАВКИ",
            r"E:\Мой диск\ООО ДЖОИНС\ОТПРАВКИ",
        ]
        for r in roots:
            if os.path.exists(r):
                root = r
                break
        if not root:
            root = roots[0]
    elif "prud" in code or code == "ip_prudnikov_wb":
        roots = [
            r"C:\YandexDisk\СЕТЕВЫЕ МАГАЗИНЫ\WILDBERRIES\ИП ПРУДНИКОВ\ОТПРАВКИ",
            r"E:\Мой диск\ИП ПРУДНИКОВ\ОТПРАВКИ",
        ]
        for r in roots:
            if os.path.exists(r):
                root = r
                break
        if not root:
            root = roots[0]
    return {
        "root": root,
        "mode": "new",
        "price_mode": "cost",
        "counterparty_id": 1,
        "sheet": "ОПРЕДЛАРТ",
        "skip_archive": True,
    }


def get_settings(db: Session, cabinet_id: int) -> Dict[str, Any]:
    cab = cabinet_service.cabinet_by_id(db, cabinet_id)
    if cab is None:
        raise ValueError("Кабинет не найден")
    s = _default_settings_for(cab)
    raw = getattr(cab, "warehouse_settings", "") or ""
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                s.update(data)
        except Exception:
            pass
    if s.get("counterparty_id") is None:
        s["counterparty_id"] = 1
    return s


def save_settings(db: Session, cabinet_id: int, settings: Dict[str, Any]) -> Dict[str, Any]:
    cab = cabinet_service.cabinet_by_id(db, cabinet_id)
    if cab is None:
        raise ValueError("Кабинет не найден")
    cur = get_settings(db, cabinet_id)
    cur.update(settings or {})
    cab.warehouse_settings = json.dumps(cur, ensure_ascii=False)
    db.commit()
    db.refresh(cab)
    return cur


def _parse_folder_name(name: str) -> Tuple[Optional[date], str, str]:
    m = NAME_RE.match(name)
    if not m:
        return None, "", "bad_name"
    d = int(m.group(2))
    mo = int(m.group(3))
    y = m.group(4)
    note = (m.group(5) or "").strip()
    try:
        yy = int(y)
        if yy < 100:
            yy += 2000
        elif yy < 1000:
            return None, note, "bad_name"
        dt = date(yy, mo, d)
        return dt, note, "ok"
    except Exception:
        return None, note, "bad_name"


def _find_db_file(folder: Path) -> Tuple[Optional[Path], Optional[str], List[str]]:
    try:
        entries = list(folder.iterdir())
    except Exception:
        return None, None, []
    candidates = []
    for e in entries:
        if e.is_file() and e.name.lower().startswith("db") and e.name.lower().endswith(".xlsm"):
            if e.name.startswith("~$"):
                continue
            candidates.append(e)
    if not candidates:
        return None, None, []
    candidates_sorted = sorted(candidates, key=lambda p: (p.stat().st_mtime_ns, p.name))
    chosen = candidates_sorted[-1]
    alts = [p.name for p in candidates_sorted if p != chosen]
    warning = None
    if len(candidates) > 1:
        warning = f"Несколько DB*.xlsm: выбрано {chosen.name}, альтернативы: {', '.join(alts)}"
    return chosen, warning, alts


def scan_folders(db: Session, cabinet_id: int) -> Dict[str, Any]:
    cab = cabinet_service.cabinet_by_id(db, cabinet_id)
    if cab is None:
        raise ValueError("Кабинет не найден")
    s = get_settings(db, cabinet_id)
    root = Path(s.get("root") or "")
    res = []
    if not str(root) or not root.exists():
        raise ValueError("Путь импорта не найден")
    skip_archive = bool(s.get("skip_archive", True))
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        name = entry.name
        if skip_archive and ("архив" in name.lower()):
            continue
        dt, note, st = _parse_folder_name(name)
        if st == "bad_name":
            res.append({"folder": name, "status": "bad_name", "date": dt.isoformat() if dt else None, "note": note})
            continue
        fpath, warning, alts = _find_db_file(entry)
        if not fpath:
            res.append({"folder": name, "status": "no_file", "date": dt.isoformat() if dt else None, "note": note})
            continue
        src_ref = f"{cab.code}:{name}"
        exists = db.query(models.WarehouseDoc).filter(models.WarehouseDoc.source_ref == src_ref).first() is not None
        if exists:
            res.append({"folder": name, "status": "imported", "date": dt.isoformat() if dt else None, "note": note, "file": fpath.name, "warning": warning})
            continue
        res.append({"folder": name, "status": "new", "date": dt.isoformat() if dt else None, "note": note, "file": fpath.name, "warning": warning})
    summary = {"new": 0, "imported": 0, "bad_name": 0, "no_file": 0, "error": 0}
    for it in res:
        st = it.get("status")
        if st in summary:
            summary[st] += 1
    return {"items": res, "summary": summary, "root": str(root)}


def _read_opredlart(file_path: Path, sheet_name: str = "ОПРЕДЛАРТ") -> pd.DataFrame:
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True, keep_links=False)
    target = None
    for s in wb.sheetnames:
        if s.strip().lower() == sheet_name.strip().lower():
            target = s
            break
    if target is None:
        for s in wb.sheetnames:
            if "опред" in s.lower():
                target = s
                break
    if target is None:
        raise ValueError("Лист ОПРЕДЛАРТ не найден")
    ws = wb[target]
    rows_iter = ws.iter_rows(values_only=True)
    header_row = None
    headers = []
    for r, row in enumerate(rows_iter, start=1):
        normed = [warehouse_service._norm_header(x) if x is not None else "" for x in row]
        if any(h in normed for h in ["артикул", "article"]) and any(h in normed for h in ["кол-во", "количество", "qty", "quantity"]):
            header_row = r
            headers = [str(x).strip() if x is not None else "" for x in row]
            break
        if r >= 12:
            break
    if header_row is None:
        raise ValueError("Не найдена шапка (Артикул/Кол-во)")
    width = len(headers)
    data = []
    for row in rows_iter:
        row = list(row)[:width]
        if len(row) < width:
            row += [None] * (width - len(row))
        if all((x is None or str(x).strip() == "") for x in row):
            continue
        data.append(row)
    return pd.DataFrame(data, columns=headers)


def import_folder(db: Session, cabinet_id: int, folder_name: str, mode: str = "new", dry_run: bool = False) -> Dict[str, Any]:
    cab = cabinet_service.cabinet_by_id(db, cabinet_id)
    if cab is None:
        raise ValueError("Кабинет не найден")
    s = get_settings(db, cabinet_id)
    root = Path(s.get("root") or "")
    folder = root / folder_name
    if not folder.exists() or not folder.is_dir():
        raise ValueError("Папка не найдена")
    dt, note, st = _parse_folder_name(folder_name)
    if st == "bad_name":
        raise ValueError("Некорректное имя папки")
    src_ref = f"{cab.code}:{folder_name}"
    if mode == "new":
        exists = db.query(models.WarehouseDoc).filter(models.WarehouseDoc.source_ref == src_ref).first()
        if exists:
            raise ValueError("Уже импортировано")
    fpath, warning, alts = _find_db_file(folder)
    if not fpath:
        raise ValueError("Файл DB*.xlsm не найден")
    sheet_name = s.get("sheet") or "ОПРЕДЛАРТ"
    df = _read_opredlart(fpath, sheet_name)
    cols_map = warehouse_service._cols_for(df, warehouse_service._DOC_COLS)
    need = {"article", "quantity"}
    missing = need - set(cols_map.keys())
    if missing:
        raise ValueError(f"В файле не найдены колонки: {', '.join(sorted(missing))}")
    art_col = cols_map["article"]
    barcode_col = cols_map.get("barcode")
    size_col = cols_map.get("size")
    qty_col = cols_map["quantity"]
    items_map = {}
    for _, row in df.iterrows():
        art = warehouse_service._clean(row.get(art_col))
        if not art:
            continue
        qty = warehouse_service._clean_num(row.get(qty_col))
        if qty <= 0:
            continue
        barcode = warehouse_service._clean(row.get(barcode_col)) if barcode_col else ""
        size = warehouse_service._clean(row.get(size_col)) if size_col else ""
        key = (barcode, art, size)
        if key not in items_map:
            items_map[key] = {"article": art, "barcode": barcode, "size": size, "quantity": 0.0}
        items_map[key]["quantity"] += qty
    items_list = list(items_map.values())
    cp_id = s.get("counterparty_id") or 1
    price_mode = s.get("price_mode") or "cost"
    doc_date = dt or date.today()
    doc_num = folder_name
    arts = {it["article"] for it in items_list}
    prods = {p.article: p for p in db.query(models.Product).filter(models.Product.article.in_(arts)).all()} if arts else {}
    for it in items_list:
        p = prods.get(it["article"])
        if price_mode == "cost" and p is not None:
            it["price"] = float(p.net_cost or 0)
        else:
            it["price"] = 0.0
        it["amount"] = round(it["quantity"] * it["price"], 2)
    total = round(sum(i["amount"] for i in items_list), 2)
    unmatched = [it["article"] for it in items_list if it["article"] not in prods]
    if dry_run:
        return {"dry_run": True, "doc_date": doc_date.isoformat(), "doc_num": doc_num, "folder": folder_name, "items_count": len(items_list), "total": total, "unmatched": unmatched, "warning": warning}
    doc = db.query(models.WarehouseDoc).filter(models.WarehouseDoc.source_ref == src_ref).first()
    if doc is None:
        doc = models.WarehouseDoc(
            doc_type="shipment",
            doc_num=doc_num,
            doc_date=doc_date,
            counterparty_id=cp_id,
            total=total,
            source="paths",
            source_ref=src_ref,
            cabinet_id=cabinet_id,
            note=note,
        )
        db.add(doc)
        db.flush()
    else:
        doc.doc_type = "shipment"
        doc.doc_num = doc_num
        doc.doc_date = doc_date
        doc.counterparty_id = cp_id
        doc.total = total
        doc.source = "paths"
        doc.cabinet_id = cabinet_id
        doc.note = note
        doc.items.clear()
    names = warehouse_service._article_names(db, arts)
    for it in items_list:
        doc.items.append(models.WarehouseDocItem(
            article=it["article"],
            name=names.get(it["article"], ""),
            barcode=it["barcode"],
            size=it["size"],
            quantity=it["quantity"],
            price=it["price"],
            amount=it["amount"],
        ))
    db.commit()
    return {"ok": True, "doc_id": doc.id, "items_count": len(items_list), "total": total, "unmatched": unmatched, "warning": warning}
