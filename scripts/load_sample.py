"""Генерирует детерминированные мок-данные (CSV в data/mock/) и загружает их в БД."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app import models

MOCK_DIR = settings.mock_dir
N_ARTICLES = 20
DAYS = 30

BASE_NAMES = [
    "Футболка хлопок", "Лонгслив", "Свитшот толстовка", "Худи с капюшоном", "Джинсы классика",
    "Брюки чинос", "Шорты", "Платье летнее", "Юбка", "Рубашка оверсайз",
    "Куртка демисезонная", "Ветровка", "Пальто", "Леггинсы", "Спортивный костюм",
    "Поло", "Кепка", "Шапка зимняя", "Носки (5 пар)", "Ремень",
]


def make_products(rng):
    rows = []
    for i in range(N_ARTICLES):
        net_cost = int(rng.integers(150, 2500))
        price = round(net_cost * float(rng.uniform(1.6, 2.6)))
        rows.append({
            "article": f"{1000 + i}",
            "name": BASE_NAMES[i],
            "brand": "MockBrand",
            "barcode": f"4{text_digits(4600000000000 + i * 137)}",
            "net_cost": net_cost,
            "price": price,
        })
    return pd.DataFrame(rows)


def text_digits(v):
    return str(abs(int(v)))


def make_wb_sales(rng, products):
    dates = [date.today() - timedelta(days=d) for d in range(DAYS)]
    rows = []
    comm = rng.uniform(0.12, 0.20)
    storage_unit = 8.0
    for _, p in products.iterrows():
        log_unit = round(float(p["price"])) * 0.08
        for d in dates:
            qty = int(rng.poisson(2.2))
            if qty == 0:
                continue
            returns = int(rng.binomial(qty, 0.04))
            revenue = round(float(p["price"]) * qty)
            commission = -round(float(p["price"]) * qty * comm)
            logistics = -round(log_unit * qty)
            storage = -round(storage_unit * qty)
            services = -round(5 * qty)
            income = revenue + commission + logistics + storage + services
            rows.append({
                "date": d, "article": p["article"],
                "quantity": qty, "returns_qty": returns,
                "revenue": revenue, "commission": commission, "logistics": logistics,
                "storage": storage, "services": services, "income": income,
            })
    return pd.DataFrame(rows)


def make_ozon_transactions(rng, products):
    dates = [date.today() - timedelta(days=d) for d in range(DAYS)]
    rows = []
    comm = rng.uniform(0.15, 0.25)
    for _, p in products.iterrows():
        delivery_unit = round(float(p["price"])) * 0.07
        for d in dates:
            order_qty = int(rng.poisson(1.6))
            if order_qty == 0:
                continue
            delivery_to = int(rng.poisson(1.1))
            delivery_from = int(rng.binomial(delivery_to, 0.05))
            sells = delivery_to - delivery_from
            revenue = round(float(p["price"]) * order_qty)
            accruals_for_sale = round(revenue * 0.98)
            sale_commission = -round(float(p["price"]) * order_qty * comm)
            services_price = -round(30 * order_qty)
            logistics = -round(delivery_unit * delivery_to)
            income = accruals_for_sale + sale_commission + services_price + logistics
            rows.append({
                "date": d, "article": p["article"],
                "quantity": sells, "returns_qty": delivery_from,
                "revenue": accruals_for_sale, "commission": sale_commission, "logistics": logistics,
                "storage": 0, "services": services_price, "income": income,
            })
    return pd.DataFrame(rows)


def make_stocks(rng, products, code):
    dates = [date.today() - timedelta(days=d) for d in range(5)]
    rows = []
    if code == "wb":
        warehouses = ["Москва", "Казань"]
    else:
        warehouses = ["FBO Ozon"]
    for d in dates:
        for _, p in products.iterrows():
            base = int(rng.integers(5, 60))
            for wh in warehouses:
                rows.append({"date": d, "article": p["article"], "warehouse": wh, "quantity": base})
    return pd.DataFrame(rows)


def make_custom_stock(products):
    rows = []
    for _, p in products.iterrows():
        rows.append({"article": p["article"], "quantity": int(rng.integers(30, 200)),
                     "net_cost": float(p["net_cost"])})
    return pd.DataFrame(rows)


def save_csvs(*dfs):
    MOCK_DIR.mkdir(parents=True, exist_ok=True)
    for df, name in dfs:
        df.to_csv(MOCK_DIR / name, index=False, encoding="utf-8", sep=";")
    print(f"Мок-файлы сохранены в {MOCK_DIR}")


def upsert_products(db, df):
    existing = set(db.execute(select(models.Product.article)).scalars().all())
    for _, r in df.iterrows():
        if r["article"] in existing:
            rec = db.get(models.Product, r["article"])
            rec.name, rec.net_cost = r["name"], float(r["net_cost"])
        else:
            db.add(models.Product(article=r["article"], name=r["name"], brand=r["brand"],
                                  barcode=r["barcode"], net_cost=float(r["net_cost"])))
    db.commit()


def upsert_sales(db, rows, marketplace_code):
    mp_id = db.execute(select(models.Marketplace.id).where(models.Marketplace.code == marketplace_code)).scalar_one()
    values = [{**r, "marketplace_id": mp_id} for r in rows]
    ins = insert(models.Sale)
    stmt = ins.on_conflict_do_update(
        index_elements=["marketplace_id", "date", "article"],
        set_={c: ins.excluded[c] for c in
              ["quantity", "returns_qty", "revenue", "commission", "logistics",
               "storage", "services", "income"]},
    )
    db.execute(stmt, values)
    db.commit()


def upsert_stocks(db, rows, marketplace_code):
    mp_id = db.execute(select(models.Marketplace.id).where(models.Marketplace.code == marketplace_code)).scalar_one()
    values = []
    for r in rows:
        values.append({**r, "marketplace_id": mp_id})
    ins = insert(models.Stock)
    stmt = ins.on_conflict_do_update(
        index_elements=["marketplace_id", "date", "article", "warehouse"],
        set_={"quantity": ins.excluded.quantity},
    )
    db.execute(stmt, values)
    db.commit()


def upsert_custom_stock(db, df):
    insert_stmt = insert(models.CustomStock).values(
        df.to_dict("records")
    ).on_conflict_do_update(
        index_elements=["article"],
        set_={"quantity": insert(models.CustomStock).excluded.quantity,
              "net_cost": insert(models.CustomStock).excluded.net_cost},
    )
    db.execute(insert_stmt)
    db.commit()


if __name__ == "__main__":
    rng = np.random.default_rng(42)
    products = make_products(rng)
    wb_sales = make_wb_sales(rng, products)
    ozon_sales = make_ozon_transactions(rng, products)
    wb_stocks = make_stocks(rng, products, "wb")
    ozon_stocks = make_stocks(rng, products, "ozon")
    custom = make_custom_stock(products)

    save_csvs(
        (products, "products.csv"),
        (wb_sales, "wb_sales_realization.csv"),
        (ozon_sales, "ozon_transactions.csv"),
        (wb_stocks, "wb_stocks.csv"),
        (ozon_stocks, "ozon_stocks.csv"),
        (custom, "custom_stock.csv"),
    )

    with SessionLocal() as db:
        upsert_products(db, products)
        upsert_sales(db, wb_sales.to_dict("records"), "wb")
        upsert_sales(db, ozon_sales.to_dict("records"), "ozon")
        upsert_stocks(db, wb_stocks.to_dict("records"), "wb")
        upsert_stocks(db, ozon_stocks.to_dict("records"), "ozon")
        upsert_custom_stock(db, custom)

        print("sales wb:", db.execute(select(models.Sale.id).where(models.Sale.marketplace_id == 1)).all().__len__())
        print("sales ozon:", db.execute(
            select(models.Sale.id).where(models.Sale.marketplace_id == 2)).all().__len__())
        print("products:", db.execute(select(models.Product.article)).all().__len__())
        print("Готово c мок-данными.")