"""Общие сервисные хелперы, переиспользуемые из хендлеров и тестов."""

import re
from typing import Optional

from sqlalchemy.orm import Session

from app import models

#: Разделитель для escape-шаблонов LIKE/ILIKE. Одинаков на всех эндпоинтах,
#: иначе literal-«%» в артикуле снова начнёт вести себя как спецсимвол.
LIKE_ESCAPE = "\\"

_LIKE_SPECIAL = frozenset((LIKE_ESCAPE, "%", "_"))


def _split_anchors(query: Optional[str]) -> tuple[str, bool, bool]:
    """Делит запрос на тело и флаги якорей.

    ``^`` в самом начале — привязка к началу строки, ``$`` в самом конце —
    к концу. Внутри строки эти символы остаются литералами. Пустое тело
    (``""``, ``"^"``, ``"$"``, ``"^$"``) означает «фильтр не нужен».
    """
    body = str(query or "").strip()
    start = body.startswith("^")
    if start:
        body = body[1:]
    end = body.endswith("$")
    if end:
        body = body[:-1]
    return body, start, end


def like_to_sql(query: Optional[str]) -> str:
    """Шаблон ILIKE для расширенного поиска.

    Семантика как в клиентском ``likeMatch``: ``*`` — любая последовательность
    символов (включая пустую), порядок кусков значим, регистр не важен.
    Остальное — буквально: ``%``, ``_`` и ``\\`` экранируются, поэтому
    «%» в артикуле больше не превращается в «что угодно».

    Якоря: ``^`` в начале запроса привязывает к началу строки, ``$`` в конце —
    к концу. ``^S*N30$`` найдёт ``SK-MI-N30``, но не ``SK-MI-N30-2``.
    Без якорей шаблон, как и раньше, обрамляется ``%`` с обеих сторон.

    Возвращает шаблон, готовый для
    ``col.ilike(like_to_sql(q), escape=LIKE_ESCAPE)``.

    Не путать с ``sync.like_pattern`` — тот конвертирует готовый SQL-шаблон
    в regex, здесь наоборот: пользовательский запрос -> SQL-шаблон.
    """
    body, start, end = _split_anchors(query)
    if not body:
        return "%%"
    out = []
    for ch in body:
        if ch == "*":
            out.append("%")
        elif ch in _LIKE_SPECIAL:
            out.append(LIKE_ESCAPE + ch)
        else:
            out.append(ch)
    return ("" if start else "%") + "".join(out) + ("" if end else "%")


def like_to_regex(query: Optional[str]) -> str:
    """Regex для pandas-фильтра, та же семантика, что у :func:`like_to_sql`."""
    body, start, end = _split_anchors(query)
    if not body:
        return ""
    src = "".join(".*" if ch == "*" else re.escape(ch) for ch in body.lower())
    # \A/\Z строже, чем ^/$: не пропускают совпадение перед хвостовым переводом строки.
    return ("\\A" if start else "") + src + ("\\Z" if end else "")


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
    rx = like_re(query)
    if rx is None:
        return True
    return bool(rx.search(str(value or "")))


def count_products_with_cost(db: Session) -> int:
    return db.query(models.Product).filter(models.Product.net_cost > 0).count()