# -*- coding: utf-8 -*-
"""Автопилот цен Ozon: /api/pricing-ozon/*.

Отдельный роутер (WB-автопилот не трогаем). Журнал решений — общая таблица
price_changes, читается общим /api/pricing/history.
"""
from app.api._common import *  # noqa: F401,F403
from app.api._common import _oz_creds  # noqa: F401  (импорт * не тащит _-имена)
from app.providers.errors import OzonApiError  # noqa: F401
from app.services import pricing_ozon as pricing_ozon_service  # noqa: F401

router = APIRouter()


@router.get("/pricing-ozon/defaults")
def pricing_ozon_defaults():
    return {"defaults": pricing_ozon_service.OZON_PRICING_DEFAULTS}


@router.post("/pricing-ozon/recommendations")
def pricing_ozon_recommendations(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Read-only расчёт рекомендаций автопилота Ozon (по базовым артикулам)."""
    rec = pricing_ozon_service.recommendations_ozon(db, settings=payload)
    return rec


@router.post("/pricing-ozon/apply")
def pricing_ozon_apply(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Применяет цены Ozon через /v1/product/import/prices и пишет журнал.

    Если передан list `ui_rows` (видимые строки таблицы) — применяет ровно их,
    иначе пересчитывает рекомендации и применяет всё.
    """
    prov = provider_factory.get_oz_provider(credentials=_oz_creds())
    ui_rows = payload.get("ui_rows")
    try:
        if ui_rows:
            return pricing_ozon_service.apply_rows_ozon(
                db, ui_rows, settings=payload, provider=prov,
            )
        return pricing_ozon_service.apply_recommendations_ozon(
            db, settings=payload, provider=prov,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except OzonApiError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ozon API не принял изменение цен: {e}")


@router.post("/pricing-ozon/apply-min")
def pricing_ozon_apply_min(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Минимальные (break-even) цены Ozon: где витрина ниже пола — поднимаем до пола."""
    prov = provider_factory.get_oz_provider(credentials=_oz_creds())
    ui_rows = payload.get("ui_rows")
    try:
        return pricing_ozon_service.apply_min_prices_ozon(
            db, rows=(ui_rows if ui_rows else None), settings=payload, provider=prov,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except OzonApiError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ozon API не принял минимальные цены: {e}")


PRICING_OZON_ACTION_RU = {
    "RAISE": "поднять цену", "LOWER": "снизить цену", "HOLD": "держать", "SKIP": "пропустить",
}


@router.post("/pricing-ozon/export")
def pricing_ozon_export(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Рекомендации автопилота Ozon в Excel. Изменения в Ozon API НЕ вносятся."""
    cols = payload.get("cols")
    rec = pricing_ozon_service.recommendations_ozon(db, settings=payload)
    df = pd.DataFrame(rec["rows"])
    if not df.empty:
        df["action"] = df["action"].map(PRICING_OZON_ACTION_RU)
        df["replenishable"] = df["replenishable"].map({True: "да", False: "нет"})
    keep = [
        "article", "name", "offers_count", "nm_id", "price", "old_price",
        "ozon_discount_pct", "target_price", "delta_price", "delta_pct",
        "net_cost", "floor_price", "margin_pct", "margin_pct_at_target",
        "stock", "velocity", "v_proj", "trend", "doc", "stock_value",
        "commission_per_one", "services_per_one", "storage_per_one",
        "return_rate", "last_sale_days_ago", "replenishable",
        "action", "status", "reason", "influenced_by_main", "main_delta_pct",
    ]
    df = df[[c for c in keep if c in df.columns]]
    df, ru = excel_io.project_export(df, export_cols("oz_pricing_export"), cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Автопилот Ozon")
    fname = f"pricing_ozon_{date.today().isoformat()}.xlsx"
    return StreamingResponse(
        buf,
        media_type=XLSX_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Count": str(len(df)),
        },
    )
