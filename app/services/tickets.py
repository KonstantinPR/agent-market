"""Очередь тикетов: файл TICKETS.md — единый источник правды.

Тот же файл читают и правят агенты/человек вручную, поэтому мутации
затрагивают только строку счётчика и тела четырёх секций тикетов
(Открытые / В работе / Заблокированные / Закрытые). Всё остальное
(шапка, правила, служебные секции) остаётся байт-в-байт.
"""

import re
import subprocess
import threading
from pathlib import Path
from typing import Optional

from app.config import BASE_DIR

TICKETS_FILE = BASE_DIR / "TICKETS.md"

_LOCK = threading.Lock()

SECTIONS = [
    ("Открытые", "open"),
    ("В работе", "in_progress"),
    ("Заблокированные", "blocked"),
    ("Закрытые", "closed"),
]
PRIORITIES = {"low", "medium", "high"}
_PLACEHOLDER = "_(пусто)_"

_COUNTER_TICK = re.compile(r"`T-(\d+)`")
_COMMIT_SUFFIX = re.compile(r"\s*—\s*ссылка на коммит `([^`]*)`$")
_TICKET_HEAD = re.compile(r"^- \[([^\]]+)\] \(([a-z]+)\)(.*)$")

_LABEL = {key: name for name, key in SECTIONS}

# Метка из имени ветки: t-<N>-<слаг> -> T-<N>. Ветка уникальна по природе git,
# поэтому номер, взятый из неё, не может достаться двум агентам одновременно —
# в отличие от общего счётчика в TICKETS.md, за который все правят одну строку.
_BRANCH_RE = re.compile(r"^t-(\d+)(?:-[a-z0-9][a-z0-9._-]*)?$", re.IGNORECASE)


def branch_ticket_id(branch: Optional[str] = None) -> Optional[str]:
    """Извлечь метку тикета из имени ветки. None, если ветка не о тикете.

    `t-42` и `t-42-cenyi-wb` -> `T-42`; `main`, `t-abc` -> None.
    """
    if branch is None:
        branch = current_branch()
    if not branch:
        return None
    b = branch.strip()
    if "/" in b:  # допускаем префиксы вида `agent/t-42-...`
        b = b.rsplit("/", 1)[-1]
    m = _BRANCH_RE.match(b)
    return f"T-{m.group(1)}" if m else None


def current_branch(cwd=None) -> Optional[str]:
    """Текущая ветка git или None (не git-репозиторий / git недоступен)."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=str(cwd) if cwd else None,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    b = r.stdout.strip()
    return b if b and b != "HEAD" else None


def validate(path=None) -> list:
    """Проверить целостность TICKETS.md. Возвращает список проблем (пусто = всё в порядке).

    Ловит то, что при параллельной работе случается тихо и незаметно:
    - одну метку в двух разделах сразу (классический конфликт двух агентов);
    - счётчик, который не совпадает с реально занятыми метками;
    - метку в несуществующем/перепутанном разделе.
    """
    p = _path(path)
    if not p.exists():
        return [f"файл не найден: {p}"]
    info = _load(p)
    problems = []

    seen = {}
    for seg in info["segs"]:
        _hdr, key, blocks = seg
        for b in blocks:
            seen.setdefault(b["id"], []).append(key)

    for tid, keys in sorted(seen.items(), key=lambda kv: _sort_key(kv[0])):
        if len(keys) > 1:
            where = ", ".join(f"«{_LABEL[k]}»" for k in keys)
            problems.append(
                f"метка {tid} встречается {len(keys)} раза ({where}) — "
                f"тикет должен быть ровно в одном разделе"
            )

    ids = [int(m.group(1)) for m in (_TICKET_NUM.match(t) for t in seen) if m]
    if info["counter"] is None:
        problems.append("не найдена строка счётчика")
    elif ids:
        want = max(ids) + 1
        if info["counter"] < want:
            problems.append(
                f"счётчик T-{info['counter']} меньше занятой метки T-{want} — "
                f"номер будет выдан повторно"
            )

    for key, blocks in ((k, _section_blocks(info, k)) for _n, k in SECTIONS):
        for b in blocks:
            if key == "closed" and b["marker"] not in ("closed", "declined"):
                problems.append(f"{b['id']}: в «Закрытые» без пометки (closed/declined)")
            if key != "closed" and b["marker"] in ("closed", "declined"):
                problems.append(f"{b['id']}: помечен {b['marker']}, но лежит в «{_LABEL[key]}»")
    return problems


def _sort_key(tid: str):
    m = _TICKET_NUM.match(tid)
    return (0, int(m.group(1))) if m else (1, tid)


_TICKET_NUM = re.compile(r"^T-(\d+)$")

BOOTSTRAP = """# Тикеты — agent_market

Единая очередь задач проекта. Каждый тикет — это задача, которую нужно
«закрыть» (реализовать и закоммитить). Тикет не перенумеровывается:
номер выдаётся один раз и не переиспользуется, даже если тикет закрыли,
не начав, или закрыли как неактуальный.

Счётчик: следующая свободная метка — `T-1`.

## Правила

- Метка тикета `T-<N>` — монотонный набор. Новый номер = из шапки счётчика,
  после выдачи счётчик увеличивается на 1.
- **Параллельная работа (несколько агентов одновременно).** Номер выдаётся
  НЕ из счётчика, а из имени ветки: сначала создай ветку `t-<N>-<слаг>`
  (`git switch -c t-32-cenyi-wb`), затем заведи тикет — метка `T-32` возьмётся
  из ветки. Имя ветки уникально по природе git, поэтому два агента не получат
  один номер. Ветку `main` для новой работы не используй.
- Счётчик — это «страховка от повторов», а не источник номера. Он всё равно
  подтягивается вперёд, поэтому поднять его руками не нужно: если он отстал,
  `validate()` это покажет.
- Целостность файла проверяет `validate()`: одна метка в двух разделах,
  счётчик меньше занятой метки, метка в неправильном разделе.
- Приоритет: `low | medium | high`. Чем выше приоритет, тем раньше берётся.
- Состояние тикета определяется разделом, в котором он лежит:
  - «Открытые» — в очереди, можно брать;
  - «В работе» — активные тикеты (может быть несколько);
  - «Заблокированные» — ждёт внешних данных/доступа/решения от владельца
    (в теле — короткое описание, что именно нужно);
  - «Закрытые» — выполненные и отклонённые (отклонённый — с пометкой
    `(declined)` в заголовке записи).
  Названия разделов здесь записаны без символа заголовка, чтобы каждая
  строка-заголовок раздела встречалась в файле ровно один раз — иначе
  правка по подстроке (`replace` по имени раздела) бьёт по этому же
  списку, а не по самому разделу.
- Формат записи тикета:
  ```markdown
  - [T-3] (high) Заголовок
    > Тело: что сделать, контекст, ссылки на файлы/строки.
  ```
- Закрытые тикеты хранятся компактно, без тела:
  ```markdown
  - [T-3] (closed) Заголовок — ссылка на коммит `abc1234`
  ```

## Как ведётся работа

- Разработка идёт только через тикеты: сначала читается этот файл,
  берётся открытый тикет с наивысшим приоритетом, переводится
  в «В работе», затем код + тесты + коммит `T-<N>: описание`.
- Коммит по закрытию тикета ссылается на его номер: `T-3: ...`.
- Если для тикета нужен токен, доступ или решение — тикет уходит
  в «Заблокированные» с описанием ожидания.

## Открытые

_(пусто)_

## В работе

_(пусто)_

## Заблокированные

_(пусто)_

## Закрытые

_(пусто)_
"""


class TicketError(Exception):
    """Ошибка работы с тикетами. status — код для HTTP-ответа."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _path(path=None) -> Path:
    return Path(path) if path is not None else Path(TICKETS_FILE)


def _parse_block(raw: list) -> dict:
    """Разбирает блок тикета (заголовок + тело) из строк файла."""
    m = _TICKET_HEAD.match(raw[0])
    if not m:
        return None
    tid, marker, rest = m.group(1), m.group(2), m.group(3).strip()
    if marker in ("closed", "declined"):
        cm = _COMMIT_SUFFIX.search(rest)
        commit = cm.group(1) if cm else None
        title = _COMMIT_SUFFIX.sub("", rest).strip() if cm else rest
        return {"id": tid, "marker": marker, "priority": None,
                "title": title, "body": [], "commit": commit, "raw": raw}
    body = []
    for ln in raw[1:]:
        s = ln.strip()
        if s.startswith(">"):
            s = s[1:].strip()
        if s:
            body.append(s)
    return {"id": tid, "marker": marker,
            "priority": marker if marker in PRIORITIES else "medium",
            "title": rest, "body": body, "commit": None, "raw": raw}


def _parse_lines(lines: list) -> dict:
    """Структурированный вид файла: счётчик + блоки по секциям."""
    counter = None
    counter_idx = None
    for i, ln in enumerate(lines):
        m = _COUNTER_TICK.search(ln)
        if m:
            counter = int(m.group(1))
            counter_idx = i
            break

    boundaries = {}
    names = {n for n, _ in SECTIONS}
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            name = ln[3:].strip()
            if name in names and name not in boundaries:
                boundaries[name] = i

    order = [(boundaries[n], key) for n, key in SECTIONS if n in boundaries]
    order.sort()
    segs = []
    for i, (hdr, key) in enumerate(order):
        end = order[i + 1][0] if i + 1 < len(order) else len(lines)
        blocks = []
        j = hdr + 1
        while j < end:
            if _TICKET_HEAD.match(lines[j]):
                k = j + 1
                while k < end and lines[k] and lines[k][0] in " \t":
                    k += 1
                b = _parse_block(lines[j:k])
                if b:
                    blocks.append(b)
                j = k
            else:
                j += 1
        segs.append([hdr, key, blocks])
    return {"counter": counter, "counter_idx": counter_idx, "segs": segs}


def _render_body(blocks: list) -> list:
    out = [""]
    if not blocks:
        out.append(_PLACEHOLDER)
    else:
        for i, b in enumerate(blocks):
            if i:
                out.append("")
            out.extend(b["raw"])
    out.append("")
    return out


def _render(lines: list, segs: list) -> list:
    """Собирает файл из исходных строк + новых блоков секций."""
    out = []
    cursor = 0
    for i, (hdr, _key, _blocks) in enumerate(segs):
        out.extend(lines[cursor:hdr])
        out.append(lines[hdr])
        out.extend(_render_body(segs[i][2]))
        cursor = segs[i + 1][0] if i + 1 < len(segs) else len(lines)
    out.extend(lines[cursor:])
    return out


def _write(path: Path, lines: list, segs: list, set_counter=None):
    out = _render(lines, segs)
    if set_counter is not None:
        for i, ln in enumerate(out):
            if _COUNTER_TICK.search(ln):
                out[i] = _COUNTER_TICK.sub(lambda m, v=set_counter: f"`T-{v}`", ln, count=1)
                break
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _load(p: Path) -> dict:
    text = p.read_text(encoding="utf-8")
    lines = text.split("\n")
    info = _parse_lines(lines)
    info["lines"] = lines
    return info


def _section_blocks(info: dict, key: str) -> list:
    for seg in info["segs"]:
        if seg[1] == key:
            return seg[2]
    return []


def _set_section_blocks(info: dict, key: str, blocks: list):
    for seg in info["segs"]:
        if seg[1] == key:
            seg[2] = blocks
            return


def _find(info: dict, tid: str):
    for seg in info["segs"]:
        for b in seg[2]:
            if b["id"] == tid:
                return seg[1], b, seg[2]
    return None, None, None


def _ticket_dict(b: dict, key: str) -> dict:
    if key == "closed":
        state = "declined" if b["marker"] == "declined" else "closed"
    else:
        state = key
    return {
        "id": b["id"],
        "state": state,
        "priority": b["priority"],
        "title": b["title"],
        "body": "\n".join(b["body"]).strip(),
        "commit": b["commit"],
    }


# ---------------------------------------------------------------- публичный API

def load(path=None) -> dict:
    """Все тикеты по секциям + текущий счётчик."""
    p = _path(path)
    if not p.exists():
        return {"counter": None, "missing": True,
                "sections": {key: [] for _name, key in SECTIONS}}
    with _LOCK:
        info = _load(p)
    out = {}
    for _name, key in SECTIONS:
        out[key] = [_ticket_dict(b, key) for b in _section_blocks(info, key)]
    return {"counter": info["counter"], "missing": False, "sections": out}


def create(title: str, body: str = "", priority: str = "medium", path=None,
           branch: Optional[str] = None) -> dict:
    """Создаёт тикет в «Открытые», назначает номер по счётчику.

    Если текущая ветка — `t-<N>-<слаг>`, метка берётся из неё, а не из
    счётчика: номер уже занят этой веткой, её не сможет создать второй агент.
    Счётчик при этом всё равно подтягивается вперёд, чтобы не выдать его снова.
    """
    p = _path(path)
    title = (title or "").strip()
    if not title:
        raise TicketError("Заголовок тикета обязателен")
    if priority not in PRIORITIES:
        raise TicketError("Приоритет должен быть low | medium | high")
    with _LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(BOOTSTRAP, encoding="utf-8")
        info = _load(p)
        if info["counter"] is None:
            raise TicketError("Не найдена строка счётчика в TICKETS.md")
        n = info["counter"]
        tid = branch_ticket_id(branch) or f"T-{n}"
        if _find(info, tid)[1] is not None:
            raise TicketError(
                f"Метка {tid} уже занята (из ветки {branch or current_branch()!r}). "
                f"Переименуйте ветку или заведите тикет без неё.", 409)
        if _TICKET_NUM.match(tid) and int(_TICKET_NUM.match(tid).group(1)) >= n:
            n = int(_TICKET_NUM.match(tid).group(1))
        raw = [f"- [{tid}] ({priority}) {title}"]
        for line in str(body).split("\n"):
            s = line.strip()
            if s:
                raw.append("    > " + s)
        block = {"id": tid, "marker": priority, "priority": priority,
                 "title": title,
                 "body": [s for s in str(body).split("\n") if s.strip()],
                 "commit": None, "raw": raw}
        blocks = _section_blocks(info, "open")
        blocks.append(block)
        _set_section_blocks(info, "open", blocks)
        _write(p, info["lines"], info["segs"], set_counter=n + 1)
        return {"id": tid, "state": "open", "priority": priority,
                "title": title, "body": (body or "").strip(), "commit": None}


def _move(tid: str, allowed: tuple, to_key: str, path, build=None, err_ctx="") -> dict:
    p = _path(path)
    if not p.exists():
        raise TicketError(f"Тикет {tid} не найден", 404)
    with _LOCK:
        info = _load(p)
        key, block, blocks = _find(info, tid)
        if block is None:
            raise TicketError(f"Тикет {tid} не найден", 404)
        if key not in allowed:
            raise TicketError(f"Тикет {tid} нельзя {err_ctx}: он сейчас в «{_LABEL[key]}»", 409)
        blocks.remove(block)
        if build is not None:
            block = build(block)
        _set_section_blocks(info, to_key, _section_blocks(info, to_key) + [block])
        _write(p, info["lines"], info["segs"])
    return _ticket_dict(block, to_key)


def _build_closed(marker: str):
    def build(b: dict) -> dict:
        commit = b.get("commit")
        tail = f" — ссылка на коммит `{commit}`" if commit else ""
        raw = [f"- [{b['id']}] ({marker}) {b['title']}{tail}"]
        return {"id": b["id"], "marker": marker, "priority": None,
                "title": b["title"], "body": [], "commit": commit, "raw": raw}
    return build


def _build_reopen(b: dict) -> dict:
    raw = [f"- [{b['id']}] (medium) {b['title']}"]
    return {"id": b["id"], "marker": "medium", "priority": "medium",
            "title": b["title"], "body": [], "commit": None, "raw": raw}


def start(tid: str, path=None) -> dict:
    return _move(tid, ("open",), "in_progress", path, err_ctx="взять в работу")


def block(tid: str, path=None) -> dict:
    return _move(tid, ("open", "in_progress"), "blocked", path, err_ctx="заблокировать")


def unblock(tid: str, path=None) -> dict:
    return _move(tid, ("blocked",), "open", path, err_ctx="вернуть в очередь")


def close(tid: str, commit: str = "", path=None) -> dict:
    def build(b: dict) -> dict:
        if commit:
            b["commit"] = commit
        return _build_closed("closed")(b)
    return _move(tid, ("open", "in_progress"), "closed", path,
                 build=build, err_ctx="закрыть")


def decline(tid: str, path=None) -> dict:
    return _move(tid, ("open", "in_progress"), "closed", path,
                 build=_build_closed("declined"), err_ctx="отклонить")


def reopen(tid: str, path=None) -> dict:
    return _move(tid, ("closed",), "open", path,
                 build=_build_reopen, err_ctx="открыть заново")


def _main(argv=None) -> int:
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "validate"
    path = Path(argv[1]) if len(argv) > 1 else None
    if cmd == "validate":
        problems = validate(path)
        if not problems:
            print(f"OK: {_path(path)} — метки уникальны, счётчик согласован")
            return 0
        print(f"Проблем в {_path(path)}: {len(problems)}")
        for x in problems:
            print("  -", x)
        return 1
    if cmd == "branch":
        b = argv[1] if len(argv) > 1 else current_branch()
        tid = branch_ticket_id(b)
        print(tid or f"ветка {b!r} не о тикете (нужна t-<N>-<слаг>)")
        return 0 if tid else 1
    print(__doc__ or "usage: validate [path] | branch [имя-ветки]")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())