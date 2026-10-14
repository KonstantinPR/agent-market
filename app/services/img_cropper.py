# -*- coding: utf-8 -*-
"""«Кроп изображений»: обрезка белого фона и вставка на белую канву.

Порт ``finance/app/modules/img_cropper.py``. Параметры категории одежды
описаны dataclass-ом (в оригинале — глобальные переменные), поэтому функции
чистые и тестируемые.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from PIL import Image, ImageOps

#: Значения по умолчанию (ветка «else» в оригинале — фолбэк на полный рост).
DEFAULT_TYPE = "Модель в полный рост"

CATEGORIES = (
    "Одежда по пояс",
    "Обувь без модели",
    "Обувь без модели малыши",
    "Модель в полный рост",
)


@dataclass(frozen=True)
class CropParams:
    k_height_left_start: float
    k_height_right_start: float
    k_bottom_left_start: float
    k_bottom_right_start: float
    step_iteration: int
    step_iteration_bottom: int
    sensibility_color: int
    count_step_j: int
    delimiter: int
    new_height_im: int
    k_width_height_im: float
    k_boot_long_legs: int

    @property
    def white_block_width(self) -> int:
        return int(self.new_height_im * self.k_width_height_im)

    @property
    def white_block_height(self) -> int:
        return self.new_height_im


_PARAMS: dict[str, CropParams] = {
    "Одежда по пояс": CropParams(0.10, 0.90, 0.10, 0.90, 25, 20, 210, 10, 4, 3000, 0.75, 1),
    "Обувь без модели": CropParams(0.10, 0.90, 0.10, 0.97, 25, 20, 215, 10, 6, 3200, 0.75, 1),
    "Обувь без модели малыши": CropParams(0.10, 0.90, 0.10, 0.97, 35, 20, 215, 10, 6, 3200, 0.75, 1),
    DEFAULT_TYPE: CropParams(0.30, 0.70, 0.20, 0.80, 5, 20, 210, 2, 4, 3000, 0.75, 1),
}


def params_for(type_clothes: str) -> CropParams:
    """Параметры кропа по типу одежды (неизвестный тип → полный рост)."""
    return _PARAMS.get(str(type_clothes).strip(), _PARAMS[DEFAULT_TYPE])


def _crop_height(pix: np.ndarray, p: CropParams) -> int:
    height, width, _color = pix.shape
    step = p.step_iteration
    left_start = width * p.k_height_left_start
    right_end = width * p.k_height_right_start
    new_height = 0
    count_i = step * p.count_step_j * p.k_boot_long_legs
    for row in pix[step * 10::step]:
        count_j = 0
        for px in row:
            if left_start < count_j < right_end:
                if int(px[0]) + int(px[1]) + int(px[2]) < p.sensibility_color * 3:
                    new_height = count_i - step * 4
                    break
            count_j += 1
            if new_height != 0:
                break
        count_i += step
    return new_height


def _crop_bottom(pix: np.ndarray, img_crop_height_top: int, p: CropParams) -> tuple[int, int, int]:
    height, width, _color = pix.shape
    step = p.step_iteration_bottom
    left_start = width * p.k_bottom_left_start
    right_end = width * p.k_bottom_right_start
    new_bottom = 0
    count_i = step * 10
    left_right_border: list[int] = []
    for row in pix[img_crop_height_top + step * 10::step]:
        count_j = step * p.count_step_j
        count_row = 0
        for px in row:
            if left_start < count_j < right_end:
                if int(px[0]) + int(px[1]) + int(px[2]) > p.sensibility_color * 3:
                    count_row += 1
                    if (
                        count_row >= (width * p.k_bottom_right_start - width * p.k_bottom_left_start) - 5
                        and count_i > height / p.delimiter
                    ):
                        new_bottom = height - img_crop_height_top - count_i - step
                        break
                else:
                    left_right_border.append(count_j)
            count_j += 1
            if new_bottom != 0:
                break
        count_i += step
    if left_right_border:
        left_border = min(left_right_border) - step * 16
        right_border = width - (max(left_right_border) + step * 4)
    else:
        left_border = 0
        right_border = 0
    return new_bottom, left_border, right_border


def crop_image(img: Image.Image, type_clothes: str) -> Image.Image:
    """Обрезать фон по типу одежды и вернуть фото на белой канве."""
    p = params_for(type_clothes)
    fixed = ImageOps.exif_transpose(img) or img
    if fixed.mode != "RGB":
        fixed = fixed.convert("RGB")
    pix = np.asarray(fixed)

    top = _crop_height(pix, p)
    bottom, left_border, right_border = _crop_bottom(pix, top, p)
    area = (left_border, top, right_border, bottom)  # left, top, right, bottom
    cropped = ImageOps.crop(fixed, area)

    old_width, old_height = cropped.size
    if old_height <= 0 or old_width <= 0:
        cropped = fixed
        old_width, old_height = cropped.size

    new_height = p.new_height_im
    new_width = int(new_height * p.k_width_height_im)
    if old_height < old_width:
        new_height = int(old_height * new_width / old_width)

    new_width = int(new_height / old_height * old_width)
    resized = cropped.resize((new_width, new_height))

    canvas = Image.new("RGB", (p.white_block_width, p.white_block_height), (255, 255, 255))
    if old_height < old_width:
        k = int((p.white_block_height - new_height) * 0.10)
        canvas.paste(resized, ((p.white_block_width - new_width + k) // 2, p.white_block_height - new_height))
    else:
        canvas.paste(resized, ((p.white_block_width - new_width) // 2, (p.white_block_height - new_height) // 2))
    return canvas


def _to_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def crop_images(
    items: Sequence[tuple[str, bytes]],
    type_clothes: str,
) -> io.BytesIO:
    """Обработать пары ``(имя_файла, байты)`` и вернуть ZIP с JPEG-ами."""
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in items:
            with Image.open(io.BytesIO(data)) as im:
                out = crop_image(im, type_clothes)
            stem = name.rsplit(".", 1)[0] if "." in name else name
            zf.writestr(f"{stem}.JPG", _to_jpeg(out))
    zbuf.seek(0)
    return zbuf
