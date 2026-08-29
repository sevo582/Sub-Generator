"""Четене на метаданните на видеото.

Ударната точка тук е завъртането: видео, снимано вертикално с телефон, се
записва легнало плюс флаг. ffprobe връща записаните размери, ffmpeg подава
на филтрите вече завъртения кадър. Всяка сметка за позиции, направена върху
първите, излиза грешна.
"""

from __future__ import annotations

import pytest

from subs.burn import ALPHA_PIX_FMT_PREFIXES, stream_rotation


def test_no_metadata_means_no_rotation():
    assert stream_rotation({"width": 1920, "height": 1080}) == 0


def test_rotation_from_the_display_matrix():
    stream = {"side_data_list": [{"side_data_type": "Display Matrix", "rotation": 90}]}
    assert stream_rotation(stream) == 90


def test_negative_rotation_is_normalised():
    assert stream_rotation({"side_data_list": [{"rotation": -90}]}) == 270


def test_rotation_from_the_legacy_tag():
    assert stream_rotation({"tags": {"rotate": "270"}}) == 270


def test_display_matrix_wins_over_the_tag():
    stream = {"side_data_list": [{"rotation": 90}], "tags": {"rotate": "180"}}
    assert stream_rotation(stream) == 90


def test_float_rotation_is_rounded():
    assert stream_rotation({"side_data_list": [{"rotation": -90.0}]}) == 270


def test_nonsense_rotation_is_ignored_not_raised():
    assert stream_rotation({"tags": {"rotate": "наляво"}}) == 0
    assert stream_rotation({"side_data_list": [{"rotation": None}]}) == 0


def test_side_data_without_rotation_is_skipped():
    stream = {"side_data_list": [{"side_data_type": "Something Else"}],
              "tags": {"rotate": "90"}}
    assert stream_rotation(stream) == 90


@pytest.mark.parametrize("pix_fmt,expected", [
    ("yuva444p10le", True), ("yuva420p", True), ("rgba", True),
    ("yuv420p", False), ("yuv444p", False),
])
def test_alpha_pixel_formats_are_recognised(pix_fmt, expected):
    assert pix_fmt.startswith(ALPHA_PIX_FMT_PREFIXES) is expected
