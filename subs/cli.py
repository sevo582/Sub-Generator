"""Команден ред.

    subs render video.mp4 --style stack
    subs render video.mp4 --style behind
    subs render video.mp4 --style behind --layer-only
    subs transcribe video.mp4
    subs render video.mp4 --words words.json --style stack
    subs styles
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .burn import FFmpegError, LAYER_SUFFIX, ToolsMissing, check_tools, probe
from .pipeline import build_blocks, check_fonts, load_words, render, save_words
from .styles import PRESETS, Style, apply_overrides, get_style, load_style_file
from .transcribe import TranscribeOptions, transcribe


def _transcribe_options(args: argparse.Namespace) -> TranscribeOptions:
    return TranscribeOptions(
        model=args.model, language=args.language, device=args.device,
        compute_type=args.compute_type, align=not args.no_align,
        align_model=args.align_model, initial_prompt=args.initial_prompt,
        batch_size=args.batch_size, threads=args.threads,
    )


def warn(message: str) -> None:
    print(f"внимание: {message}", file=sys.stderr)


def fail(message: str) -> int:
    print(f"грешка: {message}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------
# Аргументи
# --------------------------------------------------------------------------


def add_transcribe_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("транскрипция")
    group.add_argument("--model", default="large-v3",
                       help="модел на Whisper (по подразбиране: large-v3)")
    group.add_argument("--language", "-l", default=None,
                       help="код на езика, напр. bg, tr, en (по подразбиране: разпознава се)")
    group.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"),
                       help="къде да се смята (по подразбиране: auto)")
    group.add_argument("--compute-type", default=None,
                       help="тип на смятането за ctranslate2, напр. float16, int8")
    group.add_argument("--no-align", action="store_true",
                       help="без forced alignment — по-бързо, но таймингите са по-неточни")
    group.add_argument("--align-model", default=None,
                       help="wav2vec2 модел за подравняване; замества регистъра по език")
    group.add_argument("--initial-prompt", default=None,
                       help="подсказка към Whisper — помага за имена и термини")
    group.add_argument("--batch-size", type=int, default=8,
                       help="батчов режим на faster-whisper; 1 изключва "
                            "(по подразбиране: 8)")
    group.add_argument("--threads", type=int, default=None,
                       help="ядра за смятането (по подразбиране: всички)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subs",
        description="Вграждане на анимирани субтитри във вертикално видео.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"subs {__version__}")
    parser.add_argument("--traceback", action="store_true",
                        help="показва пълния traceback при грешка (за докладване на бъг)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ---- render ----
    render_parser = subparsers.add_parser(
        "render", help="транскрибира (ако трябва) и рендира видеото")
    render_parser.add_argument("video", type=Path)
    render_parser.add_argument("--style", "-s", default="stack",
                               help=f"стилов пресет: {', '.join(sorted(PRESETS))}")
    render_parser.add_argument("--style-file", type=Path, default=None,
                               help="JSON със стил, наследяващ пресет чрез \"extends\"")
    render_parser.add_argument("--set", dest="overrides", action="append", default=[],
                               metavar="ПЪТ=СТОЙНОСТ",
                               help="надделяване над стила, напр. --set stack.size_normal=0.04")
    render_parser.add_argument("--words", "-w", type=Path, default=None,
                               help="готов JSON с думи вместо нова транскрипция")
    render_parser.add_argument("--output", "-o", type=Path, default=None,
                               help="изходно видео (по подразбиране: <име>.subs.mp4)")
    render_parser.add_argument("--layer", type=Path, default=None,
                               help="слой със субтитрите върху прозрачен фон")
    render_parser.add_argument("--layer-only", action="store_true",
                               help="само слоят с прозрачност, без готово видео")
    render_parser.add_argument("--layer-format", default="prores",
                               choices=tuple(LAYER_SUFFIX),
                               help="prores (по подразбиране) или webm с VP9")
    render_parser.add_argument("--save-words", type=Path, default=None,
                               help="записва транскрипцията в JSON за ръчна поправка")
    render_parser.add_argument("--crf", type=int, default=18)
    render_parser.add_argument("--preset", default="veryfast",
                               help="пресет на x264: veryfast (по подразбиране) е "
                                    "около 2.4 пъти по-бърз от medium при същото качество")
    render_parser.add_argument("--keep-intermediate", type=Path, default=None,
                               metavar="ПАПКА",
                               help="запазва .ass и другите междинни файлове")
    render_parser.add_argument("--preview", type=float, action="append", default=[],
                               metavar="СЕКУНДА", dest="preview_times",
                               help="рисува само по един PNG кадър в дадената секунда "
                                    "вместо цялото видео; може да се повтаря")
    render_parser.add_argument("--preview-dir", type=Path, default=None,
                               help="къде да отидат кадрите от --preview "
                                    "(по подразбиране: до изходното видео)")
    render_parser.add_argument("--llm-highlight", action="store_true",
                               help="подчертаната дума се избира от езиков модел "
                                    "вместо с евристиката (изисква ANTHROPIC_API_KEY)")
    render_parser.add_argument("--llm-model", default=None,
                               help="модел за --llm-highlight")
    render_parser.add_argument("--llm-cache", type=Path, default=None,
                               help="файл с кеш на отговорите "
                                    "(по подразбиране: <видео>.highlights.json)")
    render_parser.add_argument("--dry-run", action="store_true",
                               help="показва какво ще се направи, без да пипа ffmpeg")
    render_parser.add_argument("--quiet", "-q", action="store_true")
    add_transcribe_arguments(render_parser)

    # ---- transcribe ----
    transcribe_parser = subparsers.add_parser(
        "transcribe", help="само транскрипция — JSON с думи и тайминги")
    transcribe_parser.add_argument("video", type=Path)
    transcribe_parser.add_argument("--output", "-o", type=Path, default=None,
                                   help="по подразбиране: <име>.words.json")
    add_transcribe_arguments(transcribe_parser)

    # ---- styles ----
    styles_parser = subparsers.add_parser("styles", help="изброява стиловите пресети")
    styles_parser.add_argument("--json", action="store_true",
                               help="пълните настройки в JSON")

    return parser


def parse_overrides(items: list[str]) -> dict:
    """``--set stack.size_normal=0.04`` -> вложен речник."""
    result: dict = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--set очаква ПЪТ=СТОЙНОСТ, а не {item!r}")
        path, raw = item.split("=", 1)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw  # низ без кавички
        node = result
        parts = path.strip().split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return result


def resolve_style(args: argparse.Namespace) -> Style:
    """Пресет по име или JSON файл, после надделяванията от ``--set``."""
    overrides = parse_overrides(args.overrides)
    base = load_style_file(str(args.style_file)) if args.style_file else get_style(args.style)
    return apply_overrides(base, overrides)


# --------------------------------------------------------------------------
# Команди
# --------------------------------------------------------------------------


def command_transcribe(args: argparse.Namespace) -> int:
    require_file(args.video, "видео файл")
    options = _transcribe_options(args)
    transcript = transcribe(args.video, options)
    output = args.output or args.video.with_suffix(".words.json")
    save_words(transcript, output)
    for note in transcript.notes:
        print(f"  {note}")
    print(f"{len(transcript.words)} думи → {output}")
    return 0


def command_styles(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps({n: s.to_json() for n, s in PRESETS.items()},
                         ensure_ascii=False, indent=2))
        return 0
    width = max(len(name) for name in PRESETS)
    for name, style in sorted(PRESETS.items()):
        layer = " (може и слой с прозрачност)" if style.renderer == "raster_behind" else ""
        print(f"  {name:<{width}}  {style.description}{layer}")
    print("\nНастройка: --set ПЪТ=СТОЙНОСТ, напр. --set stack.size_highlight=0.06")
    print("Пълните настройки: subs styles --json")
    return 0


def require_file(path: Path, what: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"няма такъв {what}: {path}")


def command_render(args: argparse.Namespace) -> int:
    require_file(args.video, "видео файл")
    if args.words:
        require_file(args.words, "JSON с думи")
    style = resolve_style(args)
    say = (lambda message: None) if args.quiet else print

    media = probe(args.video)
    turned = f", завъртяно на {media.rotation}°" if media.rotation else ""
    say(f"вход: {media.width}x{media.height} @ {media.fps:.2f} к/с, "
        f"{media.duration:.2f} s{turned}")
    if media.width > media.height:
        warn("видеото е хоризонтално, а стиловете са мерени за вертикално "
             "— размерите се смятат от височината и текстът ще излезе едър")

    if args.words:
        transcript = load_words(args.words)
        say(f"думи от {args.words} ({len(transcript.words)})")
    else:
        options = _transcribe_options(args)
        say("транскрибирам …")
        transcript = transcribe(args.video, options)
        for note in transcript.notes:
            say(f"  {note}")
        if args.save_words:
            save_words(transcript, args.save_words)
            say(f"думите са записани в {args.save_words}")

    if not transcript.words:
        return fail("транскрипцията е празна — няма какво да се рендира")

    check_fonts(transcript, style, warn)

    layer_only = args.layer_only
    output = None if layer_only else (args.output or _default_output(args.video, style))
    if args.preview_times:
        bad = [t for t in args.preview_times if not 0 <= t <= media.duration]
        if bad:
            raise ValueError(
                f"моментите {bad} са извън видеото (0–{media.duration:.2f} s)")
    layer = args.layer
    if layer is None and layer_only:
        layer = args.video.with_suffix("").with_name(
            args.video.stem + ".layer" + LAYER_SUFFIX[args.layer_format])

    highlighter = None
    if args.llm_highlight:
        from .highlight_llm import DEFAULT_MODEL, LlmHighlighter

        highlighter = LlmHighlighter(
            model=args.llm_model or DEFAULT_MODEL,
            cache_path=args.llm_cache or args.video.with_suffix(".highlights.json"),
        )

    blocks = build_blocks(transcript, style,
                          highlighter.choose if highlighter else None)
    if highlighter is not None:
        highlighter.save()
        for note in dict.fromkeys(highlighter.notes):
            warn(note)
    say(f"{len(blocks)} блока, стил {style.name!r} → рендерер {style.renderer!r}")
    for block in blocks[:3]:
        marked = " ".join(
            f"[{w.text}]" if i == block.highlight else w.text
            for i, w in enumerate(block.words))
        say(f"  {block.start:6.2f}–{block.end:6.2f}  {marked}")
    if len(blocks) > 3:
        say(f"  … и още {len(blocks) - 3}")

    result = render(
        args.video, transcript, style, output=output, layer=layer,
        layer_format=args.layer_format, crf=args.crf, preset=args.preset,
        keep_dir=args.keep_intermediate, dry_run=args.dry_run,
        progress=say, media=media, blocks=blocks,
        preview_times=args.preview_times, preview_dir=args.preview_dir,
    )

    for note in result.notes:
        # Предупрежденията минават през stderr дори при --quiet: счупена
        # алфа в слоя е точно това, което не бива да остане незабелязано.
        if note.startswith("ВНИМАНИЕ"):
            warn(note)
        else:
            say(f"  {note}")
    if args.dry_run:
        for command in result.commands:
            print(" ".join(command))
        return 0
    for path in result.outputs:
        print(f"готово: {path}")
    return 0


def _default_output(video: Path, style: Style) -> Path:
    return video.with_name(f"{video.stem}.{style.name}.mp4")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        # ffmpeg се проверява в командите, които наистина пипат видео.
        # ``styles`` само изброява пресети и трябва да работи и на машина,
        # на която ffmpeg още не е инсталиран — иначе първото нещо, с което
        # човек проверява дали пакетът изобщо е тръгнал, гърми без причина.
        if args.command == "render":
            check_tools()
            return command_render(args)
        if args.command == "transcribe":
            check_tools()
            return command_transcribe(args)
        if args.command == "styles":
            return command_styles(args)
    except ToolsMissing as error:
        return fail(str(error))
    except FFmpegError as error:
        return fail(str(error))
    except (KeyError, ValueError, FileNotFoundError) as error:
        return fail(str(error))
    except KeyboardInterrupt:
        return fail("прекъснато")
    except Exception as error:  # noqa: BLE001
        # Суровият traceback не помага на никого. Пълният остава зад флаг —
        # най-честите причини (недостъпен модел, пълен диск) нямат нужда от него.
        if args.traceback:
            raise
        return fail(f"{error}\n({type(error).__name__} — за подробности: --traceback)")
    return fail(f"непозната команда: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
