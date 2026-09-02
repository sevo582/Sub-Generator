"""Записване на PDF с прозрачност.

Pillow умее да записва RGBA в PDF, но като JPEG 2000 със ``/SMaskInData`` —
валидно, но почти никой четец не го рендира вярно. Тук се проверява, че се
пише обикновената конструкция, която всички разбират.
"""

from __future__ import annotations

import zlib

from PIL import Image

from subs.pdfwrite import write_rgba_pdf


def read(path) -> bytes:
    return path.read_bytes()


def test_file_looks_like_a_pdf(tmp_path):
    path = tmp_path / "a.pdf"
    write_rgba_pdf(Image.new("RGBA", (40, 20), (10, 20, 30, 255)), path)
    data = read(path)
    assert data.startswith(b"%PDF-1.5")
    assert data.rstrip().endswith(b"%%EOF")


def test_page_is_the_size_of_the_image(tmp_path):
    path = tmp_path / "a.pdf"
    write_rgba_pdf(Image.new("RGBA", (300, 120), (0, 0, 0, 255)), path)
    assert b"/MediaBox [0 0 300 120]" in read(path)


def test_transparent_image_gets_a_soft_mask(tmp_path):
    path = tmp_path / "a.pdf"
    image = Image.new("RGBA", (20, 20), (255, 0, 0, 0))
    image.putpixel((10, 10), (255, 0, 0, 255))
    write_rgba_pdf(image, path)
    data = read(path)
    assert b"/SMask 6 0 R" in data
    assert b"/DeviceGray" in data


def test_opaque_image_skips_the_mask(tmp_path):
    """Без прозрачност маската е излишна тежест."""
    path = tmp_path / "a.pdf"
    write_rgba_pdf(Image.new("RGBA", (20, 20), (0, 177, 64, 255)), path)
    assert b"/SMask" not in read(path)


def test_colours_are_written_as_plain_rgb(tmp_path):
    """Точно тук Pillow бърка: JPEG 2000 вместо FlateDecode."""
    path = tmp_path / "a.pdf"
    write_rgba_pdf(Image.new("RGBA", (8, 4), (0, 177, 64, 255)), path)
    data = read(path)
    assert b"/DeviceRGB" in data and b"/FlateDecode" in data
    assert b"JPXDecode" not in data

    start = data.index(b"stream\n", data.index(b"/DeviceRGB")) + len(b"stream\n")
    end = data.index(b"\nendstream", start)
    pixels = zlib.decompress(data[start:end])
    assert pixels[:3] == bytes((0, 177, 64)), "цветът излезе друг"
    assert len(pixels) == 8 * 4 * 3


def test_alpha_channel_is_written_verbatim(tmp_path):
    path = tmp_path / "a.pdf"
    image = Image.new("RGBA", (4, 2), (255, 255, 255, 128))
    write_rgba_pdf(image, path)
    data = read(path)
    start = data.index(b"stream\n", data.index(b"/DeviceGray")) + len(b"stream\n")
    end = data.index(b"\nendstream", start)
    assert zlib.decompress(data[start:end]) == bytes([128] * 8)


def test_offsets_in_the_cross_reference_table_are_right(tmp_path):
    """Сгрешен xref прави файла нечетим в строгите четци."""
    path = tmp_path / "a.pdf"
    write_rgba_pdf(Image.new("RGBA", (16, 16), (1, 2, 3, 200)), path)
    data = read(path)
    table = data[data.index(b"xref\n"):]
    rows = table.splitlines()[2:]
    for number, row in enumerate(rows, start=1):
        if not row.endswith(b" n "):
            break
        offset = int(row.split()[0])
        assert data[offset:offset + len(str(number))] == str(number).encode()
