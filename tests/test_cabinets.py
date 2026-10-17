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


def test_wb_nmid_to_article_reads_cabinet_schema(db_engine):
    """Карта nmID->артикул читается из схемы связки (nm_articles per-cabinet),
    а не из public: иначе остатки/воронка WB падают на nmID вместо vendorCode и
    «наши» запасы на WB кажутся нулевыми. Кэш — по схеме (не протекает между)."""
    _drop_cab_schemas()
    with SessionLocal() as db:
        user = cabinet_service.seed_user(db)
        c1 = _mk_cab(db, code="c1", schema=CAB_OO, marketplace="wb", user=user)
        c2 = _mk_cab(db, code="c2", schema=CAB_IP, marketplace="wb", user=user)
    cabinet_migrate.ensure_cabinet_schemas(engine, [c1, c2])
    with SessionLocal() as db:
        cabinet_service.apply_search_path(db, CAB_OO)
        db.add(models.NmArticle(nm_id="111", article="ART-OO"))
        db.commit()
    with SessionLocal() as db:
        cabinet_service.apply_search_path(db, CAB_IP)
        db.add(models.NmArticle(nm_id="222", article="ART-IP"))
        db.commit()

    refresh_service._wb_nm_cache_reset()
    with SessionLocal() as db:
        cabinet_service.apply_search_path(db, CAB_OO)
        assert refresh_service._wb_nmid_to_article(db) == {"111": "ART-OO"}
    with SessionLocal() as db:
        cabinet_service.apply_search_path(db, CAB_IP)
        assert refresh_service._wb_nmid_to_article(db) == {"222": "ART-IP"}
    refresh_service._wb_nm_cache_reset()


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


def test_api_cabinet_main_set_and_clear(api_client, db):
    _drop_cab_schemas()
    user = cabinet_service.seed_user(db)
    _mk_cab(db, code="mc1", schema=CAB_OO, user=user, marketplace="wb")
    c2 = _mk_cab(db, code="mc2", schema=CAB_IP, user=user, marketplace="wb")
    r = api_client.post("/api/cabinet/main", json={"id": c2.id})
    assert r.status_code == 200
    assert r.json()["main"] == c2.id
    db.refresh(user)
    assert user.main_cabinet_id == c2.id
    body = api_client.get("/api/cabinets").json()
    assert body["main"] == c2.id
    assert body["user"]["main_cabinet_id"] == c2.id
    # снятие главной связки
    r2 = api_client.post("/api/cabinet/main", json={"id": None})
    assert r2.status_code == 200
    assert r2.json()["main"] is None
    db.refresh(user)
    assert user.main_cabinet_id is None


def test_api_cabinet_main_unknown_400(api_client, db):
    _drop_cab_schemas()
    user = cabinet_service.seed_user(db)
    _mk_cab(db, code="mcx", schema=CAB_OO, user=user)
    r = api_client.post("/api/cabinet/main", json={"id": 999999})
    assert r.status_code == 400


def test_api_cabinets_reports_active_after_select(api_client, db):
    _drop_cab_schemas()
    user = cabinet_service.seed_user(db)
    _mk_cab(db, code="ac1", schema=CAB_OO, user=user, marketplace="wb")
    c2 = _mk_cab(db, code="ac2", schema=CAB_IP, user=user, marketplace="ozon")
    # активная резолвится из куки/last_cabinet_id, а не из contextvar
    assert api_client.post("/api/cabinet/select", json={"id": c2.id}).status_code == 200
    body = api_client.get("/api/cabinets").json()
    assert body["current"] is not None
    assert body["current"]["id"] == c2.id
    assert body["current"]["marketplace"] == "ozon"
    links = {lk["code"]: lk for comp in body["companies"] for lk in comp["links"]}
    assert links["ac2"]["active"] is True
    assert links["ac1"]["active"] is False


def test_api_cabinet_settings_get_and_update(api_client, db):
    user = cabinet_service.seed_user(db)
    creds = secrets.encrypt({"wb": {"standard": "abc12345secret", "finance": "fin"}})
    c = _mk_cab(db, user=user, marketplace="wb", creds=creds)
    data = api_client.get(f"/api/cabinet/{c.id}/settings").json()
    assert data["marketplace"] == "wb"
    assert data["has_keys"] is True
    by_key = {f["key"]: f for f in data["fields"]}
    assert set(by_key) == {"standard", "finance", "finance2"}
    assert by_key["standard"]["set"] is True
    assert by_key["standard"]["secret"] is True
    assert by_key["standard"]["masked"] == "abc1…cret"
    assert by_key["finance2"]["set"] is False
    # обновление: новый токен + удаление finance + выключение связки (лишний ключ игнорим)
    r = api_client.put(f"/api/cabinet/{c.id}/settings",
                       json={"creds": {"standard": "new-token-value", "finance": "",
                                       "bogus": "x"},
                             "enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    db.expire_all()
    stored = secrets.decrypt(db.execute(
        text("select creds from cabinets where id=:i"), {"i": c.id}).scalar())
    assert stored == {"wb": {"standard": "new-token-value"}}
    # повторное чтение через API тоже видит новое значение (finance удалён)
    data2 = api_client.get(f"/api/cabinet/{c.id}/settings").json()
    by_key2 = {f["key"]: f for f in data2["fields"]}
    assert by_key2["standard"]["set"] is True
    assert by_key2["standard"]["masked"] == "new-…alue"
    assert by_key2["finance"]["set"] is False
    assert by_key2["finance2"]["set"] is False


def test_api_cabinet_settings_unknown_404(api_client, db):
    cabinet_service.seed_user(db)
    _mk_cab(db)
    assert api_client.get("/api/cabinet/9999/settings").status_code == 404
    r = api_client.put("/api/cabinet/9999/settings", json={"creds": {}})
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


def test_replenish_cabinet_param_reads_selected_schema(db_engine, api_client, db):
    """`cabinet=` в /replenish читает схему выбранной связки, а не активной."""
    _drop_cab_schemas()
    from datetime import date

    with SessionLocal() as sess:
        user = cabinet_service.seed_user(sess)
        comp = _mk_company(sess, name="Фирма Р")
        c1 = _mk_cab(sess, code="rp1", name="Связка А", schema=CAB_OO,
                     marketplace="wb", company=comp, user=user)
        c2 = _mk_cab(sess, code="rp2", name="Связка Б", schema=CAB_IP,
                     marketplace="wb", company=comp, user=user)
    cabinet_migrate.ensure_cabinet_schemas(engine, [c1, c2])
    # В схеме первой связки — карточка и остаток МП по артикулу «X».
    with SessionLocal() as sess:
        cabinet_service.apply_search_path(sess, CAB_OO)
        wb_id = sess.execute(text("select id from marketplaces where code='wb'")).scalar()
        sess.add(models.MarketplaceCard(marketplace_id=wb_id, chrt_id="111",
                                        vendor_code="X", barcode="bc1"))
        sess.add(models.Stock(marketplace_id=wb_id, date=date(2026, 9, 1), article="X",
                              warehouse="Все", quantity=5, quantity_full=5, in_way=0))
        sess.commit()

    win = {"date_to": "2026-09-30", "window_days": 30}
    # Активная связка Б (пустая схема) — «X» не виден.
    r0 = api_client.get("/api/replenish", params=win,
                        cookies={"agent_cabinet": str(c2.id)})
    assert r0.status_code == 200
    assert "X" not in {row["article"] for row in r0.json()["rows"]}
    assert r0.json().get("cabinet") is None

    # Локальный выбор связки А — данные из её схемы.
    r1 = api_client.get("/api/replenish", params={**win, "cabinet": c1.id})
    assert r1.status_code == 200
    body = r1.json()
    cab = body.get("cabinet") or {}
    assert (cab.get("id"), cab.get("schema")) == (c1.id, CAB_OO)
    assert cab.get("owner", "").startswith("Фирма Р")
    assert "X" in {row["article"] for row in body["rows"]}

    # Неизвестная связка — 404.
    assert api_client.get("/api/replenish",
                          params={"cabinet": 999999}).status_code == 404

    db.rollback()  # снять ACCESS SHARE, иначе DROP SCHEMA ждёт нашу транзакцию
    with engine.begin() as conn:
        for s in (CAB_OO, CAB_IP):
            conn.execute(text(f'drop schema if exists "{s}" cascade'))
        Base.metadata.create_all(conn, checkfirst=True)


def test_wb_oz_creds_fall_back_to_session_active_cabinet(db):
    """_wb_creds/_oz_creds берут ключи активной связки из сессии, если contextvar пуст.

    Регресс: get_db и эндпоинт выполняются в разных тредах threadpool FastAPI, и
    contextvar активной связки туда не попадает. Раньше `_wb_creds()` (только
    contextvar) возвращал пусто → провайдер молча уходил на .env-ключ другого
    продавца, и «Применить в WB» падал с 400 «All item Nos. are specified
    incorrectly» (nmID чужой связки).
    """
    from app.api._common import _oz_creds, _wb_creds
    from app.services.cabinets import reset_active

    reset_active(None)  # contextvar пуст, как в треде эндпоинта
    creds = secrets.encrypt({
        "wb": {"standard": "cab-wb-token"},
        "ozon": {"client_id": "cid", "api_key": "oz-key"},
    })
    c = _mk_cab(db, code="creds1", marketplace="wb", creds=creds)
    info = cabinet_service.resolve_active(db, str(c.id))
    db._active_info = info  # как кладёт get_db

    assert _wb_creds(db) == {"standard": "cab-wb-token"}
    assert _oz_creds(db) == {"client_id": "cid", "api_key": "oz-key"}
    # без сессии (contextvar пуст) — None: провайдер ушёл бы на .env
    assert _wb_creds() is None
    assert _oz_creds() is None


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


# ---------------------------------------------------------------------------
# Эндпоинт /promo/refresh пиннит search_path (регресс UndefinedTable)
# ---------------------------------------------------------------------------
def test_promo_refresh_endpoint_pins_cabinet_schema(db_engine, patch_factory, monkeypatch):
    """/promo/refresh коммитит внутри pull_wb_promotions и затем читает
    wb_promo_nomenclatures. Без pin search_path терялся → 500 UndefinedTable.
    Здесь идёт реальный get_db (не override), поэтому проверяем и то, что связка
    пиннится, и то, что эндпоинт отвечает 200."""
    from fastapi.testclient import TestClient

    from app.main import app

    _drop_cab_schemas()
    with SessionLocal() as db:
        user = cabinet_service.seed_user(db)
        c = _mk_cab(db, code="prm", schema=CAB_OO, marketplace="wb", user=user,
                    creds=secrets.encrypt({"wb": {"standard": "x", "finance": "y"}}))
    cabinet_migrate.ensure_cabinet_schemas(engine, [c])

    pinned = []
    real_pin = cabinet_service.pin_search_path

    def _spy(db, schema):
        pinned.append(schema)
        return real_pin(db, schema)

    monkeypatch.setattr(cabinet_service, "pin_search_path", _spy)

    with TestClient(app) as client:
        r = client.post("/api/promo/refresh", cookies={"agent_cabinet": str(c.id)})
    assert r.status_code == 200, r.text
    assert r.json()["db_rows"] >= 1
    assert CAB_OO in pinned

    with engine.begin() as conn:
        conn.execute(text(f'drop schema if exists "{CAB_OO}" cascade'))
        Base.metadata.create_all(conn, checkfirst=True)
        conn.execute(text(
            "truncate refresh_runs, wb_promotions, wb_promo_nomenclatures "
            "restart identity cascade"))