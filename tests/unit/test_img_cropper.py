# -*- coding: utf-8 -*-
"""«Кроп изображений»: обрезка фона и вставка на белую канву."""

import io
import zipfile

from PIL import Image

from app.services import img_cropper as c


def _img_bytes(w=120, h=240):
    im = Image.new("RGB", (w, h), (255, 255, 255))
    for x in range(w // 4, w * 3 // 4):
        for y in range(h // 4, h * 3 // 4):
            im.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_params_for_unknown_falls_back_to_default():
    assert c.params_for("нет такой категории") is c.params_for(c.DEFAULT_TYPE)


def test_params_for_known_categories():
    for name in c.CATEGORIES:
        assert isinstance(c.params_for(name), c.CropParams)


def test_crop_images_returns_jpeg_on_canvas():
    buf = c.crop_images([("photo.png", _img_bytes())], "Одежда по пояс")
    z = zipfile.ZipFile(buf)
    assert z.namelist() == ["photo.JPG"]
    out = Image.open(io.BytesIO(z.read("photo.JPG")))
    assert out.mode == "RGB"
    assert out.size == (2250, 3000)
