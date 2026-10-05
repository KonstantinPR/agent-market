"""Тесты PDF-карточек потребности (app/services/pdf_demand.py).

Кириллицу и число вставленных картинок проверяем через canvas без сжатия
(pageCompression=0): тогда текст и словарь изображений остаются в байтах
читаемыми и не нужен сторонний парсер PDF.
"""
import io
import re

import pytest
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as rl_canvas

from app.services.pdf_demand import (
    MARGIN,
    PER_ROW,
    DemandPdf,
    build_demand_pdf,
    ensure_fonts,
    fmt_num,
    photo_rows,
    row_height,
)

JPEG_MARK = re.compile(rb"/Subtype\s*/Image")
CYR_RE = re.compile(rb"[\xd0-\xd1][\x80-\xbf]")


def jpeg(seed: int = 0, w: int = 120, h: int = 160) -> bytes:
    """Маленький JPEG. Каждый seed обязан давать уникальные байты, иначе
    reportlab переиспользует XObject и счётчики картинок в тестах врут."""
    im = Image.new("RGB", (w, h), ((seed * 61) % 256, (seed * 97) % 256, (seed * 151) % 256))
    dr = ImageDraw.Draw(im)
    for y in range(0, h, 16):
        for x in range(0, w, 16):
            if (x // 16 + y // 16 + seed) % 2 == 0:
                dr.rectangle([x, y, x + 15, y + 15], fill=(250, 250, 250))
    dr.rectangle([seed % w, 0, w - 1, (seed * 3) % h], fill=(0, 0, 0))
    b = io.BytesIO()
    im.save(b, format="JPEG", quality=80)
    return b.getvalue()


def test_jpeg_helper_produces_unique_bytes():
    """Страховка: счётчики картинок в тестах ниже полагаются на уникальность."""
    blobs = [jpeg(i) for i in range(24)]
    assert len(set(blobs)) == 24


def sizes(*pairs, avail=None):
    """Строки размеров для вёрстки: (размер, дослать) либо avail=остаток.

    По умолчанию остаток нулевой — в тестах важнее сумма «дослать».
    """
    a = 0 if avail is None else avail
    return [{"size": s, "avail": a, "to_sort": d} for s, d in pairs]


def render(cards, **kw) -> bytes:
    """Собрать PDF без сжатия потока — чтобы можно было читать содержимое."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4, pageCompression=0)
    c.setTitle("Потребность в товаре")
    doc = DemandPdf(c, **kw)
    grand = 0
    hits = 0
    for row, sz, thumbs in cards:
        grand += doc.add_card(row, sz, thumbs)
        if thumbs:
            hits += 1
    doc.add_grand_total(grand, len(cards), kw.get("with_photos", True), hits)
    doc.draw_footer()
    c.save()
    return buf.getvalue()


# ------------------------------------------------------------------ утилиты

def test_fmt_num_uses_russian_separators():
    assert fmt_num(1234) == "1 234"
    assert fmt_num(4.567) == "4,57"
    assert fmt_num(5.0) == "5"
    assert fmt_num(None) == "—"
    assert fmt_num("") == "—"
    assert fmt_num(0) == "0"


def test_photo_rows_splits_by_four():
    assert photo_rows(0) == []
    assert photo_rows(3) == [[0, 1, 2]]
    assert photo_rows(5) == [[0, 1, 2, 3], [4]]
    assert photo_rows(9) == [[0, 1, 2, 3], [4, 5, 6, 7], [8]]


def test_row_height_full_for_four_and_floor_for_one():
    assert row_height(PER_ROW) == pytest.approx(84 * 2.8346457, rel=1e-3)
    assert row_height(1) == pytest.approx(40 * 2.8346457, rel=1e-3)
    assert row_height(3) > row_height(2) > row_height(1)
    assert row_height(0) == 0


def test_fonts_registered_once():
    a = ensure_fonts()
    assert a == ensure_fonts()
    assert a[0] and a[1]


# ------------------------------------------------------------------ содержимое

def test_card_prints_article_sizes_and_total():
    cards = [({"article": "АРТ-001", "name": "Платье", "need_buy": 123},
             sizes(("42", 15), ("44", 8), ("46", 0)), [])]
    data = render(cards, cols=["name", "need_buy"], labels={"need_buy": "Купить у поставщика"})
    assert data.startswith(b"%PDF")
    assert data.rstrip().endswith(b"%%EOF")
    assert CYR_RE.search(data), "кириллица должна попадать в PDF как текст"
    assert b"23" in data                        # 15 + 8


def test_every_size_is_listed_even_without_topup():
    """Размер без добора всё равно печатается: так видно, что он есть на карточке."""
    cards = [({"article": "A-1"}, sizes(("XL-LARGE-46", 0), ("XL-LARGE-42", 3)), [])]
    data = render(cards, cols=[])
    assert b"XL-LARGE-42" in data
    assert b"XL-LARGE-46" in data


def test_sizes_are_one_triple_per_row():
    """Подстрока размера должна попадать в PDF целиком — иначе подпись переносится."""
    many = sizes(*[(str(n), n % 3) for n in range(20, 40)])
    doc = DemandPdf(rl_canvas.Canvas(io.BytesIO(), pagesize=A4))
    tbl, _height, _total = doc._sizes_table(many)
    # 20 размеров: строка заголовка + 20 строк, по 3 колонки
    assert len(tbl._cellvalues) == 21
    assert len(tbl._cellvalues[0]) == 3
    data = render([({"article": "A-1"}, many, [])], cols=[])
    for n in range(20, 40):
        assert str(n).encode() in data, f"размер {n} должен печататься"


def cell_texts(tbl) -> list:
    """Тексты ячеек. ReportLab хранит их в _ExpandedCellTuple-обёртках."""
    out = []
    for row in tbl._cellvalues:
        for c in row:
            obj = c[0] if isinstance(c, tuple) else c
            out.append(getattr(obj, "text", "") or "")
    return out


def test_zero_topup_is_dash_and_positive_is_number():
    tbl_sizes = [{"size": "42", "avail": 3, "to_sort": 0},
                 {"size": "44", "avail": 0, "to_sort": 7}]
    doc = DemandPdf(rl_canvas.Canvas(io.BytesIO(), pagesize=A4))
    tbl, _h, total = doc._sizes_table(tbl_sizes)
    assert total == 7
    texts = cell_texts(tbl)
    assert "\u2014" in texts, "нулевой добор печатается прочерком"
    assert "7" in texts
    assert "3" in texts, "остаток показывается, даже когда добавлять нечего"


def test_total_is_sum_of_printed_sizes():
    cards = [({"article": "A-1"}, sizes(("42", 15), ("44", 8)), [])]
    doc = DemandPdf(rl_canvas.Canvas(io.BytesIO(), pagesize=A4))
    total = doc.add_card(cards[0][0], cards[0][1], [])
    assert total == 23


def test_special_chars_are_escaped():
    cards = [({"article": "A&B<C>\"1\"", "name": "Платье & <b>жирное</b>"},
             sizes(("42", 1)), [])]
    data = render(cards, cols=["name"])
    assert data.startswith(b"%PDF")             # не упало на разметке
    assert b"<b>" not in data.split(b"stream")[0]


def test_cards_break_across_pages():
    cards = [
        ({"article": f"A-{i}"}, sizes(("42", 5)), [jpeg(i)] * 4)
        for i in range(1, 30)
    ]
    data = render(cards, cols=[], with_photos=True, photo_count=4)
    pages = data.count(b"/Type /Page\n") - data.count(b"/Type /Pages")
    assert pages >= 10


# ------------------------------------------------------------------ фото

def test_no_photos_when_disabled():
    cards = [({"article": "A-1"}, sizes(("42", 5)), [jpeg()] * 4)]
    data = render(cards, cols=[], with_photos=False, photo_count=4)
    assert not JPEG_MARK.search(data)


def test_photo_count_caps_images_per_card():
    for want in (1, 2, 3, 4):
        cards = [({"article": "A-1"}, sizes(("42", 5)), [jpeg(i) for i in range(9)])]
        data = render(cards, cols=[], with_photos=True, photo_count=want)
        assert len(JPEG_MARK.findall(data)) == want


def test_more_than_four_photos_stack_second_row():
    cards = [({"article": "A-1"}, sizes(("42", 5)), [jpeg(i) for i in range(6)])]
    data = render(cards, cols=[], with_photos=True, photo_count=6)
    assert len(JPEG_MARK.findall(data)) == 6


def test_unreadable_photo_bytes_do_not_break_card():
    cards = [({"article": "A-1"}, sizes(("42", 5)), [b"\x00\x01\x02\x03"])]
    data = render(cards, cols=[], with_photos=True, photo_count=2)
    assert data.startswith(b"%PDF")


# ------------------------------------------------------------------ обёртка

def test_build_demand_pdf_returns_valid_document():
    cards = [
        ({"article": f"A-{i}", "name": "Платье", "need_buy": 5},
         sizes(("42", 3)), [jpeg(i * 10 + k) for k in range(4)])
        for i in range(3)
    ]
    data = build_demand_pdf(cards, cols=["name", "need_buy"],
                            labels={"need_buy": "Купить у поставщика"},
                            with_photos=True, photo_count=4,
                            subtitle="01.09.2026 — 30.09.2026")
    assert data.startswith(b"%PDF")
    assert data.rstrip().endswith(b"%%EOF")
    # словари XObject не сжимаются, поэтому 12 уникальных картинок видны всегда
    assert len(JPEG_MARK.findall(data)) == 12
    assert b"/FlateDecode" in data               # поток страницы сжат


def test_build_demand_pdf_empty_cards_still_has_grand_total():
    data = build_demand_pdf([], cols=[], with_photos=False)
    assert data.startswith(b"%PDF")
    assert CYR_RE.search(data)


def test_build_demand_pdf_compresses_the_same_document():
    """Сжатый поток должен быть заметно меньше распакованного."""
    def mk():
        return [
            ({"article": f"A-{i}"}, sizes(("42", 3)), [jpeg(i * 10 + k) for k in range(4)])
            for i in range(6)
        ]
    packed = build_demand_pdf(mk(), cols=[], with_photos=True, photo_count=4)
    plain = render(mk(), cols=[], with_photos=True, photo_count=4)
    assert len(packed) < len(plain)
    assert len(JPEG_MARK.findall(plain)) == 24   # в распакованном видны все 24


def test_margin_respected():
    assert MARGIN > 0
