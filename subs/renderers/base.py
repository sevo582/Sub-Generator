"""Общият интерфейс на рендерерите.

Двата стила се разминават напълно при рисуването — единият вгражда субтитри,
другият растеризира кадри и изнася слой с алфа. Затова изборът на стил
избира клас, а не клон в ``if``. Трети стил = трети клас плюс запис в
``REGISTRY``; конвейерът остава непокътнат.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ClassVar

from ..burn import MediaInfo
from ..models import Block, BlockLayout
from ..styles import Style


@dataclass
class RenderRequest:
    """Всичко, което рендерерът получава, за да свърши работа."""

    source: Path
    blocks: list[Block]
    style: Style
    media: MediaInfo
    #: Готово видео с текст върху кадъра. None = не се иска.
    output: Path | None = None
    #: Слой с прозрачност. None = не се иска.
    layer: Path | None = None
    layer_format: str = "prores"
    crf: int = 18
    preset: str = "veryfast"
    #: Моменти (в секунди), за които се иска само по един кадър вместо
    #: цяло видео. Итерирането по вида става за секунди, а не за минути.
    preview_times: list[float] = field(default_factory=list)
    preview_dir: Path | None = None
    #: (начало, времетраене) за рендиране само на парче — бързият преглед
    #: в прозореца. None = цялото видео.
    segment: tuple[float, float] | None = None
    #: Височина, до която да се смали изходът. None = както е входът.
    scale_height: int | None = None
    #: Кадрова честота на изхода. None = както е входът.
    fps: float | None = None
    #: Папка, в която да останат междинните файлове (.ass и подобни).
    keep_dir: Path | None = None
    dry_run: bool = False
    progress: Callable[[str], None] = print


@dataclass
class RenderResult:
    """Какво е произведено — CLI-то го показва на потребителя."""

    outputs: list[Path] = field(default_factory=list)
    layouts: list[BlockLayout] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Renderer(ABC):
    """База за всички рендерери."""

    name: ClassVar[str]
    #: Може ли да изнесе отделен слой с алфа канал.
    supports_layer: ClassVar[bool] = False

    @abstractmethod
    def layout(self, request: RenderRequest) -> list[BlockLayout]:
        """Превръща блоковете в позиции за конкретния стил."""

    @abstractmethod
    def render(self, request: RenderRequest, layouts: list[BlockLayout]) -> RenderResult:
        """Произвежда исканите изходи."""

    @abstractmethod
    def preview(self, request: RenderRequest, layouts: list[BlockLayout]) -> RenderResult:
        """Рисува по един PNG за всеки момент от ``request.preview_times``.

        Резултатът трябва да е същият, който би се получил на този кадър от
        пълното рендиране — иначе прегледът не струва нищо.
        """

    def run(self, request: RenderRequest) -> RenderResult:
        layouts = self.layout(request)
        if request.preview_times:
            return self.preview(request, layouts)
        if request.layer is not None and not self.supports_layer:
            raise ValueError(
                f"стил {request.style.name!r} не може да изнесе слой с прозрачност; "
                "слой се поддържа само от растерните стилове (напр. behind)"
            )
        return self.render(request, layouts)


def preview_path(request: "RenderRequest", time: float) -> Path:
    """``reel.mp4`` в 2.4 s -> ``reel.stack.2.40s.png``."""
    directory = request.preview_dir or request.source.parent
    stem = f"{request.source.stem}.{request.style.name}.{time:.2f}s.png"
    return directory / stem


REGISTRY: dict[str, type[Renderer]] = {}


def register(cls: type[Renderer]) -> type[Renderer]:
    REGISTRY[cls.name] = cls
    return cls


def get_renderer(name: str) -> Renderer:
    if name not in REGISTRY:
        available = ", ".join(sorted(REGISTRY))
        raise ValueError(f"няма рендерер {name!r}; налични: {available}")
    return REGISTRY[name]()
