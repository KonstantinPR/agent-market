"""Синхронизация данных провайдеров в PostgreSQL (upsert-логика)."""
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app import models
from app.database import SessionLocal
from app.services import ozon_article

NUMERIC_COLUMNS = ["quantity", "returns_qty", "revenue", "commission",
                   "logistics", "storage", "services", "income"]


def _f(value, default=0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return v


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


def normalize_ozon_detail(df: pd.DataFrame, source: str = "api") -> Optional[pd.DataFrame]:
    """Приводит строки детализации реализаций Ozon к схеме ozon_detail_rows.

    Источники: прямой метод /v1/finance/realization/posting и разобранный
    фолбэк-отчёт /v1/report/realization/posting — оба отдают одинаковый набор
    колонок (date, posting_number, offer_id, name, sku, barcode, quantity,
    seller_price, amount, commission_ratio, commission, standard_fee, income,
    return_qty, return_total). op_key = дата|постинг|sku одинаков для обоих
    источников, поэтому повторные загрузки идемпотентно перезаписывают строки.
    """
    if df is None or df.empty:
        return None
    need = {"date", "posting_number", "offer_id", "sku"}
    if not need.issubset(df.columns):
        return None
    df = df.copy()
    dates = pd.to_datetime(df["date"], errors="coerce")

    def txt(col, default=""):
        if col not in df.columns:
            return pd.Series(default, index=df.index)
        return df[col].fillna("").astype(str).str.strip()

    def num(col, default=0):
        if col not in df.columns:
            return pd.Series(default, index=df.index)
        return pd.to_numeric(df[col], errors="coerce").fillna(default)

    out = pd.DataFrame({
        "op_key": (dates.dt.strftime("%Y-%m-%d").fillna("") + "|"
                   + txt("posting_number") + "|" + txt("sku")),
        "source": source,
        "date": dates.dt.date,
        "posting_number": txt("posting_number"),
        "offer_id": txt("offer_id"),
        "name": txt("name"),
        "sku": txt("sku"),
        "barcode": txt("barcode"),
        "quantity": num("quantity", 0).astype(int),
        "seller_price": num("seller_price"),
        "amount": num("amount"),
        "commission_ratio": num("commission_ratio"),
        "commission": num("commission"),
        "standard_fee": num("standard_fee"),
        "income": num("income"),
        "return_qty": num("return_qty", 0).astype(int),
        "return_total": num("return_total"),
    })
    out = out.dropna(subset=["date"])
    out = out[(out["offer_id"] != "") & (out["posting_number"] != "")]
    return out


def normalize_ozon_buyout(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Приводит строки выкупов Ozon (/v1/finance/products/buyout) к схеме ozon_buyouts.

    op_key = постинг|sku (даты API не отдаёт) — повторные загрузки
    идемпотентно перезаписывают строки.
    """
    if df is None or df.empty:
        return None
    need = {"posting_number", "offer_id", "sku"}
    if not need.issubset(df.columns):
        return None
    df = df.copy()

    def txt(col, default=""):
        if col not in df.columns:
            return pd.Series(default, index=df.index)
        return df[col].fillna("").astype(str).str.strip()

    def num(col, default=0):
        if col not in df.columns:
            return pd.Series(default, index=df.index)
        return pd.to_numeric(df[col], errors="coerce").fillna(default)

    out = pd.DataFrame({
        "op_key": txt("posting_number") + "|" + txt("sku"),
        "posting_number": txt("posting_number"),
        "offer_id": txt("offer_id"),
        "name": txt("name"),
        "sku": txt("sku"),
        "quantity": num("quantity", 0).astype(int),
        "seller_price": num("seller_price"),
        "buyout_price": num("buyout_price"),
        "amount": num("amount"),
        "deduction_by_category_percent": num("deduction_by_category_percent"),
        "vat_percent": num("vat_percent", 0).astype(int),
    })
    out = out[(out["offer_id"] != "") & (out["posting_number"] != "")]
    return out


def normalize_ozon_placement(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Строки стоимости размещения Ozon -> схема ozon_placements.

    Источник: /v1/report/placement/by-products -> XLSX с колонками
    date, sku, offer_id, warehouse, paid_quantity, paid_volume, storage.
    op_key = дата|sku|склад (повторные загрузки идемпотентно перезаписывают).
    """
    if df is None or df.empty:
        return None
    need = {"date", "sku", "offer_id", "storage"}
    if not need.issubset(df.columns):
        return None
    df = df.copy()
    dates = pd.to_datetime(df["date"], errors="coerce")

    def txt(col, default=""):
        if col not in df.columns:
            return pd.Series(default, index=df.index)
        return df[col].fillna("").astype(str).str.strip()

    def num(col, default=0):
        if col not in df.columns:
            return pd.Series(default, index=df.index)
        return pd.to_numeric(df[col], errors="coerce").fillna(default)

    out = pd.DataFrame({
        "op_key": (dates.dt.strftime("%Y-%m-%d").fillna("") + "|"
                   + txt("sku") + "|" + txt("warehouse", "")),
        "date": dates.dt.date,
        "sku": txt("sku"),
        "offer_id": txt("offer_id"),
        "warehouse": txt("warehouse", ""),
        "paid_quantity": num("paid_quantity", 0).astype(int),
        "paid_volume": num("paid_volume", 0),
        "storage": -num("storage"),
    })
    out = out.dropna(subset=["date"])
    out = out[(out["sku"] != "") & (out["offer_id"] != "")]
    return out


def normalize_ozon_cashflow(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Строки движения средств Ozon -> схема ozon_cash_flows.

    Источник: /v1/finance/cash-flow-statement/list с колонками period_begin,
    period_end, begin_balance, payments_amount, delivery_total, return_total,
    services_total, others_total, end_balance. op_key = period_begin —
    повторные загрузки идемпотентно перезаписывают периоды.
    """
    if df is None or df.empty:
        return None
    need = {"period_begin"}
    if not need.issubset(df.columns):
        return None
    df = df.copy()
    begins = pd.to_datetime(df["period_begin"], errors="coerce")

    def txt(col, default=""):
        if col not in df.columns:
            return pd.Series(default, index=df.index)
        return df[col].fillna("").astype(str).str.strip()

    def num(col, default=0):
        if col not in df.columns:
            return pd.Series(default, index=df.index)
        return pd.to_numeric(df[col], errors="coerce").fillna(default)

    ends = pd.to_datetime(df["period_end"], errors="coerce") if "period_end" in df.columns \
        else pd.Series(pd.NaT, index=df.index)
    out = pd.DataFrame({
        "op_key": begins.dt.strftime("%Y-%m-%d").fillna(""),
        "period_begin": begins.dt.date,
        "period_end": ends.dt.date,
        "begin_balance": num("begin_balance"),
        "payments_amount": num("payments_amount"),
        "delivery_total": num("delivery_total"),
        "return_total": num("return_total"),
        "services_total": num("services_total"),
        "others_total": num("others_total"),
        "end_balance": num("end_balance"),
    })
    out = out.dropna(subset=["period_begin"])
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
    "nm_id": ["nmId", "nm_id", "Артикул WB", "Код номенклатуры"],
    "article": ["vendorCode", "sa_name", "article", "Артикул поставщика", "Артикул продавца"],
    "brand": ["brandName", "brand", "Бренд"],
    "title": ["title", "Название товара"],
    "tech_size": ["techSize", "ts_name", "size", "Размер"],
    "sku": ["sku", "barcode", "Баркод", "ШК", "ШК (shk_id)"],
    "doc_type_name": ["docTypeName", "doc_type_name", "Тип документа", "Обоснование для оплаты"],
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
    "delivery_count": ["deliveryCount", "delivery_count", "Количество доставок"],
    "return_delivery_count": ["returnDeliveryCount", "return_delivery_count", "Количество возврата"],
    "paid_storage": ["paidStorage", "storage_fee", "Хранение (пр)", "Хранение"],
    "penalty": ["penalty", "Штраф", "Общая сумма штрафов"],
    "deduction": ["deduction", "Удержанный штраф", "Удержания"],
    "additional_payment": [
        "additionalPayment", "additional_payment", "Корректировка ВВ",
        "Корректировка Вознаграждения Вайлдберриз (ВВ)",
    ],
    "rebill_logistic_cost": [
        "rebillLogisticCost", "rebill_logistic_cost",
        "Возмещение издержек по перевозке/складским операциям",
        "Возмещение издержек по перевозке/по складским операциям с товаром",
        "Возмещение издержек по перемещению и операционной обработке товара",
    ],
    "pvz_compensation": [
        "pvzCompensation", "pvz_compensation",
        "Возмещение за выдачу и возврат товаров на ПВЗ",
    ],
    "payment_services": [
        "paymentServices", "payment_services",
        "Компенсация платёжных услуг/Комиссия за интеграцию платёжных сервисов",
        "Компенсация платёжных услуг/Комиссии за интеграцию платёжных сервисов",
    ],
    "srid": ["srid", "Srid", "SRID"],
    "order_uid": ["orderUid", "order_uid", "Id корзины заказа"],
}

DETAIL_TEXT_COLS = ["report_id", "rrd_id", "gi_id", "nm_id", "article", "brand", "title",
                    "tech_size", "sku", "doc_type_name", "office_name", "srid", "order_uid"]
DETAIL_NUM_COLS = ["quantity", "retail_price", "retail_amount", "commission_percent",
                   "ppvz_sales_commission", "for_pay", "delivery_service",
                   "delivery_count", "return_delivery_count",
                   "paid_storage", "penalty", "deduction", "additional_payment",
                   "rebill_logistic_cost", "pvz_compensation", "payment_services"]
DETAIL_DATE_COLS = ["sale_dt", "order_dt"]


def normalize_wb_detail(df: pd.DataFrame, source: str = "excel",
                        row_base: int = 0) -> Optional[pd.DataFrame]:
    """Приводит «Детализацию продаж» WB (Excel ЛК или finance-API) к схеме wb_detail_rows.

    op_key: srid -> 'sr:<srid>'; иначе report_id:rrd_id -> 'rr:<report_id>:<rrd_id>'
    (для API rrd_id гарантирован пагинацией); иначе rrd_id; иначе 'row:<n>'.
    row_base — стартовый индекс для fallback 'row:<n>' (глобально нарастающий
    при обработке нескольких файлов батчем, чтобы ключи не пересекались).
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

    # В новых отчётах WB (2026) «Тип документа» почти всегда пуст, а на строках
    # ПВЗ-компенсации он бывает ошибочно = «Продажа». Реальный тип строки лежит в
    # «Обоснование для оплаты» — используем его как приоритетный источник.
    if "doc_type_name" in ndf.columns:
        obos = _pick_col(df, ["Обоснование для оплаты", "основание для оплаты"])
        if obos is not None:
            dt_fill = df[obos].fillna("").astype(str).str.strip()
            non_empty = dt_fill.ne("")
            ndf.loc[non_empty, "doc_type_name"] = dt_fill[non_empty]
    # колонка doc_type_name в БД — VARCHAR(60); классификация работает по
    # подстрокам, поэтому усекаем длинные «Обоснование…» без потери смысла.
    ndf["doc_type_name"] = ndf["doc_type_name"].astype(str).str.strip().str[:60]

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
            key = "row:" + str(row_base + i)
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
    """Строки детализации WB -> wb_detail_rows, upsert по op_key (идемпотентно).

    Один SRID в отчёте WB — корзина заказа, в которой могут повторяться строки
    Продажа/Возврат/Доставка/Хранение/Возмещение издержек/Штраф. Чтобы не терять
    артикул/дату/тип операции (последняя строка корзины — служебная, без
    артикула), каждая корзина разбивается на ОТДЕЛЬНЫЕ операции:
    - товарные строки («Продажа»/«Возврат», есть деньги продажи) — своя строка
      на op_key;
    - если в корзине и продажа, и возврат — они дают ДВЕ строки (Продажа и
      Возврат), чтобы «Продано» и «Возврат» считались раздельно;
    - служебные строки (Доставка/Хранение/Возмещение/Штраф) денежно прикрепляются
      к товарной операции корзины (расходы WB всегда расход, знак не зависит).
    Ключ коллизии: если SRID-группа без товарных строк (чистая логистика/
    хранение) — сворачивается в одну служебную строку, «Кол-во» не считается.
    """
    if df is None or df.empty:
        return 0
    keep = ["op_key"] + DETAIL_TEXT_COLS + DETAIL_NUM_COLS + DETAIL_DATE_COLS
    # normalize может отдавать подмножество схемы (тесты/частичные файлы):
    # работаем только с присутствующими колонками, остальные запишутся дефолтами.
    cols = [c for c in keep if c in df.columns]
    gdf = df[cols].copy()
    # Дубликаты снимаем ДО группировки, чтобы идентичные служебные строки не
    # дублировали деньги при свёртке.
    gdf = gdf.drop_duplicates(subset=[c for c in cols if c != "op_key"])
    num_cols = [c for c in DETAIL_NUM_COLS if c in gdf.columns]
    text_cols = [c for c in DETAIL_TEXT_COLS if c in gdf.columns and c != "op_key"]
    date_cols = [c for c in DETAIL_DATE_COLS if c in gdf.columns]

    def _op_kind(row: dict) -> str:
        """Тип операции строки: 'Продажа' / 'Возврат' / '' (служебная)."""
        art = str(row.get("article") or "")
        if not art:
            return ""
        dt = str(row.get("doc_type_name") or "")
        if _is_return_row(dt):
            return "Возврат"
        if _is_goods_row(dt, row.get("retail_amount", 0), row.get("for_pay", 0)):
            return "Продажа"
        return ""

    def _sum_num(rows, col) -> float:
        return _f(sum(_f(r.get(col, 0)) for r in rows))

    out_rows = []
    for op_key, grp in gdf.groupby("op_key", sort=False):
        rows = grp.to_dict("records")
        base_key = str(op_key)

        goods = [r for r in rows if _op_kind(r)]
        svc = [r for r in rows if not _op_kind(r)]
        if not goods:
            # Корзина целиком служебная (логистика/хранение/возмещение без товара):
            # сворачиваем в одну строку; «Кол-во» не считаем (это не продажа).
            last = rows[-1]
            rec = {"op_key": base_key, "source": source}
            for c in text_cols:
                rec[c] = str(last.get(c, "") or "")
            for c in date_cols:
                rec[c] = _as_date(last.get(c))
            rec["quantity"] = 0
            for c in num_cols:
                if c == "quantity":
                    continue
                rec[c] = _sum_num(rows, c)
            out_rows.append(rec)
            continue

        # Разделяем операции корзины: продажи и возвраты независимы.
        sale_rows = [r for r in goods if _op_kind(r) == "Продажа"]
        ret_rows = [r for r in goods if _op_kind(r) == "Возврат"]
        kinds = []
        if sale_rows:
            kinds.append(("Продажа", sale_rows))
        if ret_rows:
            kinds.append(("Возврат", ret_rows))
        multi = len(kinds) > 1

        # Привязка служебных строк к операции: строка «Доставка» с «Количество
        # возврата» относится к возврату, с «Количество доставок» — к продаже;
        # остальные расходы корзины (Возмещение/Хранение/Штраф/ПВЗ/платёжные)
        # крепим к продаже (первой операции), чтобы не задваивать деньги.
        def _svc_target(svc_row) -> str:
            if multi:
                if _f(svc_row.get("return_delivery_count")) > 0 and ret_rows:
                    return "Возврат"
                if _f(svc_row.get("delivery_count")) > 0 and sale_rows:
                    return "Продажа"
            return kinds[0][0]

        for kind, op_rows in kinds:
            key = f"{base_key}:{kind}" if multi else base_key
            last = op_rows[-1]
            rec = {"op_key": key, "source": source}
            for c in text_cols:
                rec[c] = str(last.get(c, "") or "")
            for c in date_cols:
                rec[c] = _as_date(last.get(c))
            rec["article"] = str(last.get("article") or "")
            rec["doc_type_name"] = kind
            rec["quantity"] = int(_sum_num(op_rows, "quantity"))
            # деньги/количество самой операции
            for c in num_cols:
                if c == "quantity":
                    continue
                rec[c] = _sum_num(op_rows, c)
            # служебные строки, относящиеся К ЭТОЙ операции
            for svc_row in svc:
                if _svc_target(svc_row) != kind:
                    continue
                for c in num_cols:
                    if c == "quantity":
                        continue
                    rec[c] = _f(rec[c]) + _f(svc_row.get(c, 0))
            out_rows.append(rec)

    if not out_rows:
        return 0
    values = []
    for r in out_rows:
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
            "quantity": int(r.get("quantity", 0)),
            "retail_price": _f(r.get("retail_price")),
            "retail_amount": _f(r.get("retail_amount")),
            "commission_percent": _f(r.get("commission_percent")),
            "office_name": str(r.get("office_name", "") or ""),
            "sale_dt": r.get("sale_dt"),
            "order_dt": r.get("order_dt"),
            "ppvz_sales_commission": _f(r.get("ppvz_sales_commission")),
            "for_pay": _f(r.get("for_pay")),
            "delivery_service": _f(r.get("delivery_service")),
            "delivery_count": int(_f(r.get("delivery_count"))),
            "return_delivery_count": int(_f(r.get("return_delivery_count"))),
            "paid_storage": _f(r.get("paid_storage")),
            "penalty": _f(r.get("penalty")),
            "deduction": _f(r.get("deduction")),
            "additional_payment": _f(r.get("additional_payment")),
            "rebill_logistic_cost": _f(r.get("rebill_logistic_cost")),
            "pvz_compensation": _f(r.get("pvz_compensation")),
            "payment_services": _f(r.get("payment_services")),
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


def _is_goods_row(doc_type=None, retail_amount=0, for_pay=0) -> bool:
    """Товарная ли это строка операции (продажа/возврат), а не логистика.

    В отчёте WB строки логистики/возмещения (пустой «Тип документа»,
    «Возмещение издержек…») несут служебное «Кол-во» и не имеют денег продажи
    (retail_amount/for_pay = 0). Такие строки не должны считаться «продано, шт».
    """
    s = str(doc_type or "").lower()
    if "возврат" in s or "return" in s or "продаж" in s or "sale" in s:
        return True
    return abs(float(retail_amount or 0)) >= 0.005 or abs(float(for_pay or 0)) >= 0.005


def rebuild_sales_from_detail(db, sale_from=None, sale_to=None, source: str = "detail") -> int:
    """Пересчитывает продажи source='detail' из wb_detail_rows (сумма по дата+артикул).

    Каждая строка wb_detail_rows — операция (SRID уже сведён из основной строки
    и строк логистики). Знак: возвраты вычитают кол-во/выручку/к перечислению,
    расходы (логистика, хранение, услуги) — всегда расход независимо от типа.
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
        sign = -1 if is_ret else 1
        # Легаси-строки логистики (свёрнутые до фикса) не считаем «продано, шт».
        qty = r.quantity if _is_goods_row(
            r.doc_type_name, r.retail_amount, r.for_pay) else 0
        recs.append({
            "date": r.sale_dt,
            "article": r.article,
            "quantity": qty * sign,
            "returns_qty": qty if is_ret else 0,
            "revenue": _f(r.retail_amount) * sign,
            "commission": _f(r.ppvz_sales_commission) * sign,
            # Расходы WB — всегда расход, независимо от типа операции (возврат
            # не «возвращает» доставку/хранение/штрафы продавцу).
            "logistics": _f(r.delivery_service),
            "storage": _f(r.paid_storage),
            "services": (_f(r.penalty) + _f(r.deduction)
                          + _f(r.additional_payment) + _f(r.rebill_logistic_cost)),
            "income": _f(r.for_pay) * sign,
        })
    if not recs:
        return 0
    return upsert_sales(db, pd.DataFrame(recs), "wb", source=source)


def storage_split(
    db,
    date_from=None,
    date_to=None,
    window_days: int = 7,
    target_articles: Optional[set] = None,
    sold_fraction: float = 0.5,
) -> dict:
    """Распределение безартикульных плат «Хранение» по товарам.

    Строки хранения в детализации WB не содержат артикула (одна плата за день
    за весь склад, поле paid_storage). Чтобы отнести их к товарам, вес каждого
    товара считается как «занимаемый объём × дневной тариф × остаток»:
    storage_costs.volume × (storage_price, если >0, иначе 1) × stocks.quantity.
    Для каждого дня с платой хранения S берётся остаток на этот день —
    ближайший срез стоков в окне ±window_days дней с максимальным суммарным
    весом (самый «полный» срез рядом); если полезного среза в окне нет —
    ближайший по календарной дистанции срез с весом > 0 (в обе стороны,
    предпочтение срезу ≤ дня). Доля товара = S × вес / Σвес.

    Товар, который продали за период, тоже занимал место на складе (в среднем
    ~половину отчёта), поэтому к остатку при вычислении веса прибавляется
    проданное количество периода × sold_fraction (0.5 — «продано на середине
    отчёта»). Это не даёт топ-товарам с опустевшим остатком уходить с нулевым
    «Хранением». Артикулы без строк в стоках, но с продажами в периоде,
    учитываются по проданному количеству.

    target_articles — множество артикулов (UPPER). Если задано, веса считаются
    только по этим артикулам и нормализуются в их пределах: 100% платы
    распределяется внутри множества, «орфанов» (артикулы без продаж в окне)
    не возникает. Артикулы target без объёма/тарифа в storage_costs получают
    fallback-вес = средний «volume × tariff» целевых артикулов с объёмом,
    умноженный на «остаток + проданное × sold_fraction» из среза.

    Возвращает {article_upper: сумма_руб} за период (только товары с весом).
    Без target_articles — распределение по всем артикулам с весом.
    """
    day_totals: dict = {}
    q = select(models.WbDetailRow.sale_dt, models.WbDetailRow.paid_storage).where(
        models.WbDetailRow.paid_storage != 0,
        models.WbDetailRow.sale_dt.isnot(None),
    )
    if date_from:
        q = q.where(models.WbDetailRow.sale_dt >= date_from)
    if date_to:
        q = q.where(models.WbDetailRow.sale_dt <= date_to)
    for dt, ps in db.execute(q):
        day_totals[dt] = day_totals.get(dt, 0.0) + float(ps or 0)
    if not day_totals:
        return {}
    days = sorted(day_totals)

    # объём и тариф по артикулу (UPPER): (volume, storage_price)
    vol_rate: dict = {}
    for sc in db.execute(
        select(models.StorageCost).where(models.StorageCost.volume != 0)
    ).scalars():
        art = (sc.article or "").strip().upper()
        if not art:
            continue
        vol_rate.setdefault(art, (float(sc.volume or 0), float(sc.storage_price or 0)))
    if not vol_rate:
        return {}

    # Средний «volume × tariff» целевых артикулов с объёмом — fallback-вес для
    # артикулов target, у которых в storage_costs данных нет (вариант A).
    mean_unit_weight = 0.0
    if target_articles:
        have = [
            (float(v), float(r))
            for a, (v, r) in vol_rate.items()
            if a in target_articles
        ]
        if have:
            mean_unit_weight = sum(
                v * (r if r > 0 else 1.0) for v, r in have
            ) / len(have)

    # Продажи периода (нетто по товарным строкам): то, что покинуло склад.
    sold_qty: dict = {}
    if sold_fraction:
        sq = select(
            models.WbDetailRow.article,
            models.WbDetailRow.quantity,
            models.WbDetailRow.doc_type_name,
            models.WbDetailRow.retail_amount,
            models.WbDetailRow.for_pay,
        )
        if date_from:
            sq = sq.where(models.WbDetailRow.sale_dt >= date_from)
        if date_to:
            sq = sq.where(models.WbDetailRow.sale_dt <= date_to)
        for r in db.execute(sq):
            art = (r.article or "").strip().upper()
            if not art or not _is_goods_row(
                    r.doc_type_name, r.retail_amount, r.for_pay):
                continue
            sign = -1 if _is_return_row(r.doc_type_name) else 1
            sold_qty[art] = sold_qty.get(art, 0.0) + float(r.quantity or 0) * sign

    srows = db.execute(
        select(models.Stock.date, models.Stock.article,
               func.sum(models.Stock.quantity).label("q"))
        .where(models.Stock.quantity != 0)
        .group_by(models.Stock.date, models.Stock.article)
    ).all()
    by_date: dict = {}
    for sdt, art, qty in srows:
        by_date.setdefault(sdt, {})[(art or "").strip().upper()] = int(qty or 0)
    sdates = sorted(by_date)
    if not sdates:
        # Срезов стоков нет вовсе — используем дни с платой как опорные даты,
        # вес берётся только из проданного количества × sold_fraction.
        sdates = days
        for sd in sdates:
            by_date[sd] = {}

    # веса по всем срезам: {date: (wmap, total_w)} — только target при задании
    weight_cache: dict = {}
    for sd in sdates:
        wmap: dict = {}
        total_w = 0.0
        arts = set(by_date.get(sd, {}))
        if sold_qty:
            arts |= set(sold_qty)
        for art in arts:
            if target_articles is not None and art not in target_articles:
                continue
            qty = float(by_date.get(sd, {}).get(art, 0) or 0)
            if sold_fraction:
                qty += sold_qty.get(art, 0.0) * sold_fraction
            if qty <= 0:
                continue
            vol, rate = vol_rate.get(art, (0.0, 0.0))
            if vol > 0:
                w = vol * (rate if rate > 0 else 1.0) * qty
            elif mean_unit_weight > 0:
                w = mean_unit_weight * qty
            else:
                w = 0.0
            if w > 0:
                wmap[art] = w
                total_w += w
        weight_cache[sd] = (wmap, total_w)

    def pick_snapshot(d):
        """Лучший срез стоков для дня d.

        Самый весомый в окне ±window_days; если полезного (вес > 0) в окне нет —
        ближайший по календарной дистанции срез с весом > 0; иначе — срез ≤ дня,
        иначе первый доступный.
        """
        lo = d - timedelta(days=window_days)
        hi = d + timedelta(days=window_days)
        cands = [sd for sd in sdates if lo <= sd <= hi]
        if cands:
            best = max(cands, key=lambda sd: weight_cache[sd][1])
            if weight_cache[best][1] > 0:
                return best
        useful = [sd for sd in sdates if weight_cache[sd][1] > 0]
        if useful:
            def dist(sd):
                delta = (sd - d).days
                return (abs(delta), 0 if delta <= 0 else 1)
            return min(useful, key=dist)
        le = [sd for sd in sdates if sd <= d]
        return le[-1] if le else sdates[0]

    out: dict = {}
    for d in days:
        sdate = pick_snapshot(d)
        wmap, total_w = weight_cache[sdate]
        if total_w <= 0:
            continue
        S = day_totals[d]
        for art, w in wmap.items():
            out[art] = out.get(art, 0.0) + S * w / total_w
    return {k: round(v, 2) for k, v in out.items()}


def _redistribute_articleless(rows, cells, fields, weight_idx=(6, 7)):
    """Распределяет безартикульные расходы по артикулам пропорционально весу.

    fields — словарь {имя: (индекс_в_cells, getter)} где getter(row) -> float.
    Имя с суффиксом '.int' распределяется целыми числами (largest remainder),
    чтобы суммы в точности сходились.

    weight_idx — индексы ячеек, сумма которых даёт вес статьи
    (по умолчанию delivery_count + return_delivery_count). Если сумма <= 0,
    фолбэк: abs(sells) (индекс 0). Если весов нет — равномерно.
    """
    if not cells:
        return
    art_less = [r for r in rows if not (r.article or "").strip() or r.sale_dt is None]
    if not art_less:
        return
    totals = {name: sum(getter(r) for r in art_less) for name, (_, getter) in fields.items()}
    if all(v == 0 for v in totals.values()):
        return
    weights = {}
    for art, c in cells.items():
        w = 0
        if all(0 <= i < len(c) for i in weight_idx):
            w = max(0, sum(c[i] for i in weight_idx))
        if w <= 0:
            w = max(0, abs(c[0]))
        weights[art] = w
    total_w = sum(weights.values())
    if total_w <= 0:
        weights = {a: 1.0 for a in cells}
        total_w = len(cells)

    # Целые поля (счётчики) разносим largest-remainder, денежные — долями.
    for name, (idx, _) in fields.items():
        total = totals[name]
        if name.endswith(".int"):
            total = int(round(total))
            shares = {a: int(total * w // total_w) for a, w in weights.items()}
            rest = total - sum(shares.values())
            if rest > 0:
                rs = sorted(weights, key=lambda a: (
                    weights[a] / total_w - shares[a] / max(1, total), -weights[a]), reverse=True)
                for i in range(rest):
                    shares[rs[i % len(rs)]] += 1
            for a, c in cells.items():
                c[idx] += shares[a]
        else:
            for a, c in cells.items():
                c[idx] += total * weights[a] / total_w


def like_pattern(pattern: Optional[str]):
    """SQL LIKE '%…%' → скомпилированный regex (регистронезависимо) или None.

    Сохраняет семантику ilike: % — любая последовательность, _ — один символ.
    Используется, чтобы фильтр поиска применялся только к выводу, а не к
    агрегации (распределение расходов не должно пересчитываться под фильтр).
    """
    if not pattern:
        return None
    return re.compile(
        re.escape(pattern).replace("%", r".*").replace("_", r"."),
        re.IGNORECASE,
    )


def detail_summary_dataframe(
    db,
    date_from=None,
    date_to=None,
    article_like: Optional[str] = None,
) -> pd.DataFrame:
    """Свод «Детализации продаж» WB по артикулам — сырые деньги операций.

    В отличие от margin_detail_dataframe тут НЕТ себестоимости и маржи —
    только то, что фактически пришло/ушло по WB-отчёту. Строится напрямую
    из wb_detail_rows (операции). Возврат вычитает кол-во/выручку/комиссию/
    «к перечислению»; расходы WB (логистика, хранение, услуги) всегда
    остаются расходами. Полезно для выгрузки «как в отчёте» без аналитики.
    """
    cols = ["article", "title", "sells", "returns_qty", "revenue", "commission",
            "for_pay", "logistics", "delivery_count", "return_delivery_count",
            "storage", "pvz_compensation", "payment_services", "services",
            "ops_count", "sources"]
    out = pd.DataFrame(columns=cols)
    q = select(models.WbDetailRow)
    if date_from:
        q = q.where(models.WbDetailRow.sale_dt >= date_from)
    if date_to:
        q = q.where(models.WbDetailRow.sale_dt <= date_to)
    rows = list(db.execute(q).scalars().all())
    if not rows:
        return out

    # Фильтр поиска применяется только к выводу (см. like_pattern): распределение
    # безартикульных расходов и хранения не пересчитывается под фильтр, доли
    # считаются по всему окну.
    art_check = like_pattern(article_like)

    cells: dict = {}
    titles: dict = {}
    for r in rows:
        art = r.article
        if not art or r.sale_dt is None:
            continue
        is_ret = _is_return_row(r.doc_type_name)
        sign = -1 if is_ret else 1
        qty = r.quantity if _is_goods_row(
            r.doc_type_name, r.retail_amount, r.for_pay) else 0
        if art not in cells:
            cells[art] = [0, 0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0, set()]
        c = cells[art]
        c[0] += qty * sign                     # sells (нетто)
        c[1] += qty if is_ret else 0           # returns_qty
        c[2] += float(r.retail_amount or 0) * sign   # revenue
        c[3] += float(r.ppvz_sales_commission or 0) * sign
        c[4] += float(r.for_pay or 0) * sign         # for_pay
        c[5] += float(r.delivery_service or 0)       # logistics
        c[6] += int(r.delivery_count or 0)           # delivery_count
        c[7] += int(r.return_delivery_count or 0)    # return_delivery_count
        c[8] += float(r.paid_storage or 0)           # storage (по артикулу, обычно 0)
        c[9] += float(r.pvz_compensation or 0)       # pvz_compensation
        c[10] += float(r.payment_services or 0)      # payment_services
        c[11] += float(r.penalty or 0) + float(r.deduction or 0) \
            + float(r.additional_payment or 0) + float(r.rebill_logistic_cost or 0)
        c[12] += 1                                # ops_count
        c[13].add(str(r.source or ""))
        if (r.title or "").strip():
            titles.setdefault(art, str(r.title).strip())

    # Безартикульные расходы (логистика, услуги, компенсации ПВЗ и др.)
    # распределяются пропорционально весу статьи (delivery_count +
    # return_delivery_count, фолбэк abs(sells)) — аналог storage_split.
    _redistribute_articleless(rows, cells, {
        "logistics": (5, lambda r: float(r.delivery_service or 0)),
        "delivery_count.int": (6, lambda r: float(int(r.delivery_count or 0))),
        "return_delivery_count.int": (7, lambda r: float(int(r.return_delivery_count or 0))),
        "pvz_compensation": (9, lambda r: float(r.pvz_compensation or 0)),
        "payment_services": (10, lambda r: float(r.payment_services or 0)),
        "services": (11, lambda r: float(r.penalty or 0) + float(r.deduction or 0)
                     + float(r.additional_payment or 0) + float(r.rebill_logistic_cost or 0)),
    })

    # Безартикульные платы «Хранение» распределяются по товарам свода пропорц.
    # «объём × тариф × (остаток + проданное×0.5)» (storage_costs × stocks +
    # продажи периода). Распределение идёт только по артикулам с операциями
    # в окне — 100% платы разносится внутри свода, отдельной строки-остатка
    # не остаётся.
    storage_est_map = storage_split(
        db,
        date_from=date_from,
        date_to=date_to,
    )

    recs = []
    for art, c in cells.items():
        if art_check is not None and art_check.search(art) is None:
            continue
        (sells, returns_qty, revenue, commission, for_pay, logistics,
         delivery_count, return_delivery_count, storage, pvz_compensation,
         payment_services, services, ops, srcs) = c
        storage = storage + storage_est_map.get(art.strip().upper(), 0.0)
        recs.append({
            "article": art, "title": titles.get(art, ""),
            "sells": sells, "returns_qty": returns_qty,
            "revenue": round(revenue, 2), "commission": round(commission, 2),
            "for_pay": round(for_pay, 2), "logistics": round(logistics, 2),
            "delivery_count": delivery_count, "return_delivery_count": return_delivery_count,
            "storage": round(storage, 2), "pvz_compensation": round(pvz_compensation, 2),
            "payment_services": round(payment_services, 2),
            "services": round(services, 2),
            "ops_count": ops,
            "sources": ",".join(sorted(s for s in srcs if s)),
        })
    out = pd.DataFrame(recs, columns=cols)
    out = out.sort_values("for_pay", ascending=False).reset_index(drop=True)
    return out


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


def _oz_col(df: pd.DataFrame, *names: str) -> Optional[str]:
    """Первый существующий столбец из кандидатов (без учёта регистра)."""
    cols = {c.strip().lower(): c for c in df.columns if isinstance(c, str)}
    for n in names:
        hit = cols.get(n.lower())
        if hit:
            return hit
    return None


def normalize_oz_cards(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Приводит карточки товара Ozon (отчёт /v1/report/products) к схеме marketplace_cards.

    chrt_id = Ozon Product ID, nm_id = артикул WB (заполняется позже из карточек WB),
    vendor_code = Offer ID/Артикул, barcode = «Штрихкод (Серийный номер / EAN)».
    Размер/предмет/состав для Ozon пустые.
    """
    if df is None or df.empty:
        return None
    idx = df.index
    out = pd.DataFrame({
        "chrt_id": df.get(_oz_col(df, "Ozon Product ID", "Product ID", "product_id"), pd.Series("", index=idx)),
        "nm_id": "",
        "vendor_code": df.get(_oz_col(df, "Offer ID", "Артикул", "Артикул продавца"), pd.Series("", index=idx)),
        "brand": df.get(_oz_col(df, "Бренд", "Категория", "Brand", "Category"), pd.Series("", index=idx)),
        "subject": "",
        "size": "",
        "barcode": df.get(_oz_col(df, "Штрихкод (Серийный номер / EAN)", "Штрихкод", "Barcode", "SKU"), pd.Series("", index=idx)),
        "composition": "",
        "name": df.get(_oz_col(df, "Name", "Название товара", "Название", "Наименование"), pd.Series("", index=idx)),
        "volume_l": 0.0,
    })
    out["volume_l"] = pd.to_numeric(out["volume_l"], errors="coerce").fillna(0.0)
    for col in CARD_TEXT_COLS:
        out[col] = out[col].fillna("").astype(str).str.strip()
    out["barcode"] = out["barcode"].map(_first_scalar)
    out = out.drop_duplicates(subset=["chrt_id", "vendor_code", "barcode"], keep="first")
    out = out[out["chrt_id"] != ""]
    return out if not out.empty else None


def enrich_oz_cards_with_wb_nm(db, mdf: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """Заполняет nm_id (Артикул WB) карточек Ozon по карточкам WB.

    Сначала по штрихкоду (баркоды WB и Ozon совпадают в большинстве случаев),
    затем по артикулу: у WB артикул и размер хранятся отдельно, а у Ozon
    размер вшит в конец артикула (например JBR-LYA-297-A-BLUE-34 — артикул
    Ozon, где -34 размер; у WB это JBR-LYA-297-A-BLUE и размер 34), поэтому
    последовательно отрезаем хвостовые сегменты артикула (после "-") и ищем
    WB-карточку с таким артикулом. При совпадении в nm_id пишется реальный
    артикул WB (nmID). Строки без совпадения оставляем с пустым nm_id.
    """
    if mdf is None or mdf.empty:
        return mdf
    mp_wb = marketplace_id(db, "wb")
    wb_rows = db.execute(
        select(models.MarketplaceCard)
        .where(models.MarketplaceCard.marketplace_id == mp_wb)
        .order_by(models.MarketplaceCard.imported_at.asc(), models.MarketplaceCard.id.asc())
    ).scalars().all()
    by_barcode: dict = {}
    by_vendor: dict = {}
    for r in wb_rows:
        nm = str(r.nm_id or "").strip()
        if not nm:
            continue
        if r.barcode:
            by_barcode.setdefault(str(r.barcode).strip().lower(), nm)
        if r.vendor_code:
            by_vendor.setdefault(str(r.vendor_code).strip().lower(), nm)

    def _find_nm(barcode: str, vendor_code: str) -> str:
        if barcode:
            hit = by_barcode.get(barcode.strip().lower())
            if hit:
                return hit
        art = str(vendor_code or "").strip()
        while art:
            hit = by_vendor.get(art.lower())
            if hit:
                return hit
            if "-" not in art:
                break
            art = art.rsplit("-", 1)[0]
        return ""

    out = mdf.copy()
    out["nm_id"] = [
        _find_nm(str(r.get("barcode") or ""), str(r.get("vendor_code") or ""))
        for r in out.to_dict("records")
    ]
    return out


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
    """Карточки маркетплейса (marketplace_cards), upsert по (marketplace_id, chrt_id).

    Для Ozon vendor_code = артикул карточки (артикул товара + размер), поэтому
    base_article/size доопределяются резолвером, если провайдер их не отдал.
    У WB размер приходит из ЛК штатно и резолвер не применяется.
    """
    if df is None or df.empty:
        return 0
    mp = marketplace_id(db, code)
    omap = _ozon_base_map(db, df["vendor_code"]) if code == "ozon" else {}
    values = []
    for r in df.to_dict("records"):
        vendor = str(r["vendor_code"])
        _base, _size, _m = omap.get(vendor, ("", "", ""))
        values.append({
            "marketplace_id": mp,
            "chrt_id": str(r["chrt_id"]),
            "nm_id": str(r["nm_id"]),
            "vendor_code": vendor,
            "base_article": _base or (vendor if code == "ozon" else ""),
            "brand": str(r["brand"]),
            "subject": str(r["subject"]),
            "size": str(r.get("size") or "") or _size,
            "barcode": str(r["barcode"]),
            "volume_l": float(r.get("volume_l") or 0),
            "composition": str(r["composition"]),
            "name": str(r["name"]),
        })
    ins = insert(models.MarketplaceCard)
    stmt = ins.on_conflict_do_update(
        index_elements=["marketplace_id", "chrt_id"],
        set_={c: ins.excluded[c]
              for c in ["nm_id", "vendor_code", "base_article", "brand", "subject",
                        "size", "barcode", "volume_l", "composition", "name"]},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


# Общая схема карточки каталога «Наш склад → Товары»
CATALOG_FIELDS = ["article", "name", "brand", "subject", "size", "barcode",
                  "volume_l", "composition", "source"]
_CATALOG_TEXT_FIELDS = ["name", "brand", "subject", "composition"]


def _first_barcode(v) -> str:
    """Берёт первый штрихкод из значения (список, строка через запятую)."""
    if isinstance(v, (list, tuple)):
        return str(v[0]).strip() if v else ""
    s = str(v or "")
    if "," in s:
        return s.split(",")[0].strip()
    return s.strip()


def _wb_card_volume(v) -> float:
    """Объём из dimensions WB (Д×Ш×В, мм → литры: мм³/1e6)."""
    if not isinstance(v, dict):
        return 0.0
    try:
        l = float(v.get("length") or 0)
        w = float(v.get("width") or 0)
        h = float(v.get("height") or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(l * w * h / 1_000_000, 3)


def _catalog_subject(v) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or "")
    return str(v or "")


def normalize_catalog_card(df: pd.DataFrame, source: str = "wb") -> Optional[pd.DataFrame]:
    """Приводит карточки WB или Ozon к общей схеме каталога CATALOG_FIELDS.

    WB (реальный ответ content/v2/get/cards/list): vendorCode→article,
    title→name, subject→subject (dict.name), techSize→size, skus→barcode
    (первый штрихкод размера), объём из dimensions Д×Ш×В мм/1e6.
    Ozon (отчёт /v1/report/products): Offer ID→article, Name→name,
    SKU/Штрихкод→barcode, Category/Бренд→subject/brand.
    Строки без артикула отбрасываются, дубликаты (source, article, size, barcode)
    снимаются.
    """
    if df is None or df.empty:
        return None
    df = df.copy()
    lo = {str(c).strip().lower(): c for c in df.columns if c is not None}

    def col(*names):
        for n in names:
            hit = lo.get(n.strip().lower())
            if hit:
                return df[hit]
        return None

    def txt(series) -> pd.Series:
        return (series if series is not None else pd.Series("", index=df.index)).fillna("").astype(str).str.strip()

    empty_s = pd.Series("", index=df.index)
    zero_s = pd.Series(0.0, index=df.index)
    if source == "wb":
        raw_article = col("vendorCode", "vendor_code", "артикул")
        if raw_article is None:
            return None
        dims = col("dimensions")
        subject_src = col("subject", "предмет")
        barcode_src = col("skus", "barcodes", "barcode", "sku", "баркод", "штрихкод")
        out = {
            "article": txt(raw_article),
            "name": txt(col("title", "name", "название товара", "наименование")),
            "brand": txt(col("brand", "бренд")),
            "subject": txt((subject_src if subject_src is not None else empty_s).map(_catalog_subject)),
            "size": txt(col("techSize", "tech_size", "размер", "wbSize")),
            "barcode": (barcode_src if barcode_src is not None else empty_s).map(_first_barcode),
            "volume_l": (dims if dims is not None else pd.Series(None, index=df.index)).map(_wb_card_volume),
            "composition": txt(col("composition", "состав")),
        }
    else:
        raw_article = col("Offer ID", "артикул", "артикул продавца")
        if raw_article is None:
            return None
        brand = col("бренд", "Brand", "brand")
        cat = col("Category", "category", "категория")
        brand_src = brand if brand is not None else cat
        subject_src = cat if cat is not None else brand
        barcode_src = col("штрихкод (серийный номер / ean)", "штрихкод", "barcode", "sku")
        out = {
            "article": txt(raw_article),
            "name": txt(col("Name", "название товара", "название", "наименование")),
            "brand": txt(brand_src),
            "subject": txt(subject_src),
            "size": empty_s.copy(),
            "barcode": (barcode_src if barcode_src is not None else empty_s).map(_first_barcode),
            "volume_l": zero_s.copy(),
            "composition": empty_s.copy(),
        }
    out["source"] = source
    ndf = pd.DataFrame(out)[CATALOG_FIELDS]
    ndf["volume_l"] = pd.to_numeric(ndf["volume_l"], errors="coerce").fillna(0.0)
    ndf = ndf[ndf["article"] != ""]
    if ndf.empty:
        return None
    ndf = ndf.drop_duplicates(subset=["source", "article", "size", "barcode"], keep="first")
    return ndf


def sync_catalog_from_cards(db, cards_df: Optional[pd.DataFrame],
                            overwrite: bool = False) -> dict:
    """Сливает карточки WB+Ozon в общий каталог products + product_sizes + product_aliases.

    Идентификация строки (в порядке приоритета):
    1) баркод: product_sizes.barcode, затем products.barcode;
    2) (article,size) в product_sizes;
    3) article в products / product_aliases (алиас разрешается в канонический товар);
    4) иначе — новый товар по article.

    Если баркод строки совпал с товаром, отличным от артикула строки, чужой
    артикул записывается в product_aliases (товары объединяются, дальнейшие
    строки со старым артикулом разрешаются через алиас).

    Правила записи полей товара (name/brand/subject/volume_l/composition):
    overwrite=False (по умолчанию) — заполняются только пустые поля;
    overwrite=True — перезапись значениями карточки. net_cost и replenishable
    НЕ трогаются никогда. Размеры (product_sizes) синхронизируются всегда:
    новая строка добавляется, изменившийся баркод перезаписывается.

    Отчёт: {created_products, updated_products, sizes_added, sizes_updated,
    aliases, unmapped_fields, rows, errors}.
    """
    report = {"created_products": 0, "updated_products": 0, "sizes_added": 0,
              "sizes_updated": 0, "aliases": 0, "unmapped_fields": [],
              "rows": 0, "errors": []}
    if cards_df is None or cards_df.empty:
        return report

    known = set(CATALOG_FIELDS)
    report["unmapped_fields"] = [
        str(c) for c in cards_df.columns if str(c) not in known
    ]

    afi = cards_df.rename(str).copy()
    for c in CATALOG_FIELDS:
        if c not in afi.columns:
            afi[c] = "" if c != "volume_l" else 0.0
    for c in _CATALOG_TEXT_FIELDS + ["article", "size", "barcode", "source"]:
        afi[c] = afi[c].fillna("").astype(str).str.strip()
    afi["volume_l"] = pd.to_numeric(afi["volume_l"], errors="coerce").fillna(0.0)
    afi = afi[afi["article"] != ""]
    report["rows"] = int(len(afi))

    # Текущее состояние каталога
    products: dict = {p.article: p for p in db.execute(select(models.Product)).scalars()}
    aliases: dict = {
        a: c for a, c in db.execute(
            select(models.ProductAlias.alias_article, models.ProductAlias.article)
        )
    }
    sizes: dict = {}
    size_by_barcode: dict = {}
    for s in db.execute(select(models.ProductSize)).scalars():
        sizes[(s.article, s.size)] = s
        if s.barcode:
            size_by_barcode.setdefault(s.barcode.lower(), s.article)
    product_by_barcode: dict = {}
    for art, bar in db.execute(
        select(models.Product.article, models.Product.barcode)
    ):
        if bar:
            product_by_barcode.setdefault(bar.lower(), art)

    seen = set()
    created_here: set = set()
    for row in afi.to_dict("records"):
        art = str(row.get("article") or "")
        size = str(row.get("size") or "")
        bar = str(row.get("barcode") or "")
        key = (art, size, bar)
        if key in seen:
            continue
        seen.add(key)
        try:
            # --- идентификация (канонический артикул) ---
            canonical = None
            bl = bar.lower()
            if bl:
                canonical = size_by_barcode.get(bl) or product_by_barcode.get(bl)
            if not canonical:
                ac = aliases.get(art, art)
                if (ac, size) in sizes and ac in products:
                    canonical = ac
            if not canonical:
                if art in products:
                    canonical = art
                elif art in aliases:
                    canonical = aliases[art]
            if not canonical:
                if not art:
                    report["errors"].append("строка без артикула")
                    continue
                prod = models.Product(article=art, name="", brand="", subject="",
                                      composition="", barcode=bar,
                                      volume_l=float(row.get("volume_l") or 0))
                db.add(prod)
                products[art] = prod
                created_here.add(art)
                canonical = art
                if bl:
                    product_by_barcode.setdefault(bl, art)
                report["created_products"] += 1

            # --- алиас: чужой артикул → канонический товар ---
            if art and art != canonical and aliases.get(art) != canonical:
                db.merge(models.ProductAlias(alias_article=art, article=canonical))
                aliases[art] = canonical
                report["aliases"] += 1

            # --- поля товара (overwrite / только пустые), net_cost и replenishable не трогаем ---
            cur = products[canonical]
            dirty = False
            for field in _CATALOG_TEXT_FIELDS:
                val = str(row.get(field) or "")
                if not val:
                    continue
                cur_val = str(getattr(cur, field) or "")
                if overwrite and cur_val != val:
                    setattr(cur, field, val)
                    dirty = True
                elif not overwrite and not cur_val:
                    setattr(cur, field, val)
                    dirty = True
            vol = float(row.get("volume_l") or 0)
            cur_vol = float(getattr(cur, "volume_l") or 0)
            if (overwrite and cur_vol != vol) or (not overwrite and not cur_vol and vol):
                setattr(cur, "volume_l", vol)
                dirty = True
            if dirty and canonical not in created_here:
                report["updated_products"] += 1

            # --- размеры: всегда синхронизируем ---
            skey = (canonical, size)
            srow = sizes.get(skey)
            if srow is None:
                srow = models.ProductSize(article=canonical, size=size, barcode=bar)
                db.add(srow)
                sizes[skey] = srow
                if bar:
                    size_by_barcode.setdefault(bar.lower(), canonical)
                report["sizes_added"] += 1
            elif bar and srow.barcode != bar:
                srow.barcode = bar
                if bar.lower() not in size_by_barcode:
                    size_by_barcode[bar.lower()] = canonical
                report["sizes_updated"] += 1
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"{art}: {e}")
    db.commit()
    return report


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
    """Остатки маркетплейса -> stocks, upsert по (mp, date, article, склад, chrt_id).

    Для Ozon артикул = товар + размер, size приходит пустым — доопределяется
    резолвером, чтобы остатки по артикулу можно было получить без разбора строк.
    """
    df = _norm_num(df, ["quantity", "quantity_full", "in_way"])
    mp_id = marketplace_id(db, code)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ["chrt_id", "size", "barcode"]:
        if c not in df.columns:
            df[c] = ""
    for c in ["quantity_full", "in_way"]:
        if c not in df.columns:
            df[c] = 0
    df = df.groupby(["date", "article", "warehouse", "chrt_id"], as_index=False).agg({
        "quantity": "sum", "quantity_full": "sum", "in_way": "sum",
        "size": "first", "barcode": "first",
    })
    bmap = {}
    if code == "ozon":
        omap = _ozon_base_map(db, df["article"])
        bmap = {art: (v[0] or art, v[1]) for art, v in omap.items()}
    values = []
    for row in df.to_dict("records"):
        article = str(row["article"]).strip()
        base, size = bmap.get(article, ("", ""))
        values.append({
            "marketplace_id": mp_id,
            "date": row["date"],
            "article": article,
            "base_article": base or (article if code == "ozon" else ""),
            "warehouse": str(row.get("warehouse", "Все")).strip(),
            "chrt_id": str(row.get("chrt_id", "")).strip(),
            "size": str(row.get("size", "")).strip() or size,
            "barcode": str(row.get("barcode", "")).strip(),
            "quantity": int(row["quantity"]),
            "quantity_full": int(row["quantity_full"]),
            "in_way": int(row["in_way"]),
        })
    ins = insert(models.Stock)
    stmt = ins.on_conflict_do_update(
        index_elements=["marketplace_id", "date", "article", "warehouse", "chrt_id"],
        set_={"quantity": ins.excluded.quantity,
              "quantity_full": ins.excluded.quantity_full,
              "in_way": ins.excluded.in_way,
              "base_article": ins.excluded.base_article,
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
    for c in ["views", "opens", "adds", "orders", "cancelled", "buyouts",
              "stock_wb", "stock_mp", "add_to_wishlist", "time_to_ready_min",
              "wb_club_order_count", "wb_club_buyout_count", "wb_club_cancel_count"]:
        df[c] = pd.to_numeric(df.get(c, 0), errors="coerce").fillna(0).astype(int)
    for c in ["avg_price", "revenue", "buyout_sum", "cancel_sum",
              "stock_balance_sum", "avg_orders_per_day", "share_order_percent",
              "localization_percent", "conv_to_cart_percent",
              "conv_cart_to_order_percent", "conv_buyout_percent",
              "product_rating", "feedback_rating",
              "wb_club_order_sum", "wb_club_buyout_sum", "wb_club_cancel_sum",
              "wb_club_avg_price", "wb_club_buyout_percent",
              "wb_club_avg_orders_per_day"]:
        df[c] = pd.to_numeric(df.get(c, 0), errors="coerce").fillna(0)
    df["nm_id"] = df.get("nm_id", "").astype(str)
    df["subject_name"] = df.get("subject_name", "").astype(str)
    df["brand_name"] = df.get("brand_name", "").astype(str)
    df["title"] = df.get("title", "").astype(str)
    df["subject_id"] = df.get("subject_id", "").astype(str)
    df["tags"] = df.get("tags", "").astype(str)
    df["past_json"] = df.get("past_json", "").astype(str)
    df["comparison_json"] = df.get("comparison_json", "").astype(str)
    df["raw_json"] = df.get("raw_json", "").astype(str)
    new_cols = ["subject_name", "brand_name", "stock_wb", "stock_mp",
                "stock_balance_sum", "cancel_sum", "avg_orders_per_day",
                "share_order_percent", "add_to_wishlist", "time_to_ready_min",
                "localization_percent", "conv_to_cart_percent",
                "conv_cart_to_order_percent", "conv_buyout_percent",
                "product_rating", "feedback_rating", "wb_club_order_count",
                "wb_club_order_sum", "wb_club_buyout_count", "wb_club_buyout_sum",
                "wb_club_cancel_count", "wb_club_cancel_sum", "wb_club_avg_price",
                "wb_club_buyout_percent", "wb_club_avg_orders_per_day",
                "title", "subject_id", "tags", "past_json", "comparison_json",
                "raw_json"]
    agg = {"nm_id": "first", "views": "sum", "opens": "sum", "adds": "sum",
           "orders": "sum", "cancelled": "sum", "buyouts": "sum",
           "avg_price": "first", "revenue": "sum", "buyout_sum": "sum"}
    for c in new_cols:
        agg[c] = "sum" if c in {"stock_wb", "stock_mp", "add_to_wishlist",
                                "time_to_ready_min", "wb_club_order_count",
                                "wb_club_buyout_count", "wb_club_cancel_count",
                                "cancel_sum", "stock_balance_sum",
                                "wb_club_order_sum", "wb_club_buyout_sum",
                                "wb_club_cancel_sum"} else "first"
    df = df.groupby(["date_from", "date_to", "article"], as_index=False).agg(agg)
    values = [dict(r) for r in df.to_dict("records")]
    ins = insert(models.FunnelMetric)
    stmt = ins.on_conflict_do_update(
        index_elements=["date_from", "date_to", "article"],
        set_={c: ins.excluded[c] for c in ["nm_id", "views", "opens", "adds",
                                           "orders", "cancelled", "buyouts",
                                           "avg_price", "revenue", "buyout_sum"]
                                   + new_cols},
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
    df["marketplace"] = "wb"
    values = [dict(r) for r in df.to_dict("records")]
    ins = insert(models.PriceSnapshot)
    stmt = ins.on_conflict_do_update(
        index_elements=["article", "size"],
        set_={c: ins.excluded[c]
              for c in ["nm_id", "price", "discounted_price", "discount"]},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def _promo_dt(value) -> Optional[datetime]:
    """ISO datetime («2026-10-03T21:00:00Z») → naive UTC datetime (как в БД)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def upsert_promotions(db, list_df: pd.DataFrame,
                      details_df: Optional[pd.DataFrame] = None) -> int:
    """Акции WB (Календарь акций) -> wb_promotions (upsert по promo_id).

    list_df — список акций (id/name/type/startDateTime/endDateTime/description),
    details_df — подробности (participationPercentage, счётчики, ranging).
    Акции без числового id не сохраняются. Возвращает число записанных строк.
    """
    if list_df is None or list_df.empty:
        return 0
    rows: dict = {}
    for rec in list_df.to_dict("records"):
        try:
            pid = int(rec.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        rows[pid] = {
            "promo_id": pid,
            "name": str(rec.get("name") or ""),
            "adv_type": str(rec.get("type") or ""),
            "description": str(rec.get("description") or ""),
            "starts_at": _promo_dt(rec.get("startDateTime")),
            "ends_at": _promo_dt(rec.get("endDateTime")),
        }
    if details_df is not None and not details_df.empty:
        for rec in details_df.to_dict("records"):
            try:
                pid = int(rec.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if pid not in rows:
                continue
            row = rows[pid]
            if rec.get("name"):
                row["name"] = str(rec["name"])
            if rec.get("description"):
                row["description"] = str(rec["description"])
            row["participation_percent"] = _f(rec.get("participationPercentage"))
            row["in_promo_total"] = int(_f(rec.get("inPromoActionTotal", 0)))
            row["in_promo_leftovers"] = int(_f(rec.get("inPromoActionLeftovers", 0)))
            row["not_in_promo_total"] = int(_f(rec.get("notInPromoActionTotal", 0)))
            row["not_in_promo_leftovers"] = int(_f(rec.get("notInPromoActionLeftovers", 0)))
            row["exception_count"] = int(_f(rec.get("exceptionProductsCount", 0)))
            adv = rec.get("advantages")
            if isinstance(adv, list):
                row["advantages"] = ", ".join(str(x) for x in adv)
            else:
                row["advantages"] = str(adv or "")
            ranging = rec.get("ranging")
            if isinstance(ranging, str):
                row["ranging_json"] = ranging  # уже JSON-строка от провайдера
            elif ranging is not None:
                row["ranging_json"] = json.dumps(ranging, ensure_ascii=False, default=str)
    if not rows:
        return 0
    values = [rows[pid] for pid in sorted(rows)]
    ins = insert(models.Promotion)
    stmt = ins.on_conflict_do_update(
        index_elements=["promo_id"],
        set_={
            "name": ins.excluded.name,
            "adv_type": ins.excluded.adv_type,
            "description": ins.excluded.description,
            "advantages": ins.excluded.advantages,
            "starts_at": ins.excluded.starts_at,
            "ends_at": ins.excluded.ends_at,
            "participation_percent": ins.excluded.participation_percent,
            "in_promo_total": ins.excluded.in_promo_total,
            "in_promo_leftovers": ins.excluded.in_promo_leftovers,
            "not_in_promo_total": ins.excluded.not_in_promo_total,
            "not_in_promo_leftovers": ins.excluded.not_in_promo_leftovers,
            "exception_count": ins.excluded.exception_count,
            "ranging_json": ins.excluded.ranging_json,
            "fetched_at": func.now(),
        },
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def upsert_ozon_price_snapshots(db, df: pd.DataFrame) -> int:
    """Снимок цен/скидок Ozon (v5/product/info/prices) -> price_snapshots.
    Схема Ozon: offer_id (артикул), product_id, price_price (текущая),
    price_old_price (зачёркнутая), price_min_price. Артикул включает размер,
    поэтому size доопределяется резолвером (для цен по размеру/артикулу).
    """
    if df is None or df.empty:
        return 0
    if "offer_id" not in df.columns:
        return 0
    df = df.copy()

    def col(name, default=""):
        if name in df.columns:
            s = df[name]
            return pd.to_numeric(s, errors="coerce").fillna(default) \
                if not s.dtype == object else s.astype(str)
        return pd.Series(default, index=df.index)

    price = pd.to_numeric(df["price_price"], errors="coerce").fillna(0) \
        if "price_price" in df.columns else pd.Series(0, index=df.index)
    old = pd.to_numeric(df["price_old_price"], errors="coerce").fillna(0) \
        if "price_old_price" in df.columns else pd.Series(0, index=df.index)
    discount = pd.Series(0.0, index=df.index)
    m = price > 0
    discount[m] = ((1 - price[m] / old[m].where(old[m] > 0, price[m])) * 100).clip(lower=0)
    out = pd.DataFrame({
        "marketplace": "ozon",
        "article": df["offer_id"].astype(str).str.strip(),
        "nm_id": col("product_id", "").astype(str).str.strip(),
        "size": "",
        "price": price,
        "discounted_price": price,
        "discount": discount.round(2),
    })
    out = out[out["article"] != ""]
    out = out[(out["price"] > 0)].drop_duplicates("article")
    if out.empty:
        return 0
    omap = _ozon_base_map(db, out["article"])
    for rec in out.to_dict("records"):
        rec["size"] = omap.get(rec["article"], ("", "", ""))[1]
        rec["base_article"] = omap.get(rec["article"], ("", "", ""))[0] or rec["article"]
    values = [dict(r) for r in out.to_dict("records")]
    ins = insert(models.PriceSnapshot)
    stmt = ins.on_conflict_do_update(
        index_elements=["article", "size"],
        set_={c: ins.excluded[c]
              for c in ["nm_id", "base_article", "price", "discounted_price", "discount"]},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def _ozon_base_map(db, offers) -> dict:
    """Карта offer_id → (база, размер) для строк Ozon (см. app/services/ozon_article).

    Пустые строки отбрасываются; артикул без дефиса остаётся сам собой.
    """
    vals = [str(o or "").strip() for o in offers]
    return ozon_article.build_offer_map(db, offers=[v for v in vals if v])


def upsert_ozon_detail_rows(db, df: pd.DataFrame, source: str = "api") -> int:
    """Строки детализации реализаций Ozon -> ozon_detail_rows, upsert по op_key.

    op_key (дата|постинг|sku) одинаков для прямого метода и для разобранного
    фолбэк-отчёта, поэтому источник может меняться без задвоения строк.
    base_article/size заполняются по артикулу (артикул Ozon = артикул + размер).
    """
    if df is None or df.empty:
        return 0
    if "op_key" not in df.columns:
        return 0
    fields = ["op_key", "source", "date", "posting_number", "offer_id", "name",
              "sku", "barcode", "quantity", "seller_price", "amount",
              "commission_ratio", "commission", "standard_fee", "income",
              "return_qty", "return_total"]
    cols = [c for c in fields if c in df.columns]
    gdf = df[cols].copy().drop_duplicates("op_key")
    omap = _ozon_base_map(db, gdf["offer_id"])
    values = []
    for r in gdf.to_dict("records"):
        offer = str(r.get("offer_id", "") or "")
        _base, _size, _m = omap.get(offer, ("", "", ""))
        values.append({
            "op_key": str(r["op_key"]),
            "source": source,
            "date": _as_date(r.get("date")),
            "posting_number": str(r.get("posting_number", "") or ""),
            "offer_id": offer,
            "base_article": _base or offer,
            "size": _size,
            "name": str(r.get("name", "") or ""),
            "sku": str(r.get("sku", "") or ""),
            "barcode": str(r.get("barcode", "") or ""),
            "quantity": int(_f(r.get("quantity"))),
            "seller_price": _f(r.get("seller_price")),
            "amount": _f(r.get("amount")),
            "commission_ratio": _f(r.get("commission_ratio")),
            "commission": _f(r.get("commission")),
            "standard_fee": _f(r.get("standard_fee")),
            "income": _f(r.get("income")),
            "return_qty": int(_f(r.get("return_qty"))),
            "return_total": _f(r.get("return_total")),
        })
    if not values:
        return 0
    ins = insert(models.OzonDetailRow)
    set_cols = [c for c in values[0].keys() if c != "op_key"]
    stmt = ins.on_conflict_do_update(
        index_elements=["op_key"],
        set_={c: ins.excluded[c] for c in set_cols},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def upsert_ozon_buyouts(db, df: pd.DataFrame) -> int:
    """Выкупы Ozon -> ozon_buyouts, upsert по op_key (постинг|sku).

    base_article/size заполняются по артикулу, чтобы свод выкупов можно было
    строить по товару и искать по базовому артикулу.
    """
    if df is None or df.empty:
        return 0
    if "op_key" not in df.columns:
        return 0
    fields = ["op_key", "posting_number", "offer_id", "name", "sku", "quantity",
              "seller_price", "buyout_price", "amount",
              "deduction_by_category_percent", "vat_percent"]
    cols = [c for c in fields if c in df.columns]
    gdf = df[cols].copy().drop_duplicates("op_key")
    omap = _ozon_base_map(db, gdf["offer_id"])
    values = []
    for r in gdf.to_dict("records"):
        art = str(r.get("offer_id", "") or "")
        _base, _size, _m = omap.get(art, ("", "", ""))
        values.append({
            "op_key": str(r["op_key"]),
            "posting_number": str(r.get("posting_number", "") or ""),
            "offer_id": art,
            "base_article": _base or art,
            "size": _size,
            "name": str(r.get("name", "") or ""),
            "sku": str(r.get("sku", "") or ""),
            "quantity": int(_f(r.get("quantity"))),
            "seller_price": _f(r.get("seller_price")),
            "buyout_price": _f(r.get("buyout_price")),
            "amount": _f(r.get("amount")),
            "deduction_by_category_percent": _f(r.get("deduction_by_category_percent")),
            "vat_percent": int(_f(r.get("vat_percent"))),
        })
    if not values:
        return 0
    ins = insert(models.OzonBuyout)
    set_cols = [c for c in values[0].keys() if c != "op_key"]
    stmt = ins.on_conflict_do_update(
        index_elements=["op_key"],
        set_={c: ins.excluded[c] for c in set_cols},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def upsert_ozon_placements(db, df: pd.DataFrame) -> int:
    """Стоимость размещения Ozon -> ozon_placements, upsert по op_key.

    base_article/size заполняются по артикулу, чтобы «Хранение» можно было
    суммировать по товару, а не по размеру.
    """
    if df is None or df.empty:
        return 0
    if "op_key" not in df.columns:
        return 0
    fields = ["op_key", "date", "sku", "offer_id", "warehouse",
              "paid_quantity", "paid_volume", "storage"]
    cols = [c for c in fields if c in df.columns]
    gdf = df[cols].copy().drop_duplicates("op_key")
    omap = _ozon_base_map(db, gdf["offer_id"])
    values = []
    for r in gdf.to_dict("records"):
        offer = str(r.get("offer_id", "") or "")
        _base, _size, _m = omap.get(offer, ("", "", ""))
        values.append({
            "op_key": str(r["op_key"]),
            "date": _as_date(r.get("date")),
            "sku": str(r.get("sku", "") or ""),
            "offer_id": offer,
            "base_article": _base or offer,
            "size": _size,
            "warehouse": str(r.get("warehouse", "") or ""),
            "paid_quantity": int(_f(r.get("paid_quantity"))),
            "paid_volume": _f(r.get("paid_volume")),
            "storage": _f(r.get("storage")),
        })
    if not values:
        return 0
    ins = insert(models.OzonPlacement)
    set_cols = [c for c in values[0].keys() if c != "op_key"]
    stmt = ins.on_conflict_do_update(
        index_elements=["op_key"],
        set_={c: ins.excluded[c] for c in set_cols},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def upsert_ozon_cashflows(db, df: pd.DataFrame) -> int:
    """Движение средств Ozon -> ozon_cash_flows, upsert по op_key."""
    if df is None or df.empty:
        return 0
    if "op_key" not in df.columns:
        return 0
    fields = ["op_key", "period_begin", "period_end", "begin_balance",
              "payments_amount", "delivery_total", "return_total",
              "services_total", "others_total", "end_balance"]
    cols = [c for c in fields if c in df.columns]
    gdf = df[cols].copy().drop_duplicates("op_key")
    values = []
    for r in gdf.to_dict("records"):
        values.append({
            "op_key": str(r["op_key"]),
            "period_begin": _as_date(r.get("period_begin")),
            "period_end": _as_date(r.get("period_end")),
            "begin_balance": _f(r.get("begin_balance")),
            "payments_amount": _f(r.get("payments_amount")),
            "delivery_total": _f(r.get("delivery_total")),
            "return_total": _f(r.get("return_total")),
            "services_total": _f(r.get("services_total")),
            "others_total": _f(r.get("others_total")),
            "end_balance": _f(r.get("end_balance")),
        })
    if not values:
        return 0
    ins = insert(models.OzonCashFlow)
    set_cols = [c for c in values[0].keys() if c != "op_key"]
    stmt = ins.on_conflict_do_update(
        index_elements=["op_key"],
        set_={c: ins.excluded[c] for c in set_cols},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def normalize_ozon_accrual(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Строки начислений Ozon (/v1/finance/accrual/by-day) -> схема ozon_accruals.

    op_key приходит от провайдера (date|accrual_id|корзина|тип|sku|seq) — повторные
    загрузки того же дня идемпотентно перезаписывают строки. offer_id дорезолвится
    при записи (upsert_ozon_accruals), если провайдер его не проставил.
    """
    if df is None or df.empty:
        return None
    need = {"op_key", "date", "bucket", "type_id", "sku", "amount"}
    if not need.issubset(df.columns):
        return None
    df = df.copy()
    dates = pd.to_datetime(df["date"], errors="coerce")

    def txt(col, default=""):
        if col not in df.columns:
            return pd.Series(default, index=df.index)
        return df[col].fillna("").astype(str).str.strip()

    def num(col, default=0):
        if col not in df.columns:
            return pd.Series(default, index=df.index)
        return pd.to_numeric(df[col], errors="coerce").fillna(default)

    out = pd.DataFrame({
        "op_key": txt("op_key"),
        "date": dates.dt.date,
        "accrual_id": txt("accrual_id", ""),
        "bucket": txt("bucket", ""),
        "type_id": num("type_id", 0).astype(int),
        "sku": txt("sku", ""),
        "offer_id": txt("offer_id", ""),
        "unit_number": txt("unit_number", ""),
        "quantity": num("quantity", 0).astype(int),
        "amount": num("amount", 0),
        "seller_price": num("seller_price", 0),
        "sale_price": num("sale_price", 0),
    })
    out = out.dropna(subset=["date"])
    out = out[out["op_key"] != ""]
    return out


def upsert_ozon_accruals(db, df: pd.DataFrame) -> int:
    """Начисления Ozon -> ozon_accruals, upsert по op_key.

    offer_id дорезолвится по SKU из ozon_detail_rows (если провайдер его
    не отдал) — детализация знает пары sku -> offer_id за загруженные месяцы.
    """
    if df is None or df.empty:
        return 0
    if "op_key" not in df.columns:
        return 0
    gdf = normalize_ozon_accrual(df)
    if gdf is None or gdf.empty:
        return 0
    # Дорезолв offer_id по картам sku -> offer_id из ozon_detail_rows.
    sku_map: dict = {}
    for sku, offer in db.execute(
        select(models.OzonDetailRow.sku, models.OzonDetailRow.offer_id)
        .where(models.OzonDetailRow.sku != "",
               models.OzonDetailRow.offer_id != "")
        .distinct()
    ).all():
        sku_map.setdefault(str(sku).strip(), str(offer).strip())
    if sku_map:
        keys = gdf["sku"].astype(str).str.strip()
        gdf["offer_id"] = gdf["offer_id"].fillna("").astype(str).str.strip().replace("", pd.NA)
        gdf["offer_id"] = gdf["offer_id"].fillna(keys.map(sku_map)).fillna("")
    fields = ["op_key", "date", "accrual_id", "bucket", "type_id", "sku",
              "offer_id", "unit_number", "quantity", "amount",
              "seller_price", "sale_price"]
    cols = [c for c in fields if c in gdf.columns]
    gdf = gdf[cols].copy().drop_duplicates("op_key")
    omap = _ozon_base_map(db, gdf["offer_id"])
    values = []
    for r in gdf.to_dict("records"):
        offer = str(r.get("offer_id", "") or "")
        _base, _size, _m = omap.get(offer, ("", "", ""))
        values.append({
            "op_key": str(r["op_key"]),
            "date": _as_date(r.get("date")),
            "accrual_id": str(r.get("accrual_id", "") or ""),
            "bucket": str(r.get("bucket", "") or ""),
            "type_id": int(_f(r.get("type_id"))),
            "sku": str(r.get("sku", "") or ""),
            "offer_id": offer,
            "base_article": _base or offer,
            "size": _size,
            "unit_number": str(r.get("unit_number", "") or ""),
            "quantity": int(_f(r.get("quantity"))),
            "amount": _f(r.get("amount")),
            "seller_price": _f(r.get("seller_price")),
            "sale_price": _f(r.get("sale_price")),
        })
    if not values:
        return 0
    ins = insert(models.OzonAccrual)
    set_cols = [c for c in values[0].keys() if c != "op_key"]
    stmt = ins.on_conflict_do_update(
        index_elements=["op_key"],
        set_={c: ins.excluded[c] for c in set_cols},
    )
    db.execute(stmt, values)
    db.commit()
    return len(values)


def oz_detail_summary_dataframe(
    db,
    date_from=None,
    date_to=None,
    article_like: Optional[str] = None,
    by_size: bool = False,
) -> pd.DataFrame:
    """Свод «Детализации реализаций Ozon» по артикулам (постинги + выкупы).

    Считается напрямую из ozon_detail_rows (сырые деньги операций без
    себестоимости, как и в WB-версии). Продажа с возвратом даёт строку только в
    том же постинге; если возврат идёт отдельным постингом — отдельная строка,
    но на той же дате/артикуле: возврат учитывается отдельно (returns_qty и в
    кол-ве, и в деньгах income/amount), чтобы «Продано» оставалось гроссом.
    Выкупы (ozon_buyouts) добавляют к артикулу сумму и % выкупа к продажам.

    by_size=False (по умолчанию) — строка это товар: артикулы размеров свёрнуты
    в базовый артикул (app/services/ozon_article.py), в колонках размеры и
    артикулы показываются количеством. by_size=True — строка это артикул
    конкретного размера (прежнее поведение).
    """
    cols = ["article", "name", "size", "sizes_count", "offers_count",
            "sells", "returns_qty", "postings",
            "seller_total", "amount", "commission", "services", "income",
            "ops_count", "buyout_sum", "buyout_percent", "storage"]
    out = pd.DataFrame(columns=cols)
    q = select(models.OzonDetailRow)
    if date_from:
        q = q.where(models.OzonDetailRow.date >= date_from)
    if date_to:
        q = q.where(models.OzonDetailRow.date <= date_to)
    if article_like:
        q = q.where(models.OzonDetailRow.offer_id.ilike(f"%{article_like}%")
                    | models.OzonDetailRow.base_article.ilike(f"%{article_like}%"))
    rows = list(db.execute(q).scalars().all())
    if not rows:
        return out

    # Стоимость размещения (хранение) по артикулам за окно: ozon_placements.
    # Из отчёта /v1/report/placement/by-products/create, отрицательная = расход.
    storage_by_art: dict = {}
    pq = select(models.OzonPlacement)
    if date_from:
        pq = pq.where(models.OzonPlacement.date >= date_from)
    if date_to:
        pq = pq.where(models.OzonPlacement.date <= date_to)
    if article_like:
        pq = pq.where(models.OzonPlacement.offer_id.ilike(f"%{article_like}%")
                      | models.OzonPlacement.base_article.ilike(f"%{article_like}%"))
    for p in db.execute(pq).scalars().all():
        art = (p.offer_id or "").strip()
        if not art:
            continue
        storage_by_art[art] = storage_by_art.get(art, 0.0) + float(p.storage or 0)

    # Выкупы: у API нет дат, агрегируем по всей таблице (с тем же фильтром артикула).
    bq = select(models.OzonBuyout)
    if article_like:
        bq = bq.where(models.OzonBuyout.offer_id.ilike(f"%{article_like}%")
                      | models.OzonBuyout.base_article.ilike(f"%{article_like}%"))
    buyot = {}
    for r in db.execute(bq).scalars().all():
        art = (r.offer_id or "").strip().upper()
        if not art:
            continue
        buyot[art] = buyot.get(art, 0.0) + float(r.amount or 0)

    # Группировка: базовый артикул (товар) либо полный артикул размера.
    omap = ozon_article.build_offer_map(
        db, offers={(r.offer_id or "").strip() for r in rows}
        | set(storage_by_art) | {k.lower() for k in buyot})

    def _key(art: str) -> str:
        return ozon_article.group_key(art, omap, by_size)

    cells: dict = {}
    postings: dict = {}
    titles: dict = {}
    group_sizes: dict = {}
    group_offers: dict = {}
    for r in rows:
        art = (r.offer_id or "").strip()
        if not art or r.date is None:
            continue
        key = _key(art)
        c = cells.setdefault(key, [0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0])
        c[0] += int(r.quantity or 0)                       # sells (гросс)
        c[1] += int(r.return_qty or 0)                     # returns_qty
        c[2] += float(r.seller_price or 0) * int(r.quantity or 0)   # seller_total
        c[3] += float(r.amount or 0)                       # amount (база)
        c[4] += float(r.commission or 0)                   # commission (в минус)
        c[5] += float(r.standard_fee or 0)                 # services
        c[6] += float(r.income or 0)                       # income
        c[7] += 1                                          # ops_count
        postings.setdefault(key, set()).add(str(r.posting_number or ""))
        if (r.name or "").strip():
            titles.setdefault(key, str(r.name).strip())
        _sz = (r.size or "").strip() or ozon_article.size_of(art, omap)
        if _sz:
            group_sizes.setdefault(key, set()).add(_sz)
        group_offers.setdefault(key, set()).add(art)

    recs = []
    for key, c in cells.items():
        (sells, returns_qty, seller_total, amount, commission, services,
         income, ops) = c
        offers = sorted(group_offers.get(key) or ([key] if by_size else []))
        bsum = sum(buyot.get(a.upper(), 0.0) for a in offers) or None
        b_sum = round(bsum, 2) if bsum else 0.0
        b_pct = round(bsum / seller_total * 100, 2) \
            if bsum and seller_total > 0 else 0.0
        storage = sum(storage_by_art.get(a, 0.0) for a in offers) if offers else 0.0
        recs.append({
            "article": key, "name": titles.get(key, ""),
            "size": (ozon_article.size_of(offers[0], omap) if by_size and offers else ""),
            "sizes_count": len(group_sizes.get(key) or ()),
            "offers_count": len(offers) or 1,
            "sells": sells, "returns_qty": returns_qty,
            "postings": len(postings.get(key, set())),
            "seller_total": round(seller_total, 2),
            "amount": round(amount, 2), "commission": round(commission, 2),
            "services": round(services, 2), "income": round(income, 2),
            "ops_count": ops,
            "buyout_sum": b_sum, "buyout_percent": b_pct,
            "storage": round(storage, 2),
        })
    out = pd.DataFrame(recs)
    out = out.sort_values("income", ascending=False).reset_index(drop=True)
    return out


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