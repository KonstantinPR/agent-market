"""Связки «фирма × маркетплейс»: компании + перестройка cabinets (T-41).

Как пользоваться:
    python -m scripts.migrate_links            # dry-run: показать план
    python -m scripts.migrate_links --apply     # выполнить миграцию

Что делает (идемпотентно):
  1. создаёт таблицу companies и фирмы пользователя (ООО, ИП);
  2. ALTER cabinets: + company_id, + marketplace, бэкфилл из marketplaces,
     дроп marketplaces; связка = одна строка кабинета;
  3. заводит связку ИП × WB (code=ip_prudnikov_wb, schema=cab_ip_wb, без ключей);
  4. ensure_cabinet_schemas на все связки (создаёт схемы, включая cab_ip_wb).

Без --apply ничего не меняет (только печатает план).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app import models  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.services import cabinet_migrate  # noqa: E402


OO = "ООО «Джоинс & Компани»"
IP = "ИП Прудников Константин Григорьевич"

CODE_TO_COMPANY = {"oo_joinco": OO, "ip_prudnikov": IP}
NEW_LINK = {
    "code": "ip_prudnikov_wb", "name": IP, "schema": "cab_ip_wb",
    "marketplace": "wb", "position": 3,
}


def _has_column(conn, table: str, column: str) -> bool:
    return conn.execute(text(
        "select count(*) from information_schema.columns "
        "where table_schema='public' and table_name=:t and column_name=:c"
    ), {"t": table, "c": column}).scalar() > 0


def _companies(conn):
    """{название -> id} фирм пользователя (создаёт, если нет)."""
    user_id = conn.execute(text("select id from users order by id limit 1")).scalar()
    if user_id is None:
        raise RuntimeError("Нет пользователя — сначала поднимите сервер (seed).")
    ids = {}
    for name in (OO, IP):
        uid = conn.execute(text(
            "select id from companies where user_id=:u and name=:n limit 1"
        ), {"u": user_id, "n": name}).scalar()
        if uid is None:
            position = 1 if name == OO else 2
            uid = conn.execute(text(
                "insert into companies (user_id, name, enabled, position) "
                "values (:u, :n, true, :p) returning id"
            ), {"u": user_id, "n": name, "p": position}).scalar()
        ids[name] = uid
    return user_id, ids


def _plan() -> dict:
    with engine.begin() as conn:
        company_missing = conn.execute(text(
            "select to_regclass('public.companies') is null")).scalar()
        cab = conn.execute(text("select count(*) from cabinets")).scalar() or 0
        need_alter = not _has_column(conn, "cabinets", "company_id") or \
            not _has_column(conn, "cabinets", "marketplace")
        wb_exist = conn.execute(text(
            "select count(*) from cabinets where code='ip_prudnikov_wb'")).scalar() > 0
    return {
        "companies": company_missing,
        "cabinets": cab,
        "need_alter": need_alter,
        "new_link": not wb_exist,
    }


def _print_plan() -> None:
    p = _plan()
    print("Строение до миграции:")
    print(f"  companies         {'' if p['companies'] else 'есть'}")
    print(f"  cabinets строк    {p['cabinets']}")
    print(f"  колонки company_id/marketplace {'' if p['need_alter'] else 'есть'}")
    print(f"  связка ИП×WB      {'' if p['new_link'] else 'есть'}")
    print("\nБудут выполнены: создание companies/фирм, ALTER cabinets, "
          "связка ИП×WB + схема cab_ip_wb.")


def run() -> None:
    with engine.begin() as conn:
        if _plan()["companies"]:
            # companies таблица создаётся метаданными моделей
            pass
    Base.metadata.create_all(bind=engine)  # создаёт недостающие таблицы (companies)

    with engine.begin() as conn:
        if not _has_column(conn, "cabinets", "company_id"):
            conn.execute(text("alter table cabinets add column company_id integer"))
        if not _has_column(conn, "cabinets", "marketplace"):
            conn.execute(text(
                "alter table cabinets add column marketplace varchar(20) not null default ''"))

        uid, comp = _companies(conn)
        # Бэкфилл фирмы по коду связки.
        for code, name in CODE_TO_COMPANY.items():
            conn.execute(text(
                "update cabinets set company_id=:c where code=:code and company_id is null"
            ), {"c": comp[name], "code": code})
        # Связки без маппинга кода — по совпадению имени связки с фирмой.
        conn.execute(text(
            "update cabinets c set company_id=co.id from companies co "
            "where c.user_id=co.user_id and c.company_id is null and c.name=co.name"
        ))
        # marketplace из старого CSV-списка (первый код).
        conn.execute(text(
            "update cabinets set marketplace=split_part(marketplaces, ',', 1) "
            "where marketplace='' and marketplaces<>''"
        ))
        if _has_column(conn, "cabinets", "marketplaces"):
            conn.execute(text("alter table cabinets drop column marketplaces"))

        # Связка ИП × WB (без ключей) — история кабинета начнётся с первого refresh.
        exists = conn.execute(text(
            "select count(*) from cabinets where code=:code"
        ), {"code": NEW_LINK["code"]}).scalar() > 0
        if not exists:
            max_pos = conn.execute(text(
                "select coalesce(max(position), 0) from cabinets")).scalar() or 0
            conn.execute(text(
                "insert into cabinets (user_id, company_id, code, name, schema, "
                "marketplace, creds, enabled, position) "
                "values (:u, :c, :code, :name, :schema, :mp, '', true, :p)"),
                {"u": uid, "c": comp[IP], "code": NEW_LINK["code"],
                 "name": NEW_LINK["name"], "schema": NEW_LINK["schema"],
                 "mp": NEW_LINK["marketplace"], "p": max_pos + 1})
        # NOT NULL на фирму (если всё бэкфиллровано).
        left = conn.execute(text(
            "select count(*) from cabinets where company_id is null")).scalar()
        if left == 0:
            try:
                conn.execute(text(
                    "alter table cabinets alter column company_id set not null"))
                conn.execute(text(
                    "alter table cabinets add constraint fk_cabinets_company_id "
                    "foreign key (company_id) references companies(id)"))
            except Exception:  # noqa: BLE001 — констрейнт мог быть создан ранее
                pass
        # Маркер структурной миграции (информационный).
        conn.execute(text(
            "create table if not exists app_schema_state "
            "(key text primary key, value text)"))
        conn.execute(text(
            "insert into app_schema_state (key, value) values ('links', 'done') "
            "on conflict (key) do update set value='done'"))

    with SessionLocal() as db:
        cabs = db.execute(text("select * from cabinets order by position, id")).mappings().all()
        objs = [
            models.Cabinet(
                id=c["id"], user_id=c["user_id"], company_id=c["company_id"],
                code=c["code"], name=c["name"], schema=c["schema"],
                marketplace=c["marketplace"], creds=c["creds"] or "",
                enabled=c["enabled"], position=c["position"],
            )
            for c in cabs
        ]
    cabinet_migrate.ensure_cabinet_schemas(engine, objs)

    print("Миграция связок выполнена:")
    for o in objs:
        print(f"  {o.code:<16} {o.name:<34} {o.schema:<10} mp={o.marketplace}")
    print("Схема cab_ip_wb создана (пустая, ключи ИП×WB ещё не заданы).")


def main() -> None:
    if "--apply" in sys.argv[1:]:
        run()
    else:
        _print_plan()


if __name__ == "__main__":
    main()