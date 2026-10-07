# -*- coding: utf-8 -*-
"""Автопилот цен: /api/pricing/*."""
from app.api._common import *  # noqa: F401,F403

router = APIRouter()



# ---------------------------------------------------------------------------
# Автопилот цен Wildberries
# ---------------------------------------------------------------------------
@router.get("/pricing/defaults")
def pricing_defaults():
    return {"defaults": pricing_service.PRICING_DEFAULTS}


@router.post("/pricing/recommendations")
def pricing_recommendations(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Read-only расчёт рекомендаций по правилам R1-R10. body = настройки (перекрытие дефолтов)."""
    prices_df, updated_at, source = pricing_service.resolve_prices(db)
    rec = pricing_service.recommendations(
        db, settings=payload, prices_df=prices_df,
    )
    rec["prices_source"] = source
    rec["prices_updated_at"] = updated_at.isoformat() if updated_at else None
    return rec


@router.post("/pricing/apply")
def pricing_apply(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Применяет скидки через WB API upload/task и пишет журнал price_changes.

    Если в теле передан list `ui_rows` (видимые строки таблицы автопилота) —
    применяет ровно их (то, что видит пользователь с учётом фильтров поиска и
    скрытых колонок). Иначе пересчитывает рекомендации и применяет всё.
    """
    prov = provider_factory.get_wb_provider()
    ui_rows = payload.get("ui_rows")
    try:
        if ui_rows:
            return pricing_service.apply_rows(
                db, ui_rows, settings=payload, provider=prov,
            )
        prices_df, updated_at, source = pricing_service.resolve_prices(db)
        res = pricing_service.apply_recommendations(
            db, settings=payload, prices_df=prices_df, provider=prov,
        )
        res["prices_source"] = source
        res["prices_updated_at"] = updated_at.isoformat() if updated_at else None
        return res
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except WbApiError as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"WB API не принял изменение цен: {e}")


@router.get("/pricing/history")
def pricing_history(limit: int = 50, db: Session = Depends(get_db)):
    """Журнал решений автопилота (последние события)."""
    rows = db.execute(
        select(models.PriceChange)
        .order_by(models.PriceChange.calculated_at.desc())
        .limit(min(max(limit, 1), 200))
    ).scalars().all()
    return {
        "rows": [
            {
                "article": r.article,
                "nm_id": r.nm_id,
                "calculated_at": str(r.calculated_at) if r.calculated_at else None,
                "applied_at": str(r.applied_at) if r.applied_at else None,
                "before_discount": float(r.before_discount or 0),
                "after_discount": float(r.after_discount) if r.after_discount is not None else None,
                "action": r.action,
                "status": r.status,
                "reason": r.reason,
            }
            for r in rows
        ],
        "count": len(rows),
    }


PRICING_ACTION_RU = {
    "RAISE": "поднять цену", "LOWER": "снизить цену", "HOLD": "держать", "SKIP": "пропустить",
}


@router.post("/pricing/export")
def pricing_export(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Рекомендации автопилота в Excel. Изменения в WB API НЕ вносятся."""
    cols = payload.get("cols")
    prices_df, _updated_at, _source = pricing_service.resolve_prices(db)
    rec = pricing_service.recommendations(
        db, settings=payload, prices_df=prices_df,
    )
    df = pd.DataFrame(rec["rows"])
    if not df.empty:
        df["action"] = df["action"].map(PRICING_ACTION_RU)
        df["replenishable"] = df["replenishable"].map({True: "да", False: "нет"})
    keep = [
        "nm_id", "article", "name", "price", "current_vis", "current_discount", "target_vis",
        "target_discount", "delta_discount", "net_cost", "action", "status", "reason",
        "doc", "velocity", "trend",
        "conv_pct", "backlog", "stock", "avg_price", "eff", "floor_price",
        "max_discount_item", "margin_pct_at_target", "replenishable",
        "product_rating", "buyouts", "conv_buyout_percent", "cancel_sum",
        "add_to_wishlist", "stock_wb", "return_rate", "margin_pct", "margin_per_one",
        "revenue_per_one", "income_per_one", "commission_per_one",
        "logistics_per_one", "storage_per_one", "detail_sells", "detail_returns_qty",
        "promo_count", "promo_names", "promo_part_pct", "promo_tier_pct",
        "promo_tier_boost", "promo_need_rows", "promo_cap_pct",
        "promo_push_applied", "promo_delta_discount", "promo_score",
        "promo_score_confidence", "last_sale_days_ago",
    ]
    df = df[[c for c in keep if c in df.columns]]
    df, ru = excel_io.project_export(df, export_cols("pricing_export"), cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Автопилот")
    fname = f"pricing_{date.today().isoformat()}.xlsx"
    return StreamingResponse(
        buf,
        media_type=XLSX_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Count": str(len(df)),
        },
    )
