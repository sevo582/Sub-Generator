"""Тестове за конвейера на транскрипцията.

Истинските модели тежат гигабайти и искат мрежа, затова тук се подменят
``faster_whisper`` и ``whisperx`` с двойници. Проверяваното не е качеството
на разпознаването, а логиката около него — тя е там, където се чупи:
кой alignment модел се избира, какво става, когато не се зареди, и дали
инструментът наистина никога не гърми.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import pytest

from subs import transcribe as tr
from subs.models import Transcript, Word


# --------------------------------------------------------------------------
# Двойници
# --------------------------------------------------------------------------


@dataclass
class FakeWord:
    start: float
    end: float
    word: str


@dataclass
class FakeSegment:
    words: list[FakeWord]


class FakeInfo:
    language = "bg"


class FakeWhisperModel:
    """Замества ``faster_whisper.WhisperModel``."""

    last_kwargs: dict = {}

    def __init__(self, model, device=None, compute_type=None):
        self.model, self.device, self.compute_type = model, device, compute_type

    def transcribe(self, path, **kwargs):
        FakeWhisperModel.last_kwargs = kwargs
        words = [FakeWord(0.0, 0.4, " Тази"), FakeWord(0.5, 1.0, " програма ")]
        return [FakeSegment(words)], FakeInfo()


@pytest.fixture
def fake_faster_whisper(monkeypatch):
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setattr(tr, "extract_audio", lambda source, destination: None)
    return module


def make_whisperx(align_result=None, fail_models=()):
    """Двойник на whisperx; ``fail_models`` гърмят при зареждане."""
    module = types.ModuleType("whisperx")
    module.loaded: list[str] = []

    def load_audio(path):
        return object()

    def load_align_model(language_code, device, model_name=None):
        module.loaded.append(model_name)
        if model_name in fail_models:
            raise RuntimeError(f"не може да се изтегли {model_name}")
        return object(), {}

    def align(segments, model, metadata, audio, device, return_char_alignments=False):
        if align_result is None:
            return {"segments": [{"words": [
                {"word": "Тази", "start": 0.02, "end": 0.41},
                {"word": "програма", "start": 0.52, "end": 1.03},
            ]}]}
        return align_result

    module.load_audio = load_audio
    module.load_align_model = load_align_model
    module.align = align
    return module


# --------------------------------------------------------------------------
# Регистърът с alignment модели
# --------------------------------------------------------------------------


def test_bulgarian_has_a_model_and_is_marked_approximate():
    name, approximate = tr.align_model_for("bg")
    assert name == tr.ALIGN_MODELS["bg"]
    assert approximate, "българският няма официален модел — трябва да е отбелязан"


def test_turkish_is_exact():
    name, approximate = tr.align_model_for("tr")
    assert name == tr.ALIGN_MODELS["tr"]
    assert not approximate


def test_unknown_language_has_no_model():
    assert tr.align_model_for("sw") == (None, False)


def test_explicit_override_wins():
    assert tr.align_model_for("bg", "мой/модел") == ("мой/модел", False)


def test_language_code_with_region_still_matches():
    assert tr.align_model_for("bg-BG")[0] == tr.ALIGN_MODELS["bg"]


# --------------------------------------------------------------------------
# Разпознаване
# --------------------------------------------------------------------------


def test_recognition_strips_whitespace_and_keeps_timings(fake_faster_whisper, monkeypatch, tmp_path):
    monkeypatch.setattr(tr, "_align", lambda *a, **k: None)
    result = tr.transcribe(_touch(tmp_path), tr.TranscribeOptions(align=False))
    assert [w.text for w in result.words] == ["Тази", "програма"]
    assert result.words[0].start == pytest.approx(0.0)
    assert result.words[1].end == pytest.approx(1.0)


def test_word_timestamps_are_always_requested(fake_faster_whisper, tmp_path):
    tr.transcribe(_touch(tmp_path), tr.TranscribeOptions(align=False))
    assert FakeWhisperModel.last_kwargs["word_timestamps"] is True


def test_missing_faster_whisper_gives_an_instruction(monkeypatch, tmp_path):
    monkeypatch.setattr(tr, "extract_audio", lambda source, destination: None)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(RuntimeError, match="pip install"):
        tr.transcribe(_touch(tmp_path), tr.TranscribeOptions())


def test_unreachable_model_gives_an_actionable_message(monkeypatch, tmp_path):
    """Първото пускане тегли модела; при липса на мрежа това е най-честата
    засечка и трябва да казва къде се кешира и какво да се пробва."""

    class Unreachable:
        def __init__(self, *args, **kwargs):
            raise OSError("Tunnel connection failed: 403 Forbidden")

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = Unreachable
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setattr(tr, "extract_audio", lambda source, destination: None)

    with pytest.raises(RuntimeError) as caught:
        tr.transcribe(_touch(tmp_path), tr.TranscribeOptions(model="tiny"))
    message = str(caught.value)
    assert "tiny" in message and "HuggingFace" in message and "--model" in message


def test_failure_while_decoding_is_wrapped_too(fake_faster_whisper, monkeypatch, tmp_path):
    def explode(self, path, **kwargs):
        raise RuntimeError("ctranslate2 се задави")

    monkeypatch.setattr(FakeWhisperModel, "transcribe", explode)
    with pytest.raises(RuntimeError, match="разпознаването се провали"):
        tr.transcribe(_touch(tmp_path), tr.TranscribeOptions(align=False))


def test_missing_file_is_reported_before_any_model_is_loaded(tmp_path):
    with pytest.raises(FileNotFoundError):
        tr.transcribe(tmp_path / "няма.mp4")


# --------------------------------------------------------------------------
# Подравняване и падане назад
# --------------------------------------------------------------------------


def _transcript(language="bg"):
    return Transcript(
        words=[Word("Тази", 0.0, 0.4), Word("програма", 0.5, 1.0)],
        language=language,
    )


def test_alignment_replaces_timings(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "whisperx", make_whisperx())
    data = _transcript()
    tr._align(tmp_path / "a.wav", data, tr.TranscribeOptions(device="cpu"))
    assert data.aligned
    assert data.words[0].start == pytest.approx(0.02)


def test_bulgarian_falls_back_to_the_russian_model(monkeypatch, tmp_path):
    module = make_whisperx(fail_models=(tr.ALIGN_MODELS["bg"],))
    monkeypatch.setitem(sys.modules, "whisperx", module)
    data = _transcript()
    tr._align(tmp_path / "a.wav", data, tr.TranscribeOptions(device="cpu"))
    assert module.loaded == [tr.ALIGN_MODELS["bg"], tr.ALIGN_FALLBACKS["bg"]]
    assert data.aligned
    assert any("резервен" in note for note in data.notes)


def test_every_model_failing_keeps_the_original_timings(monkeypatch, tmp_path):
    failing = (tr.ALIGN_MODELS["bg"], tr.ALIGN_FALLBACKS["bg"])
    monkeypatch.setitem(sys.modules, "whisperx", make_whisperx(fail_models=failing))
    data = _transcript()
    before = [(w.text, w.start) for w in data.words]
    tr._align(tmp_path / "a.wav", data, tr.TranscribeOptions(device="cpu"))
    assert not data.aligned
    assert [(w.text, w.start) for w in data.words] == before
    assert any("не сработи" in note for note in data.notes)


def test_language_without_model_is_noted_not_raised(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "whisperx", make_whisperx())
    data = _transcript(language="sw")
    tr._align(tmp_path / "a.wav", data, tr.TranscribeOptions(device="cpu"))
    assert not data.aligned
    assert any("--align-model" in note for note in data.notes)


def test_half_lost_words_count_as_failure(monkeypatch, tmp_path):
    thin = {"segments": [{"words": [{"word": "Тази", "start": 0.0, "end": 0.4}]}]}
    monkeypatch.setitem(sys.modules, "whisperx", make_whisperx(align_result=thin))
    data = Transcript(words=[Word(f"д{i}", i * 0.3, i * 0.3 + 0.2) for i in range(10)],
                      language="bg")
    tr._align(tmp_path / "a.wav", data, tr.TranscribeOptions(device="cpu"))
    assert not data.aligned, "изгубени думи не бива да минават за успешно подравняване"
    assert len(data.words) == 10


def test_manual_flags_survive_alignment(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "whisperx", make_whisperx())
    data = _transcript()
    data.words[1].emphasis = True
    data.words[1].accent = True
    tr._align(tmp_path / "a.wav", data, tr.TranscribeOptions(device="cpu"))
    marked = next(w for w in data.words if w.text == "програма")
    assert marked.emphasis and marked.accent


# --------------------------------------------------------------------------
# Устройство
# --------------------------------------------------------------------------


def test_cpu_uses_int8_and_cuda_float16():
    assert tr.resolve_device("cpu") == ("cpu", "int8")
    assert tr.resolve_device("cuda") == ("cuda", "float16")


def test_auto_falls_back_to_cpu_without_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    assert tr.resolve_device("auto")[0] == "cpu"


def _touch(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"")
    return path


def test_missing_whisperx_is_explained_not_swallowed(monkeypatch, tmp_path):
    """На Python 3.14 whisperx не се инсталира. Бележката трябва да казва
    защо таймингите са по-груби, а не да мълчи или да гърми."""
    monkeypatch.setitem(sys.modules, "whisperx", None)
    data = _transcript()
    tr._align(tmp_path / "a.wav", data, tr.TranscribeOptions(device="cpu"))
    assert not data.aligned
    assert any("whisperx не е инсталиран" in note for note in data.notes)
    assert any("3.14" in note for note in data.notes)


def test_missing_whisperx_keeps_the_original_words(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "whisperx", None)
    data = _transcript()
    before = [(w.text, w.start, w.end) for w in data.words]
    tr._align(tmp_path / "a.wav", data, tr.TranscribeOptions(device="cpu"))
    assert [(w.text, w.start, w.end) for w in data.words] == before
