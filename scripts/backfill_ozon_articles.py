"""Заполняет base_article/size у накопленных данных Ozon (backfill).

Артикул Ozon = артикул товара + размер через последний «-». Колонки нужны,
чтобы отчёты по умолчанию сворачивали размеры в строку товара
(см. app/services/ozon_article.py).

Запуск:
    venv\\Scripts\\python.exe scripts\\backfill_ozon_articles.py [--dry-run]

Скрипт идемпотентен: повторный запуск перезаписывает те же значения.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.services import ozon_article  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill base_article/size для Ozon")
    ap.add_argument("--dry-run", action="store_true",
                    help="только показать, что изменится (ничего не писать)")
    ap.add_argument("--batch", type=int, default=20000, help="размер батча UPDATE")
    args = ap.parse_args()

    if args.dry_run:
        print("dry-run: карта артикулов строится, изменения не применяются")
        return 0

    t0 = time.time()
    db = SessionLocal()
    try:
        stats = ozon_article.backfill(db, batch=args.batch)
    finally:
        db.close()

    total = sum(stats.values())
    print(f"обновлено строк: {total} (за {time.time() - t0:.1f} с)")
    for table, n in stats.items():
        print(f"  {table:20s} {n:>8d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
