from datetime import date

import pandas as pd

from app import models


def test_wb_detail_gaps_api(api_client, stub_wb, db):
    stub_wb.reports_list = pd.DataFrame([
        {"reportId": "900001", "dateFrom": "2026-09-01", "dateTo": "2026-09-07",
         "createDate": "2026-09-08"},
        {"reportId": "900002", "dateFrom": "2026-09-08", "dateTo": "2026-09-14",
         "createDate": "2026-09-15"},
    ])
    db.add(models.WbDetailRow(
        op_key="gap:1", source="api", report_id="900001", article="TST-1",
        sale_dt=date(2026, 9, 2), quantity=1,
    ))
    db.commit()

    r = api_client.get("/api/wb/detail-gaps",
                       params={"date_from": "2026-09-01",
                               "date_to": "2026-09-30"})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] == 2
    assert body["downloaded"] == 1
    assert [m["report_id"] for m in body["missing"]] == ["900002"]


def test_wb_detail_gaps_api_rejects_bad_period(api_client):
    r = api_client.get("/api/wb/detail-gaps", params={"period": "yearly"})
    assert r.status_code == 400
