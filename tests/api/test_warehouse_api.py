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
    for kind in ["counterparties", "template-counterparties", "template-docs", "stock", "turnover", "docs"]:
        url = f"/api/warehouse/export/{kind}"
        if kind == "docs":
            url += "?type=receipt"
        r = api_client.get(url)
        assert r.status_code == 200, kind
        assert r.headers["content-type"].startswith("application/vnd.openxmlformats")