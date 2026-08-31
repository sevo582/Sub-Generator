"""Транскрипция с тайминги на ниво дума.

За този стил синхронът е всичко — думата трябва да светне точно когато се
изрича. Затова минаваме през WhisperX: faster-whisper дава текста, а
wav2vec2 forced alignment го залепва за звука с точност под 100 ms.
Самият faster-whisper дава думи, но таймингите му се разминават осезаемо.

**Капанът с българския.** WhisperX държи речник от подразбиращи се
alignment модели в ``whisperx/alignment.py`` (``DEFAULT_ALIGN_MODELS_TORCH``
и ``DEFAULT_ALIGN_MODELS_HF``). Там има ``tr``, ``ru``, ``uk``, но няма
``bg`` — при ``--language bg`` WhisperX гърми с
``No default align-model for language: bg``. Затова държим свой регистър
(``ALIGN_MODELS``), който може да се презапише с ``--align-model``.

Инструментът никога не бива да гръмне. Ако alignment не сработи по каквато
и да е причина, падаме на таймингите от faster-whisper и записваме
предупреждение в JSON-а.
"""

from __future__ import annotations

import importlib
import os
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path

from .burn import extract_audio
from .models import Transcript, Word

#: Подредени кандидати за forced alignment по език. Пробват се отгоре надолу
#: и първият, който се зареди, печели — така един недостъпен модел не спира
#: подравняването.
#:
#: **За българския няма официален модел.** WhisperX не го покрива, а
#: публичните wav2vec2 модели, дообучени на български, идват и си отиват.
#: Затова тук стои руският: азбуките се припокриват достатъчно, за да е
#: подравняването осезаемо по-добро от нищо, макар и приблизително.
#:
#: Ако намериш работещ български модел, сложи го пръв в кортежа. Търси се
#: така (изисква huggingface_hub, който вече идва с faster-whisper)::
#:
#:     python -c "from huggingface_hub import HfApi; [print(m.id) for m in
#:     HfApi().list_models(search='wav2vec2 bulgarian', limit=30)]"
#:
#: Провери го веднъж с ``--align-model ИМЕ``, преди да го впишеш.
ALIGN_MODELS: dict[str, tuple[str, ...]] = {
    "bg": ("jonatasgrosman/wav2vec2-large-xlsr-53-russian",),
    "tr": ("mpoyraz/wav2vec2-xls-r-300m-cv7-turkish",),
    "en": ("WAV2VEC2_ASR_BASE_960H",),
    "ru": ("jonatasgrosman/wav2vec2-large-xlsr-53-russian",),
    "uk": ("Yehor/wav2vec2-xls-r-300m-uk-with-small-lm",),
}

#: Езици, за които подравняването минава през модел за друг език и затова
#: е приблизително.
APPROXIMATE: frozenset[str] = frozenset({"bg"})


@dataclass
class TranscribeOptions:
    model: str = "large-v3"
    language: str | None = None
    device: str = "auto"
    compute_type: str | None = None
    #: Батчов режим на faster-whisper. 1 изключва и минава на последователен.
    #: На процесор разликата е забележима, защото парчетата се смятат
    #: успоредно вместо едно по едно.
    batch_size: int = 8
    #: Ядра за ctranslate2. None = всички, които машината има.
    threads: int | None = None
    align: bool = True
    align_model: str | None = None
    initial_prompt: str | None = None


def resolve_device(requested: str) -> tuple[str, str]:
    """Връща (устройство, тип на смятането).

    На CUDA ползваме float16; на CPU int8, защото float16 на CPU е бавно
    и в ctranslate2 често изобщо не се поддържа.
    """
    device = requested
    if requested == "auto":
        try:
            import torch  # локален внос: CLI-то трябва да върви и без torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"
    return device, ("float16" if device == "cuda" else "int8")


def transcribe(audio_or_video: str | os.PathLike[str],
               options: TranscribeOptions | None = None) -> Transcript:
    """Пълната транскрипция: разпознаване, после подравняване."""
    options = options or TranscribeOptions()
    source = Path(audio_or_video)
    if not source.exists():
        raise FileNotFoundError(f"няма такъв файл: {source}")

    tmpdir = tempfile.mkdtemp(prefix="subs-audio-")
    audio_path = Path(tmpdir) / "audio.wav"
    try:
        extract_audio(source, audio_path)
        transcript = _recognise(audio_path, options)
        if options.align and transcript.words:
            _align(audio_path, transcript, options)
        return transcript
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


def _recognise(audio_path: Path, options: TranscribeOptions) -> Transcript:
    """Стъпка 1: текст и груби тайминги от faster-whisper."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:  # pragma: no cover - зависи от средата
        raise RuntimeError(
            "липсва faster-whisper. Инсталирай с:\n"
            "    pip install faster-whisper\n"
            "или целия пакет за транскрипция:\n"
            "    pip install -e .[transcribe]"
        ) from error

    device, compute = resolve_device(options.device)
    compute = options.compute_type or compute
    threads = options.threads or os.cpu_count() or 4

    try:
        model = WhisperModel(options.model, device=device, compute_type=compute,
                             cpu_threads=threads)
    except Exception as error:  # noqa: BLE001 — най-често мрежа или диск
        raise RuntimeError(
            f"моделът {options.model!r} не можа да се зареди: {error}\n"
            "При първо пускане се тегли от HuggingFace и се кешира в "
            "~/.cache/huggingface (Windows: %USERPROFILE%\\.cache\\huggingface).\n"
            "Провери мрежата, или пробвай по-малък модел с --model small."
        ) from error

    batched = options.batch_size > 1
    try:
        words, info = _decode(model, audio_path, options, batched)
    except Exception as error:  # noqa: BLE001
        if not batched:
            raise RuntimeError(f"разпознаването се провали: {error}") from error
        # Батчовият режим е по-нов и на места по-капризен. Пада се на
        # последователния, вместо да се губи цялата работа.
        try:
            words, info = _decode(model, audio_path, options, batched=False)
        except Exception as second:  # noqa: BLE001
            raise RuntimeError(f"разпознаването се провали: {second}") from second
        batched = False

    language = options.language or getattr(info, "language", None) or "unknown"
    how = f"батчово по {options.batch_size}" if batched else "последователно"
    notes = [f"faster-whisper {options.model} на {device}/{compute}, "
             f"{threads} нишки, {how}"]
    return Transcript(words=words, language=language, aligned=False, notes=notes)


def _decode(model: object, audio_path: Path, options: TranscribeOptions,
            batched: bool) -> tuple[list[Word], object]:
    """Пуска разпознаването и събира думите с техните тайминги."""
    engine = model
    arguments: dict[str, object] = dict(
        language=options.language,
        word_timestamps=True,
        vad_filter=True,
        beam_size=5,
        initial_prompt=options.initial_prompt,
    )
    if batched:
        from faster_whisper import BatchedInferencePipeline

        engine = BatchedInferencePipeline(model=model)
        arguments["batch_size"] = options.batch_size

    segments, info = engine.transcribe(str(audio_path), **arguments)

    words: list[Word] = []
    for segment in segments:  # генератор — работата се случва тук
        for word in (segment.words or []):
            text = word.word.strip()
            if text:
                words.append(Word(text=text, start=float(word.start),
                                  end=float(word.end)))
    if not words:
        raise RuntimeError("нито една дума с тайминг")
    return words, info


def align_model_for(language: str,
                    override: str | None = None) -> tuple[tuple[str, ...], bool]:
    """Връща (кандидати по ред на предпочитание, дали е приблизително)."""
    if override:
        return (override,), False
    code = language.lower()[:2]
    return ALIGN_MODELS.get(code, ()), code in APPROXIMATE


def _align(audio_path: Path, transcript: Transcript, options: TranscribeOptions) -> None:
    """Стъпка 2: forced alignment. При всяка засечка връщаме грубите тайминги."""
    language = transcript.language
    candidates, approximate = align_model_for(language, options.align_model)

    if not candidates:
        transcript.notes.append(
            f"няма alignment модел за език {language!r}; таймингите са от "
            "faster-whisper (по-неточни). Подай свой с --align-model."
        )
        return

    try:
        importlib.import_module("whisperx")
    except ImportError:
        transcript.notes.append(
            "whisperx не е инсталиран — таймингите са от faster-whisper и са "
            "по-неточни. Инсталирай с: pip install -e \".[transcribe]\" "
            "(изисква Python 3.10–3.13; на 3.14 whisperx още не се поддържа)."
        )
        return

    device, _ = resolve_device(options.device)
    for index, candidate in enumerate(candidates):
        try:
            _run_alignment(audio_path, transcript, candidate, device)
        except Exception as error:  # noqa: BLE001 — всяка засечка е поправима
            transcript.notes.append(f"alignment с {candidate!r} не сработи: {error}")
            continue

        transcript.aligned = True
        note = f"подравнено с {candidate!r}"
        if approximate:
            note += " — моделът не е за този език, подравняването е приблизително"
        elif index > 0:
            note += " (резервен модел)"
        transcript.notes.append(note)
        return

    transcript.notes.append(
        "alignment не сработи с нито един модел; таймингите остават от faster-whisper"
    )


def _run_alignment(audio_path: Path, transcript: Transcript, model_name: str,
                   device: str) -> None:
    import whisperx

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        audio = whisperx.load_audio(str(audio_path))
        model, metadata = whisperx.load_align_model(
            language_code=transcript.language, device=device, model_name=model_name
        )
        segments = [{
            "start": transcript.words[0].start,
            "end": transcript.words[-1].end,
            "text": " ".join(w.text for w in transcript.words),
        }]
        result = whisperx.align(segments, model, metadata, audio, device,
                                return_char_alignments=False)

    aligned: list[Word] = []
    for segment in result.get("segments", []):
        for word in segment.get("words", []):
            text = str(word.get("word", "")).strip()
            start, end = word.get("start"), word.get("end")
            if text and start is not None and end is not None:
                aligned.append(Word(text=text, start=float(start), end=float(end)))

    if len(aligned) < max(1, len(transcript.words) // 2):
        raise RuntimeError(
            f"подравнени са само {len(aligned)} от {len(transcript.words)} думи"
        )
    _carry_flags(transcript.words, aligned)
    transcript.words = aligned


def _carry_flags(old: list[Word], new: list[Word]) -> None:
    """Пренася ръчните маркери, ако думите съвпадат по текст."""
    marked = {w.text.lower(): w for w in old if w.emphasis or w.accent}
    if not marked:
        return
    for word in new:
        source = marked.get(word.text.lower())
        if source is not None:
            word.emphasis = source.emphasis
            word.accent = source.accent
