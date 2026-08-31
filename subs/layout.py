"""Оформление: блокове, подчертана дума, позиции.

Този модул не знае нищо за ffmpeg, ASS или PIL — влизат думи с тайминги,
излизат позиции в пиксели. Затова е и единственото място в проекта, което
се тества без видео.

Двата стила споделят първите две стъпки (``split_blocks`` и
``choose_highlight``) и се разминават чак при разполагането.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Callable

from .models import Block, BlockLayout, Placed, Word
from .stopwords import stopwords_for
from .styles import BehindStyle, BlockRules, HighlightRules, StackStyle
from .textmetrics import Measurer

#: Знаци, които махаме от края на думата, преди да я мерим или сравняваме.
_TRIM = " \t\n\"'«»„“”()[]{}.,!?…:;-–—"


def normalize(text: str) -> str:
    """Дума без пунктуация и регистър — за сравнение със служебните думи."""
    return unicodedata.normalize("NFC", text).strip(_TRIM).lower()


def is_number(text: str) -> bool:
    core = normalize(text).replace(",", "").replace(".", "").replace("%", "")
    return bool(core) and any(c.isdigit() for c in core)


# --------------------------------------------------------------------------
# 1. Разбиване на блокове
# --------------------------------------------------------------------------


def split_blocks(words: list[Word], rules: BlockRules) -> list[Block]:
    """Разбива потока от думи на блокове.

    Блокът се затваря при: достигнат таван на думите, пунктуация, пауза в
    говора или прекалена продължителност. Достатъчно дълга пауза затваря
    блока независимо от броя думи — иначе един блок увисва на екрана през
    цялата пауза.
    """
    if not words:
        return []

    hard_gap = rules.split_gap * 2.5
    blocks: list[Block] = []
    current: list[Word] = []

    for index, word in enumerate(words):
        current.append(word)
        is_last = index == len(words) - 1
        gap = 0.0 if is_last else words[index + 1].start - word.end
        span = word.end - current[0].start
        count = len(current)

        close = is_last or count >= rules.max_words or gap > hard_gap
        if not close and count >= rules.min_words:
            close = (
                word.text.rstrip(" \t")[-1:] in rules.split_punctuation
                or gap > rules.split_gap
                or span >= rules.max_duration
            )

        if close:
            blocks.append(Block(words=current))
            current = []

    if current:  # предпазна мрежа; ``is_last`` вече трябва да го е затворил
        blocks.append(Block(words=current))
    return blocks


# --------------------------------------------------------------------------
# 2. Избор на подчертана дума
# --------------------------------------------------------------------------


def choose_highlight(block: Block, rules: HighlightRules, language: str | None = None) -> int:
    """Връща индекса на смисловата дума в блока.

    Ръчното маркиране с ``"emphasis": true`` в JSON-а бие евристиката.
    """
    words = block.words
    if not words:
        return -1

    for index, word in enumerate(words):
        if word.emphasis:
            return index

    stops = stopwords_for(language)
    count = len(words)
    best_index = 0
    best_score = float("-inf")
    any_content = False

    for index, word in enumerate(words):
        clean = normalize(word.text)
        if not clean:
            continue

        numeric = is_number(word.text)
        core = word.text.strip(_TRIM)
        # Марките и абревиатурите са къси по природа („PNG", „AI", „ЕС"),
        # но точно те са поантата на изречението. Затова главните букви
        # освобождават думата от прага за дължина, също като числата.
        acronym = len(core) > 1 and core.isupper()
        if not (numeric or acronym) and (clean in stops or len(clean) < rules.min_length):
            continue
        any_content = True

        score = rules.length_weight * len(clean)
        if numeric:
            score += rules.number_bonus
        if acronym:
            score += rules.caps_bonus
        if count > 1:
            # Предпочитаме средата на блока пред самите му краища.
            offset = abs(index / (count - 1) - 0.5) * 2.0
            score -= rules.edge_penalty * offset

        if score > best_score:
            best_score = score
            best_index = index

    if not any_content:
        # Само служебни думи — взимаме най-дългата, за да има все пак акцент.
        best_index = max(range(count), key=lambda i: len(normalize(words[i].text)))
    return best_index


def assign_highlights(
    blocks: list[Block],
    rules: HighlightRules,
    language: str | None = None,
    chooser: "Callable[[Block], int | None] | None" = None,
) -> list[Block]:
    """Маркира по една дума във всеки блок.

    ``chooser`` е незадължителен външен избирач (например през LLM). Ако
    върне ``None`` за някой блок, за него се ползва евристиката — така
    рендирането никога не спира заради недостъпен модел.
    """
    for block in blocks:
        index = chooser(block) if chooser is not None else None
        block.highlight = (index if index is not None
                           else choose_highlight(block, rules, language))
    return blocks


# --------------------------------------------------------------------------
# 3а. Разполагане — стил A („Стълб")
# --------------------------------------------------------------------------


@dataclass
class Line:
    """Междинен резултат: думи на един ред плюс размерите му."""

    indices: list[int]
    size: float
    width: float
    height: float


def _wrap_stack_lines(
    block: Block, style: StackStyle, measurer: Measurer, video_h: int, video_w: int
) -> list[Line]:
    """Разбива блока на редове от 1–2 думи.

    Правилото е взето от референцията, а не измислено: **късата дума взима
    следващата при себе си, дългата стои сама**. Така „Ve PNG'leri" и
    „için de" се събират на ред, а „herhangi" и „timeline'da" остават сами
    — точно каквото се вижда в референция A. Обикновеното лакомо пълнене
    по две думи вместо това оставя самотни съюзи по редовете.

    Подчертаната дума винаги стои сама: толкова е по-едра, че съсед до нея
    изглежда като грешка.
    """
    size_normal = style.size_normal * video_h
    size_highlight = style.size_highlight * video_h
    max_width = style.max_line_width * video_w
    pair_limit = style.pair_below * video_w

    lines: list[Line] = []
    index = 0
    count = len(block.words)

    while index < count:
        if index == block.highlight:
            word = block.words[index]
            size = size_highlight * word.scale
            lines.append(Line([index], size, measurer.width(word.text, size),
                              size * style.line_gap))
            index += 1
            continue

        # Ръчно уголемена дума не се събира с друга на реда: съседът до нея
        # изглежда като грешка, също както при подчертаната.
        if block.words[index].scale > 1.0:
            word = block.words[index]
            size = size_normal * word.scale
            lines.append(Line([index], size, measurer.width(word.text, size),
                              size * style.line_gap))
            index += 1
            continue

        taken = [index]
        width = measurer.width(block.words[index].text, size_normal)
        while (len(taken) < style.max_words_per_line
               and width < pair_limit
               and taken[-1] + 1 < count
               and taken[-1] + 1 != block.highlight
               and block.words[taken[-1] + 1].scale == 1.0):
            candidate = taken + [taken[-1] + 1]
            text = " ".join(block.words[i].text for i in candidate)
            candidate_width = measurer.width(text, size_normal)
            if candidate_width > max_width:
                break
            taken, width = candidate, candidate_width

        lines.append(Line(taken, size_normal, width, size_normal * style.line_gap))
        index = taken[-1] + 1

    return lines


def layout_stack(
    block: Block,
    style: StackStyle,
    measurer: Measurer,
    video_w: int,
    video_h: int,
) -> BlockLayout:
    """Изчислява позициите на всички думи в един блок за стил A."""
    lines = _wrap_stack_lines(block, style, measurer, video_h, video_w)
    if not lines:
        return BlockLayout(block, [], block.start, block.end)

    total_height = sum(line.height for line in lines)
    top = style.block_top * video_h
    max_bottom = style.block_max_bottom * video_h
    if top + total_height > max_bottom:
        top = max(0.0, max_bottom - total_height)

    left = style.margin_x * video_w
    right_limit = video_w - style.margin_x * video_w
    offset = style.zigzag * video_w

    appear = block.start - style.lead_in
    disappear = block.end + style.lead_out

    placed: list[Placed] = []
    y = top
    side = 0
    previous_side = 0

    for line_index, line in enumerate(lines):
        is_highlight_line = (line.indices == [block.highlight]
                             or (len(line.indices) == 1
                                 and block.words[line.indices[0]].scale > 1.0))
        if is_highlight_line and line_index > 0:
            # Подчертаната дума стои точно под водещия си ред, не се мести
            # настрани — иначе връзката между двата реда се губи. Затова и
            # броячът на редуването не мърда: ритъмът продължава след нея.
            current_side = previous_side
        else:
            current_side = side % 2
            side += 1
        previous_side = current_side

        x = left + offset * current_side
        if x + line.width > right_limit:
            x = max(left, right_limit - line.width)

        cursor = x
        for word_index in line.indices:
            word = block.words[word_index]
            width = measurer.width(word.text, line.size)
            placed.append(
                Placed(
                    text=word.text,
                    x=cursor,
                    y=y,
                    size=line.size,
                    kind=block.kind_of(word_index),
                    start=word.start,
                    end=word.end,
                    visible_from=_visible_from(block, word_index, appear, style),
                    hidden_after=disappear,
                    accent=word.accent
                    or (style.accent_all_highlights and word_index == block.highlight),
                    color=word.color,
                    animation=word.animation,
                    width=width,
                    height=line.size,
                )
            )
            cursor += width + measurer.width(" ", line.size)

        y += line.height

    return BlockLayout(block, placed, appear, disappear)


def _visible_from(block: Block, index: int, appear: float, style: StackStyle) -> float:
    """Кога думата се появява (избледняла) на екрана.

    По подразбиране целият блок стои от самото начало — това е ключовата
    разлика от обикновените karaoke субтитри. С ``reveal_lookahead`` = N
    се показват само N думи напред, а останалите чакат реда си.
    """
    if style.reveal_lookahead is None:
        return appear
    lead = max(0, index - style.reveal_lookahead)
    return appear if lead == 0 else block.words[lead].start


# --------------------------------------------------------------------------
# 3б. Разполагане — стил B („Зад кадъра")
# --------------------------------------------------------------------------


def _fit_key(text: str, style: BehindStyle, measurer: Measurer,
             video_w: int, video_h: int) -> tuple[float, float]:
    """Размер на ключовата дума — по зададена ширина или фиксиран.

    Оразмеряването по ширина е това, което прави ефекта еднакъв за къса и
    дълга дума: „GOLF" и „VACATION" заемат кадъра еднакво, а после и двете
    излизат извън него, докато растат.
    """
    if style.key_fill is None:
        size = style.size_key * video_h
    else:
        probe = style.size_key * video_h
        width = measurer.width(text, probe)
        size = probe if width <= 0 else probe * (style.key_fill * video_w) / width
        size = min(max(size, style.size_key_min * video_h), style.size_key_max * video_h)
    return size, measurer.width(text, size)


def layout_behind(
    block: Block,
    style: BehindStyle,
    key_measurer: Measurer,
    plain_measurer: Measurer,
    video_w: int,
    video_h: int,
) -> BlockLayout:
    """Изчислява позициите за стил B.

    Ключовата дума се центрира и нарочно излиза извън кадъра. Обикновените
    думи преди нея отиват на ред отгоре (подравнени вляво спрямо нея), тези
    след нея — на ред отдолу вдясно.
    """
    if not block.words:
        return BlockLayout(block, [], block.start, block.end)

    key_index = block.highlight if block.highlight >= 0 else 0
    key_word = block.words[key_index]
    key_text = key_word.text.upper() if style.uppercase_key else key_word.text

    key_size, key_width = _fit_key(key_text, style, key_measurer, video_w, video_h)
    if key_word.scale != 1.0:
        key_size *= key_word.scale
        key_width = key_measurer.width(key_text, key_size)

    plain_size = style.size_plain * video_h
    before = [i for i in range(key_index) if i != key_index]
    after = [i for i in range(key_index + 1, len(block.words))]

    budget = style.max_plain_words
    # При недостиг на място предпочитаме думите непосредствено до ключовата.
    before = before[-budget:]
    after = after[: max(0, budget - len(before))]

    key_x = (video_w - key_width) / 2.0
    indent = 0.10 * key_width
    left_margin = style.margin_x * video_w
    right_margin = video_w - style.margin_x * video_w

    line_h_plain = plain_size * 1.15
    line_h_key = key_size * 1.02

    total = line_h_key + (line_h_plain if before else 0.0) + (line_h_plain if after else 0.0)
    band_top = style.top * video_h
    band_bottom = style.bottom * video_h
    y = band_top
    if y + total > band_bottom:
        y = max(0.0, band_bottom - total)

    appear = block.start - style.lead_in
    disappear = block.end + style.lead_out
    placed: list[Placed] = []

    def add_plain(indices: list[int], y_pos: float, align: str) -> None:
        if not indices:
            return
        text = " ".join(block.words[i].text for i in indices)
        width = plain_measurer.width(text, plain_size)
        if align == "left":
            x = key_x + indent
        else:
            x = key_x + key_width - indent - width
        x = min(max(x, left_margin), max(left_margin, right_margin - width))

        cursor = x
        for i in indices:
            word = block.words[i]
            word_size = plain_size * word.scale
            word_width = plain_measurer.width(word.text, word_size)
            placed.append(
                Placed(
                    text=word.text,
                    x=cursor,
                    y=y_pos,
                    size=word_size,
                    kind="normal",
                    start=word.start,
                    end=word.end,
                    visible_from=appear,
                    hidden_after=disappear,
                    accent=False,
                    color=word.color,
                    animation=word.animation,
                    width=word_width,
                    height=word_size,
                )
            )
            cursor += word_width + plain_measurer.width(" ", word_size)

    if before:
        add_plain(before, y, "left")
        y += line_h_plain

    placed.append(
        Placed(
            text=key_text,
            x=key_x,
            y=y,
            size=key_size,
            kind="highlight",
            start=key_word.start,
            end=key_word.end,
            visible_from=appear,
            hidden_after=disappear,
            accent=True,
            color=key_word.color,
            animation=key_word.animation,
            width=key_width,
            height=key_size,
        )
    )
    y += line_h_key

    if after:
        add_plain(after, y, "right")

    return BlockLayout(block, placed, appear, disappear)


# --------------------------------------------------------------------------
# Общо: блоковете не бива да се застъпват
# --------------------------------------------------------------------------


def deoverlap(layouts: list[BlockLayout]) -> list[BlockLayout]:
    """Подрязва изчезването на блока, ако следващият вече се е появил."""
    for current, following in zip(layouts, layouts[1:]):
        if current.disappear > following.appear:
            middle = (current.block.end + following.block.start) / 2.0
            boundary = min(max(middle, current.block.end), following.block.start)
            current.disappear = boundary
            following.appear = boundary
            for word in current.placed:
                word.hidden_after = min(word.hidden_after, boundary)
            for word in following.placed:
                word.visible_from = max(word.visible_from, boundary)
    if layouts:
        first = layouts[0]
        if first.appear < 0:
            first.appear = 0.0
            for word in first.placed:
                word.visible_from = max(word.visible_from, 0.0)
    return layouts
