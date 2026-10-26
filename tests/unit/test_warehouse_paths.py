import os

import openpyxl
import pytest

from app import models
from app.services import warehouse_paths


def _cabinet(db, code="oo_joinco", schema=""):
    user = models.User(username="u-" + code, display_name="U")
    db.add(user)
    db.flush()
    company = models.Company(user_id=user.id, name="ООО Тест")
    db.add(company)
    db.flush()
    cab = models.Cabinet(
        user_id=user.id, company_id=company.id, code=code, name="Тест WB",
        schema=schema, marketplace="wb", creds="", enabled=True, position=0,
    )
    db.add(cab)
    db.flush()
    if not db.query(models.Counterparty).filter(models.Counterparty.id == 1).first():
        db.add(models.Counterparty(id=1, name="Wildberries", ctype="marketplace"))
    db.commit()
    return cab


def _xlsm(path, rows, sheet="ОПРЕДЛАРТ"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(row)
    wb.save(path)


def _folder(root, name, rows=None, filename="DB.xlsm"):
    folder = root / name
    folder.mkdir(parents=True)
    _xlsm(folder / filename, rows or [
        ["Артикул", "Штрихкод", "Размер", "Кол-во", "Куда"],
        ["A1", "111", "L", 2, "WB"],
        ["A1", "111", "L", 3, "WB"],
        ["A2", "222", "M", 4, "WB"],
        ["", "333", "S", 10, "WB"],
        ["A1", "111", "L", 0, "WB"],
    ])
    return folder


def test_settings_roundtrip_and_missing_path(db, tmp_path):
    cab = _cabinet(db)
    settings = warehouse_paths.get_settings(db, cab.id)
    assert settings["sheet"] == "ОПРЕДЛАРТ"
    assert settings["counterparty_id"] == 1

    saved = warehouse_paths.save_settings(db, cab.id, {
        "root": str(tmp_path / "нет-такого-пути"),
        "mode": "update",
        "price_mode": "zero",
        "skip_archive": False,
    })
    assert saved["mode"] == "update"
    assert warehouse_paths.get_settings(db, cab.id)["root"].endswith("нет-такого-пути")
    with pytest.raises(ValueError, match="Путь импорта не найден"):
        warehouse_paths.scan_folders(db, cab.id)


def test_settings_payload_lists_default_roots_and_ignores_extra_keys(db):
    """GET-полезная нагрузка: оба дефолтных пути + флаг доступности.

    В настройки при этом не должны попадать служебные/лишние поля.
    """
    cab = _cabinet(db)
    payload = warehouse_paths.settings_payload(db, cab.id)
    assert [r["path"] for r in payload["roots"]] == warehouse_paths.DEFAULT_ROOTS_JOIN
    assert all("exists" in r for r in payload["roots"])
    # Сохранённый путь — один из кандидатов (первый существующий или первый).
    assert payload["root"] in warehouse_paths.DEFAULT_ROOTS_JOIN
    assert isinstance(payload["root_exists"], bool)

    saved = warehouse_paths.save_settings(db, cab.id, {
        "mode": "update",
        "roots": [{"path": "подделка"}],
        "evil": "не сохранять",
    })
    assert saved["mode"] == "update"
    assert "roots" not in saved
    assert "evil" not in saved


def test_default_roots_by_cabinet_code(db, tmp_path):
    """Разные связки получают свои дефолтные пути (Джойнс / ИП Прудников)."""
    join = _cabinet(db, code="oo_joinco", schema="paths_join")
    prud = _cabinet(db, code="ip_prudnikov_wb", schema="paths_prud")
    assert warehouse_paths.default_roots(join) == warehouse_paths.DEFAULT_ROOTS_JOIN
    assert warehouse_paths.default_roots(prud) == warehouse_paths.DEFAULT_ROOTS_PRUD
    # Первый существующий кандидат становится рабочим путём по умолчанию.
    warehouse_paths.save_settings(db, join.id, {"root": str(tmp_path)})
    assert warehouse_paths.get_settings(db, join.id)["root"] == str(tmp_path)


def test_scan_and_import_folder(db, tmp_path):
    cab = _cabinet(db)
    db.add(models.Product(article="A1", name="Товар A1", net_cost=100))
    db.commit()
    warehouse_paths.save_settings(db, cab.id, {"root": str(tmp_path)})
    _folder(tmp_path, "440 - 26.05.2026 - Рязань")

    scan = warehouse_paths.scan_folders(db, cab.id)
    assert scan["summary"]["new"] == 1
    assert scan["items"][0]["status"] == "new"
    assert scan["items"][0]["date"] == "2026-05-26"
    assert scan["items"][0]["note"] == "Рязань"

    dry = warehouse_paths.import_folder(db, cab.id, "440 - 26.05.2026 - Рязань", dry_run=True)
    assert dry["items_count"] == 2
    assert dry["total"] == 500
    assert dry["unmatched"] == ["A2"]

    r = warehouse_paths.import_folder(db, cab.id, "440 - 26.05.2026 - Рязань")
    assert r["items_count"] == 2
    doc = db.query(models.WarehouseDoc).filter(models.WarehouseDoc.source == "paths").one()
    assert doc.source_ref == "oo_joinco:440 - 26.05.2026 - Рязань"
    assert doc.cabinet_id == cab.id
    assert doc.note == "Рязань"
    assert float(doc.total) == 500
    items = {it.article: it for it in doc.items}
    assert float(items["A1"].quantity) == 5
    assert items["A1"].barcode == "111"
    assert items["A1"].size == "L"
    assert float(items["A1"].price) == 100
    assert float(items["A2"].price) == 0

    assert warehouse_paths.scan_folders(db, cab.id)["summary"]["imported"] == 1
    with pytest.raises(ValueError, match="Уже импортировано"):
        warehouse_paths.import_folder(db, cab.id, "440 - 26.05.2026 - Рязань")

    # update rewrites the same document and does not create a duplicate
    r2 = warehouse_paths.import_folder(db, cab.id, "440 - 26.05.2026 - Рязань", mode="update")
    assert r2["doc_id"] == doc.id
    assert db.query(models.WarehouseDoc).count() == 1
    assert db.query(models.WarehouseDocItem).count() == 2


def test_scan_skips_archive_and_reports_bad_and_missing(db, tmp_path):
    cab = _cabinet(db)
    warehouse_paths.save_settings(db, cab.id, {"root": str(tmp_path)})
    _folder(tmp_path, "321 - 10.01.2023 - без файла")
    (tmp_path / "321 - 10.01.2023 - без файла" / "DB.xlsm").unlink()
    _folder(tmp_path, "АРХИВ старые")
    _folder(tmp_path, "1 - 10.01.23 - короткий год")
    _folder(tmp_path, "2 - 10.01.202 - плохой год")
    _folder(tmp_path, "3 - 32.01.2026 - плохая дата")

    scan = warehouse_paths.scan_folders(db, cab.id)
    by = {it["folder"]: it for it in scan["items"]}
    assert by["321 - 10.01.2023 - без файла"]["status"] == "no_file"
    assert by["1 - 10.01.23 - короткий год"]["status"] == "new"
    assert by["1 - 10.01.23 - короткий год"]["date"] == "2023-01-10"
    assert by["2 - 10.01.202 - плохой год"]["status"] == "bad_name"
    assert by["3 - 32.01.2026 - плохая дата"]["status"] == "bad_name"
    assert "АРХИВ старые" not in by


def test_multiple_db_files_newest_wins(db, tmp_path):
    cab = _cabinet(db)
    warehouse_paths.save_settings(db, cab.id, {"root": str(tmp_path)})
    folder = _folder(tmp_path, "392 - 01.02.2026 - два файла", filename="DB1.xlsm")
    old = folder / "DB1.xlsm"
    os.utime(old, (1000, 1000))
    _xlsm(folder / "DB2.xlsm", [
        ["Артикул", "Кол-во"],
        ["A1", 1],
    ])
    os.utime(folder / "DB2.xlsm", (2000, 2000))

    item = warehouse_paths.scan_folders(db, cab.id)["items"][0]
    assert item["file"] == "DB2.xlsm"
    assert "DB1.xlsm" in item["warning"]
