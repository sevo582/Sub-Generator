"""Стил A („Стълб") през ASS субтитри.

Защо ASS, а не рендиране на кадри: блокът е статичен. Позициите се смятат
веднъж, а през времето се мени само непрозрачността на отделните думи. Точно
за това libass е направен — един ffmpeg проход вместо десетки хиляди PNG-та.

Ключовите решения във формата:

``\\an7`` + ``\\pos``
    Котва в горния ляв ъгъл прави абсолютното позициониране предвидимо;
    иначе libass центрира спрямо размера на реда и сметките се разминават.
``PlayResX/Y`` = реалните размери на видеото
    Така ``\\fs`` е в пиксели от кадъра и съвпада с измереното с PIL.
Два реда на дума вместо ``Outline``
    Мека сянка се прави с ``\\blur`` върху черно копие на текста един слой
    отдолу. Ако сложим ``\\blur`` направо върху бялата дума, ще размажем и
    нея; ``\\bord`` пък дава дебел контур, който е съвсем друго усещане.
Две събития на дума вместо преиздаване на блока
    Състоянието се сменя точно веднъж (избледняла → плътна), затова са
    достатъчни две ``Dialogue`` реплики, а не по една за всеки момент.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from ..burn import burn_ass, ensure_parent
from ..layout import deoverlap, layout_stack
from ..models import BlockLayout, Placed
from ..styles import ShadowSpec, StackStyle
from ..textmetrics import font_path, measurer
from .base import RenderRequest, RenderResult, Renderer, register

ASS_HEADER = """[Script Info]
; Генерирано от subs — не редактирай на ръка, ще се презапише.
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: {width}
PlayResY: {height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Base,{font},{base_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def timestamp(seconds: float) -> str:
    """ASS иска H:MM:SS.cc — центисекунди, един знак за часа."""
    seconds = max(0.0, seconds)
    centis = int(round(seconds * 100))
    hours, centis = divmod(centis, 360000)
    minutes, centis = divmod(centis, 6000)
    secs, centis = divmod(centis, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centis:02d}"


def ass_colour(hex_colour: str) -> str:
    """``#RRGGBB`` -> ``&HBBGGRR&`` (ASS обръща реда на каналите)."""
    value = hex_colour.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"цветът трябва да е #RRGGBB, а не {hex_colour!r}")
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H{blue}{green}{red}&".upper()


def ass_alpha(opacity: float) -> str:
    """Непрозрачност 0..1 -> ``&HAA&`` (в ASS 00 е плътно, FF е невидимо)."""
    clamped = min(1.0, max(0.0, opacity))
    return f"&H{int(round((1.0 - clamped) * 255)):02X}&"


@register
class AssStackRenderer(Renderer):
    name = "ass_stack"
    supports_layer = False

    # ------------------------------------------------------------------
    # Оформление
    # ------------------------------------------------------------------

    def layout(self, request: RenderRequest) -> list[BlockLayout]:
        style = request.style
        stack = style.stack
        metrics = measurer(stack.font)
        layouts = [
            layout_stack(block, stack, metrics, request.media.width, request.media.height)
            for block in request.blocks
        ]
        return deoverlap(layouts)

    # ------------------------------------------------------------------
    # Генериране на .ass
    # ------------------------------------------------------------------

    def build_ass(self, layouts: list[BlockLayout], style: StackStyle,
                  width: int, height: int) -> str:
        # libass наследява от VSFilter семантиката, че размерът на шрифта е
        # височината на реда (winAscent + winDescent), а не размерът на em-а.
        # Pillow мери в em. Без този множител всичко излиза с около една
        # трета по-дребно от изчисленото. Стойността се чете от самия TTF.
        factor = measurer(style.font).ass_size_factor * style.ass_size_scale
        lines = [ASS_HEADER.format(
            width=width, height=height,
            font=style.font_family,
            base_size=round(style.size_normal * height * factor),
        )]
        for layout in layouts:
            for word in layout.placed:
                lines.extend(self._events(word, style, height, factor))
        return "".join(lines)

    def _events(self, word: Placed, style: StackStyle, height: int,
                factor: float) -> list[str]:
        size = word.size * factor
        colour = ass_colour(style.accent_color if word.accent else style.color)
        shadow = style.shadow

        events: list[str] = []
        dim_start = word.visible_from
        dim_end = min(word.start, word.hidden_after)
        solid_start = max(word.start, word.visible_from)
        solid_end = word.hidden_after

        if dim_end - dim_start > 0.01:
            events += self._pair(word, size, colour, style.dim_alpha, dim_start, dim_end,
                                 shadow, height, fade_from=None)
        if solid_end - solid_start > 0.01:
            events += self._pair(word, size, colour, 1.0, solid_start, solid_end,
                                 shadow, height,
                                 fade_from=style.dim_alpha if style.fade_ms > 0 else None,
                                 fade_ms=style.fade_ms)
        return events

    def _pair(self, word: Placed, size: float, colour: str, opacity: float,
              start: float, end: float, shadow: ShadowSpec, height: int,
              fade_from: float | None, fade_ms: int = 0) -> list[str]:
        """Едно събитие за сянката (слой 0) и едно за текста (слой 1)."""
        text = escape(word.text)
        common = f"\\an7\\bord0\\shad0\\fs{size:.1f}"

        def transition(base: float) -> str:
            if fade_from is None:
                return f"\\alpha{ass_alpha(base)}"
            return (f"\\alpha{ass_alpha(base * fade_from)}"
                    f"\\t(0,{fade_ms},\\alpha{ass_alpha(base)})")

        blur = shadow.blur * height
        shadow_tags = (
            f"{common}\\pos({word.x + shadow.dx * height:.1f},"
            f"{word.y + shadow.dy * height:.1f})"
            f"\\1c{ass_colour(shadow.color)}\\blur{blur:.1f}"
            f"{transition(opacity * shadow.alpha)}"
        )
        text_tags = f"{common}\\pos({word.x:.1f},{word.y:.1f})\\1c{colour}{transition(opacity)}"

        stamp = f"{timestamp(start)},{timestamp(end)}"
        return [
            f"Dialogue: 0,{stamp},Base,,0,0,0,,{{{shadow_tags}}}{text}\n",
            f"Dialogue: 1,{stamp},Base,,0,0,0,,{{{text_tags}}}{text}\n",
        ]

    # ------------------------------------------------------------------
    # Рендиране
    # ------------------------------------------------------------------

    def render(self, request: RenderRequest, layouts: list[BlockLayout]) -> RenderResult:
        if request.output is None:
            raise ValueError("стил със субтитри произвежда само готово видео — липсва изход")

        style = request.style.stack
        content = self.build_ass(layouts, style, request.media.width, request.media.height)
        result = RenderResult(layouts=layouts)

        workdir = Path(request.keep_dir) if request.keep_dir else Path(
            tempfile.mkdtemp(prefix="subs-ass-"))
        workdir.mkdir(parents=True, exist_ok=True)
        fonts_dir = workdir / "fonts"
        fonts_dir.mkdir(exist_ok=True)
        shutil.copy2(font_path(style.font), fonts_dir / os.path.basename(style.font))
        ass_file = workdir / "subs.ass"
        ass_file.write_text(content, encoding="utf-8")
        result.notes.append(f"ASS: {len(content.splitlines()) - 15} събития")

        try:
            command = [
                "ffmpeg", "-i", str(request.source),
                "-vf", "ass=f=subs.ass:fontsdir=fonts", "...", str(request.output),
            ]
            result.commands.append(command)
            if request.dry_run:
                result.notes.append(f"--dry-run: .ass остана в {workdir}")
                return result

            ensure_parent(request.output)
            request.progress(f"вграждам субтитрите в {request.output.name} …")
            burn_ass(request.source, workdir, "subs.ass", "fonts", request.output,
                     crf=request.crf, preset=request.preset)
            result.outputs.append(request.output)
        finally:
            if request.keep_dir is None and not request.dry_run:
                shutil.rmtree(workdir, ignore_errors=True)
        return result


def escape(text: str) -> str:
    """ASS третира ``{``, ``}`` и ``\\`` като служебни."""
    return (text.replace("\\", "\\\\")
                .replace("{", "\\{")
                .replace("}", "\\}")
                .replace("\n", " "))
