"""Кабинеты-связки: users → companies → cabinets (фирма × маркетплейс).

Иерархия:
    users      — пользователь приложения (кабинет = логин).
    companies  — фирмы пользователя (ООО, ИП …).
    cabinets   — связка «компания × маркетплейс»: строка + свои данные в
                 отдельной PG-схеме (schema).

Активная связка запроса задаётся кукой agent_cabinet и передаётся в get_db()
(database.py), который выполняет `SET search_path TO "<schema>", public`.

Вне HTTP-запросов (скрипты, тесты) активной связки нет — сессии работают
по public (общий каталог и поведение «как раньше»).
"""
from contextvars import ContextVar
from typing import Callable, Optional

import json

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
    """Лёгкий снимок связки (кабинета) для работы вне сессии (contextvar, воркеры)."""

    __slots__ = (
        "id", "code", "name", "schema", "marketplace",
        "company_id", "company_name", "enabled", "creds", "position",
    )

    def __init__(self, id, code, name, schema, marketplace,
                 company_id=None, company_name="", enabled=True, creds=None,
                 position=0):
        self.id = id
        self.code = code
        self.name = name
        self.schema = schema
        self.marketplace = (marketplace or "").strip()
        self.company_id = company_id
        self.company_name = company_name or ""
        self.enabled = enabled
        self.creds = creds or {}
        self.position = position

    @classmethod
    def from_row(cls, row: models.Cabinet, company_name: str = "") -> "CabinetInfo":
        return cls(
            id=row.id, code=row.code, name=row.name, schema=row.schema,
            marketplace=getattr(row, "marketplace", "") or "",
            company_id=getattr(row, "company_id", None),
            company_name=company_name,
            enabled=row.enabled,
            creds=secrets.decrypt(row.creds),
            position=getattr(row, "position", 0),
        )

    def to_dict(self, active: bool = False, mp_names: Optional[dict] = None) -> dict:
        return {
            "id": self.id, "code": self.code, "name": self.name,
            "schema": self.schema, "marketplace": self.marketplace,
            "marketplaces": [self.marketplace] if self.marketplace else [],
            "company_id": self.company_id, "company_name": self.company_name,
            "owner": self.owner(mp_names),
            "enabled": self.enabled, "active": active,
            "has_keys": bool(self.creds), "position": self.position,
        }

    def owner(self, mp_names: Optional[dict] = None) -> str:
        """Владелец связки: «Фирма · Маркетплейс» (напр. «ИП … · Ozon»)."""
        mp = (mp_names or {}).get(self.marketplace)
        if mp and self.company_name:
            return f"{self.company_name} · {mp}"
        if mp:
            return mp
        if self.company_name:
            return self.company_name
        return self.name or self.marketplace or ""

    def creds_for(self, api: str) -> dict:
        return self.creds.get(api, {}) or {}


def marketplace_names(db: Session) -> dict:
    """{код мп: название} по справочнику marketplaces."""
    rows = db.execute(select(models.Marketplace.code, models.Marketplace.name)).all()
    return {code: name for code, name in rows}


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
    """Идемпотентно заводит пользователя, фирмы и стартовые связки (из .env)."""
    user = seed_user(db)
    existing = db.execute(select(models.Cabinet)).scalars().all()
    if existing:
        return existing
    companies = db.execute(
        select(models.Company).where(models.Company.user_id == user.id)
        .order_by(models.Company.position, models.Company.id)
    ).scalars().all()
    by_name = {c.name: c for c in companies}
    oo = by_name.get("ООО «Джоинс & Компани»") or models.Company(
        user_id=user.id, name="ООО «Джоинс & Компани»", enabled=True, position=1)
    ip = by_name.get("ИП Прудников Константин Григорьевич") or models.Company(
        user_id=user.id, name="ИП Прудников Константин Григорьевич", enabled=True, position=2)
    if oo.id is None:
        db.add(oo)
    if ip.id is None:
        db.add(ip)
    db.flush()

    creds_oo = {"wb": {
        "standard": settings.wb_api_key,
        "finance": settings.wb_finance_api_key,
        "finance2": settings.wb_finance_api_key_2,
    }}
    creds_ip = {"ozon": {
        "client_id": settings.ozon_client_id,
        "api_key": settings.ozon_api_key,
    }}
    # WB-связка ИП: ключей может не быть (пустой dict → кабинет «без ключей»).
    creds_ip_wb = {}
    if settings.wb_api_token_ip_2:
        creds_ip_wb = {"wb": {
            "standard": settings.wb_api_token_ip_2,
            "finance": settings.wb_api_token_ip_2,
            "finance2": settings.wb_api_token_ip_2,
        }}
    cabs = [
        models.Cabinet(
            user_id=user.id, company_id=oo.id, code="oo_joinco",
            name="ООО «Джоинс & Компани»",
            schema="cab_oo", marketplace="wb",
            creds=secrets.encrypt(creds_oo), enabled=True, position=1,
        ),
        models.Cabinet(
            user_id=user.id, company_id=ip.id, code="ip_prudnikov",
            name="ИП Прудников Константин Григорьевич",
            schema="cab_ip", marketplace="ozon",
            creds=secrets.encrypt(creds_ip), enabled=True, position=2,
        ),
        models.Cabinet(
            user_id=user.id, company_id=ip.id, code="ip_prudnikov_wb",
            name="ИП Прудников Константин Григорьевич",
            schema="cab_ip_wb", marketplace="wb",
            creds=secrets.encrypt(creds_ip_wb), enabled=True, position=3,
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


def companies_of(db: Session, user: models.User) -> list[models.Company]:
    return db.execute(
        select(models.Company)
        .where(models.Company.user_id == user.id)
        .order_by(models.Company.position, models.Company.id)
    ).scalars().all()


def cabinet_by_id(db: Session, cabinet_id: int, user: Optional[models.User] = None) -> Optional[models.Cabinet]:
    q = select(models.Cabinet).where(models.Cabinet.id == cabinet_id)
    if user is not None:
        q = q.where(models.Cabinet.user_id == user.id)
    return db.execute(q).scalars().first()


def _company_name(db: Session, company_id) -> str:
    if company_id is None:
        return ""
    name = db.execute(
        select(models.Company.name).where(models.Company.id == company_id)
    ).scalar_one_or_none()
    return name or ""


def info_by_id(db: Session, cabinet_id: int) -> Optional[CabinetInfo]:
    row = cabinet_by_id(db, cabinet_id)
    if row is None:
        return None
    info = CabinetInfo.from_row(row, company_name=_company_name(db, row.company_id))
    return info


def resolve_active(db: Session, cookie_value: Optional[str] = None,
                   user: Optional[models.User] = None) -> Optional[CabinetInfo]:
    """Активная связка: кука → last_cabinet_id → первая включённая → None."""
    user = user or seed_user(db)
    if cookie_value:
        try:
            row = cabinet_by_id(db, int(cookie_value), user)
            if row is not None:
                return CabinetInfo.from_row(row, company_name=_company_name(db, row.company_id))
        except (TypeError, ValueError):
            pass
    if user.last_cabinet_id:
        row = cabinet_by_id(db, user.last_cabinet_id, user)
        if row is not None:
            return CabinetInfo.from_row(row, company_name=_company_name(db, row.company_id))
    rows = list_cabinets(db, user)
    for r in rows:
        if r.enabled:
            return CabinetInfo.from_row(r, company_name=_company_name(db, r.company_id))
    if rows:
        return CabinetInfo.from_row(rows[0], company_name=_company_name(db, rows[0].company_id))
    return None


def set_active(info: Optional[CabinetInfo]) -> None:
    _active.set(info)


def reset_active(info: Optional[CabinetInfo] = None) -> None:
    """Сброс активной связки.

    Используем set(None), а не token.reset(): set_active() вызывается в
    contextvar'е воркера threadpool (FastAPI), а reset сработает в context-е
    finally — token из другого контекста нельзя ресетнуть (ValueError).
    """
    _active.set(None)


def get_active() -> Optional[CabinetInfo]:
    return _active.get()


def active_credentials(api: str) -> dict:
    info = _active.get()
    if info is None:
        return {}
    return info.creds_for(api)


def search_path_sql(schema: str) -> str:
    """SET search_path для сессии (схема связки + общий public)."""
    return f'SET search_path TO "{schema}", public'


def apply_search_path(db: Session, schema: str) -> None:
    db.execute(text(search_path_sql(schema)))


def pin_search_path(db: Session, schema: str) -> None:
    """Пере-применяет SET search_path при начале каждой новой транзакции сессии.

    После commit сессия возвращает соединение в пул (reset — ROLLBACK, он
    откатывает незакоммиченный SET), и следующий запрос сессии может получить
    чужое соединение с дефолтным search_path. Для фонового воркера обновления
    (много commit'ов) это роняло все шаги на «refresh_runs не существует» —
    listener закрепляет схему связки за сессией на всё время её жизни.
    """
    from sqlalchemy import event

    stmt = search_path_sql(schema)

    @event.listens_for(db, "after_begin")
    def _reapply(session, transaction, connection):  # noqa: ARG001
        # SQLAlchemy передаёт (session, SessionTransaction, Connection);
        # SET выполняется на привязанном к транзакции соединении.
        connection.exec_driver_sql(stmt)


def run_per_link(db: Session, links: list, fn: Callable, restore: bool = True) -> list:
    """Выполняет fn(db, info) под search_path каждой связки из links.

    Возвращает список (info, result). После цикла восстанавливает search_path,
    бывший на входе (иначе вернувшаяся в пул сессия «протечёт» в другую связку).
    """
    start = db.execute(text("SHOW search_path")).scalar() or ""
    out = []
    try:
        for info in links:
            apply_search_path(db, info.schema)
            out.append((info, fn(db, info)))
    finally:
        if restore:
            try:
                if start.strip():
                    db.execute(text(f'SET search_path TO {start.strip()}'))
                else:
                    db.execute(text("RESET search_path"))
            except Exception:  # noqa: BLE001
                try:
                    db.execute(text("RESET search_path"))
                except Exception:  # noqa: BLE001
                    pass
    return out


def link_owner(db: Session, info: CabinetInfo) -> str:
    """Полное имя владельца связки для строк сравнения («Владелец»)."""
    if not info.company_name:
        info.company_name = _company_name(db, info.company_id)
    return info.owner(marketplace_names(db))


def get_pricing_settings(db: Session, cabinet_id: int) -> dict:
    """Настройки автопилота цен связки (JSON) или {} (пусто/битый JSON)."""
    row = cabinet_by_id(db, cabinet_id)
    if row is None:
        return {}
    raw = getattr(row, "pricing_settings", "") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_pricing_settings(db: Session, cabinet_id: int, data: dict) -> dict:
    """Сохраняет настройки автопилота цен за связкой."""
    row = cabinet_by_id(db, cabinet_id)
    if row is None:
        raise ValueError("Связка не найдена")
    row.pricing_settings = json.dumps(data or {}, ensure_ascii=False)
    db.commit()
    return data or {}


def copy_pricing_settings(db: Session, source_id: int,
                          target_ids: Optional[list] = None) -> int:
    """Копирует настройки автопилота source-связки в целевые.

    target_ids=None — во все связки того же пользователя. Возвращает число
    обновлённых связок (источник не считается).
    """
    src = cabinet_by_id(db, source_id)
    if src is None:
        raise ValueError("Связка-источник не найдена")
    q = select(models.Cabinet).where(models.Cabinet.user_id == src.user_id)
    if target_ids:
        q = q.where(models.Cabinet.id.in_(list(target_ids)))
    rows = db.execute(q).scalars().all()
    payload = getattr(src, "pricing_settings", "") or ""
    n = 0
    for r in rows:
        if r.id == src.id:
            continue
        r.pricing_settings = payload
        n += 1
    db.commit()
    return n