
from fastapi import Request
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def ensure_schema(seed: bool = True) -> None:
    """Создаёт недостающие таблицы и мягко мигрирует схему при старте.

    seed=False — только схема, без сидинга контрагентов (для штатного старта
    приложения per-запуск; сидинг при __main__ и для ручного вызова).
    """
    from app import models  # noqa: F401 — регистрация таблиц

    Base.metadata.create_all(bind=engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE price_snapshots "
            "ADD COLUMN IF NOT EXISTS marketplace VARCHAR(10) NOT NULL DEFAULT 'wb'"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_price_snapshots_marketplace "
            "ON price_snapshots (marketplace)"
        ))
        for col, ddl in [
            ("wb_detail_rows", "delivery_count INTEGER NOT NULL DEFAULT 0"),
            ("wb_detail_rows", "return_delivery_count INTEGER NOT NULL DEFAULT 0"),
            ("wb_detail_rows", "pvz_compensation NUMERIC(14, 2) NOT NULL DEFAULT 0"),
            ("wb_detail_rows", "payment_services NUMERIC(14, 2) NOT NULL DEFAULT 0"),
            # T-17: новые поля каталога товаров (create_all их к существующей таблице не добавляет)
            ("products", "subject VARCHAR(200) NOT NULL DEFAULT ''"),
            ("products", "volume_l NUMERIC(10, 3) NOT NULL DEFAULT 0"),
            ("products", "composition TEXT NOT NULL DEFAULT ''"),
        ]:
            conn.execute(text(
                f"ALTER TABLE {col} ADD COLUMN IF NOT EXISTS {ddl}"
            ))
        # Группировка артикулов Ozon: базовый артикул (товар) + размер.
        # Артикул Ozon = артикул товара + размер через последний «-»,
        # см. app/services/ozon_article.py. Заполняется при синке и бэкфилле
        # (scripts/backfill_ozon_articles.py).
        for tbl, base_type in (
            ("ozon_detail_rows", "varchar(100)"),
            ("ozon_accruals", "varchar(100)"),
            ("ozon_placements", "varchar(100)"),
            ("ozon_buyouts", "varchar(100)"),
            ("marketplace_cards", "varchar(200)"),
            ("stocks", "varchar(100)"),
            ("price_snapshots", "varchar(100)"),
        ):
            conn.execute(text(
                f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS base_article {base_type} DEFAULT ''"
            ))
            conn.execute(text(
                f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS size varchar(50) DEFAULT ''"
            ))
        for ddl in [
            "CREATE INDEX IF NOT EXISTS ix_ozon_detail_base_article "
            "ON ozon_detail_rows (base_article)",
            "CREATE INDEX IF NOT EXISTS ix_ozon_accrual_base_article "
            "ON ozon_accruals (base_article)",
            "CREATE INDEX IF NOT EXISTS ix_ozon_placement_base_article "
            "ON ozon_placements (base_article)",
            "CREATE INDEX IF NOT EXISTS ix_ozon_buyout_base_article "
            "ON ozon_buyouts (base_article)",
            "CREATE INDEX IF NOT EXISTS ix_mp_cards_base_article "
            "ON marketplace_cards (base_article)",
            "CREATE INDEX IF NOT EXISTS ix_stocks_base_article "
            "ON stocks (base_article)",
            "CREATE INDEX IF NOT EXISTS ix_price_snap_base_article "
            "ON price_snapshots (base_article)",
        ]:
            conn.execute(text(ddl))
        for ddl in [
            "subject_name VARCHAR(80) NOT NULL DEFAULT ''",
            "brand_name VARCHAR(120) NOT NULL DEFAULT ''",
            "product_rating NUMERIC(4, 1) NOT NULL DEFAULT 0",
            "feedback_rating NUMERIC(4, 2) NOT NULL DEFAULT 0",
            "stock_wb INTEGER NOT NULL DEFAULT 0",
            "stock_mp INTEGER NOT NULL DEFAULT 0",
            "stock_balance_sum NUMERIC(14, 2) NOT NULL DEFAULT 0",
            "cancel_sum NUMERIC(14, 2) NOT NULL DEFAULT 0",
            "avg_orders_per_day NUMERIC(10, 3) NOT NULL DEFAULT 0",
            "share_order_percent NUMERIC(8, 3) NOT NULL DEFAULT 0",
            "add_to_wishlist INTEGER NOT NULL DEFAULT 0",
            "time_to_ready_min INTEGER NOT NULL DEFAULT 0",
            "localization_percent NUMERIC(8, 3) NOT NULL DEFAULT 0",
            "conv_to_cart_percent NUMERIC(8, 3) NOT NULL DEFAULT 0",
            "conv_cart_to_order_percent NUMERIC(8, 3) NOT NULL DEFAULT 0",
            "conv_buyout_percent NUMERIC(8, 3) NOT NULL DEFAULT 0",
            "wb_club_order_count INTEGER NOT NULL DEFAULT 0",
            "wb_club_order_sum NUMERIC(14, 2) NOT NULL DEFAULT 0",
            "wb_club_buyout_count INTEGER NOT NULL DEFAULT 0",
            "wb_club_buyout_sum NUMERIC(14, 2) NOT NULL DEFAULT 0",
            "wb_club_cancel_count INTEGER NOT NULL DEFAULT 0",
            "wb_club_cancel_sum NUMERIC(14, 2) NOT NULL DEFAULT 0",
            "wb_club_avg_price NUMERIC(14, 2) NOT NULL DEFAULT 0",
            "wb_club_buyout_percent NUMERIC(8, 3) NOT NULL DEFAULT 0",
            "wb_club_avg_orders_per_day NUMERIC(10, 3) NOT NULL DEFAULT 0",
            "title VARCHAR(250) NOT NULL DEFAULT ''",
            "subject_id VARCHAR(40) NOT NULL DEFAULT ''",
            "tags TEXT NOT NULL DEFAULT ''",
            "past_json TEXT",
            "comparison_json TEXT",
            "raw_json TEXT",
        ]:
            conn.execute(text(
                f"ALTER TABLE funnel_metric ADD COLUMN IF NOT EXISTS {ddl}"
            ))
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS app_schema_state (key text PRIMARY KEY, value text)"
        ))
    _ensure_cabinets(seed=seed)
    if seed:
        _seed_marketplace_counterparties()


def _ensure_cabinets(seed: bool) -> None:
    """Схемы кабинетов, одноразовая раскатка и уборка пустых теней из public.

    При seed=False кабинеты не создаются, а только достраиваются схемы и
    мигрируются данные для уже существующих (штатный старт сервера).
    """
    from sqlalchemy import select

    from app import models
    from app.services import cabinet_migrate
    from app.services.cabinets import list_cabinets, seed_users_and_cabinets

    with SessionLocal() as db:
        cabs = seed_users_and_cabinets(db) if seed else []
        if not seed:
            user = db.execute(select(models.User).limit(1)).scalars().first()
            if user is not None:
                cabs = list_cabinets(db, user)
    if not cabs:
        return
    cabinet_migrate.ensure_cabinet_schemas(engine, cabs)
    cabinet_migrate.migrate_public_data(engine, cabs)
    cabinet_migrate.cleanup_public_shadows(engine)


def _seed_marketplace_counterparties() -> None:
    """WB и Ozon заводятся как контрагенты-маркетплейсы сразу (если справочник пуст)."""
    from sqlalchemy import select

    from app import models

    with SessionLocal() as db:
        count = db.execute(select(models.Counterparty).limit(1)).first()
        if count is not None:
            return
        db.add_all([
            models.Counterparty(name="Wildberries", ctype="marketplace", inn="7707323467", note="WB"),
            models.Counterparty(name="Ozon", ctype="marketplace", inn="7728567110", note="Ozon"),
        ])
        db.commit()


def get_db(request: Request = None):
    """Сессия БД. Если в запросе есть кука agent_cabinet — открывает сессию
    с `SET search_path TO "<schema_кабинета>", public` и выставляет активный
    кабинет в contextvar (см. app/services/cabinets.py).

    Вне HTTP (скрипты/тесты) активного кабинета нет — работа с общим public.
    """
    from app.services.cabinets import (
        CABINET_COOKIE, apply_search_path, resolve_active, set_active,
    )

    db = SessionLocal()
    search_path_applied = False
    try:
        if request is not None:
            info = resolve_active(db, request.cookies.get(CABINET_COOKIE))
            if info is not None and info.schema:
                apply_search_path(db, info.schema)
                search_path_applied = True
            set_active(info)
        yield db
    finally:
        if search_path_applied:
            try:
                db.execute(text("RESET search_path"))
            except Exception:  # noqa: BLE001
                pass
        db.close()