# -*- coding: utf-8 -*-
"""Обзор и обслуживание: dashboard, pulls, sync, refresh, yandex."""
from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _parse_window400,
    _resolve_compare_window,
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
def _sales_aggregate(db, from_, to_, mp_param, cost_map):
    """Свёртка продаж окна: (per_mp, total, daily).

    Источник — таблица Продажи (source != detail). Прибыль считается как
    доход − |комиссия| − |логистика| − |хранение| − себестоимость проданного.
    """
    query = (
        select(
            models.Marketplace.code.label("marketplace"),
            models.Sale.date,
            models.Sale.article,
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
        .group_by(models.Marketplace.code, models.Sale.date, models.Sale.article)
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
        cogs = int(r.sells or 0) * cost_map.get(
            (r.article or "").strip().upper(), 0.0)
        dk = str(r.date)
        daily.setdefault(dk, {"date": dk, "revenue": 0.0, "income": 0.0, "sells": 0, "profit": 0.0})
        daily[dk]["revenue"] += float(r.revenue or 0)
        daily[dk]["income"] += float(r.income or 0)
        daily[dk]["sells"] += int(r.sells or 0)
        # Прибыль = доход − комиссия/логистика/хранение − себестоимость
        # проданного. Знак затрат в данных бывает разным (WB пишет «+»,
        # Ozon «−») — нормируем abs().
        daily[dk]["profit"] += (float(r.income or 0)
                                - abs(float(r.commission or 0))
                                - abs(float(r.logistics or 0))
                                - abs(float(r.storage or 0))
                                - cogs)
    for d in daily.values():
        d["profit"] = round(d["profit"], 2)
    if per_mp:
        total["sells"] = sum(p["sells"] for p in per_mp.values())
        total["revenue"] = round(sum(p["revenue"] for p in per_mp.values()), 2)
        total["income"] = round(sum(p["income"] for p in per_mp.values()), 2)
    return per_mp, total, daily


def _dashboard_payload(db, from_, to_, mp_param, prev, top, owner="",
                       link_mp="", group_by="prefix"):
    """Полный ответ /dashboard для одной связки (search_path уже выставлен).

    link_mp — маркетплейс связки: если задан, дашборд считается только по нему.
    Иначе dashboard_kpis создаёт нулевые карточки обеих МП, и при слиянии
    (merge) карточке чужого МП доставался owner этой связки.
    """
    if link_mp:
        mp_param = link_mp
    # Себестоимость проданного: net_cost товара по UPPER-матчингу артикула.
    # Справочник маленький — читаем его в память и умножаем в Python. Это
    # принципиально: коррелированный подзапрос в SQL делал по индексу/сканированию
    # products на каждую строку продаж (O(n²)) и вешал дневной график на Postgres.
    cost_map = {
        (a or "").strip().upper(): float(c or 0.0)
        for a, c in db.execute(
            select(models.Product.article, models.Product.net_cost))
    }
    per_mp, total, daily = _sales_aggregate(db, from_, to_, mp_param, cost_map)

    # Прошлое окно — только для графиков сравнения (Динамика / Доход по МП).
    per_mp_prev, daily_prev = [], {}
    if prev is not None and prev[0] is not None and prev[1] is not None:
        prev_mp, _prev_total, daily_prev = _sales_aggregate(
            db, prev[0], prev[1], mp_param, cost_map)
        per_mp_prev = list(prev_mp.values())

    return {
        "per_marketplace": list(per_mp.values()),
        "per_marketplace_prev": per_mp_prev,
        "total": total,
        "daily": sorted(daily.values(), key=lambda d: d["date"]),
        "daily_prev": sorted(daily_prev.values(), key=lambda d: d["date"]),
        "compare_window": ({"from": str(prev[0]), "to": str(prev[1])}
                           if prev is not None and prev[0] is not None
                           and prev[1] is not None else None),
        "kpis": dashboard_service.dashboard_kpis(db, from_, to_, prev, mp_param),
        "tops": {
            "profit": dashboard_service.top_products(
                db, from_, to_, mp_param, limit=top, kind="profit"),
            "loss": dashboard_service.top_products(
                db, from_, to_, mp_param, limit=top, kind="loss"),
        },
        "price": dashboard_service.price_delta(db, from_, to_, prev, mp_param, limit=top),
        "prefixes": dashboard_service.grouped_margin(
            db, from_, to_, mp_param, limit=top, group_by=group_by),
        "stocks": dashboard_service.stocks_summary(db, to_, mp_param, from_),
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
    per_mp_prev = []
    total = {"sells": 0, "revenue": 0.0, "income": 0.0}
    daily = {}
    daily_prev = {}
    compare_window = None
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
        for e in payload.get("per_marketplace_prev") or []:
            per_mp_prev.append(dict(e, owner=owner))
        if compare_window is None and payload.get("compare_window"):
            compare_window = payload["compare_window"]
        for k in ("sells", "revenue", "income"):
            total[k] = (total[k] or 0) + (payload["total"].get(k) or 0)
        for d in payload["daily"]:
            dd = daily.setdefault(d["date"], {"date": d["date"], "revenue": 0.0, "income": 0.0, "sells": 0, "profit": 0.0})
            dd["revenue"] += float(d.get("revenue") or 0)
            dd["income"] += float(d.get("income") or 0)
            dd["sells"] += int(d.get("sells") or 0)
            dd["profit"] += float(d.get("profit") or 0)
        for d in payload.get("daily_prev") or []:
            dd = daily_prev.setdefault(d["date"], {"date": d["date"], "revenue": 0.0, "income": 0.0, "sells": 0, "profit": 0.0})
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

    for d in daily_prev.values():
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
        "per_marketplace_prev": per_mp_prev,
        "total": total,
        "daily": sorted(daily.values(), key=lambda d: d["date"]),
        "daily_prev": sorted(daily_prev.values(), key=lambda d: d["date"]),
        "compare_window": compare_window,
        "kpis": {"total": kt, "per_mp": per_mp_kpi, "compare": compare},
        "tops": {
            "profit": {"rows": _slice(tops_profit, top), "count": len(tops_profit)},
            "loss": {"rows": _slice(tops_loss, top), "count": len(tops_loss)},
        },
        "price": {
            "up": {"rows": _slice(price_up, top), "count": len(price_up)},
            "down": {"rows": _slice(price_down, top), "count": len(price_down)},
        },
        "prefixes": {"rows": _slice(prefixes, top), "count": len(prefixes),
                     "group_by": next(
                         (p["prefixes"].get("group_by") for p in payloads
                          if p.get("prefixes")), "prefix")},
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
    compare_mode: Optional[str] = None,
    compare_from: Optional[str] = None,
    compare_to: Optional[str] = None,
    top: Optional[int] = None,
    links: Optional[str] = None,
    group_by: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Обзор: KPI из детализаций + топы + дельты цены + склад + свежесть.

    marketplace: 'wb' | 'ozon' | 'wb,ozon' | 'all' (пусто = все).
    compare=1 добавляет сравнение маржи с предыдущим аналогичным окном.
    compare_mode ("prev"|"last_year"|"custom") + compare_from/compare_to — окно
    сравнения для графиков «Динамика за период» и «Доход по маркетплейсам»
    (daily_prev / per_marketplace_prev / compare_window).
    links=id,id,… — сравнение между связками: каждая связка считается под своей
    схемой, ответы сшиваются, в строках/карточках появляется «owner» (владелец).
    Фильтр связок по marketplace: при выбранном одном МП сравниваются только
    связки этого МП; «все» — любые. Без links — данные активной связки.
    Остаётся совместимым со старым ответом: per_marketplace/total/daily
    (агрегат по таблице Продажи, source != detail).
    """
    from_, to_ = _parse_window400(date_from, date_to)
    mp_param = marketplace or "all"
    group_by = group_by or "prefix"

    prev = None
    if compare and from_ and to_:
        prev = _resolve_compare_window(
            date_from, date_to, compare_mode or "", compare_from, compare_to)

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
                link_mp=info.marketplace, group_by=group_by),
        )
        results = [p for _, p in payloads]
        freshness = next((p["freshness"] for p in results if p.get("freshness")), [])
        return _merge_dashboards(results, from_, to_, top, freshness)

    return _dashboard_payload(db, from_, to_, mp_param, prev, top, owner="",
                              group_by=group_by)


def _xyz_payload(db, from_, to_, min_active_months: int = None):
    """ABC/XYZ-анализ одной связки: построчные классы + матрица ABC×XYZ (WB).

    Спрос (X/Y/Z) считается только по WB: в таблице продаж нет помесячной
    детализации Ozon. Поэтому и ABC-сводка берётся по WB — иначе в матрице
    были бы артикулы Ozon без класса спроса.
    """
    detail = dashboard_service.combined_detail(db, from_, to_, "wb")
    ab = dashboard_service._by_article(detail)
    xyz = xyz_service.xyz_rows(db, from_, to_, "wb", ab_rows=ab,
                               min_active_months=min_active_months)
    xyz_map = {str(r["article"]).strip().upper(): r["xyz_class"]
               for r in xyz["rows"]}
    matrix = xyz_service.abc_xyz_matrix(ab, xyz_map)
    return {"rows": xyz["rows"], "count": xyz["count"], "meta": xyz["meta"],
            "matrix": matrix}


#: Допустимые пороги «Новинка» (число активных месяцев) — как в UI-селекторе.
XYZ_MIN_ACTIVE_CHOICES = (1, 3, 6)


def _xyz_min_active(value) -> Optional[int]:
    """Порог «Новинка» из запроса: None — взять дефолт из настроек.

    Значение вне диапазона 1..12 месяцев отбрасывается (None), чтобы
    нельзя было запросить порог, при котором X/Y/Z не набираются.
    """
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v < 1 or v > 12:
        return None
    return v


def _xyz_empty(note: str) -> dict:
    """Пустой ответ XYZ (выбран не WB) — с пояснением вместо данных."""
    cells = [{"abc": a, "xyz": x, "articles": 0, "revenue": 0.0, "margin": 0.0}
             for a in xyz_service.ABC_ROWS for x in xyz_service.XYZ_COLS]
    return {
        "rows": [],
        "count": 0,
        "meta": {"note": note, "marketplace": "WB",
                 "window_months": 0, "min_active_months": 0,
                 "x_cv_threshold": 0.0, "y_cv_threshold": 0.0},
        "matrix": {
            "cells": cells,
            "abc_rows": list(xyz_service.ABC_ROWS),
            "xyz_cols": list(xyz_service.XYZ_COLS),
            "totals": {"articles": 0, "revenue": 0.0, "margin": 0.0},
        },
    }


@router.get("/dashboard/xyz")
def api_dashboard_xyz(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    marketplace: Optional[str] = None,
    links: Optional[str] = None,
    min_active_months: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """ABC/XYZ-анализ: стабильность спроса (X/Y/Z) + матрица ABC×XYZ.

    Считается по WB (для Ozon детализация не собирается). links=id,id,… —
    слияние по связкам: строки получают owner, матрица суммируется.
    min_active_months — порог класса «Новинка» (сколько активных месяцев
    нужно, чтобы попасть в X/Y/Z); в UI это 6 / 3 / 1, по умолчанию — из
    настроек (6). Недопустимое значение игнорируется.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    if str(marketplace or "").strip().lower() == "ozon":
        return _xyz_empty("ABC/XYZ-анализ считается только по Wildberries — "
                          "выбран Ozon.")
    min_active_months = _xyz_min_active(min_active_months)
    link_infos = _resolve_links_param(db, links)
    # Связки Ozon в XYZ не участвуют: помесячного спроса по ним нет.
    link_infos = [i for i in link_infos if i.marketplace != "ozon"]

    if len(link_infos) > 1:
        payloads = cabinet_service.run_per_link(
            db, link_infos,
            lambda db, info: dict(
                _xyz_payload(db, from_, to_, min_active_months),
                _owner=cabinet_service.link_owner(db, info)),
        )
        results = [(p.get("_owner", ""), p) for _, p in payloads]
        return _merge_xyz(results, from_, to_)

    return _xyz_payload(db, from_, to_, min_active_months)


def _merge_xyz(payloads, from_, to_):
    rows = []
    cells = {}
    meta = None
    for owner, p in payloads:
        rows.extend(dict(r, owner=owner) for r in p["rows"])
        if meta is None:
            meta = p["meta"]
        for c in p["matrix"]["cells"]:
            key = (c["abc"], c["xyz"])
            cell = cells.setdefault(key, dict(c))
            cell["articles"] += c["articles"]
            cell["revenue"] = round(cell["revenue"] + c["revenue"], 2)
            cell["margin"] = round(cell["margin"] + c["margin"], 2)
    rows.sort(key=lambda r: float(r.get("avg_month_qty") or 0), reverse=True)
    cells_list = list(cells.values())
    return {
        "rows": rows,
        "count": len(rows),
        "meta": meta or {},
        "matrix": {
            "cells": cells_list,
            "abc_rows": list(xyz_service.ABC_ROWS),
            "xyz_cols": list(xyz_service.XYZ_COLS),
            "totals": {
                "articles": sum(c["articles"] for c in cells_list),
                "revenue": round(sum(c["revenue"] for c in cells_list), 2),
                "margin": round(sum(c["margin"] for c in cells_list), 2),
            },
        },
    }


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


@router.post("/refresh/retry-failed")
def api_refresh_retry_failed(payload: dict = Body(default={}),
                             db: Session = Depends(get_db)):
    """«Дозагрузить недокачанное»: повторяет только упавшие виды выгрузки.

    По последнему фоновому заданию каждой связки берутся шаги со статусом
    failed и запускаются заново (выкачки идемпотентны). cab_id в теле
    ограничивает дозагрузку одной связкой; без него — все.
    """
    cab_id = payload.get("cab_id") if isinstance(payload, dict) else None
    if cab_id is not None:
        try:
            cab_id = int(cab_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Некорректный cab_id")
    pending = refresh_service.failed_steps_last()
    if cab_id is not None:
        pending = [p for p in pending if p["cab_id"] == cab_id]
    jobs = []
    for p in pending:
        res = refresh_service.start_refresh(
            p["api"], include_detail=p["include_detail"],
            date_from=p["date_from"], date_to=p["date_to"],
            cab_id=p["cab_id"], kinds=p["kinds"])
        if res.get("job_id"):
            jobs.append({"cab_id": p["cab_id"], "api": p["api"], **res})
    return {"jobs": jobs, "count": len(jobs)}


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
