# -*- coding: utf-8 -*-
"""Запись ключей (creds) в существующую связку-кабинет.

Зачем: у кабинетов нет UI/API для ввода ключей — creds зашиваются в БД
(Fernet, app.services.secrets) при сиде из .env. Для уже созданных связок
(например «ИП Прудников · WB», code=ip_prudnikov_wb) этот скрипт кладёт
ключи в cabinets.creds идемпотентно.

Как пользоваться:
    python -m scripts.set_cabinet_creds                # dry-run: показать план
    python -m scripts.set_cabinet_creds --apply         # записать

По умолчанию берёт WB-токен ИП из .env (WB_API_TOKEN_IP_2) и раскладывает его
в standard/finance/finance2 связки code=ip_prudnikov_wb. Переопределить код
связки можно через --code.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.services import secrets  # noqa: E402


DEFAULT_CODE = "ip_prudnikov_wb"
DEFAULT_MARKETPLACE = "wb"


def _mask(s: str) -> str:
    if not s:
        return "(пусто)"
    if len(s) <= 20:
        return s[:4] + "…"
    return s[:12] + "…" + s[-6:]


def _wb_creds(token: str) -> dict:
    return {"wb": {"standard": token, "finance": token, "finance2": token}}


def main() -> int:
    ap = argparse.ArgumentParser(description="Записать creds в связку-кабинет")
    ap.add_argument("--code", default=DEFAULT_CODE, help="код связки (code из cabinets)")
    ap.add_argument("--marketplace", default=DEFAULT_MARKETPLACE, help="маркетплейс связки")
    ap.add_argument("--apply", action="store_true", help="записать (иначе dry-run)")
    args = ap.parse_args()

    token = (settings.wb_api_token_ip_2 or "").strip()
    if not token:
        print("Ошибка: WB_API_TOKEN_IP_2 пуст в .env — нечего записывать.")
        return 1

    creds = _wb_creds(token)
    if args.marketplace != "wb":
        print(f"Ошибка: скрипт раскладывает только WB-ключи, а связка '{args.marketplace}'.")
        return 1

    with SessionLocal() as db:
        row = db.execute(text(
            "select id, code, marketplace, schema, creds from cabinets where code=:code"
        ), {"code": args.code}).mappings().first()
        if row is None:
            print(f"Ошибка: связка code='{args.code}' не найдена.")
            return 1

        before = secrets.decrypt(row["creds"] or "")
        print(f"Связка: id={row['id']} code={row['code']} schema={row['schema']} "
              f"marketplace={row['marketplace']}")
        print(f"Токен из .env: {_mask(token)}")
        print(f"Было (расшифровано): ключи={list(before.keys()) or '(нет)'}")
        print(f"Станет: {list(creds.keys())} -> "
              f"{list(creds['wb'].keys())} = {_mask(token)}")

        if not args.apply:
            print("Dry-run: ничего не изменено (запустите с --apply).")
            return 0

        db.execute(text(
            "update cabinets set creds=:c where code=:code"
        ), {"c": secrets.encrypt(creds), "code": args.code})
        db.commit()

        chk = secrets.decrypt(db.execute(text(
            "select creds from cabinets where code=:code"
        ), {"code": args.code}).scalar())
        stored = (chk.get("wb") or {})
        ok = all(stored.get(k) == token for k in ("standard", "finance", "finance2"))
        print("Записано." if ok else "ВНИМАНИЕ: расшифровка после записи не совпала!")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
