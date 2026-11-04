# -*- coding: utf-8 -*-
"""«Обработка лиц»: разбор прогресса, сборка команды, варианты, валидация."""

import pytest

from app.config import settings
from app.services import face_retouch as face


@pytest.fixture()
def pipeline(tmp_path, monkeypatch):
    """Фейковый face-retouch: venv-питон, brand_batch.py, etalon_prep.py, эталоны."""
    models = tmp_path / "models"
    models.mkdir()
    (models / "etalon_180a.npz").write_bytes(b"npz")
    (models / "etalon_ed49.npz").write_bytes(b"npz")
    venv = tmp_path / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    python = venv / "python.exe"
    python.write_bytes(b"")
    script = tmp_path / "brand_batch.py"
    script.write_text("# fake", encoding="utf-8")
    prep = tmp_path / "etalon_prep.py"
    prep.write_text("# fake", encoding="utf-8")

    monkeypatch.setattr(settings, "face_retouch_dir", str(tmp_path))
    monkeypatch.setattr(face, "_JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(face, "_LAST_SOURCE_FILE", tmp_path / "jobs" / "last.txt")
    face._JOBS.clear()
    return {"root": tmp_path, "python": python, "script": script, "models": models}


@pytest.fixture()
def fake_prep(monkeypatch):
    """Подменяет запуск etalon_prep.py: пишет «npz» в выходной файл."""
    calls = []

    def _run(image, out_npz):
        calls.append((image, out_npz))
        out_npz.write_bytes(b"npz")

    monkeypatch.setattr(face, "_run_etalon_prep", _run)
    return calls


# ------------------------------------------------------------------ прогресс


def test_parse_progress_ok():
    ev = face.parse_progress("[2/4] JZA-JNS-H6932-BLUE-1.JPG  swap=1 faces=1  13.5s")
    assert ev == {
        "kind": "done", "index": 2, "total": 4,
        "name": "JZA-JNS-H6932-BLUE-1.JPG",
        "swap": 1, "faces": 1, "seconds": 13.5,
    }


def test_parse_progress_no_face():
    ev = face.parse_progress("[1/2] side.JPG  swap=0 faces=0  4.0s")
    assert ev["kind"] == "done"
    assert ev["faces"] == 0


def test_parse_progress_failed():
    ev = face.parse_progress("[3/4] broken.JPG  FAILED: cannot identify image file")
    assert ev["kind"] == "failed"
    assert ev["index"] == 3
    assert "cannot identify" in ev["error"]


def test_parse_progress_ignores_other_output():
    for line in ("[i] brand face: etalon_180a.npz",
                 '{"total": 4, "done": 4}',
                 "[W:onnxruntime:Default, graph.cc:4198] Removing initializer",
                 ""):
        assert face.parse_progress(line) is None


# ------------------------------------------------------------------ варианты


def test_variant_v1_is_swap_without_retouch():
    v = face.get_variant("v1")
    assert "--no-retouch" in v["args"]
    i = v["args"].index("--swap-enhance")
    assert v["args"][i + 1] == "0.85"
    assert "--retouch-enhance" not in v["args"]
    assert v["label"]


def test_v1_args_are_frozen():
    """v1 — принятый результат: аргументы менять нельзя (и порядок тоже)."""
    assert face.get_variant("v1")["args"] == [
        "--no-retouch", "--swap-enhance", "0.85", "--boost", "512"]
    assert face.DEFAULT_VARIANT == "v1"


def test_v2_v3_only_change_gfpgan_strength():
    expected = {"v2": "0.55", "v3": "1.0"}
    for vid, strength in expected.items():
        args = face.get_variant(vid)["args"]
        assert args == ["--no-retouch", "--swap-enhance", strength,
                        "--boost", "512"]
        assert face.get_variant(vid)["label"]


def test_unknown_variant_rejected():
    with pytest.raises(ValueError):
        face.get_variant("v99")


def test_options_lists_pipeline(pipeline):
    opt = face.options()
    assert opt["default_variant"] == "v1"
    assert [v["id"] for v in opt["variants"]] == ["v1", "v2", "v3"]
    assert opt["default_reference"] == "etalon_180a.npz"
    assert {r["id"] for r in opt["references"]} == {
        "etalon_180a.npz", "etalon_ed49.npz"}
    assert all(r["custom"] is False for r in opt["references"])
    assert opt["custom_prefix"] == face.CUSTOM_PREFIX
    assert opt["can_add_etalon"] is True
    assert opt["ready"] is True
    assert opt["out_name"] == face.OUT_NAME


def test_reference_strips_foreign_paths(pipeline):
    # Снаружи принимается только имя файла: путь отбрасывается.
    p = face.resolve_reference(r"C:\Windows\System32\etalon_ed49.npz")
    assert p == pipeline["models"] / "etalon_ed49.npz"
    with pytest.raises(ValueError):
        face.resolve_reference("missing.npz")


# --------------------------------------------------------------- свои эталоны


def test_create_etalon_saves_custom_npz(pipeline, fake_prep):
    res = face.create_etalon("masha.jpg", b"jpeg", name="Маша")
    assert res == {"id": "custom:Маша", "label": "Маша", "custom": True}
    saved = pipeline["models"] / "custom" / "Маша.npz"
    assert saved.read_bytes() == b"npz"
    assert fake_prep  # etalon_prep.py действительно вызывался


def test_custom_etalon_appears_in_options(pipeline, fake_prep):
    face.create_etalon("masha.jpg", b"jpeg", name="Маша")
    refs = face.references()
    assert refs[-1] == {"id": "custom:Маша", "label": "Маша", "custom": True}
    # встроенные идут первыми, эталон по умолчанию не меняется
    assert [r["custom"] for r in refs[:2]] == [False, False]
    assert face.default_reference() == "etalon_180a.npz"
    assert face.resolve_reference("custom:Маша") == (
        pipeline["models"] / "custom" / "Маша.npz")


def test_create_etalon_unique_names(pipeline, fake_prep):
    assert face.create_etalon("a.jpg", b"jpeg", name="Маша")["id"] == "custom:Маша"
    assert face.create_etalon("b.jpg", b"jpeg", name="Маша")["id"] == "custom:Маша_2"


def test_create_etalon_rejects_bad_input(pipeline, fake_prep):
    with pytest.raises(ValueError, match="JPG"):
        face.create_etalon("masha.gif", b"gif")
    with pytest.raises(ValueError, match="20 МБ"):
        face.create_etalon("masha.jpg", b"x" * (face.ETALON_MAX_BYTES + 1))
    with pytest.raises(ValueError, match="не передан"):
        face.create_etalon("masha.jpg", b"")


def test_delete_etalon(pipeline, fake_prep):
    face.create_etalon("masha.jpg", b"jpeg", name="Маша")
    assert face.delete_etalon("custom:Маша") is True
    assert not (pipeline["models"] / "custom" / "Маша.npz").exists()
    with pytest.raises(ValueError, match="не найден"):
        face.delete_etalon("custom:Маша")


def test_delete_builtin_etalon_rejected(pipeline):
    with pytest.raises(ValueError, match="Встроенный"):
        face.delete_etalon("etalon_180a.npz")


# ------------------------------------------------------------------ команда


def test_build_command_uses_variant_and_reference(pipeline, tmp_path):
    src = tmp_path / "photos"
    src.mkdir()
    cmd = face.build_command([str(src)], tmp_path / "out", "v1", "etalon_180a.npz")
    assert cmd[0] == str(pipeline["python"])
    assert cmd[1:3] == ["-X", "utf8"]
    assert cmd[3] == str(pipeline["script"])
    assert str(src) in cmd
    assert str(pipeline["models"] / "etalon_180a.npz") in cmd
    assert cmd[cmd.index("-o") + 1] == str(tmp_path / "out")
    assert "--preview" in cmd
    assert "--no-retouch" in cmd
    assert "--swap-enhance" in cmd and "0.85" in cmd


def test_build_command_uses_v2_strength(pipeline, tmp_path):
    cmd = face.build_command([str(tmp_path)], tmp_path / "out", "v2", "")
    assert cmd[cmd.index("--swap-enhance") + 1] == "0.55"


def test_build_command_custom_reference(pipeline, fake_prep, tmp_path):
    face.create_etalon("masha.jpg", b"jpeg", name="Маша")
    cmd = face.build_command([str(tmp_path)], tmp_path / "out", "v1", "custom:Маша")
    assert str(pipeline["models"] / "custom" / "Маша.npz") in cmd


# ------------------------------------------------------------------ валидация


def test_start_requires_source(pipeline):
    with pytest.raises(ValueError):
        face.start_job(source_dir="", variant="v1", reference="")


def test_start_missing_folder(pipeline, tmp_path):
    with pytest.raises(ValueError):
        face.start_job(source_dir=str(tmp_path / "nope"), variant="v1")


def test_start_empty_folder(pipeline, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="нет изображений"):
        face.start_job(source_dir=str(empty), variant="v1")


def test_start_empty_uploads(pipeline):
    with pytest.raises(ValueError):
        face.start_job(uploads=[("a.jpg", b"")], variant="v1")


def test_second_job_rejected_while_running(pipeline):
    face._JOBS[987654] = {"status": "running"}
    try:
        with pytest.raises(ValueError, match="идёт обработка"):
            face.start_job(uploads=[("a.jpg", b"x")], variant="v1")
    finally:
        face._JOBS.pop(987654, None)
