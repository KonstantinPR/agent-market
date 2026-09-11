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
            return 0
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
    df = df.groupby(["date", "article", "warehouse"], as_index=False)["quantity"].sum()
    values = []
    for row in df.to_dict("records"):
        values.append({
            "marketplace_id": mp_id,
            "date": row["date"],
            "article": str(row["article"]).strip(),
            "warehouse": str(row.get("warehouse", "Все")),
            "quantity": int(row["quantity"]),
        })
    ins = insert(models.Stock)
    stmt = ins.on_conflict_do_update(
        index_elements=["marketplace_id", "date", "article", "warehouse"],
        set_={"quantity": ins.excluded.quantity},
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
    for c in ["views", "opens", "adds", "orders", "cancelled"]:
        df[c] = pd.to_numeric(df.get(c, 0), errors="coerce").fillna(0).astype(int)
    for c in ["avg_price", "revenue"]:
        df[c] = pd.to_numeric(df.get(c, 0), errors="coerce").fillna(0)
    df["nm_id"] = df.get("nm_id", "").astype(str)
    df = df.groupby(["date_from", "date_to", "article"], as_index=False).agg({
        "nm_id": "first", "views": "sum", "opens": "sum", "adds": "sum",
        "orders": "sum", "cancelled": "sum", "avg_price": "first", "revenue": "sum",
    })
    values = [dict(r) for r in df.to_dict("records")]
    ins = insert(models.FunnelMetric)
    stmt = ins.on_conflict_do_update(
        index_elements=["date_from", "date_to", "article"],
        set_={c: ins.excluded[c] for c in ["nm_id", "views", "opens", "adds",
                                            "orders", "cancelled", "avg_price", "revenue"]},
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