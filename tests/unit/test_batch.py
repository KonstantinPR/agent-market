# -*- coding: utf-8 -*-
"""Юнит-тесты сервиса массовых действий (без БД и сети)."""
from app.services import batch as batch_service
from app.services import refresh as refresh_service


def _mk_job(job_id, steps):
    return {"id": job_id, "status": "running", "scope": "all", "actions": ["refresh"],
            "started_at": "", "finished_at": None, "ok": 0, "failed": 0,
            "steps": steps, "cabs": [
                {"id": 1, "name": "Связка", "owner": "Фирма", "marketplace": "wb",
                 "status": "running", "steps": steps}]}


def test_actions_public_order_and_write_flag():
    acts = batch_service.actions_public()
    assert [a["id"] for a in acts] == ["refresh", "recalc", "apply", "minpromo"]
    by = {a["id"]: a for a in acts}
    assert by["apply"]["write"] is True
    assert by["refresh"]["write"] is False
    assert all(a["label"] for a in acts)


def test_minpromo_is_wb_only():
    assert batch_service._ACTION_BY_ID["minpromo"].get("mp") == ("wb",)


def test_normalize_actions_dedup_unknown_and_order():
    assert batch_service._normalize_actions(None) == []
    assert batch_service._normalize_actions([]) == []
    assert batch_service._normalize_actions(["recalc", "refresh", "recalc"]) == ["refresh", "recalc"]
    assert batch_service._normalize_actions(["bogus", "apply"]) == ["apply"]
    # порядок задаёт реестр, а не порядок во входе
    assert batch_service._normalize_actions(
        ["minpromo", "apply", "refresh", "recalc"]) == [
        "refresh", "recalc", "apply", "minpromo"]


def test_normalize_actions_trims_and_stringifies():
    assert batch_service._normalize_actions([" refresh ", 5, "apply"]) == ["refresh", "apply"]


def test_is_candidate():
    for a in ("RAISE", "LOWER", "HALVE"):
        assert batch_service._is_candidate({"action": a}) is True
    for a in ("KEEP", "", None, "RAISE ", "raise"):
        assert batch_service._is_candidate({"action": a}) is False


def test_run_rejects_empty_actions():
    res = batch_service.run([])
    assert res["job_id"] is None
    assert res.get("rejected")


def test_run_apply_requires_confirmation():
    res = batch_service.run(["apply"])
    assert res["job_id"] is None
    assert res.get("needs_confirm") is True


def test_job_state_attaches_live_refresh_substeps(monkeypatch):
    monkeypatch.setitem(refresh_service._JOBS, 991, {
        "id": 991, "status": "running", "api": "wb", "cab_id": 1, "cab_name": "Связка",
        "include_detail": False, "date_from": None, "date_to": None,
        "started_at": "", "finished_at": None, "ok": 1, "failed": 0,
        "steps": [
            {"kind": "prices", "label": "Цены", "status": "running", "rows": 5,
             "db_rows": 5, "window": "", "note": "", "error": ""},
            {"kind": "stock", "label": "Остатки", "status": "ok", "rows": 3,
             "db_rows": 3, "window": "", "note": "", "error": ""},
        ]})
    step = {"action": "refresh", "label": "Фирма · Обновить данные", "cab_id": 1,
            "status": "running", "rows": 0, "note": "", "error": "",
            "refresh_job_id": 991}
    monkeypatch.setitem(batch_service._JOBS, 7, _mk_job(7, [step]))

    st = batch_service.job_state(7)
    subs = st["steps"][0]["substeps"]
    assert [s["label"] for s in subs] == ["Цены", "Остатки"]
    assert subs[0]["status"] == "running" and subs[1]["rows"] == 3
    # под-этапы продублированы и в разрезе кабинетов
    assert st["cabs"][0]["steps"][0]["substeps"][1]["label"] == "Остатки"
    # служебные поля refresh-шага в под-этапы не протекают
    assert "kind" in subs[0] and "cab_id" not in subs[0]


def test_job_state_without_refresh_has_no_substeps(monkeypatch):
    step = {"action": "recalc", "label": "Фирма · Пересчитать автопилот", "cab_id": 1,
            "status": "ok", "rows": 4, "note": "", "error": ""}
    monkeypatch.setitem(batch_service._JOBS, 8, _mk_job(8, [step]))

    st = batch_service.job_state(8)
    assert "substeps" not in st["steps"][0]


def test_job_state_missing_refresh_job_has_no_substeps(monkeypatch):
    step = {"action": "refresh", "label": "Фирма · Обновить данные", "cab_id": 1,
            "status": "running", "rows": 0, "note": "", "error": "",
            "refresh_job_id": 999999}
    monkeypatch.setitem(batch_service._JOBS, 9, _mk_job(9, [step]))

    st = batch_service.job_state(9)
    assert "substeps" not in st["steps"][0]
