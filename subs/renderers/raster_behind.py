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

from PIL import Image, ImageFont

from ..burn import (LAYER_SUFFIX, MediaInfo, build_raster_command, ensure_parent,
                    extract_frame, pipe_frames, verify_alpha)
from ..layout import deoverlap, layout_behind
from ..raster import (BBOX_MARGIN, Sprite, blur_shadow, composite, ease_out,
                      entry_phase, hex_rgb)
from ..models import BlockLayout, Placed
from ..styles import BehindStyle
from ..textmetrics import font_path
from .base import RenderRequest, RenderResult, Renderer, preview_path, register

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

    @staticmethod
    def _bbox(layouts: list[BlockLayout], style: BehindStyle,
              media: MediaInfo) -> tuple[int, int, int, int]:
        """Правоъгълникът, в който изобщо може да се появи текст.

        Смята се веднъж за цялото видео и определя колко голям кадър се
        подава на ffmpeg. Взима предвид най-големия мащаб, който думата ще
        достигне, и отместването на „издигане", иначе краищата се режат.
        """
        left = top = float("inf")
        right = bottom = float("-inf")
        rise = style.rise_by * media.height
        for layout in layouts:
            for word in layout.placed:
                grow = style.scale_end if word.kind == "highlight" else 1.0
                half_w = word.width * grow / 2.0
                half_h = word.height * grow / 2.0
                centre_x = word.x + word.width / 2.0
                centre_y = word.y + word.height / 2.0
                left = min(left, centre_x - half_w)
                right = max(right, centre_x + half_w)
                top = min(top, centre_y - half_h - rise)
                bottom = max(bottom, centre_y + half_h + rise)
        if left > right:  # няма нито една дума
            return 0, 0, media.width, media.height

        left = max(0, int(left) - BBOX_MARGIN)
        top = max(0, int(top) - BBOX_MARGIN)
        right = min(media.width, int(right) + BBOX_MARGIN + 1)
        bottom = min(media.height, int(bottom) + BBOX_MARGIN + 1)
        # Четни размери и отмествания: кодеците и overlay се държат
        # предвидимо само така.
        left -= left % 2
        top -= top % 2
        width = min(media.width - left, (right - left + 1) // 2 * 2)
        height = min(media.height - top, (bottom - top + 1) // 2 * 2)
        return left, top, max(2, width), max(2, height)

    def _sprites(self, layouts: list[BlockLayout], style: BehindStyle) -> dict[int, Sprite]:
        """Една маска на дума, в максималния ѝ размер."""
        key_path = font_path(style.font_key)
        plain_path = font_path(style.font_plain)
        sprites: dict[int, Sprite] = {}
        for layout in layouts:
            for word in layout.placed:
                is_key = word.kind == "highlight"
                # Маската се рисува в най-големия размер, който думата ще
                # достигне, и после само се смалява — така ръбовете остават
                # чисти вместо да се раздуват от увеличаване.
                size = word.size * (style.scale_end if is_key else 1.0)
                font = ImageFont.truetype(key_path if is_key else plain_path,
                                          max(1, round(size)))
                skew = style.skew if is_key else 0.0
                sprites[id(word)] = Sprite(word.text, font, skew)
        return sprites

    def _draw_frame(self, time: float, layouts: list[BlockLayout],
                    sprites: dict[int, Sprite], style: BehindStyle,
                    media: MediaInfo,
                    region: tuple[int, int, int, int] | None = None
                    ) -> Image.Image | None:
        """Кадърът на слоя, или None ако в този момент няма нищо за рисуване.

        ``region`` (x, y, ширина, височина) рисува само в изрязания
        правоъгълник; координатите на думите се отместват съответно.
        """
        active = [layout for layout in layouts if layout.appear <= time < layout.disappear]
        if not active:
            return None

        origin_x, origin_y, width, height = region or (0, 0, media.width, media.height)
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        drew = False
        for layout in active:
            for word in layout.placed:
                if not (word.visible_from <= time < word.hidden_after):
                    continue
                self._draw_word(canvas, time, word, sprites[id(word)], style, media,
                                origin_x, origin_y)
                drew = True
        return canvas if drew else None

    def _draw_word(self, canvas: Image.Image, time: float, word: Placed,
                   sprite: Sprite, style: BehindStyle, media: MediaInfo,
                   origin_x: int = 0, origin_y: int = 0) -> None:
        is_key = word.kind == "highlight"

        if is_key:
            # Ключовата дума расте плавно през целия си живот на екрана —
            # това е самата същност на стила, не входна анимация.
            span = max(1e-6, word.hidden_after - word.visible_from)
            phase = ease_out((time - word.visible_from) / span)
            scale = style.scale_start + (style.scale_end - style.scale_start) * phase
        else:
            scale = 1.0

        # Входна анимация на отделната дума, върху горното.
        offset_y = 0.0
        alpha_factor = 1.0
        if word.animation == "изскачане":
            phase = entry_phase(time, word.start, style.pop_ms)
            scale *= style.pop_from + (1.0 - style.pop_from) * phase
        elif word.animation == "издигане":
            phase = entry_phase(time, word.start, style.rise_ms)
            offset_y = style.rise_by * media.height * (1.0 - phase)
        elif word.animation == "избледняване":
            alpha_factor = entry_phase(time, word.start, style.fade_in_ms)

        # Маската е нарисувана в максималния размер и се смалява до текущия.
        reference = style.scale_end if is_key else 1.0
        target = max(1, round(sprite.mask.width * scale / reference))
        mask = sprite.scaled(target)

        # Центърът стои на място, растежът изтласква краищата извън кадъра.
        centre_x = word.x + word.width / 2.0
        centre_y = word.y + word.height / 2.0 + offset_y
        x = round(centre_x - mask.width / 2.0) - origin_x
        y = round(centre_y - mask.height / 2.0) - origin_y

        # Собственият цвят на думата бие цвета на стила.
        default = style.key_color if is_key else style.plain_color
        colour = hex_rgb(word.color or default)
        opacity = (style.key_alpha if is_key else style.plain_alpha) * alpha_factor
        shadow = style.shadow_key if is_key else style.shadow_plain

        if shadow.alpha > 0:
            blurred = blur_shadow(mask, shadow, media.height)
            pad_x = (blurred.width - mask.width) // 2
            pad_y = (blurred.height - mask.height) // 2
            composite(canvas, blurred, hex_rgb(shadow.color), shadow.alpha * alpha_factor,
                       x - pad_x + round(shadow.dx * media.height),
                       y - pad_y + round(shadow.dy * media.height))
        composite(canvas, mask, colour, opacity, x, y)

    def _frames(self, layouts: list[BlockLayout], sprites: dict[int, Sprite],
                style: BehindStyle, media: MediaInfo,
                segment: tuple[float, float] | None = None,
                fps: float | None = None,
                region: tuple[int, int, int, int] | None = None) -> Iterator[bytes]:
        """Кадрите на слоя.

        При зададен сегмент се рисуват само тези във времевия прозорец, но
        с истинските времена — иначе анимациите биха тръгнали отначало и
        прегледът щеше да лъже.
        """
        size = (region[2], region[3]) if region else (media.width, media.height)
        blank = Image.new("RGBA", size, (0, 0, 0, 0)).tobytes()
        rate = fps or media.fps
        offset = segment[0] if segment else 0.0
        count = (max(1, round(segment[1] * rate)) if segment else media.frame_count)
        for index in range(count):
            frame = self._draw_frame(offset + index / rate, layouts, sprites, style,
                                     media, region)
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

        region = self._bbox(layouts, style, media)
        command = build_raster_command(request.source, media, request.output, layer,
                                       layer_format=request.layer_format,
                                       crf=request.crf, preset=request.preset,
                                       segment=request.segment,
                                       scale_height=request.scale_height,
                                       fps=request.fps,
                                       overlay_size=(region[2], region[3]),
                                       overlay_origin=(region[0], region[1]))
        result.commands.append(command)
        if request.dry_run:
            return result

        for path in (request.output, layer):
            if path is not None:
                ensure_parent(path)

        sprites = self._sprites(layouts, style)
        rate = request.fps or media.fps
        total = (max(1, round(request.segment[1] * rate)) if request.segment
                 else media.frame_count)
        state = {"last": -1}

        def report(index: int) -> None:
            percent = int(index * 100 / max(1, total))
            if percent // 10 != state["last"] // 10 or index >= total:
                state["last"] = percent
                request.progress(f"  кадър {index}/{total} ({percent}%)")

        request.progress(f"рисувам {total} кадъра …")
        saved = 1.0 - (region[2] * region[3]) / (media.width * media.height)
        request.progress(f"  слоят е {region[2]}x{region[3]} вместо "
                         f"{media.width}x{media.height} ({saved:.0%} по-малко през тръбата)")
        pipe_frames(command, self._frames(layouts, sprites, style, media,
                                          request.segment, request.fps, region), report)

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
