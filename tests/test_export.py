"""Износ на думите като отделни слоеве.

Проверява се това, което би провалило работата в редактор: че всяка дума
получава свой файл, че файлът е в размера на кадъра и че описанието казва
кога и къде е думата.
"""

from __future__ import annotations

import csv
import json

import pytest
from PIL import Image

from subs.burn import MediaInfo
from subs.export import ExportedWord, export_words, render_word, safe_name
from subs.layout import assign_highlights, split_blocks
from subs.models import Word
from subs.renderers import get_renderer
from subs.renderers.base import RenderRequest
from subs.styles import get_style
from pathlib import Path

MEDIA = MediaInfo(width=1080, height=1920, fps=30.0, duration=6.0, has_audio=False)


def layouts_for(style_name: str, words: list[Word]):
    style = get_style(style_name)
    blocks = assign_highlights(split_blocks(words, style.blocks), style.highlight, "bg")
    request = RenderRequest(source=Path("няма.mp4"), blocks=blocks, style=style,
                            media=MEDIA, progress=lambda _m: None)
    return get_renderer(style.renderer).layout(request), style


def sample_words() -> list[Word]:
    return [
        Word("Тази", 0.2, 0.55),
        Word("програма", 0.6, 1.05, color="#FF3B30"),
        Word("прави", 1.1, 1.4),
        Word("субтитрите", 1.45, 2.1, emphasis=True),
    ]


# --------------------------------------------------------------------------
# Имена на файлове
# --------------------------------------------------------------------------


def test_file_name_keeps_the_word_and_the_order():
    assert safe_name("субтитрите", 4) == "004-субтитрите.png"


def test_file_name_drops_what_windows_refuses():
    name = safe_name('а/б:в*г?"д', 1)
    assert not set(name) & set('/:*?"<>|')


def test_file_name_survives_an_unusable_word():
    assert safe_name("///", 7).startswith("007-")
    assert safe_name("///", 7).endswith(".png")


# --------------------------------------------------------------------------
# Самите файлове
# --------------------------------------------------------------------------


@pytest.mark.parametrize("style_name", ["stack", "behind"])
def test_every_word_gets_a_file(tmp_path, style_name):
    layouts, style = layouts_for(style_name, sample_words())
    exported = export_words(layouts, style, MEDIA, tmp_path, progress=lambda _m: None)
    assert exported
    for word in exported:
        assert (tmp_path / word.file).exists()


@pytest.mark.parametrize("style_name", ["stack", "behind"])
def test_files_are_full_frame_and_transparent(tmp_path, style_name):
    layouts, style = layouts_for(style_name, sample_words())
    exported = export_words(layouts, style, MEDIA, tmp_path, progress=lambda _m: None)
    image = Image.open(tmp_path / exported[0].file)
    assert image.size == (MEDIA.width, MEDIA.height), "иначе не ляга на мястото си"
    assert image.mode == "RGBA"
    assert image.getpixel((5, 5))[3] == 0, "ъгълът трябва да е прозрачен"


def test_the_word_is_actually_drawn(tmp_path):
    layouts, style = layouts_for("stack", sample_words())
    exported = export_words(layouts, style, MEDIA, tmp_path, progress=lambda _m: None)
    image = Image.open(tmp_path / exported[0].file)
    assert image.getchannel("A").getextrema()[1] > 200, "слоят излезе празен"


def test_box_matches_where_the_ink_is(tmp_path):
    """Правоъгълникът в описанието е на самата дума, без сянката —
    иначе позиционирането в редактор би било изместено надолу."""
    layouts, style = layouts_for("stack", sample_words())
    exported = export_words(layouts, style, MEDIA, tmp_path, progress=lambda _m: None)
    word = exported[0]
    alpha = Image.open(tmp_path / word.file).getchannel("A")
    # Сянката е под 55% непрозрачност и размазана; прагът оставя само текста.
    solid = alpha.point(lambda value: 255 if value > 200 else 0).getbbox()
    assert abs(solid[0] - word.x) <= 2 and abs(solid[1] - word.y) <= 2
    assert abs((solid[2] - solid[0]) - word.width) <= 2


def test_ink_sits_inside_the_line_box_like_libass_puts_it():
    """Закотвя вертикалното позициониране за стил A.

    Оформлението дава ``y`` като горния ръб на реда (котва ``\\an7``), а
    libass слага основната линия на ``usWinAscent`` под него. Ако тук се
    рисува направо по ``y``, мастилото излиза над реда и изнесеният слой
    не съвпада с готовото видео — разминаване, което се вижда чак в
    редактора.
    """
    layouts, style = layouts_for("stack", sample_words())
    word = layouts[0].placed[0]
    image, _ = render_word(word, style, MEDIA)
    top, bottom = image.getchannel("A").getbbox()[1], image.getchannel("A").getbbox()[3]
    assert top >= word.y, "мастилото не бива да е над горния ръб на реда"
    assert top < word.y + word.size, "нито под целия ред"


def test_the_shadow_grows_the_file_but_not_the_box(tmp_path):
    layouts, style = layouts_for("stack", sample_words())
    word = export_words(layouts, style, MEDIA, tmp_path,
                        progress=lambda _m: None)[0]
    whole = Image.open(tmp_path / word.file).getchannel("A").getbbox()
    assert whole[3] > word.y + word.height, "сянката трябва да излиза под текста"


def test_colour_reaches_the_exported_file(tmp_path):
    layouts, style = layouts_for("stack", sample_words())
    exported = export_words(layouts, style, MEDIA, tmp_path, progress=lambda _m: None)
    coloured = next(w for w in exported if w.text == "програма")
    image = Image.open(tmp_path / coloured.file).convert("RGBA")
    box = image.getchannel("A").getbbox()
    red, green, blue, alpha = (channel.crop(box) for channel in image.split())
    solid = alpha.point(lambda value: 255 if value > 200 else 0)
    average = lambda channel: sum(  # noqa: E731 — четимо на един ред
        value * weight for value, weight in enumerate(channel.histogram())
    )
    assert sum(solid.histogram()[255:]) > 0, "думата не е нарисувана"
    assert average(red) > average(blue) * 2, "червената дума излезе безцветна"


def test_bigger_word_makes_a_bigger_mark(tmp_path):
    small, style = layouts_for("stack", sample_words())
    words = sample_words()
    words[0].scale = 2.0
    large, _ = layouts_for("stack", words)
    a = export_words(small, style, MEDIA, tmp_path / "a", progress=lambda _m: None)[0]
    b = export_words(large, style, MEDIA, tmp_path / "b", progress=lambda _m: None)[0]
    assert b.width > a.width * 1.5


# --------------------------------------------------------------------------
# Описанието
# --------------------------------------------------------------------------


def test_manifest_lists_every_word(tmp_path):
    layouts, style = layouts_for("stack", sample_words())
    exported = export_words(layouts, style, MEDIA, tmp_path, progress=lambda _m: None)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    assert len(data["words"]) == len(exported)
    assert data["video"]["width"] == MEDIA.width
    assert data["style"] == "stack"


def test_manifest_has_both_kinds_of_timing(tmp_path):
    layouts, style = layouts_for("stack", sample_words())
    export_words(layouts, style, MEDIA, tmp_path, progress=lambda _m: None)
    first = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))["words"][0]
    assert first["start"] <= first["spoken_start"]
    assert first["spoken_end"] <= first["end"]


def test_csv_opens_in_a_spreadsheet(tmp_path):
    layouts, style = layouts_for("stack", sample_words())
    export_words(layouts, style, MEDIA, tmp_path, progress=lambda _m: None)
    raw = (tmp_path / "layers.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "без BOM Excel чете кирилицата грешно"
    rows = list(csv.reader(raw.decode("utf-8-sig").splitlines(), delimiter=";"))
    assert rows[0][1] == "дума"
    assert len(rows) == len(sample_words()) + 1


def test_exported_word_json_is_complete():
    word = ExportedWord(1, "а", 0.0, 1.0, 0.2, 0.8, "001-а.png", 10, 20, 30, 40)
    data = word.to_json()
    assert data["box"] == {"x": 10, "y": 20, "width": 30, "height": 40}
    assert data["duration"] == 1.0


def test_render_word_does_not_need_a_file_on_disk():
    layouts, style = layouts_for("behind", sample_words())
    word = layouts[0].placed[0]
    image, box = render_word(word, style, MEDIA)
    assert image.size == (MEDIA.width, MEDIA.height)
    assert box[2] > 0 and box[3] > 0


# --------------------------------------------------------------------------
# Формат и фон
# --------------------------------------------------------------------------


def test_background_names_resolve():
    from subs.export import CHROMA_GREEN, resolve_background
    from subs.raster import hex_rgb

    assert resolve_background(None) is None
    assert resolve_background("прозрачен") is None
    assert resolve_background("зелен") == hex_rgb(CHROMA_GREEN)
    assert resolve_background("#123456") == (0x12, 0x34, 0x56)


def test_unknown_background_is_reported():
    from subs.export import resolve_background

    with pytest.raises(ValueError, match="прозрачен"):
        resolve_background("морав")


def test_unknown_format_is_reported(tmp_path):
    layouts, style = layouts_for("stack", sample_words())
    with pytest.raises(ValueError, match="png"):
        export_words(layouts, style, MEDIA, tmp_path, fmt="tiff",
                     progress=lambda _m: None)


def test_green_background_fills_the_frame(tmp_path):
    from subs.export import CHROMA_GREEN
    from subs.raster import hex_rgb

    layouts, style = layouts_for("stack", sample_words())
    exported = export_words(layouts, style, MEDIA, tmp_path, background="зелен",
                            progress=lambda _m: None)
    image = Image.open(tmp_path / exported[0].file).convert("RGBA")
    assert image.getpixel((5, 5)) == hex_rgb(CHROMA_GREEN) + (255,)


def test_transparent_stays_transparent(tmp_path):
    layouts, style = layouts_for("stack", sample_words())
    exported = export_words(layouts, style, MEDIA, tmp_path, progress=lambda _m: None)
    image = Image.open(tmp_path / exported[0].file).convert("RGBA")
    assert image.getpixel((5, 5))[3] == 0


def test_pdf_files_are_written(tmp_path):
    layouts, style = layouts_for("stack", sample_words())
    exported = export_words(layouts, style, MEDIA, tmp_path, fmt="pdf",
                            progress=lambda _m: None)
    for word in exported:
        assert word.file.endswith(".pdf")
        assert (tmp_path / word.file).read_bytes().startswith(b"%PDF")


def test_manifest_records_format_and_background(tmp_path):
    layouts, style = layouts_for("stack", sample_words())
    export_words(layouts, style, MEDIA, tmp_path, fmt="pdf", background="зелен",
                 progress=lambda _m: None)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    assert data["format"] == "pdf"
    assert data["background"] == "#00B140"
    assert "хромакей" in data["note"], "плътният фон трябва да е обяснен"


def test_offset_moves_the_exported_word(tmp_path):
    words = sample_words()
    plain, style = layouts_for("stack", words)
    words[0].dx, words[0].dy = 0.05, 0.02
    moved, _ = layouts_for("stack", words)
    a = export_words(plain, style, MEDIA, tmp_path / "a", progress=lambda _m: None)[0]
    b = export_words(moved, style, MEDIA, tmp_path / "b", progress=lambda _m: None)[0]
    assert b.x - a.x == pytest.approx(0.05 * MEDIA.height, abs=2)
    assert b.y - a.y == pytest.approx(0.02 * MEDIA.height, abs=2)
