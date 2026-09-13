"""Синхронизация данных провайдеров в PostgreSQL (upsert-логика)."""
from typing import Optional

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app import models
from app.database import SessionLocal

NUMERIC_COLUMNS = ["quantity", "returns_qty", "revenue", "commission",
                   "logistics", "storage", "services", "income"]


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_ozon_realization(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Приводит строки реализации Ozon (см. OZON_RU_COLUMNS) к схеме sales."""
    if df is None or df.empty:
        return None
    need = {"date", "offer_id", "quantity", "seller_price", "income", "commission"}
    if not need.issubset(df.columns):
        return None

    def num(col, default=0):
        if col in df.columns:
            return pd.to_numeric(df[col], errors="coerce").fillna(default)
        return pd.Series(default, index=df.index)

    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"], errors="coerce").dt.date,
        "article": df["offer_id"].astype(str).str.strip(),
        "quantity": num("quantity").astype(int),
        "returns_qty": num("returns_qty").astype(int),
        "revenue": num("seller_price"),
        "income": num("income"),
        "commission": num("commission"),
        "services": 0,
        "logistics": 0,
        "storage": 0,
    })
    out = out.dropna(subset=["date"])
    out = out[out["article"] != ""]
    return out


def _norm_num(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    df = df.copy()
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def marketplace_id(db, code: str) -> int:
    return db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == code)
    ).scalar_one()


# Маппинг колонок WB-отчёта v5 (reportDetailByPeriod) на схему sales
V5_TO_SALES = {
    "date": "sale_dt", "article": "sa_name", "quantity": "quantity",
    "returns_qty": "return_amount", "revenue": "retail_amount",
    "income": "ppvz_for_pay", "commission": "ppvz_sales_commission",
    "logistics": "delivery_rub", "storage": "storage_fee",
    "services": None,  # сумма корректировок/удержаний
}

# Маппинг колонок маппинг статистики (statistics-api, snake_case) на схему sales
STAT_TO_SALES = {
    "date": "date", "article": "supplierArticle", "quantity": "quantity",
    "returns_qty": "returnedAmount", "revenue": "totalPrice",
    "income": "forPay", "commission": "commission",
    "logistics": "logistics", "storage": "storage", "services": "services",
}

# Маппинг колонок финансового отчёта (finance-api detailed) на схему sales
DETAIL_TO_SALES = {
    "date": "saleDt", "article": "vendorCode", "quantity": "quantity",
    "returns_qty": "returnedAmount", "revenue": "retailAmount",
    "income": "forPay", "commission": "ppvzSalesCommission",
    "logistics": "deliveryService", "storage": "paidStorage",
    "services": None,  # штрафы + удержания + корректировки
}


def _first_col(df: pd.DataFrame, names):
    return next((c for c in names if c in df.columns), None)


def normalize_wb_sales(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Приводит реальные WB-отчёты (v5 или финансовый) к схеме sales."""
    if df is None or df.empty:
        return None

    if "sa_name" in df.columns or "sale_dt" in df.columns:
        mapping, date_col = V5_TO_SALES, "sale_dt"
        svc_cols = ["additional_payment", "penalty", "deduction"]
    elif "supplierArticle" in df.columns and "date" in df.columns:
        mapping, date_col = STAT_TO_SALES, "date"
        svc_cols = ["services"]
    elif "vendorCode" in df.columns and "saleDt" in df.columns:
        mapping, date_col = DETAIL_TO_SALES, "saleDt"
        svc_cols = ["additionalPayment", "penalty", "deduction"]
    else:
        return None

    if date_col not in df.columns:
        return None

    def col(name):
        src = mapping[name]
        if src is None or src not in df.columns:
            if name == "services":
                parts = []
                for c in svc_cols:
                    parts.append(pd.to_numeric(df[c], errors="coerce").fillna(0) if c in df.columns else 0)
                return sum(parts[1:], parts[0]) if parts else 0
            return pd.Series(0.0, index=df.index)
        return pd.to_numeric(df[src], errors="coerce").fillna(0)

    out = pd.DataFrame({
        "date": df[date_col],
        "article": df[mapping["article"]].astype(str).str.strip(),
        "quantity": col("quantity").astype(int),
        "returns_qty": col("returns_qty").astype(int),
        "revenue": col("revenue"),
        "commission": col("commission"),
        "logistics": col("logistics"),
        "storage": col("storage"),
        "services": col("services"),
        "income": col("income"),
    })
    out = out[out["article"] != ""]
    return out


# Алиасы колонок строк «Детализации продаж» WB (Excel ЛК / finance-API) -> схема wb_detail_rows
DETAIL_FIELD_ALIASES = {
    "report_id": ["reportId", "realizationreport_id", "realizationsreport_id", "Номер отчёта"],
    "rrd_id": ["rrdId", "rrd_id", "ID строки"],
    "gi_id": ["giId", "gi_id", "ID поставки"],
    "nm_id": ["nmId", "nm_id", "Артикул WB"],
    "article": ["vendorCode", "sa_name", "article", "Артикул поставщика", "Артикул продавца"],
    "brand": ["brandName", "brand", "Бренд"],
    "title": ["title", "Название товара"],
    "tech_size": ["techSize", "ts_name", "size", "Размер"],
    "sku": ["sku", "barcode", "Баркод", "ШК", "ШК (shk_id)"],
    "doc_type_name": ["docTypeName", "doc_type_name", "Тип документа"],
    "quantity": ["quantity", "Кол-во"],
    "retail_price": ["retailPrice", "retail_price", "Цена розничная"],
    "retail_amount": ["retailAmount", "retail_amount", "Вайлдберриз реализовал Товар (Пр)"],
    "commission_percent": ["commissionPercent", "commission_percent", "Размер кВВ, %"],
    "office_name": ["officeName", "office_name", "Склад"],
    "sale_dt": ["saleDt", "sale_dt", "Дата продажи"],
    "order_dt": ["orderDt", "order_dt", "Дата заказа покупателем"],
    "ppvz_sales_commission": [
        "ppvzSalesCommission", "ppvz_sales_commission",
        "Вознаграждение с продаж до вычета услуг поверенного, без НДС",
    ],
    "for_pay": ["forPay", "for_pay", "К перечислению Продавцу за реализованный Товар"],
    "delivery_service": [
        "deliveryService", "delivery_rub",
        "Услуги по доставке товара покупателю", "Услуги по доставке товара покупателю (квВВ)",
    ],
    "paid_storage": ["paidStorage", "storage_fee", "Хранение (пр)", "Хранение"],
    "penalty": ["penalty", "Штраф"],
    "deduction": ["deduction", "Удержанный штраф"],
    "additional_payment": ["additionalPayment", "additional_payment", "Корректировка ВВ"],
    "rebill_logistic_cost": [
        "rebillLogisticCost", "rebill_logistic_cost",
        "Возмещение издержек по перевозке/складским операциям",
    ],
    "srid": ["srid", "Srid", "SRID"],
    "order_uid": ["orderUid", "order_uid", "Id корзины заказа"],
}

DETAIL_TEXT_COLS = ["report_id", "rrd_id", "gi_id", "nm_id", "article", "brand", "title",
                    "tech_size", "sku", "doc_type_name", "office_name", "srid", "order_uid"]
DETAIL_NUM_COLS = ["quantity", "retail_price", "retail_amount", "commission_percent",
                   "ppvz_sales_commission", "for_pay", "delivery_service", "paid_storage",
                   "penalty", "deduction", "additional_payment", "rebill_logistic_cost"]
DETAIL_DATE_COLS = ["sale_dt", "order_dt"]


def normalize_wb_detail(df: pd.DataFrame, source: str = "excel") -> Optional[pd.DataFrame]:
    """Приводит «Детализацию продаж» WB (Excel ЛК или finance-API) к схеме wb_detail_rows.

    op_key: srid -> 'sr:<srid>'; иначе report_id:rrd_id -> 'rr:<report_id>:<rrd_id>'
    (для API rrd_id гарантирован пагинацией); иначе rrd_id; иначе 'row:<n>'.
    Служебные строки без идентификатора и без артикула отбрасываются.
    """
    if df is None or df.empty:
        return None
    out = {}
    idx = df.index
    for field, aliases in DETAIL_FIELD_ALIASES.items():
        c = _pick_col(df, aliases)
        if c is None:
            if field in DETAIL_NUM_COLS:
                out[field] = pd.Series(0.0, index=idx)
            elif field in DETAIL_DATE_COLS:
                out[field] = pd.Series(pd.NaT, index=idx)
            else:
                out[field] = pd.Series("", index=idx)
            continue
        col = df[c]
        if field in DETAIL_NUM_COLS:
            out[field] = pd.to_numeric(col, errors="coerce").fillna(0)
        elif field in DETAIL_DATE_COLS:
            out[field] = pd.to_datetime(col, errors="coerce")
        else:
            out[field] = col.fillna("").astype(str).str.strip()
    ndf = pd.DataFrame(out)
    ndf["quantity"] = pd.to_numeric(ndf["quantity"], errors="coerce").fillna(0).astype(int)

    rows_out = []
    for i, r in enumerate(ndf.to_dict("records")):
        srid = str(r["srid"] or "").strip()
        rrd = str(r["rrd_id"] or "").strip()
        report = str(r["report_id"] or "").strip()
        article = str(r["article"] or "").strip()
        if srid:
            key = "sr:" + srid
        elif rrd and (report or source == "api"):
            key = "rr:" + report + ":" + rrd
        elif rrd:
            key = "rr:" + rrd
        elif article:
            key = "row:" + str(i)
        else:
            continue  # служебная строка без id и без артикула
        r["op_key"] = key
        r["article"] = article
        rows_out.append(r)
    if not rows_out:
        return None
    ndf = pd.DataFrame(rows_out)
    return ndf


def upsert_wb_detail_rows(db, df: pd.DataFrame, source: str = "excel") -> int:
    """Строки детализации WB -> wb_detail_rows, upsert по op_key (идемпотентно)."""
    if df is None or df.empty:
        return 0
    values = []
    for r in df.to_dict("records"):
        values.append({
            "op_key": str(r["op_key"]),
            "source": source,
            "report_id": str(r.get("report_id", "") or ""),
            "rrd_id": str(r.get("rrd_id", "") or ""),
            "gi_id": str(r.get("gi_id", "") or ""),
            "nm_id": str(r.get("nm_id", "") or ""),
            "article": str(r.get("article", "") or ""),
            "brand": str(r.get("brand", "") or ""),
            "title": str(r.get("title", "") or ""),
            "tech_size": str(r.get("tech_size", "") or ""),
            "sku": str(r.get("sku", "") or ""),
            "doc_type_name": str(r.get("doc_type_name", "") or ""),
            "quantity": int(_f(r.get("quantity", 0))),
            "retail_price": _f(r.get("retail_price")),
            "retail_amount": _f(r.get("retail_amount")),
            "commission_percent": _f(r.get("commission_percent")),
            "office_name": str(r.get("office_name", "") or ""),
            "sale_dt": _as_date(r.get("sale_dt")),
            "order_dt": _as_date(r.get("order_dt")),
            "ppvz_sales_commission": _f(r.get("ppvz_sales_commission")),
            "for_pay": _f(r.get("for_pay")),
            "delivery_service": _f(r.get("delivery_service")),
            "paid_storage": _f(r.get("paid_storage")),
            "penalty": _f(r.get("penalty")),
            "deduction": _f(r.get("deduction")),
            "additional_payment": _f(r.get("additional_payment")),
            "rebill_logistic_cost": _f(r.get("rebill_logistic_cost")),
            "srid": str(r.get("srid", "") or ""),
            "order_uid": str(r.get("order_uid", "") or ""),
        })
    ins = insert(models.WbDetailRow)
    set_cols = [c for c in values[0].keys() if c != "op_key"]
    stmt = ins.on_conflict_do_update(
        index_elements=["op_key"],
        set_={c: ins.excluded[c] for c in set_cols},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def _is_return_row(doc_type: str) -> bool:
    s = str(doc_type or "").lower()
    return "возврат" in s or "return" in s


def rebuild_sales_from_detail(db, sale_from=None, sale_to=None, source: str = "detail") -> int:
    """Пересчитывает продажи source='detail' из wb_detail_rows (сумма по дата+артикул).

    Продажи = строки не-возвратов, возвраты — отдельная колонка; финансовые
    поля суммируются по всем строкам (WB передаёт их со знаком).
    """
    q = select(models.WbDetailRow)
    if sale_from is not None:
        q = q.where(models.WbDetailRow.sale_dt >= sale_from)
    if sale_to is not None:
        q = q.where(models.WbDetailRow.sale_dt <= sale_to)
    rows = db.execute(q).scalars().all()
    if not rows:
        return 0
    recs = []
    for r in rows:
        if r.sale_dt is None or not r.article:
            continue
        is_ret = _is_return_row(r.doc_type_name)
        recs.append({
            "date": r.sale_dt,
            "article": r.article,
            "quantity": r.quantity if not is_ret else 0,
            "returns_qty": r.quantity if is_ret else 0,
            "revenue": r.retail_amount if not is_ret else 0,
            "commission": _f(r.ppvz_sales_commission),
            "logistics": _f(r.delivery_service),
            "storage": _f(r.paid_storage),
            "services": _f(r.penalty) + _f(r.deduction) + _f(r.additional_payment),
            "income": _f(r.for_pay),
        })
    if not recs:
        return 0
    return upsert_sales(db, pd.DataFrame(recs), "wb", source=source)


def _pick_col(df: pd.DataFrame, names) -> Optional[str]:
    return next((c for c in names if c in df.columns), None)


def _as_date(v):
    """Надёжно приводит значение к datetime.date (None/''/NaT -> None)."""
    if v is None or v == "":
        return None
    t = pd.to_datetime(v, errors="coerce")
    return t.date() if pd.notna(t) else None


def upsert_products(db, df: pd.DataFrame) -> int:
    existing = set(db.execute(select(models.Product.article)).scalars().all())
    n = 0
    seen = set()
    for row in df.to_dict("records"):
        art = str(row.get("article", "")).strip()
        if not art:
            continue
        if art in seen:
            continue
        seen.add(art)
        n += 1
        if art in existing:
            rec = db.get(models.Product, art)
            rec.name = str(row.get("name", rec.name))
            rec.brand = str(row.get("brand", rec.brand))
            rec.barcode = str(row.get("barcode", rec.barcode))
            rec.net_cost = _f(row.get("net_cost", rec.net_cost))
        else:
            db.add(models.Product(
                article=art,
                name=str(row.get("name", "")),
                brand=str(row.get("brand", "")),
                barcode=str(row.get("barcode", "")),
                net_cost=_f(row.get("net_cost", 0)),
            ))
    db.commit()
    return n


# Карты колонок (Excel-выгрузка ЛК WB + API WB) на схему marketplace_cards
CARD_RENAME = {
    "Код размера (chrt_id)": "chrt_id", "Код размера": "chrt_id", "chrt_id": "chrt_id",
    "chrtId": "chrt_id",
    "Артикул WB": "nm_id", "Артикул WB (nmID)": "nm_id", "nm_id": "nm_id", "nmID": "nm_id",
    "Артикул продавца": "vendor_code", "vendor_code": "vendor_code", "vendorCode": "vendor_code",
    "Артикул": "vendor_code",
    "Бренд": "brand", "brand": "brand",
    "Предмет": "subject", "subject": "subject",
    "Размер": "size", "size": "size", "techSize": "size",
    "Баркод": "barcode", "barcode": "barcode", "barcodes": "barcode", "skus": "barcode",
    "Объем, л.": "volume_l", "Объём, л.": "volume_l", "volume_l": "volume_l",
    "Состав": "composition", "composition": "composition",
    "Наименование": "name", "Название товара": "name", "title": "name",
    "name": "name",
}

CARD_TEXT_COLS = ["chrt_id", "nm_id", "vendor_code", "brand", "subject", "size",
                  "barcode", "composition", "name"]


def _first_scalar(x, default=""):
    if isinstance(x, (list, tuple)):
        return str(x[0]) if x else default
    return x


def normalize_marketplace_cards(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Приводит карточки товара WB (Excel ЛК или API) к схеме marketplace_cards."""
    if df is None or df.empty:
        return None
    df = df.rename(columns={k: v for k, v in CARD_RENAME.items() if k in df.columns})
    for col in CARD_TEXT_COLS:
        if col not in df.columns:
            df[col] = ""
    if "volume_l" not in df.columns:
        df["volume_l"] = 0.0
    df = df[CARD_TEXT_COLS + ["volume_l"]].copy()
    if "barcode" in df.columns:
        df["barcode"] = df["barcode"].map(_first_scalar)
    df["volume_l"] = pd.to_numeric(
        df["volume_l"].astype(str).str.replace(",", ".", regex=False),
        errors="coerce",
    ).fillna(0.0)
    for col in CARD_TEXT_COLS:
        df[col] = df[col].fillna("").astype(str).str.strip()
    df = df.drop_duplicates(subset=["chrt_id", "vendor_code", "barcode"], keep="first")
    df = df[df["chrt_id"] != ""]
    return df


def upsert_marketplace_cards(db, df: pd.DataFrame, code: str) -> int:
    """Карточки маркетплейса (marketplace_cards), upsert по (marketplace_id, chrt_id)."""
    if df is None or df.empty:
        return 0
    mp = marketplace_id(db, code)
    values = []
    for r in df.to_dict("records"):
        values.append({
            "marketplace_id": mp,
            "chrt_id": str(r["chrt_id"]),
            "nm_id": str(r["nm_id"]),
            "vendor_code": str(r["vendor_code"]),
            "brand": str(r["brand"]),
            "subject": str(r["subject"]),
            "size": str(r["size"]),
            "barcode": str(r["barcode"]),
            "volume_l": float(r.get("volume_l") or 0),
            "composition": str(r["composition"]),
            "name": str(r["name"]),
        })
    ins = insert(models.MarketplaceCard)
    stmt = ins.on_conflict_do_update(
        index_elements=["marketplace_id", "chrt_id"],
        set_={c: ins.excluded[c]
              for c in ["nm_id", "vendor_code", "brand", "subject", "size",
                        "barcode", "volume_l", "composition", "name"]},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def refresh_products_from_cards(db, code: str) -> tuple:
    """Обновляет общий каталог (products + nm_articles) из marketplace_cards."""
    mp = marketplace_id(db, code)
    rows = db.execute(
        select(models.MarketplaceCard)
        .where(models.MarketplaceCard.marketplace_id == mp)
        .order_by(models.MarketplaceCard.imported_at.asc(), models.MarketplaceCard.id.asc())
    ).scalars().all()
    if not rows:
        return 0, 0
    pdf = pd.DataFrame([{
        "article": r.vendor_code,
        "name": r.name,
        "brand": r.brand,
        "barcode": r.barcode,
    } for r in rows])
    pdf = pdf[pdf["article"] != ""].drop_duplicates("article")
    npdf = pd.DataFrame([{"nmID": r.nm_id, "vendorCode": r.vendor_code} for r in rows])
    npdf = npdf[(npdf["nmID"] != "") & (npdf["vendorCode"] != "")].drop_duplicates("nmID")
    n_products = upsert_products(db, pdf) if not pdf.empty else 0
    n_nm = upsert_nm_articles(db, npdf) if not npdf.empty else 0
    return n_products, n_nm


def upsert_sales(db, df: pd.DataFrame, code: str, source: str = "v5") -> int:
    if df is None or df.empty:
        return 0
    df = _norm_num(df, NUMERIC_COLUMNS)
    mp_id = marketplace_id(db, code)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df.dropna(subset=["date"])
    df["article"] = df["article"].astype(str).str.strip()
    df = df[df["article"] != ""]
    df = df.groupby(["date", "article"], as_index=False)[NUMERIC_COLUMNS].sum()
    values = []
    for row in df.to_dict("records"):
        values.append({
            "marketplace_id": mp_id,
            "date": row["date"],
            "article": str(row["article"]),
            "source": source,
            "quantity": int(row["quantity"]),
            "returns_qty": int(row["returns_qty"]),
            "revenue": float(row["revenue"]),
            "commission": float(row["commission"]),
            "logistics": float(row["logistics"]),
            "storage": float(row["storage"]),
            "services": float(row["services"]),
            "income": float(row["income"]),
        })
    ins = insert(models.Sale)
    stmt = ins.on_conflict_do_update(
        index_elements=["marketplace_id", "date", "article", "source"],
        set_={c: ins.excluded[c]
              for c in ["quantity", "returns_qty", "revenue", "commission",
                        "logistics", "storage", "services", "income"]},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def upsert_stocks(db, df: pd.DataFrame, code: str) -> int:
    df = _norm_num(df, ["quantity"])
    mp_id = marketplace_id(db, code)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ["chrt_id", "size", "barcode"]:
        if c not in df.columns:
            df[c] = ""
    df = df.groupby(["date", "article", "warehouse", "chrt_id"], as_index=False).agg({
        "quantity": "sum", "size": "first", "barcode": "first",
    })
    values = []
    for row in df.to_dict("records"):
        values.append({
            "marketplace_id": mp_id,
            "date": row["date"],
            "article": str(row["article"]).strip(),
            "warehouse": str(row.get("warehouse", "Все")).strip(),
            "chrt_id": str(row.get("chrt_id", "")).strip(),
            "size": str(row.get("size", "")).strip(),
            "barcode": str(row.get("barcode", "")).strip(),
            "quantity": int(row["quantity"]),
        })
    ins = insert(models.Stock)
    stmt = ins.on_conflict_do_update(
        index_elements=["marketplace_id", "date", "article", "warehouse", "chrt_id"],
        set_={"quantity": ins.excluded.quantity,
              "size": ins.excluded.size, "barcode": ins.excluded.barcode},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def upsert_custom_stock(db, df: pd.DataFrame) -> int:
    n = 0
    for row in df.to_dict("records"):
        art = str(row.get("article", "")).strip()
        if not art:
            continue
        n += 1
        db.merge(models.CustomStock(
            article=art,
            quantity=int(_f(row.get("quantity"))),
            net_cost=_f(row.get("net_cost")),
        ))
    db.commit()
    return n


def upsert_funnel(db, df: pd.DataFrame) -> int:
    """Пишет воронку продаж WB в funnel_metric (upsert по период+артикул)."""
    if df is None or df.empty:
        return 0
    need = {"date_from", "date_to", "article"}
    if not need.issubset(df.columns):
        return 0
    df = df.copy()
    df["date_from"] = pd.to_datetime(df["date_from"], errors="coerce").dt.date
    df["date_to"] = pd.to_datetime(df["date_to"], errors="coerce").dt.date
    df = df.dropna(subset=["date_from", "date_to"])
    df["article"] = df["article"].astype(str).str.strip()
    df = df[df["article"] != ""]
    if df.empty:
        return 0
    for c in ["views", "opens", "adds", "orders", "cancelled", "buyouts"]:
        df[c] = pd.to_numeric(df.get(c, 0), errors="coerce").fillna(0).astype(int)
    for c in ["avg_price", "revenue", "buyout_sum"]:
        df[c] = pd.to_numeric(df.get(c, 0), errors="coerce").fillna(0)
    df["nm_id"] = df.get("nm_id", "").astype(str)
    df = df.groupby(["date_from", "date_to", "article"], as_index=False).agg({
        "nm_id": "first", "views": "sum", "opens": "sum", "adds": "sum",
        "orders": "sum", "cancelled": "sum", "buyouts": "sum",
        "avg_price": "first", "revenue": "sum", "buyout_sum": "sum",
    })
    values = [dict(r) for r in df.to_dict("records")]
    ins = insert(models.FunnelMetric)
    stmt = ins.on_conflict_do_update(
        index_elements=["date_from", "date_to", "article"],
        set_={c: ins.excluded[c] for c in ["nm_id", "views", "opens", "adds",
                                            "orders", "cancelled", "buyouts",
                                            "avg_price", "revenue", "buyout_sum"]},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def upsert_nm_articles(db, df: pd.DataFrame) -> int:
    """Карта nmID -> артикул из карточек товара WB."""
    if df is None or df.empty:
        return 0
    if "nmID" not in df.columns or "vendorCode" not in df.columns:
        return 0
    pairs = (
        df[["nmID", "vendorCode"]].dropna()
        .assign(
            nm_id=lambda d: d["nmID"].astype(str).str.strip(),
            article=lambda d: d["vendorCode"].astype(str).str.strip(),
        )
        .drop_duplicates("nm_id")
    )
    pairs = pairs[(pairs["nm_id"] != "") & (pairs["article"] != "")]
    if pairs.empty:
        return 0
    values = [{"nm_id": r.nm_id, "article": r.article} for r in pairs.itertuples()]
    ins = insert(models.NmArticle)
    stmt = ins.on_conflict_do_update(
        index_elements=["nm_id"],
        set_={"article": ins.excluded.article, "updated_at": func.now()},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def upsert_price_snapshots(db, df: pd.DataFrame) -> int:
    """Снимок цен/скидок WB -> price_snapshots (upsert по article+size)."""
    if df is None or df.empty:
        return 0
    if "vendorCode" not in df.columns:
        return 0
    df = df.copy()

    def col(name, default=""):
        return df[name] if name in df.columns else pd.Series(default, index=df.index)

    def num(name, default=0.0):
        return pd.to_numeric(col(name, default), errors="coerce").fillna(default)

    df = df.assign(
        article=col("vendorCode").astype(str).str.strip(),
        nm_id=col("nmID").astype(str).str.strip(),
        size=col("size", col("techSize", col("techSizeName"))).astype(str).str.strip(),
        price=num("price"),
        discounted_price=num("discountedPrice", 0) if "discountedPrice" in df.columns else num("price"),
        discount=num("discount"),
    )
    df = df.groupby(["article", "size"], as_index=False).agg({
        "nm_id": "first", "price": "first",
        "discounted_price": "first", "discount": "first",
    })
    values = [dict(r) for r in df.to_dict("records")]
    ins = insert(models.PriceSnapshot)
    stmt = ins.on_conflict_do_update(
        index_elements=["article", "size"],
        set_={c: ins.excluded[c] for c in ["nm_id", "price", "discounted_price", "discount"]},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def upsert_storage_costs(db, df: pd.DataFrame) -> int:
    """Стоимость хранения WB -> storage_costs (upsert по nm_id)."""
    if df is None or df.empty:
        return 0
    if "nmId" not in df.columns:
        return 0
    df = df.copy()

    def col(name, default=""):
        return df[name] if name in df.columns else pd.Series(default, index=df.index)

    def num(name, default=0):
        return pd.to_numeric(col(name, default), errors="coerce").fillna(default)

    df = df.assign(
        nm_id=col("nmId").astype(str).str.strip(),
        article=col("vendorCode").astype(str).str.strip(),
        barcodes_count=num("barcodesCount", 0).astype(int),
        volume=num("volume"),
        storage_price=num("storagePricePerBarcode", 0),
        warehouse_price=num("warehousePrice", 0),
    )
    df = df.groupby("nm_id", as_index=False).agg({
        "article": "first", "barcodes_count": "mean", "volume": "mean",
        "storage_price": "mean", "warehouse_price": "mean",
    })
    values = [dict(r) for r in df.to_dict("records")]
    ins = insert(models.StorageCost)
    stmt = ins.on_conflict_do_update(
        index_elements=["nm_id"],
        set_={c: ins.excluded[c]
              for c in ["article", "barcodes_count", "volume", "storage_price", "warehouse_price"]},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def record_api_pull(db, api: str, kind: str, rows: int, db_rows: int, window: str = "") -> None:
    """Обновляет лог последней успешной загрузки из API (api+kind)."""
    ins = insert(models.ApiPull)
    stmt = ins.on_conflict_do_update(
        index_elements=["api", "kind"],
        set_={
            "last_success_at": func.now(),
            "rows": ins.excluded.rows,
            "db_rows": ins.excluded.db_rows,
            "window": ins.excluded.window,
        },
    )
    db.execute(stmt, {"api": api, "kind": kind, "rows": int(rows),
                      "db_rows": int(db_rows), "window": window})
    db.commit()


def sync_sales_window(code: str, date_from, date_to) -> dict:
    """Вытягивает продажи у провайдера и пишет в БД. Возвращает статистику."""
    from app.providers.ozon import OzonProvider
    from app.providers.wb import WbProvider

    with SessionLocal() as db:
        if code == "wb":
            df = WbProvider().get_sales_realization(date_from, date_to)
            norm = normalize_wb_sales(df)
            count = upsert_sales(db, norm, "wb") if norm is not None else 0
        else:
            df = OzonProvider().get_sales(date_from, date_to)
            norm = normalize_ozon_realization(df)
            count = upsert_sales(db, norm, "ozon", source="ozon") if norm is not None else 0
    return {"marketplace": code, "rows": int(count), "from": str(date_from), "to": str(date_to)}