"""PNG validation and in-process image bytes, separate from public JSON metadata."""

from __future__ import annotations

import struct
from io import BytesIO
from typing import Any

from PIL import Image


class ScreenshotResult(dict[str, Any]):
    """Carry already-validated, immutable PNG bytes without adding JSON fields."""

    def __init__(self, payload: dict[str, Any], png: bytes) -> None:
        super().__init__(payload)
        self.png = png


def validate_png(data: bytes) -> None:
    """Check the complete PNG stream and decode its pixels, not just its header."""
    if (
        len(data) < 45
        or not data.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        or not data.endswith(b"\x00\x00\x00\x00IEND\xae\x42\x60\x82")
    ):
        raise ValueError("Screenshot is not a complete PNG image.")
    width, height = struct.unpack_from(">II", data, 16)
    pixel_limit = Image.MAX_IMAGE_PIXELS
    if pixel_limit is not None and width * height > pixel_limit:
        raise ValueError("Screenshot exceeds the PNG pixel limit.")
    try:
        with Image.open(BytesIO(data), formats=["PNG"]) as image:
            if pixel_limit is not None and image.width * image.height > pixel_limit:
                raise ValueError("Screenshot exceeds the PNG pixel limit.")
            image.verify()
        # verify() checks chunk integrity but does not decode compressed pixels.
        with Image.open(BytesIO(data), formats=["PNG"]) as image:
            image.load()
    except (
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("Screenshot is not a valid PNG image.") from exc
