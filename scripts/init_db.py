"""Создаёт БД agent_market (если нет), таблицы и записи маркетплейсов."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2 import sql
from sqlalchemy import select

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