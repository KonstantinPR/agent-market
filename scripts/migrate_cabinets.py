"""Раскатка данных public в схемы личных кабинетов (T-40).

Как пользоваться:
    python -m scripts.migrate_cabinets            # dry-run: показать план
    python -m scripts.migrate_cabinets --apply     # выполнить миграцию

Что делает:
  1. ensure_cabinet_schemas  — создаёт схемы кабинетов и их таблицы (идемпотентно);
  2. migrate_public_data     — копирует строки public в схемы кабинетов и
     удаляет public-таблицы (однократно, по маркеру app_schema_state);
  3. cleanup_public_shadows  — дропает пустые остатки кабинетных таблиц в public.

Без --apply ничего не меняет (только печатает план).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


from app.database import Base, SessionLocal, engine  # noqa: E402
from app.services import cabinet_migrate  # noqa: E402


def _ensure_base() -> None:
    """Создаёт отсутствующие общие таблицы (users/cabinets и т.п.) — идемпотентно."""
    import app.models  # noqa: F401  — регистрирует модели в Base.metadata
    Base.metadata.create_all(bind=engine)


def _plan() -> dict:
    """Существующие строки public и куда они переедут (никаких изменений)."""
    plan: dict = {}
    with engine.begin() as conn:
        for t in sorted(cabinet_migrate.PER_CAB_TABLES):
            if not cabinet_migrate._table_exists(conn, "public", t):
                continue
            n = cabinet_migrate._table_count(conn, "public", t)
            plan[t] = {"rows": n, "split": t in cabinet_migrate.MP_SPLIT_TABLES}
    return plan


def _cabinets():
    """Кабинеты (посев идемпотентен и повторяет логику старта сервера)."""
    with SessionLocal() as db:
        from app.services import cabinets as _cs
        return _cs.seed_users_and_cabinets(db) or []


def _print_plan() -> None:
    cabs = _cabinets()
    cab_desc = [str(getattr(c, "code", "?")) + " -> " + str(getattr(c, "schema", "?")) for c in cabs]
    print("Кабинеты:", cab_desc)
    plan = _plan()
    if not plan:
        print("В public нет кабинетных таблиц — миграция ничего не перенесёт.")
        return
    for t, info in plan.items():
        tgt = "по marketplace_id/marketplace" if info["split"] else "целиком в кабинет"
        print(f"  {t:<22} {info['rows']:>6} строк  ({tgt})")
    print("\nПосле переноса строки удаляются из public (идемпотентно, повторный запуск — no-op).")


def run() -> None:
    cabs = _cabinets()
    if not cabs:
        print("Кабинетов нет — миграция не требуется.")
        return
    cabinet_migrate.ensure_cabinet_schemas(engine, cabs)
    ok = cabinet_migrate.migrate_public_data(engine, cabs)
    dropped = cabinet_migrate.cleanup_public_shadows(engine)
    if ok:
        print("Миграция выполнена.")
    else:
        print("Миграция уже была раскатана ранее (маркер app_schema_state='cabinets').")
    if dropped:
        print(f"Убрано пустых теней в public: {dropped}")


def main() -> None:
    _ensure_base()
    if "--apply" in sys.argv[1:]:
        run()
    else:
        _print_plan()


if __name__ == "__main__":
    main()