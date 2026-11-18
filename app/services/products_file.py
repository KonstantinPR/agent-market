# -*- coding: utf-8 -*-
"""T-82: каталог себестоимости из общего файла ``products.xlsx``.

Себестоимость и «ценность» товаров раньше попадали в каталог только вручную —
загрузкой файла в разделе «Товары» (``POST /api/import/net-cost``). Из-за этого
у части артикулов (например, 36 платьев ``JZA*``) в БД было 0 ₽, и автопилот
считал пол по оценке соседей (T-81), а не по факту.

Теперь основной источник — общий файл на Google Drive (по умолчанию
``E:\\Мой диск\\ОБЩИЕ\\products.xlsx``). Путь живёт в «Личные кабинеты →
Ссылки и хранилища» (ключ ``products_file``), а сервис:

* читает файл только когда у него изменился mtime (кэш в памяти);
* пишет в каталог ``products`` через ``sync_service.upsert_products``;
* **не затирает** нулями: себестоимость пишется только там, где в файле > 0,
  «ценность» — только где > 0, поставщик — только непустой;
* синхронизируется в фоне при старте приложения, при изменении mtime
  (наблюдатель раз в ``WATCH_INTERVAL`` секунд) и вручную кнопкой в UI.

Приоритет источников себестоимости в расчёте: файл → каталог БД → оценка
соседей (T-81). После синхронизации ``net_cost_est`` у таких строк снимается
сам: оценка включается только при нулевой себестоимости.
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.services import storage_settings

#: Как часто наблюдатель проверяет mtime файла (секунды).
WATCH_INTERVAL = 300

#: Заголовки файла → колонки каталога. Файл ведётся вручную, поэтому
#: поддерживаем и русские названия, и служебные имена.
COLUMN_RENAME = {
    "Артикул": "article",
    "Артикул поставщика": "article",
    "article": "article",
    "Себестоимость": "net_cost",
    "Себестоимость БАЗА": "net_cost",
    "net_cost": "net_cost",
    "Ценность": "value",
    "pure_value": "value",
    "value": "value",
    "Поставщик": "supplier",
    "supplier": "supplier",
}

_LOCK = threading.Lock()
_WATCH_STARTED = False
_STATE: dict[str, Any] = {
    "path": "",
    "exists": False,
    "file_mtime": None,
    "file_size": 0,
    "synced_at": None,
    "rows": 0,
    "updated": 0,
    "error": None,
    "running": False,
}


# ── путь и состояние ────────────────────────────────────────────────────────

def path(db: Optional[Session] = None) -> Path:
    """Путь к файлу каталога из настроек хранилищ."""
    return Path(storage_settings.products_file(db))


def _file_stat(p: Path) -> Optional[dict]:
    try:
        st = p.stat()
    except OSError:
        return None
    if not p.is_file():
        return None
    return {
        "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "size": int(st.st_size),
    }


def public_state() -> dict:
    """Состояние синхронизации для UI (без обращения к файлу)."""
    return dict(_STATE)


# ── чтение файла ────────────────────────────────────────────────────────────

def read_frame(p: Path) -> pd.DataFrame:
    """Прочитать файл и привести к колонкам каталога.

    Возвращает строки с непустым артикулом и себестоимостью > 0 — нули в файле
    означают «не заполнено», и затирать ими каталог нельзя.
    """
    df = pd.read_excel(p)
    if df is None or df.empty:
        return pd.DataFrame(columns=["article", "net_cost", "value", "supplier"])
    df = df.rename(columns={k: v for k, v in COLUMN_RENAME.items() if k in df.columns})
    if "article" not in df.columns or "net_cost" not in df.columns:
        raise ValueError(
            "в файле нет колонок «article» и «net_cost» (или «Артикул»/«Себестоимость»)"
        )
    out = pd.DataFrame({
        "article": df["article"].astype(str).str.strip(),
        "net_cost": pd.to_numeric(df["net_cost"], errors="coerce"),
    })
    if "value" in df.columns:
        out["value"] = pd.to_numeric(df["value"], errors="coerce")
    if "supplier" in df.columns:
        out["supplier"] = (df["supplier"].astype(str).str.strip()
                           .replace({"nan": "", "None": ""}))
    out = out[(out["article"] != "") & (out["article"].str.lower() != "nan")]
    out = out[out["net_cost"].fillna(0) > 0]
    out = out.drop_duplicates("article", keep="last")
    return out


# ── синхронизация ───────────────────────────────────────────────────────────

def sync(db: Session, force: bool = False) -> dict:
    """Записать себестоимость/ценность/поставщика из файла в каталог.

    ``force=False`` — если файл с прошлой синхронизации не менялся (тот же
    mtime и размер), ничего не делает. Возвращает публичное состояние.
    """
    p = path(db)
    _STATE["path"] = str(p)
    stat = _file_stat(p)
    if stat is None:
        _STATE.update(exists=False, file_mtime=None, file_size=0,
                      error=f"файл не найден: {p}")
        return public_state()
    _STATE.update(exists=True, file_mtime=stat["mtime"], file_size=stat["size"])

    if not force and _STATE.get("synced_at") and _STATE.get("error") is None:
        if (_STATE.get("file_mtime") == stat["mtime"]
                and int(_STATE.get("file_size") or 0) == stat["size"]):
            return public_state()

    if not _LOCK.acquire(blocking=False):
        # Уже идёт синхронизация (например, из наблюдателя) — не дублируем.
        return public_state()
    try:
        _STATE.update(running=True, error=None)
        df = read_frame(p)
        updated = 0
        if not df.empty:
            cost = df[["article", "net_cost"]].copy()
            supplier = (df.loc[df["supplier"] != "", ["article", "supplier"]]
                        if "supplier" in df.columns else None)
            if supplier is not None and not supplier.empty:
                cost = cost.merge(supplier, on="article", how="left")
            # Себестоимость + поставщик (если заполнен). Ценность — отдельно:
            # в файле она бывает пустой, а затирать ею каталог нельзя.
            updated = storage_upsert(db, cost)
            if "value" in df.columns:
                value = df.loc[df["value"].fillna(0) > 0, ["article", "value"]]
                if not value.empty:
                    storage_upsert(db, value)
        _STATE.update(
            synced_at=datetime.now().isoformat(timespec="seconds"),
            rows=int(len(df)),
            updated=int(updated),
            error=None,
        )
    except Exception as exc:  # noqa: BLE001 — синхронизация не должна ронять приложение
        # Если упала запись в БД, транзакция уже aborted — иначе следующий
        # public_payload/запрос в этой сессии упадёт «current transaction is aborted».
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        _STATE.update(error=f"{type(exc).__name__}: {exc}",
                      synced_at=datetime.now().isoformat(timespec="seconds"))
    finally:
        _STATE.update(running=False)
        _LOCK.release()
    return public_state()


def storage_upsert(db: Session, df: pd.DataFrame) -> int:
    """Обёртка над ``sync_service.upsert_products`` (ленивый импорт)."""
    from app.services import sync as sync_service

    return int(sync_service.upsert_products(db, df))


def maybe_sync(db: Session) -> dict:
    """Синхронизировать, только если файл изменился (mtime/размер)."""
    return sync(db, force=False)


# ── фоновые запуски ─────────────────────────────────────────────────────────

def _new_session():
    from app.database import SessionLocal

    return SessionLocal()


def startup_sync() -> None:
    """Фоновая синхронизация при старте приложения."""
    db = _new_session()
    try:
        sync(db, force=False)
    except Exception:  # noqa: BLE001 — старт приложения не должен падать
        pass
    finally:
        db.close()


def _watch_loop() -> None:
    import time

    while True:
        time.sleep(WATCH_INTERVAL)
        db = _new_session()
        try:
            maybe_sync(db)
        except Exception:  # noqa: BLE001
            pass
        finally:
            db.close()


def start_watcher() -> Optional[threading.Thread]:
    """Запустить наблюдатель mtime (daemon-нить, один на процесс)."""
    global _WATCH_STARTED
    if _WATCH_STARTED:
        return None
    _WATCH_STARTED = True
    thread = threading.Thread(target=_watch_loop, daemon=True, name="products-file-watch")
    thread.start()
    return thread
