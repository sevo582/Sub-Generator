"""Тестове за оформлението.

Всичко тук се проверява без видео и без ffmpeg — това е и причината
``layout.py`` да не знае нищо за тях.
"""

from __future__ import annotations

import pytest

from subs.layout import (choose_highlight, deoverlap, is_number, layout_behind,
                         layout_stack, normalize, split_blocks)
from subs.models import Block, Word
from subs.styles import BlockRules, HighlightRules, get_style
from subs.textmetrics import measurer

WIDTH, HEIGHT = 1080, 1920


def words(text: str, per: float = 0.4, start: float = 0.0, gap: float = 0.0) -> list[Word]:
    out: list[Word] = []
    time = start
    for token in text.split():
        out.append(Word(token, round(time, 3), round(time + per, 3)))
        time += per + gap
    return out


@pytest.fixture
def style():
    return get_style("stack")


# --------------------------------------------------------------------------
# Разбиване на блокове
# --------------------------------------------------------------------------


def test_empty_input_gives_no_blocks():
    assert split_blocks([], BlockRules()) == []


def test_every_word_lands_in_exactly_one_block():
    source = words("едно две три четири пет шест седем осем девет десет единайсет")
    blocks = split_blocks(source, BlockRules())
    flattened = [w for block in blocks for w in block.words]
    assert flattened == source


def test_block_respects_maximum_words():
    source = words("а б в г д е ж з и к л м н о п")
    rules = BlockRules(min_words=4, max_words=6)
    for block in split_blocks(source, rules):
        assert len(block.words) <= 6


def test_long_pause_splits_even_below_minimum():
    source = words("една дума", per=0.3)
    source += [Word("после", 5.0, 5.3), Word("още", 5.4, 5.7)]
    blocks = split_blocks(source, BlockRules(min_words=4, split_gap=0.4))
    assert len(blocks) == 2
    assert blocks[0].words[-1].text == "дума"


def test_punctuation_closes_block_after_minimum():
    source = words("едно две три четири. пет шест седем осем")
    blocks = split_blocks(source, BlockRules(min_words=4, max_words=8))
    assert blocks[0].words[-1].text == "четири."


def test_punctuation_does_not_split_below_minimum():
    source = words("да. три четири пет шест седем")
    blocks = split_blocks(source, BlockRules(min_words=4, max_words=8))
    assert len(blocks[0].words) >= 4


def test_duration_cap_closes_long_block():
    source = words("едно две три четири пет шест седем осем", per=0.9)
    blocks = split_blocks(source, BlockRules(min_words=3, max_words=8, max_duration=2.5))
    assert all(block.end - block.start <= 4.0 for block in blocks)
    assert len(blocks) > 1


# --------------------------------------------------------------------------
# Избор на подчертана дума
# --------------------------------------------------------------------------


def test_highlight_skips_function_words():
    block = Block(words("и в на който програмата за"))
    index = choose_highlight(block, HighlightRules(), "bg")
    assert block.words[index].text == "програмата"


def test_highlight_prefers_numbers():
    block = Block(words("струва само 250 лева на месец"))
    index = choose_highlight(block, HighlightRules(), "bg")
    assert block.words[index].text == "250"


def test_highlight_prefers_all_caps_brand():
    block = Block(words("правим това с PNG файлове винаги"))
    index = choose_highlight(block, HighlightRules(), "bg")
    assert block.words[index].text == "PNG"


def test_manual_emphasis_overrides_heuristic():
    block = Block(words("това е дълга неразбираема безсмислица"))
    block.words[0].emphasis = True
    assert choose_highlight(block, HighlightRules(), "bg") == 0


def test_highlight_falls_back_when_all_words_are_function_words():
    block = Block(words("и в на за"))
    index = choose_highlight(block, HighlightRules(), "bg")
    assert 0 <= index < len(block.words)


def test_turkish_function_words_are_recognised():
    block = Block(words("için de bir kasma yaşamıyorsun"))
    index = choose_highlight(block, HighlightRules(), "tr")
    assert block.words[index].text == "yaşamıyorsun"


def test_normalize_and_number_helpers():
    assert normalize("«Тестът».") == "тестът"
    assert is_number("30") and is_number("2,5") and is_number("15%")
    assert not is_number("тест")


# --------------------------------------------------------------------------
# Стил A — позиции
# --------------------------------------------------------------------------


def _stack(text: str, style, highlight: int | None = None):
    block = Block(words(text))
    block.highlight = (choose_highlight(block, style.highlight, "bg")
                       if highlight is None else highlight)
    return block, layout_stack(block, style.stack, measurer(style.stack.font),
                               WIDTH, HEIGHT)


def test_every_word_gets_a_position(style):
    block, layout = _stack("днес правим нещо много по-различно от вчера", style)
    assert [p.text for p in layout.placed] == [w.text for w in block.words]


def test_lines_zigzag_horizontally(style):
    # Къси думи, за да не се намеси подрязването при преливащ ред.
    _, layout = _stack("син зим кон вол рак сом кит", style, highlight=0)
    lefts = {round(p.x) for p in layout.placed}
    left = round(style.stack.margin_x * WIDTH)
    shifted = round(left + style.stack.zigzag * WIDTH)
    assert {left, shifted} <= lefts, f"очаквани котви {left} и {shifted}, получени {lefts}"


def test_overflowing_line_is_pulled_back_inside_the_frame(style):
    _, layout = _stack("днес правим нещо много по-различно от вчера", style)
    limit = WIDTH - style.stack.margin_x * WIDTH
    for placed in layout.placed:
        if placed.width <= limit - style.stack.margin_x * WIDTH:
            assert placed.x + placed.width <= limit + 1


def test_block_grows_downwards(style):
    _, layout = _stack("днес правим нещо много по-различно от вчера", style)
    tops = [p.y for p in layout.placed]
    assert tops == sorted(tops)
    assert tops[0] == pytest.approx(style.stack.block_top * HEIGHT, abs=1)


def test_highlight_is_larger_and_alone_on_its_line(style):
    block, layout = _stack("днес правим нещо много по-различно от вчера", style)
    highlight = layout.placed[block.highlight]
    assert highlight.size > style.stack.size_normal * HEIGHT
    same_row = [p for p in layout.placed if p.y == highlight.y]
    assert same_row == [highlight]


def test_short_word_pairs_with_next_long_word_stands_alone(style):
    block = Block(words("Ve PNGleri kullandigi icin de timelineda herhangi bir"))
    block.highlight = 5
    layout = layout_stack(block, style.stack, measurer(style.stack.font), WIDTH, HEIGHT)
    rows: dict[float, list[str]] = {}
    for placed in layout.placed:
        rows.setdefault(placed.y, []).append(placed.text)
    lines = [rows[y] for y in sorted(rows)]
    assert ["Ve", "PNGleri"] in lines, "късата дума взима следващата"
    assert ["kullandigi"] in lines, "дългата дума стои сама"


def test_very_long_word_stays_inside_the_frame(style):
    block = Block(words("невероятноневъзможнодългадума"))
    block.highlight = 0
    layout = layout_stack(block, style.stack, measurer(style.stack.font), WIDTH, HEIGHT)
    placed = layout.placed[0]
    assert placed.x >= 0
    # Думата може да е по-широка от лимита, но не бива да започва извън кадъра.
    assert placed.x <= WIDTH


def test_tall_block_is_pushed_up_to_stay_visible(style):
    block = Block(words(" ".join(f"дума{i}" for i in range(8))))
    block.highlight = 0
    layout = layout_stack(block, style.stack, measurer(style.stack.font), WIDTH, HEIGHT)
    bottom = max(p.y + p.height for p in layout.placed)
    assert bottom <= style.stack.block_max_bottom * HEIGHT + 1


def test_words_are_visible_from_the_start_by_default(style):
    _, layout = _stack("днес правим нещо много по-различно от вчера", style)
    assert all(p.visible_from == pytest.approx(layout.appear) for p in layout.placed)


def test_reveal_lookahead_delays_later_words(style):
    from dataclasses import replace

    tuned = replace(style.stack, reveal_lookahead=1)
    block = Block(words("едно две три четири пет шест"))
    block.highlight = 0
    layout = layout_stack(block, tuned, measurer(tuned.font), WIDTH, HEIGHT)
    assert layout.placed[0].visible_from == pytest.approx(layout.appear)
    assert layout.placed[3].visible_from > layout.appear


def test_word_turns_solid_at_its_own_start(style):
    block, layout = _stack("днес правим нещо много по-различно от вчера", style)
    for placed, word in zip(layout.placed, block.words):
        assert placed.start == pytest.approx(word.start)


# --------------------------------------------------------------------------
# Стил B — позиции
# --------------------------------------------------------------------------


def _behind(text: str, highlight: int):
    style = get_style("behind")
    block = Block(words(text))
    block.highlight = highlight
    layout = layout_behind(block, style.behind, measurer(style.behind.font_key),
                           measurer(style.behind.font_plain), WIDTH, HEIGHT)
    return style, block, layout


def test_behind_key_word_is_uppercase_and_centred():
    style, _, layout = _behind("this golf course takes", 2)
    key = next(p for p in layout.placed if p.kind == "highlight")
    assert key.text == "COURSE"
    centre = key.x + key.width / 2
    assert centre == pytest.approx(WIDTH / 2, abs=2)


def test_behind_key_word_fills_the_frame_width():
    style, _, layout = _behind("this golf course takes", 2)
    key = next(p for p in layout.placed if p.kind == "highlight")
    assert key.width == pytest.approx(style.behind.key_fill * WIDTH, rel=0.05)


def test_behind_short_and_long_key_words_get_the_same_width():
    _, _, short = _behind("a golf takes", 1)
    _, _, long_ = _behind("a vacation takes", 1)
    a = next(p for p in short.placed if p.kind == "highlight")
    b = next(p for p in long_.placed if p.kind == "highlight")
    assert a.width == pytest.approx(b.width, rel=0.05)
    assert a.size > b.size, "по-късата дума получава по-едър кегел"


def test_behind_text_stays_in_the_upper_band():
    style, _, layout = _behind("this golf course takes", 2)
    assert min(p.y for p in layout.placed) >= style.behind.top * HEIGHT - 1
    assert max(p.y + p.height for p in layout.placed) <= style.behind.bottom * HEIGHT + 1


def test_behind_plain_words_sit_above_and_below_the_key_word():
    _, _, layout = _behind("this golf course takes", 2)
    key = next(p for p in layout.placed if p.kind == "highlight")
    above = [p for p in layout.placed if p.y < key.y]
    below = [p for p in layout.placed if p.y > key.y]
    assert [p.text for p in above] == ["this", "golf"]
    assert [p.text for p in below] == ["takes"]


def test_behind_plain_words_stay_inside_the_margins():
    style, _, layout = _behind("this golf course takes", 2)
    margin = style.behind.margin_x * WIDTH
    for placed in layout.placed:
        if placed.kind == "normal":
            assert placed.x >= margin - 1


# --------------------------------------------------------------------------
# Застъпване между блокове
# --------------------------------------------------------------------------


def test_blocks_do_not_overlap_in_time(style):
    source = words("едно две три четири пет шест седем осем девет десет", per=0.2)
    blocks = split_blocks(source, style.blocks)
    for block in blocks:
        block.highlight = choose_highlight(block, style.highlight, "bg")
    metrics = measurer(style.stack.font)
    layouts = deoverlap([layout_stack(b, style.stack, metrics, WIDTH, HEIGHT)
                         for b in blocks])
    for current, following in zip(layouts, layouts[1:]):
        assert current.disappear <= following.appear + 1e-6


def test_first_block_never_appears_before_zero(style):
    block = Block(words("едно две три четири", start=0.0))
    block.highlight = 0
    layout = deoverlap([layout_stack(block, style.stack, measurer(style.stack.font),
                                     WIDTH, HEIGHT)])[0]
    assert layout.appear >= 0.0


# --------------------------------------------------------------------------
# Цвят и анимация на отделна дума
# --------------------------------------------------------------------------


def test_word_colour_and_animation_reach_the_renderer(style):
    block = Block(words("едно две три четири"))
    block.words[1].color = "#FF3B30"
    block.words[1].animation = "изскачане"
    block.highlight = 0
    layout = layout_stack(block, style.stack, measurer(style.stack.font), WIDTH, HEIGHT)
    placed = layout.placed[1]
    assert placed.color == "#FF3B30" and placed.animation == "изскачане"


def test_behind_carries_colour_to_the_key_word():
    style = get_style("behind")
    block = Block(words("това голф игрище"))
    block.words[2].color = "#30D158"
    block.highlight = 2
    layout = layout_behind(block, style.behind, measurer(style.behind.font_key),
                           measurer(style.behind.font_plain), WIDTH, HEIGHT)
    key = next(p for p in layout.placed if p.kind == "highlight")
    assert key.color == "#30D158"


def test_words_without_settings_stay_neutral(style):
    block = Block(words("едно две три четири"))
    block.highlight = 0
    layout = layout_stack(block, style.stack, measurer(style.stack.font), WIDTH, HEIGHT)
    assert all(p.color is None and p.animation == "няма" for p in layout.placed)
