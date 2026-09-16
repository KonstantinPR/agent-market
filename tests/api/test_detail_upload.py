"""Тесты ручной загрузки детализации продаж WB (Excel/zip)."""
import io
import zipfile

import pandas as pd
import pytest

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


def test_detail_rows_endpoint_lists_raw_rows(api_client):
    df = _detail_df().copy()
    df["Srid"] = ["sr-1", "sr-2"]
    r = api_client.post("/api/wb/detail-upload", files={
        "files": ("d.xlsx", _xlsx(df), XLSX),
    })
    assert r.status_code == 200
    assert r.json()["imported"] == 2
    rows = api_client.get("/api/wb/detail-rows",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert rows["total"] == 2
    assert {x["srid"] for x in rows["rows"]} == {"sr-1", "sr-2"}
    assert {x["article"] for x in rows["rows"]} == {"TST-1", "TST-2"}


def test_upload_detail_same_srid_idempotent(api_client):
    api_client.post("/api/wb/cards")  # products TST-1 для join в margin/detail
    df = _detail_df().iloc[:1].copy()
    df["Srid"] = ["sr-dedup"]
    f = _xlsx(df)
    r1 = api_client.post("/api/wb/detail-upload", files={"files": ("d.xlsx", f, XLSX)})
    r2 = api_client.post("/api/wb/detail-upload", files={"files": ("d.xlsx", f, XLSX)})
    assert r1.json()["imported"] == 1
    assert r2.json()["imported"] == 1
    rows = api_client.get("/api/wb/detail-rows",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert rows["total"] == 1
    view = api_client.get("/api/margin/detail",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert view["detail_articles"] == 1
    assert view["count"] == 1


def _dup_srid_df():
    """Продажа + «служебная» строка (доставка) с тем же Srid — дубль в батче."""
    common = {
        "Артикул поставщика": "TST-1",
        "Дата продажи": "2026-09-05",
        "Вознаграждение ВВ": -300.0,
        "Хранение (пр)": -40.0,
        "Штраф": -5.0,
    }
    return pd.DataFrame([
        {**common, "Srid": "sr-dup", "Кол-во": 1,
         "Вайлдберриз реализовал Товар (Пр)": 2000.0,
         "Услуги по доставке товара покупателю": 0.0,
         "К перечислению Продавцу за реализованный Товар": 1535.0},
        {**common, "Srid": "sr-dup", "Кол-во": 0,
         "Вайлдберриз реализовал Товар (Пр)": 0.0,
         "Услуги по доставке товара покупателю": 120.0,
         "К перечислению Продавцу за реализованный Товар": 0.0},
    ])


def test_upload_detail_duplicate_srid_collapsed_no_crash(api_client):
    r = api_client.post("/api/wb/detail-upload", files={
        "files": ("d.xlsx", _xlsx(_dup_srid_df()), XLSX),
    })
    assert r.status_code == 200
    assert r.json()["imported"] == 1
    rows = api_client.get("/api/wb/detail-rows",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert rows["total"] == 1
    row = rows["rows"][0]
    assert row["quantity"] == 1           # числовые поля суммированы
    assert row["logistics"] == 120        # доставка из служебной строки
    assert row["for_pay"] == 1535


def test_upload_detail_duplicate_srid_across_files(api_client):
    df = _dup_srid_df()
    r = api_client.post("/api/wb/detail-upload", files=[
        ("files", ("a.xlsx", _xlsx(df.iloc[:1]), XLSX)),
        ("files", ("b.xlsx", _xlsx(df.iloc[1:]), XLSX)),
    ])
    assert r.status_code == 200
    assert r.json()["imported"] == 1
    rows = api_client.get("/api/wb/detail-rows",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert rows["total"] == 1
    assert rows["rows"][0]["logistics"] == 120


def test_upload_detail_return_is_a_group_and_negative(api_client):
    df = pd.DataFrame([
        {"Srid": "sr-sale", "Артикул поставщика": "A1",
         "Дата продажи": "2026-09-05", "Тип документа": "Продажа",
         "Кол-во": 2, "Вайлдберриз реализовал Товар (Пр)": 2000.0,
         "К перечислению Продавцу за реализованный Товар": 1700.0,
         "Услуги по доставке товара покупателю": 100.0,
         "Вознаграждение ВВ": -200.0},
        {"Srid": "sr-ret", "Артикул поставщика": "A1",
         "Дата продажи": "2026-09-06", "Тип документа": "Возврат",
         "Кол-во": 1, "Вайлдберриз реализовал Товар (Пр)": 900.0,
         "К перечислению Продавцу за реализованный Товар": 700.0,
         "Услуги по доставке товара покупателю": 0.0,
         "Вознаграждение ВВ": -90.0},
    ])
    r = api_client.post("/api/wb/detail-upload", files={
        "files": ("d.xlsx", _xlsx(df), XLSX),
    })
    assert r.status_code == 200
    view = api_client.get("/api/margin/detail",
                           params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    a1 = next(x for x in view["rows"] if x["article"] == "A1")
    # возврат вычитается: 2 проданные − 1 возврат, выручка/доход нетто
    assert a1["sells"] == 1
    assert a1["revenue"] == 1100.0
    assert a1["income"] == 1000.0
    assert a1["logistics"] == 100.0  # расходы знак не меняют
    # Маржа до себестоимости: income − логистика − хранение − услуги
    assert a1["margin_gross"] == 1000.0 - 100.0  # 900
    # Маржа-себест.: margin_gross − дефолтная себестоимость 500
    assert a1["margin"] == 1000.0 - 100.0 - 500.0  # 400


def test_detail_returns_exceed_sales_no_cogs(api_client, db):
    from app import models
    db.add(models.Product(article="RET-ART", name="Возвратник", net_cost=500))
    db.commit()
    df = pd.DataFrame([
        {"Srid": "sr-s1", "Артикул поставщика": "RET-ART",
         "Дата продажи": "2026-09-05", "Тип документа": "Продажа",
         "Кол-во": 1, "Вайлдберриз реализовал Товар (Пр)": 1000.0,
         "К перечислению Продавцу за реализованный Товар": 700.0},
        {"Srid": "sr-s2", "Артикул поставщика": "RET-ART",
         "Дата продажи": "2026-09-06", "Тип документа": "Продажа",
         "Кол-во": 2, "Вайлдберриз реализовал Товар (Пр)": 2000.0,
         "К перечислению Продавцу за реализованный Товар": 1400.0},
        {"Srid": "sr-r1", "Артикул поставщика": "RET-ART",
         "Дата продажи": "2026-09-07", "Тип документа": "Возврат",
         "Кол-во": 5, "Вайлдберриз реализовал Товар (Пр)": 4500.0,
         "К перечислению Продавцу за реализованный Товар": 3500.0},
    ])
    api_client.post("/api/wb/detail-upload", files={"files": ("d.xlsx", _xlsx(df), XLSX)})
    view = api_client.get("/api/margin/detail",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    a1 = next(x for x in view["rows"] if x["article"] == "RET-ART")
    assert a1["sells"] == -2  # 3 продажи − 5 возвратов
    assert a1["income"] == 700.0 + 1400.0 - 3500.0  # возвраты идут в минус
    # Маржа до себестоимости корректна
    assert a1["margin_gross"] == pytest.approx(a1["income"])
    # возврат перекрывает продажи: себестоимость не вычитается (нет фиктивной прибыли)
    assert a1["margin"] == pytest.approx(a1["income"])
    assert a1["margin_per_one"] == 0.0
    assert a1["margin_pct"] == 0.0


def test_margin_detail_est_flag_and_formula(api_client, db):
    from app import models
    db.add(models.Product(article="WA-COST", name="С себестоимостью", net_cost=1200))
    db.add(models.Product(article="NO-COST", name="Без себестоимости", net_cost=0))
    db.commit()
    df = pd.DataFrame([
        {"Srid": "sr-a", "Артикул поставщика": "WA-COST",
         "Дата продажи": "2026-09-05", "Тип документа": "Продажа",
         "Кол-во": 1, "Вайлдберриз реализовал Товар (Пр)": 1000.0,
         "К перечислению Продавцу за реализованный Товар": 700.0},
        {"Srid": "sr-b", "Артикул поставщика": "NO-COST",
         "Дата продажи": "2026-09-05", "Тип документа": "Продажа",
         "Кол-во": 1, "Вайлдберриз реализовал Товар (Пр)": 1000.0,
         "К перечислению Продавцу за реализованный Товар": 700.0},
    ])
    api_client.post("/api/wb/detail-upload", files={"files": ("d.xlsx", _xlsx(df), XLSX)})
    view = api_client.get("/api/margin/detail",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert view["estimated"] == 1
    assert view["default_net_cost"] == 500.0
    by = {x["article"]: x for x in view["rows"]}
    assert by["WA-COST"]["net_cost"] == 1200.0
    assert by["WA-COST"]["net_cost_est"] is False
    assert by["NO-COST"]["net_cost"] == 500.0
    assert by["NO-COST"]["net_cost_est"] is True
    assert by["NO-COST"]["margin_gross"] == 700.0  # нет расходов WB — margin_gross == income
    assert by["NO-COST"]["margin"] == 700.0 - 500.0  # income − дефолтная себестоимость
    assert by["NO-COST"]["margin_pct"] == pytest.approx(20.0)


def test_export_margin_detail_missing_only(api_client, db):
    from app import models
    db.add(models.Product(article="WA-COST", name="С себестоимостью", net_cost=1200))
    db.commit()
    def row(srid, art):
        return {"Srid": srid, "Артикул поставщика": art,
                "Дата продажи": "2026-09-05", "Тип документа": "Продажа",
                "Кол-во": 1, "Вайлдберриз реализовал Товар (Пр)": 1000.0,
                "К перечислению Продавцу за реализованный Товар": 700.0}
    df = pd.DataFrame([row("sr-a", "WA-COST"), row("sr-b", "NO-COST")])
    api_client.post("/api/wb/detail-upload", files={"files": ("d.xlsx", _xlsx(df), XLSX)})
    r = api_client.get("/api/export/margin/detail", params={
        "date_from": "2026-09-01", "date_to": "2026-09-10", "missing_only": 1,
    })
    assert r.status_code == 200
    out = pd.read_excel(io.BytesIO(r.content))
    assert {"Артикул"}.issubset(set(out.columns))
    arts = set(out["Артикул"])
    assert "NO-COST" in arts
    assert "WA-COST" not in arts


def test_api_wb_detail_summary_raw_money_without_margin(api_client):
    df = pd.DataFrame([
        {"Srid": "sr-a1", "Артикул поставщика": "S1", "Дата продажи": "2026-09-05",
         "Тип документа": "Продажа", "Кол-во": 2,
         "Вайлдберриз реализовал Товар (Пр)": 2000.0,
         "Услуги по доставке товара покупателю": 100.0,
         "К перечислению Продавцу за реализованный Товар": 1700.0},
        {"Srid": "sr-a2", "Артикул поставщика": "S1", "Дата продажи": "2026-09-06",
         "Тип документа": "Возврат", "Кол-во": 1,
         "Вайлдберриз реализовал Товар (Пр)": 1000.0,
         "К перечислению Продавцу за реализованный Товар": 800.0},
    ])
    api_client.post("/api/wb/detail-upload", files={"files": ("d.xlsx", _xlsx(df), XLSX)})
    r = api_client.get("/api/wb/detail-summary", params={
        "date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    row = body["rows"][0]
    assert row["article"] == "S1"
    assert row["sells"] == 1  # 2 - 1
    assert row["returns_qty"] == 1
    assert row["revenue"] == 1000.0
    assert row["for_pay"] == 900.0
    assert row["logistics"] == 100.0
    # у свода по артикулам НЕТ аналитики (себестоимость/маржа)
    assert "margin" not in row
    assert "net_cost" not in row


def test_api_margin_detail_compare(api_client):
    def rows(srid_prefix, day, qty, for_pay):
        return [{"Srid": srid_prefix + "1", "Артикул поставщика": "CMP-1",
                 "Дата продажи": day, "Тип документа": "Продажа",
                 "Кол-во": qty, "Вайлдберриз реализовал Товар (Пр)": for_pay + 100,
                 "К перечислению Продавцу за реализованный Товар": for_pay}]
    # текущий период 09-01..09-02 (2 дня) → предыдущий 08-31..08-31
    api_client.post("/api/wb/detail-upload", files={
        "files": ("a.xlsx", _xlsx(pd.DataFrame(rows("sa", "2026-09-01", 2, 700.0))), XLSX)})
    api_client.post("/api/wb/detail-upload", files={
        "files": ("b.xlsx", _xlsx(pd.DataFrame(rows("sb", "2026-08-31", 1, 400.0))), XLSX)})
    r = api_client.get("/api/margin/detail", params={
        "date_from": "2026-09-01", "date_to": "2026-09-02", "compare": 1})
    assert r.status_code == 200
    body = r.json()
    row = next(x for x in body["rows"] if x["article"] == "CMP-1")
    # текущий: income=700, продано 2 → margin = 700 − 500(дефолт себест.)×2 = −300
    assert row["sells"] == 2
    assert row["margin"] == pytest.approx(-300.0)
    # пред. период: income=400, продано 1 → margin = 400 − 500 = −100
    assert row["sells_pp"] == 1
    assert row["margin_pp"] == pytest.approx(-100.0)
    assert row["delta_ru"] == pytest.approx(-200.0)   # −300 − (−100)
    assert row["delta_pct"] == pytest.approx(-200.0)  # −200/100×100
    assert body["prev_window"] == {"date_from": "2026-08-31", "date_to": "2026-08-31"}
    # без compare дельт нет
    r2 = api_client.get("/api/margin/detail", params={
        "date_from": "2026-09-01", "date_to": "2026-09-02"})
    row2 = next(x for x in r2.json()["rows"] if x["article"] == "CMP-1")
    assert "margin_pp" not in row2


def test_margin_detail_totals_row(api_client):
    """Ответ /margin/detail содержит totals — суммы по числовым колонкам."""
    df = pd.DataFrame([
        {"Srid": "sr-t1", "Артикул поставщика": "TOT-1",
         "Дата продажи": "2026-09-05", "Кол-во": 2,
         "Вайлдберриз реализовал Товар (Пр)": 2000.0,
         "Вознаграждение ВВ": -300.0,
         "К перечислению Продавцу за реализованный Товар": 1700.0},
        {"Srid": "sr-t2", "Артикул поставщика": "TOT-2",
         "Дата продажи": "2026-09-06", "Кол-во": 1,
         "Вайлдберриз реализовал Товар (Пр)": 1000.0,
         "Вознаграждение ВВ": -150.0,
         "К перечислению Продавцу за реализованный Товар": 850.0},
    ])
    api_client.post("/api/wb/detail-upload", files={"files": ("d.xlsx", _xlsx(df), XLSX)})
    body = api_client.get("/api/margin/detail",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    t = body["totals"]
    assert t["sells"] == 3
    assert t["revenue"] == 3000.0
    assert t["income"] == 2550.0


def test_detail_summary_new_fields(api_client):
    """Ответ /wb/detail-summary включает delivery_count, pvz_compensation, payment_services и totals."""
    df = pd.DataFrame([
        {"Srid": "sr-d1", "Артикул поставщика": "NS1",
         "Дата продажи": "2026-09-05", "Кол-во": 2,
         "Вайлдберриз реализовал Товар (Пр)": 2000.0,
         "Услуги по доставке товара покупателю": 100.0,
         "К перечислению Продавцу за реализованный Товар": 1900.0},
    ])
    api_client.post("/api/wb/detail-upload", files={"files": ("d.xlsx", _xlsx(df), XLSX)})
    r = api_client.get("/api/wb/detail-summary",
                       params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    body = r.json()
    row = body["rows"][0]
    assert row["article"] == "NS1"
    assert row["sells"] == 2
    assert "delivery_count" in row
    assert "return_delivery_count" in row
    assert "pvz_compensation" in row
    assert "payment_services" in row
    t = body["totals"]
    assert t["sells"] == 2
    assert t["revenue"] == 2000.0
    assert t["delivery_count"] == 0
    assert t["pvz_compensation"] == 0.0