"""T-19: API каталога «Наш склад → Товары» (/api/products*)."""
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _refresh(client):
    r = client.post("/api/products/refresh")
    assert r.status_code == 200, r.text
    return r.json()


def test_products_refresh_builds_catalog(api_client):
    body = _refresh(api_client)
    assert body["ok"] is True
    assert body["report"]["created_products"] == 4

    agg = api_client.get("/api/products").json()
    by = {p["article"]: p for p in agg["rows"]}
    assert {"TST-1", "TST-2", "OZ-1", "OZ-2"} <= set(by)
    assert by["TST-1"]["sizes_count"] == 1
    assert set(by["TST-1"]["tags"]) == {"wb"}
    assert set(by["OZ-1"]["tags"]) == {"ozon"}
    assert agg["price_settings"]["round_nice"] is True


def test_products_sizes_rows(api_client):
    _refresh(api_client)
    data = api_client.get("/api/products", params={"sizes": 1}).json()
    pairs = {(r["article"], r["size"]) for r in data["rows"]}
    assert ("TST-1", "46") in pairs
    assert ("TST-2", "47") in pairs
    assert ("OZ-1", "") in pairs
    assert data["count"] == 4


def test_products_stocks_and_price(api_client, db):
    from app import models

    _refresh(api_client)
    prod = db.get(models.Product, "TST-1")
    prod.net_cost = 100.0
    prod.volume_l = 1.0
    db.commit()

    data = api_client.get("/api/products", params={"stocks": 1}).json()
    by = {p["article"]: p for p in data["rows"]}
    assert "own_stock" in by["TST-1"] and "mp_stock" in by["TST-1"]
    assert by["TST-1"]["recommended_price"] == 1049.0
    assert by["TST-1"]["markup"] > 1

    liked = api_client.get("/api/products", params={"like": "TST"}).json()
    assert {p["article"] for p in liked["rows"]} == {"TST-1", "TST-2"}


def test_products_price_settings(api_client):
    r = api_client.get("/api/products/price-settings")
    assert r.status_code == 200
    body = r.json()
    assert body["defaults"]["cost_anchors"] == [[100.0, 10.0], [2000.0, 3.0]]
    assert "cost_anchors" in body["labels"]
    assert "cost_anchors" in body["hints"]


def test_products_preview_custom_settings(api_client, db):
    from app import models

    _refresh(api_client)
    prod = db.get(models.Product, "TST-1")
    prod.net_cost = 100.0
    prod.volume_l = 1.0
    db.commit()

    r = api_client.post("/api/products/preview", json={
        "like": "TST-1",
        "price_settings": {"cost_anchors": [[100.0, 2.0], [2000.0, 2.0]]},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["rows"][0]["recommended_price"] == 209.0  # 100 × 2 × 1 → 209
    assert body["price_settings"]["cost_anchors"] == [[100.0, 2.0], [2000.0, 2.0]]


def test_export_products_xlsx(api_client):
    _refresh(api_client)
    r = api_client.get("/api/export/products", params={"sizes": 1, "stocks": 1})
    assert r.status_code == 200
    assert XLSX in r.headers["content-type"]
    assert r.headers["X-Count"] == "4"

    r2 = api_client.get("/api/export/products", params={"cols": "article,name"})
    assert r2.status_code == 200
