"""Юнит-тесты сервиса тикетов (файл TICKETS.md)."""

import re

import pytest

from app.services import tickets as t


@pytest.fixture()
def tfile(tmp_path):
    p = tmp_path / "TICKETS.md"
    p.write_text(t.BOOTSTRAP, encoding="utf-8")
    return p


def _text(p):
    return p.read_text(encoding="utf-8")


def _ids(data, key):
    return [x["id"] for x in data["sections"][key]]


# ------------------------------------------------------------------ чтение

def test_load_bootstrap_empty(tfile):
    d = t.load(tfile)
    assert d["counter"] == 1
    assert d["missing"] is False
    for key in ("open", "in_progress", "blocked", "closed"):
        assert d["sections"][key] == []


def test_load_missing_file(tmp_path):
    p = tmp_path / "nothere.md"
    d = t.load(p)
    assert d["missing"] is True
    assert d["counter"] is None


# ------------------------------------------------------------------ создание

def test_create_assigns_number_and_bumps_counter(tfile):
    t.create("Сделать X", "тело", "high", path=tfile)
    assert _text(tfile).count("[T-1]") == 1
    assert re.search(r"`T-2`", _text(tfile))
    d = t.load(tfile)
    assert _ids(d, "open") == ["T-1"]
    assert d["counter"] == 2
    assert d["sections"]["open"][0]["priority"] == "high"


def test_create_multiline_body_roundtrip(tfile):
    t.create("Сделать Y", "первая\nвторая", "medium", path=tfile)
    d = t.load(tfile)
    assert d["sections"]["open"][0]["body"] == "первая\nвторая"


def test_create_validation(tfile):
    with pytest.raises(t.TicketError) as e:
        t.create("   ", path=tfile)
    assert e.value.status == 400
    with pytest.raises(t.TicketError) as e:
        t.create("Ok", priority="urgent", path=tfile)
    assert e.value.status == 400
    assert not _text(tfile).count("[T-1]")


def test_create_bootstraps_missing_file(tmp_path):
    p = tmp_path / "abc.md"
    c = t.create("Новый", "описание", "low", path=p)
    assert c["id"] == "T-1"
    assert p.exists()
    assert "## Открытые" in _text(p)


# ------------------------------------------------------------------ переходы

def test_start_moves_to_in_progress(tfile):
    t.create("A", path=tfile)
    t.start("T-1", path=tfile)
    d = t.load(tfile)
    assert _ids(d, "open") == []
    assert _ids(d, "in_progress") == ["T-1"]


def test_multiple_in_progress(tfile):
    t.create("A", path=tfile)
    t.create("B", path=tfile)
    t.start("T-1", path=tfile)
    t.start("T-2", path=tfile)
    d = t.load(tfile)
    assert set(_ids(d, "in_progress")) == {"T-1", "T-2"}


def test_block_unblock(tfile):
    t.create("A", path=tfile)
    t.start("T-1", path=tfile)
    t.block("T-1", path=tfile)
    d = t.load(tfile)
    assert _ids(d, "blocked") == ["T-1"]
    t.unblock("T-1", path=tfile)
    assert _ids(t.load(tfile), "open") == ["T-1"]


def test_close_with_commit_and_reopen(tfile):
    t.create("A", path=tfile)
    t.start("T-1", path=tfile)
    t.close("T-1", commit="abc1234", path=tfile)
    d = t.load(tfile)
    assert _ids(d, "closed") == ["T-1"]
    c = d["sections"]["closed"][0]
    assert c["state"] == "closed"
    assert c["commit"] == "abc1234"
    assert "ссылка на коммит `abc1234`" in _text(tfile)
    t.reopen("T-1", path=tfile)
    d = t.load(tfile)
    assert _ids(d, "open") == ["T-1"]
    assert d["sections"]["open"][0]["commit"] is None


def test_decline(tfile):
    t.create("A", path=tfile)
    t.decline("T-1", path=tfile)
    d = t.load(tfile)
    assert d["sections"]["closed"][0]["state"] == "declined"
    assert "(declined)" in _text(tfile)


def test_close_without_commit(tfile):
    t.create("A", path=tfile)
    t.close("T-1", path=tfile)
    d = t.load(tfile)
    assert d["sections"]["closed"][0]["commit"] is None
    closed_line = [ln for ln in _text(tfile).split("\n") if ln.startswith("- [T-1] (closed)")][0]
    assert "ссылка на коммит" not in closed_line


# ------------------------------------------------------------------ ошибки

def test_unknown_id(tfile):
    with pytest.raises(t.TicketError) as e:
        t.start("T-99", path=tfile)
    assert e.value.status == 404


def test_invalid_transition(tfile):
    t.create("A", path=tfile)
    t.start("T-1", path=tfile)
    with pytest.raises(t.TicketError) as e:
        t.start("T-1", path=tfile)
    assert e.value.status == 409
    with pytest.raises(t.TicketError) as e:
        t.unblock("T-1", path=tfile)
    assert e.value.status == 409


def test_no_file_mutation_for_unknown(tfile, tmp_path):
    p = tmp_path / "gone.md"
    with pytest.raises(t.TicketError) as e:
        t.start("T-1", path=p)
    assert e.value.status == 404


# ------------------------------------------------------------------ целостность файла

def _head(text):
    """Часть файла до секции тикетов, со счётчиком, нормализованным на «T-N»."""
    head = text.split("\n## Открытые\n")[0]
    return re.sub(r"`T-\d+`", "`T-N`", head)


def test_head_and_rules_preserved(tfile):
    head = _head(_text(tfile))
    t.create("A", "тело", "high", path=tfile)
    t.start("T-1", path=tfile)
    t.close("T-1", commit="deadbeef", path=tfile)
    t.create("B", path=tfile)
    assert _head(_text(tfile)) == head  # шапка и правила — не тронуты


def test_placeholder_restored_when_section_emptied(tfile):
    t.create("A", path=tfile)
    t.start("T-1", path=tfile)
    body = _text(tfile).split("\n## Открытые\n")[1].split("\n## В работе\n")[0]
    assert "_(пусто)_" in body


def test_existing_raw_body_indent_kept(tfile):
    """Блок с произвольным отступом тела не ломается при перемещении."""
    text = _text(tfile).replace("_(пусто)_", "- [T-1] (medium) Заголовок\n  > одна\n\t> две")
    tfile.write_text(text, encoding="utf-8")
    t.start("T-1", path=tfile)
    d = t.load(tfile)
    assert d["sections"]["in_progress"][0]["body"] == "одна\nдве"