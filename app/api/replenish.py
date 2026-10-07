# -*- coding: utf-8 -*-
"""«Потребность в товаре»: /api/replenish/* + PDF."""
from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _REPLENISH_EXPORT,
    _parse_window400,
)

router = APIRouter()



@router.get("/replenish")
def api_replenish(
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
    db: Session = Depends(get_db),
):
    """Потребность в товаре: спрос (детализации) + остатки (наш склад и МП).

    Спрос = продажи − возвраты за окно (шт/день). target_days — целевой запас
    в днях продаж; window_days — число дней по умолчанию, когда не указаны
    даты окна. sort: urgency | margin | name. view: article | sizes —
    размерный разрез (WB) по той же логике. hide_zero_sizes — в разрезе
    «по размерам» скрыть размеры без продаж и остатков (везде 0).
    """
    from_, to_ = _parse_window400(date_from, date_to)
    if date_from is None and date_to is None:
        # окно задано не фильтрами, а window_days: двигаем назад от даты to_.
        back = max(1, int(window_days or settings.sync_days_default))
        from_ = to_ - timedelta(days=back - 1)
    span = None
    try:
        span = (to_ - from_).days + 1
    except (TypeError, ValueError):
        span = max(1, int(window_days or settings.sync_days_default))

    result = replenish_service.replenish_rows(
        db, from_, to_, target_days=target_days, span_days=span,
        marketplace=marketplace, sort=sort, article_like=article_like,
        show_inactive=bool(show_inactive), hide_zero_sizes=bool(hide_zero_sizes),
        view=view,
    )
    result["date_from"] = from_.isoformat()
    result["date_to"] = to_.isoformat()
    return result


@router.post("/replenish/import-excel")
async def replenish_import_excel(file: UploadFile = File(...)):
    """Разбор Excel-файла с правками для PDF (дропзона в меню PDF).

    Файл — обычная выгрузка «Потребность» (кнопка «Excel»), отредактированная
    в Excel. Возвращает ``{rows, meta}``: строки с ключами выгрузки для POST
    ``/export/replenish/pdf`` и сводку предпросмотра — сколько карточек
    распознано, какие колонки неизвестны, сколько строк без артикула
    отброшено.
    """
    name = (file.filename or "").strip().lower()
    if not name.endswith(".xlsx"):
        raise HTTPException(400, "Нужен файл .xlsx (выгрузка «Потребность»)")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Файл пустой")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 20 МБ — похоже, это не выгрузка")
    try:
        rows, meta = excel_import.parse_replenish_excel(data, _REPLENISH_EXPORT)
    except excel_import.ExcelImportError as e:
        raise HTTPException(400, str(e))
    if not rows:
        raise HTTPException(400, "В файле нет строк с артикулом")
    meta["file"] = file.filename or ""
    return {"rows": rows, "meta": meta}
