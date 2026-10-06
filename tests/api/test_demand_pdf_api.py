"""API-тесты PDF-выгрузки потребности (/api/export/replenish/pdf).

Индекс фотографий и миниатюры подменяются на временный каталог, поэтому тесты
не ходят по диску `C:\YandexDisk\ФОТОГРАФИИ`.
"""
import io
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from app import models
from app.api import PDF_DEFAULT_LIMIT, PDF_MAX_LIMIT, PDF_MEDIA
from app.services import photos as photos_service
from app.services.replenish import WB_SORT_VELOCITY_DAYS

TODAY = date(2026, 9, 20)
D0 = TODAY - timedelta(days=29)


def _jpeg(seed: int = 0) -> bytes:
    im = Image.new("RGB", (80, 100), ((seed * 61) % 256, (seed * 97) % 256, (seed * 151) % 256))
    dr = ImageDraw.Draw(im)
    dr.rectangle([0, 0, 39, 49], fill=(255, 255, 255))
    b = io.BytesIO()
    im.save(b, format="JPEG", quality=80)
    return b.getvalue()


def _mp_id(db, code):
    return db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == code)
    ).scalar_one()


@pytest.fixture()
def pdf_api(client, db):
    """TestClient с одним товаром, которому нужно докупить 10 шт."""

    def _add(article, sold=60, stock=50, sizes=("42",)):
        db.add(models.Product(article=article, name="Платье " + article,
                              net_cost=100.0, replenishable=True))
        db.add(models.CustomStock(article=article, quantity=stock, net_cost=100.0))
        db.add(models.MarketplaceCard(
            marketplace_id=_mp_id(db, "wb"), chrt_id="c:" + article,
            vendor_code=article, nm_id="", barcode="",
        ))
        for sz in sizes:
            db.add(models.ProductSize(article=article, size=sz))
        db.add(models.WbDetailRow(
            op_key=article, source="excel", article=article, doc_type_name="Продажа",
            sale_dt=TODAY, quantity=sold, retail_amount=1000.0 * sold,
            for_pay=900.0 * sold,
        ))

    _add("PDF-ART-1", sold=60, stock=50)
    db.commit()
    yield client


@pytest.fixture()
def photo_tree(tmp_path, monkeypatch):
    """Временное дерево фото + заглушка миниатюр."""
    root = tmp_path / "photos"
    for folder in ("ДЖИНСЫ/Часть 1", "АРХИВ/старые"):
        for n in (1, 2, 3, 4):
            p = root / folder / f"PDF-ART-1-{n}.JPG"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(_jpeg(n))
    idx = photos_service.PhotoIndex(root=root)
    monkeypatch.setattr(photos_service, "get_index", lambda: idx)

    from app.services import thumbs as thumbs_service
    monkeypatch.setattr(
        thumbs_service, "thumb_bytes",
        lambda path, max_px=None, quality=None: _jpeg(abs(hash(str(path))) % 90 + 1),
    )
    return root


def test_pdf_returns_pdf_media_type(pdf_api):
    r = pdf_api.get("/api/export/replenish/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == PDF_MEDIA
    assert r.content.startswith(b"%PDF")
    assert r.content.rstrip().endswith(b"%%EOF")


def test_pdf_headers_report_counts(pdf_api):
    r = pdf_api.get("/api/export/replenish/pdf")
    assert r.headers["X-Count"] == "1"
    assert r.headers["X-Truncated"] == "0"
    assert r.headers["X-Photo-Hits"] == "0"


def test_content_disposition_has_filename(pdf_api):
    r = pdf_api.get("/api/export/replenish/pdf")
    disp = r.headers["content-disposition"]
    assert disp.startswith("attachment;")
    assert ".pdf" in disp and "potrebnost_" in disp


def test_limit_truncates_and_reports(pdf_api, db):
    for i in range(2, 7):
        db.add(models.Product(article=f"PDF-ART-{i}", name="x",
                              net_cost=100.0, replenishable=True))
        db.add(models.CustomStock(article=f"PDF-ART-{i}", quantity=1, net_cost=100.0))
        db.add(models.MarketplaceCard(
            marketplace_id=_mp_id(db, "wb"), chrt_id=f"c:{i}",
            vendor_code=f"PDF-ART-{i}", nm_id="", barcode="",
        ))
        db.add(models.WbDetailRow(
            op_key=f"PDF-ART-{i}", source="excel", article=f"PDF-ART-{i}",
            doc_type_name="Продажа", sale_dt=TODAY, quantity=60,
            retail_amount=60000.0, for_pay=54000.0,
        ))
    db.commit()

    r = pdf_api.get("/api/export/replenish/pdf", params={"limit": 2})
    assert r.headers["X-Count"] == "2"
    assert int(r.headers["X-Truncated"]) >= 1


def test_limit_is_clamped_to_max(pdf_api):
    r = pdf_api.get("/api/export/replenish/pdf", params={"limit": PDF_MAX_LIMIT + 999})
    assert r.status_code == 200
    assert int(r.headers["X-Count"]) <= PDF_MAX_LIMIT


def test_limit_zero_falls_back_to_one(pdf_api):
    r = pdf_api.get("/api/export/replenish/pdf", params={"limit": 0})
    assert r.headers["X-Count"] == "1"


def test_default_limit_header_is_default(pdf_api):
    r = pdf_api.get("/api/export/replenish/pdf")
    assert r.headers["X-Count"] <= str(PDF_DEFAULT_LIMIT)


def test_unknown_cols_are_ignored(pdf_api):
    r = pdf_api.get("/api/export/replenish/pdf", params={"cols": "нет-такой,тоже-нет"})
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")


def test_article_like_filter_applies(pdf_api):
    r = pdf_api.get("/api/export/replenish/pdf", params={"article_like": "НЕТ-ТАКОГО"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "0"
    assert r.content.startswith(b"%PDF")      # пустой отчёт всё равно валиден


def test_no_rows_still_returns_pdf(pdf_api):
    r = pdf_api.get("/api/export/replenish/pdf", params={"article_like": "ZZZ"})
    assert r.headers["X-Count"] == "0"
    assert r.headers["X-Photo-Hits"] == "0"
    assert r.content.rstrip().endswith(b"%%EOF")


# ------------------------------------------------------------------ фото

def test_photos_are_attached_when_root_exists(pdf_api, photo_tree):
    r = pdf_api.get("/api/export/replenish/pdf",
                    params={"with_photos": 1, "photo_count": 4})
    assert r.headers["X-Photo-Hits"] == "1"
    assert len(r.content) > 20_000


def test_photo_count_limits_images(pdf_api, photo_tree):
    small = pdf_api.get("/api/export/replenish/pdf",
                        params={"with_photos": 1, "photo_count": 1})
    big = pdf_api.get("/api/export/replenish/pdf",
                      params={"with_photos": 1, "photo_count": 4})
    assert small.headers["X-Photo-Hits"] == "1"
    assert len(small.content) < len(big.content)


def test_photos_disabled_ignores_missing_root(pdf_api, monkeypatch):
    from app.services import thumbs as thumbs_service
    empty = photos_service.PhotoIndex(root=Path("Z:/нет-такой-папки"))
    monkeypatch.setattr(photos_service, "get_index", lambda: empty)
    monkeypatch.setattr(
        thumbs_service, "thumb_bytes",
        lambda *a, **k: pytest.fail("миниатюры не должны строиться"),
    )
    r = pdf_api.get("/api/export/replenish/pdf", params={"with_photos": 0})
    assert r.status_code == 200
    assert r.headers["X-Photo-Hits"] == "0"


def test_missing_root_with_photos_is_400(pdf_api, monkeypatch):
    missing = photos_service.PhotoIndex(root=Path("Z:/нет-такой-папки"))
    monkeypatch.setattr(photos_service, "get_index", lambda: missing)
    r = pdf_api.get("/api/export/replenish/pdf", params={"with_photos": 1})
    assert r.status_code == 400
    assert "PHOTOS_ROOT" in r.json()["detail"]


def test_article_without_photos_still_included(pdf_api, photo_tree, db):
    """Товар без фото не выбрасывается из выгрузки."""
    db.add(models.Product(article="БЕЗ-ФОТО", name="x", net_cost=100.0, replenishable=True))
    db.add(models.CustomStock(article="БЕЗ-ФОТО", quantity=1, net_cost=100.0))
    db.add(models.MarketplaceCard(
        marketplace_id=_mp_id(db, "wb"), chrt_id="c:no", vendor_code="БЕЗ-ФОТО",
        nm_id="", barcode="",
    ))
    db.add(models.WbDetailRow(
        op_key="no", source="excel", article="БЕЗ-ФОТО", doc_type_name="Продажа",
        sale_dt=TODAY, quantity=60, retail_amount=60000.0, for_pay=54000.0,
    ))
    db.commit()
    r = pdf_api.get("/api/export/replenish/pdf", params={"with_photos": 1})
    assert r.headers["X-Count"] == "2"
    assert r.headers["X-Photo-Hits"] == "1"      # нашлось только у одного


def test_thumb_failure_degrades_gracefully(pdf_api, photo_tree, monkeypatch):
    from app.services import thumbs as thumbs_service

    def boom(*a, **k):
        raise RuntimeError("битый файл")

    monkeypatch.setattr(thumbs_service, "thumb_bytes", boom)
    r = pdf_api.get("/api/export/replenish/pdf", params={"with_photos": 1})
    assert r.status_code == 200
    assert r.headers["X-Photo-Hits"] == "0"
    assert r.content.startswith(b"%PDF")


def test_bad_date_is_400(pdf_api):
    r = pdf_api.get("/api/export/replenish/pdf",
                    params={"date_from": "мусор", "date_to": "2026-09-01"})
    assert r.status_code == 400
    assert "дата" in r.json()["detail"].lower()


def test_reversed_window_is_not_rejected(pdf_api):
    """Документирует quirk общего parse_window: порядок дат не проверяется.

    Отчёт собирается, но span становится отрицательным, а в подзаголовок
    уходят перевёрнутые даты («10.09.2026 — 01.09.2026»). Исправить нужно в
    app/services/window.py, иначе PDF-эндпоинт будет вести себя иначе всех
    остальных выгрузок.
    """
    r = pdf_api.get("/api/export/replenish/pdf",
                    params={"date_from": "2026-09-10", "date_to": "2026-09-01"})
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")


# ------------------------------------------------- окно скорости (vel_days)
def _card_with_size(db, article, size, sold=0, day=None):
    db.add(models.MarketplaceCard(
        marketplace_id=_mp_id(db, "wb"), chrt_id=f"c:{article}:{size}",
        vendor_code=article, nm_id="", barcode="", size=size,
    ))
    if sold:
        db.add(models.WbDetailRow(
            op_key=f"{article}{size}", source="excel", article=article,
            doc_type_name="Продажа", sale_dt=day or TODAY, quantity=sold,
            tech_size=size, retail_amount=1000.0 * sold, for_pay=900.0 * sold,
        ))
    db.add(models.Product(article=article, name="Товар " + article,
                          net_cost=100.0, replenishable=True))


def _footer_text(pdf: bytes) -> str:
    import fitz

    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def test_vel_days_is_reflected_in_footer(pdf_api, db):
    _card_with_size(db, "VEL-1", "42")
    db.commit()

    for params, label in (
        ({"vel_days": 180}, "180 дн"),
        ({"vel_days": 365}, "365 дн"),
        ({"vel_days": 0}, "всё время"),
    ):
        r = pdf_api.get("/api/export/replenish/pdf", params={"with_photos": 0, **params})
        assert r.status_code == 200
        assert f"скорость по {label}" in _footer_text(r.content), label


def test_unknown_vel_days_falls_back_to_default(pdf_api, db):
    _card_with_size(db, "VEL-2", "42")
    db.commit()

    r = pdf_api.get("/api/export/replenish/pdf",
                    params={"with_photos": 0, "vel_days": 7})
    assert r.status_code == 200
    assert "скорость по 180 дн" in _footer_text(r.content)


def test_old_sale_outside_window_still_gets_one_piece(pdf_api, db):
    """Продажа вне окна скорости не обнуляет размер — срабатывает правило пола."""
    _card_with_size(db, "VEL-3", "42", sold=900,
                    day=TODAY - timedelta(days=WB_SORT_VELOCITY_DAYS + 5))
    db.commit()

    r = pdf_api.get("/api/export/replenish/pdf",
                    params={"with_photos": 0, "vel_days": WB_SORT_VELOCITY_DAYS})
    assert r.status_code == 200
    text = _footer_text(r.content)
    # пустой размер всё равно получает 1 штуку, поэтому в итоге есть строка
    assert "Итого дослать на WB" in text


def test_floor_rule_is_named_in_footer(pdf_api, db):
    """Правило пола должно быть видно в документе, иначе цифры непонятны."""
    _card_with_size(db, "VEL-4", "42")
    db.commit()

    r = pdf_api.get("/api/export/replenish/pdf", params={"with_photos": 0})
    assert "пустой размер" in _footer_text(r.content)


# ------------------------------------------------- учёт прибыльности (profit)
def _footer_all(pdf: bytes) -> str:
    import fitz

    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return "\n".join(p.get_text() for p in doc)
    finally:
        doc.close()


def test_profit_mode_explains_itself_in_footer(pdf_api, db):
    _card_with_size(db, "PRF-1", "42")
    db.commit()

    r = pdf_api.get("/api/export/replenish/pdf",
                    params={"with_photos": 0, "date_from": "2026-09-11",
                            "date_to": "2026-09-20", "profit": 1})
    assert r.status_code == 200
    txt = _footer_all(r.content)
    # 10 дней периода / 4 = 2.5 дня покрытия
    assert "покрытие 2.5 дн" in txt
    assert "период/4" in txt
    assert "прибыльность" in txt


def test_profit_off_uses_target_days_in_footer(pdf_api, db):
    _card_with_size(db, "PRF-2", "42")
    db.commit()

    r = pdf_api.get("/api/export/replenish/pdf",
                    params={"with_photos": 0, "target_days": 45, "profit": 0})
    assert r.status_code == 200
    txt = _footer_all(r.content)
    assert "запас 45 дн" in txt
    assert "период/4" not in txt


def test_profit_on_is_the_default(pdf_api, db):
    """Галка включена по умолчанию — без параметра PDF считает по прибыльности."""
    _card_with_size(db, "PRF-3", "42")
    db.commit()

    r = pdf_api.get("/api/export/replenish/pdf", params={"with_photos": 0})
    assert "период/4" in _footer_all(r.content)


def test_loss_making_article_is_dropped_from_pdf(pdf_api, db):
    """Убыточный товар в шопинг-листе не нужен: PDF без него."""
    db.add(models.Product(article="PRF-LOSS", name="Убыточный", net_cost=5000.0,
                          replenishable=True))
    db.add(models.MarketplaceCard(
        marketplace_id=_mp_id(db, "wb"), chrt_id="c:PRF-LOSS:42",
        vendor_code="PRF-LOSS", nm_id="", barcode="", size="42",
    ))
    db.add(models.WbDetailRow(
        op_key="prfloss", source="excel", article="PRF-LOSS",
        doc_type_name="Продажа", sale_dt=TODAY, quantity=300,
        tech_size="42", sku="", retail_amount=30000.0, for_pay=25500.0,
    ))
    db.add(models.Stock(
        marketplace_id=_mp_id(db, "wb"), date=TODAY, article="PRF-LOSS", size="42",
        warehouse="Стек", quantity=0, quantity_full=0, in_way=0,
    ))
    db.commit()

    r = pdf_api.get("/api/export/replenish/pdf", params={"with_photos": 0})
    assert r.status_code == 200
    assert "PRF-LOSS" not in _footer_all(r.content)


def test_article_reported_in_x_truncated_when_dropped(pdf_api, db):
    """Отбракованные по прибыльности товары не должны молча исчезать из счётчика."""
    db.add(models.Product(article="PRF-TRUNC", name="Убыточный", net_cost=5000.0,
                          replenishable=True))
    db.add(models.MarketplaceCard(
        marketplace_id=_mp_id(db, "wb"), chrt_id="c:PRF-TRUNC:42",
        vendor_code="PRF-TRUNC", nm_id="", barcode="", size="42",
    ))
    db.add(models.WbDetailRow(
        op_key="prftrunc", source="excel", article="PRF-TRUNC",
        doc_type_name="Продажа", sale_dt=TODAY, quantity=300,
        tech_size="42", sku="", retail_amount=30000.0, for_pay=25500.0,
    ))
    db.add(models.Stock(
        marketplace_id=_mp_id(db, "wb"), date=TODAY, article="PRF-TRUNC", size="42",
        warehouse="Стек", quantity=0, quantity_full=0, in_way=0,
    ))
    db.commit()

    r = pdf_api.get("/api/export/replenish/pdf",
                    params={"with_photos": 0, "limit": 500})
    assert int(r.headers["X-Truncated"]) >= 1
    # остаётся только прибыльный PDF-ART-1 из фикстуры, убыточный отброшен
    assert r.headers["X-Count"] == "1"
    assert "PRF-TRUNC" not in _footer_all(r.content)


# ------------------------- поля карточки из «Вида таблицы» и фото по умолчанию
def test_status_ui_key_is_printed_in_card(pdf_api):
    """«Вид таблицы» шлёт ключ status — карточка обязана напечатать «Статус»."""
    r = pdf_api.get("/api/export/replenish/pdf",
                    params={"with_photos": 0, "cols": "status,name"})
    assert r.status_code == 200
    txt = _footer_all(r.content)
    assert "PDF-ART-1" in txt
    assert "Статус" in txt


def test_wb_sells_col_is_printed_in_card(pdf_api):
    """Новая колонка «Продано WB, шт» доступна в карточке и в выгрузке."""
    r = pdf_api.get("/api/export/replenish/pdf",
                    params={"with_photos": 0, "cols": "wb_sells,name"})
    assert r.status_code == 200
    assert "Продано WB, шт" in _footer_all(r.content)


def test_default_photo_count_is_six(pdf_api, tmp_path, monkeypatch):
    """Без параметра photo_count в карточку идёт 6 фото (раньше было 4)."""
    from app.services import thumbs as thumbs_service

    # у PDF-ART-1 должно быть больше 4 снимков, иначе 6 и 4 неразличимы
    root = tmp_path / "photos6"
    for n in range(1, 8):
        p = root / "set" / f"PDF-ART-1-{n}.JPG"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_jpeg(n))
    idx = photos_service.PhotoIndex(root=root)
    monkeypatch.setattr(photos_service, "get_index", lambda: idx)
    monkeypatch.setattr(
        thumbs_service, "thumb_bytes",
        lambda path, max_px=None, quality=None: _jpeg(abs(hash(str(path))) % 90 + 1),
    )

    default = pdf_api.get("/api/export/replenish/pdf", params={"with_photos": 1})
    four = pdf_api.get("/api/export/replenish/pdf",
                       params={"with_photos": 1, "photo_count": 4})
    six = pdf_api.get("/api/export/replenish/pdf",
                      params={"with_photos": 1, "photo_count": 6})
    assert default.status_code == 200 and four.status_code == 200
    assert default.headers["X-Photo-Hits"] == "1"
    assert len(default.content) == len(six.content)
    assert len(four.content) < len(six.content)


# ------------------------------ PDF из Excel-файла (дропзона в меню PDF) ---
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_TOTAL_RE = re.compile(r"Итого дослать на WB: ([\d ]+?) шт")


def _xlsx(headers, rows) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _post_pdf(pdf_api, rows, source="правки.xlsx", **query):
    params = {"with_photos": 0}
    params.update(query)
    payload = {"rows": rows}
    if source:
        payload["source"] = source
    return pdf_api.post("/api/export/replenish/pdf", params=params, json=payload)


def _one_total(pdf: bytes) -> str:
    m = _TOTAL_RE.search(_footer_all(pdf))
    assert m, "в документе нет строки «Итого дослать на WB»"
    return m.group(1).replace(" ", "")


def test_import_excel_returns_rows_and_meta(pdf_api):
    """POST /replenish/import-excel: строки с ключами выгрузки + сводка."""
    data = _xlsx(
        ["Артикул", "Наименование", "Спрос, шт/день", "WB дефицит, шт"],
        [["EX-1", "Платье летнее", 1.5, 7]],
    )
    r = pdf_api.post("/api/replenish/import-excel",
                     files={"file": ("правки.xlsx", data, XLSX_MIME)})
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == [{
        "article": "EX-1", "name": "Платье летнее", "demand": 1.5, "wb_def": 7,
    }]
    assert body["meta"]["count"] == 1
    assert body["meta"]["file"] == "правки.xlsx"
    assert body["meta"]["unknown"] == []


def test_import_excel_rejects_non_xlsx(pdf_api):
    r = pdf_api.post("/api/replenish/import-excel",
                     files={"file": ("t.csv", b"a,b\n1,2", "text/csv")})
    assert r.status_code == 400
    assert "xlsx" in r.json()["detail"]


def test_import_excel_rejects_empty_file(pdf_api):
    r = pdf_api.post("/api/replenish/import-excel",
                     files={"file": ("p.xlsx", b"", XLSX_MIME)})
    assert r.status_code == 400
    assert "пустой" in r.json()["detail"]


def test_import_excel_rejects_file_without_article_column(pdf_api):
    data = _xlsx(["Наименование", "Спрос, шт/день"], [["Платье", 2]])
    r = pdf_api.post("/api/replenish/import-excel",
                     files={"file": ("p.xlsx", data, XLSX_MIME)})
    assert r.status_code == 400
    assert "Артикул" in r.json()["detail"]


def test_import_excel_rejects_rows_without_article(pdf_api):
    data = _xlsx(["Артикул", "Наименование"], [["", "Итого: 5"]])
    r = pdf_api.post("/api/replenish/import-excel",
                     files={"file": ("p.xlsx", data, XLSX_MIME)})
    assert r.status_code == 400
    assert "ни одной строки" in r.json()["detail"]


def test_pdf_from_excel_prints_file_values(pdf_api):
    """PDF из файла печатает данные файла, а не строки таблицы."""
    rows = [{"article": "PDF-ART-1", "name": "Платье из файла", "need_buy": 777}]
    r = _post_pdf(pdf_api, rows)
    assert r.status_code == 200
    assert r.headers["X-Count"] == "1"
    txt = _footer_all(r.content)
    assert "Платье из файла" in txt   # имя из файла, не «Платье PDF-ART-1»
    assert "777" in txt               # «Купить у поставщика» из файла
    assert "из Excel: правки.xlsx" in txt  # приписка в шапке документа


def test_pdf_from_excel_budget_overrides_total(pdf_api):
    """Без бюджета — как в базовом режиме; «WB дефицит» задаёт итог."""
    natural = _post_pdf(pdf_api, [{"article": "PDF-ART-1"}])
    assert natural.status_code == 200

    base = pdf_api.get("/api/export/replenish/pdf",
                       params={"with_photos": 0, "article_like": "PDF-ART-1"})
    assert base.status_code == 200
    from_file = _one_total(natural.content)
    assert from_file == _one_total(base.content)   # пустой бюджет = план склада
    assert int(from_file) > 1

    bud = _post_pdf(pdf_api, [{"article": "PDF-ART-1", "wb_def": 1}])
    assert bud.status_code == 200
    assert _one_total(bud.content) == "1"          # бюджет из колонки файла


def test_pdf_from_excel_keeps_loss_making_article(pdf_api, db):
    """Убыточный из файла печатается: состав задаёт файл, не отбраковка."""
    db.add(models.Product(article="EX-LOSS", name="Убыточный", net_cost=5000.0,
                          replenishable=True))
    db.add(models.MarketplaceCard(
        marketplace_id=_mp_id(db, "wb"), chrt_id="c:EX-LOSS:42",
        vendor_code="EX-LOSS", nm_id="", barcode="", size="42",
    ))
    db.add(models.WbDetailRow(
        op_key="exloss", source="excel", article="EX-LOSS",
        doc_type_name="Продажа", sale_dt=TODAY, quantity=300,
        tech_size="42", sku="", retail_amount=30000.0, for_pay=25500.0,
    ))
    db.add(models.Stock(
        marketplace_id=_mp_id(db, "wb"), date=TODAY, article="EX-LOSS", size="42",
        warehouse="Стек", quantity=0, quantity_full=0, in_way=0,
    ))
    db.commit()

    base = pdf_api.get("/api/export/replenish/pdf", params={"with_photos": 0})
    assert base.status_code == 200
    assert "EX-LOSS" not in _footer_all(base.content)   # базовый режим отбрасывает

    r = _post_pdf(pdf_api, [{"article": "EX-LOSS", "name": "Убыточный", "wb_def": 5}])
    assert r.status_code == 200
    assert r.headers["X-Count"] == "1"
    txt = _footer_all(r.content)
    assert "EX-LOSS" in txt
    assert _one_total(r.content) == "5"
    assert "дослата задана вручную" in txt   # ×0 не спорит с бюджетом файла


def test_pdf_from_excel_ignores_table_filters(pdf_api):
    """Фильтры таблицы не опустошают PDF: состав задаёт файл."""
    r = _post_pdf(pdf_api, [{"article": "PDF-ART-1"}],
                  article_like="НЕ-НАЙДУ", marketplace="ozon")
    assert r.status_code == 200
    assert r.headers["X-Count"] == "1"
    assert "PDF-ART-1" in _footer_all(r.content)


def test_pdf_from_excel_requires_rows(pdf_api):
    r = pdf_api.post("/api/export/replenish/pdf", params={"with_photos": 0}, json={})
    assert r.status_code == 400
    assert "нет строк" in r.json()["detail"]


def test_pdf_from_excel_rejects_row_without_article(pdf_api):
    r = _post_pdf(pdf_api, [{"name": "Без артикула"}])
    assert r.status_code == 400
    assert "нет артикула" in r.json()["detail"]


def test_pdf_from_excel_attaches_photos(pdf_api, photo_tree):
    r = _post_pdf(pdf_api, [{"article": "PDF-ART-1"}],
                  with_photos=1, photo_count=2)
    assert r.status_code == 200
    assert r.headers["X-Photo-Hits"] == "1"


def test_pdf_from_excel_limit_truncates(pdf_api, db):
    for i in (2, 3):
        db.add(models.Product(article=f"PDF-ART-{i}", name="x",
                              net_cost=100.0, replenishable=True))
        db.add(models.CustomStock(article=f"PDF-ART-{i}", quantity=1, net_cost=100.0))
        db.add(models.MarketplaceCard(
            marketplace_id=_mp_id(db, "wb"), chrt_id=f"c:{i}",
            vendor_code=f"PDF-ART-{i}", nm_id="", barcode="",
        ))
        db.add(models.WbDetailRow(
            op_key=f"ex{i}", source="excel", article=f"PDF-ART-{i}",
            doc_type_name="Продажа", sale_dt=TODAY, quantity=60,
            retail_amount=60000.0, for_pay=54000.0,
        ))
    db.commit()

    rows = [{"article": f"PDF-ART-{i}"} for i in (1, 2, 3)]
    r = _post_pdf(pdf_api, rows, limit=1)
    assert r.status_code == 200
    assert r.headers["X-Count"] == "1"
    assert r.headers["X-Truncated"] == "2"
    assert "PDF-ART-1" in _footer_all(r.content)   # порядок файла сохраняется


