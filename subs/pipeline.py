"""Свързва трите стъпки: транскрипция → оформление → рендиране.

Отделен модул от CLI-то, за да може конвейерът да се вика и от код.
"""

from __future__ import annotations

import json
import locale
from pathlib import Path
from typing import Callable

from .burn import MediaInfo, probe
from .layout import assign_highlights, split_blocks
from .models import Block, Transcript
from .renderers import RenderRequest, RenderResult, get_renderer
from .styles import Style
from .textmetrics import measurer


def read_text(path: str | Path) -> tuple[str, str | None]:
    """Чете текстов файл, писан на Windows, и връща (съдържание, бележка).

    Този файл е направен да се редактира на ръка, при това предимно на
    Windows, където има два капана:

    * Notepad записва UTF-8 **с BOM**. ``utf-8`` гърми на първия знак,
      ``utf-8-sig`` чете и с, и без.
    * ``Set-Content`` в Windows PowerShell 5.1 записва в кодовата таблица
      на системата (на български Windows това е cp1251), а не в UTF-8.

    Затова се пробва UTF-8, а при неуспех — таблицата на системата. Вторият
    случай връща бележка, а не мълчи: разчитането с чужда таблица е
    предположение и потребителят трябва да го знае.
    """
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        pass

    fallback = locale.getpreferredencoding(False)
    try:
        text = data.decode(fallback)
    except (UnicodeDecodeError, LookupError):
        raise ValueError(
            f"{path} не е в UTF-8 и не се чете и с {fallback}. "
            "Запиши го наново като UTF-8. В PowerShell:\n"
            "    Get-Content файл.json | Set-Content нов.json -Encoding UTF8"
        ) from None
    return text, (
        f"{Path(path).name} не е в UTF-8; прочетен е с {fallback}. "
        "Провери, че буквите са верни, и го запиши наново с "
        "Set-Content -Encoding UTF8."
    )


def load_words(path: str | Path) -> Transcript:
    """Чете междинния JSON. Приема и обект с ``words``, и гол списък."""
    text, note = read_text(path)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} не е валиден JSON: {error}") from None
    transcript = Transcript.from_json(data)
    if note:
        transcript.notes.append(note)
    return transcript


def save_words(transcript: Transcript, path: str | Path) -> None:
    """Записва JSON-а четимо — той е за ръчна поправка, не за машина."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(transcript.to_json(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def build_blocks(transcript: Transcript, style: Style,
                 chooser: Callable[[Block], int | None] | None = None) -> list[Block]:
    """Общата част на конвейера — еднаква и за двата стила."""
    blocks = split_blocks(transcript.words, style.blocks)
    return assign_highlights(blocks, style.highlight, transcript.language, chooser)


def check_fonts(transcript: Transcript, style: Style,
                warn: Callable[[str], None]) -> None:
    """Ранно предупреждение за знаци, които шрифтът не покрива."""
    names = {style.stack.font} if style.renderer == "ass_stack" else {
        style.behind.font_key, style.behind.font_plain}
    texts = [w.text for w in transcript.words]
    for name in names:
        missing = measurer(name).missing_glyphs(texts)
        if missing:
            warn(f"шрифтът {name} не покрива: {''.join(sorted(missing))}")


def render(
    source: str | Path,
    transcript: Transcript,
    style: Style,
    output: Path | None = None,
    layer: Path | None = None,
    layer_format: str = "prores",
    crf: int = 18,
    preset: str = "medium",
    keep_dir: Path | None = None,
    dry_run: bool = False,
    progress: Callable[[str], None] = print,
    media: MediaInfo | None = None,
    blocks: list[Block] | None = None,
    preview_times: list[float] | None = None,
    preview_dir: Path | None = None,
    segment: tuple[float, float] | None = None,
    scale_height: int | None = None,
    fps: float | None = None,
) -> RenderResult:
    """Пуска рендерера, който стилът избира."""
    info = media or probe(source)
    if blocks is None:
        blocks = build_blocks(transcript, style)
    request = RenderRequest(
        source=Path(source),
        blocks=blocks,
        style=style,
        media=info,
        output=output,
        layer=layer,
        layer_format=layer_format,
        crf=crf,
        preset=preset,
        keep_dir=keep_dir,
        dry_run=dry_run,
        progress=progress,
        preview_times=list(preview_times or []),
        preview_dir=preview_dir,
        segment=segment,
        scale_height=scale_height,
        fps=fps,
    )
    return get_renderer(style.renderer).run(request)
