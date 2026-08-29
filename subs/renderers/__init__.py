"""Рендерери. Вносът тук ги регистрира в ``REGISTRY``."""

from __future__ import annotations

from .base import REGISTRY, RenderRequest, RenderResult, Renderer, get_renderer, register
from .ass_stack import AssStackRenderer
from .raster_behind import RasterBehindRenderer

__all__ = [
    "REGISTRY", "RenderRequest", "RenderResult", "Renderer",
    "get_renderer", "register", "AssStackRenderer", "RasterBehindRenderer",
]
