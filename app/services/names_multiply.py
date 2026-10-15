# -*- coding: utf-8 -*-
"""«Размножить имена»: генерация имён фото ``<артикул>-<n>.JPG``.

Порт ``finance/app/modules/text_handler.py::names_multiply``. В отличие от
оригинала (где ``start_from`` и ``step`` игнорировались), здесь ``start_from``
соблюдается: первый вариант артикула нумеруется с ``start_from``, а повторные
вхождения того же артикула продолжают нумерацию, чтобы имена не совпадали.
"""

from __future__ import annotations

from collections import defaultdict


def names_multiply(
    articles: list[str],
    multiply: int,
    start_from: int = 1,
    upper: bool = True,
) -> list[str]:
    """Список имён: на каждый артикул ``multiply`` имён подряд.

    ``articles`` — список артикулов (строки; пустые/пробельные пропускаются).
    ``upper`` — капслок: артикул в имени выводится в верхнем регистре
    (по умолчанию включён).
    """
    mult = int(multiply)
    start = int(start_from)
    if mult <= 0:
        return []

    last_used: dict[str, int] = defaultdict(lambda: start - 1)
    out: list[str] = []
    for raw in articles:
        art = str(raw).strip()
        if not art:
            continue
        if upper:
            art = art.upper()
        n = last_used[art] + 1
        for _ in range(mult):
            out.append(f"{art}-{n}.JPG")
            last_used[art] = n
            n += 1
    return out


def to_text(items: list[str]) -> str:
    """Один элемент на строку (для скачивания .txt)."""
    return "\n".join(items) + ("\n" if items else "")
