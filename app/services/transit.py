"""«В пути»: товары, которые уже отправлены, но ещё не приняты складом.

Две независимые карты (источники появятся отдельными задачами):

* ``supplier_in_transit`` — товар едет от поставщиков **к нам**. Такой товар
  уменьшает «Купить у поставщика», но не считается доступным для отгрузки на
  маркетплейсы, пока не принят на наш склад.
* ``marketplace_in_transit`` — наш товар отгружен на склад WB/Ozon, но
  маркетплейс его ещё не принял. Такой товар уменьшает дефицит и «Дослать»
  соответствующего МП: повторно его туда не отправляем.

Ключ карты — ``(UPPER(article), UPPER(size))``; ``size`` может быть пустым,
если размер неизвестен. В размерном разрезе такой ключ не размазывается по
всем размерам артикула. Пока функции возвращают пустые карты: расчёт
потребности работает как раньше, а новые поля показывают 0.
"""

from typing import Optional

from sqlalchemy.orm import Session

#: Коды маркетплейсов, для которых ведётся отдельная карта «в пути».
MARKETPLACES = ("wb", "ozon")


def normalize_key(article, size="") -> Optional[tuple[str, str]]:
    """``(article, size)`` → ключ карты ``(UPPER, UPPER)``; пустой артикул — None."""
    art = str(article or "").strip().upper()
    if not art:
        return None
    return art, str(size or "").strip().upper()


def normalize_qty(value) -> int:
    """Количество в штуках: мусор → 0, отрицательное «в пути» → 0."""
    try:
        n = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def supplier_in_transit(db: Session, as_of=None) -> dict:
    """``{(article, size): qty}`` — едет от поставщиков на наш склад.

    ``as_of`` — дата среза (для будущих источников; сейчас не используется).
    """
    return {}


def marketplace_in_transit(db: Session, as_of=None) -> dict:
    """``{"wb": {(article, size): qty}, "ozon": {...}}`` — едет на склады МП."""
    return {code: {} for code in MARKETPLACES}


def by_article(card: dict) -> dict:
    """Свернуть карту ``{(article, size): qty}`` в ``{article: qty}``."""
    out: dict = {}
    for key, qty in (card or {}).items():
        if isinstance(key, (tuple, list)):
            art = str(key[0] if key else "").strip().upper()
        else:
            art = str(key or "").strip().upper()
        n = normalize_qty(qty)
        if art and n:
            out[art] = out.get(art, 0) + n
    return out
