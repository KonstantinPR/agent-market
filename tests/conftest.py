"""Общая оснастка pytest.

- тесты работают с отдельной БД <PG_DATABASE> (default agent_market_test),
  рабочая agent_market не затрагивается;
- функции-фикстуры выдают изолированные сессии (данные чистятся между тестами);
- клиент TestClient использует те же сессии (override get_db);
- провайдеры маркетплейсов заменяются фейками (tests/fakes.py) через фабрику.
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("PG_DATABASE", "agent_market_test")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # корень проекта, чтобы импортировать app

import psycopg2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import models
from app.config import settings
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.providers import factory as provider_factory
from app.services import refresh as refresh_service

from tests.fakes import FakeOz, FakeWb

TABLES = ", ".join([
    "marketplaces", "products", "sales", "stocks", "custom_stock",
    "funnel_metric", "nm_articles", "api_pulls", "refresh_runs",
    "price_changes", "marketplace_cards", "price_snapshots", "storage_costs",
    "wb_detail_rows", "ozon_detail_rows", "ozon_buyouts",
    "ozon_accruals", "ozon_placements",
    "counterparties", "warehouse_docs", "warehouse_doc_items",
    "product_sizes", "product_aliases", "wb_promotions",
    "users", "cabinets",
])


def _ensure_test_db():
    try:
        conn = psycopg2.connect(
            host=settings.pg_host, port=settings.pg_port,
            user=settings.pg_user, password=settings.pg_password,
            dbname="postgres",
        )
    except psycopg2.OperationalError as exc:  # pragma: no cover
        pytest.skip(f"Postgres недоступен: {exc}")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("select 1 from pg_database where datname = %s", (settings.pg_database,))
        if cur.fetchone() is None:
            cur.execute(f'create database {settings.pg_database}')
    conn.close()


@pytest.fixture(scope="session")
def db_engine():
    _ensure_test_db()
    with engine.begin() as conn:
        rows = conn.execute(text(
            "select schema_name from information_schema.schemata "
            "where schema_name like 'cab_%'"
        )).scalars().all()
        for s in rows:
            conn.execute(text(f'drop schema if exists "{s}" cascade'))
        conn.execute(text("drop table if exists app_schema_state cascade"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine


@pytest.fixture()
def db(db_engine):
    with engine.begin() as conn:
        conn.execute(text(f"truncate {TABLES} restart identity cascade"))
    session = SessionLocal()
    try:
        session.add_all([
            models.Marketplace(code="wb", name="Wildberries"),
            models.Marketplace(code="ozon", name="Ozon"),
        ])
        session.commit()
        yield session
    finally:
        session.close()


@pytest.fixture()
def stub_wb():
    return FakeWb()


@pytest.fixture()
def stub_oz():
    return FakeOz()


@pytest.fixture()
def patch_factory(stub_wb, stub_oz, monkeypatch):
    monkeypatch.setattr(provider_factory, "get_wb_provider", lambda with_fail_fast=False, **kw: stub_wb)
    monkeypatch.setattr(provider_factory, "get_oz_provider", lambda with_fail_fast=False, **kw: stub_oz)
    yield


@pytest.fixture()
def client(db):
    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def api_client(client, patch_factory):
    yield client


@pytest.fixture(autouse=True)
def _clean_refresh_state():
    """Гарантирует пустое состояние фоновых заданий перед каждым тестом."""
    for _ in range(200):
        with refresh_service._LOCK:
            if not any(refresh_service._RUNNING.values()):
                break
        time.sleep(0.05)
    refresh_service._RUNNING = {}
    refresh_service._PENDING = {}
    refresh_service._BULK_PROV = {}
    refresh_service._JOBS = {}
    yield


# Фикстуры, которым нужна БД/клиент: такие тесты не входят в `-m fast`.
_DB_FIXTURES = {"db", "db_engine", "client", "api_client", "pdf_api"}


def pytest_collection_modifyitems(items):
    """Проставляет маркер fast тестам, не трогающим БД (`pytest -m fast` < 20 с)."""
    for item in items:
        path = str(item.fspath)
        if "\\tests\\api\\" in path or "\\tests\\e2e\\" in path:
            continue
        if "/tests/api/" in path or "/tests/e2e/" in path:
            continue
        names = set(getattr(item, "fixturenames", ()) or ())
        if names & _DB_FIXTURES:
            continue
        item.add_marker(pytest.mark.fast)