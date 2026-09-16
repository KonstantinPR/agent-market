"""Синхронизация данных провайдеров в PostgreSQL (upsert-логика)."""
from datetime import timedelta
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
    if article_like:
        q = q.where(models.WbDetailRow.article.ilike(f"%{article_like}%"))
    rows = list(db.execute(q).scalars().all())
    if not rows:
        return out

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
        target_articles={art.strip().upper() for art in cells},
    )

    recs = []
    for art, c in cells.items():
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
    out = pd.DataFrame(recs)
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
            "quantity_full": int(row["quantity_full"]),
            "in_way": int(row["in_way"]),
        })
    ins = insert(models.Stock)
    stmt = ins.on_conflict_do_update(
        index_elements=["marketplace_id", "date", "article", "warehouse", "chrt_id"],
        set_={"quantity": ins.excluded.quantity,
              "quantity_full": ins.excluded.quantity_full,
              "in_way": ins.excluded.in_way,
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


def upsert_ozon_price_snapshots(db, df: pd.DataFrame) -> int:
    """Снимок цен/скидок Ozon (v5/product/info/prices) -> price_snapshots.

    Схема Ozon: offer_id (артикул), product_id, price_price (текущая),
    price_old_price (зачёркнутая), price_min_price. size у Ozon один на карточку.
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
    values = [dict(r) for r in out.to_dict("records")]
    ins = insert(models.PriceSnapshot)
    stmt = ins.on_conflict_do_update(
        index_elements=["article", "size"],
        set_={c: ins.excluded[c]
              for c in ["nm_id", "price", "discounted_price", "discount"]},
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