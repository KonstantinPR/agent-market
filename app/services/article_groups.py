"""Группы товаров по артикулу: эталонная карта префиксов серий.

Вынесено из `dashboard.py` (T-81), чтобы автопилот цен мог пользоваться той же
группировкой без циклического импорта: дашборд и pricing берут `prefix_group`
отсюда, а дашборд реэкспортирует имя для обратной совместимости.

Матчинг — по началу артикула, самый длинный ключ побеждает (SOHO-FRNT раньше
SOHO, JZ2 раньше JZ/J) — дефисные ключи ловятся у сужений вида SOHO-FRNT-2S.
Промах — группа «остальные».
"""

PREFIXES_ART_DICT = {
    "SHK": "SH",
    "SH": "SH",
    "SK": "SK",
    "SN": "SK",
    "SF": "SF",
    "J": "J",
    "JZ2": "JZ",
    "JZ": "JZ",
    "MIT": "MIT",
    "AN": "MIT",
    "MK": "MIT",
    "IBZ": "IBZ",
    "TIE": "TIE",
    "FATA": "F",
    "V": "F",
    "GR": "GR",
    "LQ3": "LQ",
    "LQ": "LQ",
    "SOHO-FRNT": "SOHO-FRNT",
    "SOHOCOAT": "SOHO",
    "SOHO": "SOHO",
    "TG": "TG",
    "SEL": "STL",
    "KP": "KR",
    "MHSORT": "MHSORT",
    "MHSL": "MHS",
    "MHSE": "MHS",
    "MHSB": "MHS",
    "MHBB": "MHS",
    "MHS": "MHS",
    "BOL": "BIJ",
    "OM": "BIJ",
    "Y00": "Y00",
    "PRS": "PRS",
    "IANCO": "IANCO",
}

_PREFIX_KEYS = tuple(sorted(PREFIXES_ART_DICT, key=len, reverse=True))

OTHER = "остальные"


def prefix_group(article) -> str:
    """Группа товара по PREFIXES_ART_DICT (самый длинный ключ у начала артикула)."""
    if article is None:
        return OTHER
    s = str(article).strip().upper()
    if not s:
        return OTHER
    for key in _PREFIX_KEYS:
        if s.startswith(key):
            return PREFIXES_ART_DICT[key]
    return OTHER


def article_token(article) -> str:
    """Префикс артикула до первого дефиса: «JZA-123-1» → «JZA».

    Последняя ступень иерархии оценки себестоимости (T-81): когда ни группа,
    ни наименование не дали соседей с себестоимостью, остаётся «сужение» модели
    внутри серии.
    """
    if article is None:
        return ""
    s = str(article).strip().upper()
    if not s:
        return ""
    return s.split("-", 1)[0].strip()
