"""Стил B („Зад кадъра") през растеризация на кадри.

ASS отпада тук по три причини, всяка от които е достатъчна: не може да
изнесе слой с алфа канал; полупрозрачната заливка, през която прозира фонът,
не е контролируема с libass; а плавното мащабиране през целия живот на
думата в комбинация с горните две няма как да се сглоби.

Затова всеки кадър се рисува с Pillow и се подава на ffmpeg **през pipe**,
а не като PNG поредица — иначе на диска се изсипват десетки хиляди файлове.

Двата изхода (готово видео и слой с прозрачност) излизат от един и същи
поток кадри, разклонен с ``split`` вътре в ffmpeg — рисуваме всеки кадър
веднъж, независимо колко изхода се искат.

Важно за слоя: полупрозрачността на ключовата дума е **запечена в алфа
канала**, а не наложена като непрозрачност на целия слой. Иначе при
композиране в редактора белите думи също щяха да избледнеят.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Iterator

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..burn import (LAYER_SUFFIX, MediaInfo, build_raster_command, ensure_parent,
                    extract_frame, pipe_frames, verify_alpha)
from ..layout import deoverlap, layout_behind
from ..models import BlockLayout, Placed
from ..styles import BehindStyle, ShadowSpec
from ..textmetrics import font_path
from .base import RenderRequest, RenderResult, Renderer, preview_path, register

#: Запас около мастилото, за да има място размазването на сянката.
PAD = 8


def hex_rgb(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"цветът трябва да е #RRGGBB, а не {value!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def ease_out(progress: float) -> float:
    """Кубично забавяне: бързо в началото, спира плавно. Без подскок."""
    clamped = min(1.0, max(0.0, progress))
    return 1.0 - (1.0 - clamped) ** 3


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


def _blur_shadow(mask: Image.Image, spec: ShadowSpec, height: int) -> Image.Image:
    radius = spec.blur * height
    if radius <= 0:
        return mask
    pad = int(radius * 3) + 2
    padded = Image.new("L", (mask.width + 2 * pad, mask.height + 2 * pad), 0)
    padded.paste(mask, (pad, pad))
    return padded.filter(ImageFilter.GaussianBlur(radius))


def _composite(base: Image.Image, mask: Image.Image, colour: tuple[int, int, int],
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


@register
class RasterBehindRenderer(Renderer):
    name = "raster_behind"
    supports_layer = True

    # ------------------------------------------------------------------
    # Оформление
    # ------------------------------------------------------------------

    def layout(self, request: RenderRequest) -> list[BlockLayout]:
        from ..textmetrics import measurer

        style = request.style.behind
        layouts = [
            layout_behind(block, style, measurer(style.font_key),
                          measurer(style.font_plain),
                          request.media.width, request.media.height)
            for block in request.blocks
        ]
        return deoverlap(layouts)

    # ------------------------------------------------------------------
    # Рисуване
    # ------------------------------------------------------------------

    def _sprites(self, layouts: list[BlockLayout], style: BehindStyle) -> dict[int, Sprite]:
        """Една маска на дума, в максималния ѝ размер."""
        key_path = font_path(style.font_key)
        plain_path = font_path(style.font_plain)
        sprites: dict[int, Sprite] = {}
        for layout in layouts:
            for word in layout.placed:
                is_key = word.kind == "highlight"
                size = word.size * (style.scale_end if is_key else 1.0)
                font = ImageFont.truetype(key_path if is_key else plain_path,
                                          max(1, round(size)))
                skew = style.skew if is_key else 0.0
                sprites[id(word)] = Sprite(word.text, font, skew)
        return sprites

    def _draw_frame(self, time: float, layouts: list[BlockLayout],
                    sprites: dict[int, Sprite], style: BehindStyle,
                    media: MediaInfo) -> Image.Image | None:
        active = [layout for layout in layouts if layout.appear <= time < layout.disappear]
        if not active:
            return None

        canvas = Image.new("RGBA", (media.width, media.height), (0, 0, 0, 0))
        drew = False
        for layout in active:
            for word in layout.placed:
                if not (word.visible_from <= time < word.hidden_after):
                    continue
                self._draw_word(canvas, time, word, sprites[id(word)], style, media)
                drew = True
        return canvas if drew else None

    def _draw_word(self, canvas: Image.Image, time: float, word: Placed,
                   sprite: Sprite, style: BehindStyle, media: MediaInfo) -> None:
        is_key = word.kind == "highlight"
        if is_key:
            # Мащабът расте плавно през целия живот на думата на екрана.
            span = max(1e-6, word.hidden_after - word.visible_from)
            phase = ease_out((time - word.visible_from) / span)
            scale = style.scale_start + (style.scale_end - style.scale_start) * phase
            # Маската е нарисувана в максималния размер и се смалява до текущия.
            target = max(1, round(sprite.mask.width * scale / style.scale_end))
        else:
            target = sprite.mask.width
        mask = sprite.scaled(target)

        # Центърът стои на място, растежът изтласква краищата извън кадъра.
        centre_x = word.x + word.width / 2.0
        centre_y = word.y + word.height / 2.0
        x = round(centre_x - mask.width / 2.0)
        y = round(centre_y - mask.height / 2.0)

        colour = hex_rgb(style.key_color if is_key else style.plain_color)
        opacity = style.key_alpha if is_key else style.plain_alpha
        shadow = style.shadow_key if is_key else style.shadow_plain

        if shadow.alpha > 0:
            blurred = _blur_shadow(mask, shadow, media.height)
            pad_x = (blurred.width - mask.width) // 2
            pad_y = (blurred.height - mask.height) // 2
            _composite(canvas, blurred, hex_rgb(shadow.color), shadow.alpha,
                       x - pad_x + round(shadow.dx * media.height),
                       y - pad_y + round(shadow.dy * media.height))
        _composite(canvas, mask, colour, opacity, x, y)

    def _frames(self, layouts: list[BlockLayout], sprites: dict[int, Sprite],
                style: BehindStyle, media: MediaInfo) -> Iterator[bytes]:
        blank = Image.new("RGBA", (media.width, media.height), (0, 0, 0, 0)).tobytes()
        for index in range(media.frame_count):
            frame = self._draw_frame(index / media.fps, layouts, sprites, style, media)
            yield blank if frame is None else frame.tobytes()

    # ------------------------------------------------------------------
    # Преглед на един кадър
    # ------------------------------------------------------------------

    def preview(self, request: RenderRequest, layouts: list[BlockLayout]) -> RenderResult:
        """Един кадър, съставен по същата сметка както при пълния рендер.

        Наслагването върху кадъра се прави тук с Pillow, а не с ffmpeg —
        и двете са обикновено „over" смесване, така че резултатът е същият,
        но без да пускаме кодек за един-единствен PNG.
        """
        style = request.style.behind
        media = request.media
        sprites = self._sprites(layouts, style)
        result = RenderResult(layouts=layouts)
        layer_only = request.output is None

        temp = Path(tempfile.mkdtemp(prefix="subs-preview-"))
        try:
            for time in request.preview_times:
                overlay = self._draw_frame(time, layouts, sprites, style, media)
                if overlay is None:
                    overlay = Image.new("RGBA", (media.width, media.height), (0, 0, 0, 0))
                    result.notes.append(f"на {time:.2f} s няма текст на екрана")

                if layer_only:
                    frame = overlay
                else:
                    source_png = temp / f"src-{time:.3f}.png"
                    extract_frame(request.source, time, source_png)
                    background = Image.open(source_png).convert("RGBA")
                    if background.size != overlay.size:
                        # Стига се дотук само ако ffprobe и ffmpeg се
                        # разминават за размера — най-често при завъртян
                        # материал. Казваме го, вместо PIL да изсипе
                        # "images do not match".
                        raise ValueError(
                            f"кадърът от видеото е {background.size[0]}x"
                            f"{background.size[1]}, а слоят "
                            f"{overlay.size[0]}x{overlay.size[1]}. "
                            "Ако видеото е завъртяно, това е бъг в subs — "
                            "прати изхода на: ffprobe -show_streams видео"
                        )
                    frame = Image.alpha_composite(background, overlay)

                destination = preview_path(request, time)
                ensure_parent(destination)
                frame.save(destination)
                result.outputs.append(destination)
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        return result

    # ------------------------------------------------------------------
    # Рендиране
    # ------------------------------------------------------------------

    def render(self, request: RenderRequest, layouts: list[BlockLayout]) -> RenderResult:
        style = request.style.behind
        media = request.media
        result = RenderResult(layouts=layouts)

        layer = request.layer
        if layer is not None:
            expected = LAYER_SUFFIX[request.layer_format]
            if layer.suffix.lower() != expected:
                layer = layer.with_suffix(expected)
                result.notes.append(f"разширението на слоя е сменено на {expected}")

        command = build_raster_command(request.source, media, request.output, layer,
                                       layer_format=request.layer_format,
                                       crf=request.crf, preset=request.preset)
        result.commands.append(command)
        if request.dry_run:
            return result

        for path in (request.output, layer):
            if path is not None:
                ensure_parent(path)

        sprites = self._sprites(layouts, style)
        total = media.frame_count
        state = {"last": -1}

        def report(index: int) -> None:
            percent = int(index * 100 / max(1, total))
            if percent // 10 != state["last"] // 10 or index >= total:
                state["last"] = percent
                request.progress(f"  кадър {index}/{total} ({percent}%)")

        request.progress(f"рисувам {total} кадъра …")
        pipe_frames(command, self._frames(layouts, sprites, style, media), report)

        if request.output is not None:
            result.outputs.append(request.output)
        if layer is not None:
            result.outputs.append(layer)
            ok, detail = verify_alpha(layer)
            if ok:
                result.notes.append(f"алфа каналът е наред ({detail})")
            else:
                result.notes.append(
                    f"ВНИМАНИЕ: слоят изглежда без алфа канал ({detail}) — "
                    "фонът ще излезе черен в редактора"
                )
        return result
