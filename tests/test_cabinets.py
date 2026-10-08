# -*- coding: utf-8 -*-
"""Личные кабинеты: схемы, миграция, поиск по схеме, эндпоинты, refresh по кабинету."""
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.database import Base, SessionLocal, engine
from app.services import cabinet_migrate
from app.services import cabinets as cabinet_service
from app.services import refresh as refresh_service
from app.services.cabinets import seed_users_and_cabinets

CAB_OO = "cab_oo_test"
CAB_IP = "cab_ip_test"


def _mk_cab(db: Session, code="cab1", name="Кабинет", schema=CAB_OO,
            marketplaces="wb,ozon", user=None) -> models.Cabinet:
    user = user or cabinet_service.seed_user(db)
    c = models.Cabinet(user_id=user.id, code=code, name=name, schema=schema,
                       marketplaces=marketplaces, creds="", enabled=True, position=9)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _drop_cab_schemas():
    with engine.begin() as conn:
        for s in (CAB_OO, CAB_IP):
            conn.execute(text(f'drop schema if exists "{s}" cascade'))
        conn.execute(text(
            "create table if not exists app_schema_state (key text primary key, value text)"))
        conn.execute(text("truncate app_schema_state"))
        conn.execute(text(
            "truncate sales, stocks, refresh_runs, api_pulls, marketplaces, "
            "users, cabinets restart identity cascade"))
        conn.execute(text(
            "insert into marketplaces (code, name) values ('wb', 'WB'), ('ozon', 'Ozon')"))


# ---------------------------------------------------------------------------
# Сиды
# ---------------------------------------------------------------------------
def test_seed_users_and_cabinets_creates_user_and_two_cabs(db_engine):
    _drop_cab_schemas()
    with SessionLocal() as db:
        cabs = seed_users_and_cabinets(db)
        assert len(cabs) == 2
        codes = {c.code for c in cabs}
        assert codes == {"oo_joinco", "ip_prudnikov"}
        user = cabinet_service.seed_user(db)
        assert user.last_cabinet_id is not None


# ---------------------------------------------------------------------------
# Схема кабинета и поиск по search_path
# ---------------------------------------------------------------------------
def test_search_path_routes_queries_into_cab_schema(db_engine):
    _drop_cab_schemas()
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{CAB_OO}"'))
        conn.execute(text('create table if not exists public.refresh_runs_like '
                          '(like public.refresh_runs including defaults)'))
        conn.execute(text(f'create table if not exists "{CAB_OO}".refresh_runs_like '
                          '(like public.refresh_runs including defaults)'))
        conn.execute(text(
            "insert into public.refresh_runs_like (api, status, started_at, results) "
            "values ('wb', 'running', now(), '[]')"))
        conn.execute(text(
            f"insert into \"{CAB_OO}\".refresh_runs_like (api, status, started_at, results) "
            f"values ('ozon', 'running', now(), '[]')"))
    with SessionLocal() as db:
        cabinet_service.apply_search_path(db, CAB_OO)
        rows = db.execute(text("select api from refresh_runs_like order by api")).scalars().all()
        assert rows == ["ozon"]
    with engine.begin() as conn:
        conn.execute(text(f'drop schema "{CAB_OO}" cascade'))
        conn.execute(text("drop table public.refresh_runs_like"))


def test_ensure_cabinet_schemas_creates_schemas_and_tables(db_engine):
    _drop_cab_schemas()
    with SessionLocal() as db:
        user = cabinet_service.seed_user(db)
        c1 = _mk_cab(db, code="c1", schema=CAB_OO, marketplaces="wb", user=user)
        c2 = _mk_cab(db, code="c2", schema=CAB_IP, marketplaces="ozon", user=user)
    cabinet_migrate.ensure_cabinet_schemas(engine, [c1, c2])
    with engine.begin() as conn:
        for s in (CAB_OO, CAB_IP):
            assert conn.execute(text(
                f"select to_regclass('{s}.refresh_runs') is not null")).scalar() is True
            assert conn.execute(text(
                f"select to_regclass('{s}.sales') is not null")).scalar() is True
            assert conn.execute(text(
                f"select to_regclass('{s}.ozon_detail_rows') is not null")).scalar() is True


# ---------------------------------------------------------------------------
# Миграция public -> схемы
# ---------------------------------------------------------------------------
def test_migration_moves_rows_and_is_idempotent(db_engine):
    _drop_cab_schemas()
    from datetime import date

    with SessionLocal() as db:
        user = cabinet_service.seed_user(db)
        c1 = _mk_cab(db, code="c1", schema=CAB_OO, marketplaces="wb", user=user)
        c2 = _mk_cab(db, code="c2", schema=CAB_IP, marketplaces="ozon", user=user)
        wb_id = db.execute(text("select id from marketplaces where code='wb'")).scalar()
        oz_id = db.execute(text("select id from marketplaces where code='ozon'")).scalar()
        db.add_all([
            models.Sale(date=date(2026, 9, 1), marketplace_id=wb_id, article="A1",
                        quantity=2, revenue=100, income=90),
            models.Sale(date=date(2026, 9, 1), marketplace_id=oz_id, article="O1",
                        quantity=1, revenue=50, income=45),
        ])
        db.commit()
    # продажи идут в схемы кабинетов (без drop_public), миграция раскатывает
    cabinet_migrate.ensure_cabinet_schemas(engine, [c1, c2])
    assert cabinet_migrate.migrate_public_data(engine, [c1, c2]) is True
    with engine.begin() as conn:
        wb_n = conn.execute(text(f'select count(*) from "{CAB_OO}".sales')).scalar()
        oz_n = conn.execute(text(f'select count(*) from "{CAB_IP}".sales')).scalar()
        assert (wb_n, oz_n) == (1, 1)
        assert conn.execute(text("select to_regclass('public.sales') is not null")).scalar() is False
    # повторный вызов — no-op
    assert cabinet_migrate.migrate_public_data(engine, [c1, c2]) is False
    # восстановим public-таблицы, «съеденные» миграцией, и уберём временные схемы,
    # чтобы остальные тесты сессии не упали
    with engine.begin() as conn:
        Base.metadata.create_all(conn, checkfirst=True)
        for s in (CAB_OO, CAB_IP):
            conn.execute(text(f'drop schema if exists "{s}" cascade'))
        conn.execute(text("truncate app_schema_state"))


# ---------------------------------------------------------------------------
# Эндпоинты кабинетов
# ---------------------------------------------------------------------------
def test_api_cabinets_list_and_select(api_client, db):
    user = cabinet_service.seed_user(db)
    c = _mk_cab(db, user=user)
    r = api_client.get("/api/cabinets")
    assert r.status_code == 200
    data = r.json()
    assert len(data["cabinets"]) == 1
    assert data["cabinets"][0]["id"] == c.id
    r2 = api_client.post("/api/cabinet/select", json={"id": c.id})
    assert r2.status_code == 200
    assert r2.cookies.get("agent_cabinet") == str(c.id)
    db.refresh(user)
    assert user.last_cabinet_id == c.id


def test_api_cabinet_select_unknown_404(api_client, db):
    user = cabinet_service.seed_user(db)
    _mk_cab(db, user=user)
    r = api_client.post("/api/cabinet/select", json={"id": 9999})
    assert r.status_code == 404


def test_api_refresh_all_returns_job_ids(api_client, db, patch_factory):
    user = cabinet_service.seed_user(db)
    _mk_cab(db, user=user, marketplaces="wb")
    r = api_client.post("/api/refresh-all", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    assert all(j.get("job_id") for j in body["jobs"])


# ---------------------------------------------------------------------------
# Фоновое обновление пишет в схему кабинета
# ---------------------------------------------------------------------------
def _wait_job(job_id, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = refresh_service.job_state(job_id)
        if st and st["status"] == "done":
            return st
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} не завершился")


def test_refresh_worker_targets_cabinet_schema(db_engine, patch_factory):
    _drop_cab_schemas()
    with SessionLocal() as db:
        user = cabinet_service.seed_user(db)
        c = _mk_cab(db, code="wrk", schema=CAB_OO, marketplaces="wb", user=user)
    cabinet_migrate.ensure_cabinet_schemas(engine, [c])
    with engine.begin() as conn:  # legacy-запись в public, чтобы ловить «утечку»
        conn.execute(text(
            "insert into public.refresh_runs (api, status, started_at, results) "
            "values ('wb', 'running', now(), '[]')"))
    start = refresh_service.start_refresh(
        "wb", include_detail=False,
        date_from="2026-09-01", date_to="2026-09-10", cab_id=c.id)
    assert start["job_id"]
    st = _wait_job(start["job_id"])
    assert st["failed"] == 0
    assert st["cab_id"] == c.id
    with engine.begin() as conn:
        cab_runs = conn.execute(text(f'select count(*) from "{CAB_OO}".refresh_runs')).scalar()
        pub_runs = conn.execute(text("select count(*) from public.refresh_runs")).scalar()
    assert cab_runs == 1
    assert pub_runs == 1  # legacy-строка осталась в public (новая ушла в схему кабинета)
    with engine.begin() as conn:
        conn.execute(text(f'drop schema if exists "{CAB_OO}" cascade'))
        conn.execute(text("truncate public.refresh_runs"))