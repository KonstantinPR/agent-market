"""Кабинеты: users + cabinets, активный кабинет запроса, credentials, схемы.

Кабинет = «набор ключей маркетплейсов + свои данные». Данные кабинета лежат
в отдельной PG-схеме; активный кабинет запроса задаётся кукой agent_cabinet
и передаётся в get_db() (database.py), который выполняет
`SET search_path TO "<schema>", public`.

Вне HTTP-запросов (скрипты, тесты) активного кабинета нет — сессии работают
по public (общий каталог и поведение «как раньше»).
"""
from contextvars import ContextVar, Token
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.services import secrets

CABINET_COOKIE = "agent_cabinet"

_active: ContextVar[Optional["CabinetInfo"]] = ContextVar(
    "agent_cabinet", default=None
)


class CabinetInfo:
    """Лёгкий снимок кабинета для работы вне сессии (contextvar, воркеры)."""

    __slots__ = ("id", "code", "name", "schema", "marketplaces", "enabled", "creds")

    def __init__(self, id, code, name, schema, marketplaces, enabled, creds):
        self.id = id
        self.code = code
        self.name = name
        self.schema = schema
        self.marketplaces = marketplaces
        self.enabled = enabled
        self.creds = creds

    @classmethod
    def from_row(cls, row: models.Cabinet) -> "CabinetInfo":
        mps = [m for m in (row.marketplaces or "").split(",") if m.strip()]
        return cls(
            id=row.id, code=row.code, name=row.name, schema=row.schema,
            marketplaces=mps, enabled=row.enabled,
            creds=secrets.decrypt(row.creds),
        )

    def to_dict(self, active: bool = False) -> dict:
        return {
            "id": self.id, "code": self.code, "name": self.name,
            "schema": self.schema, "marketplaces": list(self.marketplaces),
            "enabled": self.enabled, "active": active,
            "has_keys": bool(self.creds),
        }

    def creds_for(self, api: str) -> dict:
        return self.creds.get(api, {}) or {}


def seed_user(db: Session) -> models.User:
    """Возвращает единственного (для текущего этапа) пользователя приложения."""
    user = db.execute(select(models.User).order_by(models.User.id).limit(1)).scalars().first()
    if user is not None:
        return user
    user = models.User(
        username="konstantin.pr", display_name="Константин Прудников",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def seed_users_and_cabinets(db: Session) -> list[models.Cabinet]:
    """Идемпотентно заводит пользователя и стартовые кабинеты (из .env)."""
    user = seed_user(db)
    existing = db.execute(select(models.Cabinet)).scalars().all()
    if existing:
        return existing
    creds_oo = {"wb": {
        "standard": settings.wb_api_key,
        "finance": settings.wb_finance_api_key,
        "finance2": settings.wb_finance_api_key_2,
    }}
    creds_ip = {"ozon": {
        "client_id": settings.ozon_client_id,
        "api_key": settings.ozon_api_key,
    }}
    cabs = [
        models.Cabinet(
            user_id=user.id, code="oo_joinco",
            name="ООО «Джоинс & Компани»",
            schema="cab_oo", marketplaces="wb",
            creds=secrets.encrypt(creds_oo), enabled=True, position=1,
        ),
        models.Cabinet(
            user_id=user.id, code="ip_prudnikov",
            name="ИП Прудников Константин Григорьевич",
            schema="cab_ip", marketplaces="ozon",
            creds=secrets.encrypt(creds_ip), enabled=True, position=2,
        ),
    ]
    db.add_all(cabs)
    db.flush()
    if user.last_cabinet_id is None:
        user.last_cabinet_id = cabs[0].id
    db.commit()
    for c in cabs:
        db.refresh(c)
    return cabs


def list_cabinets(db: Session, user: models.User) -> list[models.Cabinet]:
    return db.execute(
        select(models.Cabinet)
        .where(models.Cabinet.user_id == user.id)
        .order_by(models.Cabinet.position, models.Cabinet.id)
    ).scalars().all()


def cabinet_by_id(db: Session, cabinet_id: int, user: Optional[models.User] = None) -> Optional[models.Cabinet]:
    q = select(models.Cabinet).where(models.Cabinet.id == cabinet_id)
    if user is not None:
        q = q.where(models.Cabinet.user_id == user.id)
    return db.execute(q).scalars().first()


def info_by_id(db: Session, cabinet_id: int) -> Optional[CabinetInfo]:
    row = cabinet_by_id(db, cabinet_id)
    return CabinetInfo.from_row(row) if row else None


def resolve_active(db: Session, cookie_value: Optional[str] = None,
                   user: Optional[models.User] = None) -> Optional[CabinetInfo]:
    """Активный кабинет: кука → last_cabinet_id → первый включённый → None."""
    user = user or seed_user(db)
    if cookie_value:
        try:
            row = cabinet_by_id(db, int(cookie_value), user)
            if row is not None:
                return CabinetInfo.from_row(row)
        except (TypeError, ValueError):
            pass
    if user.last_cabinet_id:
        row = cabinet_by_id(db, user.last_cabinet_id, user)
        if row is not None:
            return CabinetInfo.from_row(row)
    rows = list_cabinets(db, user)
    for r in rows:
        if r.enabled:
            return CabinetInfo.from_row(r)
    if rows:
        return CabinetInfo.from_row(rows[0])
    return None


def set_active(info: Optional[CabinetInfo]) -> Token:
    return _active.set(info)


def reset_active(token: Token) -> None:
    _active.reset(token)


def get_active() -> Optional[CabinetInfo]:
    return _active.get()


def active_credentials(api: str) -> dict:
    info = _active.get()
    if info is None:
        return {}
    return info.creds_for(api)


def search_path_sql(schema: str) -> str:
    """SET search_path для сессии кабинета (схема кабинета + общий public)."""
    return f'SET search_path TO "{schema}", public'


def apply_search_path(db: Session, schema: str) -> None:
    db.execute(text(search_path_sql(schema)))