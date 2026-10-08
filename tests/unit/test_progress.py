# -*- coding: utf-8 -*-
"""Прогресс длительных операций (app/services/progress.py).

- юнит-тесты форматирования и TTL без БД;
- интеграция: middleware кладёт X-Progress-Id в ContextVar, который виден и в
  sync-эндпоинте (threadpool), откуда report() пишет состояние, читаемое через
  GET /api/progress.
"""
import pandas as pd
import pytest

from app.services import progress as progress_service


@pytest.fixture(autouse=True)
def _clean_progress():
    progress_service._STATE.clear()
    yield
    progress_service.set_op("")
    progress_service._STATE.clear()


def test_report_requires_op_in_context():
    progress_service.set_op("")
    progress_service.report(label="X", done=1)
    assert progress_service._STATE == {}


def test_report_builds_text_with_percent():
    progress_service.set_op("op1")
    progress_service.report(label="Карточки WB", stage="стр. 2",
                            done=4820, total=16000, unit="тов.")
    assert progress_service.snapshot("op1") == {
        "text": "Карточки WB: стр. 2 · 4 820/16 000 тов. (30%)"}


def test_report_merges_fields():
    progress_service.set_op("op2")
    progress_service.report(label="Воронка WB", stage="стр. 1", unit="стр.")
    progress_service.report(done=1000)
    assert progress_service.snapshot("op2")["text"] == "Воронка WB: стр. 1 · 1 000 стр."


def test_stage_can_be_cleared():
    progress_service.set_op("op3")
    progress_service.report(label="Продажи WB", stage="запрос отчёта…", unit="стр.")
    progress_service.report(stage="получено", done=5)
    assert progress_service.snapshot("op3")["text"] == "Продажи WB: получено · 5 стр."
    progress_service.report(stage="", done=5)
    assert progress_service.snapshot("op3")["text"] == "Продажи WB: 5 стр."


def test_snapshot_expires_after_ttl(monkeypatch):
    progress_service.set_op("op4")
    monkeypatch.setattr(progress_service.time, "time", lambda: 1000.0)
    progress_service.report(label="X", done=1)
    monkeypatch.setattr(progress_service.time, "time", lambda: 1000.0 + progress_service.TTL + 1)
    assert progress_service.snapshot("op4") is None


def test_snapshot_unknown_op():
    assert progress_service.snapshot("nope") is None


def test_progress_route_without_op_returns_empty(client):
    r = client.get("/api/progress")
    assert r.status_code == 200
    assert r.json() == {"text": ""}


def test_progress_contextvar_reaches_sync_endpoint(client, monkeypatch):
    """X-Progress-Id → middleware → ContextVar → report() внутри sync-эндпоинта."""
    from app.services import refresh as refresh_service

    def fake_pull(db, write_db=True):
        progress_service.report(label="Карточки WB", stage="стр. 2",
                                done=4820, total=16000, unit="тов.")
        return {"df": pd.DataFrame(), "count": 0, "db_rows": 0,
                "rows": 0, "window": "сегодня"}

    monkeypatch.setattr(refresh_service, "pull_wb_cards", fake_pull)
    r = client.post("/api/wb/cards?excel=0", headers={"X-Progress-Id": "op_test"})
    assert r.status_code == 200
    snap = client.get("/api/progress", params={"op": "op_test"})
    assert snap.status_code == 200
    assert snap.json()["text"] == "Карточки WB: стр. 2 · 4 820/16 000 тов. (30%)"