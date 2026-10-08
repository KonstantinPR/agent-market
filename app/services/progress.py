# -*- coding: utf-8 -*-
"""Прогресс длительных операций для зелёной строки состояния (#headerMsg).

Синхронная выкачка (POST /api/wb/cards и т.п.) отдаёт ответ одним куском: пока
запрос висит, браузер не знает, что происходит на сервере. Механизм:
- фронт генерит op-id, шлёт его заголовком ``X-Progress-Id`` и раз в секунду
  опрашивает ``GET /api/progress?op=…``;
- middleware в app/main.py кладёт op-id в ContextVar (anyio копирует контекст
  и в sync-эндпоинты в threadpool, поэтому провайдеры его видят);
- провайдеры в циклах пагинации зовут :func:`report` — реестр собирает
  label/stage/done/total и отдаёт готовую строку вида
  «Карточки WB: стр. 12 · 4 820/16 000 тов. (30%)».

Границы:
- реестр процессный — работает, пока uvicorn поднимает один воркер
  (app/main.py: uvicorn.run без --workers); при нескольких воркерах опрос
  вернёт пусто и фронт показывает прошедшее время (деградация, не поломка);
- у каждой вкладки свой op-id — параллельные операции не смешиваются;
- запись живёт TTL: упавшая операция перестаёт отдаваться сама;
- в фоновых refresh-заданиях ContextVar пуст — report() молча no-op,
  там прогресс идёт по шагам job-состояния (см. refreshHeaderText в app.js).
"""
import threading
import time
from contextvars import ContextVar

#: Имя заголовка, которым фронтенд передаёт op-id операции.
HEADER = "X-Progress-Id"

#: Сколько секунд живёт запись без обновлений (между страницами выкачки бывают
#: паузы под rate-limit — берём с запасом).
TTL = 15.0

_OP: ContextVar[str] = ContextVar("agent_market_progress_op", default="")
_LOCK = threading.Lock()
_STATE: dict = {}  # op_id -> {label, stage, done, total, unit, updated}


def set_op(op_id: str) -> None:
    """Кладёт op-id текущего запроса в ContextVar (вызывается из middleware)."""
    _OP.set(op_id or "")


def get_op() -> str:
    return _OP.get()


def report(label=None, stage=None, done=None, total=None, unit=None) -> None:
    """Обновляет прогресс текущей операции. Без op-id в контексте — no-op.

    Поля накапливаются: следующий вызов может менять только stage,
    label/done/total предыдущие сохраняются.
    """
    op = _OP.get()
    if not op:
        return
    now = time.time()
    with _LOCK:
        rec = _STATE.get(op)
        if rec is None or now - rec["updated"] > TTL:
            rec = {"label": "", "stage": "", "done": None,
                   "total": None, "unit": "строк", "updated": now}
            _STATE[op] = rec
        if label:
            rec["label"] = str(label)
        if stage is not None:
            rec["stage"] = str(stage)
        if done is not None:
            rec["done"] = int(done)
        if total is not None:
            rec["total"] = int(total)
        if unit:
            rec["unit"] = str(unit)
        rec["updated"] = now
        _prune(now)


def snapshot(op_id: str):
    """{"text": "…"} для GET /api/progress, либо None (нет/устарело)."""
    if not op_id:
        return None
    now = time.time()
    with _LOCK:
        _prune(now)
        rec = _STATE.get(op_id)
        if rec is None:
            return None
        return {"text": _fmt(rec)}


def _prune(now: float) -> None:
    dead = [k for k, r in _STATE.items() if now - r["updated"] > TTL]
    for k in dead:
        del _STATE[k]


def _num(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def _fmt(rec: dict) -> str:
    label, stage = rec["label"], rec["stage"]
    done, total = rec["done"], rec["total"]
    head = ""
    if label:
        head = label + (":" if stage or done is not None else "")
    tail = []
    if stage:
        tail.append(stage)
    if done is not None:
        if total:
            pct = round(100 * done / total)
            tail.append(f"{_num(done)}/{_num(total)} {rec['unit']} ({pct}%)")
        else:
            tail.append(f"{_num(done)} {rec['unit']}")
    if not tail:
        return label
    return (head + " " if head else "") + " · ".join(tail)
