"""Стиловите пресети като данни.

Всеки пресет е дърво от dataclass-ове, а не разпръснати константи. Добавянето
на нов стил е нов запис в ``PRESETS`` (или JSON файл през ``--style-file``),
без да се пипа логиката.

Размерите, които започват с ``size_``, ``top``, ``gap`` и подобни, са **дроби
от височината на кадъра**, а не пиксели — така един и същ пресет работи и на
1080x1920, и на 720x1280. Хоризонталните дроби (``margin_x``, ``zigzag``,
``max_line_width``) са спрямо ширината.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from typing import Any, Literal

RendererName = Literal["ass_stack", "raster_behind"]


@dataclass(frozen=True)
class BlockRules:
    """Правила за разбиване на изречението на блокове.

    Общи са за двата стила — това е споделената част от конвейера.
    """

    min_words: int = 4
    max_words: int = 8
    #: Пауза в говора (секунди), която сама по себе си затваря блока.
    split_gap: float = 0.45
    #: Пунктуация, която затваря блока, ако вече има поне ``min_words``.
    split_punctuation: str = ".!?…:;"
    #: Максимална продължителност на блок в секунди.
    max_duration: float = 4.0


@dataclass(frozen=True)
class HighlightRules:
    """Как се избира подчертаната дума."""

    #: Думи под тази дължина не стават за подчертани (освен числа и
    #: абревиатури с главни букви).
    min_length: int = 4
    #: Числата винаги бият по тежест — в референциите те са поантата.
    number_bonus: float = 3.0
    #: Бонус за дума, писана с главни букви (марка, абревиатура). Голям е
    #: нарочно: „PNG" е три знака, но е поантата на изречението, а
    #: точкуването по дължина иначе винаги би я подминало.
    caps_bonus: float = 5.0
    #: Тежест на дължината на думата (в знаци).
    length_weight: float = 1.0
    #: Наказание за близост до края на блока (предпочитаме средата).
    edge_penalty: float = 0.6


@dataclass(frozen=True)
class ShadowSpec:
    """Мека тъмна сянка. Радиусът и отместването са дроби от височината."""

    blur: float = 0.006
    dx: float = 0.0
    dy: float = 0.005
    alpha: float = 0.55
    color: str = "#000000"


@dataclass(frozen=True)
class StackStyle:
    """Стил A — „Стълб"."""

    font: str = "Montserrat-ExtraBold.ttf"
    #: Име на фамилията, както го вижда libass (виж tools/build_fonts.py).
    font_family: str = "Montserrat ExtraBold"

    size_normal: float = 0.036
    size_highlight: float = 0.055
    line_gap: float = 1.15

    #: Горният ръб на първия ред, дроб от височината.
    block_top: float = 0.44
    #: Блокът не слиза под този ред; ако не се събира, се вдига нагоре.
    block_max_bottom: float = 0.90
    #: Ляв ръб на „левите" редове. В референция A блокът е осезаемо
    #: отместен навътре, не залепен за ръба на кадъра.
    margin_x: float = 0.19
    #: Хоризонтално отместване на редовете в зиг-заг, дроб от ширината.
    zigzag: float = 0.17
    max_line_width: float = 0.76
    max_words_per_line: int = 2
    #: Дума, по-тясна от този дял от ширината, взима следващата при себе си.
    #: По-широката остава сама на реда — така се държи референция A.
    pair_below: float = 0.22

    #: Непрозрачност на още неизречените думи (0..1).
    dim_alpha: float = 0.45
    shadow: ShadowSpec = field(default_factory=ShadowSpec)

    color: str = "#FFFFFF"
    accent_color: str = "#8FE9F7"
    #: Ако е False, акцентният цвят се дава само на думи с "accent": true.
    accent_all_highlights: bool = False

    lead_in: float = 0.15
    lead_out: float = 0.25
    #: Продължителност на прехода избледняла -> плътна, милисекунди.
    fade_ms: int = 80

    #: None = целият блок стои на екрана от самото начало (референция A).
    #: Цяло число N = показват се само N думи напред, останалите чакат.
    reveal_lookahead: int | None = None

    #: Калибровка: libass понякога рендира с лек отстъп от изчисленията с PIL.
    ass_size_scale: float = 1.0


@dataclass(frozen=True)
class BehindStyle:
    """Стил B — „Зад кадъра"."""

    font_key: str = "Montserrat-BlackItalic.ttf"
    font_plain: str = "Montserrat-ExtraBold.ttf"

    #: Ключовата дума се оразмерява така, че да заеме този дял от ширината
    #: на кадъра. Заедно с плавното уголемяване това е причината думата да
    #: „изтласка" краищата си извън кадъра — точно ефектът от референция B.
    #: ``None`` връща фиксирания размер ``size_key``.
    key_fill: float | None = 0.98
    #: Граници на размера, за да не стане къса дума абсурдно едра, нито
    #: дълга — нечетима. Дроби от височината.
    size_key_min: float = 0.075
    size_key_max: float = 0.20
    #: Размерът, когато ``key_fill`` е изключено.
    size_key: float = 0.095
    size_plain: float = 0.030

    #: Вертикалният пояс, в който живее текстът (дроби от височината).
    top: float = 0.09
    bottom: float = 0.33
    margin_x: float = 0.06

    uppercase_key: bool = True
    #: Синтетичен курсив: наклон на буквите. 0 = без наклон.
    skew: float = 0.20

    key_color: str = "#3DFF4B"
    #: Непрозрачност на заливката на ключовата дума — фонът прозира.
    key_alpha: float = 0.88
    plain_color: str = "#FFFFFF"
    plain_alpha: float = 1.0

    #: Плавно уголемяване през целия живот на думата (ease-out).
    scale_start: float = 1.0
    scale_end: float = 1.13

    #: В референцията ключовата дума няма забележима сянка — тя се чете
    #: заради размера си, а сянката само мърси полупрозрачната заливка.
    shadow_key: ShadowSpec = field(
        default_factory=lambda: ShadowSpec(blur=0.004, dy=0.003, alpha=0.0)
    )
    shadow_plain: ShadowSpec = field(
        default_factory=lambda: ShadowSpec(blur=0.005, dy=0.004, alpha=0.50)
    )

    lead_in: float = 0.10
    lead_out: float = 0.20

    #: Максимален брой обикновени думи около ключовата.
    max_plain_words: int = 3


@dataclass(frozen=True)
class Style:
    """Пълен пресет."""

    name: str
    description: str
    renderer: RendererName
    blocks: BlockRules = field(default_factory=BlockRules)
    highlight: HighlightRules = field(default_factory=HighlightRules)
    stack: StackStyle = field(default_factory=StackStyle)
    behind: BehindStyle = field(default_factory=BehindStyle)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


_STACK = Style(
    name="stack",
    description="Стълб от думи в зиг-заг, изгражда се докато човекът говори (референция A)",
    renderer="ass_stack",
)

#: Същият стил A, но с размерите, измерениот кадрите на референция A.
#: Заданието задава 0.036/0.055; премерено от самата референция излиза
#: по-дребна нормална дума и по-рязък контраст с подчертаната (около 1:1.8
#: вместо 1:1.5). Държим двата варианта един до друг, за да се избере на око.
_STACK_REF = Style(
    name="stack-ref",
    description="Като stack, но с размерите, премерени от кадрите на референция A",
    renderer="ass_stack",
    stack=StackStyle(size_normal=0.030, size_highlight=0.054, dim_alpha=0.40),
)

_BEHIND = Style(
    name="behind",
    description="Огромна полупрозрачна ключова дума в горната трета, за слагане зад човека (референция B)",
    renderer="raster_behind",
    blocks=BlockRules(min_words=1, max_words=4, split_gap=0.35, max_duration=2.6),
)

PRESETS: dict[str, Style] = {s.name: s for s in (_STACK, _STACK_REF, _BEHIND)}


def _merge(base: Any, patch: dict[str, Any]) -> Any:
    """Прилага речник върху dataclass, рекурсивно за вложените."""
    if not is_dataclass(base):
        return patch
    known = {f.name: f for f in fields(base)}
    values: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in known:
            raise ValueError(f"непознато поле в стила: {key!r}")
        current = getattr(base, key)
        if is_dataclass(current) and isinstance(value, dict):
            values[key] = _merge(current, value)
        else:
            values[key] = value
    return replace(base, **values) if values else base


def apply_overrides(style: Style, overrides: dict[str, Any] | None) -> Style:
    """Прилага речник с надделявания върху готов стил."""
    return _merge(style, overrides) if overrides else style


def get_style(name: str, overrides: dict[str, Any] | None = None) -> Style:
    """Връща пресет по име, по желание с приложени надделявания."""
    if name not in PRESETS:
        available = ", ".join(sorted(PRESETS))
        raise ValueError(f"няма стил {name!r}; налични: {available}")
    style = PRESETS[name]
    if overrides:
        style = _merge(style, overrides)
    return style


def load_style_file(path: str) -> Style:
    """Чете стил от JSON.

    Файлът може да наследява пресет чрез ключа ``"extends"``::

        {"extends": "stack", "name": "stack-cyan",
         "stack": {"accent_all_highlights": true}}
    """
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    parent = data.pop("extends", None)
    if parent is None:
        raise ValueError("стиловият файл трябва да съдържа \"extends\" с име на пресет")
    return get_style(parent, data)
