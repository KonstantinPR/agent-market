from datetime import date

import pandas as pd

from app.services.excel_io import df_to_excel_stream


def _xlsx(df):
    return df_to_excel_stream(df).getvalue()


def _docs_df(rows):
    return pd.DataFrame(rows, columns=["Дата", "№ документа", "Контрагент", "Артикул", "Кол-во", "Цена"])


def test_import_and_list_counterparties(api_client):
    df = pd.DataFrame([
        {"Наименование": "Wildberries", "Тип": "маркетплейс", "ИНН": "7707323467"},
        {"Наименование": "ООО Поставщик", "Тип": "поставщик"},
    ])
    r = api_client.post(
        "/api/warehouse/import/counterparties",
        files={"file": ("cp.xlsx", _xlsx(df), "application/octet-stream")},
    )
    assert r.status_code == 200
    assert r.json()["created"] == 2

    rows = api_client.get("/api/warehouse/counterparties").json()["rows"]
    assert len(rows) == 2
    wb = next(x for x in rows if x["name"] == "Wildberries")
    assert wb["ctype"] == "marketplace"


def test_import_receipt_and_stock_via_api(api_client):
    today = date.today().isoformat()
    df = _docs_df([
        [today, "ТН-1", "ООО Поставщик", "A1", 5, 200],
        [today, "ТН-1", "ООО Поставщик", "A2", 3, 150],
    ])
    r = api_client.post(
        "/api/warehouse/import/docs?type=receipt",
        files={"file": ("rec.xlsx", _xlsx(df), "application/octet-stream")},
    )
    assert r.status_code == 200
    assert r.json()["docs_created"] == 1

    docs = api_client.get("/api/warehouse/docs?type=receipt").json()["rows"]
    assert len(docs) == 1
    doc_id = docs[0]["id"]
    assert docs[0]["items_count"] == 2

    items = api_client.get(f"/api/warehouse/docs/{doc_id}/items").json()["rows"]
    assert len(items) == 2

    stock = api_client.get("/api/warehouse/stock").json()["rows"]
    by_art = {r["article"]: r for r in stock}
    assert by_art["A1"]["received"] == 5
    assert by_art["A1"]["balance"] == 5


def test_doc_crud_json_api(api_client):
    r = api_client.post("/api/warehouse/counterparties", json={"name": "ООО Поставщик"})
    cp_id = r.json()["id"]

    r = api_client.post("/api/warehouse/docs", json={
        "type": "receipt", "doc_num": "ТН-77",
        "doc_date": date.today().isoformat(), "counterparty_id": cp_id, "note": "",
        "items": [{"article": "B1", "quantity": 2, "price": 500}],
    })
    assert r.status_code == 200
    assert r.json()["total"] == 1000
    doc_id = r.json()["id"]

    assert api_client.delete(f"/api/warehouse/docs/{doc_id}").status_code == 200
    assert len(api_client.get("/api/warehouse/docs?type=receipt").json()["rows"]) == 0


def test_exports_return_xlsx(api_client):
    for kind in ["counterparties", "template-counterparties", "template-docs", "stock", "turnover", "docs", "docs-list"]:
        url = f"/api/warehouse/export/{kind}"
        if kind in ("docs", "docs-list"):
            url += "?type=receipt"
        r = api_client.get(url)
        assert r.status_code == 200, kind
        assert r.headers["content-type"].startswith("application/vnd.openxmlformats")


def test_paths_settings_scan_and_import(api_client, db, tmp_path):
    import openpyxl

    from app import models

    user = models.User(username="wh-paths-user")
    db.add(user)
    db.flush()
    company = models.Company(user_id=user.id, name="ООО Пути")
    db.add(company)
    db.flush()
    cab = models.Cabinet(
        user_id=user.id, company_id=company.id, code="oo_joinco", name="Джойнс WB",
        schema="cab_paths", marketplace="wb", creds="", enabled=True, position=0,
    )
    db.add(cab)
    db.add(models.Counterparty(id=1, name="Wildberries", ctype="marketplace"))
    db.commit()

    folder = tmp_path / "440 - 26.05.2026 - Рязань"
    folder.mkdir()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ОПРЕДЛАРТ"
    for row in [["Артикул", "Кол-во"], ["A1", 2], ["A1", 3]]:
        ws.append(row)
    wb.save(folder / "DB.xlsm")

    r = api_client.put("/api/warehouse/paths/settings", json={
        "cabinet_id": cab.id,
        "settings": {
            "root": str(tmp_path), "sheet": "ОПРЕДЛАРТ", "price_mode": "zero",
            # Служебные ключи не должны попасть в warehouse_settings (белый список).
            "roots": [{"path": "подделка"}], "evil": 1,
        },
    })
    assert r.status_code == 200
    data = api_client.get(f"/api/warehouse/paths/settings?cabinet_id={cab.id}").json()
    assert data["root"] == str(tmp_path)
    assert data["root_exists"] is True
    # GET отдаёт оба дефолтных пути связки — по ним строится селект в UI.
    assert [x["path"] for x in data["roots"]] == [
        r"C:\YandexDisk\СЕТЕВЫЕ МАГАЗИНЫ\WILDBERRIES\ДЖОИНС\ОТПРАВКИ",
        r"E:\Мой диск\ООО ДЖОИНС\ОТПРАВКИ",
    ]
    assert all("exists" in x for x in data["roots"])
    # Белый список: служебные ключи не попали в сохранённый JSON.
    import json
    db.expire_all()
    raw = json.loads(db.get(models.Cabinet, cab.id).warehouse_settings)
    assert "roots" not in raw and "evil" not in raw
    assert raw["root"] == str(tmp_path)

    scan = api_client.post("/api/warehouse/paths/scan", json={"cabinet_id": cab.id}).json()
    assert scan["summary"]["new"] == 1
    assert scan["items"][0]["folder"] == folder.name

    imp = api_client.post("/api/warehouse/paths/import", json={
        "cabinet_id": cab.id, "folder": folder.name,
    })
    assert imp.status_code == 200, imp.text
    assert imp.json()["items_count"] == 1
    assert imp.json()["total"] == 0

    rows = api_client.get("/api/warehouse/docs?type=shipment").json()["rows"]
    assert len(rows) == 1
    assert rows[0]["folder"] == folder.name
    assert rows[0]["note"] == "Рязань"
    assert rows[0]["source"] == "paths"

    items = api_client.get(f"/api/warehouse/docs/{rows[0]['id']}/items").json()["rows"]
    assert len(items) == 1
    assert items[0]["barcode"] == ""
    assert items[0]["size"] == ""
    assert items[0]["quantity"] == 5

    assert api_client.get("/api/warehouse/export/docs-list?type=shipment").status_code == 200
