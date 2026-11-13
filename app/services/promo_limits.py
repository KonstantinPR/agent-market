"""Пороги участия в акциях WB («до N ₽») из Excel личного кабинета.

Публичный API WB порогов **автоакций** не отдаёт: `/promotions/nomenclatures`
для auto отвечает 422 (в документации прямо «неприменим для автоакций»). Поэтому
единственный источник — Excel-выгрузка из ЛК: «Цены и скидки» → акция →
«Обновить через Excel». Здесь мы её разбираем и складываем в `wb_promo_limits`.

Семантика: `planPrice` («Плановая цена») — это цена, **до которой** товар может
стоять на витрине и оставаться участником акции. Участие живо, пока витрина
≤ planPrice. Сверено на живых данных: витрина 1 287,90 ≤ пороги 1 299 / 1 374 /
1 388 / 1 450 → 4 акции, и 1 287,90 > 1 273 → в пятой товара нет.

Разбор намеренно эвристический (образца ЛК под рукой не было): ищем строку
заголовков по узнаваемым подписям («Артикул», «Плановая цена», «Цена»). Если
разметка не сошлась — API возвращает найденные колонки, чтобы UI/пользователь
могли задать сопоставление вручную (`column_map`).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app import models

#: Сколько дней пороги считаются свежими. Дата загрузки в ЛК живёт 30 дней,
#: как и минимальные цены автоакций; после — правило не применяется.
DEFAULT_EXPIRE_DAYS = 30.0
#: С какого возраста данных предупреждать в UI/причине.
DEFAULT_WARN_DAYS = 7.0

# Максимум строк, которые сканируем в поисках заголовка (ЛК кладёт шапку сверху).
_HEADER_SCAN_ROWS = 25

_NM_RE = re.compile(r"\d{4,}")


def _norm_header(value) -> str:
    """Подпись колонки → нормализованная строка для распознавания."""
    s = str(value or "").strip().lower().replace("ё", "е")
    s = re.sub(r"[«»\"'`]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _role_of(header: str) -> Optional[str]:
    """Роль колонки по её подписи: nm / plan / price / discount / name."""
    h = _norm_header(header)
    if not h:
        return None
    if ("артикул" in h and "продав" not in h) or "nm_id" in h or "nmid" in h \
            or "номенклатур" in h or "код товара" in h:
        return "nm"
    if "план" in h and "цен" in h:
        return "plan"
    if "plan" in h and "price" in h:
        return "plan"
    if "цена участия" in h or "цена акции" in h or "цена для акции" in h:
        return "plan"
    if "скидк" in h:
        return "discount"
    if "наимен" in h or "назван" in h:
        return "name"
    if "цена" in h and "до скидки" not in h:
        return "price"
    return None


def _map_headers(row: list) -> dict:
    """Строка заголовков → {роль: индекс колонки}. Первая встреченная роль важнее."""
    mapping: dict = {}
    for idx, value in enumerate(row):
        role = _role_of(value)
        if role and role not in mapping:
            mapping[role] = idx
    return mapping


def _score_mapping(mapping: dict) -> int:
    return len(mapping)


def _find_header(raw: pd.DataFrame) -> tuple[int, dict]:
    """Ищет строку заголовков. Возвращает (индекс строки, карта ролей)."""
    best = (-1, {})
    for idx in range(min(_HEADER_SCAN_ROWS, len(raw))):
        row = list(raw.iloc[idx].values)
        mapping = _map_headers(row)
        if _score_mapping(mapping) > _score_mapping(best[1]):
            best = (idx, mapping)
        if "nm" in mapping and ("plan" in mapping or "price" in mapping):
            return idx, mapping
    return best


def _num(value) -> Optional[float]:
    """Цена из ячейки: «1 299,00 ₽» → 1299.0. None, если числа нет."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("nan", "nat", "none", "-"):
        return None
    s = s.replace("\xa0", "").replace(" ", "").replace("₽", "")
    s = s.replace(",", ".")
    # Оставляем цифры, точку и минус (валиют «руб.», «RUB» и т.п.)
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s or s in ("-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _nm(value) -> str:
    """nmID из ячейки: «1099756595» / «1099756595.0» → строка цифр."""
    if value is None:
        return ""
    s = str(value).strip()
    m = _NM_RE.search(s.replace("\xa0", ""))
    return m.group(0) if m else ""


def parse_limits(data: bytes, column_map: Optional[dict] = None,
                 sheet=0) -> tuple[list, dict]:
    """Разбирает Excel ЛК → (строки-пороги, мета).

    Каждая строка: ``{nm_id, plan_price, price?, discount?}``; дубли nm
    схлопываются по максимальному plan_price (разные размеры одного товара —
    участие сохраняется, если хотя бы один размер проходит порог).

    ``column_map`` — ручное сопоставление ``{роль: подпись_колонки}`` (роли:
    nm / plan / price / discount / name) на случай нестандартной выгрузки.
    """
    raw = pd.read_excel(BytesIO(data), sheet_name=sheet, header=None, dtype=str)
    raw = raw.dropna(how="all")
    if raw.empty:
        raise ValueError("файл пуст")
    header_idx, mapping = _find_header(raw)

    if column_map:
        headers = [_norm_header(v) for v in list(raw.iloc[header_idx].values)] \
            if header_idx >= 0 else []
        # Пользователь даёт подписи; ищем их по нормализованному заголовку.
        mapped: dict = {}
        for role, title in column_map.items():
            if role not in ("nm", "plan", "price", "discount", "name"):
                continue
            want = _norm_header(title)
            for idx, h in enumerate(headers):
                if h == want or (want and want in h):
                    mapped[role] = idx
                    break
        if mapped:
            mapping = mapped
            if header_idx < 0:
                header_idx = 0

    if header_idx < 0 or "nm" not in mapping:
        cols = [str(v) for v in list(raw.iloc[0].values) if str(v) != "nan"]
        raise ValueError(
            "не нашёл колонку «Артикул WB» в первых строках файла; "
            "колонки: " + ", ".join(cols[:12]))
    if "plan" not in mapping:
        if "price" in mapping:
            # Единственная «Цена» в файле автоакций — это и есть плановая цена.
            mapping["plan"] = mapping["price"]
            mapping.pop("price", None)
        else:
            raise ValueError(
                "в файле нет колонки «Плановая цена» (и нет колонки «Цена», "
                "которую можно принять за неё)")

    rows: dict = {}
    skipped = 0
    for idx in range(header_idx + 1, len(raw)):
        rec = list(raw.iloc[idx].values)
        nm = _nm(rec[mapping["nm"]]) if mapping["nm"] < len(rec) else ""
        plan = _num(rec[mapping["plan"]]) if mapping["plan"] < len(rec) else None
        if not nm or plan is None or plan <= 0:
            skipped += 1
            continue
        price = None
        if mapping.get("price") is not None and mapping["price"] < len(rec):
            price = _num(rec[mapping["price"]])
        discount = None
        if mapping.get("discount") is not None and mapping["discount"] < len(rec):
            discount = _num(rec[mapping["discount"]])
        prev = rows.get(nm)
        if prev is None or plan > prev["plan_price"]:
            rows[nm] = {
                "nm_id": nm,
                "plan_price": round(plan, 2),
                "price": round(price, 2) if price is not None else None,
                "discount": round(discount, 2) if discount is not None else None,
            }

    meta = {
        "header_row": header_idx + 1 if header_idx >= 0 else None,
        "columns": {role: str(raw.iloc[header_idx, idx])
                    for role, idx in mapping.items()},
        "rows": len(raw) - header_idx - 1,
        "parsed": len(rows),
        "skipped": skipped,
    }
    return list(rows.values()), meta


def infer_promo_id(db, filename: str) -> int:
    """Акция по имени файла: сначала номер promo_id, потом название акции.

    Возвращает 0, если однозначно определить не удалось (файл без привязки).
    """
    name = str(filename or "")
    if not name:
        return 0
    try:
        promos = db.execute(select(models.Promotion)).scalars().all()
    except Exception:  # noqa: BLE001 — нет таблицы не должен ломать импорт
        return 0
    if not promos:
        return 0
    low = name.lower()
    # 1) Число в имени файла, совпадающее с promo_id.
    for num in re.findall(r"\d{3,}", name):
        try:
            pid = int(num)
        except ValueError:
            continue
        if any(int(p.promo_id or 0) == pid for p in promos):
            return pid
    # 2) Название акции в имени файла (без учёта регистра и лишних символов).
    norm = re.sub(r"[^a-zа-я0-9]+", " ", low).strip()
    for p in promos:
        pname = re.sub(r"[^a-zа-я0-9]+", " ", str(p.name or "").lower()).strip()
        if pname and len(pname) >= 4 and pname in norm:
            return int(p.promo_id or 0)
    return 0


def import_limits(db, data: bytes, file_name: str,
                  promo_id: Optional[int] = None,
                  column_map: Optional[dict] = None) -> dict:
    """Импортирует Excel ЛК в `wb_promo_limits` (upsert по promo_id+nm_id).

    Строки, которых нет в новом файле, у той же акции удаляются: файл — полный
    срез порогов, а не дельта. ``promo_id`` не задан → пытаемся вычислить по
    имени файла, иначе 0 («без привязки»).
    """
    rows, meta = parse_limits(data, column_map=column_map)
    if not rows:
        raise ValueError("в файле не нашлось ни одной строки с артикулом и плановой ценой")
    pid = int(promo_id or 0)
    if not pid:
        pid = infer_promo_id(db, file_name)
    now = datetime.utcnow()
    values = []
    for r in rows:
        values.append({
            "promo_id": pid,
            "nm_id": r["nm_id"],
            "plan_price": r["plan_price"],
            "price": r.get("price"),
            "discount": r.get("discount"),
            "source": "excel",
            "file_name": str(file_name or "")[:255],
            "fetched_at": now,
        })
    ins = insert(models.PromoLimit)
    stmt = ins.on_conflict_do_update(
        index_elements=["promo_id", "nm_id"],
        set_={
            "plan_price": ins.excluded.plan_price,
            "price": ins.excluded.price,
            "discount": ins.excluded.discount,
            "source": ins.excluded.source,
            "file_name": ins.excluded.file_name,
            "fetched_at": ins.excluded.fetched_at,
        },
    )
    db.execute(stmt, values)
    nm_ids = [v["nm_id"] for v in values]
    db.execute(
        delete(models.PromoLimit).where(
            models.PromoLimit.promo_id == pid,
            models.PromoLimit.source == "excel",
            models.PromoLimit.nm_id.notin_(nm_ids),
        )
    )
    db.commit()
    meta.update({"promo_id": pid, "imported": len(values),
                 "file_name": str(file_name or "")})
    return meta


def _age_days(fetched_at) -> Optional[int]:
    if fetched_at is None:
        return None
    try:
        return max(0, int((datetime.utcnow() - fetched_at).total_seconds() // 86400))
    except (TypeError, ValueError):
        return None


def limits_map(db, expire_days: float = DEFAULT_EXPIRE_DAYS) -> dict:
    """nm_id → список порогов ``{plan_price, promo_id, source, age_days}``.

    Источники:
      * `wb_promo_limits` — Excel ЛК, строки не старше ``expire_days``;
      * `wb_promo_nomenclatures` — API regular-акций (`plan_price`), только
        участники (`in_action=True`) активных акций.

    Дубли (та же акция и та же цена из двух источников) схлопываются.
    """
    out: dict = {}
    seen: dict = {}

    def add(nm, promo_id, price, source, age):
        nm = str(nm or "")
        price = _num(price)
        if not nm or price is None or price <= 0:
            return
        key = (str(promo_id or 0), round(price, 2))
        if key in seen.get(nm, set()):
            return
        seen.setdefault(nm, set()).add(key)
        out.setdefault(nm, []).append({
            "plan_price": round(price, 2),
            "promo_id": int(promo_id or 0),
            "source": source,
            "age_days": age,
        })

    now = datetime.utcnow()
    cutoff = now - timedelta(days=max(0.0, float(expire_days or 0)))
    try:
        for row in db.execute(
            select(models.PromoLimit).where(models.PromoLimit.fetched_at >= cutoff)
        ).scalars():
            add(row.nm_id, row.promo_id, row.plan_price, "excel",
                _age_days(row.fetched_at))
    except Exception:  # noqa: BLE001 — нет таблицы/колонки не роняет расчёт
        pass

    try:
        rows = db.execute(
            select(
                models.PromoNomenclature.nm_id,
                models.PromoNomenclature.promo_id,
                models.PromoNomenclature.plan_price,
                models.PromoNomenclature.fetched_at,
            )
            .join(models.Promotion,
                  models.Promotion.promo_id == models.PromoNomenclature.promo_id)
            .where(
                models.Promotion.adv_type == "regular",
                models.Promotion.starts_at <= now,
                models.Promotion.ends_at >= now,
                models.PromoNomenclature.in_action.is_(True),
            )
        ).all()
        for nm, pid, price, fetched in rows:
            add(nm, pid, price, "api", _age_days(fetched))
    except Exception:  # noqa: BLE001
        pass

    return out


def promo_names(db) -> dict:
    """promo_id → название акции (для подписей в UI)."""
    try:
        return {int(p.promo_id): (p.name or "")
                for p in db.execute(select(models.Promotion)).scalars()}
    except Exception:  # noqa: BLE001
        return {}


def summary(db) -> dict:
    """Сводка загруженных порогов по акциям — для панели «Пороги акций»."""
    rows = []
    try:
        rows = list(db.execute(
            select(models.PromoLimit).order_by(
                models.PromoLimit.promo_id,
                models.PromoLimit.nm_id,
            )
        ).scalars())
    except Exception:  # noqa: BLE001
        rows = []
    names = promo_names(db)
    groups: dict = {}
    for r in rows:
        pid = int(r.promo_id or 0)
        g = groups.setdefault(pid, {
            "promo_id": pid,
            "promo_name": names.get(pid, ""),
            "products": 0,
            "rows": 0,
            "min_plan_price": None,
            "max_plan_price": None,
            "file_name": "",
            "fetched_at": None,
        })
        g["rows"] += 1
        g["products"] += 1
        price = float(r.plan_price or 0)
        if price > 0:
            g["min_plan_price"] = (price if g["min_plan_price"] is None
                                   else min(g["min_plan_price"], price))
            g["max_plan_price"] = (price if g["max_plan_price"] is None
                                   else max(g["max_plan_price"], price))
        if r.fetched_at and (g["fetched_at"] is None or r.fetched_at > g["fetched_at"]):
            g["fetched_at"] = r.fetched_at
            g["file_name"] = r.file_name or ""
    promotions = []
    for pid in sorted(groups):
        g = groups[pid]
        g["age_days"] = _age_days(g["fetched_at"])
        g["fetched_at"] = g["fetched_at"].isoformat() if g["fetched_at"] else None
        promotions.append(g)
    return {
        "promotions": promotions,
        "total_rows": len(rows),
        "total_products": len({str(r.nm_id) for r in rows}),
    }


def clear(db, promo_id: Optional[int] = None) -> int:
    """Удаляет пороги (все или только выбранной акции). Возвращает число строк."""
    stmt = delete(models.PromoLimit)
    if promo_id is not None:
        stmt = stmt.where(models.PromoLimit.promo_id == int(promo_id))
    res = db.execute(stmt)
    db.commit()
    return int(res.rowcount or 0)
