"""Всичко, което говори с ffmpeg.

Две правила, които спестяват часове:

1. Никакъв ``shell=True``. Командите са списъци от аргументи.
2. Филтърните низове на ffmpeg се разделят с ``:`` и ``,``. Windows път
   като ``C:\\Users\\...`` ги чупи и екранирането е капан. Затова .ass
   файлът и шрифтовете се пишат във временна папка и ffmpeg се стартира
   **с cwd в нея**, а във филтъра влизат само относителни имена без
   двоеточие. Входният и изходният път остават нормални аргументи, където
   двоеточието не пречи.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterator, Sequence


class FFmpegError(RuntimeError):
    """ffmpeg върна ненулев код. Съобщението носи опашката от stderr."""


class ToolsMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    width: int
    height: int
    fps: float
    duration: float
    has_audio: bool
    #: Завъртане от метаданните, в градуси. Размерите по-горе са вече
    #: разменени, ако е ±90 — държим го само за да го покажем на човека.
    rotation: int = 0

    @property
    def frame_count(self) -> int:
        return max(1, round(self.duration * self.fps))


def check_tools() -> None:
    """Проверява за ffmpeg и ffprobe и дава указания, ако липсват."""
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise ToolsMissing(
            f"липсва {' и '.join(missing)} в PATH.\n"
            "Windows: winget install Gyan.FFmpeg  (или изтегли от gyan.dev "
            "и добави папката bin към PATH)\n"
            "macOS:   brew install ffmpeg\n"
            "Linux:   sudo apt install ffmpeg"
        )


def stream_rotation(stream: dict) -> int:
    """Завъртането от метаданните на потока, в градуси 0/90/180/270.

    Видео, снимано вертикално с телефон, често се записва легнало плюс флаг
    за завъртане. ffprobe връща **записаните** размери, а ffmpeg подава на
    филтрите кадъра **след** завъртането — разминаване, което обърква всяка
    сметка за позиции.

    Стойността се търси на двете места, където живее: матрицата на дисплея
    в ``side_data_list`` (модерният начин) и остарелия таг ``rotate``.
    """
    value = None
    for side_data in stream.get("side_data_list", []):
        if "rotation" in side_data:
            value = side_data["rotation"]
            break
    if value is None:
        value = stream.get("tags", {}).get("rotate")
    try:
        degrees = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return degrees % 360


def probe(path: str | os.PathLike[str]) -> MediaInfo:
    """Чете размери, кадрова честота и наличие на звук.

    Размерите са тези, които наистина излизат от ffmpeg — при завъртян
    материал са разменени спрямо записаните.
    """
    check_tools()
    command = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise FFmpegError(f"ffprobe не можа да прочете {path}:\n{result.stderr.strip()}")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError(f"{path} няма видео поток")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        fps = float(Fraction(rate))
    except (ZeroDivisionError, ValueError):
        fps = 0.0
    if fps <= 0:
        fps = 30.0

    duration = 0.0
    for candidate in (video.get("duration"), data.get("format", {}).get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    if duration <= 0 and video.get("nb_frames"):
        duration = float(video["nb_frames"]) / fps

    width, height = int(video["width"]), int(video["height"])
    rotation = stream_rotation(video)
    if rotation in (90, 270):
        width, height = height, width

    return MediaInfo(
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        has_audio=has_audio,
        rotation=rotation,
    )


def run(command: Sequence[str], cwd: str | os.PathLike[str] | None = None) -> None:
    """Пуска ffmpeg и вдига разбираема грешка при провал."""
    result = subprocess.run(list(command), cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-25:])
        raise FFmpegError(f"ffmpeg се провали (код {result.returncode}):\n{tail}")


def extract_audio(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    """Изнася моно 16 kHz WAV — това очакват и WhisperX, и faster-whisper."""
    check_tools()
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(destination),
    ])


# --------------------------------------------------------------------------
# Стил A: вграждане на .ass
# --------------------------------------------------------------------------


def burn_ass(
    source: str | os.PathLike[str],
    ass_dir: str | os.PathLike[str],
    ass_name: str,
    fonts_subdir: str,
    output: str | os.PathLike[str],
    crf: int = 18,
    preset: str = "medium",
    segment: tuple[float, float] | None = None,
    scale_height: int | None = None,
    fps: float | None = None,
) -> list[str]:
    """Вгражда .ass във видеото.

    ``ass_dir`` е работната папка; ``ass_name`` и ``fonts_subdir`` са
    относителни спрямо нея имена без двоеточие — виж бележката най-горе.

    ``segment`` (начало, времетраене) реже парче — ползва се за бързия
    преглед в прозореца. ``-copyts`` е задължително при него: без него
    ffmpeg нулира времената, филтърът ``ass`` вижда парчето като начало на
    видеото и рисува грешния блок.

    Връща командата, за да може да се покаже при ``--dry-run``.
    """
    filters = [f"ass=f={ass_name}:fontsdir={fonts_subdir}"]
    if scale_height:
        filters.append(f"scale=-2:{int(scale_height)}")

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if segment is not None:
        command += ["-ss", f"{max(0.0, segment[0]):.3f}", "-copyts"]
    command += ["-i", os.path.abspath(str(source))]
    if segment is not None:
        # ``-to`` е абсолютно, а ``-t`` е относително. С ``-copyts`` времената
        # остават абсолютни, тоест ``-t`` би отрязало парчето от нулата на
        # видеото и щеше да върне по-малко, отколкото е поискано.
        command += ["-to", f"{max(0.0, segment[0]) + max(0.05, segment[1]):.3f}",
                    "-avoid_negative_ts", "make_zero"]
    command += ["-vf", ",".join(filters)]
    if fps:
        command += ["-r", f"{fps:.4f}"]
    command += [
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-pix_fmt", "yuv420p",
    ]
    command += ["-an"] if segment is not None else ["-c:a", "copy"]
    command += ["-movflags", "+faststart", os.path.abspath(str(output))]
    run(command, cwd=str(ass_dir))
    return command


def preview_ass_frame(
    source: str | os.PathLike[str],
    ass_dir: str | os.PathLike[str],
    ass_name: str,
    fonts_subdir: str,
    time: float,
    output: str | os.PathLike[str],
) -> list[str]:
    """Един кадър с вградени субтитри, за преглед на вида.

    ``-copyts`` е задължително: при бързо превъртане с ``-ss`` преди ``-i``
    ffmpeg нулира времената, филтърът ``ass`` вижда кадъра като нулева
    секунда и рисува грешния блок — или никакъв.

    ``-update 1`` позволява един-единствен PNG вместо поредица.
    """
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, time):.3f}", "-copyts",
        "-i", os.path.abspath(str(source)),
        "-vf", f"ass=f={ass_name}:fontsdir={fonts_subdir}",
        "-frames:v", "1", "-update", "1",
        os.path.abspath(str(output)),
    ]
    run(command, cwd=str(ass_dir))
    return command


def extract_frame(source: str | os.PathLike[str], time: float,
                  output: str | os.PathLike[str]) -> None:
    """Изнася един кадър от изходното видео като PNG."""
    check_tools()
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, time):.3f}", "-i", os.path.abspath(str(source)),
        "-frames:v", "1", "-update", "1", os.path.abspath(str(output)),
    ])


# --------------------------------------------------------------------------
# Стил B: растерни кадри през pipe
# --------------------------------------------------------------------------

#: Кодеци за слоя с прозрачност. ProRes 4444 е по подразбиране, защото го
#: приема всеки редактор и не губи качество по ръбовете на буквите.
LAYER_CODECS: dict[str, list[str]] = {
    "prores": ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
               "-alpha_bits", "16", "-vendor", "apl0"],
    "webm": ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "24",
             "-auto-alt-ref", "0"],
}

LAYER_SUFFIX: dict[str, str] = {"prores": ".mov", "webm": ".webm"}

#: Формати на пиксела, които носят алфа канал.
ALPHA_PIX_FMT_PREFIXES = ("yuva", "rgba", "bgra", "argb", "abgr", "ya", "gbrap")


def build_raster_command(
    source: str | os.PathLike[str],
    media: MediaInfo,
    output: str | os.PathLike[str] | None,
    layer: str | os.PathLike[str] | None,
    layer_format: str = "prores",
    crf: int = 18,
    preset: str = "medium",
    segment: tuple[float, float] | None = None,
    scale_height: int | None = None,
    fps: float | None = None,
) -> list[str]:
    """Сглобява една ffmpeg команда с два изхода от един поток кадри.

    Кадрите влизат като суров RGBA през stdin. Ако се искат и двата изхода,
    потокът се разклонява с ``split`` — така рисуваме всеки кадър веднъж.
    """
    if output is None and layer is None:
        raise ValueError("трябва поне един изход — готово видео или слой")
    if layer_format not in LAYER_CODECS:
        raise ValueError(f"непознат формат за слоя: {layer_format!r}")

    rate = fps or media.fps
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if segment is not None:
        command += ["-ss", f"{max(0.0, segment[0]):.3f}"]
    command += ["-i", os.path.abspath(str(source))]
    command += [
        "-f", "rawvideo", "-pixel_format", "rgba",
        "-video_size", f"{media.width}x{media.height}",
        "-framerate", f"{rate:.6f}",
        "-i", "-",
    ]
    if segment is not None:
        command += ["-t", f"{max(0.05, segment[1]):.3f}"]

    tail = f",scale=-2:{int(scale_height)}" if scale_height else ""
    if output is not None and layer is not None:
        graph = ("[1:v]split=2[lay][ovl];"
                 f"[0:v][ovl]overlay=format=auto:shortest=1{tail}[comp]")
        overlay_label, layer_label = "[comp]", "[lay]"
    elif output is not None:
        graph = f"[0:v][1:v]overlay=format=auto:shortest=1{tail}[comp]"
        overlay_label, layer_label = "[comp]", None
    else:
        graph = "[1:v]null[lay]"
        overlay_label, layer_label = None, "[lay]"
    command += ["-filter_complex", graph]

    if overlay_label is not None:
        command += ["-map", overlay_label]
        if media.has_audio and segment is None:
            command += ["-map", "0:a:0", "-c:a", "copy"]
        else:
            command += ["-an"]
        if fps:
            # Изходът иначе върви по честотата на източника и кадрите просто
            # се дублират — за преглед това е излишна работа при декодиране.
            command += ["-r", f"{rate:.6f}"]
        command += [
            "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            os.path.abspath(str(output)),
        ]

    if layer_label is not None:
        # Слоят е без звук нарочно — внася се върху оригинала, който вече го има.
        command += ["-map", layer_label, "-an"]
        command += LAYER_CODECS[layer_format]
        command += [os.path.abspath(str(layer))]

    return command


def pipe_frames(command: Sequence[str], frames: Iterator[bytes],
                on_progress: Callable[[int], None] | None = None) -> None:
    """Пуска ffmpeg и му подава кадрите през stdin.

    Ако ffmpeg падне рано, ``write`` вдига BrokenPipe — прихващаме го, за да
    покажем истинската причина от stderr, а не голия trace.
    """
    process = subprocess.Popen(list(command), stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert process.stdin is not None
    index = 0
    try:
        for frame in frames:
            process.stdin.write(frame)
            index += 1
            if on_progress is not None and index % 15 == 0:
                on_progress(index)
    except BrokenPipeError:
        pass
    finally:
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        # ``communicate`` иначе се опитва да затвори stdin втори път и гърми
        # с "flush of closed file", преди да сме видели истинската грешка.
        process.stdin = None
    _, stderr = process.communicate()
    if on_progress is not None:
        on_progress(index)
    if process.returncode != 0:
        tail = "\n".join((stderr or b"").decode("utf-8", "replace").strip().splitlines()[-25:])
        raise FFmpegError(f"ffmpeg се провали (код {process.returncode}):\n{tail}")


def decode_rgb_frames(path: str | os.PathLike[str]) -> tuple[int, int, float, list[bytes]]:
    """Декодира цял (кратък) файл в суров RGB за възпроизвеждане в прозорец.

    Ползва се само за парчетата за преглед, затова всичко влиза в паметта:
    270x480 при 12 к/с прави под 0.4 MB на кадър. За цяло видео това не е
    подходящо и не бива да се вика с такова.
    """
    info = probe(path)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", os.path.abspath(str(path)),
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-5:]
        raise FFmpegError("не мога да декодирам прегледа:\n" + "\n".join(tail))

    size = info.width * info.height * 3
    data = result.stdout
    frames = [data[i:i + size] for i in range(0, len(data) - size + 1, size)]
    return info.width, info.height, info.fps, frames


def verify_alpha(path: str | os.PathLike[str]) -> tuple[bool, str]:
    """Проверява, че алфата в слоя е оцеляла.

    Класическият капан е ``yuv420p`` да изяде канала и фонът да излезе черен,
    без ffmpeg да се оплаче. Тук четем формата на пиксела и питаме ffprobe
    дали има алфа компонент.
    """
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=pix_fmt,codec_name:stream_tags=alpha_mode",
        "-print_format", "json", str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        return False, f"ffprobe не можа да прочете слоя: {result.stderr.strip()}"
    stream = json.loads(result.stdout)["streams"][0]
    pix_fmt = str(stream.get("pix_fmt", ""))
    codec = stream.get("codec_name")
    detail = f"{codec} / {pix_fmt}"

    if pix_fmt.startswith(ALPHA_PIX_FMT_PREFIXES):
        return True, detail

    # WebM носи алфата на VP9 в отделен канал на ниво контейнер, затова
    # ffprobe показва yuv420p за самия поток. Маркерът е таг alpha_mode=1.
    if str(stream.get("tags", {}).get("alpha_mode", "")) == "1":
        return True, (f"{detail} + alpha_mode=1 — при внасяне ffmpeg иска изричен "
                      "декодер: ffmpeg -c:v libvpx-vp9 -i слой.webm ...")
    return False, detail


def ensure_parent(path: str | os.PathLike[str]) -> None:
    parent = Path(path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
