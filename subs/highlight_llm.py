"""Избор на подчертана дума през LLM — вариант v2, зад флаг.

Евристиката в ``layout.choose_highlight`` е по подразбиране и е достатъчна
в повечето случаи. Този модул е за блоковете, в които смисълът, а не
дължината, решава коя дума е поантата.

Две неща по устройство:

* **Кеш на диска.** Едно и също изречение се пита веднъж. Без това всяко
  повторно рендиране на същото видео би зависело от мрежата, а точно това
  не искаме.
* **Никога не е фатално.** Липсващ ключ, паднала мрежа, странен отговор —
  всичко връща ``None`` и конвейерът пада обратно на евристиката.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .models import Block

DEFAULT_MODEL = "claude-sonnet-5"

PROMPT = (
    "Ето списък от думи от един кадър субтитри. Точно една от тях е смисловата "
    "дума — марка, число, ключово понятие или поантата на изказването. "
    "Отговори САМО с нейния индекс (цяло число), без обяснение.\n\n"
    "Думи:\n{listing}\n\nИндекс:"
)


class LlmHighlighter:
    """Пита модел коя дума в блока е ключовата.

    Използва се само ако е поискано изрично; иначе изобщо не се създава.
    """

    def __init__(self, model: str = DEFAULT_MODEL, cache_path: str | Path | None = None,
                 api_key: str | None = None) -> None:
        self.model = model
        self.cache_path = Path(cache_path) if cache_path else None
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._cache: dict[str, int] = self._load_cache()
        self._client = None
        self.notes: list[str] = []

    # -- кеш ------------------------------------------------------------

    def _load_cache(self) -> dict[str, int]:
        if self.cache_path and self.cache_path.exists():
            try:
                return json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def save(self) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _key(words: list[str]) -> str:
        return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()[:16]

    # -- заявка ---------------------------------------------------------

    def _ensure_client(self) -> bool:
        if self._client is not None:
            return True
        if not self.api_key:
            self.notes.append(
                "няма ANTHROPIC_API_KEY — подчертаната дума се избира с евристиката")
            return False
        try:
            import anthropic
        except ImportError:
            self.notes.append(
                "липсва пакетът anthropic (pip install anthropic) — "
                "подчертаната дума се избира с евристиката")
            return False
        self._client = anthropic.Anthropic(api_key=self.api_key)
        return True

    def choose(self, block: Block) -> int | None:
        """Връща индекс на ключовата дума или ``None``, ако не е успяло."""
        texts = [word.text for word in block.words]
        if len(texts) < 2:
            return 0 if texts else None

        key = self._key(texts)
        if key in self._cache:
            index = self._cache[key]
            return index if 0 <= index < len(texts) else None

        if not self._ensure_client():
            return None

        listing = "\n".join(f"{i}: {text}" for i, text in enumerate(texts))
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=8,
                messages=[{"role": "user", "content": PROMPT.format(listing=listing)}],
            )
            reply = "".join(part.text for part in response.content
                            if getattr(part, "type", "") == "text")
        except Exception as error:  # noqa: BLE001 — не бива да спира рендирането
            self.notes.append(f"заявката до модела не сработи: {error}")
            return None

        match = re.search(r"-?\d+", reply)
        if not match:
            self.notes.append(f"неразбираем отговор от модела: {reply!r}")
            return None
        index = int(match.group())
        if not 0 <= index < len(texts):
            self.notes.append(f"моделът върна индекс извън блока: {index}")
            return None

        self._cache[key] = index
        return index
