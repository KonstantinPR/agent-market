"""Общие сервисные хелперы, переиспользуемые из хендлеров и тестов."""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def resolve_marketplace_ids(db: Session, marketplace: Optional[str]):
    """'wb,ozon' | 'wb' | '' | 'all' -> список id маркетплейсов (None = все)."""
    if not marketplace or marketplace.strip().lower() == "all":
        return None
    codes = [c.strip() for c in marketplace.split(",") if c.strip()]
    ids = []
    for code in codes:
        mp_id = db.execute(
            select(models.Marketplace.id).where(models.Marketplace.code == code)
        ).scalar()
        if mp_id is not None:
            ids.append(mp_id)
    return ids or None


def count_products_with_cost(db: Session) -> int:
    return db.query(models.Product).filter(models.Product.net_cost > 0).count()