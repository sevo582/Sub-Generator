"""Общи неща за рисуването с Pillow.

Отделени от рендерера на стил B, защото ги ползва и износът на отделни думи
за редактор. Тук няма нищо за конкретен стил.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .styles import ShadowSpec

#: Запас около мастилото, за да има място размазването на сянката.
PAD = 8

#: Допълнителен запас около правоъгълника, в който има текст. Покрива
#: размазването на сянката и закръгленията при мащабиране.
BBOX_MARGIN = 24


def hex_rgb(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"цветът трябва да е #RRGGBB, а не {value!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def ease_out(progress: float) -> float:
    """Кубично забавяне: бързо в началото, спира плавно. Без подскок."""
    clamped = min(1.0, max(0.0, progress))
    return 1.0 - (1.0 - clamped) ** 3


def entry_phase(time: float, start: float, milliseconds: int) -> float:
    """Докъде е стигнала входната анимация: 0 при започване, 1 при край.

    Преди ``start`` връща 1 — думата стои в покой, докато не ѝ дойде редът.
    Така двата рендерера се държат еднакво: анимацията се пуска в момента,
    в който думата се изрича, а не когато се появи на екрана.
    """
    if milliseconds <= 0 or time < start:
        return 1.0
    return ease_out((time - start) / (milliseconds / 1000.0))


class Sprite:
    """Маската на една дума, нарисувана веднъж и преоразмерявана после.

    Маската се рисува в най-големия размер, който думата ще достигне, и
    после само се смалява — така ръбовете остават чисти през цялата
    анимация вместо да се раздуват от увеличаване.
    """

    def __init__(self, text: str, font: ImageFont.FreeTypeFont, skew: float) -> None:
        left, top, right, bottom = font.getbbox(text)
        width = max(1, right - left) + 2 * PAD
        height = max(1, bottom - top) + 2 * PAD
        shear = abs(skew) * height
        canvas = Image.new("L", (width + int(shear) + 1, height), 0)
        ImageDraw.Draw(canvas).text((PAD - left + shear, PAD - top), text, font=font, fill=255)
        if skew:
            # Синтетичен курсив: наклон около долния ръб, за да не „подскочи".
            canvas = canvas.transform(
                canvas.size, Image.Transform.AFFINE,
                (1, skew, -skew * canvas.height, 0, 1, 0),
                resample=Image.Resampling.BICUBIC,
            )
        self.mask = canvas.crop(canvas.getbbox() or (0, 0, 1, 1))

        self._cache: dict[int, Image.Image] = {}

    def scaled(self, width: int) -> Image.Image:
        """Маската, смалена до дадена ширина. Резултатите се кешират —
        мащабът се движи плавно, но закръглен до цял пиксел се повтаря."""
        if width <= 0 or width == self.mask.width:
            return self.mask
        cached = self._cache.get(width)
        if cached is None:
            height = max(1, round(self.mask.height * width / self.mask.width))
            cached = self.mask.resize((width, height), Image.Resampling.LANCZOS)
            self._cache[width] = cached
        return cached


def blur_shadow(mask: Image.Image, spec: ShadowSpec, height: int) -> Image.Image:
    radius = spec.blur * height
    if radius <= 0:
        return mask
    pad = int(radius * 3) + 2
    padded = Image.new("L", (mask.width + 2 * pad, mask.height + 2 * pad), 0)
    padded.paste(mask, (pad, pad))
    return padded.filter(ImageFilter.GaussianBlur(radius))


def composite(base: Image.Image, mask: Image.Image, colour: tuple[int, int, int],
               opacity: float, x: int, y: int) -> None:
    """Слага оцветена маска върху кадъра с коректно „over" смесване.

    Наивното ``paste`` с маска смесва RGB канала с прозрачния черен фон и
    оставя тъмен ореол около буквите. Затова кадърът се композира само в
    правоъгълника на думата, но през ``alpha_composite``.
    """
    if opacity < 1.0:
        mask = mask.point(lambda value: int(value * opacity))
    sprite = Image.new("RGBA", mask.size, colour + (0,))
    sprite.putalpha(mask)
    box = (x, y, x + mask.width, y + mask.height)
    if box[2] <= 0 or box[3] <= 0 or box[0] >= base.width or box[1] >= base.height:
        return
    region = base.crop(box)
    base.paste(Image.alpha_composite(region, sprite), box)


