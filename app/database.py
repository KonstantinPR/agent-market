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
        ]:
            conn.execute(text(
                f"ALTER TABLE {col} ADD COLUMN IF NOT EXISTS {ddl}"
            ))
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
            "raw_json TEXT",
        ]:
            conn.execute(text(
                f"ALTER TABLE funnel_metric ADD COLUMN IF NOT EXISTS {ddl}"
            ))
    if seed:
        _seed_marketplace_counterparties()


def _seed_marketplace_counterparties() -> None:
    """WB и Ozon заводятся как контрагенты-маркетплейсы сразу (если справочник пуст)."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()