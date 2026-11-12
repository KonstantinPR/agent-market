# -*- coding: utf-8 -*-
"""Ежедневная выгрузка аналитики на диск: набор WB и архив версий."""
import json
from pathlib import Path

from app.services import analytics_disk

ROOT = Path(__file__).resolve().parents[2]


def test_wb_reports_has_six_named_files():
    """WB-набор — 6 файлов: воронка, детализация, остатки, хранение,
    карточки, группы (порядок тоже важен — так их ждёт пользователь)."""
    names = [r[0] for r in analytics_disk.WB_REPORTS]
    assert names == [
        "margin_funnel.xlsx",
        "margin_detail.xlsx",
        "margin_stocks.xlsx",
        "margin_storage.xlsx",
        "margin_cards.xlsx",
        "margin_groups.xlsx",
    ]


def test_wb_reports_export_dicts_exist():
    """Каждый файл ссылается на живой словарь колонок columns.json."""
    doc = json.loads((ROOT / "app" / "static" / "columns.json")
                     .read_text(encoding="utf-8"))
    for _fname, _sheet, export_name, _fn in analytics_disk.WB_REPORTS:
        assert export_name in doc["dicts"], export_name


def test_archive_existing_moves_and_overwrites(tmp_path):
    """Старый файл уходит в ARCHIVE с суффиксом _archive; следующий запуск
    перезаписывает архив, а не копит историю."""
    folder = tmp_path / "WB"
    folder.mkdir()
    (folder / "margin_detail.xlsx").write_bytes(b"old")

    dest = analytics_disk._archive_existing(folder, "margin_detail.xlsx")
    assert dest == folder / "ARCHIVE" / "margin_detail_archive.xlsx"
    assert not (folder / "margin_detail.xlsx").exists()
    assert dest.read_bytes() == b"old"

    (folder / "margin_detail.xlsx").write_bytes(b"new")
    dest2 = analytics_disk._archive_existing(folder, "margin_detail.xlsx")
    assert dest2 == dest
    assert dest.read_bytes() == b"new"
    assert len(list((folder / "ARCHIVE").iterdir())) == 1


def test_archive_existing_noop_when_missing(tmp_path):
    assert analytics_disk._archive_existing(tmp_path, "nope.xlsx") is None
    assert not (tmp_path / "ARCHIVE").exists()


def test_write_reports_six_files_and_archive_rotation(db, tmp_path):
    """Полный прогон: 6 файлов WB, второй запуск кладёт прежние в ARCHIVE."""
    from datetime import date
    from types import SimpleNamespace

    from app import models

    from app.services import cabinets as cabinet_service

    user = cabinet_service.seed_user(db)
    company = models.Company(user_id=user.id, name="Фирма",
                             analytics_dir="firm")
    db.add(company)
    db.commit()
    db.add_all([
        models.Product(article="A1", name="Товар", net_cost=100.0),
        models.WbDetailRow(op_key="sr:1", source="excel", article="A1",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 4),
                           quantity=1, retail_amount=1000.0, for_pay=900.0),
        models.Stock(marketplace_id=1, date=date(2026, 9, 4), article="A1",
                     warehouse="WH1", chrt_id="1", quantity=5,
                     quantity_full=5, in_way=0),
        models.StorageCost(nm_id="1", article="A1", barcodes_count=1,
                           volume=1.0, storage_price=10.0,
                           warehouse_price=5.0),
        models.MarketplaceCard(marketplace_id=1, chrt_id="1", nm_id="1",
                               vendor_code="A1", barcode="2000000000001",
                               brand="Бренд", subject="Куртки", size="",
                               name="Товар", composition="Хлопок"),
    ])
    db.commit()

    info = SimpleNamespace(id=1, name="Связка", marketplace="wb",
                           company_id=company.id)
    res = analytics_disk.write_reports(
        db, info, date(2026, 9, 1), date(2026, 9, 10), root=str(tmp_path))
    assert res["files"] == [r[0] for r in analytics_disk.WB_REPORTS]
    folder = tmp_path / "firm" / "WB"
    for fname in res["files"]:
        assert (folder / fname).exists(), fname

    # Повторный прогон: прежние версии переехали в ARCHIVE (по одному на файл).
    analytics_disk.write_reports(
        db, info, date(2026, 9, 11), date(2026, 9, 20), root=str(tmp_path))
    archived = sorted(p.name for p in (folder / "ARCHIVE").iterdir())
    assert archived == sorted(
        Path(f).stem + "_archive" + Path(f).suffix for f in res["files"])
