# -*- coding: utf-8 -*-
"""T-80: API настроек хранилищ и ссылок (шторка «Личные кабинеты»)."""
from app import models
from app.services import cabinets as cabinet_service


def test_storage_settings_get(client):
    r = client.get("/api/settings/storage")
    assert r.status_code == 200
    body = r.json()
    assert set(body["roots"]) == {
        "photos_root", "analytics_root", "products_file", "reports_folder", "stock_root"}
    assert body["roots"]["photos_root"]["title"] == "Фото товаров"
    # T-82: файл каталога себестоимости — отдельный корень (kind=file).
    file_row = body["roots"]["products_file"]
    assert file_row["kind"] == "file"
    assert file_row["value"].endswith("products.xlsx")
    assert "products_sync" in body
    assert isinstance(body["links"], list) and body["links"]
    assert body["companies"] == []


def test_storage_settings_put_and_reset(client, tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    drive = tmp_path / "drive"
    drive.mkdir()

    r = client.put("/api/settings/storage", json={
        "photos_root": str(photos),
        "analytics_root": str(drive),
        "reports_folder": "reports/2026",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["roots"]["photos_root"]["value"] == str(photos)
    assert body["roots"]["photos_root"]["exists"] is True
    assert body["roots"]["analytics_root"]["custom"] is True
    assert body["roots"]["reports_folder"]["value"] == "/reports/2026"

    r = client.delete("/api/settings/storage/photos_root")
    assert r.status_code == 200
    assert r.json()["roots"]["photos_root"]["custom"] is False

    r = client.delete("/api/settings/storage/nonsense")
    assert r.status_code == 400


def test_storage_settings_links(client):
    r = client.put("/api/settings/storage", json={
        "links": [
            {"title": "GitHub", "url": "https://github.com/KonstantinPR/agent-market"},
            {"title": "Своя", "url": "https://example.com/page"},
        ],
    })
    assert r.status_code == 200
    links = r.json()["links"]
    assert [link["title"] for link in links] == ["GitHub", "Своя"]
    assert all(link["id"] for link in links)

    r = client.put("/api/settings/storage", json={
        "links": [{"title": "Плохая", "url": "javascript:alert(1)"}],
    })
    assert r.status_code == 400

    r = client.delete("/api/settings/storage/links")
    assert r.status_code == 200
    assert len(r.json()["links"]) >= 4


def test_company_dir_api(client, db, tmp_path):
    user = cabinet_service.seed_user(db)
    company = models.Company(user_id=user.id, name="Фирма", analytics_dir="firm")
    db.add(company)
    db.commit()
    db.refresh(company)

    drive = tmp_path / "drive"
    (drive / "firm" / "WB").mkdir(parents=True)
    client.put("/api/settings/storage", json={"analytics_root": str(drive)})

    body = client.get("/api/settings/storage").json()
    row = next(c for c in body["companies"] if c["id"] == company.id)
    assert row["analytics_dir"] == "firm"
    assert row["exists"] is True

    r = client.put(f"/api/settings/storage/company/{company.id}",
                   json={"analytics_dir": "firm/WB"})
    assert r.status_code == 200, r.text
    row = next(c for c in r.json()["companies"] if c["id"] == company.id)
    assert row["analytics_dir"] == "firm/WB"

    r = client.put(f"/api/settings/storage/company/{company.id}",
                   json={"analytics_dir": "C:\\absolute"})
    assert r.status_code == 400

    r = client.put("/api/settings/storage/company/999999",
                   json={"analytics_dir": "x"})
    assert r.status_code == 404


def test_reveal_endpoint(client, tmp_path, monkeypatch):
    from app.services import storage_settings

    root = tmp_path / "photos"
    inside = root / "sub"
    inside.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    client.put("/api/settings/storage", json={"photos_root": str(root)})

    opened = []
    monkeypatch.setattr(storage_settings.os, "startfile", opened.append, raising=False)

    r = client.post("/api/system/reveal", json={"path": str(inside)})
    assert r.status_code == 200, r.text
    assert opened == [str(inside.resolve())]

    r = client.post("/api/system/reveal", json={"path": str(outside)})
    assert r.status_code == 400
