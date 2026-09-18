"""T-17: схема каталога «Наш склад → Товары».

Колонки products (subject/volume_l/composition) и таблицы
product_sizes / product_aliases создаются миграцией идемпотентно.
"""
from sqlalchemy import inspect, text

from app import models
from scripts import init_db


def test_catalog_schema_tables_and_columns(db, db_engine):
    insp = inspect(db_engine)

    prod_cols = {c["name"] for c in insp.get_columns("products")}
    assert {"subject", "volume_l", "composition"} <= prod_cols

    size_cols = {c["name"] for c in insp.get_columns("product_sizes")}
    assert {"id", "article", "size", "barcode"} <= size_cols
    uniq_cols = {
        tuple(c["column_names"]) for c in insp.get_unique_constraints("product_sizes")
    }
    assert ("article", "size") in uniq_cols
    idx_cols = {tuple(c["column_names"]) for c in insp.get_indexes("product_sizes")}
    assert ("barcode",) in idx_cols

    alias_cols = {c["name"] for c in insp.get_columns("product_aliases")}
    assert {"alias_article", "article", "updated_at"} <= alias_cols

    fk_targets = {fk["referred_table"] for fk in insp.get_foreign_keys("product_sizes")}
    assert fk_targets == {"products"}
    al_fk = {fk["referred_table"] for fk in insp.get_foreign_keys("product_aliases")}
    assert al_fk == {"products"}


def test_catalog_migration_idempotent(db, db_engine):
    """Повторный прогон create_tables() не падает и не меняет схему."""
    before = {c["name"] for c in inspect(db_engine).get_columns("products")}
    init_db.create_tables()
    after = {c["name"] for c in inspect(db_engine).get_columns("products")}
    assert before == after


def test_size_cascade_delete(db, db_engine):
    """Удаление товара каскадом убирает его размеры и алиасы."""
    prod = models.Product(article="A1", name="Товар")
    db.add(prod)
    db.flush()
    db.add(models.ProductSize(article="A1", size="42", barcode="2000000001"))
    db.add(models.ProductAlias(alias_article="A1-old", article="A1"))
    db.commit()

    prod = db.get(models.Product, "A1")
    db.delete(prod)
    db.commit()

    remaining = db.execute(text(
        "SELECT count(*) FROM product_sizes WHERE article = 'A1'"
    )).scalar()
    aliases = db.execute(text(
        "SELECT count(*) FROM product_aliases WHERE article = 'A1'"
    )).scalar()
    assert remaining == 0
    assert aliases == 0