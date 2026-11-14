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
                     "price": None, "discount": None, "in_action": None}]
    assert "plan" in meta["columns"]


# Реальная шапка выгрузки ЛК («Цены и скидки» → акция → «Обновить через Excel»).
LK_HEADER = (
    "Товар уже участвует в акции", "Бренд", "Предмет", "Наименование",
    "Артикул поставщика", "Артикул WB", "Последний баркод",
    "Плановая цена для акции", "Текущая розничная цена", "Валюта",
    "Текущая скидка на сайте, %", "Загружаемая скидка для участия в акции",
    "Статус",
)


def test_parse_limits_real_lk_header():
    """Реальная шапка ЛК: nm берётся из «Артикул WB», а не «Артикул поставщика»."""
    body = [list(LK_HEADER)]
    body.extend([
        ["Нет", "Elena Chezelle", "Жакеты", "Жакет-полупальто", "SH034D",
         6626002, "2012530802021", 5147, 6050, "RUB", 0, "15",
         "Не участвует: не добавлен или цена выше плановой"],
        ["Да", "Elena Chezelle", "Жакеты", "Жакет", "SH097D",
         6626023, "2000036947582", 5098, 7040, "RUB", 29, "29",
         "Участвует: добавлен, цена равна или ниже плановой"],
        ["Да", "Elena Chezelle", "Жакеты", "Жакет", "SH099D",
         6626024, "2000036947583", 5100, 6900, "RUB", 26, "26",
         "Участвует: добавлен, цена равна или ниже плановой"],
    ])
    rows, meta = pl.parse_limits(_xlsx(body))
    got = {r["nm_id"]: r for r in rows}
    assert set(got) == {"6626002", "6626023", "6626024"}
    assert got["6626002"]["plan_price"] == 5147.0
    assert got["6626002"]["price"] == 6050.0
    assert got["6626023"]["discount"] == 29.0
    assert got["6626023"]["in_action"] is True
    assert got["6626002"]["in_action"] is False
    assert meta["parsed"] == 3
    assert meta["skipped"] == 0
    assert meta["in_action"] == 2
    assert meta["columns"]["nm"] == "Артикул WB"
    assert meta["columns"]["discount"] == "Текущая скидка на сайте, %"
    assert meta["columns"]["in_action"] == "Товар уже участвует в акции"


def test_parse_limits_blocked_autodiscount_is_not_discount():
    """В автоскидках «Минимальная цена…» и «Блокировка…» не являются скидкой."""
    header = list(LK_HEADER[:11]) + [
        "Минимальная цена для применения скидки по автоакции",
        "Блокировка применения скидки по автоакции",
        "Сколько дней осталось до конца акции",
    ] + list(LK_HEADER[11:])
    body = [header]
    body.append([
        "Нет", "Бренд", "Предмет", "Товар", "ART1", 7000001, "2000000000001",
        1000, 1300, "RUB", 12, 500, "Да", 5, "15",
        "Не участвует: стоит блокировка на снижение цены",
    ])
    rows, meta = pl.parse_limits(_xlsx(body))
    assert rows[0]["discount"] == 12.0
    assert "минимальн" not in meta["columns"]["discount"].lower()
    assert meta["columns"]["in_action"] == "Товар уже участвует в акции"


def test_parse_limits_status_fallback_and_size_union():
    """Без колонки «Товар уже участвует…» участие — из «Статус»; размеры объединяются."""
    header = ("Артикул WB", "Наименование", "Плановая цена для акции", "Статус")
    body = [list(header)]
    body.extend([
        [900001, "Товар, 42", 1000, "Не участвует: не добавлен или цена выше плановой"],
        [900001, "Товар, 44", 1100, "Участвует: добавлен, цена равна или ниже плановой"],
        [900002, "Другой", 1200, "Не участвует: стоит блокировка на снижение цены"],
    ])
    rows, meta = pl.parse_limits(_xlsx(body))
    got = {r["nm_id"]: r for r in rows}
    assert got["900001"]["plan_price"] == 1100.0
    assert got["900001"]["in_action"] is True
    assert got["900002"]["in_action"] is False
    assert meta["columns"]["status"] == "Статус"


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


def test_import_limits_stores_in_action(db):
    """Участие из файла ЛК сохраняется и отдаётся в limits_map."""
    body = [list(LK_HEADER)]
    body.extend([
        ["Да", "B", "P", "Участвует", "A1", 7000001, "2000000000001",
         1000, 1300, "RUB", 12, "15", "Участвует: добавлен, цена равна или ниже плановой"],
        ["Нет", "B", "P", "Не участвует", "A2", 7000002, "2000000000002",
         1100, 1400, "RUB", 0, "15", "Не участвует: не добавлен или цена выше плановой"],
    ])
    meta = pl.import_limits(db, _xlsx(body), "Чёрная пятница (автоакция).xlsx",
                            promo_id=3148)
    assert meta["in_action"] == 1
    limits = pl.limits_map(db, expire_days=30)
    assert limits["7000001"][0]["in_action"] is True
    assert limits["7000002"][0]["in_action"] is False


def test_infer_promo_id_from_filename(db):
    now = datetime.utcnow()
    db.add(models.Promotion(
        promo_id=2766, name="Хиты осени", adv_type="regular", description="",
        starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5)))
    db.commit()
    assert pl.infer_promo_id(db, "2766.xlsx") == 2766
    assert pl.infer_promo_id(db, "выгрузка Хиты осени.xlsx") == 2766
    assert pl.infer_promo_id(db, "неизвестно.xlsx") == 0


def test_infer_promo_id_real_lk_file_names(db):
    """Реальные имена выгрузок ЛК: «- 2» не путается с базовой, повторные
    названия разных месяцев разводятся по активности акции."""
    now = datetime.utcnow()
    past = (now - timedelta(days=40), now - timedelta(days=1))
    live = (now - timedelta(days=1), now + timedelta(days=5))
    for pid, name, dates in [
        (3048, "Скидки на немаркированные товары (автоматические скидки)", past),
        (3174, "Скидки на немаркированные товары (автоматические скидки)", live),
        (3148, "Чёрная пятница (автоакция)", live),
        (3150, "Чёрная пятница - 2 (автоакция)", live),
    ]:
        db.add(models.Promotion(
            promo_id=pid, name=name, adv_type="auto", description="",
            starts_at=dates[0], ends_at=dates[1]))
    db.commit()
    assert pl.infer_promo_id(
        db, "Все товары подходящие для акции_Чёрная пятница (автоакция)_"
            "13.11.2026 17.18.30.xlsx") == 3148
    assert pl.infer_promo_id(
        db, "Все товары подходящие для акции_Чёрная пятница - 2 (автоакция)_"
            "13.11.2026 17.18.24.xlsx") == 3150
    assert pl.infer_promo_id(
        db, "Товары для исключения из акции_Скидки на немаркированные товары "
            "(автоматические скидки)_13.11.2026 17.18.09.xlsx") == 3174
