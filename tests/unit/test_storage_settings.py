# -*- coding: utf-8 -*-
"""T-80: настройки хранилищ и ссылок проекта (app_settings)."""
import pytest

from app import models
from app.services import cabinets as cabinet_service
from app.services import photos as photos_service
from app.services import storage_settings


def test_defaults_from_config_and_builtin_links(db):
    payload = storage_settings.public_payload(db)
    defaults = storage_settings.default_roots()
    assert payload["roots"]["photos_root"]["value"] == defaults["photos_root"]
    assert payload["roots"]["analytics_root"]["value"] == defaults["analytics_root"]
    assert payload["roots"]["reports_folder"]["value"] == defaults["reports_folder"]
    for key in storage_settings.ROOT_KEYS:
        assert payload["roots"][key]["custom"] is False
    assert payload["roots"]["reports_folder"]["kind"] == "yandex"
    ids = {link["id"] for link in payload["links"]}
    assert {"github", "local", "gdrive", "yandex"} <= ids


def test_save_and_reset_photos_root(db, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    payload = storage_settings.save(db, {"photos_root": str(root)})
    row = payload["roots"]["photos_root"]
    assert row["value"] == str(root)
    assert row["custom"] is True
    assert row["exists"] is True

    # Значение переживает новый запрос (лежит в app_settings).
    again = storage_settings.public_payload(db)["roots"]["photos_root"]
    assert again["value"] == str(root)
    assert again["custom"] is True

    payload = storage_settings.reset(db, "photos_root")
    row = payload["roots"]["photos_root"]
    assert row["custom"] is False
    assert row["value"] == storage_settings.default_roots()["photos_root"]


def test_reports_folder_normalized_and_url(db):
    payload = storage_settings.save(db, {"reports_folder": "reports\\2026"})
    row = payload["roots"]["reports_folder"]
    assert row["value"] == "/reports/2026"
    assert row["url"] == "https://disk.yandex.ru/client/disk/reports/2026"
    with pytest.raises(ValueError):
        storage_settings.save(db, {"reports_folder": "/agent/../secret"})


def test_links_saved_validated_and_reset(db):
    with pytest.raises(ValueError):
        storage_settings.save(db, {"links": [{"title": "X", "url": "javascript:alert(1)"}]})
    with pytest.raises(ValueError):
        storage_settings.save(db, {"links": [{"title": "", "url": "https://x.ru"}]})

    payload = storage_settings.save(db, {
        "links": [{"title": "Своя ссылка", "url": "https://example.com"}],
    })
    assert len(payload["links"]) == 1
    assert payload["links"][0]["title"] == "Своя ссылка"
    assert payload["links"][0]["id"].startswith("link-")

    payload = storage_settings.reset(db, "links")
    assert {link["id"] for link in payload["links"]} >= {"github", "local"}


def test_company_analytics_dir(db, tmp_path):
    user = cabinet_service.seed_user(db)
    company = models.Company(user_id=user.id, name="Фирма", analytics_dir="firm")
    db.add(company)
    db.commit()
    db.refresh(company)

    root = tmp_path / "drive"
    (root / "firm").mkdir(parents=True)
    storage_settings.save(db, {"analytics_root": str(root)})
    payload = storage_settings.public_payload(db, companies=[company])
    row = payload["companies"][0]
    assert row["path"] == str(root / "firm")
    assert row["exists"] is True

    with pytest.raises(ValueError):
        storage_settings.save_company_dir(db, company, r"C:\absolute\path")
    with pytest.raises(ValueError):
        storage_settings.save_company_dir(db, company, "../outside")

    payload = storage_settings.save_company_dir(
        db, company, r"firm\AGENT MARKET\АНАЛИТИКА", companies=[company])
    assert company.analytics_dir == r"firm\AGENT MARKET\АНАЛИТИКА"
    assert payload["companies"][0]["analytics_dir"] == company.analytics_dir


def test_photos_root_change_resets_index(db, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(photos_service, "set_index", lambda index: calls.append(index))
    root = tmp_path / "photos"
    root.mkdir()
    storage_settings.save(db, {"photos_root": str(root)})
    assert calls == [None]


def test_join_folder():
    assert storage_settings.join_folder("/agent_market", "Наш склад") == "/agent_market/Наш склад"
    assert storage_settings.join_folder("/", "Наш склад", "Приход") == "/Наш склад/Приход"
    assert storage_settings.join_folder("/agent_market/", "/Наш склад/") == "/agent_market/Наш склад"


def test_reveal_only_inside_storage(db, tmp_path, monkeypatch):
    root = tmp_path / "photos"
    inside = root / "sub"
    inside.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    storage_settings.save(db, {"photos_root": str(root)})

    opened = []
    monkeypatch.setattr(storage_settings.os, "startfile", opened.append, raising=False)

    assert storage_settings.reveal(db, str(inside), []) == str(inside.resolve())
    assert len(opened) == 1

    with pytest.raises(ValueError):
        storage_settings.reveal(db, str(outside), [])
    with pytest.raises(ValueError):
        storage_settings.reveal(db, str(root / "missing"), [])
