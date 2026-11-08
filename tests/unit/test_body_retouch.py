# -*- coding: utf-8 -*-
"""«Ретушь тела»: разбор прогресса, сборка команды, варианты, задания."""

import sys
import time
from pathlib import Path

import pytest

from app.config import settings
from app.services import body_retouch as body

FAKE_SCRIPT = '''
import pathlib, sys

args = sys.argv[1:]
out = pathlib.Path(args[args.index("-o") + 1])
out.mkdir(parents=True, exist_ok=True)
(out / "preview").mkdir(exist_ok=True)
(out / "preview" / "a__preview.jpg").write_bytes(b"jpg")
(out / "a.jpg").write_bytes(b"jpg")
print("[1/1] a.jpg  skin=3.2% match=1 noskin=0  4.2s", flush=True)
print('{"total": 1, "done": 1}')
'''


@pytest.fixture()
def pipeline(tmp_path, monkeypatch):
    """Фейковый пайплайн: «venv-питон», body_retouch.py, модель сегментации."""
    models = tmp_path / "models"
    models.mkdir()
    (models / body.MODEL_NAME).write_bytes(b"tflite")
    script = tmp_path / body.SCRIPT_NAME
    script.write_text(FAKE_SCRIPT, encoding="utf-8")

    monkeypatch.setattr(settings, "face_retouch_dir", str(tmp_path))
    # «venv-питон» — настоящий интерпретатор, чтобы фейковый скрипт реально шёл.
    monkeypatch.setattr(body, "python_exe", lambda: Path(sys.executable))
    monkeypatch.setattr(body, "_JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(body, "_LAST_SOURCE_FILE", tmp_path / "jobs" / "last.txt")
    body._JOBS.clear()
    return {"root": tmp_path, "models": models, "script": script}


# ------------------------------------------------------------------ прогресс


def test_parse_progress_ok():
    ev = body.parse_progress(
        "[2/4] JZA-JNS-H6932-BLUE-1.JPG  skin=1.4% match=1 noskin=0  4.5s")
    assert ev == {
        "kind": "done", "index": 2, "total": 4,
        "name": "JZA-JNS-H6932-BLUE-1.JPG",
        "skin_pct": 1.4, "matched": 1, "no_skin": False, "seconds": 4.5,
    }


def test_parse_progress_no_skin():
    ev = body.parse_progress("[1/2] shoes.jpg  skin=0.0% match=0 noskin=1  2.0s")
    assert ev["kind"] == "done"
    assert ev["no_skin"] is True and ev["skin_pct"] == 0.0


def test_parse_progress_failed():
    ev = body.parse_progress("[3/4] broken.JPG  FAILED: cannot identify image file")
    assert ev["kind"] == "failed"
    assert ev["index"] == 3
    assert "cannot identify" in ev["error"]


def test_parse_progress_ignores_other_output():
    for line in ("[i] pipeline: body_retouch.py",
                 '{"total": 1, "done": 1}',
                 "INFO: Created TensorFlow Lite XNNPACK delegate for CPU.",
                 ""):
        assert body.parse_progress(line) is None


# ------------------------------------------------------------------ варианты


def test_variants_map_to_presets():
    assert [v["id"] for v in body.variants()] == ["s1", "s2", "s3"]
    assert body.get_variant("s2")["preset"] == "medium"
    assert body.get_variant("s1")["preset"] == "soft"
    assert body.get_variant("s3")["preset"] == "gloss"
    assert body.DEFAULT_VARIANT == "s2"
    for v in body.variants():
        assert v["label"] and v["hint"]


def test_unknown_variant_rejected():
    with pytest.raises(ValueError):
        body.get_variant("s99")


def test_options_lists_pipeline(pipeline):
    opt = body.options()
    assert opt["default_variant"] == "s2"
    assert [v["id"] for v in opt["variants"]] == ["s1", "s2", "s3"]
    assert opt["ready"] is True
    assert opt["out_name"] == body.OUT_NAME
    assert ".jpg" in opt["extensions"]
    assert opt["model"].endswith(body.MODEL_NAME)


def test_options_not_ready_without_model(pipeline):
    (pipeline["models"] / body.MODEL_NAME).unlink()
    assert body.options()["ready"] is False


# ------------------------------------------------------------------ команда


def test_build_command_folder_and_preset(pipeline, tmp_path):
    src = tmp_path / "photos"
    src.mkdir()
    cmd = body.build_command([str(src)], tmp_path / "out", "s2", True)
    assert cmd[0] == sys.executable
    assert cmd[1:3] == ["-X", "utf8"]
    assert cmd[3] == str(pipeline["script"])
    assert str(src) in cmd
    assert cmd[cmd.index("-o") + 1] == str(tmp_path / "out")
    assert cmd[cmd.index("--preset") + 1] == "medium"
    assert "--preview" in cmd
    assert "--no-match" not in cmd


def test_build_command_no_match_flag(pipeline, tmp_path):
    cmd = body.build_command([str(tmp_path)], tmp_path / "out", "s3", False)
    assert cmd[cmd.index("--preset") + 1] == "gloss"
    assert "--no-match" in cmd


# ------------------------------------------------------------------ валидация


def test_start_requires_source(pipeline):
    with pytest.raises(ValueError, match="папку"):
        body.start_job(source_dir="", variant="s2")


def test_start_missing_folder(pipeline, tmp_path):
    with pytest.raises(ValueError, match="не найдена"):
        body.start_job(source_dir=str(tmp_path / "nope"), variant="s2")


def test_start_empty_folder(pipeline, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="нет изображений"):
        body.start_job(source_dir=str(empty), variant="s2")


def test_start_empty_uploads(pipeline):
    with pytest.raises(ValueError, match="Не переданы"):
        body.start_job(uploads=[("a.jpg", b"")], variant="s2")


def test_second_job_rejected_while_running(pipeline):
    body._JOBS[987654] = {"status": "running"}
    try:
        with pytest.raises(ValueError, match="идёт обработка"):
            body.start_job(uploads=[("a.jpg", b"x")], variant="s2")
    finally:
        body._JOBS.pop(987654, None)


def test_start_without_pipeline_reports_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "face_retouch_dir", str(tmp_path))
    monkeypatch.setattr(body, "_JOBS", {})
    with pytest.raises(ValueError, match="не найден"):
        body.start_job(uploads=[("a.jpg", b"x")], variant="s2")


# ------------------------------------------------------------- задание целиком


def _wait(job_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = body.job_state(job_id)
        if st and st["status"] != "running":
            return st
        time.sleep(0.05)
    raise AssertionError("задание не завершилось")


def test_upload_job_makes_zip_without_previews(pipeline):
    res = body.start_job(uploads=[("a.jpg", b"jpg-bytes")], variant="s2")
    st = _wait(res["job_id"])
    assert st["status"] == "done"
    assert st["mode"] == "upload"
    assert st["done"] == 1 and st["percent"] == 100
    assert st["match"] is True
    zpath = body.zip_path(res["job_id"])
    assert zpath is not None and zpath.is_file()
    import zipfile
    names = zipfile.ZipFile(zpath).namelist()
    assert names == ["a.jpg"]           # превью в архив не попадает
    body.clear_job_dir(res["job_id"])


def test_folder_job_writes_into_body_subfolder(pipeline, tmp_path):
    src = tmp_path / "photos"
    src.mkdir()
    (src / "a.jpg").write_bytes(b"jpg-bytes")
    res = body.start_job(source_dir=str(src), variant="s1", match=False)
    st = _wait(res["job_id"])
    assert st["status"] == "done"
    assert st["mode"] == "folder"
    assert st["out_dir"] == str(src / body.OUT_NAME)
    assert st["match"] is False
    assert (src / body.OUT_NAME / "a.jpg").is_file()
    assert body.last_source() == str(src)
    body.clear_job_dir(res["job_id"])


def test_stop_unknown_job(pipeline):
    assert body.stop_job(123456) is False
