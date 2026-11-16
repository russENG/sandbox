from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from couple_calendar.images import MAX_BYTES, MAX_LONG_EDGE, MAX_PIXELS, prepare_image


def _encode(img: Image.Image, fmt: str, **kwargs) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def _decode(data: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(data)))


def test_small_jpeg_is_passed_through():
    raw = _encode(Image.new("RGB", (800, 600), (10, 20, 30)), "JPEG")
    out = prepare_image(raw)
    assert out.media_type == "image/jpeg"
    assert base64.b64decode(out.data) == raw
    assert out.to_block()["source"]["type"] == "base64"


def test_large_photo_is_shrunk_within_limits():
    raw = _encode(Image.new("RGB", (4032, 3024), (200, 100, 50)), "PNG")
    out = prepare_image(raw)
    img = _decode(out.data)
    assert out.media_type == "image/jpeg"
    assert max(img.size) <= MAX_LONG_EDGE
    assert img.size[0] * img.size[1] <= MAX_PIXELS
    assert len(base64.b64decode(out.data)) <= MAX_BYTES
    assert abs(img.size[0] / img.size[1] - 4 / 3) < 0.01  # 縦横比は保つ


def test_exif_rotation_is_applied():
    img = Image.new("RGB", (300, 100), (0, 0, 0))
    exif = img.getexif()
    exif[0x0112] = 6  # 90度回転して表示する写真
    out = prepare_image(_encode(img, "JPEG", exif=exif))
    assert _decode(out.data).size == (100, 300)


def test_transparent_png_becomes_white_jpeg():
    img = Image.new("RGBA", (3000, 100), (0, 0, 0, 0))
    out = prepare_image(_encode(img, "PNG"))
    decoded = _decode(out.data).convert("RGB")
    assert decoded.getpixel((0, 0)) == (255, 255, 255)


def test_not_an_image():
    with pytest.raises(ValueError):
        prepare_image(b"hello")
