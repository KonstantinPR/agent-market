from datetime import date, timedelta

import pytest

from app.services.window import parse_window


def test_default_window_is_today_minus_default_days():
    from_, to_ = parse_window()
    assert to_ == date.today()
    assert from_ == date.today() - timedelta(days=30)


def test_custom_dates():
    from_, to_ = parse_window("2026-08-01", "2026-08-31")
    assert from_ == date(2026, 8, 1)
    assert to_ == date(2026, 8, 31)


def test_partial_dates():
    from_, to_ = parse_window("2026-08-01")
    assert from_ == date(2026, 8, 1)
    assert to_ == date.today()


def test_invalid_date_raises():
    with pytest.raises(ValueError):
        parse_window("not-a-date")