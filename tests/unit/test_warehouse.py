from datetime import date

import pandas as pd

from app import models
from app.services import warehouse


def _docs_df(rows):
    return pd.DataFrame(
        rows,
        columns=["Дата", "№ документа", "Контрагент", "Артикул", "Кол-во", "Цена"],
    )


def test_import_counterparties_upsert_and_ctype(db):
    df = pd.DataFrame([
        {"Наименование": "Wildberries", "ИНН": "7707323467", "Тип": "маркетплейс", "Телефон": "", "Примечание": "WB"},
        {"Наименование": "Ozon", "ИНН": "7728567110", "Тип": "маркетплейс", "Примечание": ""},
        {"Наименование": "ООО Поставщик", "Тип": "поставщик"},
    ])
    r = warehouse.import_counterparties(db, df)
    assert r == {"created": 3, "updated": 0}
    wb = db.query(models.Counterparty).filter(models.Counterparty.name == "Wildberries").one()
    assert wb.ctype == "marketplace"
    assert wb.inn == "7707323467"

    r2 = warehouse.import_counterparties(db, df)
    assert r2 == {"created": 0, "updated": 3}


def test_receipt_import_groups_documents_and_recalc_net_cost(db):
    db.add(models.Product(article="A1", name="Товар А1", net_cost=100))
    db.add(models.CustomStock(article="A1", quantity=10, net_cost=100))
    db.commit()

    df = _docs_df([
        [date(2026, 9, 1), "ТН-1", "ООО Поставщик", "A1", 5, 200],
        [date(2026, 9, 1), "ТН-1", "ООО Поставщик", "A2", 3, 150],
        [date(2026, 9, 2), "ТН-2", "ООО Поставщик", "A1", 2, 400],
    ])
    r = warehouse.import_docs(db, df, doc_type="receipt")
    assert r["docs_created"] == 2

    docs = db.query(models.WarehouseDoc).all()
    assert len(docs) == 2
    n1 = next(d for d in docs if d.doc_num == "ТН-1")
    assert n1.doc_date == date(2026, 9, 1)
    assert len(n1.items) == 2
    assert float(n1.total) == 5 * 200 + 3 * 150

    rows = {r_["article"]: r_ for r_ in warehouse.stock_view(db)}
    a1 = rows["A1"]
    assert a1["start_qty"] == 10
    assert a1["received"] == 7
    assert a1["shipped"] == 0
    assert a1["balance"] == 17
    assert a1["avg_cost"] == round((10 * 100 + 5 * 200 + 2 * 400) / 17, 2)

    prod = db.query(models.Product).get("A1")
    assert float(prod.net_cost) == a1["avg_cost"]


def test_import_docs_idempotent(db):
    df = _docs_df([
        [date(2026, 9, 1), "ТН-1", "ООО Поставщик", "A1", 5, 200],
        [date(2026, 9, 1), "ТН-1", "ООО Поставщик", "A2", 3, 150],
    ])
    warehouse.import_docs(db, df, doc_type="receipt")
    warehouse.import_docs(db, df, doc_type="receipt")
    warehouse.import_docs(db, df, doc_type="receipt")

    docs = db.query(models.WarehouseDoc).all()
    assert len(docs) == 1
    assert len(docs[0].items) == 2
    assert float(docs[0].total) == 5 * 200 + 3 * 150


def test_shipment_decreases_balance_and_turnover(db):
    db.add(models.Counterparty(name="Wildberries", ctype="marketplace"))
    db.add(models.Counterparty(name="ООО Поставщик", ctype="supplier"))
    db.commit()
    warehouse.import_docs(
        db, _docs_df([[date(2026, 9, 1), "П-1", "ООО Поставщик", "A1", 10, 100]]),
        doc_type="receipt",
    )
    warehouse.import_docs(
        db, _docs_df([[date(2026, 9, 5), "ОТ-1", "Wildberries", "A1", 4, 300]]),
        doc_type="shipment",
    )

    rows = {r_["article"]: r_ for r_ in warehouse.stock_view(db)}
    assert rows["A1"]["balance"] == 6
    assert rows["A1"]["shipped"] == 4

    turnover = {t["counterparty"]: t for t in warehouse.turnover_view(db)}
    assert turnover["ООО Поставщик"]["in_n"] == 1
    assert turnover["Wildberries"]["out_sum"] == 4 * 300


def test_create_doc_and_delete(db):
    cp = models.Counterparty(name="ООО Поставщик")
    db.add(cp)
    db.commit()

    r = warehouse.create_doc(
        db, "receipt", "ТН-77", date(2026, 9, 3), cp.id, "тест",
        [{"article": "B1", "quantity": 2, "price": 500}],
    )
    assert r["total"] == 1000
    assert db.query(models.WarehouseDoc).count() == 1

    warehouse.delete_doc(db, r["id"])
    assert db.query(models.WarehouseDoc).count() == 0
    assert db.query(models.WarehouseDocItem).count() == 0


def test_docs_export_columns(db):
    db.add(models.Counterparty(name="ООО Поставщик"))
    db.commit()
    warehouse.import_docs(
        db, _docs_df([[date(2026, 9, 1), "П-1", "ООО Поставщик", "A1", 5, 200]]),
        doc_type="receipt",
    )
    df = warehouse.docs_dataframe(db, "receipt")
    assert list(df.columns) == ["Дата", "№ документа", "Контрагент", "Артикул", "Кол-во", "Цена"]
    assert len(df) == 1
    assert df.iloc[0]["Контрагент"] == "ООО Поставщик"


def test_weighted_avg_ignores_shipment_price(db):
    # себестоимость строится только по приходам, цена отгрузки не влияет
    warehouse.import_docs(
        db, _docs_df([[date(2026, 9, 1), "П-1", "П", "A1", 10, 200]]), doc_type="receipt",
    )
    warehouse.import_docs(
        db, _docs_df([[date(2026, 9, 2), "ОТ-1", "WB", "A1", 6, 999]]), doc_type="shipment",
    )
    rows = {r_["article"]: r_ for r_ in warehouse.stock_view(db)}
    assert rows["A1"]["avg_cost"] == 200
    assert rows["A1"]["balance"] == 4