import re
from datetime import date, timedelta
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.providers import factory as provider_factory
from app.providers.ozon import OZON_RU_COLUMNS
from app.providers.wb import DETAIL_RU_COLUMNS, SALES_RU_COLUMNS, V5_RU_COLUMNS
from app.services import (
    common as common_service,
    excel_io,
    margin as margin_service,
    refresh as refresh_service,
    sync as sync_service,
)
from app.services.window import parse_window

router = APIRouter(prefix="/api")


def _parse_window400(date_from=None, date_to=None):
    """parse_window + превращает некорректную дату в HTTP 400 (а не 500)."""
    try:
        return parse_window(date_from, date_to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Некорректная дата: {e}")


@router.get("/sales")
def api_sales(
    marketplace: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    query = (
        select(
            models.Marketplace.code.label("marketplace"),
            models.Sale.date,
            models.Sale.article,
            func.max(models.Product.name).label("name"),
            func.sum(models.Sale.quantity).label("quantity"),
            func.sum(models.Sale.revenue).label("revenue"),
            func.sum(models.Sale.income).label("income"),
        )
        .select_from(models.Sale)
        .join(models.Marketplace, models.Sale.marketplace_id == models.Marketplace.id)
        .join(models.Product, models.Sale.article == models.Product.article)
        .where(models.Sale.date >= from_, models.Sale.date <= to_,
               models.Sale.source != "detail")
        .group_by(models.Marketplace.code, models.Sale.date, models.Sale.article)
        .order_by(models.Sale.date.desc(), models.Sale.article)
    )
    if marketplace:
        query = query.where(models.Marketplace.code == marketplace)

    rows = [
        {
            "date": str(r.date),
            "marketplace": r.marketplace,
            "article": r.article,
            "name": r.name,
            "quantity": int(r.quantity or 0),
            "revenue": float(r.revenue or 0),
            "income": float(r.income or 0),
        }
        for r in db.execute(query)
    ]
    return {"rows": rows, "count": len(rows), "date_from": str(from_), "date_to": str(to_)}


@router.get("/margin")
def api_margin(
    marketplace: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    source: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    mp_ids = common_service.resolve_marketplace_ids(db, marketplace)

    df = margin_service.margin_dataframe(
        db, date_from=from_, date_to=to_, marketplace=mp_ids,
        article_like=article_like, source=source,
    )
    return {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
    }


@router.get("/margin/detail")
def api_margin_detail(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Прибыльность по Детализации Продаж WB (строки sales с source='detail')."""
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.margin_dataframe(
        db, date_from=from_, date_to=to_, marketplace=None,
        article_like=article_like, source="detail",
    )
    return {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
    }


@router.get("/margin/funnel")
def api_margin_funnel(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Прибыльность по Воронке Продаж WB (данные funnel_metric, оценка)."""
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.funnel_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like
    )
    return {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "snapshot_from": df.attrs.get("date_from", ""),
        "snapshot_to": df.attrs.get("date_to", ""),
    }


@router.get("/pulls")
def api_pulls(db: Session = Depends(get_db)):
    """Лог последних успешных загрузок из API маркетплейсов."""
    from sqlalchemy import desc

    rows = db.execute(
        select(
            models.ApiPull.api, models.ApiPull.kind,
            models.ApiPull.last_success_at, models.ApiPull.rows,
            models.ApiPull.db_rows, models.ApiPull.window,
        ).order_by(desc(models.ApiPull.last_success_at))
    ).all()
    return [
        {
            "api": api, "kind": kind,
            "last_success_at": last_success_at.isoformat(sep=" ") if last_success_at else None,
            "rows": rows_n, "db_rows": db_rows, "window": window,
        }
        for api, kind, last_success_at, rows_n, db_rows, window in rows
    ]


def _real_ozon_articles(db, from_: date, to_: date):
    """Реальные артикулы Ozon = offer_id из живого /v2/finance/realization за месяцы окна."""
    real: set = set()
    months = set()
    yy, mm = from_.year, from_.month
    while True:
        months.add((yy, mm))
        if (yy, mm) == (to_.year, to_.month):
            break
        mm += 1
        if mm == 13:
            mm, yy = 1, yy + 1

    ok, fail = 0, 0
    try:
        prov = provider_factory.get_oz_provider()
        for year_, month_ in sorted(months):
            try:
                df = prov.get_realization(month_, year_)
                ok += 1
                for offer_id in df["offer_id"]:
                    s = str(offer_id).strip()
                    if s:
                        real.add(s)
            except Exception:  # noqa: BLE001
                fail += 1
    except Exception as exc:  # noqa: BLE001
        return real, f"OzonProvider не доступен: {exc}"

    if real or (ok and not fail):
        return real, f"live /v2/finance/realization (ok={ok}, fail={fail})"

    # фоллбэк-эвристика: реальные артикулы Ozon не выглядят как моки
    q = (
        select(models.Sale.article)
        .join(models.Marketplace, models.Sale.marketplace_id == models.Marketplace.id)
        .where(models.Marketplace.code == "ozon")
    )
    for (art,) in db.execute(q):
        s = str(art).strip()
        if s and not re.fullmatch(r"\d+", s) and not re.fullmatch(r"JBG-10\d{2}", s):
            real.add(s)
    return real, f"fallback-heuristic (live недоступен: ok={ok}, fail={fail})"


def _pick_test_articles(db, by_art, real_ozon):
    """Автоотбор: реальные Ozon с себестоимостью + без неё (демо), плюс WB."""
    def key(a):
        return by_art[a]["income"]

    real_cost = sorted(
        (a for a in real_ozon if a in by_art and by_art[a]["net_cost"] > 0),
        key=key, reverse=True,
    )[:7]
    real_nocost = sorted(
        (a for a in real_ozon if a in by_art
         and by_art[a]["net_cost"] <= 0 and by_art[a]["sells"] > 0),
        key=key, reverse=True,
    )[:2]
    wb = sorted(
        (a for a in by_art if "wb" in by_art[a]["marketplaces"]
         and by_art[a]["net_cost"] > 0),
        key=key, reverse=True,
    )[:2]
    return real_cost + real_nocost + wb


@router.get("/margin/test")
def api_margin_test(
    articles: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Тест маржинальности на выборке товаров (read-only).

    Сверяет значения из БД с эталоном реальных артикулов Ozon
    (живой /v2/finance/realization) и ставит флаги источника/себестоимости.
    """
    from_, to_ = _parse_window400(date_from, date_to)

    real_ozon, prov_note = _real_ozon_articles(db, from_, to_)

    agg_q = (
        select(
            models.Marketplace.code.label("marketplace"),
            models.Sale.article,
            func.max(models.Product.name).label("name"),
            func.sum(models.Sale.quantity).label("sells"),
            func.sum(models.Sale.returns_qty).label("returns_qty"),
            func.sum(models.Sale.revenue).label("revenue"),
            func.sum(models.Sale.commission).label("commission"),
            func.sum(models.Sale.logistics).label("logistics"),
            func.sum(models.Sale.storage).label("storage"),
            func.sum(models.Sale.services).label("services"),
            func.sum(models.Sale.income).label("income"),
            func.max(models.Product.net_cost).label("net_cost"),
        )
        .select_from(models.Sale)
        .join(models.Marketplace, models.Sale.marketplace_id == models.Marketplace.id)
        .join(models.Product, models.Sale.article == models.Product.article)
        .where(models.Sale.date >= from_, models.Sale.date <= to_)
        .group_by(models.Marketplace.code, models.Sale.article)
        .order_by(models.Sale.article)
    )

    by_art: dict = {}
    for r in db.execute(agg_q):
        mp, art = r.marketplace, str(r.article)
        rec = by_art.setdefault(art, {
            "article": art, "name": r.name or "",
            "marketplaces": [], "sells": 0, "returns_qty": 0,
            "revenue": 0.0, "commission": 0.0, "logistics": 0.0,
            "storage": 0.0, "services": 0.0, "income": 0.0,
            "net_cost": float(r.net_cost or 0),
            "income_ozon": 0.0, "income_wb": 0.0,
        })
        rec["name"] = rec["name"] or (r.name or "")
        rec["marketplaces"].append(mp)
        rec["sells"] += int(r.sells or 0)
        rec["returns_qty"] += int(r.returns_qty or 0)
        rec["revenue"] += float(r.revenue or 0)
        rec["commission"] += float(r.commission or 0)
        rec["logistics"] += float(r.logistics or 0)
        rec["storage"] += float(r.storage or 0)
        rec["services"] += float(r.services or 0)
        rec["income"] += float(r.income or 0)
        if mp == "ozon":
            rec["income_ozon"] += float(r.income or 0)
        elif mp == "wb":
            rec["income_wb"] += float(r.income or 0)

    requested = None
    if articles:
        requested = [a.strip() for a in articles.split(",") if a.strip()]

    tested = requested if requested is not None else _pick_test_articles(db, by_art, real_ozon)
    if requested is not None:
        tested = [a for a in requested if a in by_art]

    rows = []
    for art in tested:
        rec = by_art[art]
        nc = rec["net_cost"]
        margin = rec["income"] - nc * rec["sells"]
        margin_per_one = margin / rec["sells"] if rec["sells"] else 0.0
        margin_pct = margin / rec["income"] * 100 if rec["income"] else 0.0
        comp = rec["revenue"] + rec["commission"] + rec["logistics"] + rec["storage"] + rec["services"]
        other = rec["income"] - comp
        src_parts = []
        if "ozon" in rec["marketplaces"]:
            src_parts.append("Ozon (реал)" if art in real_ozon else "Ozon (мок)")
        if "wb" in rec["marketplaces"]:
            src_parts.append("WB")
        rows.append({
            "article": art,
            "name": rec["name"],
            "marketplaces": rec["marketplaces"],
            "source": " + ".join(src_parts),
            "is_real": art in real_ozon,
            "has_net_cost": nc > 0,
            "sells": rec["sells"],
            "returns_qty": rec["returns_qty"],
            "revenue": round(rec["revenue"], 2),
            "commission": round(rec["commission"], 2),
            "logistics": round(rec["logistics"], 2),
            "storage": round(rec["storage"], 2),
            "services": round(rec["services"], 2),
            "other": round(other, 2),
            "income": round(rec["income"], 2),
            "income_ozon": round(rec["income_ozon"], 2),
            "income_wb": round(rec["income_wb"], 2),
            "net_cost": nc,
            "cost_total": round(nc * rec["sells"], 2),
            "margin": round(margin, 2),
            "margin_per_one": round(margin_per_one, 2),
            "margin_pct": round(margin_pct, 2),
            "identity_ok": abs(other) <= 0.02,
        })
    rows.sort(key=lambda x: (-x["sells"], x["article"]))

    sold_no_cost = sorted(
        a for a, rec in by_art.items() if rec["net_cost"] <= 0 and rec["sells"] > 0
    )
    ozon_mock_left = sum(
        1 for a, rec in by_art.items()
        if "ozon" in rec["marketplaces"] and a not in real_ozon and rec["sells"] > 0
    )

    return {
        "rows": rows,
        "count": len(rows),
        "date_from": str(from_),
        "date_to": str(to_),
        "meta": {
            "real_ozon": sorted(real_ozon),
            "real_ozon_note": prov_note,
            "sold_without_cost": len(sold_no_cost),
            "sold_without_cost_sample": sold_no_cost[:10],
            "ozon_mock_leftovers": ozon_mock_left,
            "articles_param": requested is not None,
        },
    }


@router.get("/dashboard")
def api_dashboard(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    query = (
        select(
            models.Marketplace.code.label("marketplace"),
            func.sum(models.Sale.quantity).label("sells"),
            func.sum(models.Sale.revenue).label("revenue"),
            func.sum(models.Sale.income).label("income"),
            func.sum(models.Sale.commission).label("commission"),
            func.sum(models.Sale.logistics).label("logistics"),
            func.sum(models.Sale.storage).label("storage"),
        )
        .select_from(models.Sale)
        .join(models.Marketplace, models.Sale.marketplace_id == models.Marketplace.id)
        .where(models.Sale.date >= from_, models.Sale.date <= to_,
               models.Sale.source != "detail")
        .group_by(models.Marketplace.code)
    )
    per_mp = []
    total = {"sells": 0, "revenue": 0.0, "income": 0.0}
    for r in db.execute(query):
        per_mp.append({
            "marketplace": r.marketplace,
            "sells": int(r.sells or 0),
            "revenue": float(r.revenue or 0),
            "income": float(r.income or 0),
            "commission": float(r.commission or 0),
            "logistics": float(r.logistics or 0),
            "storage": float(r.storage or 0),
        })
        total["sells"] += int(r.sells or 0)
        total["revenue"] += float(r.revenue or 0)
        total["income"] += float(r.income or 0)

    daily_q = (
        select(
            models.Sale.date,
            func.sum(models.Sale.revenue).label("revenue"),
            func.sum(models.Sale.income).label("income"),
            func.sum(models.Sale.quantity).label("sells"),
        )
        .where(models.Sale.date >= from_, models.Sale.date <= to_,
               models.Sale.source != "detail")
        .group_by(models.Sale.date)
        .order_by(models.Sale.date)
    )
    daily = [
        {
            "date": str(r.date),
            "revenue": float(r.revenue or 0),
            "income": float(r.income or 0),
            "sells": int(r.sells or 0),
        }
        for r in db.execute(daily_q)
    ]
    return {
        "per_marketplace": per_mp,
        "total": total,
        "daily": daily,
        "date_from": str(from_),
        "date_to": str(to_),
    }


@router.get("/stocks")
def api_stocks(
    marketplace: Optional[str] = None,
    db: Session = Depends(get_db),
):
    latest = db.scalar(select(func.max(models.Stock.date)))
    query = (
        select(
            models.Marketplace.code.label("marketplace"),
            models.Stock.article,
            models.Product.name,
            models.Stock.warehouse,
            models.Stock.quantity,
        )
        .select_from(models.Stock)
        .join(models.Marketplace, models.Stock.marketplace_id == models.Marketplace.id)
        .join(models.Product, models.Stock.article == models.Product.article)
        .where(models.Stock.date == latest)
    )
    if marketplace:
        query = query.where(models.Marketplace.code == marketplace)

    rows = [
        {
            "date": str(latest),
            "marketplace": r.marketplace,
            "article": r.article,
            "name": r.name,
            "warehouse": r.warehouse,
            "quantity": int(r.quantity or 0),
        }
        for r in db.execute(query)
    ]
    return {"date": str(latest), "rows": rows, "count": len(rows)}


@router.get("/products")
def api_products(db: Session = Depends(get_db)):
    rows = [
        {
            "article": r.article,
            "name": r.name,
            "brand": r.brand,
            "barcode": r.barcode,
            "net_cost": float(r.net_cost or 0),
        }
        for r in db.execute(
            select(
                models.Product.article,
                models.Product.name,
                models.Product.brand,
                models.Product.barcode,
                models.Product.net_cost,
            ).order_by(models.Product.article)
        )
    ]
    return {"rows": rows, "count": len(rows)}


@router.get("/custom-stock")
def api_custom_stock(db: Session = Depends(get_db)):
    rows = [
        {
            "article": r.article,
            "name": r.name,
            "quantity": int(r.quantity or 0),
            "net_cost": float(r.net_cost or 0),
            "updated_at": str(r.updated_at) if r.updated_at else "",
        }
        for r in db.execute(
            select(
                models.CustomStock.article,
                models.CustomStock.quantity,
                models.CustomStock.net_cost,
                models.CustomStock.updated_at,
                models.Product.name,
            )
            .select_from(models.CustomStock)
            .join(models.Product, models.CustomStock.article == models.Product.article, isouter=True)
        )
    ]
    return {"rows": rows, "count": len(rows)}


@router.post("/import/products")
async def import_products(file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read()
    df = excel_io.read_excel_bytes(data)
    df = df.rename(columns={
        "Артикул поставщика": "article",
        "Артикул": "article",
        "article": "article",
        "Себестоимость": "net_cost",
        "Себестоимость БАЗА": "net_cost",
        "net_cost": "net_cost",
        "Наименование": "name",
        "name": "name",
    })
    n = sync_service.upsert_products(db, df)
    return {"imported": n, "total": len(df), "filename": file.filename}


@router.post("/import/net-cost")
async def import_net_cost(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Обновляет себестоимость (net_cost) товаров из файла с колонками article/net_cost."""
    data = await file.read()
    df = excel_io.read_excel_bytes(data)
    df = df.rename(columns={
        "Артикул": "article",
        "article": "article",
        "Себестоимость": "net_cost",
        "net_cost": "net_cost",
    })
    if "article" not in df.columns or "net_cost" not in df.columns:
        raise HTTPException(status_code=400, detail="В файле нет колонок article и net_cost")
    n = sync_service.upsert_products(db, df)
    filled = common_service.count_products_with_cost(db)
    return {"imported": n, "total": len(df), "with_cost_total": filled, "filename": file.filename}


@router.post("/import/custom-stock")
async def import_custom_stock(file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read()
    df = excel_io.read_excel_bytes(data)
    df = df.rename(columns={
        "Артикул": "article",
        "Количество": "quantity",
        "Кол-во": "quantity",
        "Закупочная цена": "net_cost",
        "Закуп. цена": "net_cost",
        "Себестоимость": "net_cost",
    })
    n = sync_service.upsert_custom_stock(db, df)
    return {"imported": n, "filename": file.filename}


@router.get("/export/margin")
def export_margin(
    marketplace: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    mp_ids = common_service.resolve_marketplace_ids(db, marketplace)
    df = margin_service.margin_dataframe(
        db, date_from=from_, date_to=to_, marketplace=mp_ids, article_like=article_like
    )
    df = df.rename(columns={
        "article": "Артикул", "name": "Наименование", "sells": "Продано, шт",
        "revenue": "Выручка, руб", "commission": "Комиссия, руб",
        "logistics": "Логистика, руб", "storage": "Хранение, руб",
        "services": "Услуги, руб", "income": "К перечислению, руб",
        "net_cost": "Себестоимость, руб", "other": "Прочее, руб",
        "margin": "Маржа, руб",
        "margin_per_one": "Маржа на ед., руб", "margin_pct": "Маржа, %",
    })
    buf = excel_io.df_to_excel_stream(df, sheet_name="Маржа")
    fname = f"margin_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/margin/detail")
def export_margin_detail(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.margin_dataframe(
        db, date_from=from_, date_to=to_, marketplace=None,
        article_like=article_like, source="detail",
    )
    df = df.rename(columns={
        "article": "Артикул", "name": "Наименование", "sells": "Продано, шт",
        "revenue": "Выручка, руб", "commission": "Комиссия, руб",
        "logistics": "Логистика, руб", "storage": "Хранение, руб",
        "services": "Услуги, руб", "income": "К перечислению, руб",
        "net_cost": "Себестоимость, руб", "other": "Прочее, руб",
        "margin": "Маржа, руб",
        "margin_per_one": "Маржа на ед., руб", "margin_pct": "Маржа, %",
    })
    buf = excel_io.df_to_excel_stream(df, sheet_name="Маржа")
    fname = f"margin_detail_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/margin/funnel")
def export_margin_funnel(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.funnel_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like
    )
    df = df.rename(columns={
        "article": "Артикул", "name": "Наименование", "views": "Просмотры",
        "opens": "Открытия карточки", "adds": "В корзину", "orders": "Заказы",
        "cancelled": "Отмены", "avg_price": "Ср. цена, руб",
        "revenue": "Выручка (оценка), руб",
        "cart_pct": "В корзину, %", "order_pct": "Заказы, %",
        "net_cost": "Себестоимость, руб", "margin": "Маржа (оценка), руб",
        "margin_pct": "Маржа, %",
    })
    buf = excel_io.df_to_excel_stream(df, sheet_name="Воронка")
    fname = f"margin_funnel_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/sales")
def export_sales(
    marketplace: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    payload = api_sales(marketplace, date_from, date_to, db)
    df = pd.DataFrame(payload["rows"])
    df = df.rename(columns={
        "date": "Дата", "marketplace": "Маркетплейс", "article": "Артикул",
        "name": "Наименование", "quantity": "Продано, шт",
        "revenue": "Выручка, руб", "income": "К перечислению, руб",
    })
    buf = excel_io.df_to_excel_stream(df, sheet_name="Продажи")
    fname = f"sales_{payload['date_from']}_{payload['date_to']}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/sync/{code}")
def api_sync(code: str, date_from: Optional[str] = None, date_to: Optional[str] = None):
    if code not in ("wb", "ozon"):
        return {"error": "unknown marketplace"}, 400
    from_, to_ = _parse_window400(date_from, date_to)
    return sync_service.sync_sales_window(code, from_, to_)


# ---------------------------------------------------------------------------
# Загрузки данных через WB API (раздел «WB API» в меню)
# ---------------------------------------------------------------------------
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_response(df: pd.DataFrame, filename: str, count: int):
    buf = excel_io.df_to_excel_stream(df)
    return StreamingResponse(
        buf,
        media_type=XLSX_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Count": str(count),
        },
    )


@router.post("/wb/cards")
def wb_cards(write_db: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_wb_cards(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    return _xlsx_response(res["df"], "wb_cards.xlsx", res["count"])


@router.post("/wb/stock")
def wb_stock(write_db: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_wb_stock(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    return _xlsx_response(res["df"], "wb_stock.xlsx", res["count"])


@router.post("/wb/funnel")
def wb_funnel(date_from: Optional[str] = None, date_to: Optional[str] = None,
              write_db: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_wb_funnel(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    return _xlsx_response(res["df"], f"wb_funnel_{from_}_{to_}.xlsx", res["count"])


@router.post("/wb/prices")
def wb_prices(write_db: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_wb_prices(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    return _xlsx_response(res["df"], "wb_prices.xlsx", res["count"])


@router.post("/wb/storage")
def wb_storage(days: int = 7, write_db: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_wb_storage(db, days, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    return _xlsx_response(res["df"], "wb_storage.xlsx", res["count"])


@router.post("/wb/sales")
def wb_sales(date_from: Optional[str] = None, date_to: Optional[str] = None,
             write_db: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_wb_sales(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    df = res["df"]
    if "sa_name" in df.columns:
        export = df.rename(columns=V5_RU_COLUMNS)
    elif "supplierArticle" in df.columns:
        export = df.rename(columns=SALES_RU_COLUMNS)
    else:
        export = df
    return _xlsx_response(export, f"wb_sales_{from_}_{to_}.xlsx", res["count"])


@router.post("/wb/detail")
def wb_detail(date_from: Optional[str] = None, date_to: Optional[str] = None,
              write_db: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_wb_detail(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    df = res["df"]
    export = df.rename(columns=DETAIL_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"wb_detail_{from_}_{to_}.xlsx", res["count"])


@router.post("/ozon/cards")
def ozon_cards(write_db: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_oz_cards(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    return _xlsx_response(res["df"], "ozon_cards.xlsx", res["count"])


@router.post("/ozon/stock")
def ozon_stock(write_db: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_oz_stock(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    return _xlsx_response(res["df"], "ozon_stock.xlsx", res["count"])


@router.post("/ozon/prices")
def ozon_prices(write_db: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_oz_prices(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    return _xlsx_response(res["df"], "ozon_prices.xlsx", res["count"])


@router.post("/ozon/realization")
def ozon_realization(month: Optional[int] = None, year: Optional[int] = None,
                     write_db: int = 1, db: Session = Depends(get_db)):
    if month is None or year is None:
        today = date.today().replace(day=1) - timedelta(days=1)
        year, month = today.year, today.month
    try:
        res = refresh_service.pull_oz_realization(db, month, year, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    df = res["df"]
    export = df.rename(columns=OZON_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"ozon_realization_{year:04d}-{month:02d}.xlsx", res["count"])


@router.post("/ozon/cashflow")
def ozon_cashflow(date_from: Optional[str] = None, date_to: Optional[str] = None,
                  write_db: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_oz_cashflow(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    return _xlsx_response(res["df"], f"ozon_cashflow_{from_}_{to_}.xlsx", res["count"])


# ------------------------------------------------------- массовое обновление (кнопка)
@router.post("/refresh")
def api_refresh(api: str, detail: int = 0, date_from: Optional[str] = None,
                date_to: Optional[str] = None):
    """Запускает фоновое обновление маркетплейса (wb|ozon). Ошибки изолированы по видам."""
    if api not in ("wb", "ozon"):
        raise HTTPException(status_code=400, detail='Параметр api должен быть "wb" или "ozon"')
    try:
        return refresh_service.start_refresh(api, include_detail=bool(detail),
                                             date_from=date_from, date_to=date_to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Некорректная дата: {e}")


@router.get("/refresh")
def api_refresh_list():
    return {"jobs": refresh_service.active_jobs()}


@router.get("/refresh/history")
def api_refresh_history(limit: int = 10, db: Session = Depends(get_db)):
    return {"runs": refresh_service.history(db, limit=min(max(limit, 1), 50))}


@router.get("/refresh/{job_id}")
def api_refresh_state(job_id: int):
    state = refresh_service.job_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    return state