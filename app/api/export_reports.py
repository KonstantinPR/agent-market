# -*- coding: utf-8 -*-
"""Excel-выгрузки отчётов: margin/sales/products/replenish/dashboard."""
from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _REPLENISH_EXPORT,
    _REPLENISH_EXPORT_SIZES,
    _fmt_days,
    _parse_window400,
    _replenish_cols,
    _xlsx_response,
)

router = APIRouter()



@router.get("/export/dashboard")
def export_dashboard(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    marketplace: Optional[str] = None,
    compare: int = 0,
    db: Session = Depends(get_db),
):
    """Дашборд одним Excel-файлом: KPI, топы, изменения цен, группы."""
    from_, to_ = _parse_window400(date_from, date_to)
    mp_param = marketplace or "all"

    prev = None
    if compare and from_ and to_:
        delta = (to_ - from_).days
        prev = (from_ - timedelta(days=delta), from_ - timedelta(days=1))

    kpis = dashboard_service.dashboard_kpis(db, from_, to_, prev, mp_param)
    profit = dashboard_service.top_products(db, from_, to_, mp_param, kind="profit")
    loss = dashboard_service.top_products(db, from_, to_, mp_param, kind="loss")
    price = dashboard_service.price_delta(db, from_, to_, prev, mp_param)
    prefixes = dashboard_service.prefix_margin(db, from_, to_, mp_param)

    def _kpi_frame(row):
        return {
            "Показатель": row["marketplace"] or "Итого",
            "Выручка, руб": row["revenue"],
            "Доход, руб": row["income"],
            "Прибыль, руб": row["margin"],
            "Прибыль/шт, руб": row["margin_per_one"],
            "Рентабельность, %": row["margin_pct"],
            "Продано, шт": row["sells"],
            "Товаров": row["articles"],
        }

    kpi_rows = [_kpi_frame(kpis["total"])] + [_kpi_frame(m) for m in kpis["per_mp"]]
    if kpis["compare"]:
        kpi_rows.append({
            "Показатель": "Δ к прошлому периоду, руб",
            "Прибыль, руб": kpis["compare"]["delta_ru"],
            "Рентабельность, %": kpis["compare"]["delta_pct"],
        })

    tops_cols = ["article", "name", "marketplace", "sells", "returns_qty",
                 "revenue", "income", "margin", "margin_per_one", "margin_pct"]
    tops_ru = {
        "article": "Артикул", "name": "Наименование", "marketplace": "МП",
        "sells": "Продано, шт", "returns_qty": "Возвращено, шт",
        "revenue": "Выручка, руб", "income": "Доход, руб", "margin": "Прибыль, руб",
        "margin_per_one": "Прибыль/шт, руб", "margin_pct": "Рентабельность, %",
    }
    price_cols = ["article", "name", "avg", "avg_prev", "delta_ru", "delta_pct", "margin"]
    price_ru = {
        "article": "Артикул", "name": "Наименование", "avg": "Цена сейчас, руб",
        "avg_prev": "Цена прошлого пер., руб", "delta_ru": "Δ, руб",
        "delta_pct": "Δ, %", "margin": "Прибыль, руб",
    }
    prefix_cols = ["prefix", "articles", "sells", "revenue", "income", "margin",
                   "margin_per_one", "margin_pct"]
    prefix_ru = {
        "prefix": "Группа", "articles": "Товаров", "sells": "Продано, шт",
        "revenue": "Выручка, руб", "income": "Доход, руб", "margin": "Прибыль, руб",
        "margin_per_one": "Прибыль/шт, руб", "margin_pct": "Рентабельность, %",
    }

    def frame(rows, cols, ru):
        df = pd.DataFrame(rows, columns=cols)
        if df.empty:
            df = pd.DataFrame(columns=cols)
        return df.rename(columns=ru)

    sheets = {
        "KPI": pd.DataFrame(kpi_rows),
        "Прибыльные": frame(profit["rows"], tops_cols, tops_ru),
        "Убыточные": frame(loss["rows"], tops_cols, tops_ru),
        "Рост цены": frame(price["up"]["rows"], price_cols, price_ru),
        "Снижение цены": frame(price["down"]["rows"], price_cols, price_ru),
        "Группы": frame(prefixes["rows"], prefix_cols, prefix_ru),
    }
    buf = excel_io.dfs_to_excel_stream(sheets)
    fname = f"dashboard_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/products")
def export_products(
    like: Optional[str] = None,
    sizes: int = 0,
    stocks: int = 0,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт каталога «Наш склад → Товары» в Excel по видимым колонкам."""
    payload = api_products(like=like, sizes=sizes, stocks=stocks, db=db)
    df = pd.DataFrame(payload["rows"])
    if "tags" in df.columns:
        df["tags"] = df["tags"].map(lambda v: ", ".join(v) if isinstance(v, list) else v)
    order = [c for c in ["article", "name", "brand", "subject", "size", "sizes_count",
                         "barcode", "volume_l", "composition", "net_cost", "replenishable",
                         "tags", "own_stock", "mp_stock", "recommended_price", "markup"]
             if c in df.columns]
    if order:
        df = df[order]
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "name": "Наименование", "brand": "Бренд",
        "subject": "Предмет", "size": "Размер", "sizes_count": "Размеров",
        "barcode": "Баркод", "volume_l": "Объём, л", "composition": "Состав",
        "net_cost": "Себестоимость", "replenishable": "Докупаемый",
        "tags": "Маркетплейсы", "own_stock": "Свой склад", "mp_stock": "Остаток МП",
        "recommended_price": "Рекоменд. цена", "markup": "Наценка",
    }, cols)
    df = df.rename(columns=ru)
    return _xlsx_response(df, "products.xlsx", payload["count"])


@router.get("/export/replenish")
def export_replenish(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    target_days: int = 30,
    window_days: int = 30,
    marketplace: Optional[str] = None,
    sort: str = "urgency",
    article_like: Optional[str] = None,
    show_inactive: int = 0,
    hide_zero_sizes: int = 0,
    view: str = "article",
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)
    if date_from is None and date_to is None:
        back = max(1, int(window_days or settings.sync_days_default))
        from_ = to_ - timedelta(days=back - 1)
    span = (to_ - from_).days + 1
    result = replenish_service.replenish_rows(
        db, from_, to_, target_days=target_days, span_days=span,
        marketplace=marketplace, sort=sort, article_like=article_like,
        show_inactive=bool(show_inactive), hide_zero_sizes=bool(hide_zero_sizes),
        view=view,
    )
    df = pd.DataFrame(result["rows"])
    if df.empty:
        df = pd.DataFrame(columns=list(_REPLENISH_EXPORT))
    key_map = _REPLENISH_EXPORT if view != "sizes" else _REPLENISH_EXPORT_SIZES
    df, ru = excel_io.project_export(df, key_map, ",".join(_replenish_cols(cols, key_map)))
    df = df.rename(columns=ru)
    sheet = "Потребность по размерам" if view == "sizes" else "Потребность"
    buf = excel_io.df_to_excel_stream(df, sheet_name=sheet)
    tpl = "replenish_{0}_{1}_{2}.xlsx"
    fname = tpl.format(view, from_, to_)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/replenish/pdf")
def export_replenish_pdf(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    target_days: int = 30,
    window_days: int = 30,
    marketplace: Optional[str] = None,
    sort: str = "urgency",
    article_like: Optional[str] = None,
    show_inactive: int = 0,
    cols: Optional[str] = None,
    with_photos: int = 1,
    photo_count: int = 6,
    vel_days: int = replenish_service.WB_SORT_VELOCITY_DAYS,
    profit: int = 1,
    limit: int = PDF_DEFAULT_LIMIT,
    db: Session = Depends(get_db),
):
    """Карточки потребности в PDF из строк таблицы (базовый режим).

    ``cols`` — те же ключи, что и в Excel-выгрузке. Фото подтягиваются из
    индекса фотографий (рекурсивный обход диска, кэш в памяти), миниатюры
    кэшируются на диске, поэтому повторные выгрузки быстрые.

    ``vel_days`` — окно скорости продаж размера: 180, 365 или 0 (всё время).

    ``profit`` — учитывать прибыльность: покрытие = период/4 дней продаж, а
    целевой уровень домножается на коэффициент по рентабельности (0…2).
    При ``profit=0`` работает как раньше: покрытие = «Запас», без коэффициента.

    PDF из Excel-файла с правками — отдельный ``POST /export/replenish/pdf``.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    if date_from is None and date_to is None:
        back = max(1, int(window_days or settings.sync_days_default))
        from_ = to_ - timedelta(days=back - 1)
    span = (to_ - from_).days + 1

    common = dict(
        target_days=target_days, span_days=span, marketplace=marketplace,
        sort=sort, article_like=article_like, show_inactive=bool(show_inactive),
    )
    main = replenish_service.replenish_rows(db, from_, to_, view="article", **common)

    rows = main["rows"]
    cap = max(1, min(int(limit or PDF_DEFAULT_LIMIT), PDF_MAX_LIMIT))
    truncated = max(0, len(rows) - cap)
    rows = rows[:cap]
    return _replenish_pdf_build(
        db, rows,
        from_=from_, to_=to_, span=span, truncated=truncated,
        target_days=target_days, vel_days=vel_days, profit=profit,
        cols=cols, with_photos=with_photos, photo_count=photo_count,
        article_like=article_like,
    )


@router.post("/export/replenish/pdf")
def export_replenish_pdf_from_excel(
    payload: dict = Body(...),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    target_days: int = 30,
    window_days: int = 30,
    cols: Optional[str] = None,
    with_photos: int = 1,
    photo_count: int = 6,
    vel_days: int = replenish_service.WB_SORT_VELOCITY_DAYS,
    profit: int = 1,
    limit: int = PDF_DEFAULT_LIMIT,
    db: Session = Depends(get_db),
):
    """PDF из строк Excel-файла (дропзона в меню PDF).

    ``payload`` = ``{"rows": [...], "source": "имя.xlsx"}`` — строки из
    ``POST /replenish/import-excel``, ключи как в выгрузке Excel.

    ``wb_def`` («WB дефицит, шт») каждой строки задаёт бюджет «Итого
    дослать» по артикулу (см. ``replenish.apply_sort_budget``): пустая
    ячейка — бюджет не задан, размеры считаются по складу как обычно.
    Отбраковка убыточных/полных карточек не применяется: файл есть истина,
    ненужное владелец удалил сам. Фильтры таблицы (marketplace, поиск,
    сортировка) игнорируются — состав задаёт файл; окно/скорость/прибыльность
    и фото действуют как в базовом режиме.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    if date_from is None and date_to is None:
        back = max(1, int(window_days or settings.sync_days_default))
        from_ = to_ - timedelta(days=back - 1)
    span = (to_ - from_).days + 1

    raw = payload.get("rows")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "В файле нет строк для карточек")
    rows: list = []
    budgets: dict = {}
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HTTPException(
                400, f"Строка {i + 1}: ожидался объект, получен {type(item).__name__}"
            )
        r = dict(item)
        art = str(r.get("article") or "").strip()
        if not art:
            raise HTTPException(400, f"Строка {i + 1}: нет артикула")
        r["article"] = art
        rows.append(r)
        v = r.get("wb_def")
        if v is None or v == "":
            continue  # пустая ячейка = бюджет не задан, план как обычно
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f:  # NaN
            continue
        budgets[art.upper()] = max(0, int(round(f)))

    cap = max(1, min(int(limit or PDF_DEFAULT_LIMIT), PDF_MAX_LIMIT))
    truncated = max(0, len(rows) - cap)
    rows = rows[:cap]

    source = str(payload.get("source") or "").strip()[:60]
    return _replenish_pdf_build(
        db, rows,
        from_=from_, to_=to_, span=span, truncated=truncated,
        target_days=target_days, vel_days=vel_days, profit=profit,
        cols=cols, with_photos=with_photos, photo_count=photo_count,
        budgets=budgets,
        subtitle_extra=(" · из Excel: " + source) if source else " · из Excel",
    )

def _replenish_pdf_build(
    db: Session,
    rows: list,
    *,
    from_: date,
    to_: date,
    span: int,
    truncated: int,
    target_days: int,
    vel_days: int,
    profit,
    cols: Optional[str],
    with_photos: int,
    photo_count: int,
    article_like: Optional[str] = None,
    budgets: Optional[dict] = None,
    subtitle_extra: str = "",
) -> Response:
    """Общая сборка PDF-потребности: план подсортировки, фото, карточки.

    ``rows`` — строки с ключами выгрузки, уже обрезанные по ``limit``.
    ``budgets`` не None = режим файла (PDF из Excel): «Итого дослать»
    берёт бюджет из колонки «WB дефицит» по артикулу, а отбраковка
    убыточных/полных карточек не применяется — файл есть истина
    (см. ``replenish.apply_sort_budget``).
    ``subtitle_extra`` — приписка в шапке (имя Excel-файла).
    """
    vel_days = int(vel_days or 0)
    if vel_days not in replenish_service.WB_SORT_VELOCITY_WINDOWS:
        vel_days = replenish_service.WB_SORT_VELOCITY_DAYS
    vel_label = "всё время" if vel_days == 0 else f"{vel_days} дн"

    # План подсортировки: полный список размеров берём из карточки WB, а
    # скорость — за длинное окно. По окну спроса (30 дн) размеры без продаж
    # получают цель 0 и выпадают, хотя остатков на них может не быть вовсе.
    use_profit = bool(int(profit or 0))
    coverage = None
    factors = None
    if use_profit:
        # покрытие = четверть выбранного периода: недельный цикл пополнения
        coverage = max(0.25, span / 4.0)
        factors = replenish_service.wb_profit_factors(
            db, to_, velocity_days=vel_days,
            articles=[r.get("article") for r in rows],
            article_like=article_like,
        )
    plan = replenish_service.wb_sorting_plan(
        db, to_, target_days=target_days, velocity_days=vel_days,
        articles=[r.get("article") for r in rows],
        coverage_days=coverage, profit_factor=factors,
    )

    if budgets is not None:
        # Режим Excel: «Итого дослать» и распределение по размерам идут по
        # колонке «WB дефицит» из файла (пустые ячейки не задают бюджет).
        replenish_service.apply_sort_budget(plan, budgets)
    elif use_profit:
        # убыточные и уже полные товары в шопинг-листе не нужны: иначе PDF
        # наполовину состоит из нулей. Считаем отбракованные, чтобы X-Truncated
        # остался честным. В режиме Excel не применяется: файл есть истина.
        alive = [r for r in rows if plan.get(
            str(r.get("article") or "").strip().upper(), {"total": 0}
        )["total"] > 0]
        truncated += len(rows) - len(alive)
        rows = alive
        plan = {k: v for k, v in plan.items() if v["total"] > 0}

    # Дальше идёт только CPU-работа (обход фото, ReportLab) — она занимает
    # секунды-двадцать. Соединение из пула (5 + overflow 10) держим ровно
    # столько, сколько нужно БД: иначе несколько открытых PDF занимают весь
    # пул, и обычные запросы вроде «Применить» встают в 30-секундную очередь
    # pool_timeout. Дальше db не используется, close() в get_db идемпотентен.
    db.close()

    # Размеры по артикулу: в карточке это блок «размер / наличие / дослать».
    sizes_by_article: dict[str, list[dict]] = {
        art: v["sizes"] for art, v in plan.items() if v["sizes"]
    }

    want_photos = bool(with_photos) and photo_count > 0
    n_photos = max(0, min(int(photo_count or 0), PDF_MAX_PHOTOS)) if want_photos else 0

    index = photos_service.get_index()
    if want_photos and rows and not index.root.is_dir():
        raise HTTPException(
            400,
            f"Папка с фотографиями не найдена: {index.root}. "
            "Укажите путь в настройке PHOTOS_ROOT.",
        )
    jobs: list = []
    if want_photos and rows:
        tasks = []
        for i, r in enumerate(rows):
            paths = index.find(r.get("article") or "", n_photos)
            for p in paths:
                tasks.append((i, p))
        results: dict[tuple[int, str], bytes] = {}
        if tasks:
            with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4))) as pool:
                futures = {
                    pool.submit(thumbs_service.thumb_bytes, p): (i, p) for i, p in tasks
                }
                for fut in as_completed(futures):
                    idx, path = futures[fut]
                    try:
                        data = fut.result()
                    except Exception:
                        data = None
                    if data:
                        results[(idx, str(path))] = data
        buckets: dict[int, list[bytes]] = {i: [] for i in range(len(rows))}
        for (i, path), data in sorted(results.items(), key=lambda kv: kv[0]):
            buckets[i].append(data)
        for i, r in enumerate(rows):
            jobs.append((r, sizes_by_article.get(str(r.get("article") or "").strip().upper(), []),
                         buckets.get(i, [])))
    else:
        jobs = [
            (r, sizes_by_article.get(str(r.get("article") or "").strip().upper(), []), [])
            for r in rows
        ]

    # Только колонки из карты экспорта — иначе в подписи останется сырой ключ.
    wanted = _replenish_cols(cols, _REPLENISH_EXPORT)
    if not wanted:
        wanted = ["name", "need_buy", "demand", "status_label"]

    pdf = pdf_demand.build_demand_pdf(
        jobs,
        cols=wanted,
        labels=_REPLENISH_EXPORT,
        with_photos=want_photos,
        photo_count=n_photos or PDF_MAX_PHOTOS,
        subtitle=(
            f"{from_:%d.%m.%Y} — {to_:%d.%m.%Y}"
            f" · {'покрытие ' + _fmt_days(coverage) + ' дн (период/4)' if use_profit else 'запас ' + str(int(target_days or 0)) + ' дн'}"
            f" · скорость по {vel_label}"
            + (f" · прибыльность 0…{replenish_service.PROFIT_FACTOR_MAX:g}×"
               if use_profit else "")
            + (f" · пустой размер ≥ {replenish_service.WB_SORT_MIN_SIZE_STOCK} шт"
               if use_profit else "")
            + " · WB"
            + subtitle_extra
        ),
    )
    hits = sum(1 for _, _, t in jobs if t)
    fname = f"potrebnost_{from_:%Y-%m-%d}_{to_:%Y-%m-%d}.pdf"
    return Response(
        content=pdf,
        media_type=PDF_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Count": str(len(jobs)),
            "X-Photo-Hits": str(hits),
            "X-Truncated": str(truncated),
        },
    )


@router.get("/export/margin/detail")
def export_margin_detail(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    missing_only: int = 0,
    compare: int = 0,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.margin_detail_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        default_net_cost=settings.default_net_cost,
    )
    if compare and date_from and date_to:
        try:
            f = date.fromisoformat(date_from)
            t = date.fromisoformat(date_to)
            delta = (t - f).days
            prev_df = margin_service.margin_detail_dataframe(
                db, date_from=f - timedelta(days=delta), date_to=f - timedelta(days=1),
                article_like=article_like, default_net_cost=settings.default_net_cost,
            )
            df = margin_service.compare_margin_periods(df, prev_df)
        except (ValueError, TypeError):
            pass
    if missing_only:
        df = df[df["net_cost_est"] == True].drop(columns=["net_cost_est"])
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "nm_id": "Артикул WB", "name": "Наименование", "sells": "Продано, шт",
        "returns_qty": "Возвращено, шт",
        "stock_qty": "Остаток, шт", "stock_total": "Остаток всего, шт",
        "stock_in_way": "В пути, шт",
        "revenue": "Выручка, руб", "commission": "Комиссия, руб",
        "logistics": "Логистика, руб", "storage": "Хранение, руб",
        "services": "Услуги, руб", "income": "К перечислению, руб",
        "net_cost": "Себестоимость, руб", "margin_gross": "Маржа, до себестоимости, руб",
        "margin": "Прибыль, руб",
        "margin_per_one": "Прибыль на ед., руб", "margin_pct": "Прибыль, %",
        "margin_pct_income": "Прибыль % (к перечислению), %",
        "net_cost_est": "Себестоимость оценка",
        "sells_pp": "Пред. период: Продано, шт", "margin_pp": "Пред. период: Прибыль, руб",
        "delta_ru": "Δ прибыли, руб", "delta_pct": "Δ прибыли, %",
        "logistics_out": "Логистика туда, руб",
        "logistics_in": "Логистика обратно, руб",
        "commission_per_one": "Комиссия на ед., руб",
        "logistics_per_one": "Логистика на ед., руб",
        "logistics_out_per_one": "Логистика туда на ед., руб",
        "logistics_in_per_one": "Логистика обратно на ед., руб",
        "storage_per_one": "Хранение на ед., руб",
        "income_per_one": "К перечисл. на ед., руб",
        "revenue_per_one": "Средняя цена, руб",
        "margin_gross_per_one": "Маржа до себест. на ед., руб",
        "return_rate": "Доля возвратов, %",
    }, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Маржа")
    fname = (("detail_missing_cost" if missing_only else "margin_detail")) + f"_{from_}_{to_}.xlsx"
    if compare:
        fname = fname.replace(".xlsx", "_compare.xlsx")
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/margin/ozon-detail")
def export_margin_ozon_detail(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    missing_only: int = 0,
    compare: int = 0,
    by_size: int = 0,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.ozon_margin_detail_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        default_net_cost=settings.default_net_cost, by_size=bool(by_size),
    )
    if compare and date_from and date_to:
        try:
            f = date.fromisoformat(date_from)
            t = date.fromisoformat(date_to)
            delta = (t - f).days
            prev_df = margin_service.ozon_margin_detail_dataframe(
                db, date_from=f - timedelta(days=delta), date_to=f - timedelta(days=1),
                article_like=article_like, default_net_cost=settings.default_net_cost,
                by_size=bool(by_size),
            )
            df = margin_service.compare_margin_periods(df, prev_df)
        except (ValueError, TypeError):
            pass
    if missing_only:
        df = df[df["net_cost_est"] == True].drop(columns=["net_cost_est"])
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "nm_id": "Артикул WB", "name": "Наименование",
        "size": "Размер", "sizes_count": "Размеров", "offers_count": "Артикулов",
        "sells": "Продано, шт", "returns_qty": "Возвращено, шт",
        "postings": "Постинги",
        "revenue": "Выручка, руб", "commission": "Комиссия, руб",
        "services": "Услуги, руб", "income": "К перечислению, руб",
        "cashflow_est": "На р/с (оценка), руб",
        "storage": "Хранение, руб",
        "net_cost": "Себестоимость, руб", "margin": "Прибыль, руб",
        "margin_gross": "Маржа, до себестоимости, руб",
        "net_cost_est": "Себестоимость оценка",
        "margin_per_one": "Прибыль на ед., руб", "margin_pct": "Прибыль, %",
        "amount": "Сумма продажи, руб",
        "sells_pp": "Пред. период: Продано, шт", "margin_pp": "Пред. период: Прибыль, руб",
        "delta_ru": "Δ прибыли, руб", "delta_pct": "Δ прибыли, %",
        "commission_per_one": "Комиссия на ед., руб",
        "services_per_one": "Услуги на ед., руб",
        "storage_per_one": "Хранение на ед., руб",
        "income_per_one": "К перечисл. на ед., руб",
        "revenue_per_one": "Средняя цена, руб",
        "return_rate": "Доля возвратов, %",
        "accrued_sale": "Начислено: продажа, руб",
        "accrued_commission": "Начислено: комиссия, руб",
        "accrued_logistics": "Начислено: логистика, руб",
        "accrued_services": "Начислено: услуги, руб",
        "accrued_other": "Начислено: прочее, руб",
        "accrued_net": "На р/с (по начислениям), руб",
        "accrued_diff": "Δ нач. vs детал., руб",
        "accrued_coverage": "Есть начисления",
        "has_detail": "Есть детализация",
        "margin_accrued": "Прибыль (по начислениям), руб",
    }, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Маржа")
    fname = (("detail_missing_cost" if missing_only else "margin_ozon_detail")) + f"_{from_}_{to_}.xlsx"
    if compare:
        fname = fname.replace(".xlsx", "_compare.xlsx")
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/margin/funnel")
def export_margin_funnel(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.funnel_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like
    )
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "name": "Наименование", "views": "Просмотры",
        "opens": "Открытия карточки", "adds": "В корзину", "orders": "Заказы",
        "cancelled": "Отмены", "buyouts": "Выкупы", "avg_price": "Ср. цена, руб",
        "revenue": "Выручка (оценка), руб",
        "cart_pct": "В корзину, %", "order_pct": "Заказы, %",
        "net_cost": "Себестоимость, руб", "margin": "Маржа (оценка), руб",
        "margin_pct": "Маржа, %", "storage_est": "Хранение (оц.), руб",
        "buyout_sum": "Выкуп, руб", "subject_name": "Предмет",
        "brand_name": "Бренд", "product_rating": "Рейтинг товара",
        "feedback_rating": "Рейтинг отзывов", "stock_wb": "Остаток WB, шт",
        "stock_mp": "Остаток МП, шт", "stock_balance_sum": "Остаток (баланс)",
        "cancel_sum": "Отмены, руб", "avg_orders_per_day": "Заказов в день",
        "share_order_percent": "Доля заказов, %", "add_to_wishlist": "В избранное",
        "time_to_ready_min": "До готовности, мин", "localization_percent": "Локализация, %",
        "conv_to_cart_percent": "В корзину (воронка), %",
        "conv_cart_to_order_percent": "Корзина→Заказ, %",
        "conv_buyout_percent": "Выкуп, %",
        "wb_club_order_count": "WB Клуб: заказы", "wb_club_order_sum": "WB Клуб: заказы, руб",
        "wb_club_buyout_count": "WB Клуб: выкупы", "wb_club_buyout_sum": "WB Клуб: выкупы, руб",
        "wb_club_cancel_count": "WB Клуб: отмены", "wb_club_cancel_sum": "WB Клуб: отмены, руб",
        "wb_club_avg_price": "WB Клуб: ср. цена", "wb_club_buyout_percent": "WB Клуб: выкуп, %",
        "wb_club_avg_orders_per_day": "WB Клуб: заказов в день",
        "title": "Название", "subject_id": "ID предмета", "tags": "Теги",
        "past_views": "Пред. период: просмотры", "past_adds": "Пред. период: в корзину",
        "past_orders": "Пред. период: заказы", "past_cancelled": "Пред. период: отмены",
        "past_buyouts": "Пред. период: выкупы", "past_revenue": "Пред. период: выручка",
        "past_buyout_sum": "Пред. период: выкуп, руб", "past_cancel_sum": "Пред. период: отмены, руб",
        "past_avg_price": "Пред. период: ср. цена",
        "dy_views": "Динамика просмотров, %", "dy_adds": "Динамика корзины, %",
        "dy_orders": "Динамика заказов, %", "dy_cancelled": "Динамика отмен, %",
        "dy_buyouts": "Динамика выкупов, %", "dy_revenue": "Динамика выручки, %",
        "dy_avg_price": "Динамика ср. цены, %",
    }, cols)
    df = df.rename(columns=ru)
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
    article_like: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    payload = api_sales(marketplace, date_from, date_to, db)
    df = pd.DataFrame(payload["rows"])
    if article_like:
        keep = df["article"].str.lower().str.contains(
            common_service.like_to_regex(article_like), regex=True, na=False
        )
        df = df[keep].reset_index(drop=True)
    df, ru = excel_io.project_export(df, {
        "date": "Дата", "marketplace": "Маркетплейс", "article": "Артикул",
        "name": "Наименование", "quantity": "Продано, шт",
        "revenue": "Выручка, руб", "income": "К перечислению, руб",
    }, cols)
    df = df.rename(columns=ru)
    fname = f"sales_{payload['date_from']}_{payload['date_to']}.xlsx"
    return _xlsx_response(df, fname, len(df))
