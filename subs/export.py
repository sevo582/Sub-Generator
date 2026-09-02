"""Износ на всяка дума като отделен прозрачен PNG.

Готовото видео е един слой — в редактор няма как да се хване отделна дума.
Затова тук всяка дума излиза като собствен файл с прозрачен фон, **в пълния
размер на кадъра и на точното си място**. В CapCut (или Premiere, или
Resolve) се внасят и всяка ляга там, където ѝ е мястото, без да се мести
нищо на ръка. Оттам нататък всяка си е отделен обект: ключови кадри,
ефекти, движение — каквото трябва.

Изнася се **неподвижен** кадър, а не парче видео. Това е нарочно: в CapCut
неподвижен слой се анимира свободно, докато в записана анимация вече има
движение, което не се маха. Анимациите от инструмента не се запичат в
PNG-тата — те са за готовото видео.

Заедно с файловете излиза и ``layers.json`` с тайминги и позиции, плюс
``layers.csv`` за отваряне в таблица.
"""

from __future__ import annotations

import csv
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .burn import MediaInfo
from .models import BlockLayout, Placed
from .pdfwrite import write_rgba_pdf
from .raster import Sprite, composite, hex_rgb
from .styles import BehindStyle, ShadowSpec, StackStyle, Style
from .textmetrics import font_path

#: Формати на изнесените файлове.
FORMATS: tuple[str, ...] = ("png", "pdf")

#: Класическото хромакей зелено. Приема се и произволен ``#RRGGBB``.
CHROMA_GREEN = "#00B140"

#: Имена на фоновете, които се пишат на командния ред.
BACKGROUNDS: tuple[str, ...] = ("прозрачен", "зелен")


@dataclass
class ExportedWord:
    """Един изнесен файл плюс това, което трябва, за да се сложи в редактор."""

    index: int
    text: str
    #: Докато думата стои на екрана — това е дължината на слоя в редактора.
    start: float
    end: float
    #: Кога се изрича — за ефект точно върху нея.
    spoken_start: float
    spoken_end: float
    file: str
    x: int
    y: int
    width: int
    height: int

    def to_json(self) -> dict[str, object]:
        return {
            "index": self.index,
            "text": self.text,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.end - self.start, 3),
            "spoken_start": round(self.spoken_start, 3),
            "spoken_end": round(self.spoken_end, 3),
            "file": self.file,
            "box": {"x": self.x, "y": self.y,
                    "width": self.width, "height": self.height},
        }


def safe_name(text: str, index: int, suffix: str = "png") -> str:
    """Име на файл, което Windows приема, без да губи коя е думата."""
    cleaned = "".join(
        char for char in unicodedata.normalize("NFC", text)
        if char.isalnum() or char in " -_"
    ).strip().replace(" ", "_")
    return f"{index:03d}-{cleaned[:40] or 'дума'}.{suffix}"


def resolve_background(name: str | None) -> tuple[int, int, int] | None:
    """Превръща името на фона в цвят. ``None`` значи прозрачен.

    Приема ``прозрачен``/``transparent``, ``зелен``/``green`` и произволен
    ``#RRGGBB``.
    """
    if not name or name in ("прозрачен", "transparent", "none"):
        return None
    if name in ("зелен", "green"):
        return hex_rgb(CHROMA_GREEN)
    if name.startswith("#"):
        return hex_rgb(name)
    raise ValueError(
        f"непознат фон {name!r}; налични: прозрачен, зелен или #RRGGBB")


def flatten(image: Image.Image, background: tuple[int, int, int] | None
            ) -> Image.Image:
    """Слага фон под думата, ако е поискан такъв."""
    if background is None:
        return image
    plate = Image.new("RGBA", image.size, background + (255,))
    return Image.alpha_composite(plate, image)


def save_layer(image: Image.Image, path: Path, fmt: str) -> None:
    """Записва слоя. PDF-ът носи истинска прозрачност — виж ``pdfwrite``."""
    if fmt == "pdf":
        write_rgba_pdf(image, path)
    else:
        image.save(path, "PNG")


def _word_settings(style: Style, word: Placed) -> tuple[str, str, float, ShadowSpec,
                                                        float, bool]:
    """Шрифт, цвят, непрозрачност и сянка за дума, според стила."""
    if style.renderer == "raster_behind":
        behind: BehindStyle = style.behind
        is_key = word.kind == "highlight"
        return (behind.font_key if is_key else behind.font_plain,
                word.color or (behind.key_color if is_key else behind.plain_color),
                behind.key_alpha if is_key else behind.plain_alpha,
                behind.shadow_key if is_key else behind.shadow_plain,
                behind.skew if is_key else 0.0,
                True)
    stack: StackStyle = style.stack
    colour = word.color or (stack.accent_color if word.accent else stack.color)
    return stack.font, colour, 1.0, stack.shadow, 0.0, False


def render_word(word: Placed, style: Style, media: MediaInfo) -> tuple[Image.Image,
                                                                      tuple[int, int,
                                                                            int, int]]:
    """Рисува една дума върху прозрачен кадър в пълния размер на видеото.

    Позиционирането следва това на съответния рендерер, иначе изнесените
    слоеве нямаше да лягат там, където са в готовото видео:

    * стил A минава през libass, който при котва ``\\an7`` слага основната
      линия на ``usWinAscent`` под подадената точка — тук се пресмята
      същото и текстът се рисува по основна линия;
    * стил B рисува центрирано и с наклон, точно както прави рендерерът.

    Връща и правоъгълника, който думата заема.
    """
    name, colour, opacity, shadow, skew, centred = _word_settings(style, word)
    size = max(1, round(word.size))
    font = ImageFont.truetype(font_path(name), size)
    mask = Image.new("L", (media.width, media.height), 0)

    if centred:
        sprite = Sprite(word.text, font, skew)
        ink = sprite.mask
        x = round(word.x + word.width / 2.0 - ink.width / 2.0)
        y = round(word.y + word.height / 2.0 - ink.height / 2.0)
        mask.paste(ink, (x, y))
    else:
        from .textmetrics import measurer

        baseline = word.y + measurer(name).win_ascent_ratio * word.size
        ImageDraw.Draw(mask).text((round(word.x), round(baseline)), word.text,
                                  font=font, fill=255, anchor="ls")

    box = mask.getbbox() or (0, 0, 1, 1)
    canvas = Image.new("RGBA", (media.width, media.height), (0, 0, 0, 0))

    if shadow.alpha > 0:
        offset = Image.new("L", mask.size, 0)
        offset.paste(mask, (round(shadow.dx * media.height),
                            round(shadow.dy * media.height)))
        radius = shadow.blur * media.height
        if radius > 0:
            offset = offset.filter(ImageFilter.GaussianBlur(radius))
        composite(canvas, offset, hex_rgb(shadow.color), shadow.alpha, 0, 0)
    composite(canvas, mask, hex_rgb(colour), opacity, 0, 0)
    return canvas, (box[0], box[1], box[2] - box[0], box[3] - box[1])


def export_words(layouts: list[BlockLayout], style: Style, media: MediaInfo,
                 destination: Path, fmt: str = "png",
                 background: str | None = None,
                 progress: Callable[[str], None] = print) -> list[ExportedWord]:
    """Изнася всяка дума като отделен файл и описва резултата."""
    if fmt not in FORMATS:
        raise ValueError(f"непознат формат {fmt!r}; налични: {', '.join(FORMATS)}")
    plate = resolve_background(background)

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    exported: list[ExportedWord] = []
    index = 0
    for layout in layouts:
        for word in layout.placed:
            index += 1
            image, box = render_word(word, style, media)
            image = flatten(image, plate)
            name = safe_name(word.text, index, fmt)
            save_layer(image, destination / name, fmt)
            exported.append(ExportedWord(
                index=index, text=word.text, start=word.visible_from,
                end=word.hidden_after, spoken_start=word.start, spoken_end=word.end,
                file=name, x=box[0], y=box[1], width=box[2], height=box[3]))
            if index % 10 == 0:
                progress(f"  {index} думи …")

    write_manifest(exported, style, media, destination, fmt, plate)
    progress(f"{len(exported)} думи в {destination}")
    return exported


def write_manifest(words: list[ExportedWord], style: Style, media: MediaInfo,
                   destination: Path, fmt: str = "png",
                   plate: tuple[int, int, int] | None = None) -> None:
    """Описанието: JSON за програми, CSV за човек с таблица."""
    note = ("Всеки файл е в пълния размер на кадъра, тоест се внася както е "
            "и ляга на мястото си. Времената са в секунди от началото на "
            "видеото: start/end е докато думата стои на екрана, "
            "spoken_start/spoken_end е кога се изрича.")
    if plate is not None:
        note += (" Фонът е плътен: слоевете се ползват един по един с "
                 "хромакей, а не се наслагват — един върху друг ще се закрият.")

    data = {
        "style": style.name,
        "format": fmt,
        "background": ("#%02X%02X%02X" % plate) if plate else "прозрачен",
        "video": {"width": media.width, "height": media.height, "fps": media.fps},
        "note": note,
        "words": [word.to_json() for word in words],
    }
    (destination / "layers.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    with open(destination / "layers.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["№", "дума", "на екрана от", "до", "времетраене",
                         "изрича се от", "до", "файл"])
        for word in words:
            writer.writerow([word.index, word.text, f"{word.start:.3f}",
                             f"{word.end:.3f}", f"{word.end - word.start:.3f}",
                             f"{word.spoken_start:.3f}", f"{word.spoken_end:.3f}",
                             word.file])
