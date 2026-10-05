"""API-тесты PDF-выгрузки потребности (/api/export/replenish/pdf).

Индекс фотографий и миниатюры подменяются на временный каталог, поэтому тесты
не ходят по диску `C:\YandexDisk\ФОТОГРАФИИ`.
"""
import io
from datetime import date, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from app import models
from app.api import PDF_DEFAULT_LIMIT, PDF_MAX_LIMIT, PDF_MEDIA
from app.services import photos as photos_service

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
