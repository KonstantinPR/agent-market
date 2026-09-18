"""T-20: модуль рекомендуемой (базовой) цены — лог-интерполяция, clamp, округление.

Модель: цена = себестоимость × f(себестоимость) × f(объём), множители — «×».
"""
import math

import pytest

from app.services import base_price
from app.services.base_price import (
    PRICE_DEFAULTS,
    _interp_factor,
    _round_nice,
    merge_price_settings,
    recommended_price,
)


def test_interp_at_anchor_exact():
    assert _interp_factor(100, PRICE_DEFAULTS["cost_anchors"]) == pytest.approx(10.0)
    assert _interp_factor(2000, PRICE_DEFAULTS["cost_anchors"]) == pytest.approx(3.0)


def test_interp_between_anchors_loglinear():
    # середина в лог-пространстве между ×10 (100) и ×3 (2000) → √(10·3)
    f = _interp_factor(math.sqrt(100 * 2000), PRICE_DEFAULTS["cost_anchors"])
    assert f == pytest.approx((10 * 3) ** 0.5, rel=1e-6)


def test_interp_inside_interval():
    # для 500: t = (ln500−ln100)/(ln2000−ln100) = ln5/ln20; ln f = ln10 + t·ln(3/10)
    t = math.log(5) / math.log(20)
    expected = math.exp(math.log(10) + t * math.log(3 / 10))
    assert _interp_factor(500, PRICE_DEFAULTS["cost_anchors"]) == pytest.approx(expected)


def test_clamp_below_and_above():
    anchors = [[100, 10], [2000, 3]]
    assert _interp_factor(1, anchors) == pytest.approx(10.0)   # дешевле 100 — ×10
    assert _interp_factor(50, anchors) == pytest.approx(10.0)
    assert _interp_factor(5000, anchors) == pytest.approx(3.0)  # дороже 2000 — ×3


def test_volume_below_one_no_boost():
    # объём < 1 л не повышает наценку (clamp к первому якорю ×1)
    assert _interp_factor(0.0, PRICE_DEFAULTS["vol_anchors"]) == pytest.approx(1.0)
    assert _interp_factor(0.4, PRICE_DEFAULTS["vol_anchors"]) == pytest.approx(1.0)
    assert _interp_factor(1, PRICE_DEFAULTS["vol_anchors"]) == pytest.approx(1.0)


def test_volume_interp():
    assert _interp_factor(10, PRICE_DEFAULTS["vol_anchors"]) == pytest.approx(2.0)
    # 2.5 л: t = ln(2.5)/ln(10) ≈ 0.398; f = 2^t ≈ 1.319
    f = _interp_factor(2.5, PRICE_DEFAULTS["vol_anchors"])
    assert f == pytest.approx(2 ** (math.log(2.5) / math.log(10)), rel=1e-6)


def test_recommended_price_multiplies():
    # себестоимость 500, объём 2.5: 500 × f_cost(500) × f_vol(2.5)
    comp = base_price.price_components(500, 2.5)
    f_cost = _interp_factor(500, PRICE_DEFAULTS["cost_anchors"])
    f_vol = _interp_factor(2.5, PRICE_DEFAULTS["vol_anchors"])
    assert comp["f_cost"] == pytest.approx(f_cost, rel=1e-4)
    assert comp["f_vol"] == pytest.approx(f_vol, rel=1e-4)
    assert comp["raw"] == pytest.approx(500 * f_cost * f_vol, rel=1e-4)
    price = recommended_price(500, 2.5)
    assert price == _round_nice(500 * f_cost * f_vol)
    assert price >= 500 * f_cost * f_vol


def test_price_never_below_cost():
    for cost in (10, 100, 500, 2000, 5000):
        assert recommended_price(cost, 0) >= cost - 1e-9
        assert recommended_price(cost, 20) >= cost - 1e-9


def test_round_nice_tiers():
    assert _round_nice(3.5) == 4.0       # шаг 5: …4/…9
    assert _round_nice(12) == 14.0       # шаг 5: …4/…9
    assert _round_nice(999) == 999.0     # уже «вверх»: 1000−1
    assert _round_nice(1000) == 1049.0   # шаг 50: …49
    assert _round_nice(3776) == 3799.0
    assert _round_nice(12400) == 12499.0  # шаг 100: …99
    assert _round_nice(0) == 0.0
    for p in (3.5, 12, 3776, 12400):
        assert _round_nice(p) >= p


def test_round_off_disables_nice():
    price = recommended_price(500, 2.5, {"round_nice": False})
    assert price == pytest.approx(base_price.price_components(500, 2.5)["raw"], abs=0.01)


def test_recommended_cost_100_is_1000():
    # себестоимость 100 ₽, объём 0 → ×10 → 1000 ₽, «вверх до …9» → 1049
    assert recommended_price(100, 0) == 1049.0
    assert recommended_price(100, 0, {"round_nice": False}) == pytest.approx(1000.0)


def test_recommended_volume_boost():
    # один и тот же товар в 8 л дороже, чем в 1 л
    small = recommended_price(1000, 1)
    big = recommended_price(1000, 8)
    assert big > small


def test_merge_price_settings_sanitizes():
    merged = merge_price_settings({
        "cost_anchors": [["4000", "1.5"], [500, 12], [200, 7], "мусор", [1]],
        "vol_anchors": [[10, 2], [1, 1]],
        "round_nice": "0",
        "unknown_key": 123,
    })
    assert merged["cost_anchors"] == [[200.0, 7.0], [500.0, 12.0], [4000.0, 1.5]]
    assert merged["vol_anchors"] == [[1.0, 1.0], [10.0, 2.0]]
    assert merged["round_nice"] is False
    assert "unknown_key" not in merged


def test_merge_round_nice_bool_variants():
    assert merge_price_settings({"round_nice": False})["round_nice"] is False
    assert merge_price_settings({"round_nice": 0})["round_nice"] is False
    assert merge_price_settings({"round_nice": "да"})["round_nice"] is True


def test_merge_price_settings_requires_two_anchors():
    merged = merge_price_settings({"cost_anchors": [[100, 10]]})
    assert merged["cost_anchors"] == PRICE_DEFAULTS["cost_anchors"]