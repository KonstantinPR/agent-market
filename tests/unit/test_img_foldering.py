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


def test_plan_wb_missing_first_photo_shifts_numbers(tmp_path):
    """Нет ``-1``: ближайшее фото (``-2``) получает позицию ``-1``."""
    idx = FakeIndex({
        "A1": _Entry([(2, _mk(tmp_path, "a1-2.jpg", b"second")),
                      (3, _mk(tmp_path, "a1-3.jpg", b"third"))]),
    })
    entries, _ = f.plan(["a1"], {"A1": "1"}, "WB", "ONLY_NEW", index=idx)
    files = entries[0].files
    assert [n for n, _ in files] == ["a1-1.jpg", "a1-2.jpg"]
    assert files[0][1].read_bytes() == b"second"
    assert files[1][1].read_bytes() == b"third"


def test_plan_wb_missing_first_photo_shift_by_three(tmp_path):
    idx = FakeIndex({"A1": _Entry([(3, _mk(tmp_path, "a1-3.jpg"))])})
    entries, _ = f.plan(["a1"], {"A1": "1"}, "WB", index=idx)
    assert [n for n, _ in entries[0].files] == ["a1-1.jpg"]


def test_plan_wb_existing_first_photo_keeps_gap(tmp_path):
    """``-1`` на месте — номера не сдвигаем, дырка в нумерации остаётся."""
    idx = FakeIndex({
        "A1": _Entry([(1, _mk(tmp_path, "a1-1.jpg")),
                      (3, _mk(tmp_path, "a1-3.jpg"))]),
    })
    entries, _ = f.plan(["a1"], {"A1": "1"}, "WB", "ONLY_NEW", index=idx)
    assert [n for n, _ in entries[0].files] == ["a1-1.jpg", "a1-3.jpg"]


def test_plan_wb_shift_all_mode_and_unumbered_name(tmp_path):
    idx = FakeIndex({
        "A1": _Entry([
            (2, _mk(tmp_path, "a1-2.jpg", b"a")),
            (2, _mk(tmp_path, "a1-2-extra.jpg", b"b")),
            (4, _mk(tmp_path, "a1-4.jpg", b"c")),
        ]),
    })
    entries, _ = f.plan(["a1"], {"A1": "1"}, "WB", "ALL", index=idx)
    # имя без числового хвоста остаётся как есть — позицию задаёт индекс
    assert [n for n, _ in entries[0].files] == ["a1-2-extra.jpg", "a1-1.jpg", "a1-3.jpg"]


def test_plan_wb_shift_keeps_desc_and_padding(tmp_path):
    idx = FakeIndex({
        "A1": _Entry([(2, _mk(tmp_path, "a1-002.JPG", b"x")),
                      (3, _mk(tmp_path, "a1-003.JPG", b"y"))]),
    })
    entries, _ = f.plan(["a1"], {"A1": "1"}, "WB", "ONLY_NEW", "DESCENDING", index=idx)
    files = entries[0].files
    assert [n for n, _ in files] == ["a1-002.JPG", "a1-001.JPG"]
    assert files[0][1].read_bytes() == b"y"
    assert files[1][1].read_bytes() == b"x"


def test_plan_ozon_ignores_shift(tmp_path):
    idx = FakeIndex({
        "A1": _Entry([(2, _mk(tmp_path, "a1-2.jpg")),
                      (3, _mk(tmp_path, "a1-3.jpg"))]),
    })
    entries, _ = f.plan(["a1"], {}, "OZON", index=idx)
    assert [n for n, _ in entries[0].files] == ["a1.jpg", "a1_2.jpg"]


def test_shift_name_without_number(tmp_path):
    assert f._shift_name("NO8B7861.JPG", 2) == "NO8B7861.JPG"
    assert f._shift_name("a1-5.jpg", 0) == "a1-5.jpg"
    assert f._shift_name("a1_5.jpg", 2) == "a1_3.jpg"


# ---------------------------------------------------- замены позиций (N ↔ M)


def test_normalize_pairs_forms_and_garbage():
    assert f.normalize_pairs([[1, 3], [2, 4]]) == [(1, 3), (2, 4)]
    assert f.normalize_pairs(["1\u21943", "2:4", "3-5"]) == [(1, 3), (2, 4), (3, 5)]
    assert f.normalize_pairs(None, 3) == [(1, 3)]  # legacy «поменять 1 и N»
    assert f.normalize_pairs([[3, 1]]) == [(3, 1)]  # обратная пара — та же замена
    assert f.normalize_pairs([[1, 1], [0, 2], ["x", 3], [2, 4], [4, 2]]) == [(2, 4)]
    assert f.normalize_pairs(None, 1) == []
    assert len(f.normalize_pairs([[i, i + 1] for i in range(1, 60)])) == f.MAX_PAIRS


def test_apply_pairs_skips_out_of_range(tmp_path):
    files = [("a1-1.jpg", _mk(tmp_path, "a1-1.jpg")),
             ("a1-2.jpg", _mk(tmp_path, "a1-2.jpg", b"yy"))]
    assert f._apply_pairs(files, [(1, 5)]) == files
    assert f._apply_pairs(files, [(3, 1)]) == files


def test_plan_photo_pairs_swaps_content_not_names(tmp_path):
    idx = FakeIndex({"A1": _Entry([
        (1, _mk(tmp_path, "a1-1.jpg", b"one")),
        (2, _mk(tmp_path, "a1-2.jpg", b"two")),
        (3, _mk(tmp_path, "a1-3.jpg", b"three")),
    ])})
    entries, rep = f.plan(["a1"], {"A1": "111"}, "WB", index=idx, photo_pairs=[[1, 3]])
    files = entries[0].files
    assert [n for n, _ in files] == ["a1-1.jpg", "a1-2.jpg", "a1-3.jpg"]
    assert [p.read_bytes() for _, p in files] == [b"three", b"two", b"one"]
    row = rep["with_photos"][0]
    assert row["order"][0] == {"pos": 1, "name": "a1-1.jpg", "src": "a1-3.jpg"}
    assert row["skipped"] == []


def test_plan_photo_pairs_chain(tmp_path):
    idx = FakeIndex({"A1": _Entry([
        (1, _mk(tmp_path, "a1-1.jpg", b"1")),
        (2, _mk(tmp_path, "a1-2.jpg", b"2")),
        (3, _mk(tmp_path, "a1-3.jpg", b"3")),
    ])})
    entries, _ = f.plan(["a1"], {"A1": "1"}, "WB", index=idx,
                        photo_pairs=[[1, 2], [2, 3]])
    assert [p.read_bytes() for _, p in entries[0].files] == [b"2", b"3", b"1"]


def test_plan_photo_pairs_skip_when_too_few_photos(tmp_path):
    idx = FakeIndex({"A1": _Entry([
        (1, _mk(tmp_path, "a1-1.jpg", b"one")),
        (2, _mk(tmp_path, "a1-2.jpg", b"two")),
    ])})
    entries, rep = f.plan(["a1"], {"A1": "1"}, "WB", index=idx, photo_pairs=[[1, 5]])
    assert [p.read_bytes() for _, p in entries[0].files] == [b"one", b"two"]
    assert rep["with_photos"][0]["skipped"] == ["1\u21945"]


def test_plan_legacy_change_order_equals_pair(tmp_path):
    def idx():
        return FakeIndex({"A1": _Entry([
            (1, _mk(tmp_path, "a1-1.jpg", b"one")),
            (2, _mk(tmp_path, "a1-2.jpg", b"two")),
            (3, _mk(tmp_path, "a1-3.jpg", b"three")),
        ])})

    legacy, _ = f.plan(["a1"], {"A1": "1"}, "WB", index=idx(), change_order=3)
    pairs, _ = f.plan(["a1"], {"A1": "1"}, "WB", index=idx(), photo_pairs=[[1, 3]])
    assert [p.read_bytes() for _, p in legacy[0].files] == [p.read_bytes() for _, p in pairs[0].files]


def test_plan_photo_pairs_are_per_article(tmp_path):
    idx = FakeIndex({
        "A1": _Entry([(1, _mk(tmp_path, "a1-1.jpg", b"a1")),
                      (2, _mk(tmp_path, "a1-2.jpg", b"a2"))]),
        "B2": _Entry([(1, _mk(tmp_path, "b2-1.jpg", b"b1")),
                      (2, _mk(tmp_path, "b2-2.jpg", b"b2"))]),
    })
    entries, _ = f.plan(["a1", "b2"], {"A1": "1", "B2": "2"}, "WB", index=idx,
                        photo_pairs=[[1, 2]])
    got = {e.article: [p.read_bytes() for _, p in e.files] for e in entries}
    assert got == {"a1": [b"a2", b"a1"], "b2": [b"b2", b"b1"]}


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
