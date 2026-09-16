from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def ensure_schema() -> None:
    """Создаёт недостающие таблицы и мягко мигрирует схему при старте."""
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