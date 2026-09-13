import io
import zipfile

import pandas as pd

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return buf.getvalue()


WB_COLS = {
    "Бренд": ["КАТ", "ABSENT"],
    "Предмет": ["Брюки", "Туфли"],
    "Код размера (chrt_id)": [111, 222],
    "Артикул продавца": ["V-1", "V-2"],
    "Артикул WB": [501, 502],
    "Размер": ["26", "38"],
    "Баркод": ["8001", "8002"],
    "Объем, л.": ["2,7", "8.16"],
    "Состав": ["хлопок", "кожа 20%"],
}


def _wb_df():
    return dict(WB_COLS)


# ---------------------------------------------------------------------- импорт карточек
def test_import_cards_single_file(api_client):
    df = pd.DataFrame(_wb_df())
    r = api_client.post("/api/import/cards?marketplace=wb",
                        files={"files": ("wb_cards.xlsx", _xlsx(df), XLSX)})
    body = r.json()
    assert r.status_code == 200
    assert body["imported"] == 2
    assert body["total"] == 2

    cards = api_client.get("/api/cards?marketplace=wb").json()
    assert cards["count"] == 2
    by_chrt = {row["chrt_id"]: row for row in cards["rows"]}
    first = by_chrt["111"]
    assert first["vendor_code"] == "V-1"
    assert first["volume_l"] == 2.7
    assert first["composition"] == "хлопок"

    # общий каталог обновлён из карточек
    products = api_client.get("/api/products").json()
    arts = {p["article"] for p in products["rows"]}
    assert {"V-1", "V-2"} <= arts
    assert products["count"] == 2

    pulls = api_client.get("/api/pulls").json()
    assert any(p["api"] == "wb" and p["kind"] == "cards_excel" and p["db_rows"] == 2 for p in pulls)


def test_import_cards_multiple_files(api_client):
    df1 = pd.DataFrame(_wb_df())
    df2 = pd.DataFrame({k: [f"{v}-2" for v in vals[:1]] for k, vals in _wb_df().items()})
    r = api_client.post(
        "/api/import/cards?marketplace=wb",
        files=[
            ("files", ("a.xlsx", _xlsx(df1), XLSX)),
            ("files", ("b.xlsx", _xlsx(df2), XLSX)),
        ],
    )
    body = r.json()
    assert r.status_code == 200
    assert body["imported"] == 3
    assert body["total"] == 3
    assert body["files"][0]["rows"] == 2
    assert body["files"][1]["rows"] == 1
    assert api_client.get("/api/cards").json()["count"] == 3


def test_import_cards_zip_with_mixed_members(api_client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cards1.xlsx", _xlsx(pd.DataFrame(_wb_df())))
        zf.writestr("cards2.xlsx", _xlsx(pd.DataFrame({k: [f"{v}-3"] for k, v in _wb_df().items()})))
        zf.writestr("readme.txt", "игнорим")
    r = api_client.post("/api/import/cards?marketplace=wb",
                        files={"files": ("cards.zip", buf.getvalue(), "application/zip")})
    body = r.json()
    assert r.status_code == 200
    assert body["imported"] == 3
    assert body["total"] == 3
    assert api_client.get("/api/cards").json()["count"] == 3


def test_import_cards_non_excel_only_400(api_client):
    r = api_client.post("/api/import/cards?marketplace=wb",
                        files={"files": ("data.txt", b"hello", "text/plain")})
    assert r.status_code == 400


def test_import_cards_bad_marketplace_400(api_client):
    df = pd.DataFrame(_wb_df())
    r = api_client.post("/api/import/cards?marketplace=yandex",
                        files={"files": ("c.xlsx", _xlsx(df), XLSX)})
    assert r.status_code == 400


def test_cards_search(api_client):
    df = pd.DataFrame(_wb_df())
    api_client.post("/api/import/cards?marketplace=wb",
                    files={"files": ("c.xlsx", _xlsx(df), XLSX)})
    r = api_client.get("/api/cards?marketplace=wb&like=ABSENT").json()
    assert r["count"] == 1
    assert r["total"] == 1
    assert r["rows"][0]["vendor_code"] == "V-2"


def test_cards_server_pagination(api_client):
    df1 = pd.DataFrame(_wb_df())
    df2 = pd.DataFrame({k: [f"{v}-2" for v in vals[:1]] for k, vals in _wb_df().items()})
    api_client.post(
        "/api/import/cards?marketplace=wb",
        files=[
            ("files", ("a.xlsx", _xlsx(df1), XLSX)),
            ("files", ("b.xlsx", _xlsx(df2), XLSX)),
        ],
    )
    page1 = api_client.get("/api/cards?marketplace=wb&limit=2&offset=0").json()
    assert page1["total"] == 3
    assert page1["count"] == 2
    page2 = api_client.get("/api/cards?marketplace=wb&limit=2&offset=2").json()
    assert page2["total"] == 3
    assert page2["count"] == 1
    seen = {r["chrt_id"] for r in page1["rows"]} | {r["chrt_id"] for r in page2["rows"]}
    assert seen == {"111", "222", "111-2"}


def test_cards_offset_beyond_total_is_empty(api_client):
    df = pd.DataFrame(_wb_df())
    api_client.post("/api/import/cards?marketplace=wb",
                    files={"files": ("c.xlsx", _xlsx(df), XLSX)})
    r = api_client.get("/api/cards?marketplace=wb&limit=5&offset=10").json()
    assert r["total"] == 2
    assert r["count"] == 0
    assert r["rows"] == []


# ------------------------------------------------------------------ API pull пишет карточки
def test_wb_cards_api_writes_marketplace_cards(api_client):
    r = api_client.post("/api/wb/cards")
    assert r.status_code == 200
    cards = api_client.get("/api/cards?marketplace=wb").json()
    assert cards["count"] == 2
    assert {row["chrt_id"] for row in cards["rows"]} == {"101", "102"}
    assert {row["nm_id"] for row in cards["rows"]} == {"1001", "1002"}