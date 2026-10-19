# -*- coding: utf-8 -*-
"""Массовые действия по связкам: «Запустить» у кнопки связки.

Один запуск прогоняет выбранные действия по списку связок в правильном порядке:

    обновление данных → пересчёт автопилота → отправка коррекции цен →
    шаблон мин. цены автоакций

Связки обрабатываются по очереди, главная — первой (чтобы зависимые связки,
когда включён follow_main, успели увидеть свежую рекомендацию главной). Каждое
действие идёт под своей схемой связки и со своими ключами — status quo
активной связки запроса (кука agent_cabinet) здесь не используется.

Разрушающее действие одно — «отправить коррекцию цен» (реальная запись в API
маркетплейса). Оно отмечается в реестре ACTIONS флагом write и в UI выключено
по умолчанию; /api/batch/preview показывает, сколько изменений уйдёт по каждой
связке, до подтверждения.

Задания живут в памяти процесса (как refresh-задания): фоновая нить пишет
job_state, UI опрашивает GET /api/batch/{job_id}.
"""
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.database import SessionLocal
from app.providers import factory as provider_factory
from app.services import cabinets as cabinet_service
from app.services import pricing as pricing_service
from app.services import pricing_ozon as pricing_ozon_service
from app.services import refresh as refresh_service
from app.services.cabinets import apply_search_path, pin_search_path

#: Действия пайплайна в порядке выполнения. write — реальная запись в API
#: маркетплейса (требует подтверждения), mp — на каких маркетплейсах применимо.
ACTIONS = [
    {"id": "refresh", "label": "Обновить данные", "write": False},
    {"id": "recalc", "label": "Пересчитать автопилот", "write": False},
    {"id": "apply", "label": "Отправить коррекцию цен", "write": True},
    {"id": "minpromo", "label": "Мин. цены автоакций", "write": False, "mp": ("wb",)},
]

_ACTION_BY_ID = {a["id"]: a for a in ACTIONS}

SCOPES = ("all", "active", "main")

_LOCK = threading.Lock()
_SEQ = 0
_JOBS: dict = {}

#: Сгенерированные шаблоны мин. цены: job_id -> {cab_id: путь к .xlsx}
_MINPROMO_DIR = BASE_DIR / "data" / "batch"


def _next_id() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def _now() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


# --------------------------------------------------------------- реестр/скоп

def actions_public() -> list:
    """Описание действий для UI (порядок, подписи, что необратимо)."""
    return [{"id": a["id"], "label": a["label"], "write": bool(a["write"])}
            for a in ACTIONS]


def _normalize_actions(actions) -> list:
    if not actions:
        return []
    out = []
    for a in actions:
        aid = str(a).strip()
        if aid in _ACTION_BY_ID and aid not in out:
            out.append(aid)
    # Порядок — как в реестре (refresh → recalc → apply → minpromo).
    return [a["id"] for a in ACTIONS if a["id"] in out]


def _resolve_cabinets(db: Session, user, scope: str) -> list:
    """Список связок запуска: все включённые с ключами / активная / главная.

    Главная связка ставится первой (зависимые следуют за ней при follow_main).
    """
    scope = scope if scope in SCOPES else "all"
    if scope == "active":
        info = cabinet_service.resolve_active(db, None, user)
        links = [info] if info else []
    elif scope == "main":
        info = cabinet_service.resolve_main(db, user)
        links = [info] if info else []
    else:
        links = []
        for c in cabinet_service.list_cabinets(db, user):
            if not c.enabled:
                continue
            info = cabinet_service.info_by_id(db, c.id, user)
            if info is None or not info.creds_for(info.marketplace):
                continue  # без ключей нельзя: провайдер уйдёт на .env-продавца
            links.append(info)
    main = cabinet_service.resolve_main(db, user)
    if main is not None and len(links) > 1:
        order = {info.id: i for i, info in enumerate(links)}
        links.sort(key=lambda i: (0 if i.id == main.id else 1, order.get(i.id, 0)))
    return links


def _cab_dict(info) -> dict:
    return {
        "id": info.id, "name": info.name, "owner": info.owner(),
        "marketplace": info.marketplace,
    }


# ------------------------------------------------------------ выборки/расчёт

def _mp_label(mp: str) -> str:
    return {"wb": "WB", "ozon": "Ozon"}.get(mp or "", (mp or "").upper())


def _settings(db: Session, info) -> dict:
    return cabinet_service.get_pricing_settings(db, info.id) or {}


def _recalc(db: Session, info, settings: dict) -> dict:
    """Рекомендации автопилота связки (read-only) по её маркетплейсу."""
    if info.marketplace == "ozon":
        return pricing_ozon_service.recommendations_ozon(db, settings)
    prices_df, _u, _s = pricing_service.resolve_prices(db)
    main_map = pricing_service.main_influence_map(db, settings)
    return pricing_service.recommendations(
        db, settings, prices_df=prices_df, main_map=main_map)


def _is_candidate(row: dict) -> bool:
    return row.get("action") in ("RAISE", "LOWER", "HALVE")


def _provider_for(info):
    if info.marketplace == "ozon":
        return provider_factory.get_oz_provider(
            credentials=info.creds_for("ozon") or None)
    return provider_factory.get_wb_provider(
        credentials=info.creds_for("wb") or None)


def candidates_count(db: Session, info, settings: Optional[dict] = None) -> int:
    """Сколько изменений уйдёт при «отправить коррекцию цен» по связке."""
    settings = settings if settings is not None else _settings(db, info)
    # apply принудительно показывает и «мёртвые» товары (HALVE) — как сам apply.
    ss = dict(pricing_service.merge_settings(settings))
    ss["show_zero"] = True
    rec = _recalc(db, info, ss)
    return sum(1 for r in (rec.get("rows") or []) if _is_candidate(r))


# ------------------------------------------------------------------ preview

def preview(db: Session, user, actions, scope: str) -> dict:
    """Предпросмотр без записи: сколько изменений уйдёт по каждой связке."""
    acts = _normalize_actions(actions)
    links = _resolve_cabinets(db, user, scope)
    out = []
    for info in links:
        row = _cab_dict(info)
        row["counts"] = {}
        if any(a in ("recalc", "apply", "minpromo") for a in acts):
            try:
                apply_search_path(db, info.schema)
                db._active_info = info
                settings = _settings(db, info)
                n = candidates_count(db, info, settings)
                for a in acts:
                    if a in ("recalc", "apply", "minpromo"):
                        row["counts"][a] = n
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)[:200]
        out.append(row)
    return {"cabs": out, "actions": acts, "scope": scope}


# ----------------------------------------------------------------- состояние

def _add_step(job: dict, cab: dict, action: str, label: str) -> dict:
    step = {"action": action, "label": label, "cab_id": cab["id"], "status": "pending",
            "rows": 0, "note": "", "error": ""}
    cab["steps"].append(step)
    job["steps"].append(step)
    return step


def _flat_label(cab: dict, action_label: str) -> str:
    return f'{cab["owner"]} · {action_label}'


def job_state(job_id: int) -> Optional[dict]:
    job = _JOBS.get(job_id)
    if job is None:
        return None
    return {
        "id": job["id"], "status": job["status"], "scope": job["scope"],
        "actions": job["actions"], "started_at": job["started_at"],
        "finished_at": job["finished_at"], "ok": job["ok"], "failed": job["failed"],
        "steps": job["steps"],
        "cabs": [{"id": c["id"], "name": c["name"], "owner": c["owner"],
                  "marketplace": c["marketplace"], "status": c["status"],
                  "steps": c["steps"]} for c in job["cabs"]],
    }


def active_jobs() -> list:
    return sorted(
        [{"id": j["id"], "status": j["status"], "scope": j["scope"],
          "actions": j["actions"], "started_at": j["started_at"],
          "finished_at": j["finished_at"], "ok": j["ok"], "failed": j["failed"]}
         for j in _JOBS.values()],
        key=lambda j: j["id"], reverse=True)[:20]


# --------------------------------------------------------------------- запуск

def run(actions, scope: str = "all", include_detail: bool = False,
        confirmed: bool = False) -> dict:
    """Ставит массовый прогон в фоновую нить. Возвращает {job_id, needs_confirm}."""
    acts = _normalize_actions(actions)
    if not acts:
        return {"job_id": None, "rejected": "не выбрано ни одного действия"}
    if "apply" in acts and not confirmed:
        return {"job_id": None, "needs_confirm": True,
                "rejected": "коррекция цен требует подтверждения"}
    with _LOCK:
        job_id = _next_id()
        job = {
            "id": job_id, "status": "running", "scope": scope, "actions": acts,
            "include_detail": bool(include_detail),
            "started_at": _now(), "finished_at": None, "ok": 0, "failed": 0,
            "steps": [], "cabs": [],
        }
        _JOBS[job_id] = job
    threading.Thread(target=_worker, args=(job_id, acts, scope, bool(include_detail)),
                     daemon=True).start()
    return {"job_id": job_id}


def _run_step(job: dict, step: dict, fn) -> None:
    step["status"] = "running"
    try:
        note, rows = fn()
        step["status"] = "ok"
        step["note"] = note or ""
        step["rows"] = int(rows or 0)
        job["ok"] += 1
    except Exception as e:  # noqa: BLE001 — сбой одной связки не роняет прогон
        step["status"] = "failed"
        step["error"] = str(e)[:300]
        job["failed"] += 1


def _wait_refresh(job_id: int, timeout_s: int = 1800) -> dict:
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = refresh_service.job_state(job_id)
        if st is None:
            return {"ok": 0, "failed": 0, "note": "задание обновления потеряно"}
        if st.get("status") == "done":
            return {"ok": int(st.get("ok", 0)), "failed": int(st.get("failed", 0)),
                    "note": ""}
        time.sleep(1.0)
    return {"ok": 0, "failed": 1, "note": "обновление не завершилось за 30 мин"}


def _do_refresh(info, include_detail: bool) -> tuple:
    res = refresh_service.start_refresh(
        info.marketplace, include_detail=include_detail, cab_id=info.id)
    if not res.get("job_id"):
        return (res.get("rejected") or "нечего обновлять", 0)
    r = _wait_refresh(res["job_id"])
    note = f"шагов ок: {r['ok']}, с ошибкой: {r['failed']}"
    if r.get("note"):
        note += " · " + r["note"]
    if r["failed"] and not r["ok"]:
        raise RuntimeError(note)
    return (note, r["ok"])


def _do_recalc(db: Session, info, settings: dict) -> tuple:
    rec = _recalc(db, info, settings)
    rows = rec.get("rows") or []
    n = sum(1 for r in rows if _is_candidate(r))
    return (f"строк: {len(rows)}, к изменению: {n}", len(rows))


def _do_apply(db: Session, info, settings: dict) -> tuple:
    prov = _provider_for(info)
    if info.marketplace == "ozon":
        res = pricing_ozon_service.apply_recommendations_ozon(
            db, settings=settings, provider=prov)
    else:
        prices_df, _u, _s = pricing_service.resolve_prices(db)
        main_map = pricing_service.main_influence_map(db, settings)
        res = pricing_service.apply_recommendations(
            db, settings=settings, prices_df=prices_df, provider=prov,
            main_map=main_map)
    pushed = int(res.get("pushed", 0) or 0)
    return (res.get("note") or f"отправлено изменений: {pushed}", pushed)


def _do_minpromo(db: Session, info, settings: dict, job_id: int) -> tuple:
    if info.marketplace != "wb":
        return ("только для WB — пропущено", 0)
    rec = _recalc(db, info, settings)
    rows = rec.get("rows") or []
    s = rec.get("settings") or pricing_service.merge_settings(settings)
    buf, count, total = pricing_service.minpromo_template(rows, s)
    pricing_service.record_minpromo_export(db, rows, s)
    out_dir = _MINPROMO_DIR / f"job{job_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"minpromo_{info.id}.xlsx"
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return (f"строк в шаблоне: {count} из {total}", count)


def minpromo_path(job_id: int, cab_id: int) -> Optional[Path]:
    job = _JOBS.get(job_id)
    if job is None:
        return None
    for c in job["cabs"]:
        if c["id"] == cab_id:
            return c.get("minpromo_path")
    return None


def _reset_search_path(db: Session) -> None:
    """Возвращает search_path сессии к дефолту и фиксирует это в соединении.

    `pin_search_path` вешает after_begin-listener, который заново ставит схему
    связки на старте каждой транзакции. Просто `RESET search_path` без commit'а
    откатывается при закрытии сессии, и соединение уходит в пул с чужой схемой
    (следующий запрос увидит таблицы связки вместо public).
    """
    try:
        db.rollback()
    except Exception:  # noqa: BLE001
        pass
    try:
        db.execute(text("RESET search_path"))
        db.commit()
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _worker(job_id: int, acts: list, scope: str, include_detail: bool) -> None:
    job = _JOBS[job_id]
    db = SessionLocal()
    try:
        user = cabinet_service.seed_user(db)
        links = _resolve_cabinets(db, user, scope)
        for info in links:
            cab = _cab_dict(info)
            cab["status"] = "running"
            cab["steps"] = []
            cab["minpromo_path"] = None
            job["cabs"].append(cab)

            def _mk(action_id: str) -> dict:
                label = _ACTION_BY_ID[action_id]["label"]
                return _add_step(job, cab, action_id, _flat_label(cab, label))

            pinned = False
            try:
                apply_search_path(db, info.schema)
                pin_search_path(db, info.schema)
                pinned = True
            except Exception as e:  # noqa: BLE001
                for aid in acts:
                    st = _mk(aid)
                    st["status"] = "failed"
                    st["error"] = f"не удалось переключить схему: {e}"[:300]
                    job["failed"] += 1
                cab["status"] = "failed"
                db.rollback()
                continue

            db._active_info = info
            try:
                settings = _settings(db, info)
                for aid in acts:
                    step = _mk(aid)
                    if aid == "refresh":
                        _run_step(job, step, lambda: _do_refresh(info, include_detail))
                    elif aid == "recalc":
                        _run_step(job, step, lambda: _do_recalc(db, info, settings))
                    elif aid == "apply":
                        _run_step(job, step, lambda: _do_apply(db, info, settings))
                    elif aid == "minpromo":
                        _run_step(
                            job, step,
                            lambda: _do_minpromo(db, info, settings, job_id))
                        if step["status"] == "ok":
                            cab["minpromo_path"] = str(
                                _MINPROMO_DIR / f"job{job_id}" / f"minpromo_{info.id}.xlsx")
            except Exception as e:  # noqa: BLE001
                cab["status"] = "failed"
                cab.setdefault("error", str(e)[:300])
            else:
                cab["status"] = ("failed"
                                 if any(s["status"] == "failed" for s in cab["steps"])
                                 else "done")
            finally:
                if pinned:
                    _reset_search_path(db)
    finally:
        _reset_search_path(db)
        db.close()
        job["status"] = "done"
        job["finished_at"] = _now()
