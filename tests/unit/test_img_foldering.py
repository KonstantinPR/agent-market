# -*- coding: utf-8 -*-
"""«Фото для маркетплейса»: раскладка фото WB/Ozon в ZIP."""

import io
import zipfile

from app.services import img_foldering as f


class _Entry:
    def __init__(self, items):
        self.items = items


class FakeIndex:
    """Мини-индекс в интерфейсе ``PhotoIndex.get``."""

    def __init__(self, mapping):
        self._m = mapping

    def get(self, article, allow_prefix=False):
        return self._m.get(str(article).strip().upper())


def _mk(tmp_path, name, data=b"x"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _index(tmp_path):
    return FakeIndex({
        "A1": _Entry([(1, _mk(tmp_path, "a1-1.jpg")), (2, _mk(tmp_path, "a1-2.jpg"))]),
        "B2": _Entry([(1, _mk(tmp_path, "b2-1.jpg", b"yy"))]),
    })


def test_plan_wb_uses_nm_folder(tmp_path):
    idx = _index(tmp_path)
    entries, rep = f.plan(["a1"], {"A1": "111"}, "WB", "ONLY_NEW", "ASCENDING", index=idx)
    assert [e.folder for e in entries] == ["111"]
    assert [n for n, _ in entries[0].files] == ["a1-1.jpg", "a1-2.jpg"]
    assert rep["found"] == 1 and rep["photos"] == 2


def test_plan_wb_without_nm(tmp_path):
    idx = _index(tmp_path)
    entries, rep = f.plan(["b2"], {}, "WB", index=idx)
    assert entries == []
    assert rep["no_nm"] == ["b2"]


def test_plan_missing_photo(tmp_path):
    idx = _index(tmp_path)
    entries, rep = f.plan(["zzz"], {"ZZZ": "9"}, "WB", index=idx)
    assert entries == []
    assert rep["missing"] == ["zzz"]


def test_plan_ozon_folder_and_names(tmp_path):
    idx = _index(tmp_path)
    entries, _ = f.plan(["a1"], {}, "OZON", index=idx)
    assert entries[0].folder == "a1"
    assert [n for n, _ in entries[0].files] == ["a1.jpg", "a1_2.jpg"]


def test_plan_only_new_dedupes_by_number(tmp_path):
    idx = FakeIndex({
        "A1": _Entry([
            (1, _mk(tmp_path, "n-1a.jpg")),
            (1, _mk(tmp_path, "n-1b.jpg")),
            (2, _mk(tmp_path, "n-2.jpg")),
        ]),
    })
    only_new, _ = f.plan(["a1"], {"A1": "1"}, "WB", "ONLY_NEW", index=idx)
    all_photos, _ = f.plan(["a1"], {"A1": "1"}, "WB", "ALL", index=idx)
    assert len(only_new[0].files) == 2
    assert len(all_photos[0].files) == 3


def test_plan_descending_order(tmp_path):
    idx = _index(tmp_path)
    entries, _ = f.plan(["a1"], {"A1": "1"}, "WB", "ONLY_NEW", "DESCENDING", index=idx)
    assert [n for n, _ in entries[0].files] == ["a1-2.jpg", "a1-1.jpg"]


def test_build_zip_names(tmp_path):
    e = f.ArticleEntry(
        article="a1", folder="a1",
        files=[("a1.jpg", _mk(tmp_path, "a1.jpg")),
               ("a1_2.jpg", _mk(tmp_path, "a1_2.jpg", b"yy"))],
    )
    buf = f.build_zip([e])
    z = zipfile.ZipFile(buf)
    assert sorted(z.namelist()) == ["a1/a1.jpg", "a1/a1_2.jpg"]


def test_build_zip_leading_nulls(tmp_path):
    e = f.ArticleEntry(
        article="a1", folder="a1",
        files=[("a1.jpg", _mk(tmp_path, "a1.jpg")),
               ("a1_2.jpg", _mk(tmp_path, "a1_2.jpg", b"yy"))],
    )
    z = zipfile.ZipFile(f.build_zip([e], leading_nulls=True))
    assert "a1/a1_002.jpg" in z.namelist()


def test_build_zip_change_order_swaps_content(tmp_path):
    p1 = _mk(tmp_path, "a1.jpg", b"first")
    p2 = _mk(tmp_path, "a1_2.jpg", b"second")
    e = f.ArticleEntry(article="a1", folder="a1",
                       files=[("a1.jpg", p1), ("a1_2.jpg", p2)])
    z = zipfile.ZipFile(f.build_zip([e], change_order=2))
    assert z.read("a1/a1.jpg") == b"second"
    assert z.read("a1/a1_2.jpg") == b"first"


def test_build_zip_of_zips(tmp_path):
    e1 = f.ArticleEntry(article="a1", folder="a1",
                        files=[("a1.jpg", _mk(tmp_path, "a1.jpg", b"aaaa"))])
    e2 = f.ArticleEntry(article="b2", folder="b2",
                        files=[("b2.jpg", _mk(tmp_path, "b2.jpg", b"bbbb"))])
    z = zipfile.ZipFile(f.build_zip([e1, e2], max_part_mb=0))
    names = set(z.namelist())
    assert "README.txt" in names
    assert "images_part_001.zip" in names and "images_part_002.zip" in names
    part = zipfile.ZipFile(io.BytesIO(z.read("images_part_001.zip")))
    assert part.namelist() == ["a1/a1.jpg"]
