"""Тесты индекса фотографий (app/services/photos.py).

Индекс строится по временному дереву в tmp_path — реальный диск на 137 ГБ
в тестах не используется.
"""
from pathlib import Path

from app.services.photos import (
    MIN_PREFIX_LEN,
    PREFIX_FALLBACK,
    ArticlePhotos,
    PhotoIndex,
    clean_stem,
    folder_rank,
    split_photo,
)


def _touch(root: Path, rel: str, data: bytes = b"\xff\xd8stub") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# ------------------------------------------------------------------ разбор имён

def test_clean_stem_drops_windows_junk():
    assert clean_stem("SH097-WB1305C262B-8 — копия") == "SH097-WB1305C262B-8"
    assert clean_stem("JRF-ROYCE-831A-66-1-DARK-BLUE-1 (2)") == "JRF-ROYCE-831A-66-1-DARK-BLUE-1"
    assert clean_stem("SH033-020124K086-2.JPG.JPG") == "SH033-020124K086-2"
    assert clean_stem("X-1 - copy") == "X-1"
    assert clean_stem("X-1") == "X-1"


def test_split_photo_keeps_article_with_dashes():
    assert split_photo("WLP-117116-13-1") == ("WLP-117116-13", 1)
    assert split_photo("JBG-5868-207A-3") == ("JBG-5868-207A", 3)
    assert split_photo("SK011-N30-COLDWHITE-2") == ("SK011-N30-COLDWHITE", 2)


def test_split_photo_without_number_is_first_photo():
    assert split_photo("NO8B7861") == ("NO8B7861", 1)
    assert split_photo("FaceApp_1593605347825") == ("FaceApp_1593605347825", 1)


def test_split_photo_ignores_trailing_copy_suffix():
    assert split_photo("JBG-829-692T-4 — копия") == ("JBG-829-692T", 4)


def test_folder_rank_marks_archive_anywhere(tmp_path):
    assert folder_rank(tmp_path / "ДЖИНСЫ" / "Часть 1" / "A-1.JPG", tmp_path) == 0
    assert folder_rank(tmp_path / "АРХИВ" / "0 - первые" / "A-1.JPG", tmp_path) == 1
    assert folder_rank(tmp_path / "архив" / "x" / "A-1.JPG", tmp_path) == 1


# ------------------------------------------------------------------ индекс

def test_index_finds_by_article_ignoring_case(tmp_path):
    _touch(tmp_path, "ДЖИНСЫ/Часть 1/SOHO-4016-411-Brown-1.JPG")
    _touch(tmp_path, "ДЖИНСЫ/Часть 1/SOHO-4016-411-Brown-2.JPG")
    idx = PhotoIndex(root=tmp_path)
    assert [p.name for p in idx.find("soho-4016-411-brown", 4)] == [
        "SOHO-4016-411-Brown-1.JPG",
        "SOHO-4016-411-Brown-2.JPG",
    ]
    assert idx.find("", 4) == []


def test_index_orders_by_photo_number_not_filename(tmp_path):
    for n in (10, 2, 1):
        _touch(tmp_path, f"К/ABC-{n}.JPG")
    idx = PhotoIndex(root=tmp_path)
    assert [p.name for p in idx.find("ABC", 3)] == ["ABC-1.JPG", "ABC-2.JPG", "ABC-10.JPG"]


def test_index_prefers_active_folder_over_archive(tmp_path):
    _touch(tmp_path, "АРХИВ/0 - первые джинсы/J03-BLUE-1.JPG")
    _touch(tmp_path, "ДЖИНСЫ/Часть 164 Джинсы/J03-BLUE-1.JPG")
    _touch(tmp_path, "ДЖИНСЫ/Часть 164 Джинсы/J03-BLUE-2.JPG")
    idx = PhotoIndex(root=tmp_path)
    got = idx.find("J03-BLUE", 4)
    assert [p.parent.name for p in got] == ["Часть 164 Джинсы"] * 2
    assert idx.get("J03-BLUE").rank == 0


def test_index_falls_back_to_archive_when_only_there(tmp_path):
    _touch(tmp_path, "АРХИВ/старые/Q-1.JPG")
    idx = PhotoIndex(root=tmp_path)
    assert idx.get("Q").rank == 1
    assert idx.find("Q", 1)[0].name == "Q-1.JPG"


def test_index_picks_folder_with_most_photos(tmp_path):
    _touch(tmp_path, "К/Часть 1/M-1.JPG")
    _touch(tmp_path, "К/Часть 2/M-1.JPG")
    _touch(tmp_path, "К/Часть 2/M-2.JPG")
    _touch(tmp_path, "К/Часть 2/M-3.JPG")
    idx = PhotoIndex(root=tmp_path)
    assert idx.get("M").folder.name == "Часть 2"


def test_index_collects_deep_nesting_and_extra_extensions(tmp_path):
    _touch(tmp_path, "ОСТАЛЬНОЕ/Часть 1/1/Фотопарсер WB_files/PH-1.webp")
    _touch(tmp_path, "ОСТАЛЬНОЕ/Часть 1/1/Фотопарсер WB_files/PH-2.webp")
    _touch(tmp_path, "К/Глубоко/ещё/вложенно/DD-1.png")
    idx = PhotoIndex(root=tmp_path)
    assert len(idx.find("DD", 4)) == 1
    assert len(idx.find("PH", 4)) == 2          # webp тоже индексируется
    assert idx.stats["articles"] == 2


def test_index_folder_keeps_original_case(tmp_path):
    _touch(tmp_path, "К/Часть 2/M-1.JPG")
    _touch(tmp_path, "К/Часть 2/M-2.JPG")
    idx = PhotoIndex(root=tmp_path)
    assert idx.get("M").folder.name == "Часть 2"
    assert idx.find("M", 1)[0].parent.name == "Часть 2"


def test_index_treats_unnumbered_files_as_separate_articles(tmp_path):
    _touch(tmp_path, "ОБУВЬ/Часть 99 Сапоги/Новая папка/NO8B7861.JPG")
    _touch(tmp_path, "ОБУВЬ/Часть 99 Сапоги/Новая папка/NO8B7862.JPG")
    idx = PhotoIndex(root=tmp_path)
    assert len(idx.find("NO8B7861", 4)) == 1
    assert len(idx.find("NO8B7862", 4)) == 1
    assert idx.stats["articles"] == 2


def test_index_dedupes_same_article_in_one_folder(tmp_path):
    _touch(tmp_path, "К/Часть 1/DUP-1.JPG")
    _touch(tmp_path, "К/Часть 1/DUP-1.JPG")
    idx = PhotoIndex(root=tmp_path)
    assert len(idx.find("DUP", 4)) == 1


def test_index_missing_root_is_empty_but_alive(tmp_path):
    idx = PhotoIndex(root=tmp_path / "нет-такой")
    assert idx.find("ABC", 4) == []
    assert idx.refresh()["root_exists"] is False


# ------------------------------------------------------------------ фолбэк по префиксу

def test_prefix_fallback_drops_variant_tail(tmp_path):
    _touch(tmp_path, "К/Часть 1/WLP-TM130005-1506-1.JPG")
    _touch(tmp_path, "К/Часть 1/WLP-TM130005-1506-2.JPG")
    idx = PhotoIndex(root=tmp_path)
    assert len(idx.find("wlp-tm130005-1506-m2", 4)) == 2
    assert idx.find("wlp-tm130005-1506-m2", 4, allow_prefix=False) == []


def test_prefix_fallback_needs_reasonable_length(tmp_path):
    _touch(tmp_path, "К/AB-1.JPG")
    idx = PhotoIndex(root=tmp_path)
    # «AB-C-D» -> «AB-C» короче MIN_PREFIX_LEN, поэтому не угадываем
    assert idx.find("AB-C-D", 4) == []
    assert MIN_PREFIX_LEN == 6


def test_prefix_fallback_does_not_fire_on_exact_match(tmp_path):
    _touch(tmp_path, "К/AAA-1.JPG")
    _touch(tmp_path, "К/AAA-1-1.JPG")
    idx = PhotoIndex(root=tmp_path)
    assert [p.name for p in idx.find("AAA-1", 4)] == ["AAA-1-1.JPG"]
    assert PREFIX_FALLBACK is True


# ------------------------------------------------------------------ кэш

def test_index_is_cached_until_ttl_expires(tmp_path):
    _touch(tmp_path, "К/Z-1.JPG")
    idx = PhotoIndex(root=tmp_path, ttl=1000.0)
    assert len(idx.find("Z", 1)) == 1
    _touch(tmp_path, "К/Z-2.JPG")
    assert len(idx.find("Z", 4)) == 1        # из кэша — второго файла ещё нет
    assert len(idx.refresh()["articles"] >= 1 and idx.find("Z", 4)) == 2


def test_invalidate_forces_rebuild(tmp_path):
    _touch(tmp_path, "К/Z-1.JPG")
    idx = PhotoIndex(root=tmp_path, ttl=1000.0)
    assert len(idx.find("Z", 4)) == 1
    _touch(tmp_path, "К/Z-2.JPG")
    idx.invalidate()
    assert len(idx.find("Z", 4)) == 2


def test_article_photos_paths_respects_count():
    ap = ArticlePhotos(article="A", folder=Path("."), rank=0,
                       items=[(n, Path(f"A-{n}.JPG")) for n in range(1, 6)])
    assert [p.name for p in ap.paths(3)] == ["A-1.JPG", "A-2.JPG", "A-3.JPG"]
    assert ap.paths(0) == []
    assert len(ap.paths(99)) == 5


def test_articles_list_sorted(tmp_path):
    _touch(tmp_path, "К/b-1.JPG")
    _touch(tmp_path, "К/A-1.JPG")
    idx = PhotoIndex(root=tmp_path)
    assert idx.articles() == ["A", "B"]
