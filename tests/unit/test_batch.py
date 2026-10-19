# -*- coding: utf-8 -*-
"""Юнит-тесты сервиса массовых действий (без БД и сети)."""
from app.services import batch as batch_service


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
