# -*- coding: utf-8 -*-
"""Личные кабинеты-связки: схемы, миграция, поиск по схеме, эндпоинты, refresh по связке."""
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.database import Base, SessionLocal, engine
from app.services import cabinet_migrate
from app.services import cabinets as cabinet_service
from app.services import refresh as refresh_service
from app.services import secrets
from app.services.cabinets import seed_users_and_cabinets

CAB_OO = "cab_oo_test"
CAB_IP = "cab_ip_test"


def _mk_company(db: Session, name="Фирма тест", position=9) -> models.Company:
    user = cabinet_service.seed_user(db)
    c = models.Company(user_id=user.id, name=name, enabled=True, position=position)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _mk_cab(db: Session, code="cab1", name="Кабинет", schema=CAB_OO,
            marketplace="wb", company=None, creds="", user=None) -> models.Cabinet:
    user = user or cabinet_service.seed_user(db)
    if company is None:
        company = _mk_company(db)
    c = models.Cabinet(user_id=user.id, company_id=company.id, code=code, name=name,
                       schema=schema, marketplace=marketplace, creds=creds,
                       enabled=True, position=9)
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
            "users, cabinets, companies restart identity cascade"))
        conn.execute(text(
            "insert into marketplaces (code, name) values ('wb', 'WB'), ('ozon', 'Ozon')"))


# ---------------------------------------------------------------------------
# Сиды
# ---------------------------------------------------------------------------
def test_seed_users_and_cabinets_creates_three_links(db_engine):
    _drop_cab_schemas()
    with SessionLocal() as db:
        cabs = seed_users_and_cabinets(db)
        assert len(cabs) == 3
        assert {c.code for c in cabs} == {"oo_joinco", "ip_prudnikov", "ip_prudnikov_wb"}
        by_code = {c.code: c for c in cabs}
        assert by_code["oo_joinco"].marketplace == "wb"
        assert by_code["ip_prudnikov"].marketplace == "ozon"
        assert by_code["ip_prudnikov_wb"].marketplace == "wb"
        assert by_code["ip_prudnikov_wb"].schema == "cab_ip_wb"
        user = cabinet_service.seed_user(db)
        assert user.last_cabinet_id is not None
        # связки сгруппированы по фирмам
        comps = cabinet_service.companies_of(db, user)
        names = {c.name for c in comps}
        assert "ООО «Джоинс & Компани»" in names
        assert "ИП Прудников Константин Григорьевич" in names
        ip_links = [c for c in cabs if c.company_id == by_code["ip_prudnikov"].company_id]
        assert {c.marketplace for c in ip_links} == {"ozon", "wb"}


# ---------------------------------------------------------------------------
# Схема связки и поиск по search_path
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
        c1 = _mk_cab(db, code="c1", schema=CAB_OO, marketplace="wb", user=user)
        c2 = _mk_cab(db, code="c2", schema=CAB_IP, marketplace="ozon", user=user)
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
        c1 = _mk_cab(db, code="c1", schema=CAB_OO, marketplace="wb", user=user)
        c2 = _mk_cab(db, code="c2", schema=CAB_IP, marketplace="ozon", user=user)
        wb_id = db.execute(text("select id from marketplaces where code='wb'")).scalar()
        oz_id = db.execute(text("select id from marketplaces where code='ozon'")).scalar()
        db.add_all([
            models.Sale(date=date(2026, 9, 1), marketplace_id=wb_id, article="A1",
                        quantity=2, revenue=100, income=90),
            models.Sale(date=date(2026, 9, 1), marketplace_id=oz_id, article="O1",
                        quantity=1, revenue=50, income=45),
        ])
        db.commit()
    # продажи идут в схемы связок (без drop_public), миграция раскатывает
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
# Эндпоинты связок
# ---------------------------------------------------------------------------
def test_api_cabinets_list_and_select(api_client, db):
    user = cabinet_service.seed_user(db)
    comp = _mk_company(db, name="Фирма А")
    c = _mk_cab(db, company=comp)
    r = api_client.get("/api/cabinets")
    assert r.status_code == 200
    data = r.json()
    assert len(data["companies"]) == 1
    comps = [x for x in data["companies"] if x["name"] == "Фирма А"]
    assert len(comps) == 1
    assert len(comps[0]["links"]) == 1
    link = comps[0]["links"][0]
    assert link["id"] == c.id
    assert link["marketplace"] == "wb"
    assert "Владелец" not in link  # owner строится из имени фирмы · МП
    assert "·" in link["owner"]
    r2 = api_client.post("/api/cabinet/select", json={"id": c.id})
    assert r2.status_code == 200
    assert r2.cookies.get("agent_cabinet") == str(c.id)
    db.refresh(user)
    assert user.last_cabinet_id == c.id
    # текущая связка в /api/cabinet/current тоже с owner
    r3 = api_client.get("/api/cabinet/current")
    assert r3.json()["owner"].startswith("Фирма А")


def test_api_cabinet_select_unknown_404(api_client, db):
    user = cabinet_service.seed_user(db)
    _mk_cab(db, user=user)
    r = api_client.post("/api/cabinet/select", json={"id": 9999})
    assert r.status_code == 404


def test_api_refresh_all_returns_job_ids(api_client, db, patch_factory):
    user = cabinet_service.seed_user(db)
    creds = secrets.encrypt({"wb": {"standard": "x", "finance": "y", "finance2": "z"}})
    _mk_cab(db, user=user, marketplace="wb", creds=creds)
    r = api_client.post("/api/refresh-all", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    assert all(j.get("job_id") for j in body["jobs"])
    # ждём завершения фоновых воркеров, пока патч провайдеров ещё активен
    for j in body["jobs"]:
        _wait_job(j["job_id"], timeout=30)


def test_api_refresh_skips_link_without_keys(api_client, db):
    user = cabinet_service.seed_user(db)
    c = _mk_cab(db, user=user, marketplace="wb", creds="")
    r = api_client.post("/api/refresh", params={"api": "wb", "cab_id": c.id})
    assert r.status_code == 400
    assert "ключей" in r.json().get("detail", "")


def test_api_pricing_settings_roundtrip_and_copy(api_client, db):
    _drop_cab_schemas()
    user = cabinet_service.seed_user(db)
    comp = _mk_company(db, name="Фирма А")
    c1 = _mk_cab(db, code="ps1", name="Каб 1", schema=CAB_OO, company=comp, user=user)
    c2 = _mk_cab(db, code="ps2", name="Каб 2", schema=CAB_IP, company=comp, user=user)
    assert api_client.post("/api/cabinet/select", json={"id": c1.id}).status_code == 200
    # нет ключей-связки или пустые настройки — отдаёт {}
    assert api_client.get("/api/cabinet/pricing-settings").json()["settings"] == {}
    payload = {"mode": "new", "markup": 5, "nested": {"a": 1}}
    r = api_client.put("/api/cabinet/pricing-settings", json={"settings": payload})
    assert r.status_code == 200
    assert r.json()["settings"] == payload
    assert api_client.get("/api/cabinet/pricing-settings").json()["settings"] == payload
    # копирование во все связки пользователя (источник не считается)
    r = api_client.post("/api/cabinet/pricing-settings/copy", json={})
    assert r.status_code == 200
    assert r.json()["copied"] == 1
    db.expire_all()
    assert cabinet_service.get_pricing_settings(db, c1.id) == payload
    assert cabinet_service.get_pricing_settings(db, c2.id) == payload



# ---------------------------------------------------------------------------
# Сравнение между связками (dashboard, margin/detail)
# ---------------------------------------------------------------------------
def test_dashboard_links_merges_two_cabinets(db_engine, api_client, db):
    _drop_cab_schemas()
    from datetime import date

    with SessionLocal() as sess:
        user = cabinet_service.seed_user(sess)
        comp1 = _mk_company(sess, name="Фирма А")
        comp2 = _mk_company(sess, name="Фирма Б")
        c1 = _mk_cab(sess, code="ln1", name="Связка 1", schema=CAB_OO,
                     marketplace="wb", company=comp1, user=user)
        c2 = _mk_cab(sess, code="ln2", name="Связка 2", schema=CAB_IP,
                     marketplace="wb", company=comp2, user=user)
        wb_id = sess.execute(text("select id from marketplaces where code='wb'")).scalar()
        sess.add_all([
            models.Sale(date=date(2026, 9, 1), marketplace_id=wb_id, article="A1",
                        quantity=2, revenue=100, income=90),
            models.Sale(date=date(2026, 9, 1), marketplace_id=wb_id, article="A2",
                        quantity=3, revenue=150, income=140),
        ])
        sess.commit()
    cabinet_migrate.ensure_cabinet_schemas(engine, [c1, c2])
    # перенесём продажи в схемы связок напрямую (миграция public не нужна)
    with engine.begin() as conn:
        conn.execute(text(
            f'insert into "{CAB_OO}".sales '
            "(marketplace_id, date, article, source, quantity, returns_qty, revenue, "
            "commission, logistics, storage, services, income) "
            "select marketplace_id, date, article, source, quantity, "
            "coalesce(returns_qty,0), revenue, coalesce(commission,0), "
            "coalesce(logistics,0), coalesce(storage,0), coalesce(services,0), income "
            "from public.sales where article='A1'"))
        conn.execute(text(
            f'insert into "{CAB_IP}".sales '
            "(marketplace_id, date, article, source, quantity, returns_qty, revenue, "
            "commission, logistics, storage, services, income) "
            "select marketplace_id, date, article, source, quantity, "
            "coalesce(returns_qty,0), revenue, coalesce(commission,0), "
            "coalesce(logistics,0), coalesce(storage,0), coalesce(services,0), income "
            "from public.sales where article='A2'"))
    r = api_client.get(
        "/api/dashboard", params={"date_from": "2026-09-01", "date_to": "2026-09-30",
                                  "marketplace": "wb", "links": f"{c1.id},{c2.id}"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["kpis"]["per_mp"]) == 2
    owners = {p.get("owner", "") for p in data["kpis"]["per_mp"]}
    assert len(owners) == 2
    # слияние данных таблицы Продажи двух связок (сумма по строкам)
    total = data["total"]
    assert total["sells"] == 5
    assert total["revenue"] == 250
    assert total["income"] == 230
    assert len(data["daily"]) == 1
    assert data["daily"][0]["sells"] == 5
    # строки топов снабжены владельцем
    assert all(r.get("owner") for r in data["tops"]["profit"]["rows"])
    db.rollback()  # освобождаем транзакцию фикстуры (иначе DROP SCHEMA ждёт её локи)

    with engine.begin() as conn:
        for s in (CAB_OO, CAB_IP):
            conn.execute(text(f'drop schema if exists "{s}" cascade'))
            Base.metadata.create_all(conn, checkfirst=True)


# ---------------------------------------------------------------------------
# Фоновое обновление пишет в схему связки
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
        c = _mk_cab(db, code="wrk", schema=CAB_OO, marketplace="wb", user=user)
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
    assert st["failed"] == 0, st["steps"]
    assert st["cab_id"] == c.id
    with engine.begin() as conn:
        cab_runs = conn.execute(text(f'select count(*) from "{CAB_OO}".refresh_runs')).scalar()
        pub_runs = conn.execute(text("select count(*) from public.refresh_runs")).scalar()
    assert cab_runs == 1
    assert pub_runs == 1  # legacy-строка осталась в public (новая ушла в схему связки)
    with engine.begin() as conn:
        conn.execute(text(f'drop schema if exists "{CAB_OO}" cascade'))
        conn.execute(text("truncate public.refresh_runs"))