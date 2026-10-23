"""Общие утилиты воронки продаж WB (funnel_metric).

Используются и в WB API → Воронка продаж (/api/funnel), и в Прибыльность →
Воронка Продаж WB (/api/margin/funnel): выбор хранимого среза под запрошенный
диапазон и парсинг блоков statistic.past / statistic.comparison в плоские
колонки (past_*, dy_*).
"""
import json


def pick_funnel_window(windows, from_, to_):
    """Выбор хранимого среза funnel_metric под запрошенный диапазон.

    Сначала точное совпадение окна; затем самый широкий срез, целиком лежащий
    внутри запрошенного диапазона; затем самый свежий пересекающийся; иначе None.
    """
    exact = [w for w in windows if w[0] == from_ and w[1] == to_]
    if exact:
        return exact[0]
    inside = [w for w in windows if w[0] >= from_ and w[1] <= to_]
    if inside:
        return max(inside, key=lambda w: ((w[1] - w[0]).days, w[1], w[0]))
    overlap = [w for w in windows if w[0] < to_ and w[1] > from_]
    if overlap:
        return max(overlap, key=lambda w: (w[1], w[0]))
    return None


# Поля блоков statistic.past и statistic.comparison из ответа
# analytics/v3/sales-funnel/products: значения предыдущего периода (past_*)
# и динамика в процентах к нему (dy_*, ключ "…Dynamic").
FUNNEL_PAST_KEYS = {
    "views": "openCount",
    "adds": "cartCount",
    "orders": "orderCount",
    "cancelled": "cancelCount",
    "buyouts": "buyoutCount",
    "revenue": "orderSum",
    "buyout_sum": "buyoutSum",
    "cancel_sum": "cancelSum",
    "avg_price": "avgPrice",
}
FUNNEL_DY_KEYS = {
    "views": "openCountDynamic",
    "adds": "cartCountDynamic",
    "orders": "orderCountDynamic",
    "cancelled": "cancelCountDynamic",
    "buyouts": "buyoutCountDynamic",
    "revenue": "orderSumDynamic",
    "avg_price": "avgPriceDynamic",
}

# Поля «Воронки Продаж WB», доступные для сравнения по периодам.
# key -> (подпись, тип значения, вид итога). Должно совпадать с
# FUNNEL_CMP_FIELDS в app/static/app.js (паритет проверяет tests/unit/test_margin.py).
FUNNEL_CMP_FIELDS = {
    "views": ("Просмотры", "int", "sum"),
    "adds": ("В корзину", "int", "sum"),
    "orders": ("Заказы", "int", "sum"),
    "buyouts": ("Выкупы", "int", "sum"),
    "cancelled": ("Отмены", "int", "sum"),
    "revenue": ("Выручка", "money", "sum"),
    "buyout_sum": ("Сумма выкупов", "money", "sum"),
    "margin": ("Маржа", "money", "sum"),
    "margin_pct": ("Маржа, %", "pct", "avg"),
    "avg_price": ("Ср. цена", "money", "avg"),
    "cart_pct": ("В корзину, %", "pct", "avg"),
    "order_pct": ("Заказы, %", "pct", "avg"),
    "conv_buyout_percent": ("Выкуп, %", "pct", "avg"),
    "product_rating": ("Рейтинг товара", "num", "avg"),
}


def short_window(d_from, d_to) -> str:
    """Короткая подпись окна (ДД.ММ–ДД.ММ.ГГГГ) для колонок сравнения."""
    if d_from == d_to:
        return d_from.strftime("%d.%m.%Y")
    if d_from.year == d_to.year:
        return "%s–%s" % (d_from.strftime("%d.%m"), d_to.strftime("%d.%m.%Y"))
    return "%s–%s" % (d_from.strftime("%d.%m.%Y"), d_to.strftime("%d.%m.%Y"))


def flatten_past_dy(row: dict) -> None:
    """Дополняет строку воронки прошлым периодом (past_*) и динамикой (dy_*).

    Читает и удаляет из row ключи past_json/comparison_json (мутация).
    "Усиливает" суффиксные поля как в /api/funnel.
    """

    def _num(v):
        try:
            f = float(v)
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return 0

    past, comp = {}, {}
    try:
        past = json.loads(row.pop("past_json") or "{}")
    except Exception:  # noqa: BLE001
        row.pop("past_json", None)
    try:
        comp = json.loads(row.pop("comparison_json") or "{}")
    except Exception:  # noqa: BLE001
        row.pop("comparison_json", None)
    for key, src in FUNNEL_PAST_KEYS.items():
        row["past_" + key] = _num(past.get(src, 0))
    for key, src in FUNNEL_DY_KEYS.items():
        row["dy_" + key] = _num(comp.get(src, 0))


def funnel_columns():
    """Список колонок funnel_metric (без date_from/date_to и JSON-блоков)."""
    return [
        "nm_id", "article", "views", "opens", "adds", "orders", "cancelled",
        "buyouts", "avg_price", "revenue", "buyout_sum", "subject_name",
        "brand_name", "product_rating", "feedback_rating", "stock_wb", "stock_mp",
        "stock_balance_sum", "cancel_sum", "avg_orders_per_day",
        "share_order_percent", "add_to_wishlist", "time_to_ready_min",
        "localization_percent", "conv_to_cart_percent",
        "conv_cart_to_order_percent", "conv_buyout_percent",
        "wb_club_order_count", "wb_club_order_sum", "wb_club_buyout_count",
        "wb_club_buyout_sum", "wb_club_cancel_count", "wb_club_cancel_sum",
        "wb_club_avg_price", "wb_club_buyout_percent",
        "wb_club_avg_orders_per_day", "title", "subject_id", "tags",
        "past_json", "comparison_json",
    ]