"""Карточки «Потребность в товаре» в PDF (reportlab).

Раскладка карточки (по ТЗ): фотографии товара, затем артикул, затем размеры с
количеством, затем итог по товару; в конце документа — итог по всей потребности.

Геометрия сетки фотографий — обобщение макета «1 крупное слева + 3 справа»:
первое фото занимает всю высоту блока, остальные делят правую колонку поровну.
Сетка одна на карточку: одна крупная + N-1 мелких в правой колонке.
При N > 4 фотографии разбиваются на строки по 4, последняя строка — остаток.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, Table, TableStyle

# -- страница ---------------------------------------------------------------

PAGE_W, PAGE_H = A4
MARGIN = 11 * mm
CONTENT_W = PAGE_W - 2 * MARGIN
TOP = PAGE_H - MARGIN
BOTTOM = MARGIN + 10 * mm          # выше — только футер
FOOTER_TEXT = "Потребность в товаре"

# -- сетка фотографий -------------------------------------------------------

PHOTO_GAP = 2.4 * mm
PORTRAIT = 0.75                   # типичное соотношение фото библиотеки (2250x3000)
SMALL_HEADROOM = 1.10             # запас по ширине правой колонки
ROW_H_MAX = 84 * mm                # высота строки из 4 фото
ROW_H_MIN = 40 * mm                # пол для одной строки из 1 фото
PER_ROW = 4                        # сколько фото в строке

#: сколько троек «размер / наличие / дослать» в строке. Один: подписи размеров
#: WB бывают до 13 символов («small size», «58/182»), в узкой колонке они
#: переносятся и товар на складе становится нечитаемым.
SIZE_GROUPS = 1
SIZE_COL_W = (0.42, 0.26, 0.32)   # доли ширины: размер / наличие / дослать

# -- цвета ------------------------------------------------------------------

C_TEXT = colors.HexColor("#111111")
C_MUTED = colors.HexColor("#5A5A5A")
C_LINE = colors.HexColor("#C9C9C9")
C_ZEBRA = colors.HexColor("#F4F4F4")
C_TOTAL = colors.HexColor("#000000")

_FONTS: tuple[str, str] | None = None

_REGULAR_CANDIDATES = (
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
    r"C:\Windows\Fonts\tahoma.ttf",
)
_BOLD_CANDIDATES = (
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\segoeuiz.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\calibrib.ttf",
    r"C:\Windows\Fonts\tahomabd.ttf",
)
_BUNDLED_REGULAR = ("Vera.ttf",)
_BUNDLED_BOLD = ("VeraBd.ttf",)


def _try_register(name: str, candidates: tuple[str, ...], bundled: tuple[str, ...]) -> str | None:
    for p in candidates:
        if os.path.exists(p):
            try:
                pdfmetrics.registerFont(TTFont(name, p))
                return name
            except Exception:
                pass
    import reportlab

    fdir = Path(reportlab.__file__).parent / "fonts"
    for fn in bundled:
        p = fdir / fn
        if p.exists():
            try:
                pdfmetrics.registerFont(TTFont(name, str(p)))
                return name
            except Exception:
                pass
    return None


def ensure_fonts() -> tuple[str, str]:
    """Зарегистрировать шрифт с кириллицей. ``Helvetica`` — последний резерв."""
    global _FONTS
    if _FONTS is not None:
        return _FONTS
    bold = _try_register("DemandSans-Bold", _BOLD_CANDIDATES, _BUNDLED_BOLD)
    reg = _try_register("DemandSans", _REGULAR_CANDIDATES, _BUNDLED_REGULAR)
    if bold and reg:
        pdfmetrics.registerFontFamily(
            "DemandSans", normal="DemandSans", bold="DemandSans-Bold",
            italic="DemandSans", boldItalic="DemandSans-Bold",
        )
        _FONTS = (reg, bold)
    elif reg:
        _FONTS = (reg, reg)
    else:
        _FONTS = ("Helvetica", "Helvetica-Bold")
    return _FONTS


def _styles() -> dict:
    reg, bold = ensure_fonts()
    return {
        "reg": reg,
        "bold": bold,
        "article": ParagraphStyle(
            "article", fontName=bold, fontSize=12.5, leading=15,
            textColor=C_TEXT, alignment=TA_LEFT,
        ),
        "name": ParagraphStyle(
            "name", fontName=reg, fontSize=8, leading=10,
            textColor=C_MUTED, alignment=TA_LEFT,
        ),
        "attr": ParagraphStyle(
            "attr", fontName=reg, fontSize=7.2, leading=9,
            textColor=C_TEXT, alignment=TA_LEFT,
        ),
        "size": ParagraphStyle(
            "size", fontName=reg, fontSize=9.5, leading=12,
            textColor=C_TEXT, alignment=TA_LEFT,
        ),
        "sizeth": ParagraphStyle(
            "sizeth", fontName=bold, fontSize=7, leading=9,
            textColor=C_MUTED, alignment=TA_LEFT,
        ),
        "sizel": ParagraphStyle(
            "sizel", fontName=reg, fontSize=9.5, leading=12,
            textColor=C_MUTED, alignment=TA_RIGHT,
        ),
        "sizeb": ParagraphStyle(
            "sizeb", fontName=bold, fontSize=9.5, leading=12,
            textColor=C_TEXT, alignment=TA_RIGHT,
        ),
        "total": ParagraphStyle(
            "total", fontName=bold, fontSize=10.5, leading=13,
            textColor=C_TOTAL, alignment=TA_RIGHT,
        ),
        "foot": ParagraphStyle(
            "foot", fontName=reg, fontSize=7.5, leading=9,
            textColor=C_MUTED, alignment=TA_LEFT,
        ),
        "grand": ParagraphStyle(
            "grand", fontName=bold, fontSize=15, leading=19,
            textColor=C_TEXT, alignment=TA_LEFT,
        ),
        "grandsub": ParagraphStyle(
            "grandsub", fontName=reg, fontSize=9, leading=12,
            textColor=C_MUTED, alignment=TA_LEFT,
        ),
    }


# -- форматирование ---------------------------------------------------------

def _int0(v) -> int:
    """Мягкое приведение к неотрицательному целому."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def fmt_num(v) -> str:
    """Число в русской типографике: ``1 234,5``."""
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    if isinstance(v, float):
        if abs(v - round(v)) < 1e-9:
            return f"{int(round(v)):,}".replace(",", " ")
        return f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return str(v)


def _esc(v) -> str:
    return escape("" if v is None else str(v))


def row_height(n_in_row: int) -> float:
    if n_in_row <= 0:
        return 0.0
    if n_in_row >= PER_ROW:
        return ROW_H_MAX
    return max(ROW_H_MIN, ROW_H_MAX * n_in_row / PER_ROW)


def photo_rows(n: int) -> list[list[int]]:
    """Разбить ``n`` фото на строки: ``5 -> [[0,1,2,3],[4]]``."""
    if n <= 0:
        return []
    out = []
    i = 0
    while i < n:
        out.append(list(range(i, min(i + PER_ROW, n))))
        i += PER_ROW
    return out


# -- сборка -----------------------------------------------------------------

class DemandPdf:
    """Постраничная сборка карточек. Пул потоков для миниатюр — снаружи."""

    def __init__(
        self,
        canvas,
        cols: list[str] | None = None,
        labels: dict[str, str] | None = None,
        with_photos: bool = True,
        photo_count: int = 4,
        subtitle: str = "",
    ) -> None:
        self.c = canvas
        self.st = _styles()
        self.cols = [c for c in (cols or []) if c]
        self.labels = labels or {}
        self.with_photos = with_photos and photo_count > 0
        self.photo_count = max(0, int(photo_count))
        self.subtitle = subtitle
        self.y = TOP
        self.page_no = 0
        self.attrs = [
            c for c in self.cols if c not in ("article", "size", "name")
        ]

    # -- инфраструктура страниц ----------------------------------------

    def new_page(self) -> None:
        # Футер рисуем на закрываемой странице, чтобы он был на каждой.
        if self.y < TOP - 0.5:
            self.draw_footer()
        self.c.showPage()
        self.page_no += 1
        self.y = TOP

    def need(self, h: float) -> None:
        """Перенести на новую страницу, если не влезает ``h``."""
        if self.y - h < BOTTOM:
            self.new_page()

    def draw_footer(self) -> None:
        parts = [FOOTER_TEXT]
        if self.subtitle:
            parts.append(self.subtitle)
        parts.append(f"стр. {self.page_no + 1}")
        self.c.saveState()
        self.c.setFont(self.st["reg"], 7.5)
        self.c.setFillColor(C_MUTED)
        self.c.drawString(MARGIN, MARGIN + 3 * mm, " · ".join(parts))
        self.c.restoreState()

    # -- блоки ----------------------------------------------------------

    def _photo_grid_height(self, count: int) -> float:
        if not self.with_photos or count <= 0:
            return 0.0
        rows = photo_rows(min(count, self.photo_count))
        return sum(row_height(len(r)) for r in rows) + PHOTO_GAP * (len(rows) - 1)

    def _draw_photos(self, x: float, y_top: float, count: int, thumbs) -> float:
        """Отрисовать сетку фото. Возвращает фактическую высоту блока."""
        if not self.with_photos or count <= 0:
            return 0.0
        data = [t for t in thumbs[: self.photo_count] if isinstance(t, bytes)]
        if not data:
            return 0.0

        y = y_top
        idx = 0
        while idx < len(data):
            take = min(PER_ROW, len(data) - idx)
            h = row_height(take)
            self._photo_row(x, y, h, data[idx:idx + take])
            idx += take
            y -= h
            if idx < len(data):
                y -= PHOTO_GAP
        return y_top - y

    def _photo_row(self, x: float, y_top: float, h: float, blobs: list[bytes]) -> float:
        """Одна строка сетки: крупное слева + остальные в правой колонке.

        Ширины считаем от типичной вертикальной пропорции, чтобы полоса фото
        не растягивалась на всю страницу пустотой справа. Возвращает ширину полосы.
        """
        n = len(blobs)
        big_w = PORTRAIT * h
        if n == 1:
            self._place_photo(blobs[0], x, y_top - h, big_w, h)
            return big_w
        # Справа n-1 ячеек с (n-2) промежутками — колонка должна занять всю h.
        n_small = n - 1
        ch = (h - PHOTO_GAP * (n_small - 1)) / n_small
        small_w = PORTRAIT * ch * SMALL_HEADROOM
        self._place_photo(blobs[0], x, y_top - h, big_w, h)
        for i in range(1, n):
            # Промежуток уже учтён в ch, но в позициях его надо применить явно.
            cell_bottom = y_top - i * ch - (i - 1) * PHOTO_GAP
            self._place_photo(
                blobs[i], x + big_w + PHOTO_GAP, cell_bottom, small_w, ch
            )
        return big_w + PHOTO_GAP + small_w

    def _place_photo(self, blob: bytes, x: float, y: float, w: float, h: float) -> None:
        """Вписать фото в ячейку с сохранением пропорций, по центру."""
        try:
            from reportlab.lib.utils import ImageReader

            iw, ih = ImageReader(io.BytesIO(blob)).getSize()
        except Exception:
            return
        if iw <= 0 or ih <= 0:
            return
        s = min(w / iw, h / ih)
        dw, dh = iw * s, ih * s
        dx, dy = x + (w - dw) / 2, y + (h - dh) / 2
        self.c.saveState()
        try:
            self.c.drawImage(ImageReader(io.BytesIO(blob)), dx, dy, dw, dh,
                             mask="auto")
        except Exception:
            self.c.setFillColor(C_ZEBRA)
            self.c.rect(x, y, w, h, fill=1, stroke=0)
        self.c.restoreState()

    def _para(self, p: Paragraph) -> float:
        w, h = p.wrap(CONTENT_W, 1e6)
        p.drawOn(self.c, MARGIN, self.y - h)
        self.y -= h
        return h

    # -- карточка -------------------------------------------------------

    def add_card(self, row: dict, sizes: list[dict], thumbs: list) -> int:
        """Напечатать карточку товара. Возвращает итог по товару (шт)."""
        photo_h = self._photo_grid_height(len(thumbs) if self.with_photos else 0)
        article_p = Paragraph(_esc(row.get("article")), self.st["article"])
        aw, ah = article_p.wrap(CONTENT_W, 1e6)

        name_p = None
        if "name" in self.cols and (row.get("name") or "").strip():
            name_p = Paragraph(_esc(row.get("name")), self.st["name"])
            nh = name_p.wrap(CONTENT_W, 1e6)[1]
        else:
            nh = 0.0

        attr_tbl, attr_h = self._attrs_table(row)
        size_tbl, size_h, total = self._sizes_table(sizes)
        total_p = Paragraph(
            f"Итого дослать на WB: <b>{fmt_num(total)}</b> шт", self.st["total"]
        )
        tw, th = total_p.wrap(CONTENT_W, 1e6)

        block_h = (
            photo_h
            + (PHOTO_GAP if photo_h else 0)
            + ah + 1.5 * mm
            + nh
            + attr_h
            + size_h
            + 2 * mm
            + th
            + 5 * mm
        )
        self.need(block_h)

        top = self.y
        if self.with_photos and thumbs:
            used = self._draw_photos(MARGIN, top, len(thumbs), thumbs)
            if used:
                self.y -= used + PHOTO_GAP

        self._para(article_p)
        self.y -= 1.5 * mm
        if name_p is not None:
            self._para(name_p)
        if attr_tbl is not None:
            attr_tbl.drawOn(self.c, MARGIN, self.y - attr_h)
            self.y -= attr_h
        if size_tbl is not None:
            size_tbl.drawOn(self.c, MARGIN, self.y - size_h)
            self.y -= size_h
        self.y -= 2 * mm
        self._para(total_p)

        self.y -= 5 * mm
        self.c.setStrokeColor(C_LINE)
        self.c.setLineWidth(0.4)
        self.c.line(MARGIN, self.y + 2 * mm, PAGE_W - MARGIN, self.y + 2 * mm)
        self.y -= 2 * mm
        return total

    def _attrs_table(self, row: dict):
        """Галочки колонок -> компактная сетка «подпись: значение»."""
        if not self.attrs:
            return None, 0.0
        cells = []
        for key in self.attrs:
            label = self.labels.get(key, key)
            val = fmt_num(row.get(key))
            cells.append(
                Paragraph(
                    f'<font color="#5A5A5A">{_esc(label)}:</font> '
                    f'<b>{_esc(val)}</b>',
                    self.st["attr"],
                )
            )
        if not cells:
            return None, 0.0
        per = 3
        rows = [cells[i:i + per] for i in range(0, len(cells), per)]
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        tbl = Table(rows, colWidths=[CONTENT_W / width] * width, hAlign="LEFT")
        tbl.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 1.2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.25, C_LINE),
                ]
            )
        )
        _, h = tbl.wrapOn(self.c, CONTENT_W, 1e6)
        return tbl, h + 2 * mm

    def _sizes_table(self, sizes: list[dict]):
        """Все размеры карточки: наличие на WB и сколько дослать.

        Итог = сумма «дослать» по размерам, он же уходит в «Итого» карточки.
        Столбцы идут тройками (размер / наличие / дослать) и заполняются сверху
        вниз: товар с 20 размерами занимает 7 строк вместо двадцати.
        """
        items = []
        for s in sizes:
            items.append((
                str(s.get("size") or "—"),
                max(0, _int0(s.get("avail"))),
                max(0, _int0(s.get("to_sort"))),
            ))
        if not items:
            return None, 0.0, 0

        groups = min(SIZE_GROUPS, len(items))
        n_rows = -(-len(items) // groups)
        columns: list[list] = [[] for _ in range(groups)]
        for i, it in enumerate(items):
            columns[i // n_rows].append(it)

        head: list[str] = []
        for _ in range(groups):
            head += ["Размер", "Наличие", "Дослать"]

        data = [[Paragraph(h, self.st["sizeth"]) for h in head]]
        for r in range(n_rows):
            row: list = []
            for col in columns:
                if r < len(col):
                    sz, avail, to_sort = col[r]
                    row.append(Paragraph(_esc(sz), self.st["size"]))
                    row.append(Paragraph(fmt_num(avail), self.st["sizel"]))
                    row.append(Paragraph(
                        fmt_num(to_sort) if to_sort > 0 else "—",
                        self.st["sizeb"] if to_sort > 0 else self.st["sizel"],
                    ))
                else:
                    row += ["", "", ""]
            data.append(row)

        w = CONTENT_W / (groups * 3)
        col_w: list[float] = []
        for _ in range(groups):
            col_w += [w * SIZE_COL_W[0], w * SIZE_COL_W[1], w * SIZE_COL_W[2]]
        tbl = Table(data, colWidths=col_w, hAlign="LEFT", repeatRows=1)
        style = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("LINEBELOW", (0, 1), (-1, -2), 0.25, C_ZEBRA),
        ]
        for g in range(1, groups):
            style.append(("LINEBEFORE", (g * 3, 0), (g * 3, -1), 0.25, C_LINE))
        tbl.setStyle(TableStyle(style))
        _, h = tbl.wrapOn(self.c, CONTENT_W, 1e6)
        return tbl, h + 1.5 * mm, sum(it[2] for it in items)

    # -- финал ----------------------------------------------------------

    def add_grand_total(self, total: int, cards: int, with_photos: bool, hits: int) -> None:
        self.need(46 * mm)
        self.y -= 8 * mm
        p = Paragraph("ИТОГО ДОСЛАТЬ НА WB", self.st["grand"])
        self._para(p)
        p2 = Paragraph(
            f'<b>{fmt_num(total)}</b> шт &nbsp;&nbsp;·&nbsp;&nbsp; товаров: {fmt_num(cards)}',
            self.st["total"],
        )
        self._para(p2)
        if with_photos:
            p3 = Paragraph(
                f"с фотографиями: {fmt_num(hits)} из {fmt_num(cards)}",
                self.st["grandsub"],
            )
            self._para(p3)


def build_demand_pdf(
    cards: list[tuple[dict, list[dict], list[bytes]]],
    cols: list[str] | None = None,
    labels: dict[str, str] | None = None,
    with_photos: bool = True,
    photo_count: int = 4,
    subtitle: str = "",
) -> bytes:
    """Собрать PDF. ``cards`` = [(строка потребности, размеры, миниатюры), ...]."""
    from reportlab.pdfgen import canvas as rl_canvas

    ensure_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Потребность в товаре")
    c.setAuthor("Agent Market")

    doc = DemandPdf(
        c, cols=cols, labels=labels, with_photos=with_photos,
        photo_count=photo_count, subtitle=subtitle,
    )
    grand = 0
    hits = 0
    for row, sizes, thumbs in cards:
        grand += doc.add_card(row, sizes, thumbs)
        if thumbs:
            hits += 1
    doc.add_grand_total(grand, len(cards), with_photos, hits)
    doc.draw_footer()
    c.save()
    return buf.getvalue()