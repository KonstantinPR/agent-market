# -*- coding: utf-8 -*-
"""Вьюхи карточек/воронки/цен/продаж/остатков: custom-stock."""
from app.api._common import *  # noqa: F401,F403

router = APIRouter()



@router.get("/custom-stock")
def api_custom_stock(db: Session = Depends(get_db)):
    rows = [
        {
            "article": r.article,
            "name": r.name,
            "quantity": int(r.quantity or 0),
            "net_cost": float(r.net_cost or 0),
            "updated_at": str(r.updated_at) if r.updated_at else "",
        }
        for r in db.execute(
            select(
                models.CustomStock.article,
                models.CustomStock.quantity,
                models.CustomStock.net_cost,
                models.CustomStock.updated_at,
                models.Product.name,
            )
            .select_from(models.CustomStock)
            .join(models.Product, models.CustomStock.article == models.Product.article, isouter=True)
        )
    ]
    return {"rows": rows, "count": len(rows)}
