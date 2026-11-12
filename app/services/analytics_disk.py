# -*- coding: utf-8 -*-
"""Выгрузка Excel-отчётов Прибыльности в папки Dropbox на локальном диске.

Кнопка «Обновить аналитику на диск» в «Массовых действиях» формирует ежедневный
набор отчётов раздела Прибыльность и ПЕРЕЗАПИСЫВАЕТ стабильные .xlsx-файлы в
папку фирмы (companies.analytics_dir) с подпапкой по маркетплейсу (WB/OZON).
Так отчёты доступны с других устройств через синхронизацию Dropbox.

Набор WB (6 файлов):
  1. margin_funnel.xlsx  — воронка продаж (с областью «движение за 2 года»);
  2. margin_detail.xlsx  — детализация продаж (та же область);
  3. margin_stocks.xlsx  — остатки на последнем срезе;
  4. margin_storage.xlsx — стоимость хранения;
  5. margin_cards.xlsx   — все карточки WB с баркодами и описанием;
  6. margin_groups.xlsx  — группы товаров как «Обзор» + динамика к прошлому
     периоду и к 365 дням до периода.

Период берётся из шапки (С/По), как у существующих кнопок «Загрузить на диск».
Каждый файл получает второй (пустой) лист с датами периода в названии —
«01.09.2026-10.09.2026», чтобы по открытой книге было видно, за что отчёт.
Отчёты и их колонки — те же, что и в HTTP-экспорте (export_reports.py),
поэтому генерируем их прямо из сервис-датафреймов, без StreamingResponse.

Перед записью нового файла прежняя версия переносится в подпапку ARCHIVE с
суффиксом ``_archive`` (``margin_detail_archive.xlsx``). Следующий запуск
перезаписывает этот единственный архивный файл — история не копится.
"""
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.services import dashboard as dashboard_service
from app.services import excel_io, margin as margin_service
from app.services.columns import export_cols
from app.services.window import parse_window

#: Подпапка с предыдущими версиями отчётов.
ARCHIVE_DIR = "ARCHIVE"

# Явная регистрация словаря групп: в WB_REPORTS ключ подставляется переменной
# export_name, а манифест колонок (tests/unit/test_column_manifest.py) ищет
# литеральную ссылку на каждый словарь columns.json.
export_cols("export_margin_groups")


# ---------------------------------------------------------------------------
# Датафреймы дополнительных отчётов
# ---------------------------------------------------------------------------
def _stocks_df(db: Session, date_from, date_to) -> pd.DataFrame:
    """Остатки WB на последнем срезе (как вкладка «Остатки»)."""
    from app.api._common import api_stocks

    payload = api_stocks(marketplace="wb", db=db)
    return pd.DataFrame(payload.get("rows") or [])


def _storage_df(db: Session, date_from, date_to) -> pd.DataFrame:
    """Стоимость хранения WB (storage_costs, последняя загрузка)."""
    from app.api._common import api_storage_cost

    payload = api_storage_cost(db=db)
    return pd.DataFrame(payload.get("rows") or [])


def _cards_df(db: Session, date_from, date_to) -> pd.DataFrame:
    """Все карточки WB (marketplace_cards): баркоды + описание.

    api_cards ограничен 5000 строк на страницу, поэтому для выгрузки читаем
    таблицу целиком — это «все карточки товаров» из требования.
    """
    cols = ["chrt_id", "nm_id", "vendor_code", "brand", "subject", "size",
            "barcode", "volume_l", "composition", "name"]
    rows = db.execute(
        select(
            models.MarketplaceCard.chrt_id,
            models.MarketplaceCard.nm_id,
            models.MarketplaceCard.vendor_code,
            models.MarketplaceCard.brand,
            models.MarketplaceCard.subject,
            models.MarketplaceCard.size,
            models.MarketplaceCard.barcode,
            models.MarketplaceCard.volume_l,
            models.MarketplaceCard.composition,
            models.MarketplaceCard.name,
        )
        .join(models.Marketplace,
              models.MarketplaceCard.marketplace_id == models.Marketplace.id)
        .where(models.Marketplace.code == "wb")
        .order_by(models.MarketplaceCard.vendor_code,
                  models.MarketplaceCard.size)
    )
    return pd.DataFrame([
        {
            "chrt_id": str(r.chrt_id or ""),
            "nm_id": str(r.nm_id or ""),
            "vendor_code": str(r.vendor_code or ""),
            "brand": str(r.brand or ""),
            "subject": str(r.subject or ""),
            "size": str(r.size or ""),
            "barcode": str(r.barcode or ""),
            "volume_l": float(r.volume_l or 0),
            "composition": str(r.composition or ""),
            "name": str(r.name or ""),
        }
        for r in rows
    ], columns=cols)


#: Метрики групп, для которых считается динамика.
_GROUP_METRICS = ("sells", "revenue", "income", "margin")


def _group_map(db: Session, date_from, date_to) -> dict:
    """{prefix: строка grouped_margin} за окно."""
    payload = dashboard_service.grouped_margin(
        db, date_from, date_to, marketplace="wb", limit=None)
    return {str(r.get("prefix") or ""): r for r in (payload.get("rows") or [])}


def _dyn_pct(cur: float, base) -> Optional[float]:
    """Динамика, %: (cur − base) / |base| × 100. None — базы нет/она нулевая."""
    if base is None or base == 0:
        return None
    return round((cur - base) / abs(base) * 100, 2)


def _groups_df(db: Session, date_from, date_to) -> pd.DataFrame:
    """Показатели групп товаров (group_by=prefix) как в «Обзоре» + динамика.

    К текущим метрикам добавляются значения предыдущего аналогичного периода и
    365 дней непосредственно перед текущим окном, а также динамика в % по
    продажам/выручке/к перечислению/прибыли.
    """
    current_rows = dashboard_service.grouped_margin(
        db, date_from, date_to, marketplace="wb", limit=None).get("rows") or []
    if not current_rows:
        return pd.DataFrame()
    days = (date_to - date_from).days + 1
    prev_from, prev_to = date_from - timedelta(days=days), date_from - timedelta(days=1)
    old_from, old_to = date_from - timedelta(days=365), date_from - timedelta(days=1)
    prev = _group_map(db, prev_from, prev_to)
    old = _group_map(db, old_from, old_to)
    out = []
    for src in current_rows:
        prefix = str(src.get("prefix") or "")
        row = dict(src)
        p = prev.get(prefix)
        o = old.get(prefix)
        for m in _GROUP_METRICS:
            cur = float(src.get(m) or 0)
            pv = float(p.get(m) or 0) if p is not None else None
            ov = float(o.get(m) or 0) if o is not None else None
            row["%s_prev" % m] = pv
            row["dyn_%s_prev" % m] = _dyn_pct(cur, pv)
            row["%s_365" % m] = ov
            row["dyn_%s_365" % m] = _dyn_pct(cur, ov)
        out.append(row)
    return pd.DataFrame(out)


#: Отчёты по маркетплейсу: (имя файла, лист, cols-ключ, функция датафрейма).
#: Имена файлов стабильны — повторный запуск перезаписывает прежние, а старая
#: версия уходит в ARCHIVE.
WB_REPORTS = (
    ("margin_funnel.xlsx", "Воронка", "export_margin_funnel",
     lambda db, f, t: margin_service.funnel_dataframe(
         db, date_from=f, date_to=t, include_universe=True)),
    ("margin_detail.xlsx", "Маржа", "export_margin_detail",
     lambda db, f, t: margin_service.margin_detail_dataframe(
         db, date_from=f, date_to=t, default_net_cost=settings.default_net_cost,
         include_universe=True)),
    ("margin_stocks.xlsx", "Остатки", "export_wb_stock", _stocks_df),
    ("margin_storage.xlsx", "Хранение", "export_wb_storage", _storage_df),
    ("margin_cards.xlsx", "Карточки", "export_wb_cards", _cards_df),
    ("margin_groups.xlsx", "Группы", "export_margin_groups", _groups_df),
)
OZON_REPORTS = (
    ("margin_ozon_detail.xlsx", "Маржа", "export_margin_ozon_detail",
     lambda db, f, t: margin_service.ozon_margin_detail_dataframe(
         db, date_from=f, date_to=t, default_net_cost=settings.default_net_cost)),
)

_REPORTS_BY_MP = {"wb": WB_REPORTS, "ozon": OZON_REPORTS}


def company_dir(db: Session, info, root: Optional[str] = None) -> Optional[Path]:
    """Папка выгрузки связки: <root>/<analytics_dir фирмы>/<WB|OZON>.

    None — у фирмы не задан analytics_dir (выгрузка не настроена) или связка
    не привязана к компании. analytics_dir хранится в companies.
    """
    company_id = getattr(info, "company_id", None)
    rel = ""
    if company_id:
        from sqlalchemy import select

        from app import models
        row = db.execute(
            select(models.Company.analytics_dir)
            .where(models.Company.id == company_id)
        ).scalar()
        rel = (row or "").strip()
    if not rel:
        return None
    mp_dir = "WB" if info.marketplace == "wb" else "OZON"
    base = Path(root if root is not None else settings.analytics_root)
    return base / Path(rel) / mp_dir


def period_sheet_name(from_, to_) -> str:
    """Название листа-метки периода: «01.09.2026-10.09.2026».

    Excel ограничивает имя листа 31 символом и запрещает : \ / ? * [ ] —
    точки и дефисы разрешены, 21 символ влезает с запасом.
    """
    return f"{from_:%d.%m.%Y}-{to_:%d.%m.%Y}"


def _df_to_bytes(df, sheet_name: str, export_name: str,
                 period: Optional[str] = None) -> bytes:
    df, ru = excel_io.project_export(df, export_cols(export_name), None)
    df = df.rename(columns=ru)
    # Второй лист — пустая метка периода: по имени видно, за какие даты отчёт
    # (сам файл перезаписывается, имя файла стабильное).
    sheets = {sheet_name: df}
    if period:
        sheets[period] = pd.DataFrame()
    buf = excel_io.dfs_to_excel_stream(sheets)
    return buf.getvalue()


def _archive_existing(folder: Path, fname: str) -> Optional[Path]:
    """Переносит текущий файл в ARCHIVE/<stem>_archive<suffix>.

    Предыдущий архивный файл перезаписывается (история не копится). Возвращает
    путь архива или None, если переносить нечего.
    """
    target = folder / fname
    if not target.exists():
        return None
    archive = folder / ARCHIVE_DIR
    archive.mkdir(parents=True, exist_ok=True)
    dest = archive / (target.stem + "_archive" + target.suffix)
    if dest.exists():
        dest.unlink()
    shutil.move(str(target), str(dest))
    return dest


def write_reports(db: Session, info, date_from, date_to, root: Optional[str] = None) -> dict:
    """Формирует и перезаписывает Excel-отчёты связки в её папку Dropbox.

    Вызывается под search_path связки (см. бatch._worker). Возвращает
    {"folder": путь, "files": [...], "rows": N}. Бросает RuntimeError, если
    папка фирмы не настроена, или если ни один отчёт не удалось сформировать.
    """
    folder = company_dir(db, info, root=root)
    if folder is None:
        raise RuntimeError(
            f"для фирмы связки «{info.name}» не задана папка аналитики "
            "(companies.analytics_dir)")
    reports = _REPORTS_BY_MP.get(info.marketplace, ())
    if not reports:
        raise RuntimeError(f"маркетплейс {info.marketplace} не поддерживается")
    from_, to_ = parse_window(date_from, date_to)
    folder.mkdir(parents=True, exist_ok=True)
    period = period_sheet_name(from_, to_)

    files = []
    rows = 0
    for fname, sheet, export_name, fn in reports:
        df = fn(db, from_, to_)
        data = _df_to_bytes(df, sheet, export_name, period=period)
        # Прежняя версия — в ARCHIVE (перезаписывается при следующем запуске).
        _archive_existing(folder, fname)
        tmp = folder / (fname + ".tmp")
        tmp.write_bytes(data)
        shutil.move(str(tmp), str(folder / fname))  # атомарная замена
        files.append(fname)
        rows += int(len(df))
    if not files:
        raise RuntimeError("не сформировано ни одного файла")
    return {"folder": str(folder), "files": files, "rows": rows}
