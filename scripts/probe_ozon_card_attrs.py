"""Проба атрибутов карточек Ozon: есть ли в CSV отчёта по товарам колонка размера.

Зачем: ``normalize_oz_cards`` сейчас кладёт в ``marketplace_cards.size`` пустую
строку, а размер для кросс-листингованных с WB товаров выводится из хвоста
артикула. У Ozon-собственных товаров хвост артикула — это цвет
(``TIE-BIGBAN-17-PINKMILK``), поэтому размер остаётся пустым (68–85% строк в
placements/stocks/detail). Если в отчёте Ozon есть атрибут размера, его можно
пробросить в карточки и закрыть пробел.

Проба намеренно не зовёт ``OzonProvider.get_cards()``: тот ждёт отчёт до 25×20 с.
Здесь своя ограниченная попытка, чтобы при лимите 429 (или долгой генерации
отчёта) скрипт падал быстро и его можно было повторить позже.

Запуск (нужны OZON_CLIENT_ID/OZON_API_KEY в .env):
    venv\\Scripts\\python.exe scripts\\probe_ozon_card_attrs.py [--attempts 6] [--rows 3]
"""
import argparse
import io
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SIZE_RE = re.compile(r"размер|size|габарит", re.IGNORECASE)


def _fetch_csv(prov, attempts: int):
    from app.providers.ozon import OZON_API

    resp = prov._post(f"{OZON_API}/v1/report/products/create", {
        "language": "DEFAULT", "offer_id": [], "search": "", "sku": [],
        "visibility": "ALL",
    })
    code = resp.json()["result"]["code"]
    for _ in range(attempts):
        info = prov._post(f"{OZON_API}/v1/report/info", {"code": code})
        result = info.json()["result"]
        status = result.get("status")
        if status == "success":
            import requests
            return requests.get(result["file"], timeout=300).content
        if status not in ("processing", "waiting"):
            raise RuntimeError(f"отчёт по карточкам: статус {status}")
        time.sleep(20)
    raise TimeoutError(f"отчёт по карточкам не готов за {attempts * 20} с")


def main() -> int:
    ap = argparse.ArgumentParser(description="Проба колонок отчёта Ozon по товарам")
    ap.add_argument("--attempts", type=int, default=6,
                    help="сколько раз ждать готовности отчёта (20 с между попытками)")
    ap.add_argument("--rows", type=int, default=3, help="сколько примеров значений")
    args = ap.parse_args()

    import pandas as pd

    from app.providers.ozon import OzonApiError, OzonProvider

    prov = OzonProvider()
    try:
        raw = _fetch_csv(prov, args.attempts)
    except OzonApiError as e:
        print(f"Ozon API: {e}")
        print("Проба не удалась — обычно это лимит 429. Повторить позже.")
        return 1
    except (TimeoutError, RuntimeError) as e:
        print(f"Отчёт не получен: {e}")
        print("Причины: лимит 429 или отчёт ещё генерируется (--attempts больше).")
        return 1

    df = pd.read_csv(io.BytesIO(raw), sep=";", dtype=str)
    cols = [str(c) for c in df.columns]
    print(f"строк: {len(df)}, колонок: {len(cols)}\n")

    print("-- колонки, похожие на размер --")
    hits = [c for c in cols if SIZE_RE.search(c)]
    if not hits:
        print("нет ни одной подходящей колонки")
    for c in hits:
        vals = df[c].fillna("").astype(str).str.strip()
        non_empty = vals[vals != ""]
        print(f"  {c!r}: непустых {len(non_empty)} из {len(df)}, "
              f"примеры {non_empty.value_counts().head(args.rows).to_dict()}")

    print("\n-- все колонки --")
    for c in cols:
        print(f"  {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())