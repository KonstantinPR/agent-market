"""API-тесты тикетов. ТИКЕТ-файл подменяется на временный (БД не нужна)."""

import pytest

from app.services import tickets as ts


@pytest.fixture()
def ticket_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(ts, "TICKETS_FILE", tmp_path / "TICKETS.md")
    with TestClient(app) as c:
        yield c


def test_get_tickets_empty(ticket_api):
    r = ticket_api.get("/api/tickets")
    assert r.status_code == 200
    j = r.json()
    assert j["missing"] is True
    assert set(j["sections"]) == {"open", "in_progress", "blocked", "closed"}


def test_create_and_get(ticket_api):
    r = ticket_api.post("/api/tickets", json={"title": "Добавить фичу", "priority": "high"})
    assert r.status_code == 200
    j = r.json()
    assert j["id"] == "T-1"
    assert j["state"] == "open"
    d = ticket_api.get("/api/tickets").json()
    assert d["sections"]["open"][0]["title"] == "Добавить фичу"
    assert d["counter"] == 2


def test_create_validation(ticket_api):
    assert ticket_api.post("/api/tickets", json={"title": ""}).status_code == 400
    assert ticket_api.post("/api/tickets", json={"title": "X", "priority": "urgent"}).status_code == 400


def test_start_close_flow(ticket_api):
    ticket_api.post("/api/tickets", json={"title": "Фича"})
    r = ticket_api.post("/api/tickets/T-1/start")
    assert r.status_code == 200
    assert r.json()["state"] == "in_progress"
    r = ticket_api.post("/api/tickets/T-1/close", json={"commit": "a1b2c3"})
    assert r.status_code == 200
    d = ticket_api.get("/api/tickets").json()
    assert d["sections"]["closed"][0]["commit"] == "a1b2c3"


def test_decline_and_reopen(ticket_api):
    ticket_api.post("/api/tickets", json={"title": "Не надо"})
    assert ticket_api.post("/api/tickets/T-1/decline").json()["state"] == "declined"
    assert ticket_api.post("/api/tickets/T-1/reopen").status_code == 200
    assert ticket_api.get("/api/tickets").json()["sections"]["open"]


def test_block_unblock(ticket_api):
    ticket_api.post("/api/tickets", json={"title": "Ждёт токен"})
    ticket_api.post("/api/tickets/T-1/block")
    assert ticket_api.get("/api/tickets").json()["sections"]["blocked"]
    ticket_api.post("/api/tickets/T-1/unblock")
    assert ticket_api.get("/api/tickets").json()["sections"]["open"]


def test_unknown_ticket_404(ticket_api):
    assert ticket_api.post("/api/tickets/T-99/start").status_code == 404


def test_invalid_transition_409(ticket_api):
    ticket_api.post("/api/tickets", json={"title": "A"})
    ticket_api.post("/api/tickets/T-1/start")
    assert ticket_api.post("/api/tickets/T-1/start").status_code == 409