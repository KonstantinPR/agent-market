"""Общие сервисные хелперы, переиспользуемые из хендлеров и тестов."""

import re
from typing import Optional

from sqlalchemy.orm import Session

from app import models

#: Разделитель для escape-шаблонов LIKE/ILIKE. Одинаков на всех эндпоинтах,
#: иначе literal-«%» в артикуле снова начнёт вести себя как спецсимвол.
LIKE_ESCAPE = "\\"

_LIKE_SPECIAL = frozenset((LIKE_ESCAPE, "%", "_"))


def like_to_sql(query: Optional[str]) -> str:
    """Шаблон ILIKE для расширенного поиска.

    Семантика как в клиентском ``likeMatch``: ``*`` — любая последовательность
    символов (включая пустую), порядок кусков значим, регистр не важен.
    Остальное — буквально: ``%``, ``_`` и ``\\`` экранируются, поэтому
    «%» в артикуле больше не превращается в «что угодно».

    Возвращает шаблон с обрамляющими ``%``, готовый для
    ``col.ilike(like_to_sql(q), escape=LIKE_ESCAPE)``.

    Не путать с ``sync.like_pattern`` — тот конвертирует готовый SQL-шаблон
    в regex, здесь наоборот: пользовательский запрос -> SQL-шаблон.
    """
    out = []
    for ch in str(query or "").strip():
        if ch == "*":
            out.append("%")
        elif ch in _LIKE_SPECIAL:
            out.append(LIKE_ESCAPE + ch)
        else:
            out.append(ch)
    return "%" + "".join(out) + "%"


def like_to_regex(query: Optional[str]) -> str:
    """Regex для pandas-фильтра, та же семантика, что у :func:`like_to_sql`."""
    return "".join(".*" if ch == "*" else re.escape(ch)
                   for ch in str(query or "").strip().lower())


def like_col(col, query: Optional[str]):
    """Колонка (или любой SQL-вызов) с расширенным поиском по «*».

    Использовать вместо ``col.ilike(f"%{q}%")`` — так «%» и «_» из ввода
    остаются литералами.
    """
    return col.ilike(like_to_sql(query), escape=LIKE_ESCAPE)


def like_re(query: Optional[str]):
    """Скомпилированный регистронезависимый regex по запросу, или None.

    Для фильтров, которые применяются к уже посчитанному выводу (не к SQL),
    чтобы агрегация не пересчитывалась под фильтр.
    """
    src = like_to_regex(query)
    return re.compile(src, re.IGNORECASE) if src else None


def like_match(value, query: Optional[str]) -> bool:
    """Расширенный поиск в Python — для in-memory фильтров."""
    q = str(query or "").strip()
    if not q:
        return True
    return bool(like_re(q).search(str(value or "")))


def count_products_with_cost(db: Session) -> int:
    return db.query(models.Product).filter(models.Product.net_cost > 0).count()