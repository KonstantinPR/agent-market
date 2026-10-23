# -*- coding: utf-8 -*-
"""Юнит-тесты выгрузки аналитики на диск (app/services/analytics_disk.py).

Реальные отчёты Прибыльности требуют данных в схеме связки, поэтому для
write_reports подменяем реестр отчётов лёгким фейком (пишем одну строку).
Проверяем: сборку пути фирма→WB/OZON, пропуск неизвестной фирмы, атомарную
перезапись файлов и разбор периода.
"""
import pandas as pd
import pytest

from app import models
from app.services import analytics_disk as ad
from app.services import cabinets as cabinet_service


def _mk_company(db, name="Фирма аналитики", analytics_dir=""):
    user = cabinet_service.seed_user(db)
    c = models.Company(user_id=user.id, name=name, enabled=True, position=9,
                       analytics_dir=analytics_dir)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _mk_cab(db, company, marketplace="wb", schema=None, code="ad-cab"):
    user = cabinet_service.seed_user(db)
    c = models.Cabinet(user_id=user.id, company_id=company.id, code=code,
                       name="Связка", schema=schema or ("cab_" + code.replace("-", "_")),
                       marketplace=marketplace,
                       creds="", enabled=True, position=9)
    db.add(c)
    db.commit()
    db.refresh(c)
    return cabinet_service.info_by_id(db, c.id, user)


def test_company_dir_wb_and_ozon(db, tmp_path):
    comp = _mk_company(db, analytics_dir=r"ООО ДЖОИНС\AGENT MARKET\АНАЛИТИКА")
    wb = _mk_cab(db, comp, marketplace="wb", code="ad-wb")
    oz = _mk_cab(db, comp, marketplace="ozon", code="ad-oz")

    assert ad.company_dir(db, wb, root=str(tmp_path)) == (
        tmp_path / "ООО ДЖОИНС" / "AGENT MARKET" / "АНАЛИТИКА" / "WB")
    assert ad.company_dir(db, oz, root=str(tmp_path)) == (
        tmp_path / "ООО ДЖОИНС" / "AGENT MARKET" / "АНАЛИТИКА" / "OZON")


def test_company_dir_none_without_analytics_dir(db, tmp_path):
    comp = _mk_company(db, analytics_dir="")
    cab = _mk_cab(db, comp)
    assert ad.company_dir(db, cab, root=str(tmp_path)) is None


def test_write_reports_creates_and_overwrites(db, tmp_path, monkeypatch):
    comp = _mk_company(db, analytics_dir=r"ИП ПРУДНИКОВ\AGENT MARKET\АНАЛИТИКА")
    wb = _mk_cab(db, comp, marketplace="wb")

    calls = {}

    def fake_report(name):
        def _fn(dbb, f, t):
            calls[name] = (f, t)
            return pd.DataFrame({"A": [1, 2]})
        return _fn

    monkeypatch.setattr(ad, "_REPORTS_BY_MP", {
        "wb": (
            ("margin_funnel.xlsx", "Воронка", "export_margin_funnel",
             fake_report("funnel")),
            ("margin_detail.xlsx", "Маржа", "export_margin_detail",
             fake_report("detail")),
        ),
    })
    monkeypatch.setattr(ad, "_df_to_bytes", lambda df, sheet, name, **kw: b"DATA")

    res = ad.write_reports(db, wb, "2026-09-01", "2026-09-10", root=str(tmp_path))
    folder = tmp_path / "ИП ПРУДНИКОВ" / "AGENT MARKET" / "АНАЛИТИКА" / "WB"
    assert res["files"] == ["margin_funnel.xlsx", "margin_detail.xlsx"]
    assert (folder / "margin_funnel.xlsx").read_bytes() == b"DATA"
    assert (folder / "margin_detail.xlsx").read_bytes() == b"DATA"
    # период распарсен в date-объекты
    assert str(calls["funnel"][0]) == "2026-09-01"
    assert str(calls["detail"][1]) == "2026-09-10"
    assert res["rows"] == 4  # 2 строки × 2 отчёта
    # временных файлов не осталось, файлы атомарно заменены
    assert not list(folder.glob("*.tmp"))

    # повторный запуск перезаписывает (файл не растёт, .tmp не копится)
    monkeypatch.setattr(ad, "_df_to_bytes", lambda df, sheet, name, **kw: b"NEW")
    res2 = ad.write_reports(db, wb, "2026-09-01", "2026-09-10", root=str(tmp_path))
    assert res2["files"] == res["files"]
    assert (folder / "margin_funnel.xlsx").read_bytes() == b"NEW"


def test_write_reports_raises_without_folder(db):
    comp = _mk_company(db, analytics_dir="")
    cab = _mk_cab(db, comp)
    with pytest.raises(RuntimeError, match="папка аналитики"):
        ad.write_reports(db, cab, "2026-09-01", "2026-09-10")


def test_write_reports_raises_for_unknown_marketplace(db, tmp_path, monkeypatch):
    comp = _mk_company(db, analytics_dir="ФИРМА")
    cab = _mk_cab(db, comp, marketplace="wb")
    monkeypatch.setattr(ad, "_REPORTS_BY_MP", {})
    with pytest.raises(RuntimeError, match="не поддерживается"):
        ad.write_reports(db, cab, "2026-09-01", "2026-09-10", root=str(tmp_path))


def test_period_sheet_name_fits_excel_limits():
    from datetime import date

    name = ad.period_sheet_name(date(2026, 9, 1), date(2026, 9, 10))
    assert name == "01.09.2026-10.09.2026"
    assert len(name) <= 31
    assert not set(name) & set(r":\/?*[]")


def test_df_to_bytes_adds_empty_period_sheet():
    """Второй лист книги назван периодом и пуст (данные — только на первом)."""
    from io import BytesIO

    data = ad._df_to_bytes(
        pd.DataFrame({"A": [1, 2]}), "Воронка", "export_margin_funnel",
        period="01.09.2026-10.09.2026")
    xl = pd.ExcelFile(BytesIO(data), engine="openpyxl")
    assert xl.sheet_names == ["Воронка", "01.09.2026-10.09.2026"]
    assert len(pd.read_excel(xl, sheet_name="Воронка")) == 2
    assert pd.read_excel(xl, sheet_name="01.09.2026-10.09.2026").empty

