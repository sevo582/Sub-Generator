"""Измерване на текст със същия TTF, който получава и рендерерът.

Позиционирането в стил A е абсолютно — за да сложим ред на точното място,
трябва да знаем колко е широк. Мярката идва от Pillow върху същия файл,
който подаваме на libass, така че разминаването остава в рамките на
единици пиксели (и се калибрира с ``StackStyle.ass_size_scale``).
"""

from __future__ import annotations

import functools
import os
from typing import Iterable

from PIL import ImageFont

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")


def font_path(name: str) -> str:
    """Приема име на вграден шрифт или директен път до TTF файл."""
    if os.path.isabs(name) or os.sep in name or (os.altsep and os.altsep in name):
        path = name
    else:
        path = os.path.join(FONTS_DIR, name)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"шрифтът {name!r} не е намерен ({path}). "
            f"Вградените са в {FONTS_DIR}; може да подадеш и пълен път."
        )
    return path


class Measurer:
    """Кешира ``ImageFont`` по размер — иначе всяка дума отваря файла наново."""

    def __init__(self, path: str) -> None:
        self.path = path

    @functools.lru_cache(maxsize=64)
    def font(self, px: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(self.path, max(1, int(px)))

    def width(self, text: str, px: float) -> float:
        """Ширина на пробега (advance), не на мастилото.

        За подреждане на редове ни трябва advance — така интервалите между
        думите излизат същите, каквито ги смята и libass.
        """
        return float(self.font(round(px)).getlength(text))

    def ink_box(self, text: str, px: float) -> tuple[int, int, int, int]:
        """Правоъгълникът на мастилото спрямо позицията на изписване."""
        return self.font(round(px)).getbbox(text)

    def ascent(self, px: float) -> float:
        return float(self.font(round(px)).getmetrics()[0])

    def line_height(self, px: float) -> float:
        ascent, descent = self.font(round(px)).getmetrics()
        return float(ascent + descent)

    @functools.cached_property
    def ass_size_factor(self) -> float:
        """С колко да умножим желания em, за да получим ``\\fs``.

        ASS наследява семантиката на VSFilter: размерът на шрифта е
        **височината на реда**, тоест ``usWinAscent + usWinDescent``, а не
        размерът на em-а. Pillow работи с em. За Montserrat ExtraBold
        разликата е 1.56x — достатъчно, за да изглежда всичко смалено, ако
        подадем измереното наум.

        Стойността се чете от самия файл, вместо да е константа, за да е
        вярна и когато някой смени шрифта.
        """
        from fontTools.ttLib import TTFont

        font = TTFont(self.path)
        upem = font["head"].unitsPerEm
        os2 = font["OS/2"]
        line = os2.usWinAscent + os2.usWinDescent
        return line / upem if upem else 1.0

    @functools.cached_property
    def win_ascent_ratio(self) -> float:
        """``usWinAscent`` в дялове от em — разстоянието от горния ръб на
        реда (котва ``\\an7``) до основната линия."""
        from fontTools.ttLib import TTFont

        font = TTFont(self.path)
        return font["OS/2"].usWinAscent / font["head"].unitsPerEm

    def missing_glyphs(self, texts: Iterable[str]) -> set[str]:
        """Знаци, които шрифтът не покрива — за ранно предупреждение."""
        from fontTools.ttLib import TTFont  # локален внос: нужен е само тук

        cmap = TTFont(self.path).getBestCmap()
        missing: set[str] = set()
        for text in texts:
            for char in text:
                if char.isspace():
                    continue
                if ord(char) not in cmap:
                    missing.add(char)
        return missing


@functools.lru_cache(maxsize=8)
def measurer(name: str) -> Measurer:
    return Measurer(font_path(name))
