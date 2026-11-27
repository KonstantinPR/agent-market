# -*- coding: utf-8 -*-
from datetime import date
from pathlib import Path

import pandas as pd

from app import models
from app.services import packing, storage_settings


def _setup_stock(db, tmp_path, monkeypatch):
    root = tmp_path / "stock"
    root.mkdir()
    monkeypatch.setattr(storage_settings, "stock_root", lambda db=None: str(root))
    monkeypatch.setattr(packing, "STOCK_FILE_NAME", "OSTATKI.xlsx")
    monkeypatch.setattr(packing, "ORIGINAL_DIR_NAME", "ORIGINAL")
    df = pd.DataFrame([
        {"Артикул": "A1", "Размер": "42", "Штрихкод": "111", "Остаток": 5, "Наименование": "Товар A1"},
        {"Артикул": "A1", "Размер": "44", "Штрихкод": "222", "Остаток": 1, "Наименование": "Товар A1"},
    ])
    path = root / "OSTATKI.xlsx"
    df.to_excel(path, index=False)
    return root, path


def test_stock_column_aliases_kolvo_sht():
    df = pd.DataFrame([{"Артикул товара": "A1", "Размер": "42", "Кол-во шт.": 3}])
    col = packing._find_col(df, packing._STOCK_COLS["qty"])
    assert col == "Кол-во шт."


def test_packing_draft_commit_revert(db, tmp_path, monkeypatch):
    root, stock_path = _setup_stock(db, tmp_path, monkeypatch)

    def fake_replenish(*args, **kwargs):
        return {"rows": [
            {"article": "A1", "name": "Товар A1", "size": "42", "barcode": "111", "to_sort": 3},
            {"article": "A1", "name": "Товар A1", "size": "44", "barcode": "222", "to_sort": 2},
        ], "meta": {}}

    monkeypatch.setattr(packing.replenish_service, "replenish_rows", fake_replenish)

    draft = packing.build_draft(db, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31))
    assert draft["count"] == 2
    by_size = {i["size"]: i for i in draft["items"]}
    assert by_size["42"]["need_qty"] == 3
    assert by_size["42"]["available_qty"] == 5
    assert by_size["42"]["pack_qty"] == 3
    assert by_size["44"]["available_qty"] == 1
    assert by_size["44"]["pack_qty"] == 1

    res = packing.commit_draft(db, draft)
    assert res["ok"] is True
    assert Path(res["backup_file"]).exists()
    assert Path(res["packing_file"]).exists()
    assert (root / "ORIGINAL").exists()

    after = pd.read_excel(stock_path)
    qty = {str(r["Размер"]): int(r["Остаток"]) for _, r in after.iterrows()}
    assert qty["42"] == 2
    assert qty["44"] == 0

    hist = packing.list_packing_lists(db)
    assert len(hist) == 1
    assert hist[0]["status"] == "committed"

    rev = packing.revert_packing_list(db, res["id"])
    assert rev["ok"] is True
    restored = pd.read_excel(stock_path)
    qty2 = {str(r["Размер"]): int(r["Остаток"]) for _, r in restored.iterrows()}
    assert qty2["42"] == 5
    assert qty2["44"] == 1
    pl = db.get(models.PackingList, res["id"])
    assert pl.status == "reverted"


def test_packing_revert_blocked_when_stock_changed(db, tmp_path, monkeypatch):
    root, stock_path = _setup_stock(db, tmp_path, monkeypatch)
    monkeypatch.setattr(packing.replenish_service, "replenish_rows", lambda *a, **k: {"rows": [
        {"article": "A1", "name": "Товар A1", "size": "42", "barcode": "111", "to_sort": 1},
    ], "meta": {}})
    draft = packing.build_draft(db, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31))
    res = packing.commit_draft(db, draft)
    df = pd.read_excel(stock_path)
    df.loc[df["Размер"] == 42, "Остаток"] = 99
    df.to_excel(stock_path, index=False)
    try:
        packing.revert_packing_list(db, res["id"])
        assert False, "expected PackingError"
    except packing.PackingError as e:
        assert "изменился" in str(e).lower()