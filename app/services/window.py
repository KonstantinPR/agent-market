"""Работа с периодами: единый парсер окон дат для всей системы."""

from datetime import date, timedelta
from typing import Optional

from app.config import settings


def parse_window(date_from=None, date_to=None, default_days: Optional[int] = None):
    """Разрешает строки дат (YYYY-MM-DD) в (date, date).

    Пустое значение = окно по умолчанию (сегодня минус sync_days_default).
    """
    to_ = date.today()
    days = default_days if default_days is not None else settings.sync_days_default
    from_ = to_ - timedelta(days=days)
    if date_from:
        from_ = date.fromisoformat(str(date_from))
    if date_to:
        to_ = date.fromisoformat(str(date_to))
    return from_, to_