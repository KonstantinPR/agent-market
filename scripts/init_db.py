"""Создаёт БД agent_market (если нет), таблицы и записи маркетплейсов."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2 import sql
from sqlalchemy import select, text

from app.config import settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app import models  # noqa: E402


def create_database():
    conn = psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        user=settings.pg_user,
        password=settings.pg_password,
        dbname="postgres",
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (settings.pg_database,))
        if cur.fetchone():
            print(f"БД '{settings.pg_database}' уже существует")
        else:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(settings.pg_database)))
            print(f"БД '{settings.pg_database}' создана")
    conn.close()


def create_tables():
    Base.metadata.create_all(bind=engine)
    # Миграции для существующих таблиц (create_all не добавляет колонки)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS replenishable boolean DEFAULT false"))
        # Фаза C: каталог «Наш склад → Товары» — поля из карточек WB/Ozon
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS subject varchar(200) DEFAULT ''"))
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS volume_l numeric(10,3) DEFAULT 0"))
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS composition text DEFAULT ''"))
        # Фаза B: остатки по размерам
        conn.execute(text("ALTER TABLE stocks ADD COLUMN IF NOT EXISTS chrt_id varchar(40) DEFAULT ''"))
        conn.execute(text("ALTER TABLE stocks ADD COLUMN IF NOT EXISTS size varchar(50) DEFAULT ''"))
        conn.execute(text("ALTER TABLE stocks ADD COLUMN IF NOT EXISTS barcode varchar(100) DEFAULT ''"))
        conn.execute(text("ALTER TABLE stocks ADD COLUMN IF NOT EXISTS quantity_full integer DEFAULT 0"))
        conn.execute(text("ALTER TABLE stocks ADD COLUMN IF NOT EXISTS in_way integer DEFAULT 0"))
        # Фаза A: воронка — выкупы и сумма выкупа
        conn.execute(text("ALTER TABLE funnel_metric ADD COLUMN IF NOT EXISTS buyouts integer DEFAULT 0"))
        conn.execute(text("ALTER TABLE funnel_metric ADD COLUMN IF NOT EXISTS buyout_sum numeric(14,2) DEFAULT 0"))
        # Фаза A2: воронка — полный набор полей отчёта (title, subjectId, tags, past, comparison)
        conn.execute(text("ALTER TABLE funnel_metric ADD COLUMN IF NOT EXISTS title varchar(250) DEFAULT ''"))
        conn.execute(text("ALTER TABLE funnel_metric ADD COLUMN IF NOT EXISTS subject_id varchar(40) DEFAULT ''"))
        conn.execute(text("ALTER TABLE funnel_metric ADD COLUMN IF NOT EXISTS tags text DEFAULT ''"))
        conn.execute(text("ALTER TABLE funnel_metric ADD COLUMN IF NOT EXISTS past_json text"))
        conn.execute(text("ALTER TABLE funnel_metric ADD COLUMN IF NOT EXISTS comparison_json text"))
        conn.execute(text("ALTER TABLE stocks DROP CONSTRAINT IF EXISTS uq_stocks_market_date_article_wh"))
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint
                               WHERE conname = 'uq_stocks_market_date_article_wh_chrt') THEN
                    ALTER TABLE stocks ADD CONSTRAINT uq_stocks_market_date_article_wh_chrt
                        UNIQUE (marketplace_id, date, article, warehouse, chrt_id);
                END IF;
            END $$;
        """))
    print("Таблицы созданы/синхронизированы")


def seed_marketplaces():
    with SessionLocal() as db:
        existing = db.execute(select(models.Marketplace.code)).scalars().all()
        to_add = {"wb": "Wildberries", "ozon": "Ozon"}
        for code, name in to_add.items():
            if code not in existing:
                db.add(models.Marketplace(code=code, name=name))
                print(f"Маркетплейс '{name}' добавлен")
        db.commit()


if __name__ == "__main__":
    print(f"Хост: {settings.pg_host}:{settings.pg_port}, БД: {settings.pg_database}")
    create_database()
    create_tables()
    seed_marketplaces()
    print("Готово.")