# -*- coding: utf-8 -*-
"""API массовых действий по связкам (/api/batch*).

Проверяются: реестр действий, обязательное подтверждение разрушающей
коррекции цен, пропуск связок без ключей в предпросмотре и полный прогон
обновления с ожиданием фонового задания.
"""
import time

import pytest
from sqlalchemy import text

from app import models
from app.database import Base, engine
from app.services import batch as batch_service
from app.services import cabinet_migrate
from app.services import cabinets as cabinet_service
from app.services import secrets

SCHEMA = "cab_batch_test"


def _mk_company(db, name="Фирма масс"):
    user = cabinet_service.seed_user(db)
    c = models.Company(user_id=user.id, name=name, enabled=True, position=9)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _mk_cab(db, code, schema=SCHEMA, marketplace="wb", creds="", name="Связка"):
    user = cabinet_service.seed_user(db)
    company = _mk_company(db)
    c = models.Cabinet(user_id=user.id, company_id=company.id, code=code, name=name,
                       schema=schema, marketplace=marketplace, creds=creds,
                       enabled=True, position=9)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _drop_schema():
    with engine.begin() as conn:
        conn.execute(text(f'drop schema if exists "{SCHEMA}" cascade'))
        Base.metadata.create_all(conn, checkfirst=True)


@pytest.fixture(autouse=True)
def _clean_batch_jobs():
    with batch_service._LOCK:
        batch_service._JOBS.clear()
    yield
    with batch_service._LOCK:
        batch_service._JOBS.clear()


def _wait_batch(job_id, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = batch_service.job_state(job_id)
        if st and st["status"] == "done":
            return st
        time.sleep(0.1)
    raise AssertionError(f"batch job {job_id} не завершился")


def test_batch_actions_endpoint(api_client):
    r = api_client.get("/api/batch/actions")
    assert r.status_code == 200
    body = r.json()
    assert [a["id"] for a in body["actions"]] == [
        "refresh", "recalc", "apply", "minpromo", "analytics"]
    assert set(body["scopes"]) == {"all", "active", "main"}


def test_batch_run_rejects_empty_actions(api_client):
    r = api_client.post("/api/batch/run", json={"actions": []})
    assert r.status_code == 400


def test_batch_run_apply_needs_confirmation(api_client, db):
    _mk_cab(db, "b-conf", creds=secrets.encrypt({"wb": {"standard": "t"}}))
    r = api_client.post("/api/batch/run", json={"actions": ["apply"], "scope": "all"})
    assert r.status_code == 409
    assert "подтверд" in r.json()["detail"].lower()


def test_batch_preview_skips_cabs_without_keys(api_client, db):
    _mk_cab(db, "b-nokeys", creds="")
    r = api_client.post("/api/batch/preview",
                        json={"actions": ["recalc"], "scope": "all"})
    assert r.status_code == 200
    assert r.json()["cabs"] == []


def test_batch_state_unknown_404(api_client):
    assert api_client.get("/api/batch/99999").status_code == 404


def test_batch_minpromo_unknown_404(api_client):
    assert api_client.get("/api/batch/99999/minpromo/1").status_code == 404


def test_batch_run_refresh_completes(db_engine, api_client, db, patch_factory):
    _drop_schema()
    c = _mk_cab(db, "b-run", marketplace="wb",
                creds=secrets.encrypt({"wb": {"standard": "x", "finance": "y"}}))
    cabinet_migrate.ensure_cabinet_schemas(engine, [c])

    r = api_client.post("/api/batch/run", json={"actions": ["refresh"], "scope": "all"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert job_id

    st = _wait_batch(job_id)
    assert st["status"] == "done"
    assert st["ok"] == 1 and st["failed"] == 0, st["steps"]
    step = st["steps"][0]
    assert step["action"] == "refresh"
    assert step["status"] == "ok"
    assert step["cab_id"] == c.id
    # подробный прогресс: под-этапы вложенного обновления (цены/остатки/…)
    assert step.get("substeps"), "нет под-этапов обновления"
    labels = [s["label"] for s in step["substeps"]]
    assert "Цены" in labels and "Остатки" in labels
    assert all(s["status"] == "ok" for s in step["substeps"])
    # разрез по кабинетам тоже несёт под-этапы
    assert st["cabs"][0]["steps"][0]["substeps"]

    # состояние и список заданий доступны через API
    r2 = api_client.get(f"/api/batch/{job_id}")
    assert r2.status_code == 200 and r2.json()["status"] == "done"
    ids = [j["id"] for j in api_client.get("/api/batch").json()["jobs"]]
    assert job_id in ids

    db.rollback()  # снять ACCESS SHARE, иначе DROP SCHEMA ждёт нашу транзакцию
    _drop_schema()
    # Воркеры (batch и вложенный refresh) пиннят search_path на своей сессии;
    # соединения возвращаются в пул со схемой связки. Сброс пула не даёт
    # «протечь» search_path в последующие тесты того же процесса.
    engine.dispose()
