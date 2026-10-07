# -*- coding: utf-8 -*-
"""Прайс/импорт товаров: /api/products/*."""
from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _catalog_price_fields,
    _eff_net_cost,
    _prefix_avg_cost_map,
    _unit_economics_map,
)

router = APIRouter()



@router.post("/products/refresh")
def products_refresh(overwrite: int = 0, db: Session = Depends(get_db)):
    """Обновляет общий каталог из карточек WB+Ozon (pull_catalog)."""
    try:
        res = refresh_service.pull_catalog(db, overwrite=bool(overwrite))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    return {"ok": True, **res}


@router.post("/products/preview")
def products_preview(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Пересчёт рекомендуемой (mode=recommended) или минимальной (mode=min) цены
    с пользовательскими коэффициентами (без записи)."""
    settings = base_price_service.merge_price_settings(payload.get("price_settings"))
    mode = str(payload.get("mode") or "recommended").strip().lower()
    like = str(payload.get("like") or "").strip().lower()
    ue_map, ue_global = _unit_economics_map(db)
    prods = list(db.execute(
        select(models.Product).order_by(models.Product.article)).scalars())
    prefix_map = _prefix_avg_cost_map(prods)

    def ue_for(article: str):
        return ue_map.get((article or "").strip().upper()) or ue_global

    rows = []
    for p in prods:
        if like and not (common_service.like_match(p.article, like)
                         or common_service.like_match(p.name, like)):
            continue
        row = {"article": p.article, "name": p.name,
               "net_cost": float(p.net_cost or 0), "volume_l": float(p.volume_l or 0)}
        fields = _catalog_price_fields(p, settings, _eff_net_cost(p.article, p.net_cost, prefix_map), ue_for(p.article))
        price = float(fields["min_price"] if mode == "min" else fields["recommended_price"])
        row.update(fields)
        row["price"] = price
        row["mode"] = mode
        rows.append(row)
    return {"rows": rows, "count": len(rows), "price_settings": settings}


@router.post("/products/prices/apply")
def products_prices_apply(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Применяет рекомендуемые (mode=recommended) или минимальные (mode=min) цены
    **только к видимым в таблице товарам** и пушит на Wildberries через WB API
    (v2/upload/task). Для минимальных цен учитывается порог WB (public API
    priceLimits.minPrice): итог = max(расчёт, мин. цена WB). Артикулы без nm_id
    (не заведены на WB) пропускаются молча [pushed]."""
    settings = base_price_service.merge_price_settings(payload.get("price_settings"))
    mode = str(payload.get("mode") or "recommended").strip().lower()
    like = str(payload.get("like") or "").strip().lower()
    sizes = int(payload.get("sizes") or 0)
    stocks = int(payload.get("stocks") or 0)

    prods = list(db.execute(
        select(models.Product).order_by(models.Product.article)
    ).scalars())
    prefix_map = _prefix_avg_cost_map(prods)
    ue_map, ue_global = _unit_economics_map(db)

    def ue_for(article: str):
        return ue_map.get((article or "").strip().upper()) or ue_global

    rows = []
    for p in prods:
        if like and not (common_service.like_match(p.article, like)
                         or common_service.like_match(p.name, like)):
            continue
        cost = _eff_net_cost(p.article, p.net_cost, prefix_map)
        if mode == "min":
            price = float(base_price_service.minimum_price(cost, ue_for(p.article), settings))
        else:
            price = float(base_price_service.recommended_price(cost, p.volume_l, settings))
        rows.append({
            "article": p.article,
            "name": p.name,
            "net_cost": float(p.net_cost or 0),
            "volume_l": float(p.volume_l or 0),
            "price": price,
            "mode": mode,
        })

    if not rows:
        return {"ok": True, "pushed": 0, "skipped": 0, "note": "Нет видимых товаров."}

    nm_map = {a.article: a.nm_id for a in db.execute(select(models.NmArticle)).scalars()}
    items = []
    skipped = []
    for r in rows:
        nm = nm_map.get(r["article"])
        if not nm:
            skipped.append(r["article"])
            continue
        items.append({
            "nmID": int(nm),
            "price": float(r["price"] or 0),
        })
    if not items:
        return {"ok": True, "pushed": 0, "skipped": len(skipped),
                "note": "Ни один из видимых товаров не заведён в WB (нет nm_id)."}
    prov = provider_factory.get_wb_provider()
    if mode == "min":
        wb_min_prices = {}
        try:
            wb_min_prices = prov.get_min_prices([it["nmID"] for it in items]) or {}
        except Exception:  # noqa: BLE001 — тихо, клампинг по WB пропускается
            wb_min_prices = {}
        for it in items:
            wb_min = float(wb_min_prices.get(str(it["nmID"]), 0) or 0)
            it["price"] = max(float(it["price"] or 0), wb_min)
    for it in items:
        it["discount"] = 0
    try:
        res = prov.update_prices(items)
    except WbApiError as e:
        return {"ok": False, "pushed": 0, "skipped": len(skipped), "error": str(e)}

    notes = [f"Отправлено товаров: {len(items)}"]
    if mode == "min":
        notes.append(" по минимальной цене (break-even)")
    else:
        notes.append(" по рекомендуемой цене")
    if sizes or stocks:
        notes.append(" с учётом фильтров «с размерами/остатки»")
    if skipped:
        notes.append(f"; пропущено без nm_id: {len(skipped)}")
    return {
        "ok": True,
        "pushed": len(items),
        "skipped": len(skipped),
        "task_id": res.get("task_id") if isinstance(res, dict) else None,
        "note": " ".join(notes),
    }


@router.get("/products/price-settings")
def products_price_settings():
    return {
        "defaults": base_price_service.PRICE_DEFAULTS,
        "labels": base_price_service.PRICE_LABELS,
        "hints": base_price_service.PRICE_HINTS,
    }


@router.post("/products/replenishable")
def product_replenishable(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Переключает флаг «докупаемый» у товара (влияет на автопилот цен WB)."""
    article = str(payload.get("article", "")).strip()
    if not article:
        raise HTTPException(status_code=400, detail="Не указан article")
    prod = db.scalar(select(models.Product).where(models.Product.article == article))
    if prod is None:
        raise HTTPException(status_code=404, detail=f"Товар {article} не найден")
    prod.replenishable = bool(payload.get("value", False))
    db.commit()
    return {"article": article, "replenishable": bool(prod.replenishable)}
