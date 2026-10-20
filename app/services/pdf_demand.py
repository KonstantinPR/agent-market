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

# -- мобильный режим («Для поставщика мобайл») ------------------------------
# Под смартфон: две фотографии в ряд во всю ширину страницы, крупнее шрифты,
# крупнее фотоблок. Формат страницы остаётся A4 — листать удобно, а пайплайн
# (футер, колонки размеров) не меняется.
MOBILE_PER_ROW = 2                 # сколько фото в строке
MOBILE_ROW_H_MAX = 120 * mm        # высота строки из 2 фото — фото почти в пол-страницы
MOBILE_ROW_H_MIN = 70 * mm         # пол для одной строки из 1 фото
MOBILE_PHOTO_CAP = 2               # максимум фото на карточку = одна мобильная строка

#: сколько троек «размер / наличие / дослать» в строке. Один: подписи размеров
#: WB бывают до 13 символов («small size», «58/182»), в узкой колонке они
#: переносятся и товар на складе становится нечитаемым.
SIZE_GROUPS = 1
SIZE_COL_W = (0.42, 0.26, 0.32)   # доли ширины: размер / наличие / дослать
#: Режим «для поставщика»: две колонки (размер / количество), без наличия.
SUPPLIER_COL_W = (0.6, 0.4)

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


def _styles(mobile: bool = False) -> dict:
    """Стили текста. ``mobile=True`` — крупнее шрифты под чтение с телефона."""
    reg, bold = ensure_fonts()
    if mobile:
        # Единственная колонка нормальной ширины: можно позволить крупный кегль.
        fs = {
            "article": (17, 21), "name": (11, 14), "attr": (10, 13),
            "size": (13, 16), "sizeth": (10, 13), "total": (14.5, 18),
            "foot": (9, 11), "grand": (19, 24), "grandsub": (12, 15),
        }
    else:
        fs = {
            "article": (12.5, 15), "name": (8, 10), "attr": (7.2, 9),
            "size": (9.5, 12), "sizeth": (7, 9), "total": (10.5, 13),
            "foot": (7.5, 9), "grand": (15, 19), "grandsub": (9, 12),
        }
    return {
        "reg": reg,
        "bold": bold,
        "article": ParagraphStyle(
            "article", fontName=bold, fontSize=fs["article"][0],
            leading=fs["article"][1], textColor=C_TEXT, alignment=TA_LEFT,
        ),
        "name": ParagraphStyle(
            "name", fontName=reg, fontSize=fs["name"][0],
            leading=fs["name"][1], textColor=C_MUTED, alignment=TA_LEFT,
        ),
        "attr": ParagraphStyle(
            "attr", fontName=reg, fontSize=fs["attr"][0],
            leading=fs["attr"][1], textColor=C_TEXT, alignment=TA_LEFT,
        ),
        "size": ParagraphStyle(
            "size", fontName=reg, fontSize=fs["size"][0],
            leading=fs["size"][1], textColor=C_TEXT, alignment=TA_LEFT,
        ),
        "sizeth": ParagraphStyle(
            "sizeth", fontName=bold, fontSize=fs["sizeth"][0],
            leading=fs["sizeth"][1], textColor=C_MUTED, alignment=TA_LEFT,
        ),
        "sizel": ParagraphStyle(
            "sizel", fontName=reg, fontSize=fs["size"][0],
            leading=fs["size"][1], textColor=C_MUTED, alignment=TA_RIGHT,
        ),
        "sizeb": ParagraphStyle(
            "sizeb", fontName=bold, fontSize=fs["size"][0],
            leading=fs["size"][1], textColor=C_TEXT, alignment=TA_RIGHT,
        ),
        "total": ParagraphStyle(
            "total", fontName=bold, fontSize=fs["total"][0],
            leading=fs["total"][1], textColor=C_TOTAL, alignment=TA_RIGHT,
        ),
        "foot": ParagraphStyle(
            "foot", fontName=reg, fontSize=fs["foot"][0],
            leading=fs["foot"][1], textColor=C_MUTED, alignment=TA_LEFT,
        ),
        "grand": ParagraphStyle(
            "grand", fontName=bold, fontSize=fs["grand"][0],
            leading=fs["grand"][1], textColor=C_TEXT, alignment=TA_LEFT,
        ),
        "grandsub": ParagraphStyle(
            "grandsub", fontName=reg, fontSize=fs["grandsub"][0],
            leading=fs["grandsub"][1], textColor=C_MUTED, alignment=TA_LEFT,
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


def row_height(
    n_in_row: int,
    per_row: int = PER_ROW,
    h_max: float = ROW_H_MAX,
    h_min: float = ROW_H_MIN,
) -> float:
    if n_in_row <= 0:
        return 0.0
    if n_in_row >= per_row:
        return h_max
    return max(h_min, h_max * n_in_row / per_row)


def photo_rows(n: int, per_row: int = PER_ROW) -> list[list[int]]:
    """Разбить ``n`` фото на строки: ``5 -> [[0,1,2,3],[4]]``.

    ``per_row=2`` (мобильный режим) даёт ``5 -> [[0,1],[2,3],[4]]``.
    """
    if n <= 0:
        return []
    if per_row <= 0:
        per_row = PER_ROW
    out = []
    i = 0
    while i < n:
        out.append(list(range(i, min(i + per_row, n))))
        i += per_row
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
        supplier: bool = False,
        mobile: bool = False,
    ) -> None:
        self.c = canvas
        self.mobile = bool(mobile)
        self.st = _styles(mobile=self.mobile)
        # Геометрия фото — на экземпляр: мобильный режим печатает по два фото
        # в ряд крупнее и ограничивает карточку одной строкой.
        if self.mobile:
            self.per_row = MOBILE_PER_ROW
            self.row_h_max = MOBILE_ROW_H_MAX
            self.row_h_min = MOBILE_ROW_H_MIN
        else:
            self.per_row = PER_ROW
            self.row_h_max = ROW_H_MAX
            self.row_h_min = ROW_H_MIN
        self.cols = [c for c in (cols or []) if c]
        self.labels = labels or {}
        self.with_photos = with_photos and photo_count > 0
        self.photo_count = max(0, int(photo_count))
        if self.mobile:
            # «2 фото в ряд»: не больше одной строки на карточку.
            self.photo_count = min(self.photo_count, MOBILE_PHOTO_CAP) \
                if self.photo_count else 0
        self.subtitle = subtitle
        self.supplier = bool(supplier)
        self.y = TOP
        self.page_no = 0
        if self.supplier:
            # «Для поставщика»: только фото, артикул, название и «размер / кол-во».
            # Блок атрибутов (аналитика) не печатаем вовсе, название — всегда.
            if "name" not in self.cols:
                self.cols = self.cols + ["name"]
            self.attrs = []
        else:
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
        txt = " · ".join(parts)
        foot_size = 9.0 if self.mobile else 7.5

        # Одна линия не влезает в ширину (приписка «из Excel: имя» длинная),
        # а drawString не переносит текст — он молча уезжает за край страницы.
        # Пробуем ужать; если и так не помещается — разделяем на две строки.
        def _fit(line: str, start: float) -> tuple[str, float]:
            size = start
            while size > 5.5 and (
                pdfmetrics.stringWidth(line, self.st["reg"], size) > CONTENT_W
            ):
                size -= 0.25
            w = pdfmetrics.stringWidth(line, self.st["reg"], size)
            if w > CONTENT_W:  # без пробелов влезть не может — рубим хвост
                budget = size * 0.6
                while w > CONTENT_W and line:
                    step = max(1, int((w - CONTENT_W) / budget) + 1)
                    line = line[:-step]
                    w = pdfmetrics.stringWidth(line, self.st["reg"], size)
                line = line.rstrip(" ·") + "…"
            return line, size

        self.c.saveState()
        self.c.setFillColor(C_MUTED)
        if pdfmetrics.stringWidth(txt, self.st["reg"], foot_size) <= CONTENT_W:
            lines = [(txt, foot_size)]
        elif self.subtitle and pdfmetrics.stringWidth(
            FOOTER_TEXT + f" · стр. {self.page_no + 1}", self.st["reg"], foot_size
        ) <= CONTENT_W:
            lines = [
                _fit(FOOTER_TEXT + f" · стр. {self.page_no + 1}", foot_size),
                _fit(self.subtitle, foot_size),
            ]
        else:
            lines = [_fit(txt, foot_size)]
        y = MARGIN + 3 * mm
        for line, size in lines:
            self.c.setFont(self.st["reg"], size)
            self.c.drawString(MARGIN, y, line)
            y += 4 * mm
        self.c.restoreState()

    # -- блоки ----------------------------------------------------------

    def _photo_grid_height(self, count: int) -> float:
        if not self.with_photos or count <= 0:
            return 0.0
        rows = photo_rows(min(count, self.photo_count), self.per_row)
        return sum(
            row_height(len(r), self.per_row, self.row_h_max, self.row_h_min)
            for r in rows
        ) + PHOTO_GAP * (len(rows) - 1)

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
            take = min(self.per_row, len(data) - idx)
            h = row_height(take, self.per_row, self.row_h_max, self.row_h_min)
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

        Мобильный режим: ``n`` равных колонок во всю ширину страницы — фото
        крупные, сетка «2 в ряд» читается с телефона.
        """
        n = len(blobs)
        if self.mobile:
            cell_w = (CONTENT_W - PHOTO_GAP * (n - 1)) / n
            for i, blob in enumerate(blobs):
                self._place_photo(
                    blob, x + i * (cell_w + PHOTO_GAP), y_top - h, cell_w, h
                )
            return CONTENT_W
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

        # Режим прибыльности: под артикулом показываем применённый коэффициент,
        # иначе нельзя понять, почему у прибыльного товара «Дослать» больше.
        # Считаем ПОСЛЕ таблицы размеров: при k≤0 обычный режим печатает
        # «не подсортировываем», но бюджет из Excel («WB дефицит») может
        # задать «Дослать» вручную — тогда важнее факт ручной дослаты.
        factor_p = None
        if not self.supplier and sizes and sizes[0].get("factor") is not None:
            k = float(sizes[0].get("factor") or 0)
            if k <= 0:
                txt = ("учёт прибыльности: подсорт ×1 — товар убыточный, не подсортировываем"
                       if total == 0 else
                       "учёт прибыльности: подсорт ×0 — убыточный, дослата задана вручную")
            else:
                txt = f"учёт прибыльности: подсорт ×{k:.2f}"
            factor_p = Paragraph(_esc(txt), self.st["name"])
            fh = factor_p.wrap(CONTENT_W, 1e6)[1] + 1 * mm
        else:
            fh = 0.0

        total_p = Paragraph(
            f"Итого дослать на WB: <b>{fmt_num(total)}</b> шт", self.st["total"]
        )
        tw, th = total_p.wrap(CONTENT_W, 1e6)

        block_h = (
            photo_h
            + (PHOTO_GAP if photo_h else 0)
            + ah + 1.5 * mm
            + nh
            + fh
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
        if factor_p is not None:
            self._para(factor_p)
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

        Режим «для поставщика»: только размеры с количеством > 0 и две колонки
        (размер / количество) — без остатков и прочей служебной аналитики.
        """
        if self.supplier:
            items = []
            for s in sizes:
                qty = max(0, _int0(s.get("to_sort")))
                if qty > 0:
                    items.append((str(s.get("size") or "—"), qty))
            if not items:
                return None, 0.0, 0
            head = ["Размер", "Кол-во, шт"]
            data = [[Paragraph(h, self.st["sizeth"]) for h in head]]
            for sz, qty in items:
                data.append([
                    Paragraph(_esc(sz), self.st["size"]),
                    Paragraph(fmt_num(qty), self.st["sizeb"]),
                ])
            w = CONTENT_W / len(head)
            col_w = [w * SUPPLIER_COL_W[0], w * SUPPLIER_COL_W[1]]
            tbl = Table(data, colWidths=col_w, hAlign="LEFT", repeatRows=1)
            tbl.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                        ("LEFTPADDING", (0, 0), (-1, -1), 2),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                        ("LINEBELOW", (0, 1), (-1, -2), 0.25, C_ZEBRA),
                    ]
                )
            )
            _, h = tbl.wrapOn(self.c, CONTENT_W, 1e6)
            return tbl, h + 1.5 * mm, sum(qty for _sz, qty in items)

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
    supplier: bool = False,
    mobile: bool = False,
) -> bytes:
    """Собрать PDF. ``cards`` = [(строка потребности, размеры, миниатюры), ...].

    ``supplier=True`` — упрощённый режим «для поставщика»: только фото,
    артикул, название и размеры с количеством (> 0), без аналитики.

    ``mobile=True`` — раскладка под смартфон: по два крупных фото в ряд,
    крупнее шрифты. Обычно включается вместе с ``supplier``.
    """
    from reportlab.pdfgen import canvas as rl_canvas

    ensure_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Потребность в товаре")
    c.setAuthor("Agent Market")

    doc = DemandPdf(
        c, cols=cols, labels=labels, with_photos=with_photos,
        photo_count=photo_count, subtitle=subtitle, supplier=supplier,
        mobile=mobile,
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