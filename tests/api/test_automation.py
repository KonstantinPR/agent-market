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
