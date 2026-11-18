# -*- coding: utf-8 -*-
"""T-82: каталог себестоимости из общего файла products.xlsx."""
from pathlib import Path

import pandas as pd
import pytest

from app import models
from app.services import products_file as pf
from app.services import storage_settings as ss


def _write_xlsx(path: Path, rows: list[dict]) -> Path:
    pd.DataFrame(rows).to_excel(path, index=False)
    return path


def test_read_frame_maps_columns_and_filters_zero_cost(tmp_path):
    """pure_value → value, «Поставщик» → supplier; строки с cost ≤ 0 отбрасываются."""
    p = _write_xlsx(tmp_path / "products.xlsx", [
        {"article": "A1", "net_cost": 100, "pure_value": 250, "Поставщик": "ООО Ромашка"},
        {"article": "A2", "net_cost": 0, "pure_value": 300, "Поставщик": "ИП Тест"},
        {"article": "A3", "net_cost": None, "pure_value": None, "Поставщик": None},
        {"article": " A1 ", "net_cost": 120, "pure_value": 260, "Поставщик": "ООО Ромашка"},
    ])
    df = pf.read_frame(p)
    # A1 дублируется — остаётся последняя строка; нули и пустые отброшены.
    assert list(df["article"]) == ["A1"]
    assert df.iloc[0]["net_cost"] == 120
    assert df.iloc[0]["value"] == 260
    assert df.iloc[0]["supplier"] == "ООО Ромашка"


def test_read_frame_requires_cost_columns(tmp_path):
    p = _write_xlsx(tmp_path / "bad.xlsx", [{"article": "A1", "price": 100}])
    with pytest.raises(ValueError):
        pf.read_frame(p)


def test_sync_updates_catalog_without_wiping_zeroes(db, tmp_path):
    """Синхронизация пишет cost/value/supplier, но нули файла не затирают каталог."""
    db.add(models.Product(article="A1", name="Товар 1", net_cost=0, value=0))
    db.add(models.Product(article="A2", name="Товар 2", net_cost=50, value=70,
                          supplier="Старый"))
    db.commit()
    p = _write_xlsx(tmp_path / "products.xlsx", [
        {"article": "A1", "net_cost": 350, "pure_value": 700, "Поставщик": "ИП Новый"},
        {"article": "A2", "net_cost": 0, "pure_value": 0, "Поставщик": None},
    ])
    ss.save(db, {"products_file": str(p)})

    st = pf.sync(db, force=True)
    assert st["error"] is None
    assert st["rows"] == 1  # A2 отброшен: себестоимость 0
    assert st["updated"] == 1
    assert st["exists"] is True

    a1 = db.get(models.Product, "A1")
    db.refresh(a1)
    assert float(a1.net_cost) == pytest.approx(350.0)
    assert float(a1.value) == pytest.approx(700.0)
    assert a1.supplier == "ИП Новый"
    assert a1.name == "Товар 1"  # имя из файла не перетирается

    a2 = db.get(models.Product, "A2")
    db.refresh(a2)
    assert float(a2.net_cost) == pytest.approx(50.0)
    assert float(a2.value) == pytest.approx(70.0)
    assert a2.supplier == "Старый"


def test_sync_skips_unchanged_file(db, tmp_path, monkeypatch):
    """Повторная синхронизация без изменений файла ничего не пишет."""
    db.add(models.Product(article="A1", name="Товар", net_cost=0, value=0))
    db.commit()
    p = _write_xlsx(tmp_path / "products.xlsx",
                    [{"article": "A1", "net_cost": 100, "pure_value": 200}])
    ss.save(db, {"products_file": str(p)})
    assert pf.sync(db, force=True)["updated"] == 1

    calls = []
    monkeypatch.setattr(pf, "storage_upsert",
                        lambda db_, df_: calls.append(len(df_)) or len(df_))
    st = pf.sync(db, force=False)
    assert calls == []           # файл не менялся — upsert не вызывался
    assert st["rows"] == 1


def test_sync_reports_missing_file(db, tmp_path):
    ss.save(db, {"products_file": str(tmp_path / "нет-файла.xlsx")})
    st = pf.sync(db, force=True)
    assert st["exists"] is False
    assert "не найден" in (st["error"] or "")


def test_products_file_default_follows_analytics_root(db):
    """Дефолт файла каталога — <корень аналитики>/ОБЩИЕ/products.xlsx."""
    data = ss.public_payload(db)
    row = data["roots"]["products_file"]
    assert row["kind"] == "file"
    assert row["value"].endswith("products.xlsx")
    assert "products_sync" in data

    new_root = r"D:\Диск"
    ss.save(db, {"analytics_root": new_root})
    expected = str(Path(new_root) / "ОБЩИЕ" / "products.xlsx")
    assert ss.products_file(db) == expected
    assert ss.public_payload(db)["roots"]["products_file"]["default"] == expected

    # Явное переопределение файла живёт отдельно от корня аналитики.
    ss.save(db, {"products_file": r"D:\Каталог\costs.xlsx"})
    assert ss.products_file(db) == r"D:\Каталог\costs.xlsx"
    ss.reset(db, "products_file")
    assert ss.products_file(db) == expected
