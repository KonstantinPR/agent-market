# -*- coding: utf-8 -*-
"""Генератор docs/api.md (карта эндпоинтов) и таблицы размеров в docs/architecture.md.

Карта строится из реального FastAPI-приложения (app.main:app), поэтому не
протухает при добавлении/переименовании роутов. Таблица размеров — из файлов
на диске; роли захардкожены ниже (меняются редко, строки — часто).

Запуск:
    python scripts/gen_api_map.py           # перегенерить оба документа
    python scripts/gen_api_map.py --check   # exit 1, если документы устарели
"""
from __future__ import annotations

import argparse
import io
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Файл -> роль (см. docs/architecture.md). Порядок не важен, таблица сортируется
# по числу строк по убыванию.
FILE_ROLES = {
    "app/api.py": "все HTTP-эндпоинты, один `APIRouter(prefix=\"/api\")`",
    "app/static/app.js": "весь фронтенд (вкладки, таблицы, fetch к /api)",
    "app/static/index.html": "разметка: навигация `data-tab` + секции `tab-*`",
    "app/static/style.css": "стили (панели «Вид таблицы», тулбары, таблицы)",
    "app/models.py": "SQLAlchemy-модели (схема БД)",
    "app/database.py": "engine/session PostgreSQL",
    "app/config.py": "`Settings`, читается из `.env`",
    "app/main.py": "сборка FastAPI-приложения, `/`, статика",
    "app/services/sync.py": "запись выгрузок WB/Ozon в БД",
    "app/services/pricing.py": "автопилот цен: R1–R11, расчёт скидок",
    "app/services/refresh.py": "`pull_*`-функции, фоновые задания, «Обновить WB/Ozon»",
    "app/services/margin.py": "маржинальность по детализациям, группировка артикулоразмеров",
    "app/services/replenish.py": "подсортировка WB, план дефицита, скорость продаж",
    "app/services/dashboard.py": "сводные панели дашборда",
    "app/services/tickets.py": "реестр тикетов `TICKETS.md` (create/validate/report)",
    "app/services/pdf_demand.py": "PDF «Потребность в товаре»",
    "app/services/photos.py": "индекс фото на диске, выбор папки, fallback по префиксу",
    "app/services/thumbs.py": "миниатюры PDF в `data/thumbs/`",
    "app/services/warehouse.py": "складские документы (приход/отгрузка, обороты)",
    "app/services/ozon_article.py": "резолвер артикула/размера Ozon (`base_article`)",
    "app/services/base_price.py": "рекомендуемая (базовая) цена",
    "app/services/excel_import.py": "разбор отредактированного Excel «Потребность»",
    "app/services/excel_io.py": "чтение/запись Excel",
    "app/services/funnel.py": "воронка продаж (агрегация)",
    "app/services/yandex_disk.py": "загрузка отчётов на Яндекс.Диск",
    "app/services/common.py": "общие хелперы (wildcard-фильтр `like_*`, окна)",
    "app/services/window.py": "окна дат",
    "app/providers/wb.py": "HTTP к Wildberries",
    "app/providers/ozon.py": "HTTP к Ozon",
    "app/providers/factory.py": "точка подмены провайдеров в тестах",
    "app/providers/base.py": "базовый провайдер",
    "app/providers/errors.py": "нормализованные ошибки апстримов",
    "scripts/init_db.py": "создание схемы + идемпотентные ALTER",
    "scripts/load_sample.py": "демо-данные",
    "tests/js/test_js_helpers.py": "проверка app.js через Node `vm`",
    "tests/api/test_endpoints.py": "HTTP-эндпоинты через TestClient",
    "tests/unit/test_margin.py": "юнит-тесты маржинальности",
}

API_MD = ROOT / "docs" / "api.md"
ARCH_MD = ROOT / "docs" / "architecture.md"
BEGIN = "<!-- BEGIN:file-stats -->"
END = "<!-- END:file-stats -->"
GENERATED = (
    "<!-- Сгенерировано: python scripts/gen_api_map.py -- не редактировать руками. -->"
)


def _write(path: Path, text: str) -> None:
    """Пишет UTF-8 без BOM и без CRLF (Python 3.9 не знает write_text(newline=))."""
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _count_lines(path: Path) -> int:
    with path.open(encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def file_stats_md() -> str:
    rows = []
    for rel, role in FILE_ROLES.items():
        p = ROOT / rel
        if not p.exists():
            raise SystemExit(f"нет файла из FILE_ROLES: {rel}")
        rows.append((_count_lines(p), rel, role))
    rows.sort(reverse=True)
    lines = ["| Файл | Строк | Роль |", "|---|---|---|"]
    lines += [f"| `{rel}` | {n} | {role} |" for n, rel, role in rows]
    return "\n".join(lines)


def _first_docstring(fn) -> str:
    doc = (fn.__doc__ or "").strip().splitlines()
    return doc[0].strip() if doc else ""


def api_md() -> str:
    from app.main import app  # импорт здесь: скрипт должен жить в scripts/

    routes = [r for r in app.routes if hasattr(r, "methods")]
    api_routes = [r for r in routes if r.path.startswith("/api")]
    api_routes.sort(key=lambda r: (r.path, ",".join(sorted(r.methods))))

    groups: dict[str, list] = {}
    for r in api_routes:
        rest = r.path[len("/api"):].lstrip("/")
        group = rest.split("/", 1)[0] if rest else "(корень)"
        groups.setdefault(group, []).append(r)

    out = [
        "# Карта API",
        "",
        GENERATED,
        "",
        f"Всего эндпоинтов `/api/*`: **{len(api_routes)}**, "
        f"групп: **{len(groups)}**. Источник — реестр роутов FastAPI "
        "(`app.main:app`), поэтому список всегда совпадает с кодом.",
        "",
        "Общий префикс и общие `Depends` заданы в `app/api.py`. "
        "Все ответы — JSON или файл (`StreamingResponse`/`FileResponse`); "
        "`response_model` сейчас почти не используется — схему смотреть "
        "в теле хендлера.",
        "",
        "## Содержание",
        "",
    ]
    for g in sorted(groups):
        out.append(f"- [{g}](#{_anchor(g)}) — {len(groups[g])} шт.")
    out.append("")

    for g in sorted(groups):
        out += [f"## {g}", "", "| Метод | Путь | Функция | Описание |", "|---|---|---|---|"]
        for r in groups[g]:
            methods = ", ".join(sorted(m for m in r.methods if m != "HEAD"))
            fn = r.endpoint
            doc = _first_docstring(fn) or ""
            doc = doc.replace("|", "\\|")
            out.append(
                f"| {methods} | `{r.path}` | `{getattr(fn, '__name__', '?')}` | {doc} |"
            )
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _anchor(text: str) -> str:
    return re.sub(r"[^a-zа-я0-9-]", "", text.lower().replace(" ", "-"))


def _sync_architecture(stats: str) -> bool:
    """Обновляет блок file-stats в architecture.md. True — файл изменился."""
    if not ARCH_MD.exists():
        raise SystemExit(f"нет {ARCH_MD}")
    text = ARCH_MD.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise SystemExit(f"в {ARCH_MD.name} нет маркеров {BEGIN} / {END}")
    new_block = f"{BEGIN}\n{stats}\n{END}"
    start = text.index(BEGIN)
    end = text.index(END) + len(END)
    updated = text[:start] + new_block + text[end:]
    changed = updated != text
    if changed:
        _write(ARCH_MD, updated)
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="не писать, а проверить актуальность (exit 1 = устарело)")
    args = ap.parse_args()

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    stats = file_stats_md()
    api = api_md()

    stale = []
    if not API_MD.exists() or API_MD.read_text(encoding="utf-8") != api:
        stale.append(str(API_MD.relative_to(ROOT)))
    if not ARCH_MD.exists():
        raise SystemExit(f"нет {ARCH_MD}")

    if args.check:
        text = ARCH_MD.read_text(encoding="utf-8")
        if BEGIN not in text or END not in text:
            stale.append(str(ARCH_MD.relative_to(ROOT)) + " (нет маркеров)")
        else:
            cur = text[text.index(BEGIN):text.index(END) + len(END)]
            if cur != f"{BEGIN}\n{stats}\n{END}":
                stale.append(str(ARCH_MD.relative_to(ROOT)))
        if stale:
            print("Устарело: " + ", ".join(stale) + " — запустите python scripts/gen_api_map.py")
            return 1
        print("docs/api.md и docs/architecture.md актуальны.")
        return 0

    _write(API_MD, api)
    changed = _sync_architecture(stats)
    print(f"docs/api.md записан ({API_MD.stat().st_size} байт); "
          f"таблица в architecture.md {'обновлена' if changed else 'без изменений'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
