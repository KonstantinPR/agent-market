# -*- coding: utf-8 -*-
"""«Обработка лиц»: замена лица на эталонное (варианты 1–3).

Пайплайн живёт в отдельном проекте ``face-retouch`` (onnxruntime, mediapipe,
GFPGAN), поэтому здесь он не переписывается: web-слой запускает
``brand_batch.py`` подпроцессом тем же venv-питоном, где лежат модели.

Все варианты — inswapper_128 (pixel boost 512) + GFPGAN сразу после свапа и
**без** стадии gloss (``--no-retouch``); различаются только силой GFPGAN:
v1 — 0.85 (принятый результат, заморожен), v2 — 0.55, v3 — 1.0.
Разрешение, EXIF и всё вне овала лица (фон, одежда, волосы, уши, шея) не
меняются.

Эталоны — встроенные ``models/*.npz`` и свои, загруженные из UI:
``etalon_prep.py`` готовит из фото ``models/custom/<имя>.npz``, он же
появляется в селекторе эталонов как ``custom:<имя>``.

Две моды задания:

- **folder** — папка с исходниками, результат кладётся рядом в подпапку
  ``Готовые`` (по умолчанию обрабатываются прямо исходники);
- **upload** — файлы из drop-зоны, результат собирается в ZIP на скачивание.

Обе идут в фоновой нити по образцу ``img_foldering``: UI получает ``job_id``,
опрашивает состояние и после завершения скачивает архив / открывает папку.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import BASE_DIR, settings

#: Расширения, которые принимает ``brand_batch.py`` (``retouch.EXT``).
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

#: Имя подпапки с результатом в режиме «папка с исходниками».
OUT_NAME = "Готовые"

#: Реестр вариантов обработки. Новые варианты добавляются сюда — UI и API
#: читают список через :func:`options`, переписывать фронтенд не нужно.
#: v1 менять нельзя: это принятый результат, он должен оставаться бит-в-бит
#: прежним (см. тест ``test_v1_args_are_frozen``).
VARIANTS = {
    "v1": {
        "id": "v1",
        "label": "Вариант 1 — эталон 180a",
        "hint": "Свап эталонного лица (inswapper 512) + GFPGAN 0.85, без ретуши кожи",
        "args": ["--no-retouch", "--swap-enhance", "0.85", "--boost", "512"],
    },
    "v2": {
        "id": "v2",
        "label": "Вариант 2 — мягкий",
        "hint": "Тот же свап, GFPGAN 0.55: меньше «пластика», кожа и поры натуральнее",
        "args": ["--no-retouch", "--swap-enhance", "0.55", "--boost", "512"],
    },
    "v3": {
        "id": "v3",
        "label": "Вариант 3 — детальный",
        "hint": "Тот же свап, GFPGAN 1.0: максимум детализации, черты резче",
        "args": ["--no-retouch", "--swap-enhance", "1.0", "--boost", "512"],
    },
}
DEFAULT_VARIANT = "v1"

#: Эталоны по умолчанию, если пользователь не выбрал (первый найденный).
PREFERRED_REFERENCES = ("etalon_180a.npz", "etalon_brand.npz")

#: Свои эталоны (загруженные из UI) лежат отдельно от встроенных и имеют
#: id вида ``custom:<имя>``; встроенные — просто имя файла.
CUSTOM_PREFIX = "custom:"
CUSTOM_DIR_NAME = "custom"
ETALON_EXT = ".npz"
#: Фото, из которого готовится эталон (``etalon_prep.py`` умеет те же форматы).
ETALON_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".jfif"}
ETALON_MAX_BYTES = 20 * 1024 * 1024
ETALON_TIMEOUT = 180

_JOB_DIR = BASE_DIR / "data" / "faces"
_LAST_SOURCE_FILE = _JOB_DIR / "last_source.txt"

_PROGRESS_RE = re.compile(
    r"^\[(\d+)/(\d+)\]\s+(.*?)\s{2,}swap=(\d+)\s+faces=(\d+)\s+([\d.]+)s\s*$")
_FAILED_RE = re.compile(r"^\[(\d+)/(\d+)\]\s+(.*?)\s{2,}FAILED:\s*(.+?)\s*$")


# --------------------------------------------------------------------- пути


def work_dir() -> Path:
    """Папка проекта ``face-retouch`` (модели, venv, brand_batch.py)."""
    return Path(settings.face_retouch_dir)


def python_exe() -> Path:
    return work_dir() / ".venv" / "Scripts" / "python.exe"


def batch_script() -> Path:
    return work_dir() / "brand_batch.py"


def etalon_script() -> Path:
    """``etalon_prep.py`` — готовит эталон (кроп 1024 + GFPGAN) из фото."""
    return work_dir() / "etalon_prep.py"


def models_dir() -> Path:
    return work_dir() / "models"


def custom_dir() -> Path:
    """Свои эталоны, загруженные из UI (встроенные лежат прямо в ``models``)."""
    return models_dir() / CUSTOM_DIR_NAME


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


def _builtin_references() -> list[dict]:
    folder = models_dir()
    if not folder.is_dir():
        return []
    return [{"id": p.name, "label": p.stem, "custom": False}
            for p in sorted(folder.glob(f"*{ETALON_EXT}"))]


def _custom_references() -> list[dict]:
    folder = custom_dir()
    if not folder.is_dir():
        return []
    return [{"id": CUSTOM_PREFIX + p.stem, "label": p.stem, "custom": True}
            for p in sorted(folder.glob(f"*{ETALON_EXT}"))]


def references() -> list[dict]:
    """Эталоны: сначала встроенные, потом свои (id — безопасный ключ)."""
    return _builtin_references() + _custom_references()


def default_reference() -> str:
    """Встроенный эталон по умолчанию; свой им никогда не становится."""
    names = [r["id"] for r in _builtin_references()]
    for preferred in PREFERRED_REFERENCES:
        if preferred in names:
            return preferred
    return names[0] if names else ""


def _etalon_stem(raw: str) -> str:
    """``custom:имя`` (или ``имя.npz``) → безопасное имя без пути и расширения."""
    value = str(raw or "").strip()
    if value.startswith(CUSTOM_PREFIX):
        value = value[len(CUSTOM_PREFIX):]
    value = Path(value).name
    if value.lower().endswith(ETALON_EXT):
        value = value[: -len(ETALON_EXT)]
    return value.strip()


def resolve_reference(name: str) -> Path:
    """Эталон → путь. Наружу отдаются только имена без обхода путей."""
    raw = str(name or "").strip()
    if not raw:
        raw = default_reference()
        if not raw:
            raise ValueError("В models нет ни одного эталона (*.npz)")
    stem = _etalon_stem(raw)
    if not stem:
        raise ValueError(f"Эталон не найден: {name}")
    if raw.startswith(CUSTOM_PREFIX):
        path = custom_dir() / f"{stem}{ETALON_EXT}"
    else:
        path = models_dir() / f"{stem}{ETALON_EXT}"
    if not path.is_file():
        raise ValueError(f"Эталон не найден: {raw}")
    return path


# -------------------------------------------------------------- свои эталоны

_NAME_BAD_RE = re.compile(r"[^0-9A-Za-zА-Яа-яЁё _-]+")
_NAME_MAX = 40


def _clean_etalon_name(name: str, filename: str) -> str:
    """Имя эталона из подписи или имени файла: без пути, мусора и хвоста .jpg."""
    base = str(name or "").strip() or Path(str(filename or "")).stem
    base = Path(base).name
    base = re.sub(r"\.(?:jpe?g|png|webp|npz)$", "", base, flags=re.IGNORECASE)
    base = _NAME_BAD_RE.sub(" ", base)
    base = re.sub(r"\s+", " ", base).strip(" ._-")
    return base[:_NAME_MAX].strip() or "etalon"


def _unique_etalon_path(stem: str) -> tuple[Path, str]:
    folder = custom_dir()
    folder.mkdir(parents=True, exist_ok=True)
    candidate, n = stem, 1
    while (folder / f"{candidate}{ETALON_EXT}").exists():
        n += 1
        candidate = f"{stem}_{n}"
    return folder / f"{candidate}{ETALON_EXT}", candidate


def _run_etalon_prep(image: Path, out_npz: Path) -> None:
    """``etalon_prep.py``: фото → .npz (кроп 1024 + GFPGAN)."""
    cmd = [str(python_exe()), "-X", "utf8", str(etalon_script()),
           str(image), "-o", str(out_npz)]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            cmd, cwd=str(work_dir()), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=ETALON_TIMEOUT, creationflags=creationflags)
    except subprocess.TimeoutExpired:
        raise ValueError(
            f"Подготовка эталона не уложилась в {ETALON_TIMEOUT} с") from None
    except OSError as exc:
        raise ValueError(f"Не удалось запустить etalon_prep.py: {exc}") from exc
    if proc.returncode == 0 and out_npz.is_file():
        return
    out = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    tail = " ".join(out.splitlines()[-3:])
    if "no face" in tail.lower():
        raise ValueError("На фото не найдено лицо — нужен портрет анфас, "
                         "лицо крупно и без сильного наклона")
    raise ValueError("Не удалось подготовить эталон: "
                     + (tail or f"код {proc.returncode}"))


def create_etalon(filename: str, data: bytes, name: str = "") -> dict:
    """Фото пользователя → свой эталон в ``models/custom/<имя>.npz``."""
    if not data:
        raise ValueError("Файл не передан")
    ext = Path(str(filename or "")).suffix.lower()
    if ext not in ETALON_IMAGE_EXT:
        raise ValueError("Нужен JPG, PNG или WEBP")
    if len(data) > ETALON_MAX_BYTES:
        raise ValueError("Файл больше 20 МБ")
    if not python_exe().is_file() or not etalon_script().is_file():
        raise ValueError("Нет etalon_prep.py или его venv: " + str(work_dir()))
    stem = _clean_etalon_name(name, filename)
    _JOB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="etalon_", dir=str(_JOB_DIR)))
    try:
        src = tmp / f"src{ext}"
        src.write_bytes(data)
        out_npz = tmp / f"out{ETALON_EXT}"
        _run_etalon_prep(src, out_npz)
        dst, final_stem = _unique_etalon_path(stem)
        shutil.move(str(out_npz), str(dst))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"id": CUSTOM_PREFIX + final_stem, "label": final_stem, "custom": True}


def delete_etalon(etalon_id: str) -> bool:
    """Удаляет свой эталон; встроенные неудаляемы."""
    raw = str(etalon_id or "").strip()
    if not raw.startswith(CUSTOM_PREFIX):
        raise ValueError("Встроенный эталон удалить нельзя")
    stem = _etalon_stem(raw)
    path = custom_dir() / f"{stem}{ETALON_EXT}"
    if not stem or not path.is_file():
        raise ValueError("Эталон не найден")
    with _LOCK:
        busy = any(j["status"] == "running"
                   and j.get("reference") == f"{stem}{ETALON_EXT}"
                   for j in _JOBS.values())
    if busy:
        raise ValueError("Этот эталон используется текущей обработкой")
    try:
        path.unlink()
    except OSError as exc:
        raise ValueError(f"Не удалось удалить эталон: {exc}") from exc
    return True


def build_command(source: list, out_dir: Path, variant: str,
                  reference: str) -> list[str]:
    """Команда запуска ``brand_batch.py`` для варианта и эталона."""
    v = get_variant(variant)
    cmd = [
        str(python_exe()), "-X", "utf8", str(batch_script()),
        *[str(s) for s in source],
        "--source", str(resolve_reference(reference)),
        "-o", str(out_dir),
        "--preview",
        *v["args"],
    ]
    return cmd


# ------------------------------------------------------------------ прогресс


def parse_progress(line: str) -> Optional[dict]:
    """Строка ``brand_batch.py`` → событие прогресса (или None).

    Формат: ``[1/4] name.JPG  swap=1 faces=1  13.2s``; ошибка —
    ``[1/4] name.JPG  FAILED: ...``. Прочий вывод (onnxruntime, сводка JSON)
    игнорируется.
    """
    m = _PROGRESS_RE.match(line or "")
    if m:
        return {
            "kind": "done",
            "index": int(m.group(1)),
            "total": int(m.group(2)),
            "name": m.group(3),
            "swap": int(m.group(4)),
            "faces": int(m.group(5)),
            "seconds": float(m.group(6)),
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
        "references": references(),
        "default_reference": default_reference(),
        "default_source": last_source() or settings.face_source_default,
        "out_name": OUT_NAME,
        "ready": (python_exe().is_file() and batch_script().is_file()
                  and bool(references())),
        "face_dir": str(work_dir()),
        "extensions": sorted(IMAGE_EXT),
        # свои эталоны: папка, префикс id и можно ли их готовить (etalon_prep.py)
        "custom_dir": str(custom_dir()),
        "custom_prefix": CUSTOM_PREFIX,
        "can_add_etalon": (python_exe().is_file() and etalon_script().is_file()),
        "etalon_extensions": sorted(ETALON_IMAGE_EXT),
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


def start_job(source_dir: str = "", variant: str = DEFAULT_VARIANT,
              reference: str = "", uploads: Optional[list] = None,
              job_id: Optional[int] = None) -> dict:
    """Ставит обработку в фоновую нить. Возвращает ``{job_id}``.

    ``uploads`` — список ``(имя файла, bytes)`` для режима drop-зоны; если он
    пуст, обрабатывается папка ``source_dir``.

    Валидация (нет папки/картинок, незнакомый вариант, нет пайплайна) —
    ``ValueError``, API превращает её в 400.
    """
    uploads = list(uploads or [])
    variant = (variant or DEFAULT_VARIANT).strip() or DEFAULT_VARIANT
    v = get_variant(variant)

    if not python_exe().is_file() or not batch_script().is_file():
        raise ValueError(
            "Пайплайн обработки лиц не найден: " + str(work_dir())
            + " (нужны .venv\\Scripts\\python.exe и brand_batch.py)")
    # id эталона (`custom:<имя>` или имя файла) идёт в воркер как есть:
    # resolve_reference(ref.name) потерял бы префикс своих эталонов.
    ref_id = str(reference or "").strip() or default_reference()
    ref = resolve_reference(ref_id)

    # Модели (inswapper + GFPGAN) тяжёлые: два одновременных прогона на 8 ГБ
    # VRAM только мешают друг другу, поэтому второе задание не запускаем.
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
        filename = f"faces_{variant}.zip"
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
        filename = f"faces_{variant}.zip"
        _remember_source(folder)

    job = {
        "id": job_id,
        "status": "running",
        "mode": mode,
        "variant": variant,
        "variant_label": v["label"],
        "reference": ref.name,
        "source": source,
        "out_dir": str(out_dir),
        "started_at": _now(),
        "finished_at": None,
        "total": total,
        "done": 0,
        "no_face": 0,
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
              variant, ref_id),
        daemon=True,
    ).start()
    return {"job_id": job_id}


def _run_worker(job_id: int, sources: list, out_dir: Path,
                variant: str, reference: str) -> None:
    job = _JOBS[job_id]
    try:
        cmd = build_command(sources, out_dir, variant, reference)
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
                elif not ev.get("faces"):
                    job["no_face"] += 1
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
            if p.is_file():
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
        "reference": job["reference"],
        "source": job["source"],
        "out_dir": job["out_dir"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "total": job["total"],
        "done": job["done"],
        "no_face": job["no_face"],
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
