"""Растежът на ключовата дума в стил „Зад кадъра" не бива да излиза от видеото.

``BehindStyle.scale_end`` е един и същ таван за всяка дума, но една вече
широка дума (дълъг текст, или ръчно увеличена през ``word.scale``) достига
край на растежа, който е по-широк от самия кадър — думата се реже на ръба.
``RasterBehindRenderer._key_growth`` смята таван per-дума, за да не се стига
дотам.
"""

from __future__ import annotations

import pytest

from subs.burn import MediaInfo
from subs.models import Placed
from subs.renderers.raster_behind import RasterBehindRenderer
from subs.styles import BehindStyle

MEDIA = MediaInfo(width=1080, height=1920, fps=30.0, duration=10.0, has_audio=True)


def _key(width: float, height: float = 150.0) -> Placed:
    return Placed(text="ДУМА", x=(MEDIA.width - width) / 2.0, y=800.0, size=height,
                 kind="highlight", start=0.0, end=1.0, visible_from=0.0,
                 hidden_after=1.0, width=width, height=height)


def test_key_growth_matches_style_cap_for_a_word_with_room_to_grow():
    style = BehindStyle(scale_end=1.13)
    word = _key(width=500.0)
    assert RasterBehindRenderer._key_growth(word, style, MEDIA) == pytest.approx(1.13)


def test_key_growth_is_clamped_so_the_word_never_overflows_the_frame():
    style = BehindStyle(scale_end=1.13)
    # key_fill=0.98 в реалния стил вече дава дума близо до пълната ширина —
    # растежът по подразбиране (1.13x) би я преляла.
    word = _key(width=MEDIA.width * 0.98)
    growth = RasterBehindRenderer._key_growth(word, style, MEDIA)

    assert growth < style.scale_end
    assert word.width * growth <= MEDIA.width + 1e-6


def test_key_growth_never_shrinks_below_the_laid_out_size():
    style = BehindStyle(scale_end=1.13)
    # Патологичен случай: думата вече е по-широка от кадъра (напр. ръчно
    # увеличена през word.scale) — растежът не бива да я смалява допълнително.
    word = _key(width=MEDIA.width * 1.5)
    assert RasterBehindRenderer._key_growth(word, style, MEDIA) == pytest.approx(1.0)


def test_key_growth_also_bounds_by_height():
    style = BehindStyle(scale_end=2.0)
    word = _key(width=100.0, height=MEDIA.height * 0.9)
    growth = RasterBehindRenderer._key_growth(word, style, MEDIA)

    assert growth < style.scale_end
    assert word.height * growth <= MEDIA.height + 1e-6


def test_key_growth_defaults_to_style_cap_for_a_zero_sized_word():
    style = BehindStyle(scale_end=1.13)
    word = _key(width=0.0, height=0.0)
    assert RasterBehindRenderer._key_growth(word, style, MEDIA) == pytest.approx(1.13)
