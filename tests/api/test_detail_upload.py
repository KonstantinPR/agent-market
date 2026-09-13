"""Тесты ручной загрузки детализации продаж WB (Excel/zip)."""
import io
import zipfile

import pandas as pd

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return buf.getvalue()


def _detail_df():
    # Русские заголовки файла WB «Детализация продаж»
    return pd.DataFrame([
        {
            "Артикул поставщика": "TST-1",
            "Дата продажи": "2026-09-05",
            "Кол-во": 2,
            "Вайлдберриз реализовал Товар (Пр)": 2000.0,
            "Вознаграждение ВВ": -300.0,
            "Услуги по доставке товара покупателю": -120.0,
            "Хранение (пр)": -40.0,
            "Штраф": -5.0,
            "К перечислению Продавцу за реализованный Товар": 1535.0,
        },
        {
            "Артикул поставщика": "TST-2",
            "Дата продажи": "2026-09-06",
            "Кол-во": 1,
            "Вайлдберриз реализовал Товар (Пр)": 900.0,
            "Вознаграждение ВВ": -135.0,
            "Услуги по доставке товара покупателю": -60.0,
            "Хранение (пр)": 0.0,
            "Штраф": 0.0,
            "К перечислению Продавцу за реализованный Товар": 705.0,
        },
    ])


def test_upload_detail_xlsx_writes_source_detail(api_client):
    api_client.post("/api/wb/cards")  # products TST-1/TST-2 для join в margin/detail
    r = api_client.post("/api/wb/detail-upload", files={
        "files": ("d.xlsx", _xlsx(_detail_df()), XLSX),
    })
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == 2
    assert body["imported"] == 2
    assert not body["errors"]

    pulls = api_client.get("/api/pulls").json()
    assert any(p["api"] == "wb" and p["kind"] == "detail" and p["db_rows"] == 2 for p in pulls)

    view = api_client.get("/api/margin/detail",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert view["count"] == 2
    assert {row["article"] for row in view["rows"]} == {"TST-1", "TST-2"}


def test_upload_detail_zip(api_client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("one.xlsx", _xlsx(_detail_df().iloc[:1]))
        zf.writestr("two.xlsx", _xlsx(_detail_df().iloc[1:]))
    buf.seek(0)
    r = api_client.post("/api/wb/detail-upload", files={
        "files": ("d.zip", buf.getvalue(), "application/zip"),
    })
    assert r.status_code == 200
    assert r.json()["rows"] == 2
    assert r.json()["imported"] == 2


def test_upload_detail_bad_structure_rejected(api_client):
    df = pd.DataFrame([{"Товар": "X", "Сумма": "10"}])
    r = api_client.post("/api/wb/detail-upload", files={
        "files": ("bad.xlsx", _xlsx(df), XLSX),
    })
    assert r.status_code == 400
    assert "Артикул" in r.json()["detail"]


def test_upload_detail_skips_non_excel(api_client):
    r = api_client.post("/api/wb/detail-upload", files={
        "files": ("notes.txt", b"hello", "text/plain"),
    })
    assert r.status_code == 400


def test_upload_detail_write_db_0(api_client):
    r = api_client.post("/api/wb/detail-upload?write_db=0", files={
        "files": ("d.xlsx", _xlsx(_detail_df()), XLSX),
    })
    assert r.status_code == 200
    assert r.json()["rows"] == 2
    assert r.json()["imported"] == 0