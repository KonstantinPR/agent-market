from datetime import date

import pandas as pd

from app import models
from app.services import refresh as r
from tests.fakes import FakeWb


def _add_row(db, report_id, day, article="TST-1"):
    db.add(models.WbDetailRow(
        op_key=f"gap:{report_id}:{day}", source="api", report_id=report_id,
        article=article, sale_dt=day, quantity=1,
    ))


def _add_row_blank_report(db, day, article="TST-1"):
    """Строка из ручной загрузки Excel ЛК: номера отчёта нет (report_id='')."""
    db.add(models.WbDetailRow(
        op_key=f"gap:blank:{day}", source="excel", report_id="",
        article=article, sale_dt=day, quantity=1,
    ))


def test_gaps_detects_missing_reports(db):
    stub = FakeWb()
    stub.reports_list = pd.DataFrame([
        {"reportId": "900001", "dateFrom": "2026-09-01", "dateTo": "2026-09-07",
         "createDate": "2026-09-08"},
        {"reportId": "900003", "dateFrom": "2026-09-08", "dateTo": "2026-09-14",
         "createDate": "2026-09-15"},
        {"reportId": "900005", "dateFrom": "2026-09-15", "dateTo": "2026-09-21",
         "createDate": "2026-09-22"},
    ])
    _add_row(db, "900001", date(2026, 9, 2))
    _add_row(db, "900005", date(2026, 9, 16))
    db.commit()

    out = r.wb_detail_gaps(db, date_from="2026-09-01", date_to="2026-09-30",
                           provider=stub)
    assert out["available"] == 3
    assert out["downloaded"] == 2
    assert [m["report_id"] for m in out["missing"]] == ["900003"]
    assert out["missing"][0]["date_from"] == "2026-09-08"
    assert out["from"] == "2026-09-01" and out["to"] == "2026-09-21"


def test_gaps_empty_when_all_downloaded(db):
    stub = FakeWb()
    stub.reports_list = pd.DataFrame([
        {"reportId": "900001", "dateFrom": "2026-09-01", "dateTo": "2026-09-07",
         "createDate": "2026-09-08"},
    ])
    _add_row(db, "900001", date(2026, 9, 2))
    db.commit()

    out = r.wb_detail_gaps(db, date_from="2026-09-01", date_to="2026-09-30",
                           provider=stub)
    assert out["available"] == 1
    assert out["downloaded"] == 1
    assert out["missing"] == []


def test_gaps_period_with_rows_counts_as_downloaded(db):
    """Ручная загрузка Excel ЛК без номера отчёта всё равно закрывает неделю."""
    stub = FakeWb()
    stub.reports_list = pd.DataFrame([
        {"reportId": "910001", "dateFrom": "2026-09-01", "dateTo": "2026-09-07",
         "createDate": "2026-09-08"},
        {"reportId": "910002", "dateFrom": "2026-09-08", "dateTo": "2026-09-14",
         "createDate": "2026-09-15"},
    ])
    _add_row_blank_report(db, date(2026, 9, 3))
    db.commit()

    out = r.wb_detail_gaps(db, date_from="2026-09-01", date_to="2026-09-30",
                           provider=stub)
    assert out["available"] == 2
    assert out["downloaded"] == 1
    assert [m["report_id"] for m in out["missing"]] == ["910002"]


def test_gaps_reports_error_as_http_400_on_bad_date(db):
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        r.wb_detail_gaps(db, date_from="нет-даты", date_to="2026-09-30",
                         provider=FakeWb())
    assert exc.value.status_code == 400
