# -*- coding: utf-8 -*-
"""API раздела «Автоматизация»."""

import io
import time
import urllib.parse
import zipfile

import pytest
from PIL import Image

from app import models
from app.config import settings
from app.services import face_retouch as face_service
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


def _wait_job(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/api/automation/foldering/build/{job_id}").json()
        if st["status"] in ("done", "failed"):
            return st
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} не завершился за {timeout} с")


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
    job_id = r.json()["job_id"]
    st = _wait_job(client, job_id)
    assert st["status"] == "done", st

    r = client.get(f"/api/automation/foldering/build/{job_id}/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.namelist() == ["222/b2-1.jpg"]


def test_foldering_build_no_photos(client):
    r = client.post("/api/automation/foldering/build", data={"articles": "nope", "marketplace": "WB"})
    assert r.status_code == 200
    st = _wait_job(client, r.json()["job_id"])
    assert st["status"] == "failed"
    assert st["error"]


def test_foldering_preview_photo_pairs(client, db, tmp_path, monkeypatch):
    db.add(models.NmArticle(nm_id="333", article="C3"))
    db.commit()
    fake = FakeIndex({"C3": _Entry([
        (1, _mk(tmp_path, "c3-1.jpg", b"one")),
        (2, _mk(tmp_path, "c3-2.jpg", b"two")),
    ])})
    monkeypatch.setattr(photos, "get_index", lambda: fake)

    r = client.post("/api/automation/foldering/preview", data={
        "articles": "c3", "marketplace": "WB", "photo_pairs": "[[1, 2]]",
    })
    assert r.status_code == 200
    row = r.json()["with_photos"][0]
    assert row["order"] == [
        {"pos": 1, "name": "c3-1.jpg", "src": "c3-2.jpg"},
        {"pos": 2, "name": "c3-2.jpg", "src": "c3-1.jpg"},
    ]
    assert row["skipped"] == []


def test_foldering_preview_photo_pairs_bad_json(client):
    r = client.post("/api/automation/foldering/preview", data={
        "articles": "a1", "photo_pairs": "[[1,",
    })
    assert r.status_code == 400
    assert "photo_pairs" in r.json()["detail"]


def test_foldering_build_photo_pairs(client, db, tmp_path, monkeypatch):
    db.add(models.NmArticle(nm_id="444", article="D4"))
    db.commit()
    fake = FakeIndex({"D4": _Entry([
        (1, _mk(tmp_path, "d4-1.jpg", b"one")),
        (2, _mk(tmp_path, "d4-2.jpg", b"two")),
    ])})
    monkeypatch.setattr(photos, "get_index", lambda: fake)

    r = client.post("/api/automation/foldering/build", data={
        "articles": "d4", "marketplace": "WB", "photo_pairs": "[[1, 2]]",
    })
    job_id = r.json()["job_id"]
    st = _wait_job(client, job_id)
    assert st["status"] == "done", st

    r = client.get(f"/api/automation/foldering/build/{job_id}/download")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["444/d4-1.jpg", "444/d4-2.jpg"]
    assert z.read("444/d4-1.jpg") == b"two"
    assert z.read("444/d4-2.jpg") == b"one"


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


# ------------------------------------------------------------------ обработка лиц


def test_face_options(client):
    r = client.get("/api/automation/face/options")
    assert r.status_code == 200
    j = r.json()
    assert [v["id"] for v in j["variants"]] == ["v1", "v2", "v3"]
    assert j["default_variant"] == "v1"
    assert j["out_name"] == "Готовые"
    assert ".jpg" in j["extensions"]
    assert isinstance(j["ready"], bool)


def test_face_start_requires_source(client):
    r = client.post("/api/automation/face/start", data={"variant": "v1"})
    assert r.status_code == 400
    assert r.json()["detail"]


def test_face_start_unknown_variant(client):
    r = client.post("/api/automation/face/start",
                    data={"variant": "v99", "source_dir": "C:\\photos"})
    assert r.status_code == 400
    assert "вариант" in r.json()["detail"].lower()


def test_face_start_missing_folder(client):
    r = client.post("/api/automation/face/start",
                    data={"variant": "v1", "source_dir": "C:\\no-such-folder-xyz"})
    assert r.status_code == 400


def test_face_state_unknown(client):
    assert client.get("/api/automation/face/state/999999").status_code == 404


def test_face_zip_unknown(client):
    assert client.get("/api/automation/face/zip/999999").status_code == 404


# ------------------------------------------------------- свои эталоны (API)


@pytest.fixture()
def face_pipeline(tmp_path, monkeypatch):
    """Мини-пайплайн: models + venv-питон + скрипты; etalon_prep подменён."""
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "etalon_180a.npz").write_bytes(b"npz")
    venv = tmp_path / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    (venv / "python.exe").write_bytes(b"")
    (tmp_path / "brand_batch.py").write_text("# fake", encoding="utf-8")
    (tmp_path / "etalon_prep.py").write_text("# fake", encoding="utf-8")
    monkeypatch.setattr(settings, "face_retouch_dir", str(tmp_path))
    monkeypatch.setattr(face_service, "_JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(face_service, "_LAST_SOURCE_FILE",
                        tmp_path / "jobs" / "last.txt")
    monkeypatch.setattr(face_service, "_run_etalon_prep",
                        lambda image, out: out.write_bytes(b"npz"))
    face_service._JOBS.clear()
    return tmp_path


def _etalon_url(etalon_id):
    return "/api/automation/face/etalons/" + urllib.parse.quote(etalon_id, safe="")


def test_face_etalon_add_and_delete(client, face_pipeline):
    r = client.post(
        "/api/automation/face/etalons",
        data={"name": "Маша"},
        files=[("file", ("masha.jpg", b"jpeg-bytes", "image/jpeg"))],
    )
    assert r.status_code == 200
    j = r.json()
    assert j == {"id": "custom:Маша", "label": "Маша", "custom": True}
    assert (face_pipeline / "models" / "custom" / "Маша.npz").is_file()

    opt = client.get("/api/automation/face/options").json()
    assert opt["references"][-1]["id"] == "custom:Маша"

    d = client.delete(_etalon_url("custom:Маша"))
    assert d.status_code == 200 and d.json() == {"deleted": True}
    assert not (face_pipeline / "models" / "custom" / "Маша.npz").exists()


def test_face_etalon_add_bad_file(client, face_pipeline):
    r = client.post(
        "/api/automation/face/etalons",
        files=[("file", ("masha.gif", b"gif", "image/gif"))],
    )
    assert r.status_code == 400
    assert "JPG" in r.json()["detail"]


def test_face_etalon_delete_builtin(client, face_pipeline):
    r = client.delete(_etalon_url("etalon_180a.npz"))
    assert r.status_code == 400
    assert "Встроенный" in r.json()["detail"]

