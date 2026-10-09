# -*- coding: utf-8 -*-
"""Обзор и обслуживание: dashboard, pulls, sync, refresh, yandex."""
from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _parse_window400,
    _resolve_links_param,
)
from app.services import cabinets as cabinet_service

router = APIRouter()


@router.get("/progress")
async def api_progress(op: Optional[str] = None):
    """Прогресс текущей выкачки для зелёной строки состояния.

    Фронтенд шлёт op-id заголовком X-Progress-Id и опрашивает этот роут раз в
    секунду, пока висит синхронный запрос; пустая строка = прогресса нет
    (показываем прошедшее время). Без БД и threadpool — отвечает мгновенно
    даже при загруженных выкачками воркерах.
    """
    from app.services import progress as progress_service

    snap = progress_service.snapshot(op or "")
    return {"text": snap["text"] if snap else ""}



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


# ---------------------------------------------------------------------------
# Обзор (dashboard), в т.ч. сравнение между связками (параметр links=id,id,…)
# ---------------------------------------------------------------------------
def _dashboard_payload(db, from_, to_, mp_param, prev, top, owner="",
                       link_mp=""):
    """Полный ответ /dashboard для одной связки (search_path уже выставлен).

    link_mp — маркетплейс связки: если задан, дашборд считается только по нему.
    Иначе dashboard_kpis создаёт нулевые карточки обеих МП, и при слиянии
    (merge) карточке чужого МП доставался owner этой связки.
    """
    if link_mp:
        mp_param = link_mp
    query = (
        select(
            models.Marketplace.code.label("marketplace"),
            models.Sale.date,
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
        .group_by(models.Marketplace.code, models.Sale.date)
    )
    per_mp = {}
    total = {"sells": 0, "revenue": 0.0, "income": 0.0}
    daily = {}
    wanted = dashboard_service._wanted(mp_param)
    for r in db.execute(query):
        if r.marketplace not in wanted:
            continue
        per_mp.setdefault(r.marketplace, {
            "marketplace": r.marketplace,
            "sells": 0, "revenue": 0.0, "income": 0.0,
            "commission": 0.0, "logistics": 0.0, "storage": 0.0,
        })
        p = per_mp[r.marketplace]
        p["sells"] += int(r.sells or 0)
        p["revenue"] += float(r.revenue or 0)
        p["income"] += float(r.income or 0)
        p["commission"] += float(r.commission or 0)
        p["logistics"] += float(r.logistics or 0)
        p["storage"] += float(r.storage or 0)
        dk = str(r.date)
        daily.setdefault(dk, {"date": dk, "revenue": 0.0, "income": 0.0, "sells": 0, "profit": 0.0})
        daily[dk]["revenue"] += float(r.revenue or 0)
        daily[dk]["income"] += float(r.income or 0)
        daily[dk]["sells"] += int(r.sells or 0)
        # Операционная прибыль = доход минус комиссия/логистика/хранение.
        # Знак затрат в данных бывает разным (WB пишет «+», Ozon «−») — нормируем abs().
        daily[dk]["profit"] += (float(r.income or 0)
                                - abs(float(r.commission or 0))
                                - abs(float(r.logistics or 0))
                                - abs(float(r.storage or 0)))
    for d in daily.values():
        d["profit"] = round(d["profit"], 2)
    if per_mp:
        total["sells"] = sum(p["sells"] for p in per_mp.values())
        total["revenue"] = round(sum(p["revenue"] for p in per_mp.values()), 2)
        total["income"] = round(sum(p["income"] for p in per_mp.values()), 2)

    return {
        "per_marketplace": list(per_mp.values()),
        "total": total,
        "daily": sorted(daily.values(), key=lambda d: d["date"]),
        "kpis": dashboard_service.dashboard_kpis(db, from_, to_, prev, mp_param),
        "tops": {
            "profit": dashboard_service.top_products(
                db, from_, to_, mp_param, limit=top, kind="profit"),
            "loss": dashboard_service.top_products(
                db, from_, to_, mp_param, limit=top, kind="loss"),
        },
        "price": dashboard_service.price_delta(db, from_, to_, prev, mp_param, limit=top),
        "prefixes": dashboard_service.prefix_margin(
            db, from_, to_, mp_param, limit=top),
        "stocks": dashboard_service.stocks_summary(db, to_, mp_param),
        "freshness": dashboard_service.freshness(db),
        "date_from": str(from_),
        "date_to": str(to_),
        "_owner": owner,
    }


def _sum_float_row(base: dict, add: dict, keys) -> None:
    for k in keys:
        base[k] = round((base.get(k) or 0) + float(add.get(k) or 0), 2)


def _merge_dashboards(payloads, from_, to_, top, freshness):
    """Сшивает ответы /dashboard по связкам: владелец в каждой строке/карточке."""
    per_mp = []
    total = {"sells": 0, "revenue": 0.0, "income": 0.0}
    daily = {}
    kt = {"sells": 0, "returns_qty": 0, "revenue": 0.0, "income": 0.0,
          "margin_gross": 0.0, "margin": 0.0, "articles": 0, "net_cost_est": 0}
    prev_sum = {"sells": 0, "returns_qty": 0, "revenue": 0.0, "income": 0.0,
                "margin_gross": 0.0, "margin": 0.0}
    per_mp_kpi = []
    tops_profit, tops_loss, price_up, price_down, prefixes = [], [], [], [], []
    stocks = {}
    for payload in payloads:
        owner = payload.get("_owner", "")
        for e in payload["per_marketplace"]:
            per_mp.append(dict(e, owner=owner))
        for k in ("sells", "revenue", "income"):
            total[k] = (total[k] or 0) + (payload["total"].get(k) or 0)
        for d in payload["daily"]:
            dd = daily.setdefault(d["date"], {"date": d["date"], "revenue": 0.0, "income": 0.0, "sells": 0, "profit": 0.0})
            dd["revenue"] += float(d.get("revenue") or 0)
            dd["income"] += float(d.get("income") or 0)
            dd["sells"] += int(d.get("sells") or 0)
            dd["profit"] += float(d.get("profit") or 0)
        k = payload["kpis"]
        t = k["total"] or {}
        kt["sells"] += int(t.get("sells") or 0)
        kt["returns_qty"] += int(t.get("returns_qty") or 0)
        kt["articles"] += int(t.get("articles") or 0)
        kt["net_cost_est"] += int(t.get("net_cost_est") or 0)
        _sum_float_row(kt, t, ("revenue", "income", "margin_gross", "margin"))
        for e in k.get("per_mp") or []:
            per_mp_kpi.append(dict(e, owner=owner))
        cmp = k.get("compare")
        if cmp and cmp.get("prev"):
            pt = cmp["prev"]
            prev_sum["sells"] += int(pt.get("sells") or 0)
            prev_sum["returns_qty"] += int(pt.get("returns_qty") or 0)
            _sum_float_row(prev_sum, pt, ("revenue", "income", "margin_gross", "margin"))
        for src, dst in ((payload["tops"]["profit"]["rows"], tops_profit),
                         (payload["tops"]["loss"]["rows"], tops_loss)):
            for r in src:
                dst.append(dict(r, owner=owner))
        for r in payload["price"]["up"]["rows"]:
            price_up.append(dict(r, owner=owner))
        for r in payload["price"]["down"]["rows"]:
            price_down.append(dict(r, owner=owner))
        for r in payload["prefixes"]["rows"]:
            prefixes.append(dict(r, owner=owner))
        for code, st in payload["stocks"].items():
            key = f"{code}:{owner}" if owner else code
            stocks[key] = dict(st, code=code, owner=owner)

    total["sells"] = int(total["sells"])
    total["revenue"] = round(float(total["revenue"]), 2)
    total["income"] = round(float(total["income"]), 2)

    for d in daily.values():
        d["profit"] = round(d["profit"], 2)
        d["revenue"] = round(d["revenue"], 2)
        d["income"] = round(d["income"], 2)

    margin = kt["margin"] or 0
    income = kt["income"] or 0
    sells = kt["sells"] or 0
    kt["margin_per_one"] = round(margin / sells, 2) if sells else 0.0
    kt["margin_pct"] = round(margin / income * 100, 2) if income else 0.0

    def _sort_top(rows, key, rev):
        return sorted(rows, key=key, reverse=rev)

    tops_profit = _sort_top(tops_profit, lambda r: (r.get("margin") if r.get("margin") is not None else float("-inf")), True)
    tops_loss = _sort_top(tops_loss, lambda r: (r.get("margin") if r.get("margin") is not None else float("inf")), False)

    def _slice(rows, limit):
        return rows if limit is None else rows[:limit]

    price_up = sorted(price_up, key=lambda r: (r.get("delta_pct") if r.get("delta_pct") is not None else float("-inf")), reverse=True)
    price_down = sorted(price_down, key=lambda r: (r.get("delta_pct") if r.get("delta_pct") is not None else float("inf")))
    prefixes = sorted(prefixes, key=lambda r: (r.get("margin") if r.get("margin") is not None else float("-inf")), reverse=True)

    compare = None
    if prev_sum.get("margin"):
        delta_ru = round(margin - prev_sum["margin"], 2)
        compare = {
            "prev": prev_sum,
            "delta_ru": delta_ru,
            "delta_pct": round(delta_ru / abs(prev_sum["margin"]) * 100, 2),
        }

    return {
        "per_marketplace": per_mp,
        "total": total,
        "daily": sorted(daily.values(), key=lambda d: d["date"]),
        "kpis": {"total": kt, "per_mp": per_mp_kpi, "compare": compare},
        "tops": {
            "profit": {"rows": _slice(tops_profit, top), "count": len(tops_profit)},
            "loss": {"rows": _slice(tops_loss, top), "count": len(tops_loss)},
        },
        "price": {
            "up": {"rows": _slice(price_up, top), "count": len(price_up)},
            "down": {"rows": _slice(price_down, top), "count": len(price_down)},
        },
        "prefixes": {"rows": _slice(prefixes, top), "count": len(prefixes)},
        "stocks": stocks,
        "freshness": freshness,
        "date_from": str(from_),
        "date_to": str(to_),
    }


@router.get("/dashboard")
def api_dashboard(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    marketplace: Optional[str] = None,
    compare: int = 0,
    top: Optional[int] = None,
    links: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Обзор: KPI из детализаций + топы + дельты цены + склад + свежесть.

    marketplace: 'wb' | 'ozon' | 'wb,ozon' | 'all' (пусто = все).
    compare=1 добавляет сравнение маржи с предыдущим аналогичным окном.
    links=id,id,… — сравнение между связками: каждая связка считается под своей
    схемой, ответы сшиваются, в строках/карточках появляется «owner» (владелец).
    Фильтр связок по marketplace: при выбранном одном МП сравниваются только
    связки этого МП; «все» — любые. Без links — данные активной связки.
    Остаётся совместимым со старым ответом: per_marketplace/total/daily
    (агрегат по таблице Продажи, source != detail).
    """
    from_, to_ = _parse_window400(date_from, date_to)
    mp_param = marketplace or "all"

    prev = None
    if compare and from_ and to_:
        delta = (to_ - from_).days
        prev = (from_ - timedelta(days=delta), from_ - timedelta(days=1))

    link_infos = _resolve_links_param(db, links)
    wanted = dashboard_service._wanted(mp_param)
    if link_infos and set(wanted) == {"wb"}:
        link_infos = [i for i in link_infos if i.marketplace == "wb"]
    elif link_infos and set(wanted) == {"ozon"}:
        link_infos = [i for i in link_infos if i.marketplace == "ozon"]

    if len(link_infos) > 1:
        payloads = cabinet_service.run_per_link(
            db, link_infos,
            lambda db, info: _dashboard_payload(
                db, from_, to_, mp_param, prev, top,
                owner=cabinet_service.link_owner(db, info),
                link_mp=info.marketplace),
        )
        results = [p for _, p in payloads]
        freshness = next((p["freshness"] for p in results if p.get("freshness")), [])
        return _merge_dashboards(results, from_, to_, top, freshness)

    return _dashboard_payload(db, from_, to_, mp_param, prev, top, owner="")


@router.post("/sync/{code}")
def api_sync(code: str, date_from: Optional[str] = None, date_to: Optional[str] = None):
    if code not in ("wb", "ozon"):
        return {"error": "unknown marketplace"}, 400
    from_, to_ = _parse_window400(date_from, date_to)
    return sync_service.sync_sales_window(code, from_, to_)


# ------------------------------------------------------- массовое обновление (кнопка)
@router.post("/refresh")
def api_refresh(api: str, detail: int = 0, date_from: Optional[str] = None,
                date_to: Optional[str] = None, cab_id: Optional[int] = None,
                db: Session = Depends(get_db)):
    """Запускает фоновое обновление маркетплейса (wb|ozon). Ошибки изолированы по видам.

    cab_id — связка, по умолчанию активная в запросе (кука agent_cabinet).
    Связка без ключей для api не обновляется (иначе провайдер возьмёт ключи по
    умолчанию из .env и запишет чужие данные в схему связки).
    """
    if api not in ("wb", "ozon"):
        raise HTTPException(status_code=400, detail='Параметр api должен быть "wb" или "ozon"')
    if cab_id is not None:
        info = cabinet_service.info_by_id(db, cab_id)
        if info is None:
            raise HTTPException(status_code=404, detail="Связка не найдена")
        if not info.creds_for(api):
            raise HTTPException(
                status_code=400,
                detail=f'У связки «{info.name}» нет ключей маркетплейса {api.upper()}',
            )
    try:
        return refresh_service.start_refresh(api, include_detail=bool(detail),
                                             date_from=date_from, date_to=date_to,
                                             cab_id=cab_id)
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


# ------------------------------------------------------- Яндекс.Диск (файлы отчётов)
@router.post("/yandex/upload")
async def yandex_upload(file: UploadFile = File(...), folder: str = "/agent_market"):
    try:
        token = yandex_service.require_token()
        data = await file.read()
        name = file.filename or "file.xlsx"
        return yandex_service.upload_bytes(token, folder, name, data)
    except yandex_service.YandexDiskError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/yandex/list")
def yandex_list(folder: str = "/agent_market"):
    try:
        token = yandex_service.require_token()
        return yandex_service.list_files(token, folder)
    except yandex_service.YandexDiskError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/yandex/download")
def yandex_download(path: str):
    try:
        token = yandex_service.require_token()
        data = yandex_service.download_bytes(token, path)
    except yandex_service.YandexDiskError as e:
        raise HTTPException(status_code=400, detail=str(e))
    name = path.rsplit("/", 1)[-1] or "file.xlsx"
    return StreamingResponse(
        BytesIO(data),
        media_type=XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.delete("/yandex/delete")
def yandex_delete(path: str):
    try:
        token = yandex_service.require_token()
        yandex_service.delete_file(token, path)
    except yandex_service.YandexDiskError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "path": path}
