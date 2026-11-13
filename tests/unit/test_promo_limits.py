# -*- coding: utf-8 -*-
"""T-76: пороги участия WB («до N ₽») — разбор Excel ЛК, хранение, выборка.

Публичный API автоакций порогов не отдаёт (422), поэтому единственный
источник — Excel из ЛК. Парсер эвристический: ищет строку заголовков по
подписям и роли колонок, поддерживает ручной `column_map`.
"""
from datetime import datetime, timedelta
from io import BytesIO

import pandas as pd
import pytest

from app import models
from app.services import promo_limits as pl


def _xlsx(rows) -> bytes:
    buf = BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, header=False)
    return buf.getvalue()


def _table(rows, header=("Артикул WB", "Наименование", "Плановая цена", "Скидка, %")):
    body = [list(header)]
    body.extend(rows)
    return _xlsx(body)


def test_parse_limits_finds_header_and_collapses_sizes():
    """Шапка не в первой строке; дубли nm схлопываются по максимальному порогу."""
    data = _xlsx([
        ["Отчёт по акции", None, None, None],
        ["Артикул WB", "Наименование", "Плановая цена", "Скидка, %"],
        [1099756595, "Товар, размер 42", "1 388,00", "12,7"],
        [1099756595, "Товар, размер 44", "1 450,00", "8,8"],
        [1099756596, "Другой товар", 1299, 18],
        ["Итого", None, None, None],
    ])
    rows, meta = pl.parse_limits(data)
    got = {r["nm_id"]: r["plan_price"] for r in rows}
    assert got == {"1099756595": 1450.0, "1099756596": 1299.0}
    assert meta["header_row"] == 2
    assert meta["parsed"] == 2
    assert "nm" in meta["columns"] and "plan" in meta["columns"]


def test_parse_limits_single_price_column_is_plan():
    """В файле автоакций одна «Цена» — это и есть плановая цена участия."""
    data = _table([[123456, "Товар", "1 299,00"]],
                  header=("Артикул WB", "Наименование", "Цена"))
    rows, meta = pl.parse_limits(data)
    assert rows == [{"nm_id": "123456", "plan_price": 1299.0,
                     "price": None, "discount": None}]
    assert "plan" in meta["columns"]


def test_parse_limits_column_map_override():
    """Нестандартные подписи: роли задаются вручную через column_map."""
    data = _table([[555666, "1 388,00"]],
                  header=("Код", "Цена с учётом акции"))
    with pytest.raises(ValueError):
        pl.parse_limits(data)
    rows, _meta = pl.parse_limits(
        data, column_map={"nm": "Код", "plan": "Цена с учётом акции"})
    assert rows[0]["nm_id"] == "555666"
    assert rows[0]["plan_price"] == 1388.0


def test_parse_limits_requires_article_column():
    data = _table([["Товар", "1 388,00"]],
                  header=("Наименование", "Плановая цена"))
    with pytest.raises(ValueError):
        pl.parse_limits(data)


def test_limits_map_skips_expired_and_merges_api(db):
    """Excel-строки старше срока не применяются; API regular-акций добавляется."""
    now = datetime.utcnow()
    db.add(models.PromoLimit(promo_id=1, nm_id="100", plan_price=1388.0,
                             source="excel", fetched_at=now))
    db.add(models.PromoLimit(promo_id=2, nm_id="100", plan_price=1500.0,
                             source="excel", fetched_at=now - timedelta(days=45)))
    db.add(models.PromoLimit(promo_id=3, nm_id="200", plan_price=999.0,
                             source="excel", fetched_at=now))
    db.add(models.Promotion(
        promo_id=77, name="ЛЕТО", adv_type="regular", description="",
        starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5),
        participation_percent=5.0, ranging_json=""))
    db.add(models.PromoNomenclature(
        promo_id=77, nm_id="200", in_action=True, plan_price=1200.0,
        price=1300.0, discount=0.0, plan_discount=7.0))
    db.commit()

    limits = pl.limits_map(db, expire_days=30)
    assert [i["plan_price"] for i in limits["100"]] == [1388.0]
    assert sorted(i["plan_price"] for i in limits["200"]) == [999.0, 1200.0]
    sources = {i["source"] for i in limits["200"]}
    assert sources == {"excel", "api"}


def test_import_limits_upserts_and_drops_stale_rows(db):
    """Повторный импорт обновляет строки и удаляет исчезнувшие из файла."""
    data = _table([[111111, "Товар", 1000], [222222, "Второй", 1200]])
    meta = pl.import_limits(db, data, "promo_2766.xlsx", promo_id=2766)
    assert meta["imported"] == 2
    assert meta["promo_id"] == 2766

    data2 = _table([[111111, "Товар", 1100]])
    meta2 = pl.import_limits(db, data2, "promo_2766.xlsx", promo_id=2766)
    assert meta2["imported"] == 1
    rows = db.query(models.PromoLimit).order_by(models.PromoLimit.nm_id).all()
    assert [(r.nm_id, float(r.plan_price)) for r in rows] == [("111111", 1100.0)]
    summary = pl.summary(db)
    assert summary["total_products"] == 1
    assert summary["promotions"][0]["promo_id"] == 2766


def test_infer_promo_id_from_filename(db):
    now = datetime.utcnow()
    db.add(models.Promotion(
        promo_id=2766, name="Хиты осени", adv_type="regular", description="",
        starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5)))
    db.commit()
    assert pl.infer_promo_id(db, "2766.xlsx") == 2766
    assert pl.infer_promo_id(db, "выгрузка Хиты осени.xlsx") == 2766
    assert pl.infer_promo_id(db, "неизвестно.xlsx") == 0
