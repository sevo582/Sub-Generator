"""Ефектът „блести": пулсиращо просветване, докато думата стои плътна.

За разлика от другите три анимации, това не е еднократен вход — трае цялото
време, в което думата се изрича. Затова двете половини (растерния рендерер и
ASS) имат отделни, но огледални механизми: тук се проверяват и двата.
"""

from __future__ import annotations

import pytest

from subs.raster import sparkle_phase
from subs.renderers.ass_stack import AssStackRenderer
from subs.styles import StackStyle


def test_sparkle_phase_is_zero_before_the_word_starts():
    assert sparkle_phase(0.5, 1.0, 200) == 0.0


def test_sparkle_phase_peaks_at_half_period():
    assert sparkle_phase(1.1, 1.0, 200) == pytest.approx(1.0)


def test_sparkle_phase_returns_to_zero_at_a_full_period():
    assert sparkle_phase(1.2, 1.0, 200) == pytest.approx(0.0, abs=1e-6)


def test_sparkle_phase_keeps_pulsing_after_several_periods():
    """За разлика от ``entry_phase``, не спира при 1 — тупти, докато трае."""
    assert sparkle_phase(3.1, 1.0, 200) == pytest.approx(1.0)


def test_sparkle_phase_is_disabled_with_a_zero_period():
    assert sparkle_phase(5.0, 1.0, 0) == 0.0


def test_ass_sparkle_pulses_scale_up_and_back():
    style = StackStyle(sparkle_ms=200, sparkle_scale=1.1, sparkle_color="#FF0000")
    scale, _colour = AssStackRenderer._sparkle(style, "&H00FFFFFF&", duration_ms=200)

    assert "\\fscx110\\fscy110" in scale
    assert "\\fscx100\\fscy100" in scale


def test_ass_sparkle_alternates_between_bright_and_base_colour():
    style = StackStyle(sparkle_ms=200, sparkle_scale=1.1, sparkle_color="#FF0000")
    _scale, colour = AssStackRenderer._sparkle(style, "&H00FFFFFF&", duration_ms=200)

    assert "\\1c&H0000FF&" in colour  # #FF0000 -> &HBBGGRR&
    assert "\\1c&H00FFFFFF&" in colour  # връща се към подадения базов цвят


def test_ass_sparkle_covers_the_full_duration_with_several_pulses():
    style = StackStyle(sparkle_ms=100)
    scale, colour = AssStackRenderer._sparkle(style, "&H00FFFFFF&", duration_ms=350)

    assert scale.count("\\t(") == colour.count("\\t(") == 7  # 3 пълни + 1 отрязан цикъл


def test_ass_sparkle_is_empty_when_the_word_has_no_solid_duration():
    style = StackStyle()
    assert AssStackRenderer._sparkle(style, "&H00FFFFFF&", duration_ms=0) == ("", "")


def test_ass_placement_centres_sparkle_like_the_pop_animation():
    """Мащабирането в ASS е спрямо котвата — без \\an5 думата би „набъбвала“
    надясно-надолу вместо на място, точно като при „изскачане“."""
    from subs.models import Placed

    word = Placed(text="дума", x=10.0, y=20.0, size=40.0, kind="normal",
                 start=0.0, end=1.0, visible_from=0.0, hidden_after=1.0,
                 animation="блести")
    style = StackStyle()
    tags = AssStackRenderer._placement(word, word.size, style, height=1920, animate=True)
    assert tags.startswith("\\an5\\pos(")
