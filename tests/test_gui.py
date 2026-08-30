"""Логиката зад прозореца.

Тук няма tkinter — проверява се само това, което стои между таблицата и
конвейера: превръщането на редове в думи и обратно, четенето на времена
както ги пише човек, и проверките, които влизат в дневника.
"""

from __future__ import annotations

import pytest

from subs.gui import (LANGUAGES, MODELS, Row, default_output, format_time,
                      parse_time, rows_from_transcript, transcript_from_rows,
                      validate)
from subs.models import Transcript, Word
from pathlib import Path


def test_decimal_comma_is_accepted():
    """На български десетичната е запетая — човек ще напише 1,25."""
    assert parse_time("1,25") == pytest.approx(1.25)
    assert parse_time(" 2.5 ") == pytest.approx(2.5)


def test_negative_and_nonsense_times_are_rejected():
    with pytest.raises(ValueError):
        parse_time("-1")
    with pytest.raises(ValueError):
        parse_time("кръгла нула")


def test_time_is_shown_with_two_decimals():
    assert format_time(1.2345) == "1.23"
    assert format_time(0) == "0.00"


def test_row_shows_its_marks():
    assert Row("а", 0, 1).values()[3] == ""
    assert Row("а", 0, 1, emphasis=True).values()[3] == "★"
    assert Row("а", 0, 1, emphasis=True, accent=True).values()[3] == "★●"


def test_roundtrip_through_the_table_keeps_everything():
    original = Transcript(
        words=[Word("Тази", 0.2, 0.5), Word("PNG", 0.6, 1.0, emphasis=True, accent=True)],
        language="bg")
    rows = rows_from_transcript(original)
    restored = transcript_from_rows(rows, "bg")
    assert [(w.text, w.start, w.end, w.emphasis, w.accent) for w in restored.words] == \
           [(w.text, w.start, w.end, w.emphasis, w.accent) for w in original.words]
    assert restored.language == "bg"


def test_unknown_language_is_not_written_as_none():
    assert transcript_from_rows([Row("а", 0, 1)], None).language == "unknown"


def test_validate_is_quiet_when_everything_is_fine():
    assert validate([Row("едно", 0.0, 0.4), Row("две", 0.5, 0.9)]) == []


def test_validate_reports_empty_text():
    assert any("празен" in p for p in validate([Row("  ", 0.0, 0.4)]))


def test_validate_reports_reversed_times():
    assert any("преди началото" in p for p in validate([Row("а", 1.0, 0.5)]))


def test_validate_reports_overlap():
    rows = [Row("едно", 0.0, 1.0), Row("две", 0.5, 1.5)]
    assert any("застъпват" in p for p in validate(rows))


def test_validate_points_at_the_right_rows():
    rows = [Row("едно", 0.0, 0.4), Row("две", 0.5, 0.9), Row("", 1.0, 1.4)]
    assert any("ред 3" in p for p in validate(rows))


def test_default_output_carries_the_style_name():
    assert default_output(Path("/тук/reel.mov"), "behind").name == "reel.behind.mp4"


def test_language_choices_cover_the_priority_languages():
    codes = {code for _, code in LANGUAGES}
    assert {"bg", "tr", "en"} <= codes
    assert None in codes, "трябва да има и автоматично разпознаване"


def test_models_are_ordered_from_small_to_large():
    assert MODELS[0] == "tiny" and MODELS[-1].startswith("large")
