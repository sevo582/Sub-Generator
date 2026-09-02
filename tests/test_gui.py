"""Логиката зад прозореца.

Тук няма tkinter — проверява се само това, което стои между таблицата и
конвейера: превръщането на редове в думи и обратно, четенето на времена
както ги пише човек, и проверките, които влизат в дневника.
"""

from __future__ import annotations

import pytest

from subs.gui import (LANGUAGES, MODELS, Row, default_output, format_time,
                      parse_time, preview_window, rows_from_transcript,
                      transcript_from_rows, validate)
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


# --------------------------------------------------------------------------
# Цвят, анимация и преглед на парче
# --------------------------------------------------------------------------


def test_row_shows_colour_and_animation():
    values = Row("а", 0, 1, color="#FF3B30", animation="изскачане").values()
    assert values[4] == "#FF3B30" and values[5] == "изскачане"


def test_row_without_colour_shows_a_dash():
    assert Row("а", 0, 1).values()[4] == "—"


def test_middle_is_where_the_frame_is_drawn():
    assert Row("а", 1.0, 2.0).middle == pytest.approx(1.5)


def test_colour_and_animation_survive_the_table():
    original = Transcript(
        words=[Word("а", 0, 1, color="#0A84FF", animation="издигане")], language="bg")
    restored = transcript_from_rows(rows_from_transcript(original), "bg")
    assert restored.words[0].color == "#0A84FF"
    assert restored.words[0].animation == "издигане"


def test_palette_entries_are_valid_hex():
    from subs.gui import PALETTE

    for colour in PALETTE:
        assert len(colour) == 7 and colour.startswith("#")
        int(colour[1:], 16)


def test_preview_window_starts_a_little_before_the_word():
    rows = [Row("а", 0.0, 0.4), Row("б", 2.0, 2.4)]
    start, length = preview_window(rows, 1, 3.0, 10.0)
    assert start == pytest.approx(1.6), "тръгва преди думата, за да се види как влиза"
    assert length == pytest.approx(3.0)


def test_preview_window_never_starts_before_zero():
    assert preview_window([Row("а", 0.1, 0.4)], 0, 3.0, 10.0)[0] == 0.0


def test_preview_window_is_clipped_to_the_video():
    rows = [Row("а", 4.8, 5.0)]
    start, length = preview_window(rows, 0, 3.0, 5.0)
    assert start + length <= 5.0 + 1e-6


def test_preview_window_survives_an_empty_table():
    assert preview_window([], 0, 3.0, 5.0) == (0.0, 3.0)


def test_animations_are_the_ones_the_renderers_know():
    from subs.models import ANIMATIONS

    assert ANIMATIONS[0] == "няма"
    assert set(ANIMATIONS) == {"няма", "изскачане", "издигане", "избледняване"}


# --------------------------------------------------------------------------
# Местоположение на думата
# --------------------------------------------------------------------------


def test_nudge_moves_by_one_step():
    from subs.gui import NUDGE_STEP, nudged

    assert nudged(0.0, 0.0, 1, 0) == (pytest.approx(NUDGE_STEP), 0.0)
    assert nudged(0.0, 0.0, 0, -1) == (0.0, pytest.approx(-NUDGE_STEP))


def test_nudges_accumulate():
    from subs.gui import NUDGE_STEP, nudged

    dx, dy = 0.0, 0.0
    for _ in range(3):
        dx, dy = nudged(dx, dy, 1, 1)
    assert dx == pytest.approx(3 * NUDGE_STEP)
    assert dy == pytest.approx(3 * NUDGE_STEP)


def test_offset_is_shown_in_pixels_of_the_current_video():
    """Дробта е за да работи на всяка резолюция; човек мисли в пиксели."""
    row = Row("а", 0, 1, dx=0.005, dy=-0.01)
    assert row.values(1920)[7] == "+10, -19"
    assert row.values(1280)[7] == "+6, -13"


def test_no_offset_shows_a_dash():
    assert Row("а", 0, 1).values(1920)[7] == "—"


def test_offset_survives_the_table():
    original = Transcript(words=[Word("а", 0, 1, dx=0.02, dy=-0.03)], language="bg")
    restored = transcript_from_rows(rows_from_transcript(original), "bg")
    assert restored.words[0].dx == pytest.approx(0.02)
    assert restored.words[0].dy == pytest.approx(-0.03)
