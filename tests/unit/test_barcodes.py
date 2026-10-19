# -*- coding: utf-8 -*-
"""Генерация штрихкодов: Code128, EAN-13, Data Matrix."""

import base64
import zipfile

import pytest

from app.services import barcodes as bc

PNG_MAGIC = b"\x89PNG"
GOOD_EAN12 = "460123456789"
GOOD_EAN13 = "4601234567893"  # 12 цифр + верная контрольная


def test_code128_png():
    png = bc.generate_png("ABC-123", "code128")
    assert png.startswith(PNG_MAGIC)


def test_ean13_12_digits_adds_check_digit():
    assert bc.generate_png(GOOD_EAN12, "ean13").startswith(PNG_MAGIC)


def test_ean13_13_digits_with_valid_check_digit():
    assert bc.generate_png(GOOD_EAN13, "ean13").startswith(PNG_MAGIC)


def test_ean13_wrong_check_digit_rejected():
    with pytest.raises(ValueError, match="контрольная цифра"):
        bc.generate_png("4601234567891", "ean13")


def test_ean13_letters_rejected():
    with pytest.raises(ValueError, match="цифры"):
        bc.generate_png("ABC123", "ean13")


def test_ean13_wrong_length_rejected():
    with pytest.raises(ValueError, match="12 или 13"):
        bc.generate_png("46012345678", "ean13")


def test_empty_code_rejected():
    with pytest.raises(ValueError, match="Пустой"):
        bc.generate_png("   ", "code128")


def test_unknown_kind_rejected():
    with pytest.raises(ValueError, match="Неизвестный тип"):
        bc.generate_png("ABC", "qr")


def test_datamatrix_png():
    pytest.importorskip("pylibdmtx.pylibdmtx")
    png = bc.generate_png("0104600123456789012310ABCDEF012345", "datamatrix")
    assert png.startswith(PNG_MAGIC)


def test_long_code_auto_switches_to_datamatrix():
    pytest.importorskip("pylibdmtx.pylibdmtx")
    long_code = "01" + "46" + "12345678901234" + "21" + "A" * 90  # >100 символов
    assert len(long_code) > bc.DM_LIMIT
    rows = bc.generate_batch([long_code], "code128")
    assert rows[0]["kind"] == "datamatrix"
    assert rows[0]["ok"] and rows[0]["png"] is not None


def test_batch_reports_errors_per_row():
    rows = bc.generate_batch(["", "ABC", GOOD_EAN12], "ean13")
    assert [r["ok"] for r in rows] == [False, False, True]
    assert rows[0]["error"] == "Пустой код"
    assert "цифры" in rows[1]["error"]


def test_batch_renders_only_first_max_preview(monkeypatch):
    monkeypatch.setattr(bc, "MAX_PREVIEW", 2)
    codes = [f"{GOOD_EAN12[:-3]}{i:03d}" for i in range(4)]
    rows = bc.generate_batch(codes, "ean13")
    assert [r["png"] is not None for r in rows] == [True, True, False, False]
    assert all(r["ok"] for r in rows)


def test_build_zip_names_and_content():
    buf = bc.build_zip(["A1", "A2"], "code128")
    with zipfile.ZipFile(buf) as zf:
        assert zf.namelist() == ["bar_0001.png", "bar_0002.png"]
        assert zf.read("bar_0001.png").startswith(PNG_MAGIC)


def test_build_zip_skips_invalid_keeps_valid():
    buf = bc.build_zip(["", "B7"], "code128")
    with zipfile.ZipFile(buf) as zf:
        assert zf.namelist() == ["bar_0001.png"]


def test_build_zip_all_invalid_raises():
    with pytest.raises(ValueError, match="Нет валидных"):
        bc.build_zip(["", "x"], "ean13")


def test_to_data_url_prefix():
    expected = "data:image/png;base64," + base64.b64encode(b"\x89PNG...").decode()
    assert bc.to_data_url(b"\x89PNG...") == expected


# ------------------------------------------------------------------ T-49: регрессия (пользовательский ввод)

#: Два реальных кода маркировки из баг-репорта: второй содержит запятую внутри.
GS1_LINE1 = "0104670630636090215LcFWqhXYe:XY91EE1292nJjYsEmycZ+vKSpUiOOz+UOVn9cFEnjk8uO5f4Tx1ho="
GS1_LINE2 = "0104670630636090215gE0XLM,YF-Wg91EE1292VuREv8Zh8IFEiWr6bTdtw82pE7i/E+j+8BKvhpehplQ="


def test_gs1_datamatrix_comma_inside_is_single_code():
    """Регрессия: запятая внутри кода не должна делить его на части."""
    pytest.importorskip("pylibdmtx.pylibdmtx")
    rows = bc.generate_batch([GS1_LINE1, GS1_LINE2], "datamatrix")
    assert len(rows) == 2
    assert all(r["ok"] for r in rows)
    assert rows[1]["code"] == GS1_LINE2  # запятая сохранена


def test_gs1_datamatrix_same_length_same_size():
    """Одинаковая длина кодов → одинаковый размер изображения (модуль стабилен)."""
    from io import BytesIO

    from PIL import Image

    pytest.importorskip("pylibdmtx.pylibdmtx")
    rows = bc.generate_batch([GS1_LINE1, GS1_LINE2], "datamatrix")
    sizes = {Image.open(BytesIO(r["png"])).size for r in rows}
    assert len(sizes) == 1, f"размеры изображений разные: {sizes}"


def test_datamatrix_quiet_zone_and_scale():
    """Матрица масштабируется ×DM_SCALE, тихая зона ≥1 модуля со всех сторон."""
    from io import BytesIO

    from PIL import Image

    from pylibdmtx.pylibdmtx import encode

    code = "0104670630636090215LcFWqhXYe"
    png = bc.generate_png(code, "datamatrix")
    im = Image.open(BytesIO(png))
    native = encode(code.encode("utf-8"))
    assert im.size[0] == native.width * bc.DM_SCALE + 2 * bc.DM_QUIET
    # верх тихой зоны: 40px шапка «ЧЕСТНЫЙ ЗНАК» ≥ DM_QUIET
    assert 40 >= bc.DM_QUIET


def test_datamatrix_caption_finance_split():
    """Подпись под матрицей — 3 строки со слайсом как в finance ([0:18]/[18:31]/[32:38])."""
    pytest.importorskip("pylibdmtx.pylibdmtx")
    code = "0104670630636090215LcFWqhXYe:XY91EE1292nJjYsEmycZ"
    assert [code[a:b] for a, b in bc.DM_CAPTION_SLICES] == [
        code[0:18],
        code[18:31],
        code[32:38],
    ]
    rows = bc.generate_batch([code], "datamatrix")
    assert rows[0]["ok"]


def test_datamatrix_long_code_renders():
    """Длинный код (83 символа) рендерится — лишние символы просто не попадают в 3 строки."""
    pytest.importorskip("pylibdmtx.pylibdmtx")
    rows = bc.generate_batch([GS1_LINE1], "datamatrix")
    assert rows[0]["ok"] and rows[0]["png"] is not None
