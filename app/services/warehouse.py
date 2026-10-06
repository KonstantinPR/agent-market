"""Учётная система «Наш склад»: контрагенты, приход/отгрузка, остатки.

Всё завязано на Excel: импорт (пачка документов одним файлом), экспорт,
шаблоны. Остатки считаются как ∑приход − ∑отгрузка (+ начальные = custom_stock).
Себестоимость единицы — взвешенная средняя по приходам (с учётом начального
остатка), одновременно перезаписывается в products.net_cost.
"""

from datetime import datetime
from typing import Optional

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.services.common import like_match
from app.services.excel_io import df_to_excel_stream

CP_TYPE_LABELS = {
    "supplier": "Поставщик",
    "buyer": "Покупатель",
    "marketplace": "Маркетплейс",
    "carrier": "Перевозчик",
    "other": "Другое",
}

DOC_TYPE_LABELS = {"receipt": "Приход", "shipment": "Отгрузка"}

_CP_COLS = {
    "name": ["наименование", "контрагент", "поставщик", "покупатель", "name", "название"],
    "inn": ["инн", "inn"],
    "ctype": ["тип", "вид", "type"],
    "phone": ["телефон", "тел", "phone"],
    "note": ["примечание", "комментарий", "комментарии", "note"],
}

_DOC_COLS = {
    "date": ["дата", "date"],
    "doc_num": ["номер документа", "№ документа", "номер", "№", "документ", "накладная", "номер накладной", "doc_num"],
    "counterparty": ["контрагент", "поставщик", "покупатель", "counterparty", "supplier", "buyer"],
    "article": ["артикул", "article", "articul", "sku", "артикул товара"],
    "quantity": ["кол-во", "количество", "кол", "qty", "quantity", "кол."],
    "price": ["цена", "цена закупки", "закупочная цена", "цена продажи", "продажная цена", "price", "стоимость за ед."],
}

_CP_TYPE_SYN = {
    "поставщик": "supplier",
    "покупатель": "buyer",
    "маркетплейс": "marketplace",
    "маркетплeйс": "marketplace",
    "перевозчик": "carrier",
    "другое": "other",
}


def _norm_header(s) -> str:
    return str(s).strip().lower().replace("ё", "е")


def _match_column(cols, mapping_aliases, canonical) -> Optional[str]:
    norm = {_norm_header(c): c for c in cols}
    for alias in mapping_aliases:
        if alias in norm:
            return norm[alias]
    return None


def _cols_for(df: pd.DataFrame, mapping: dict) -> dict:
    """return {canonical: actual_col}"""
    found = {}
    for canonical, aliases in mapping.items():
        col = _match_column(df.columns, aliases, canonical)
        if col is not None:
            found[canonical] = col
    return found


def _clean(v):
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    return str(v).strip()


def _clean_num(v):
    if v is None or v == "" or (isinstance(v, float) and pd.isna(v)):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".").replace("\u00a0", "").replace(" ", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_date(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, datetime):
        return v.date()
    s = str(v).strip()
    if not s:
        return None
    # excel/pandas теряет время: "2026-09-01 00:00:00"
    s = s.split(" ")[0]
    try:
        return pd.to_datetime(s, dayfirst=s.count(".") > 0).date()
    except (ValueError, TypeError):
        try:
            return pd.to_datetime(s).date()
        except (ValueError, TypeError):
            return None


def _normalize_ctype(raw) -> str:
    s = _clean(raw).lower().replace("ё", "е")
    return _CP_TYPE_SYN.get(s, "other")


def _row_to_item(row_values: dict) -> dict:
    return {
        "article": _clean(row_values.get("article", "")),
        "quantity": _clean_num(row_values.get("quantity", 0)),
        "price": _clean_num(row_values.get("price", 0)),
    }


def import_counterparties(db: Session, df: pd.DataFrame) -> dict:
    """Импорт справочника контрагентов из Excel (upsert по наименованию)."""
    cols = _cols_for(df, _CP_COLS)
    if "name" not in cols:
        raise ValueError("В файле контрагентов не найдена колонка «Наименование»")

    created, updated = 0, 0
    name_col = cols["name"]
    for _, row in df.iterrows():
        name = _clean(row.get(name_col))
        if not name:
            continue
        cp = db.query(models.Counterparty).filter(models.Counterparty.name == name).first()
        values = {
            "inn": _clean(row.get(cols["inn"])) if "inn" in cols else "",
            "ctype": _normalize_ctype(row.get(cols["ctype"])) if "ctype" in cols else "other",
            "phone": _clean(row.get(cols["phone"])) if "phone" in cols else "",
            "note": _clean(row.get(cols["note"])) if "note" in cols else "",
        }
        if cp is None:
            db.add(models.Counterparty(name=name, **values))
            created += 1
        else:
            for k, v in values.items():
                setattr(cp, k, v)
            updated += 1
    db.commit()
    return {"created": created, "updated": updated}


def import_docs(db: Session, df: pd.DataFrame, doc_type: str, source: str = "excel") -> dict:
    """Импорт прихода/отгрузки. Пачка документов одним файлом:
    строки группируются в документы по (дата, № документа, контрагент)."""
    if doc_type not in ("receipt", "shipment"):
        raise ValueError(f"Неизвестный тип документа: {doc_type}")
    cols = _cols_for(df, _DOC_COLS)
    need = {"date", "article", "quantity", "price"}
    missing = need - set(cols.keys())
    if missing:
        raise ValueError(f"В файле не найдены колонки: {', '.join(sorted(missing))}")
    # для прихода контрагент обязателен, для отгрузки допустим без контрагента
    if doc_type == "receipt" and "counterparty" not in cols:
        raise ValueError("В файле прихода не найдена колонка «Контрагент»")

    groups: dict[tuple, list[dict]] = {}
    date_col = cols["date"]
    doc_col = cols["doc_num"] if "doc_num" in cols else None
    cp_col = cols.get("counterparty")
    art_col = cols["article"]
    qty_col = cols["quantity"]
    price_col = cols["price"]

    for _, row in df.iterrows():
        date = _parse_date(row.get(date_col))
        doc_num = _clean(row.get(doc_col)) if doc_col else ""
        cp_name = _clean(row.get(cp_col)) if cp_col else ""
        item = {"article": _clean(row.get(art_col)), "quantity": _clean_num(row.get(qty_col)), "price": _clean_num(row.get(price_col))}
        if not item["article"] or item["quantity"] == 0:
            continue
        key = (date, doc_num, cp_name)
        groups.setdefault(key, []).append(item)

    docs_created = docs_updated = 0
    articles = set()
    for (date, doc_num, cp_name), items in groups.items():
        if date is None:
            date = datetime.now().date()
        cp_id = None
        if cp_name:
            cp = db.query(models.Counterparty).filter(models.Counterparty.name == cp_name).first()
            if cp is None:
                cp = models.Counterparty(name=cp_name)
                db.add(cp)
                db.flush()
            cp_id = cp.id

        doc = (
            db.query(models.WarehouseDoc)
            .filter(
                models.WarehouseDoc.doc_type == doc_type,
                models.WarehouseDoc.doc_num == doc_num,
                models.WarehouseDoc.doc_date == date,
                models.WarehouseDoc.counterparty_id == cp_id,
            )
            .first()
        )
        total = round(sum(i["quantity"] * i["price"] for i in items), 2)
        if doc is None:
            doc = models.WarehouseDoc(
                doc_type=doc_type, doc_num=doc_num, doc_date=date,
                counterparty_id=cp_id, total=total, source=source,
            )
            db.add(doc)
            db.flush()
            docs_created += 1
        else:
            docs_updated += 1
            doc.total = total
            doc.source = source
            doc.items.clear()  # каскад delete-orphan заменяет старые строки

        names = _article_names(db, {i["article"] for i in items})
        for it in items:
            doc.items.append(models.WarehouseDocItem(
                article=it["article"], name=names.get(it["article"], ""),
                quantity=it["quantity"], price=it["price"],
                amount=round(it["quantity"] * it["price"], 2),
            ))
            articles.add(it["article"])

    db.commit()
    if articles:
        recalc_net_cost(db, articles)
    return {"docs_created": docs_created, "docs_updated": docs_updated}


def _article_names(db: Session, articles: set) -> dict:
    if not articles:
        return {}
    rows = db.execute(
        select(models.Product.article, models.Product.name).where(models.Product.article.in_(articles))
    ).all()
    return {a: n for a, n in rows}


def recalc_net_cost(db: Session, articles: Optional[set] = None) -> None:
    """Взвешенная средняя себестоимость по приходам (start = custom_stock).

    cost = (start_qty * start_cost + ∑приход.qty*price) / (start_qty + ∑приход.qty)
    Дальше обновляются products.net_cost, чтобы маржа считалась актуально.
    """
    items = db.query(models.WarehouseDocItem).all()
    if articles is not None:
        items = [i for i in items if i.article in articles]
    groups: dict[str, dict] = {}
    for it in items:
        g = groups.setdefault(it.article, {"recv": 0.0, "recv_sum": 0.0, "ship": 0.0})
        if it.doc.doc_type == "receipt":
            g["recv"] += float(it.quantity)
            g["recv_sum"] += float(it.quantity) * float(it.price)
        else:
            g["ship"] += float(it.quantity)

    start = db.query(models.CustomStock).all()
    for s in start:
        g = groups.setdefault(s.article, {"recv": 0.0, "recv_sum": 0.0, "ship": 0.0})
        g.setdefault("start_qty", 0.0)
        g.setdefault("start_cost", 0.0)
        g["start_qty"] = float(s.quantity)
        g["start_cost"] = float(s.net_cost)

    to_update = []
    for article, g in groups.items():
        start_qty = g.get("start_qty", 0.0)
        recv = g["recv"]
        denom = start_qty + recv
        if denom <= 0:
            continue
        start_cost_total = g.get("start_cost", 0.0) * start_qty
        avg = (start_cost_total + g["recv_sum"]) / denom
        to_update.append((article, round(avg, 2)))

    for article, avg in to_update:
        prod = db.query(models.Product).filter(models.Product.article == article).first()
        if prod is not None:
            prod.net_cost = avg
    db.commit()


def stock_view(db: Session, article_like: str = "") -> list[dict]:
    """Остатки: начальный (custom_stock), приход, отгрузка, баланс, себестоимость."""
    items = db.query(models.WarehouseDocItem).all()
    groups: dict[str, dict] = {}
    for it in items:
        g = groups.setdefault(it.article, {"received": 0.0, "shipped": 0.0})
        if it.doc.doc_type == "receipt":
            g["received"] += float(it.quantity)
            g["srecv_sum"] = g.get("srecv_sum", 0.0) + float(it.quantity) * float(it.price)
        else:
            g["shipped"] += float(it.quantity)

    start = db.query(models.CustomStock).all()
    for s in start:
        g = groups.setdefault(s.article, {"received": 0.0, "shipped": 0.0})
        g["start_qty"] = float(s.quantity)
        g["start_cost"] = float(s.net_cost)

    names = _article_names(db, set(groups.keys())) if groups else {}
    rows = []
    for article, g in groups.items():
        start_qty = g.get("start_qty", 0.0)
        received = g["received"]
        shipped = g["shipped"]
        start_cost_total = g.get("start_cost", 0.0) * start_qty
        denom = start_qty + received
        avg = (start_cost_total + g.get("srecv_sum", 0.0)) / denom if denom > 0 else g.get("start_cost", 0.0)
        balance = start_qty + received - shipped
        rows.append({
            "article": article,
            "name": names.get(article, ""),
            "start_qty": round(start_qty, 3),
            "received": round(received, 3),
            "shipped": round(shipped, 3),
            "balance": round(balance, 3),
            "avg_cost": round(avg, 2),
            "stock_value": round(avg * balance, 2),
        })
    if article_like:
        rows = [r for r in rows
                if like_match(r["article"], article_like)
                or like_match(r["name"], article_like)]
    rows.sort(key=lambda r: (-r["balance"], r["article"]))
    return rows


def turnover_view(db: Session) -> list[dict]:
    """Обороты по контрагентам: приход / отгрузка (кол-во документов и суммы)."""
    docs = db.query(models.WarehouseDoc).all()
    agg: dict[Optional[int], dict] = {}
    for d in docs:
        g = agg.setdefault(d.counterparty_id, {"in_n": 0, "in_sum": 0.0, "out_n": 0, "out_sum": 0.0})
        key = "in" if d.doc_type == "receipt" else "out"
        g[f"{key}_n"] += 1
        g[f"{key}_sum"] += float(d.total or 0)
    names = {cp.id: cp.name for cp in db.query(models.Counterparty).all()}
    rows = []
    for cp_id, g in agg.items():
        rows.append({
            "counterparty": names.get(cp_id, "—"),
            "counterparty_id": cp_id,
            **g,
        })
    rows.sort(key=lambda r: (-(r["in_sum"] + r["out_sum"]), r["counterparty"]))
    return rows


# ---------- Excel: импорт/экспорт/шаблоны ----------

DOC_EXPORT_HEADERS = ["Дата", "№ документа", "Контрагент", "Артикул", "Кол-во", "Цена"]


def docs_dataframe(db: Session, doc_type: str) -> pd.DataFrame:
    rows = []
    for d in db.query(models.WarehouseDoc).filter(models.WarehouseDoc.doc_type == doc_type).order_by(models.WarehouseDoc.doc_date).all():
        cp_name = ""
        if d.counterparty_id:
            cp = db.get(models.Counterparty, d.counterparty_id)
            cp_name = cp.name if cp else ""
        for it in d.items:
            rows.append([d.doc_date, d.doc_num, cp_name, it.article, float(it.quantity), float(it.price)])
    return pd.DataFrame(rows, columns=DOC_EXPORT_HEADERS)


def counterparties_dataframe(db: Session) -> pd.DataFrame:
    rows = [[cp.name, cp.inn, CP_TYPE_LABELS.get(cp.ctype, cp.ctype), cp.phone, cp.note]
            for cp in db.query(models.Counterparty).order_by(models.Counterparty.name).all()]
    return pd.DataFrame(rows, columns=["Наименование", "ИНН", "Тип", "Телефон", "Примечание"])


def docs_template_df() -> pd.DataFrame:
    return pd.DataFrame([
        ["2026-09-01", "ТН-001", "Поставщик ООО «Ромашка»", "1005", 10, 350.00],
        ["2026-09-01", "ТН-001", "Поставщик ООО «Ромашка»", "2007", 5, 720.50],
        ["2026-09-05", "ТН-014", "Wildberries", "1005", 8, 599.00],
    ], columns=DOC_EXPORT_HEADERS)


def counterparties_template_df() -> pd.DataFrame:
    return pd.DataFrame([
        ["Wildberries", "7707323467", "маркетплейс", "", "WB"],
        ["Ozon", "7728567110", "маркетплейс", "", "Ozon"],
        ["Поставщик ООО «Пример»", "", "поставщик", "+7 900 000-00-00", ""],
    ], columns=["Наименование", "ИНН", "Тип", "Телефон", "Примечание"])


def file_for(kind: str, db: Session, doc_type: str = ""):
    if kind == "counterparties":
        return df_to_excel_stream(counterparties_dataframe(db), "Контрагенты")
    if kind == "template-counterparties":
        return df_to_excel_stream(counterparties_template_df(), "Контрагенты")
    if kind == "template-docs":
        return df_to_excel_stream(docs_template_df(), doc_type or "Документы")
    if kind == "docs":
        return df_to_excel_stream(docs_dataframe(db, doc_type), DOC_TYPE_LABELS.get(doc_type, "Документы"))
    if kind == "stock":
        rows = [[r["article"], r["name"], r["start_qty"], r["received"], r["shipped"], r["balance"], r["avg_cost"], r["stock_value"]]
                for r in stock_view(db)]
        return df_to_excel_stream(pd.DataFrame(rows, columns=[
            "Артикул", "Наименование", "Начальный остаток", "Приход", "Отгрузка",
            "Остаток", "Себестоимость ед.", "Стоимость остатка"]), "Остатки")
    if kind == "turnover":
        rows = [[r["counterparty"], r["in_n"], r["in_sum"], r["out_n"], r["out_sum"]]
                for r in turnover_view(db)]
        return df_to_excel_stream(pd.DataFrame(rows, columns=[
            "Контрагент", "Приход, док.", "Приход, сумма", "Отгрузка, док.", "Отгрузка, сумма"]), "Обороты")
    raise ValueError(f"Неизвестный экспорт: {kind}")


def delete_doc(db: Session, doc_id: int) -> None:
    doc = db.get(models.WarehouseDoc, doc_id)
    if doc is None:
        raise ValueError("Документ не найден")
    articles = {it.article for it in doc.items}
    db.delete(doc)
    db.commit()
    if articles:
        recalc_net_cost(db, articles)


def create_doc(db: Session, doc_type: str, doc_num: str, doc_date, counterparty_id, note: str, items: list[dict], source: str = "ui") -> dict:
    """Создание документа из UI/JSON. items: [{article, quantity, price}]."""
    if doc_type not in ("receipt", "shipment"):
        raise ValueError(f"Неизвестный тип документа: {doc_type}")
    if not items:
        raise ValueError("Документ должен содержать хотя бы одну строку")
    doc = models.WarehouseDoc(
        doc_type=doc_type, doc_num=doc_num, doc_date=doc_date,
        counterparty_id=counterparty_id, note=note, total=0, source=source,
    )
    db.add(doc)
    db.flush()
    names = _article_names(db, {i["article"] for i in items})
    total = 0.0
    for i in items:
        qty = float(i.get("quantity", 0) or 0)
        price = float(i.get("price", 0) or 0)
        total += qty * price
        db.add(models.WarehouseDocItem(
            doc_id=doc.id, article=i["article"], name=names.get(i["article"], ""),
            quantity=qty, price=price, amount=round(qty * price, 2),
        ))
    doc.total = round(total, 2)
    db.commit()
    recalc_net_cost(db, {i["article"] for i in items})
    return {"id": doc.id, "total": doc.total}