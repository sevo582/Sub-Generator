"""Прозорец за desktop — същият конвейер, само че с бутони.

Разделението е нарочно: всичко над ``class App`` е чиста логика без tkinter
и се тества без дисплей. Прозорецът само вика нея.

Дългите операции (транскрипция, рендиране) вървят в отделна нишка и
разговарят с прозореца през опашка. Ако се пуснат направо, Windows обявява
прозореца за увиснал още на първата минута.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import Transcript, Word

#: Модели на Whisper, подредени по големина. Колкото по-надолу, толкова
#: по-точно и по-бавно.
MODELS = ("tiny", "base", "small", "medium", "large-v3")

#: Езиците по приоритет от заданието, плюс автоматично разпознаване.
LANGUAGES = (("автоматично", None), ("български", "bg"),
             ("турски", "tr"), ("английски", "en"))

VIDEO_TYPES = [("Видео", "*.mp4 *.mov *.m4v *.avi *.mkv *.webm"), ("Всички", "*.*")]
JSON_TYPES = [("JSON с думи", "*.json"), ("Всички", "*.*")]

#: Бърза палитра до избирача на цвят — това са цветовете, които се ползват
#: най-често, за да не се минава през диалог за всяка дума.
PALETTE = ("#FFFFFF", "#FF3B30", "#FF9F0A", "#FFD60A",
           "#30D158", "#0A84FF", "#8FE9F7", "#BF5AF2")

#: Преглед на парче: колко секунди и с каква едрина да се рендира. Малко
#: и дребно нарочно — целта е да се види след секунди, не да е за качване.
PREVIEW_SECONDS = 3.0
PREVIEW_HEIGHT = 480
PREVIEW_FPS = 12.0


# --------------------------------------------------------------------------
# Чиста логика — без tkinter, тества се без дисплей
# --------------------------------------------------------------------------


def format_time(seconds: float) -> str:
    return f"{seconds:.2f}"


def parse_time(text: str) -> float:
    """Приема ``1.25`` и ``1,25`` — на български десетичната е запетая."""
    value = float(str(text).strip().replace(",", "."))
    if value < 0:
        raise ValueError("времето не може да е отрицателно")
    return value


@dataclass
class Row:
    """Един ред от таблицата с думи."""

    text: str
    start: float
    end: float
    emphasis: bool = False
    accent: bool = False
    color: str | None = None
    animation: str = "няма"

    def values(self) -> tuple[str, str, str, str, str, str]:
        marks = ("★" if self.emphasis else "") + ("●" if self.accent else "")
        return (self.text, format_time(self.start), format_time(self.end), marks,
                self.color or "—", self.animation)

    @property
    def middle(self) -> float:
        """Средата на думата — там се показва кадърът при преглед."""
        return (self.start + self.end) / 2.0


def rows_from_transcript(transcript: Transcript) -> list[Row]:
    return [Row(w.text, w.start, w.end, w.emphasis, w.accent, w.color, w.animation)
            for w in transcript.words]


def transcript_from_rows(rows: Iterable[Row], language: str | None,
                         notes: list[str] | None = None) -> Transcript:
    words = [Word(r.text, r.start, r.end, r.emphasis, r.accent, r.color, r.animation)
             for r in rows]
    return Transcript(words=words, language=language or "unknown",
                      notes=list(notes or []))


def validate(rows: list[Row]) -> list[str]:
    """Проверява таблицата и връща списък с оплаквания за дневника.

    Не спира нищо — човекът може нарочно да е оставил нещо междинно, докато
    редактира. Целта е да не се учуди после защо блокът изглежда странно.
    """
    problems: list[str] = []
    for index, row in enumerate(rows, start=1):
        if not row.text.strip():
            problems.append(f"ред {index}: празен текст")
        if row.end < row.start:
            problems.append(f"ред {index}: краят е преди началото")
    for index, (current, following) in enumerate(zip(rows, rows[1:]), start=1):
        if following.start < current.end - 1e-6:
            problems.append(f"редове {index} и {index + 1} се застъпват във времето")
    return problems


def preview_window(rows: list[Row], index: int, duration: float,
                   limit: float) -> tuple[float, float]:
    """Кой отрязък да се рендира при „Пусни" от даден ред.

    Започва малко преди думата, за да се види как влиза, и не излиза извън
    видеото.
    """
    start = max(0.0, rows[index].start - 0.4) if 0 <= index < len(rows) else 0.0
    length = max(0.2, min(duration, max(0.2, limit - start)))
    return start, length


def default_output(video: Path, style: str) -> Path:
    return video.with_name(f"{video.stem}.{style}.mp4")


def reveal(path: Path) -> None:
    """Отваря папката на файла в системния файлов мениджър."""
    from .burn import quiet

    path = Path(path)
    try:
        if sys.platform == "win32":
            subprocess.run(["explorer", "/select,", str(path)], **quiet(check=False))
        elif sys.platform == "darwin":
            subprocess.run(["open", "-R", str(path)], **quiet(check=False))
        else:
            subprocess.run(["xdg-open", str(path.parent)], **quiet(check=False))
    except OSError:
        pass


# --------------------------------------------------------------------------
# Работна нишка
# --------------------------------------------------------------------------


class Worker:
    """Пуска една задача в нишка и връща съобщения през опашка.

    Съобщенията са двойки ``(вид, стойност)``: ``log``, ``done``, ``error``.
    """

    def __init__(self) -> None:
        self.queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def log(self, message: str) -> None:
        self.queue.put(("log", message))

    def start(self, work: Callable[[Callable[[str], None]], Any]) -> None:
        if self.busy:
            raise RuntimeError("вече върви задача")

        def run() -> None:
            try:
                self.queue.put(("done", work(self.log)))
            except Exception as error:  # noqa: BLE001 — нишката не бива да мре мълчаливо
                self.queue.put(("error", (str(error), traceback.format_exc())))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()


def main(argv: list[str] | None = None) -> int:
    """Пуска прозореца. Внасяме tkinter тук, за да върви модулът и без него."""
    try:
        from .guiwindow import App
    except ImportError as error:
        print(f"грешка: липсва tkinter ({error}).\n"
              "Windows: преинсталирай Python с включена опцията "
              "'tcl/tk and IDLE'.\nLinux: sudo apt install python3-tk",
              file=sys.stderr)
        return 1
    App(argv).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
