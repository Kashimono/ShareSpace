"""Render the SVG product mark into PNG and multi-size Windows ICO assets."""

from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parents[1]
SVG_PATH = ROOT / "assets" / "sharespace.svg"
PNG_PATH = ROOT / "assets" / "sharespace.png"
ICO_PATH = ROOT / "assets" / "sharespace.ico"
SIZES = (16, 32, 48, 64, 128, 256)


def _render(renderer: QSvgRenderer, size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    return image


def _dib_image(image: QImage) -> bytes:
    size = image.width()
    pixels = bytearray()
    for y in range(size - 1, -1, -1):
        for x in range(size):
            pixel = image.pixelColor(x, y)
            pixels.extend((pixel.blue(), pixel.green(), pixel.red(), pixel.alpha()))
    mask_row_bytes = ((size + 31) // 32) * 4
    mask = bytes(mask_row_bytes * size)
    header = struct.pack(
        "<IIIHHIIIIII",
        40, size, size * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0,
    )
    return header + pixels + mask


def main() -> None:
    renderer = QSvgRenderer(QByteArray(SVG_PATH.read_bytes()))
    if not renderer.isValid():
        raise SystemExit(f"Invalid SVG icon: {SVG_PATH}")
    PNG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _render(renderer, 256).save(str(PNG_PATH), "PNG")

    images = [(size, _dib_image(_render(renderer, size))) for size in SIZES]
    offset = 6 + 16 * len(images)
    entries = bytearray()
    payload = bytearray()
    for size, data in images:
        entries.extend(struct.pack(
            "<BBBBHHII", 0 if size == 256 else size, 0 if size == 256 else size,
            0, 0, 1, 32, len(data), offset,
        ))
        payload.extend(data)
        offset += len(data)
    ICO_PATH.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + entries + payload)


if __name__ == "__main__":
    main()
