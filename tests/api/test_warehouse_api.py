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


# ── «Отправки»: связки, товарный разрез, сводка (T-58) ───────────────────────

def _two_cabinets(db):
    """Две связки одной фирмы: WB и Ozon — как в шапке приложения."""
    from app import models

    user = models.User(username="wh-ship-user")
    db.add(user)
    db.flush()
    company = models.Company(user_id=user.id, name="Джойнс")
    db.add(company)
    db.flush()
    wb = models.Cabinet(
        user_id=user.id, company_id=company.id, code="oo_joinco", name="Джойнс WB",
        schema="cab_ship_wb", marketplace="wb", creds="", enabled=True, position=0,
    )
    oz = models.Cabinet(
        user_id=user.id, company_id=company.id, code="ip_prudnikov_oz", name="Прудников Ozon",
        schema="cab_ship_oz", marketplace="ozon", creds="", enabled=True, position=1,
    )
    db.add_all([wb, oz])
    db.commit()
    return wb.id, oz.id


def _shipment(db, num, day, items, cabinet_id=None):
    """Отправка прямо в БД: товарный разрез проверяем без Excel и папок."""
    from app import models

    doc = models.WarehouseDoc(
        doc_type="shipment", doc_num=num, doc_date=day, cabinet_id=cabinet_id,
        source="paths", source_ref=f"oo_joinco:{num}", total=0,
    )
    db.add(doc)
    db.flush()
    total = 0.0
    for article, barcode, size, qty, price in items:
        amount = qty * price
        total += amount
        db.add(models.WarehouseDocItem(
            doc_id=doc.id, article=article, name="Товар " + article, barcode=barcode,
            size=size, quantity=qty, price=price, amount=amount,
        ))
    doc.total = total
    db.commit()
    return doc.id


def _shipment_fixture(db):
    """Две WB-отправки, одна Ozon и одна без связки (ручной импорт)."""
    wb_id, oz_id = _two_cabinets(db)
    _shipment(db, "440 - 10.01.2026 - Рязань", date(2026, 1, 10), [
        ("A1", "111", "M", 2, 100),
        ("A2", "222", "L", 1, 300),
    ], cabinet_id=wb_id)
    _shipment(db, "441 - 20.02.2026 - Тула", date(2026, 2, 20), [
        ("A1", "111", "M", 3, 200),
    ], cabinet_id=wb_id)
    _shipment(db, "OZ-1", date(2026, 3, 1), [
        ("B1", "333", "", 5, 50),
    ], cabinet_id=oz_id)
    _shipment(db, "MANUAL-1", date(2026, 3, 5), [
        ("A1", "111", "M", 1, 500),
    ], cabinet_id=None)
    return wb_id, oz_id


def test_shipment_cabinets_counts(api_client, db):
    wb_id, oz_id = _shipment_fixture(db)

    rows = api_client.get("/api/warehouse/shipment-cabinets").json()["rows"]
    by_id = {r["id"]: r for r in rows}
    assert by_id[wb_id]["count"] == 2
    assert by_id[oz_id]["count"] == 1
    # Документы без связки в счётчики не попадают — их не к чему приписать.
    assert None not in by_id
    assert by_id[wb_id]["label"] == "Джойнс · Wildberries"


def test_docs_list_filters_by_cabinets_and_keeps_unassigned(api_client, db):
    wb_id, oz_id = _shipment_fixture(db)

    all_rows = api_client.get("/api/warehouse/docs?type=shipment").json()["rows"]
    assert len(all_rows) == 4

    only_wb = api_client.get(
        f"/api/warehouse/docs?type=shipment&cabinet_ids={wb_id}").json()["rows"]
    # Отправки WB + документ без связки: чужие связки скрыты, «бесхозное» видно.
    assert sorted(r["doc_num"] for r in only_wb) == [
        "440 - 10.01.2026 - Рязань", "441 - 20.02.2026 - Тула", "MANUAL-1",
    ]

    both = api_client.get(
        f"/api/warehouse/docs?type=shipment&cabinet_ids={wb_id},{oz_id}").json()["rows"]
    assert len(both) == 4

    # Мусор в параметре = без фильтра, а не пустая таблица.
    junk = api_client.get("/api/warehouse/docs?type=shipment&cabinet_ids=x,").json()["rows"]
    assert len(junk) == 4


def test_shipment_items_rows_filter_and_search(api_client, db):
    wb_id, oz_id = _shipment_fixture(db)

    data = api_client.get("/api/warehouse/shipment-items").json()
    assert data["group"] == "rows"
    assert data["total"] == 5 and data["count"] == 5
    # Свежие отправки сверху.
    assert data["rows"][0]["doc_num"] == "MANUAL-1"
    assert data["rows"][0]["cabinet"] == ""  # ручной документ без связки

    wb_rows = api_client.get(
        f"/api/warehouse/shipment-items?cabinet_ids={wb_id}").json()["rows"]
    assert {r["article"] for r in wb_rows} == {"A1", "A2"}
    # Строки WB подписаны связкой, строка ручного документа — пустой связкой:
    # документы без связки видны при любом фильтре (см. wh-shipment).
    assert {r["cabinet"] for r in wb_rows} == {"Джойнс · Wildberries", ""}

    # Поиск «*» — любая часть, регистр не важен; ищет и по артикулу, и по штрихкоду.
    found = api_client.get("/api/warehouse/shipment-items?article_like=a*").json()
    assert found["total"] == 4
    by_barcode = api_client.get("/api/warehouse/shipment-items?article_like=333").json()
    assert [r["article"] for r in by_barcode["rows"]] == ["B1"]

    # T-67: якоря «^…$» работают и на серверном фильтре — точное совпадение,
    # а не подстрока.
    anchored = api_client.get(
        "/api/warehouse/shipment-items?article_like=^A1$").json()
    assert anchored["total"] == 3
    assert {r["article"] for r in anchored["rows"]} == {"A1"}
    prefix_only = api_client.get(
        "/api/warehouse/shipment-items?article_like=^B").json()
    assert [r["article"] for r in prefix_only["rows"]] == ["B1"]

    assert api_client.get(
        "/api/warehouse/shipment-items?group=nope").status_code == 400


def test_shipment_items_paging_reports_truncation(api_client, db):
    _shipment_fixture(db)

    page = api_client.get("/api/warehouse/shipment-items?limit=2&offset=2").json()
    assert page["total"] == 5
    assert page["count"] == 2
    assert page["offset"] == 2 and page["limit"] == 2
    assert page["truncated"] is True
    # Итоги считаются по всей выборке, а не по отданной порции.
    assert page["totals"]["quantity"] == 12
    assert page["totals"]["amount"] == 1850
    assert page["totals"]["price"] == 1850 / 12
    assert page["totals"]["rows"] == 5 and page["totals"]["docs"] == 4

    tail = api_client.get("/api/warehouse/shipment-items?limit=2&offset=4").json()
    assert tail["count"] == 1 and tail["truncated"] is False
    # В сводке «документов» нет смысла — поле остаётся пустым, а не врёт.
    sku = api_client.get("/api/warehouse/shipment-items?group=sku").json()
    assert sku["totals"]["docs"] is None
    assert sku["totals"]["quantity"] == 12


def test_shipment_summary_sku_and_article(api_client, db):
    wb_id, _oz_id = _shipment_fixture(db)

    sku = api_client.get("/api/warehouse/shipment-items?group=sku").json()["rows"]
    a1 = next(r for r in sku if r["article"] == "A1")
    # 2 шт по 100 + 3 шт по 200 + 1 шт по 500 (документ без связки тоже виден).
    assert a1["quantity"] == 6
    assert a1["amount"] == 1300
    assert a1["price"] == 1300 / 6  # средневзвешенная, а не среднее цен
    assert a1["docs_count"] == 3
    assert a1["barcode"] == "111" and a1["size"] == "M"
    assert a1["first_date"] == "2026-01-10" and a1["last_date"] == "2026-03-05"
    # Самые «тяжёлые» строки сверху.
    assert sku[0]["article"] == "A1"

    art = api_client.get(
        f"/api/warehouse/shipment-items?group=article&cabinet_ids={wb_id}").json()["rows"]
    a1_wb = next(r for r in art if r["article"] == "A1")
    # 2+3 шт из отправок WB и 1 шт из документа без связки — он виден всегда.
    assert a1_wb["quantity"] == 6 and a1_wb["amount"] == 1300
    assert a1_wb["barcode"] == "" and a1_wb["size"] == ""  # размеры сложены
    assert a1_wb["docs_count"] == 3
    assert a1_wb["cabinets"] == "Джойнс · Wildberries"
    # Товар чужой связки в выборку не попал.
    assert [r["article"] for r in art] == ["A1", "A2"]


def test_shipment_exports_return_xlsx(api_client, db):
    wb_id, _oz_id = _shipment_fixture(db)

    for kind in ["shipment-items", "shipment-sku", "shipment-article"]:
        r = api_client.get(
            f"/api/warehouse/export/{kind}?article_like=A1&cabinet_ids={wb_id}")
        assert r.status_code == 200, kind
        assert r.headers["content-type"].startswith("application/vnd.openxmlformats")

    import openpyxl
    from io import BytesIO

    buf = BytesIO(api_client.get(
        "/api/warehouse/export/shipment-sku").content)
    ws = openpyxl.load_workbook(buf).active
    head = [c.value for c in ws[1]]
    assert head[:4] == ["Артикул", "Наименование", "Штрихкод", "Размер"]
    assert head[-1] == "Последняя"
    assert ws.max_row == 4  # шапка + 3 разреза артикул+размер (A1/A2/B1)

    # Список документов тоже уважает фильтр связок.
    buf = BytesIO(api_client.get(
        f"/api/warehouse/export/docs-list?type=shipment&cabinet_ids={wb_id}").content)
    ws = openpyxl.load_workbook(buf).active
    assert ws.max_row == 4  # шапка + 2 документа WB + документ без связки

    # …и поиск: выгрузка совпадает с тем, что видно в таблице.
    buf = BytesIO(api_client.get(
        "/api/warehouse/export/docs-list?type=shipment&article_like=Тула").content)
    ws = openpyxl.load_workbook(buf).active
    assert ws.max_row == 2  # шапка + «441 - 20.02.2026 - Тула»
