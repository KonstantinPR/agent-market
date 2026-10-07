# -*- coding: utf-8 -*-
"""Excel-выгрузки WB: /api/export/wb/*."""
from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _parse_window400,
    _xlsx_response,
)

router = APIRouter()



@router.get("/export/wb/detail-summary")
def export_wb_detail_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)
    df = sync_service.detail_summary_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like
    )
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "title": "Наименование", "sells": "Продано, шт",
        "returns_qty": "Возвращено, шт", "revenue": "Реализовано, руб",
        "commission": "Комиссия, руб", "for_pay": "К перечислению, руб",
        "logistics": "Доставка, руб", "delivery_count": "Доставок, шт",
        "return_delivery_count": "Возврат доставок, шт",
        "storage": "Хранение, руб",
        "pvz_compensation": "ПВЗ-компенсации, руб",
        "payment_services": "Платёжные услуги, руб",
        "services": "Услуги/штрафы, руб", "ops_count": "Операций",
        "sources": "Источник",
    }, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Детализация по артикулам")
    fname = f"wb_detail_summary_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/wb/detail-rows")
def export_wb_detail_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    limit: int = 5000,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт сырых строк «Детализации продаж» WB (wb_detail_rows) в Excel."""
    from_, to_ = _parse_window400(date_from, date_to)
    q = (
        select(models.WbDetailRow)
        .where(models.WbDetailRow.sale_dt.isnot(None))
        .order_by(models.WbDetailRow.sale_dt, models.WbDetailRow.id)
        .limit(limit)
    )
    if from_:
        q = q.where(models.WbDetailRow.sale_dt >= from_)
    if to_:
        q = q.where(models.WbDetailRow.sale_dt <= to_)
    if article_like:
        q = q.where(common_service.like_col(models.WbDetailRow.article, article_like))
    rows = db.execute(q).scalars().all()
    recs = [{
        "date": r.sale_dt.isoformat() if r.sale_dt else "",
        "article": r.article, "title": r.title, "doc_type": r.doc_type_name,
        "quantity": r.quantity, "retail_price": float(r.retail_price or 0),
        "retail_amount": float(r.retail_amount or 0),
        "commission": float(r.ppvz_sales_commission or 0),
        "for_pay": float(r.for_pay or 0),
        "logistics": float(r.delivery_service or 0),
        "storage": float(r.paid_storage or 0),
        "services": float((r.penalty or 0) + (r.deduction or 0) + (r.additional_payment or 0)),
        "office": r.office_name, "srid": r.srid, "source": r.source,
    } for r in rows]
    df = pd.DataFrame(recs, columns=["date", "article", "title", "doc_type", "quantity",
                                      "retail_price", "retail_amount", "commission",
                                      "for_pay", "logistics", "storage", "services",
                                      "office", "srid", "source"])
    df, ru = excel_io.project_export(df, {
        "date": "Дата", "article": "Артикул", "title": "Наименование",
        "doc_type": "Тип документа", "quantity": "Кол-во",
        "retail_price": "Цена розничная", "retail_amount": "Реализовано, руб",
        "commission": "Комиссия, руб", "for_pay": "К перечислению, руб",
        "logistics": "Доставка, руб", "storage": "Хранение, руб",
        "services": "Услуги/штрафы, руб", "office": "Склад",
        "srid": "SRID", "source": "Источник",
    }, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Строки детализации")
    fname = f"wb_detail_rows_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )

# ------------------------------------------------------- экспорт разделов WB API (вьюхи)
@router.get("/export/wb/cards")
def export_wb_cards(
    marketplace: str = "wb",
    like: Optional[str] = None,
    limit: int = 5000,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт карточек WB из БД (marketplace_cards) в Excel."""
    payload = api_cards(marketplace=marketplace, like=like, limit=limit, offset=0, db=db)
    df = pd.DataFrame(payload["rows"], columns=[
        "chrt_id", "nm_id", "vendor_code", "brand", "subject", "size", "barcode",
        "volume_l", "composition", "name",
    ])
    df, ru = excel_io.project_export(df, {
        "chrt_id": "Код размера", "nm_id": "Артикул WB", "vendor_code": "Артикул продавца",
        "brand": "Бренд", "subject": "Предмет", "size": "Размер", "barcode": "Баркод",
        "volume_l": "Объём, л", "composition": "Состав", "name": "Наименование",
    }, cols)
    df = df.rename(columns=ru)
    return _xlsx_response(df, "wb_cards.xlsx", payload["total"])


@router.get("/export/wb/stock")
def export_wb_stock(
    marketplace: str = "wb",
    by_size: int = 1,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт остатков WB (последний срез stocks) в Excel.

    by_size=1 — по размерам (как таблица по умолчанию); иначе агрегат по артикулу+склад.
    """
    payload = api_stocks(marketplace=marketplace, db=db)
    recs = payload["rows"]
    if not by_size:
        agg = {}
        for r in recs:
            key = (r["article"], r["warehouse"])
            a = agg.setdefault(key, {
                "date": r["date"], "marketplace": r["marketplace"], "article": r["article"],
                "name": r["name"], "warehouse": r["warehouse"],
                "quantity": 0, "quantity_full": 0, "in_way": 0,
            })
            a["quantity"] += r["quantity"]
            a["quantity_full"] += r["quantity_full"]
            a["in_way"] += r["in_way"]
        recs = [
            {"date": a["date"], "marketplace": a["marketplace"], "article": a["article"],
             "name": a["name"], "warehouse": a["warehouse"], "quantity": a["quantity"],
             "quantity_full": a["quantity_full"], "in_way": a["in_way"]}
            for a in agg.values()
        ]
        df_cols = ["date", "marketplace", "article", "name", "warehouse", "quantity",
                   "quantity_full", "in_way"]
        ru = {
            "date": "Дата", "marketplace": "Маркетплейс", "article": "Артикул",
            "name": "Наименование", "warehouse": "Склад", "quantity": "Доступно",
            "quantity_full": "Всего на складах", "in_way": "В пути",
        }
    else:
        df_cols = ["date", "marketplace", "article", "name", "chrt_id", "size", "barcode",
                   "warehouse", "quantity", "quantity_full", "in_way"]
        ru = {
            "date": "Дата", "marketplace": "Маркетплейс", "article": "Артикул",
            "name": "Наименование", "chrt_id": "Код размера", "size": "Размер",
            "barcode": "Баркод", "warehouse": "Склад", "quantity": "Доступно",
            "quantity_full": "Всего на складах", "in_way": "В пути",
        }
    df = pd.DataFrame(recs, columns=df_cols)
    df, ru = excel_io.project_export(df, ru, cols)
    df = df.rename(columns=ru)
    fname = "wb_stock.xlsx" if by_size else "wb_stock_agg.xlsx"
    return _xlsx_response(df, fname, len(recs))


@router.get("/export/wb/prices")
def export_wb_prices(marketplace: str = "wb", article_like: Optional[str] = None,
                     cols: Optional[str] = None, db: Session = Depends(get_db)):
    """Экспорт текущих цен/скидок (price_snapshots) в Excel."""
    payload = api_prices(marketplace=marketplace, article_like=article_like, db=db)
    df = pd.DataFrame(payload["rows"], columns=[
        "article", "nm_id", "size", "name", "price", "discounted_price", "discount",
    ])
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "nm_id": "Артикул WB", "size": "Размер",
        "name": "Наименование", "price": "Цена без скидки",
        "discounted_price": "Цена со скидкой", "discount": "Скидка, %",
    }, cols)
    df = df.rename(columns=ru)
    fname = f"{marketplace}_prices.xlsx"
    return _xlsx_response(df, fname, payload["count"])


@router.get("/export/wb/storage")
def export_wb_storage(article_like: Optional[str] = None, cols: Optional[str] = None,
                      db: Session = Depends(get_db)):
    """Экспорт стоимости хранения WB (storage_costs) в Excel."""
    payload = api_storage_cost(article_like=article_like, db=db)
    df = pd.DataFrame(payload["rows"], columns=[
        "nm_id", "article", "name", "barcodes_count", "volume", "storage_price",
        "warehouse_price",
    ])
    df, ru = excel_io.project_export(df, {
        "nm_id": "Артикул WB", "article": "Артикул", "name": "Наименование",
        "barcodes_count": "Баркодов", "volume": "Объём, л",
        "storage_price": "Хранение за баркод", "warehouse_price": "Сумма хранения",
    }, cols)
    df = df.rename(columns=ru)
    return _xlsx_response(df, "wb_storage.xlsx", payload["count"])


@router.get("/export/wb/funnel")
def export_wb_funnel(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт воронки продаж WB (funnel_metric) в Excel."""
    payload = api_funnel(date_from=date_from, date_to=date_to, article_like=article_like, db=db)
    df = pd.DataFrame(payload["rows"])
    df, ru = excel_io.project_export(df, {
        "date_from": "С", "date_to": "По", "nm_id": "Артикул WB", "article": "Артикул",
        "name": "Наименование", "subject_name": "Предмет", "brand_name": "Бренд",
        "product_rating": "Рейтинг карточки", "feedback_rating": "Рейтинг по отзывам",
        "views": "Просмотры", "opens": "Открытия", "adds": "В корзину",
        "orders": "Заказы", "cancelled": "Отмены", "cancel_sum": "Сумма отмен",
        "buyouts": "Выкупы", "avg_price": "Ср. цена", "revenue": "Выручка",
        "buyout_sum": "Сумма выкупа", "avg_orders_per_day": "Ср. заказов в день",
        "share_order_percent": "Доля в выручке, %", "add_to_wishlist": "В отложенные",
        "time_to_ready_min": "Время доставки, мин", "localization_percent": "Локальные заказы, %",
        "conv_to_cart_percent": "В корзину, %", "conv_cart_to_order_percent": "К заказу, %",
        "conv_buyout_percent": "К выкупу, %",
        "wb_club_order_count": "WB Клуб: заказы", "wb_club_order_sum": "WB Клуб: заказы, ₽",
        "wb_club_buyout_count": "WB Клуб: выкупы", "wb_club_buyout_sum": "WB Клуб: выкупы, ₽",
        "wb_club_cancel_count": "WB Клуб: отмены", "wb_club_cancel_sum": "WB Клуб: отмены, ₽",
        "wb_club_avg_price": "WB Клуб: ср. цена", "wb_club_buyout_percent": "WB Клуб: % выкупа",
        "wb_club_avg_orders_per_day": "WB Клуб: заказов/день",
        "stock_wb": "Остатки WB", "stock_mp": "Остатки свой склад",
        "stock_balance_sum": "Сумма остатков",
    }, cols)
    df = df.rename(columns=ru)
    return _xlsx_response(df, f"wb_funnel_{payload['date_from']}_{payload['date_to']}.xlsx",
                          payload["count"])
