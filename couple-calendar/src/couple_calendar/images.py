"""LINE から届いた画像を、Claude に渡せる形に整える。

スマホの写真は大きいことが多いので、Claude が読める上限（長辺 2576px・約375万画素）に収まるよう縮小し、
写真の向き（EXIF）も直す。学校のお便りなど文字の多い画像を読むため、上限いっぱいまでは解像度を保つ。
"""

from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass

MAX_LONG_EDGE = 2576
MAX_PIXELS = 3_750_000
MAX_BYTES = 3_700_000  # base64 にすると約 5MB
EXIF_ORIENTATION = 0x0112

_MEDIA_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}


@dataclass(frozen=True)
class ImageInput:
    media_type: str
    data: str  # base64

    def to_block(self) -> dict:
        return {"type": "image", "source": {"type": "base64", "media_type": self.media_type, "data": self.data}}


def prepare_image(raw: bytes) -> ImageInput:
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        raise ValueError("画像を読み込めませんでした。") from e

    media_type = _MEDIA_TYPES.get((img.format or "").upper())
    width, height = img.size
    rotated = img.getexif().get(EXIF_ORIENTATION, 1) != 1
    too_big = max(width, height) > MAX_LONG_EDGE or width * height > MAX_PIXELS
    if media_type and not too_big and not rotated and len(raw) <= MAX_BYTES:
        return ImageInput(media_type, base64.standard_b64encode(raw).decode("ascii"))

    img = ImageOps.exif_transpose(img)
    width, height = img.size
    scale = min(1.0, MAX_LONG_EDGE / max(width, height), math.sqrt(MAX_PIXELS / (width * height)))
    if scale < 1.0:
        img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))  # 透明部分は白にする
        background.paste(rgba, mask=rgba.getchannel("A"))
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    for quality in (88, 80, 70, 60):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_BYTES:
            break
    return ImageInput("image/jpeg", base64.standard_b64encode(buf.getvalue()).decode("ascii"))
