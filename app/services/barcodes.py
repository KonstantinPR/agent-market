# -*- coding: utf-8 -*-
"""Генерация штрихкодов: Code128, EAN-13, Data Matrix (Честный знак).

По мотивам ``finance/app/modules/barcode_module.py``: Code128 и EAN-13 — через
``python-barcode`` (PNG), Data Matrix — через ``pylibdmtx`` + Pillow с белой
подложкой, заголовком «ЧЕСТНЫЙ ЗНАК» над матрицей (шрифт ужимается, чтобы не
выходить за ширину квадрата) и подписью кода под матрицей — 3 строки со
слайсом как в finance (``[0:18]``, ``[18:31]``, ``[32:38]``). Шрифт —
системный Arial, без загрузки из сети. Строки длиннее ``DM_LIMIT`` символов
автоматически рисуются как Data Matrix — 1D-коды физически не вместят
31-символьный код маркировки Честного знака.
"""

from __future__ import annotations

import zipfile
from base64 import b64encode
from functools import lru_cache
from io import BytesIO
from pathlib import Path

#: Допустимые типы штрихкодов (значения в UI и API).
KINDS = ("code128", "ean13", "datamatrix")

#: Строки длиннее этого количества символов всегда рисуются как Data Matrix.
DM_LIMIT = 100

#: Максимум картинок с data-url в ответе предпросмотра (остальные — статус).
MAX_PREVIEW = 300

#: Апскейл модуля Data Matrix: pylibdmtx рисует модуль 5px — мелко для
#: сканирования с экрана и печати. ×2 (NEAREST, чёткие границы) → 10px.
DM_SCALE = 2

#: Подпись кода под матрицей — раскладка как в finance: 3 строки
#: ``[0:18]``, ``[18:31]``, ``[32:38]`` (типовой код ЧЗ = 31 символ).
DM_CAPTION_SLICES = ((0, 18), (18, 31), (32, 38))

#: Шапка над матрицей и кегли шрифта (как в finance: 22 — шапка, 16 — код).
DM_HEADER = "ЧЕСТНЫЙ ЗНАК"
DM_HEADER_FONT = 22
DM_CAPTION_FONT = 16
DM_LINE_STEP = 16

#: Тихая зона вокруг матрицы (≥1 модуля по ISO/ГОСТ).
DM_QUIET = 10

#: Системный шрифт для подписей Data Matrix (Linux-сервер без Arial — default).
_FONT_PATH = Path(r"C:\Windows\Fonts\arial.ttf")


def _check(code: str, kind: str) -> str | None:
    """Дешёвая проверка кода без рендера. Возвращает текст ошибки или None."""
    if not code:
        return "Пустой код"
    if kind == "ean13":
        if not code.isdigit():
            return "EAN-13: только цифры"
        if len(code) not in (12, 13):
            return "EAN-13: нужно 12 или 13 цифр"
    return None


def _resolve_kind(code: str, kind: str) -> str:
    """Автопереключение на Data Matrix для сверхдлинных строк (как в finance)."""
    if kind != "datamatrix" and len(code) > DM_LIMIT:
        return "datamatrix"
    return kind


@lru_cache(maxsize=64)
def _font(size: int):
    """Системный Arial (или шрифт по умолчанию, если недоступен)."""
    from PIL import ImageFont

    try:
        return ImageFont.truetype(str(_FONT_PATH), size)
    except OSError:
        return ImageFont.load_default()


def _fit_font(draw, text: str, max_width: int, base: int, min_size: int = 8):
    """Крупнейший кегль ≤ base, при котором текст влезает в max_width пикселей.

    Возвращает ``(font, text_width)``. Нужно, чтобы шапка «ЧЕСТНЫЙ ЗНАК» и
    строки кода не вылезали за границы квадрата матрицы.
    """
    size = base
    while size > min_size:
        font = _font(size)
        w = draw.textbbox((0, 0), text, font=font)[2]
        if w <= max_width:
            return font, w
        size -= 1
    font = _font(min_size)
    return font, draw.textbbox((0, 0), text, font=font)[2]


def _code128_png(code: str) -> bytes:
    from barcode import Code128
    from barcode.writer import ImageWriter

    rv = BytesIO()
    Code128(code, writer=ImageWriter()).write(rv)
    return rv.getvalue()


def _ean13_check_digit(first12: str) -> str:
    """Контрольная цифра EAN-13: чётные позиции ×1, нечётные ×3 (как у GTIN)."""
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12))
    return str((10 - total % 10) % 10)


def _ean13_png(code: str) -> bytes:
    from barcode.ean import EuropeanArticleNumber13
    from barcode.writer import ImageWriter

    # python-barcode не валидирует 13-ю цифру (молча пересчитывает) — сверяем сами,
    # иначе опечатка в теле кода дала бы тихо неверный штрихкод.
    if len(code) == 13:
        expected = code[:12] + _ean13_check_digit(code[:12])
        if code != expected:
            raise ValueError(f"EAN-13: неверная контрольная цифра (верный код: {expected})")
    # 12 цифр — контрольная считается классом автоматически.
    rv = BytesIO()
    EuropeanArticleNumber13(code, writer=ImageWriter()).write(rv)
    return rv.getvalue()


def _datamatrix_png(code: str) -> bytes:
    from PIL import Image, ImageDraw
    from pylibdmtx.pylibdmtx import encode

    encoded = encode(code.encode("utf-8"))
    dm = Image.frombytes("RGB", size=(encoded.width, encoded.height), data=encoded.pixels)
    # NEAREST: модуль должен оставаться чётким квадратом, без сглаживания.
    dm = dm.resize((dm.width * DM_SCALE, dm.height * DM_SCALE), Image.NEAREST)
    width, height = dm.size

    caption = [code[a:b] for a, b in DM_CAPTION_SLICES]
    pad_x, pad_top = DM_QUIET, 40
    pad_bottom = DM_QUIET + DM_LINE_STEP * len(caption)
    canvas = Image.new("RGB", (width + 2 * pad_x, pad_top + height + pad_bottom), (255, 255, 255))
    canvas.paste(dm, (pad_x, pad_top))

    draw = ImageDraw.Draw(canvas)
    # Шапка и строки кода центрируются по квадрату матрицы и ужимаются,
    # если не влезают по его ширине (как в finance, но без обрезки краёв).
    font_h, w_h = _fit_font(draw, DM_HEADER, width, DM_HEADER_FONT)
    draw.text((pad_x + (width - w_h) // 2, 8), DM_HEADER, font=font_h, fill=(0, 0, 0))
    y = pad_top + height + DM_QUIET  # тихая зона ≥1 модуля и под матрицей
    for line in caption:
        if line:
            font_c, w_c = _fit_font(draw, line, width, DM_CAPTION_FONT)
            draw.text((pad_x + (width - w_c) // 2, y), line, font=font_c, fill=(0, 0, 0))
        y += DM_LINE_STEP

    rv = BytesIO()
    canvas.save(rv, format="PNG")
    return rv.getvalue()


def generate_png(code: str, kind: str = "code128") -> bytes:
    """Один PNG-штрихкод. Кидает ValueError/BarcodeError при неверном коде."""
    code = (code or "").strip()
    err = _check(code, kind)
    if err:
        raise ValueError(err)
    kind = _resolve_kind(code, kind)
    if kind == "code128":
        return _code128_png(code)
    if kind == "ean13":
        return _ean13_png(code)
    if kind == "datamatrix":
        return _datamatrix_png(code)
    raise ValueError(f"Неизвестный тип: {kind}")


def generate_batch(codes: list[str], kind: str = "code128") -> list[dict]:
    """Пакетная генерация: ``[{code, ok, error, png, kind}]`` в порядке кодов.

    Первые ``MAX_PREVIEW`` кодов рендерятся полностью (png); остальным
    выполняется только дешёвая проверка (png=None) — предпросмотр сотен
    тысяч картинок браузер всё равно не покажет.
    """
    out: list[dict] = []
    for code in codes:
        code = (code or "").strip()
        row: dict = {"code": code, "ok": False, "error": None, "png": None, "kind": kind}
        err = _check(code, kind)
        if err:
            row["error"] = err
            out.append(row)
            continue
        row["kind"] = _resolve_kind(code, kind)
        if len(out) < MAX_PREVIEW:
            try:
                row["png"] = generate_png(code, kind)
                row["ok"] = True
            except Exception as exc:  # noqa: BLE001 — ошибка одного кода не роняет батч
                row["error"] = str(exc)
        else:
            row["ok"] = True
        out.append(row)
    return out


def to_data_url(png: bytes) -> str:
    """PNG-байты → ``data:image/png;base64,...`` для <img src> в предпросмотре."""
    return "data:image/png;base64," + b64encode(png).decode("ascii")


def build_zip(codes: list[str], kind: str = "code128") -> BytesIO:
    """ZIP с PNG: ``bar_0001.png``, ``bar_0002.png``… (как в finance).

    Невалидные строки пропускаются; если ни один код не сошёлся — ValueError.
    """
    rows = generate_batch(codes, kind)
    ok_rows = [r for r in rows if r["ok"]]
    if not ok_rows:
        errors = "; ".join(f"{r['code'] or '∅'}: {r['error']}" for r in rows[:5])
        raise ValueError(f"Нет валидных кодов: {errors}")

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, False) as zf:
        for i, row in enumerate(ok_rows, 1):
            png = row["png"] or generate_png(row["code"], row["kind"])
            zf.writestr(f"bar_{i:04d}.png", png)
    buf.seek(0)
    return buf
