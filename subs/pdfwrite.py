"""Записване на PDF с истинска прозрачност.

Pillow умее да записва RGBA в PDF, но го прави като JPEG 2000 със
``/SMaskInData``. Това е валидно по спецификация и почти никой четец не го
рендира вярно — плътно зелено излиза лилаво в poppler, а тъкмо в чужд четец
файлът ще бъде отворен.

Затова тук се пише най-обикновената конструкция, която всички разбират:
изображението е ``/DeviceRGB`` с ``/FlateDecode``, а прозрачността е
отделно сиво изображение, закачено като ``/SMask``. Няма зависимости — само
``zlib`` от стандартната библиотека.
"""

from __future__ import annotations

import zlib
from pathlib import Path

from PIL import Image


def _stream(number: int, options: str, data: bytes) -> bytes:
    return (f"{number} 0 obj\n<< {options} /Length {len(data)} >>\nstream\n"
            .encode("ascii") + data + b"\nendstream\nendobj\n")


def write_rgba_pdf(image: Image.Image, path: str | Path) -> None:
    """Записва изображението като едностраничен PDF в размер 1 пиксел = 1 точка.

    Прозрачността се запазва, ако изображението има такава; ако е плътно,
    маската се пропуска и файлът излиза по-малък.
    """
    image = image.convert("RGBA")
    width, height = image.size
    alpha = image.getchannel("A")
    transparent = alpha.getextrema()[0] < 255

    colour = zlib.compress(image.convert("RGB").tobytes(), 9)
    content = f"q {width} 0 0 {height} 0 0 cm /Im0 Do Q".encode("ascii")

    objects: list[bytes] = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (f"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}]"
         f" /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
         ).encode("ascii"),
        _stream(4, f"/Type /XObject /Subtype /Image /Width {width} /Height {height}"
                   f" /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode"
                   + (" /SMask 6 0 R" if transparent else ""), colour),
        _stream(5, "", content),
    ]
    if transparent:
        objects.append(_stream(
            6, f"/Type /XObject /Subtype /Image /Width {width} /Height {height}"
               f" /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode",
            zlib.compress(alpha.tobytes(), 9)))

    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for body in objects:
        offsets.append(len(out))
        out += body

    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{start}\n%%EOF\n").encode("ascii")

    Path(path).write_bytes(bytes(out))
