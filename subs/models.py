"""Данните, които текат през конвейера.

Три нива:

``Word``
    Резултатът от транскрипцията — текст и тайминг. Това е и форматът на
    междинния JSON, който се редактира на ръка.
``Block``
    Група думи, които стоят заедно на екрана, с посочена подчертана дума.
``Placed``
    Дума с изчислена позиция и размер, готова за рендиране.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

Kind = Literal["normal", "highlight"]

#: Анимации, които може да получи отделна дума. Всяка е реализирана и в
#: двата рендерера — иначе стилът щеше да значи различно нещо според това
#: как се рендира.
ANIMATIONS: tuple[str, ...] = ("няма", "изскачане", "издигане", "избледняване")


@dataclass
class Word:
    """Една дума с тайминг.

    ``emphasis`` и ``accent`` се четат от JSON-а и позволяват ръчно
    надделяване над евристиката: ``emphasis`` налага думата да е
    подчертаната в блока си, ``accent`` ѝ дава акцентния цвят.
    """

    text: str
    start: float
    end: float
    emphasis: bool = False
    accent: bool = False
    #: Собствен цвят ``#RRGGBB``; None означава цвета от стила.
    color: str | None = None
    #: Име от ``ANIMATIONS``. Непознато име се приема като „няма".
    animation: str = "няма"
    #: Множител върху размера, който стилът дава на думата. 1.0 = както е
    #: по стил; 1.5 я прави един и половина пъти по-едра.
    scale: float = 1.0

    def __post_init__(self) -> None:
        if self.end < self.start:
            self.end = self.start
        # Нула или отрицателен размер значи невидима дума; по-разумно е да
        # се върне към нормалния, отколкото да изчезне без обяснение.
        if not self.scale or self.scale <= 0:
            self.scale = 1.0

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "text": self.text,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
        }
        if self.emphasis:
            data["emphasis"] = True
        if self.accent:
            data["accent"] = True
        if self.color:
            data["color"] = self.color
        if self.animation and self.animation != "няма":
            data["animation"] = self.animation
        if abs(self.scale - 1.0) > 1e-6:
            data["scale"] = round(self.scale, 3)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Word":
        return cls(
            text=str(data["text"]),
            start=float(data["start"]),
            end=float(data["end"]),
            emphasis=bool(data.get("emphasis", False)),
            accent=bool(data.get("accent", False)),
            color=data.get("color") or None,
            animation=str(data.get("animation") or "няма"),
            scale=float(data.get("scale") or 1.0),
        )


@dataclass
class Transcript:
    """Пълният резултат от транскрипцията плюс метаданни за диагностика."""

    words: list[Word]
    language: str = "unknown"
    aligned: bool = False
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "aligned": self.aligned,
            "notes": self.notes,
            "words": [w.to_json() for w in self.words],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Transcript":
        if isinstance(data, list):  # позволяваме и гол списък от думи
            return cls(words=[Word.from_json(w) for w in data])
        return cls(
            words=[Word.from_json(w) for w in data.get("words", [])],
            language=str(data.get("language", "unknown")),
            aligned=bool(data.get("aligned", False)),
            notes=list(data.get("notes", [])),
        )


@dataclass
class Block:
    """Група думи, показвани заедно.

    ``highlight`` е индекс в ``words``; -1 означава „няма подчертана дума"
    (случва се само при празен блок).
    """

    words: list[Word]
    highlight: int = -1

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def kind_of(self, index: int) -> Kind:
        return "highlight" if index == self.highlight else "normal"


@dataclass
class Placed:
    """Дума с готова позиция в пиксели.

    ``x``/``y`` са горният ляв ъгъл на реда (котва ``\\an7`` в ASS —
    същата конвенция се ползва и от растерния рендерер, за да няма два
    различни координатни модела в проекта).

    ``visible_from`` е моментът, в който думата се появява избледняла,
    ``start`` — моментът, в който става плътна.
    """

    text: str
    x: float
    y: float
    size: float
    kind: Kind
    start: float
    end: float
    visible_from: float
    hidden_after: float
    accent: bool = False
    #: Собствен цвят на думата; None = цветът от стила.
    color: str | None = None
    animation: str = "няма"
    width: float = 0.0
    height: float = 0.0


@dataclass
class BlockLayout:
    """Разположението на един блок — това връща ``layout`` към рендерера."""

    block: Block
    placed: list[Placed]
    appear: float
    disappear: float

    def __iter__(self) -> Iterable[Placed]:
        return iter(self.placed)
