# -*- coding: utf-8 -*-
"""Выгрузка Excel-отчётов Прибыльности в папки Dropbox на локальном диске.

Кнопка «Обновить аналитику на диск» в «Массовых действиях» формирует отчёты
раздела Прибыльность и ПЕРЕЗАПИСЫВАЕТ стабильные .xlsx-файлы в папку фирмы
(companies.analytics_dir) с подпапкой по маркетплейсу (WB/OZON). Так отчёты
доступны с других устройств через синхронизацию Dropbox.

Период берётся из шапки (С/По), как у существующих кнопок «Загрузить на диск».
Каждый файл получает второй (пустой) лист с датами периода в названии —
«01.09.2026-10.09.2026», чтобы по открытой книге было видно, за что отчёт.
Отчёты и их колонки — те же, что и в HTTP-экспорте (export_reports.py),
поэтому генерируем их прямо из сервис-датафреймов, без StreamingResponse.
"""
import shutil
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.config import settings
from app.services import excel_io, margin as margin_service
from app.services.columns import export_cols
from app.services.window import parse_window

#: Отчёты по маркетплейсу: (имя файла, лист, cols-ключ, функция датафрейма).
#: Имена файлов стабильны — повторный запуск перезаписывает прежние.
WB_REPORTS = (
    ("margin_funnel.xlsx", "Воронка", "export_margin_funnel",
     lambda db, f, t: margin_service.funnel_dataframe(
         db, date_from=f, date_to=t)),
    ("margin_detail.xlsx", "Маржа", "export_margin_detail",
     lambda db, f, t: margin_service.margin_detail_dataframe(
         db, date_from=f, date_to=t, default_net_cost=settings.default_net_cost)),
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

    Excel ограничивает имя листа 31 символом и запрещает : \\ / ? * [ ] —
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
        tmp = folder / (fname + ".tmp")
        tmp.write_bytes(data)
        shutil.move(str(tmp), str(folder / fname))  # атомарная замена
        files.append(fname)
        rows += int(len(df))
    if not files:
        raise RuntimeError("не сформировано ни одного файла")
    return {"folder": str(folder), "files": files, "rows": rows}
