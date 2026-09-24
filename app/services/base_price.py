"""Базовая (рекомендуемая) цена товара каталога «Наш склад → Товары».

Модель (согласована в Q&A): цена = себестоимость × f(себестоимость) × f(объём).
Оба множителя — лог-линейная интерполяция по опорным точкам (якорям);
за пределами диапазона множитель закрепляется на крайней точке. Объём < 1 л
не повышает наценку. Цена никогда не опускается ниже себестоимости.
Округление «вверх до …9» с шагом по величине цены.

Раздельные коэффициенты по маркетплейсам (WB/Ozon/прочие) — тикет T-23.

Настройки меняются в UI («Установить цены»), хранятся в браузере (localStorage),
в БД не пишутся (как pricing_settings автопилота).
"""
import math

# себестоимость, ₽ → множитель цены
PRICE_DEFAULTS = {
    "cost_anchors": [[100.0, 10.0], [2000.0, 3.0]],
    "vol_anchors": [[1.0, 1.0], [10.0, 2.0]],   # объём, л → множитель цены
    "round_nice": True,                          # округлять вверх до «…9»
    "min_margin_pct": 10.0,                      # мин. прибыль для break-even (минимальная цена)
}

PRICE_LABELS = {
    "cost_anchors": "Наценка от себестоимости",
    "vol_anchors": "Наценка за объём",
    "round_nice": "Округлять до …9",
    "min_margin_pct": "Мин. прибыль, %",
}

PRICE_HINTS = {
    "cost_anchors": "Себестоимость, ₽ → ×Множитель цены. Между строками — плавная "
                    "интерполяция, за краями — крайнее значение. Пример: 100 ₽ → ×10, "
                    "2000 ₽ → ×3. Формат строки: «<себестоимость> ×<множитель>».",
    "vol_anchors": "Объём товара, л → ×Множитель цены сверху. Пример: 1 л → ×1, "
                   "10 л → ×2. Объём меньше 1 л не повышает цену.",
    "round_nice": "Округлять рекомендуемую цену вверх так, чтобы она оканчивалась на 9 "
                  "(например 376 → 379, 3776 → 3799).",
    "min_margin_pct": "Минимальная прибыль сверх расходов (Win×Loss): "
                      "(себестоимость + хранение + логистика + услуги) ÷ (1 − комиссия% − мин.прибыль%). "
                      "0 = точка безубыточности.",
}

_KNOWN_KEYS = set(PRICE_DEFAULTS)
_FLOAT_KEYS = {"min_margin_pct"}


def merge_price_settings(payload=None) -> dict:
    """Накладывает пользовательские настройки на PRICE_DEFAULTS.

    Неизвестные ключи игнорируются; якоря сортируются по первому значению;
    строки без валидной пары отбрасываются; минимум — две опорные точки.
    """
    if not payload:
        return dict(PRICE_DEFAULTS)
    out = dict(PRICE_DEFAULTS)
    for key in _KNOWN_KEYS:
        if key not in payload:
            continue
        val = payload[key]
        if key == "round_nice":
            if isinstance(val, bool):
                out[key] = val
            else:
                out[key] = str(val).lower() in ("1", "true", "on", "yes", "да")
            continue
        if key in _FLOAT_KEYS:
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            out[key] = max(0.0, num)
            continue
        anchors = []
        for a in val or []:
            try:
                anchors.append([float(a[0]), float(a[1])])
            except (TypeError, ValueError, IndexError):
                continue
        if len(anchors) >= 2:
            anchors.sort(key=lambda p: p[0])
            out[key] = anchors
    return out


def _interp_factor(x: float, anchors) -> float:
    """Лог-линейная интерполяция множителя; за пределами — clamp к крайнему."""
    if not anchors:
        return 1.0
    if x <= 0:
        return 1.0
    if len(anchors) == 1:
        return float(anchors[0][1])
    pts = sorted((float(px), float(m)) for px, m in anchors)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    lx = math.log(x)
    for i in range(len(pts) - 1):
        (a0, f0), (a1, f1) = pts[i], pts[i + 1]
        l0, l1 = math.log(a0), math.log(a1)
        if l0 <= lx <= l1:
            t = (lx - l0) / (l1 - l0)
            return math.exp(math.log(f0) + t * (math.log(f1) - math.log(f0)))
    return pts[-1][1]


def _round_nice(price: float) -> float:
    """Вверх до «красивого» числа, оканчивающегося на 9.

    Шаг зависит от величины: <100 ₽ → …9/…4 (шаг 5), <1000 → …9 (шаг 10),
    <10000 → …49/…99 (шаг 50), дальше → …99/…999 (шаг 100).
    Никогда не снижает цену. 0 → 0.
    """
    if price <= 0:
        return 0.0
    if price < 100:
        step = 5
    elif price < 1000:
        step = 10
    elif price < 10000:
        step = 50
    else:
        step = 100
    return float((math.floor(price / step) + 1) * step - 1)


def price_components(cost, vol_l, settings=None) -> dict:
    """Разбивка рекомендуемой цены: множители и итог (без округления)."""
    s = merge_price_settings(settings)
    cost = float(cost or 0)
    vol = float(vol_l or 0)
    f_cost = _interp_factor(cost, s["cost_anchors"])
    f_vol = _interp_factor(vol, s["vol_anchors"])
    raw = cost * f_cost * f_vol
    return {
        "f_cost": round(float(f_cost), 4),
        "f_vol": round(float(f_vol), 4),
        "markup": round(float(f_cost * f_vol), 4),
        "raw": round(float(raw), 2),
    }


def recommended_price(cost, vol_l, settings=None) -> float:
    """Рекомендуемая базовая цена (₽) с округлением, не ниже себестоимости."""
    comp = price_components(cost, vol_l, settings)
    s = merge_price_settings(settings)
    price = comp["raw"]
    if price <= 0:
        return 0.0
    if s["round_nice"]:
        price = _round_nice(price)
    return max(float(price), float(cost or 0))


def minimum_price(cost, ue=None, settings=None) -> float:
    """Минимальная цена без убытка (break-even) на единицу, ₽.

    ue — усреднённая единичная экономика артикула (или глобальная):
    {"comm_rate": доля комиссии от выручки [0..1], "logistics_unit": ₽/шт,
    "storage_unit": ₽/шт, "other_unit": ₽/шт}. Если unit-экономики нет —
    минимум равен себестоимости. Формула: (себестоимость + хранение +
    логистика + услуги) ÷ (1 − комиссия% − мин.прибыль%). Не опускается
    ниже себестоимости, округление не применяется (реальный break-even).
    """
    cost = float(cost or 0)
    if not ue:
        return round(cost, 2)
    s = merge_price_settings(settings)
    denom = 1 - float(ue.get("comm_rate") or 0) - float(s["min_margin_pct"]) / 100
    if denom <= 0.05:
        denom = 0.05
    parts = (float(ue.get("logistics_unit") or 0)
             + float(ue.get("storage_unit") or 0)
             + float(ue.get("other_unit") or 0))
    price = (parts + cost) / denom
    return round(max(float(price), cost), 2)