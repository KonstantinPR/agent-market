"""Схема кабинетов (schema-per-cabinet) и миграция данных public -> схемы.

Общие таблицы (каталог, склад, контрагенты, users, cabinets) живут в public.
Все «рыночные» таблицы — в схемах кабинетов (cab_*). Переключение кабинета =
`SET search_path TO "<schema>", public`.

Маркер app_schema_state.key='cabinets' сделан одноразовым: миграция раскатывает
существующие данные public по кабинетам только один раз (идемпотентно, в одной
транзакции); дальше каждая схема обновляется провайдерами своего кабинета.
"""
import logging
import re
from typing import Optional

from sqlalchemy import text

from app.database import Base

log = logging.getLogger("agent_market.cabinets")

# --- Таблицы, живущие в схеме кабинета (все «рыночные» данные) ---------------
# Таблицы, которые в кабинете одни на маркетплейсы, а в БД строка делится по
# marketplace_id (или колонке marketplace).
MP_SPLIT_TABLES = frozenset({"sales", "stocks", "marketplace_cards", "price_snapshots"})
# Таблицы только WB (переносим целиком в первый кабинет).
WB_ONLY_TABLES = frozenset({
    "wb_detail_rows", "funnel_metric", "nm_articles",
    "price_changes", "storage_costs", "wb_promotions",
})
# Таблицы только Ozon.
OZ_ONLY_TABLES = frozenset({
    "ozon_detail_rows", "ozon_placements", "ozon_cash_flows",
    "ozon_accruals", "ozon_buyouts",
})
# Служебные state-таблицы API (свои у каждого кабинета; в БД делим по api).
PER_CAB_STATE_TABLES = frozenset({"api_pulls", "refresh_runs"})

PER_CAB_TABLES = (
    MP_SPLIT_TABLES | WB_ONLY_TABLES | OZ_ONLY_TABLES | PER_CAB_STATE_TABLES
)

MARKER_KEY = "cabinets"


def _per_cab_table_objs() -> list:
    names = PER_CAB_TABLES
    return [t for t in Base.metadata.sorted_tables if t.name in names]


# ---------------------------------------------------------------------------
# Создание схем и таблиц кабинетов (идемпотентно, каждый старт)
# ---------------------------------------------------------------------------
def _detach_public_defaults(conn, schema: str, table: str) -> None:
    """Перенаправляет sequence-defaults из public на последовательности cab-схемы.

    `LIKE public.t INCLUDING ALL` копирует default вида nextval('t_id_seq')
    без квалификатора схемы — при вставке он резолвится через search_path на
    public-последовательность, и дроп public-таблицы ломает такие default'ы.
    Для каждой serial-колонки создаём собственную последовательность в cab-схеме
    и перепривязываем default (если резолв последовательности указывает наружу,
    т.е. не на cab-схему).
    """
    rows = conn.execute(text(
        "select column_name, column_default "
        "from information_schema.columns "
        "where table_schema = :s and table_name = :t "
        "and column_default like 'nextval(%'"
    ), {"s": schema, "t": table}).fetchall()
    for col, default in rows:
        # nextval('имя_последовательности'::regclass)
        m = re.search(r"nextval\('([^']+)'", default)
        if not m:
            continue
        # to_regclass резолвит имя через search_path и возвращает каноническое
        # (схема-qualified, если не в текущей схеме).
        resolved = conn.execute(text("select to_regclass(:n)"), {"n": m.group(1)}).scalar()
        if resolved is None:
            continue
        owner_schema = str(resolved).split(".", 1)[0].strip('"')
        if owner_schema == schema:
            continue
        conn.execute(text(
            f'create sequence if not exists "{schema}"."{table}_{col}_seq"'))
        conn.execute(text(f'alter table "{schema}"."{table}" alter column "{col}" '
                          f'set default nextval(:q)'), {"q": f"{schema}.{table}_{col}_seq"})


def _create_table_in(conn, table, schema: str) -> None:
    """CREATE TABLE для схемы кабинета по структуре public-таблицы.

    Если public-таблица существует — копируем структуру через `LIKE public.table
    INCLUDING ALL` (типы, NOT NULL, defaults, FK/индексы где применимо), чтобы
    `INSERT INTO ... SELECT *` никогда не падал из-за расхождения типов.
    Если public-таблицы нет (новое окружение) — фолбэк на Base.metadata.create_all.
    """
    if _table_exists(conn, schema, table.name):
        return
    if _table_exists(conn, "public", table.name):
        # Копируем полную структуру (включая defaults/constraints/indexes по PG)
        try:
            conn.execute(text(
                f'CREATE TABLE "{schema}"."{table.name}" '
                f'(LIKE public."{table.name}" INCLUDING ALL)'
            ))
            _detach_public_defaults(conn, schema, table.name)
            return
        except Exception:  # pragma: no cover — fallback
            pass
    # Фолбэк: создаём по метаданным моделей
    Base.metadata.create_all(conn, tables=[table], checkfirst=False)


def ensure_cabinet_schemas(engine, cabinets: list) -> None:
    """Создаёт schemas кабинетов и их таблицы. Общие таблицы не трогает.

    CREATE TABLE подставляется в схему кабинета явно (schema-qualified), FK на
    общие public-таблицы (marketplaces/products) рендерятся без квалификатора и
    резолвятся через search_path "<schema>", public.
    """
    if not cabinets:
        return
    with engine.begin() as conn:
        for schema in {getattr(c, "schema") for c in cabinets}:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            conn.execute(text(f'SET search_path TO "{schema}", public'))
            for table in _per_cab_table_objs():
                _create_table_in(conn, table, schema)
        conn.execute(text("RESET search_path"))


# ---------------------------------------------------------------------------
# Одноразовая миграция существующих данных public -> схемы кабинетов
# ---------------------------------------------------------------------------
def _marker(conn) -> str:
    return conn.execute(text(
        "select value from app_schema_state where key = :k"
    ), {"k": MARKER_KEY}).scalar()


def _set_marker(conn, value: str) -> None:
    conn.execute(text(
        "insert into app_schema_state (key, value) values (:k, :v) "
        "on conflict (key) do update set value = excluded.value"
    ), {"k": MARKER_KEY, "v": value})


def _table_exists(conn, schema: str, table: str) -> bool:
    return conn.execute(
        text("select to_regclass(:n) is not null"), {"n": f"{schema}.{table}"}
    ).scalar()


def _table_count(conn, schema: str, table: str) -> int:
    if not _table_exists(conn, schema, table):
        return 0
    return conn.execute(text(f'select count(*) from "{schema}"."{table}"')).scalar()


def _fix_sequence(conn, schema: str, table: str) -> None:
    if not conn.execute(text(
        "select 1 from information_schema.columns "
        "where table_schema = :s and table_name = :t and column_name = 'id'"
    ), {"s": schema, "t": table}).scalar():
        return
    seq = conn.execute(
        text("select pg_get_serial_sequence(:t, 'id')"), {"t": f"{schema}.{table}"}
    ).scalar()
    if not seq:
        return
    max_id = conn.execute(text(f'select coalesce(max(id), 1) from "{schema}"."{table}"')).scalar()
    conn.execute(text("select setval(:seq, :v)"), {"seq": seq, "v": int(max_id)})


def _copy_filtered(conn, src_col: str, value, src: str, dst: str, api_code: str) -> int:
    """Копирует строки public.src с src_col = value в dst-таблицу кабинета
    с явным перечислением колонок (устойчиво к расхождению типов).
    """
    if not _table_exists(conn, "public", src) or not _table_exists(conn, dst, src):
        return 0
    # Получаем колонки, общие для public.src и dst.src
    cols_pub = conn.execute(
        text(
            "select column_name from information_schema.columns "
            "where table_schema='public' and table_name=:t order by ordinal_position"
        ),
        {"t": src},
    ).scalars().all()
    cols_dst = conn.execute(
        text(
            "select column_name from information_schema.columns "
            "where table_schema=:s and table_name=:t order by ordinal_position"
        ),
        {"s": dst, "t": src},
    ).scalars().all()
    cols = [c for c in cols_pub if c in cols_dst]
    if not cols:
        return 0
    col_list = ", ".join(f'"{c}"' for c in cols)
    src_list = ", ".join(f'"public"."{src}"."{c}"' for c in cols)
    conn.execute(
        text(
            f'insert into "{dst}"."{src}" ({col_list}) select {src_list} '
            f'from "public"."{src}" where "{src_col}" = :v'
        ),
        {"v": value},
    )
    _fix_sequence(conn, dst, src)
    return _table_count(conn, dst, src)


def _copy_all(conn, src: str, dst: str) -> int:
    """Копирует все строки public.src в dst-таблицу кабинета (схема кабинета пуста)."""
    if not _table_exists(conn, "public", src) or not _table_exists(conn, dst, src):
        return 0
    cols_pub = conn.execute(
        text(
            "select column_name from information_schema.columns "
            "where table_schema='public' and table_name=:t order by ordinal_position"
        ),
        {"t": src},
    ).scalars().all()
    cols_dst = conn.execute(
        text(
            "select column_name from information_schema.columns "
            "where table_schema=:s and table_name=:t order by ordinal_position"
        ),
        {"s": dst, "t": src},
    ).scalars().all()
    cols = [c for c in cols_pub if c in cols_dst]
    if not cols:
        return 0
    col_list = ", ".join(f'"{c}"' for c in cols)
    src_list = ", ".join(f'"public"."{src}"."{c}"' for c in cols)
    conn.execute(
        text(f'insert into "{dst}"."{src}" ({col_list}) select {src_list} from "public"."{src}"')
    )
    _fix_sequence(conn, dst, src)
    return _table_count(conn, dst, src)


def _cab_schema_for(cabinets: list, api: str) -> Optional[str]:
    """Схема кабинета, у которого в marketplaces есть api (wb/ozon)."""
    for c in cabinets:
        mps = (getattr(c, "marketplaces", "") or "").split(",")
        if api in [m.strip() for m in mps if m.strip()]:
            return getattr(c, "schema")
    return None


def migrate_public_data(engine, cabinets: list) -> bool:
    """Однократная раскатка данных public по кабинетам. True — миграция прошла."""
    if not cabinets:
        return False
    schema_wb = _cab_schema_for(cabinets, "wb")
    schema_oz = _cab_schema_for(cabinets, "ozon")
    with engine.begin() as conn:
        if _marker(conn) == "done":
            return False
        wb_id = conn.execute(
            text("select id from marketplaces where code = 'wb'")
        ).scalar()
        oz_id = conn.execute(
            text("select id from marketplaces where code = 'ozon'")
        ).scalar()
        moved = 0
        # Куда копировать строку в зависимости от маркетплейса.
        def target_schema(api: str) -> Optional[str]:
            return schema_wb if api == "wb" else (schema_oz if api == "ozon" else None)

        # Таблицы, делимые по маркетплейсу: копируем по кускам и удаляем public.
        for t in MP_SPLIT_TABLES:
            if not _table_exists(conn, "public", t):
                continue
            total = _table_count(conn, "public", t)
            done = 0
            if t == "price_snapshots":
                if schema_wb:
                    done += _copy_filtered(conn, "marketplace", "wb", t, schema_wb, "wb")
                if schema_oz:
                    done += _copy_filtered(conn, "marketplace", "ozon", t, schema_oz, "ozon")
            else:
                for api, mp_value in (("wb", wb_id), ("ozon", oz_id)):
                    dst = target_schema(api)
                    if dst and mp_value is not None:
                        done += _copy_filtered(conn, "marketplace_id", mp_value, t, dst, api)
            if total == done or total == 0:
                try:
                    conn.execute(text(f'drop table "public"."{t}"'))
                except Exception as e:
                    try:
                        conn.execute(text(f'drop table "public"."{t}" cascade'))
                    except Exception:
                        raise e
        # Таблицы целиком -> схема кабинета (копия + дроп public: cab-схема уже
        # содержит пустые аналоги, созданные ensure_cabinet_schemas).
        for t in WB_ONLY_TABLES:
            if schema_wb and _table_exists(conn, "public", t):
                total = _table_count(conn, "public", t)
                done = _copy_all(conn, t, schema_wb)
                if total == done or total == 0:
                    conn.execute(text(f'drop table "public"."{t}"'))
                    moved += total
                else:
                    raise RuntimeError(
                        f"Миграция {t}: {total} строк в public, скопировано {done}.")
        for t in OZ_ONLY_TABLES:
            if schema_oz and _table_exists(conn, "public", t):
                total = _table_count(conn, "public", t)
                done = _copy_all(conn, t, schema_oz)
                if total == done or total == 0:
                    conn.execute(text(f'drop table "public"."{t}"'))
                    moved += total
                else:
                    raise RuntimeError(
                        f"Миграция {t}: {total} строк в public, скопировано {done}.")
        # Служебные (api_pulls, refresh_runs) — делим по api.
        for t in PER_CAB_STATE_TABLES:
            if not _table_exists(conn, "public", t):
                continue
            for api, dst in (("wb", schema_wb), ("ozon", schema_oz)):
                if dst is not None:
                    moved += _copy_filtered(conn, "api", api, t, dst, api)
            total = _table_count(conn, "public", t)
            done = sum(
                _table_count(conn, s, t) for s in {schema_wb, schema_oz} if s is not None
            )
            if total == done or total == 0:
                conn.execute(text(f'drop table "public"."{t}"'))
            else:
                raise RuntimeError(f"Миграция {t}: {total} строк, скопировано {done}.")
        _set_marker(conn, "done")
        log.info("Раскатали кабинеты: перенесено строк из public: %s", moved)
        return True


# ---------------------------------------------------------------------------
# Уборка пустых теней per-cab таблиц из public (само-лечение на каждом старте)
# ---------------------------------------------------------------------------
def cleanup_public_shadows(engine) -> int:
    """Дропает пустые остатки per-cab таблиц в public (после раскатки/новых версий).

    Не пустые не трогаем — чтобы не потерять данные (такое должно было быть
    обработано настоящей миграцией). Возвращает число дропнутых таблиц.
    """
    dropped = 0
    with engine.begin() as conn:
        for t in PER_CAB_TABLES:
            if not _table_exists(conn, "public", t):
                continue
            n = conn.execute(text(f'select 1 from "public"."{t}" limit 1')).scalar()
            if n is None:
                conn.execute(text(f'drop table "public"."{t}"'))
                dropped += 1
    return dropped