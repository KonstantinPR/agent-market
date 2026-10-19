# -*- coding: utf-8 -*-
"""API раздела «Автоматизация»."""

import io
import zipfile

from PIL import Image

from app import models
from app.services import photos


class _Entry:
    def __init__(self, items):
        self.items = items


class FakeIndex:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, article, allow_prefix=False):
        return self._m.get(str(article).strip().upper())


def _mk(tmp_path, name, data=b"x"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _png_bytes(w=120, h=240):
    im = Image.new("RGB", (w, h), (255, 255, 255))
    for x in range(w // 4, w * 3 // 4):
        for y in range(h // 4, h * 3 // 4):
            im.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ имена


def test_names_multiply(client):
    r = client.post("/api/automation/names/multiply", data={"articles": "A B", "multiply": "2"})
    assert r.status_code == 200
    j = r.json()
    assert j["items"] == ["A-1.JPG", "A-2.JPG", "B-1.JPG", "B-2.JPG"]
    assert j["count"] == 4


def test_names_download(client):
    r = client.post("/api/automation/names/download", data={"articles": "A", "multiply": "1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "A-1.JPG" in r.text


def test_names_requires_articles(client):
    assert client.post("/api/automation/names/multiply", data={"articles": ""}).status_code == 400


def test_names_multiply_validation(client):
    r = client.post("/api/automation/names/multiply", data={"articles": "A", "multiply": "0"})
    assert r.status_code == 400


def test_names_multiply_defaults_to_one(client):
    r = client.post("/api/automation/names/multiply", data={"articles": "A"})
    assert r.status_code == 200
    assert r.json()["items"] == ["A-1.JPG"]


def test_names_multiply_upper_default(client):
    # Капслок включён по умолчанию.
    r = client.post("/api/automation/names/multiply", data={"articles": "wlp-117"})
    assert r.status_code == 200
    assert r.json()["items"] == ["WLP-117-1.JPG"]


def test_names_multiply_upper_false(client):
    r = client.post("/api/automation/names/multiply",
                    data={"articles": "wlp-117", "upper": "false"})
    assert r.status_code == 200
    assert r.json()["items"] == ["wlp-117-1.JPG"]


def test_names_download_upper_false(client):
    r = client.post("/api/automation/names/download",
                    data={"articles": "wlp-117", "upper": "false"})
    assert r.status_code == 200
    assert "wlp-117-1.JPG" in r.text


def test_template_xlsx(client):
    r = client.get("/api/automation/template")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert len(r.content) > 0


# ------------------------------------------------------------------ фолдеринг


def test_foldering_preview(client, db, tmp_path, monkeypatch):
    db.add(models.NmArticle(nm_id="111", article="A1"))
    db.commit()
    fake = FakeIndex({"A1": _Entry([(1, _mk(tmp_path, "a1-1.jpg"))])})
    monkeypatch.setattr(photos, "get_index", lambda: fake)

    r = client.post("/api/automation/foldering/preview", data={"articles": "a1", "marketplace": "WB"})
    assert r.status_code == 200
    rep = r.json()
    assert rep["found"] == 1 and rep["photos"] == 1


def test_foldering_build_zip(client, db, tmp_path, monkeypatch):
    db.add(models.NmArticle(nm_id="222", article="B2"))
    db.commit()
    fake = FakeIndex({"B2": _Entry([(1, _mk(tmp_path, "b2-1.jpg", b"img"))])})
    monkeypatch.setattr(photos, "get_index", lambda: fake)

    r = client.post("/api/automation/foldering/build", data={"articles": "b2", "marketplace": "WB"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.namelist() == ["222/b2-1.jpg"]


def test_foldering_build_no_photos(client):
    r = client.post("/api/automation/foldering/build", data={"articles": "nope", "marketplace": "WB"})
    assert r.status_code == 400


# ------------------------------------------------------------------ кроп


def test_crop_build(client):
    r = client.post(
        "/api/automation/crop/build",
        data={"type_clothes": "Одежда по пояс"},
        files=[("files", ("p.png", _png_bytes(), "image/png"))],
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.namelist() == ["p.JPG"]


def test_crop_build_no_images(client):
    r = client.post("/api/automation/crop/build", data={"type_clothes": "Одежда по пояс"})
    assert r.status_code == 400


# ------------------------------------------------------------------ штрихкоды


def test_barcodes_preview_ok(client):
    r = client.post(
        "/api/automation/barcodes/preview",
        data={"codes": "ABC123\n460123456789", "kind": "code128"},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["count"] == 2 and j["ok_count"] == 2
    assert j["truncated"] is False
    for it in j["items"]:
        assert it["ok"] and it["data_url"].startswith("data:image/png;base64,")


def test_barcodes_preview_bad_kind(client):
    r = client.post("/api/automation/barcodes/preview", data={"codes": "A", "kind": "qr"})
    assert r.status_code == 400


def test_barcodes_preview_empty(client):
    r = client.post("/api/automation/barcodes/preview", data={"codes": "", "kind": "code128"})
    assert r.status_code == 400


def test_barcodes_preview_ean13_invalid_row_is_item_error(client):
    r = client.post(
        "/api/automation/barcodes/preview",
        data={"codes": "ABC\n460123456789", "kind": "ean13"},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["ok_count"] == 1
    bad = [it for it in j["items"] if not it["ok"]]
    assert len(bad) == 1 and "цифры" in bad[0]["error"]


def test_barcodes_preview_from_file(client):
    r = client.post(
        "/api/automation/barcodes/preview",
        data={"codes": "", "kind": "code128"},
        files=[("file", ("codes.txt", b"K1\nK2\n", "text/plain"))],
    )
    assert r.status_code == 200
    assert r.json()["count"] == 2


def test_barcodes_preview_gs1_comma_inside_not_split(client):
    """Регрессия T-49: запятая внутри кода маркировки не делит строку."""
    line1 = "0104670630636090215LcFWqhXYe:XY91EE1292nJjYsEmycZ+vKSpUiOOz+UOVn9cFEnjk8uO5f4Tx1ho="
    line2 = "0104670630636090215gE0XLM,YF-Wg91EE1292VuREv8Zh8IFEiWr6bTdtw82pE7i/E+j+8BKvhpehplQ="
    r = client.post(
        "/api/automation/barcodes/preview",
        data={"codes": line1 + "\n" + line2, "kind": "datamatrix"},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["count"] == 2 and j["ok_count"] == 2
    assert j["items"][1]["code"] == line2


def test_barcodes_preview_crlf_lines(client):
    r = client.post(
        "/api/automation/barcodes/preview",
        data={"codes": "K1\r\nK2\r\n", "kind": "code128"},
    )
    assert r.status_code == 200
    assert r.json()["count"] == 2


def test_barcodes_preview_txt_file_keeps_comma(client):
    payload = "0104670630636090215gE0XLM,YF-Wg91EE1292\nABC\n".encode()
    r = client.post(
        "/api/automation/barcodes/preview",
        data={"codes": "", "kind": "datamatrix"},
        files=[("file", ("codes.txt", payload, "text/plain"))],
    )
    assert r.status_code == 200
    j = r.json()
    assert j["count"] == 2
    assert j["items"][0]["code"] == "0104670630636090215gE0XLM,YF-Wg91EE1292"


def test_barcodes_build_zip(client):
    r = client.post(
        "/api/automation/barcodes/build",
        data={"codes": "A1\nA2", "kind": "code128"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "barcodes_code128.zip" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.namelist() == ["bar_0001.png", "bar_0002.png"]
    assert z.read("bar_0001.png").startswith(b"\x89PNG")


def test_barcodes_build_all_invalid(client):
    r = client.post(
        "/api/automation/barcodes/build",
        data={"codes": "abc", "kind": "ean13"},
    )
    assert r.status_code == 400


def test_barcodes_build_empty(client):
    r = client.post("/api/automation/barcodes/build", data={"codes": "", "kind": "code128"})
    assert r.status_code == 400


def test_barcodes_catalog_sizes_and_fallback(client, db):
    db.add(models.Product(article="A1", name="С размерами", barcode=""))
    db.add_all([
        models.ProductSize(article="A1", size="S", barcode="4600000000012"),
        models.ProductSize(article="A1", size="M", barcode=""),
    ])
    db.add(models.Product(article="B1", name="Без размеров", barcode="4600000000029"))
    db.add(models.Product(article="C1", name="Пусто", barcode=""))
    db.commit()

    r = client.get("/api/automation/barcodes/catalog")
    assert r.status_code == 200
    j = r.json()
    assert j["count"] == 2 and j["truncated"] is False
    assert j["items"] == [
        {"article": "A1", "size": "S", "barcode": "4600000000012"},
        {"article": "B1", "size": "", "barcode": "4600000000029"},
    ]


def test_barcodes_catalog_filter_articles(client, db):
    db.add(models.Product(article="A1", name="", barcode="4600000000012"))
    db.add(models.Product(article="B1", name="", barcode="4600000000029"))
    db.commit()

    r = client.get("/api/automation/barcodes/catalog", params={"articles": "a1"})
    j = r.json()
    assert j["count"] == 1 and j["items"][0]["article"] == "A1"


def test_barcodes_catalog_limit(client, db):
    db.add(models.Product(article="A1", name="Товар", barcode=""))
    db.add_all([
        models.ProductSize(article="A1", size=f"{i}", barcode=f"46000000000{i}")
        for i in range(3)
    ])
    db.commit()

    r = client.get("/api/automation/barcodes/catalog", params={"limit": 2})
    j = r.json()
    assert j["count"] == 2 and j["truncated"] is True
