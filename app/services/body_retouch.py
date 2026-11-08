# -*- coding: utf-8 -*-
"""«Ретушь тела»: кожа рук, кистей, плеч и шеи + подтяжка тона лица.

Пайплайн — отдельный проект ``face-retouch`` (там же venv и модели), поэтому
web-слой не переписывает обработку, а запускает ``body_retouch.py``
подпроцессом. Сегментатор — MediaPipe ``selfie_multiclass_256x256``: он сам
отделяет кожу тела от одежды, фона и волос, поэтому товар (джинсы, юбка,
топ) остаётся байт-в-байт прежним.

Варианты отличаются только силой стадий (пресеты ``soft`` / ``medium`` /
``gloss`` в ``body_retouch.py``). Тон-мэтч (лицо → кожа тела) включается
галочкой в UI и передаётся как ``--no-match`` наоборот.

Две моды задания, как у «Обработки лиц»:

- **folder** — папка с исходниками, результат в подпапку ``Ретушь тела``;
- **upload** — файлы из drop-зоны, результат собирается в ZIP.

Обе идут в фоновой нити: UI получает ``job_id``, опрашивает состояние и
скачивает архив / открывает папку.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import BASE_DIR, settings

#: Расширения исходников (``body_retouch.EXT``).
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

#: Имя подпапки с результатом в режиме «папка с исходниками». Отличается от
#: ``Готовые`` у лиц: два прогона по одной папке не должны перемешиваться.
OUT_NAME = "Ретушь тела"

#: Имя скрипта и файла модели относительно каталога пайплайна.
SCRIPT_NAME = "body_retouch.py"
MODEL_NAME = "selfie_multiclass_256x256.tflite"

#: Реестр вариантов: UI и API читают его через :func:`options`.
VARIANTS = {
    "s1": {
        "id": "s1",
        "label": "Мягкая",
        "hint": "Заметно только в сравнении: мелкие дефекты и лёгкое выравнивание",
        "preset": "soft",
    },
    "s2": {
        "id": "s2",
        "label": "Средняя",
        "hint": "Рабочий вариант для карточки: вены и краснота уходят, рельеф остаётся",
        "preset": "medium",
    },
    "s3": {
        "id": "s3",
        "label": "Глянцевая",
        "hint": "Каталожный вид: кожа глаже и светлее, блики приглушены",
        "preset": "gloss",
    },
}
DEFAULT_VARIANT = "s2"

_JOB_DIR = BASE_DIR / "data" / "body"
_LAST_SOURCE_FILE = _JOB_DIR / "last_source.txt"

_PROGRESS_RE = re.compile(
    r"^\[(\d+)/(\d+)\]\s+(.*?)\s{2,}skin=([\d.]+)%\s+match=(\d+)\s+"
    r"noskin=(\d+)\s+([\d.]+)s\s*$")
_FAILED_RE = re.compile(r"^\[(\d+)/(\d+)\]\s+(.*?)\s{2,}FAILED:\s*(.+?)\s*$")


# --------------------------------------------------------------------- пути


def work_dir() -> Path:
    """Папка пайплайна ``face-retouch``: venv, модели, скрипты."""
    return Path(settings.face_retouch_dir)


def python_exe() -> Path:
    return work_dir() / ".venv" / "Scripts" / "python.exe"


def script() -> Path:
    return work_dir() / SCRIPT_NAME


def model_path() -> Path:
    return work_dir() / "models" / MODEL_NAME


def _now() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


# ---------------------------------------------------------------- варианты


def variants() -> list[dict]:
    return [
        {"id": v["id"], "label": v["label"], "hint": v["hint"]}
        for v in VARIANTS.values()
    ]


def get_variant(variant: str) -> dict:
    v = VARIANTS.get((variant or "").strip())
    if v is None:
        raise ValueError(f"Неизвестный вариант обработки: {variant}")
    return v


def ready() -> bool:
    return (python_exe().is_file() and script().is_file()
            and model_path().is_file())


# ------------------------------------------------------------------ прогресс


def parse_progress(line: str) -> Optional[dict]:
    """Строка ``body_retouch.py`` → событие прогресса (или None).

    Формат: ``[1/4] name.JPG  skin=3.2% match=1 noskin=0  4.2s``; ошибка —
    ``[1/4] name.JPG  FAILED: ...``.
    """
    m = _PROGRESS_RE.match(line or "")
    if m:
        return {
            "kind": "done",
            "index": int(m.group(1)),
            "total": int(m.group(2)),
            "name": m.group(3),
            "skin_pct": float(m.group(4)),
            "matched": int(m.group(5)),
            "no_skin": bool(int(m.group(6))),
            "seconds": float(m.group(7)),
        }
    m = _FAILED_RE.match(line or "")
    if m:
        return {
            "kind": "failed",
            "index": int(m.group(1)),
            "total": int(m.group(2)),
            "name": m.group(3),
            "error": m.group(4),
        }
    return None


# -------------------------------------------------------------- последняя папка


def last_source() -> str:
    try:
        return _LAST_SOURCE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _remember_source(path: Path) -> None:
    try:
        _LAST_SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LAST_SOURCE_FILE.write_text(str(path), encoding="utf-8")
    except OSError:
        pass


def _count_images(folder: Path) -> int:
    return sum(1 for p in folder.iterdir()
               if p.is_file() and p.suffix.lower() in IMAGE_EXT)


def options() -> dict:
    return {
        "variants": variants(),
        "default_variant": DEFAULT_VARIANT,
        "default_source": last_source() or settings.face_source_default,
        "out_name": OUT_NAME,
        "ready": ready(),
        "pipeline_dir": str(work_dir()),
        "script": str(script()),
        "model": str(model_path()),
        "extensions": sorted(IMAGE_EXT),
    }


# --------------------------------------------------------------------- задания

_LOCK = threading.Lock()
_SEQ = 0
_JOBS: dict = {}


def _next_id() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def new_job_id() -> int:
    with _LOCK:
        return _next_id()


def build_command(source: list, out_dir: Path, variant: str,
                  match: bool) -> list[str]:
    """Команда запуска ``body_retouch.py`` для варианта."""
    v = get_variant(variant)
    cmd = [
        str(python_exe()), "-X", "utf8", str(script()),
        *[str(s) for s in source],
        "-o", str(out_dir),
        "--preset", v["preset"],
        "--preview",
    ]
    if not match:
        cmd.append("--no-match")
    return cmd


def start_job(source_dir: str = "", variant: str = DEFAULT_VARIANT,
              match: bool = True, uploads: Optional[list] = None,
              job_id: Optional[int] = None) -> dict:
    """Ставит обработку в фоновую нить. Возвращает ``{job_id}``.

    ``uploads`` — список ``(имя файла, bytes)`` для режима drop-зоны; если он
    пуст, обрабатывается папка ``source_dir``.
    """
    uploads = list(uploads or [])
    variant = (variant or DEFAULT_VARIANT).strip() or DEFAULT_VARIANT
    v = get_variant(variant)

    if not ready():
        raise ValueError(
            "Пайплайн ретуши тела не найден: " + str(work_dir())
            + " (нужны .venv\\Scripts\\python.exe, " + SCRIPT_NAME
            + " и models\\" + MODEL_NAME + ")")

    with _LOCK:
        if any(j["status"] == "running" for j in _JOBS.values()):
            raise ValueError("Уже идёт обработка — дождитесь окончания или остановите её")

    if job_id is None:
        job_id = new_job_id()
    job_dir = _JOB_DIR / f"job{job_id}"
    files: list[Path] = []

    if uploads:
        in_dir = job_dir / "in"
        out_dir = job_dir / "out"
        in_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        used: set[str] = set()
        for raw_name, data in uploads:
            name = Path(str(raw_name or "image.jpg")).name or "image.jpg"
            if not data:
                continue
            stem, suffix = Path(name).stem, Path(name).suffix or ".jpg"
            candidate, n = name, 1
            while candidate.lower() in used:
                n += 1
                candidate = f"{stem}_{n}{suffix}"
            used.add(candidate.lower())
            dst = in_dir / candidate
            dst.write_bytes(data)
            files.append(dst)
        if not files:
            raise ValueError("Не переданы изображения")
        mode = "upload"
        source = ""
        total = len(files)
        filename = f"body_{variant}.zip"
    else:
        folder = Path(str(source_dir or "").strip().strip('"')).expanduser()
        if not str(source_dir or "").strip():
            raise ValueError("Укажите папку с исходниками или перетащите фото")
        if not folder.is_dir():
            raise ValueError(f"Папка не найдена: {folder}")
        try:
            total = _count_images(folder)
        except OSError as exc:
            raise ValueError(f"Не удалось прочитать папку: {exc}")
        if total == 0:
            raise ValueError("В папке нет изображений (JPG, PNG, WEBP)")
        out_dir = folder / OUT_NAME
        mode = "folder"
        source = str(folder)
        filename = f"body_{variant}.zip"
        _remember_source(folder)

    job = {
        "id": job_id,
        "status": "running",
        "mode": mode,
        "variant": variant,
        "variant_label": v["label"],
        "match": bool(match),
        "source": source,
        "out_dir": str(out_dir),
        "started_at": _now(),
        "finished_at": None,
        "total": total,
        "done": 0,
        "no_skin": 0,
        "matched": 0,
        "failed_files": 0,
        "current": "",
        "filename": filename,
        "zip_path": None,
        "error": "",
        "cancel": False,
        "proc": None,
        "tail": [],
    }
    with _LOCK:
        _JOBS[job_id] = job

    threading.Thread(
        target=_run_worker,
        args=(job_id, [str(p) for p in files] if files else [source], out_dir,
              variant, bool(match)),
        daemon=True,
    ).start()
    return {"job_id": job_id}


def _run_worker(job_id: int, sources: list, out_dir: Path,
                variant: str, match: bool) -> None:
    job = _JOBS[job_id]
    try:
        cmd = build_command(sources, out_dir, variant, match)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.Popen(
            cmd,
            cwd=str(work_dir()),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        job["proc"] = proc
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            job["tail"] = (job["tail"] + [line])[-20:]
            ev = parse_progress(line)
            if ev:
                job["done"] = ev["index"]
                job["total"] = ev["total"]
                job["current"] = ev["name"]
                if ev["kind"] == "failed":
                    job["failed_files"] += 1
                elif ev.get("no_skin"):
                    job["no_skin"] += 1
                elif ev.get("matched"):
                    job["matched"] += 1
            if job["cancel"]:
                proc.terminate()
                break
        code = proc.wait()
        job["proc"] = None

        if job["cancel"]:
            job["status"] = "cancelled"
            return
        if code != 0 and job["done"] == 0:
            err = "\n".join(job["tail"][-5:]) or f"код возврата {code}"
            raise RuntimeError(err)
        if not out_dir.is_dir():
            raise RuntimeError("Пайплайн не создал папку с результатом")
        if job["mode"] == "upload":
            zip_path = _JOB_DIR / f"job{job_id}" / job["filename"]
            _zip_folder(out_dir, zip_path)
            job["zip_path"] = str(zip_path)
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001 — падение воркера не должно висеть в UI
        job["status"] = "failed"
        job["error"] = str(exc)[:500]
    finally:
        job["finished_at"] = _now()


def _zip_folder(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file() and "preview" not in p.relative_to(src).parts:
                z.write(p, p.relative_to(src).as_posix())


def job_state(job_id: int) -> Optional[dict]:
    job = _JOBS.get(job_id)
    if job is None:
        return None
    percent = 0
    if job["total"]:
        percent = int(round(100 * job["done"] / job["total"]))
    return {
        "id": job["id"],
        "status": job["status"],
        "mode": job["mode"],
        "variant": job["variant"],
        "variant_label": job["variant_label"],
        "match": job["match"],
        "source": job["source"],
        "out_dir": job["out_dir"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "total": job["total"],
        "done": job["done"],
        "no_skin": job["no_skin"],
        "matched": job["matched"],
        "failed_files": job["failed_files"],
        "current": job["current"],
        "percent": percent,
        "filename": job["filename"],
        "ready": bool(job["zip_path"]),
        "error": job["error"],
        "log": job["tail"][-5:],
    }


def zip_path(job_id: int) -> Optional[Path]:
    job = _JOBS.get(job_id)
    if job and job.get("zip_path"):
        return Path(job["zip_path"])
    return None


def stop_job(job_id: int) -> bool:
    """Просит воркер остановиться (подпроцесс убивается в его же нити)."""
    job = _JOBS.get(job_id)
    if job is None or job["status"] != "running":
        return False
    job["cancel"] = True
    proc = job.get("proc")
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass
    return True


def open_folder(job_id: int) -> bool:
    """Открывает папку с результатом в проводнике (локальное приложение)."""
    job = _JOBS.get(job_id)
    if job is None:
        return False
    path = Path(job["out_dir"])
    if not path.is_dir():
        path = Path(job["out_dir"]).parent
    if not path.is_dir():
        return False
    try:
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606 — только Windows, локальный UI
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        return False
    return True


def clear_job_dir(job_id: int) -> None:
    """Удаляет временную папку задания (для тестов/уборки)."""
    shutil.rmtree(_JOB_DIR / f"job{job_id}", ignore_errors=True)
