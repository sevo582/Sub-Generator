"""Сглобява шрифтовете в ``subs/assets/fonts`` от пакетите на Fontsource.

Fontsource публикува Montserrat нарязан по подмножества (latin, latin-ext,
cyrillic, cyrillic-ext), защото за уеб това е по-икономично. За нас това не
върши работа — един и същи блок може да съдържа и кирилица, и турски
диакритики. Затова подмножествата се сливат обратно в един TTF на тегло.

Освен това всеки резултат получава уникално име на фамилия
(``Montserrat ExtraBold`` вместо ``Montserrat`` + стил ``ExtraBold``).
libass избира шрифт през fontconfig по име на фамилия; ако три файла се
представят като една фамилия, изборът на тегло става лотария.

Употреба::

    npm pack @fontsource/montserrat
    tar xzf fontsource-montserrat-*.tgz
    python tools/build_fonts.py package

Изисква ``fonttools[woff]`` (fonttools + brotli).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

from fontTools.merge import Merger
from fontTools.ttLib import TTFont

SUBSETS = ("latin", "latin-ext", "cyrillic", "cyrillic-ext")

# (вариант във Fontsource, име на изходния файл, име на фамилията)
VARIANTS = (
    ("800-normal", "Montserrat-ExtraBold.ttf", "Montserrat ExtraBold"),
    ("900-italic", "Montserrat-BlackItalic.ttf", "Montserrat Black Italic"),
    ("700-normal", "Montserrat-Bold.ttf", "Montserrat Bold"),
)

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "subs", "assets", "fonts")


def _set_family(font: TTFont, family: str) -> None:
    """Прави ``family`` единственото име на фамилия, а стилът — Regular."""
    postscript = family.replace(" ", "")
    values = {1: family, 2: "Regular", 4: family, 6: postscript, 16: family, 17: "Regular"}
    name = font["name"]
    for record in list(name.names):
        if record.nameID in values:
            name.setName(values[record.nameID], record.nameID,
                         record.platformID, record.platEncID, record.langID)


def build(src_dir: str, variant: str, out_name: str, family: str) -> None:
    files_dir = os.path.join(src_dir, "files")
    tmp = tempfile.mkdtemp(prefix="subsfont-")
    try:
        parts: list[str] = []
        for subset in SUBSETS:
            src = os.path.join(files_dir, f"montserrat-{subset}-{variant}.woff2")
            if not os.path.exists(src):
                print(f"  пропуснато (липсва): {src}")
                continue
            font = TTFont(src)
            font.flavor = None  # woff2 -> ttf
            part = os.path.join(tmp, f"{subset}.ttf")
            font.save(part)
            parts.append(part)
        if not parts:
            raise SystemExit(f"няма подмножества за вариант {variant}")

        merged = Merger().merge(parts)
        _set_family(merged, family)
        os.makedirs(OUT_DIR, exist_ok=True)
        out_path = os.path.join(OUT_DIR, out_name)
        merged.save(out_path)

        cmap = merged.getBestCmap()
        missing = [c for c in "AЯщöğşıçü" if ord(c) not in cmap]
        status = "OK" if not missing else f"ЛИПСВАТ: {''.join(missing)}"
        print(f"{out_name}: {len(cmap)} знака, фамилия {family!r} — {status}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    src = sys.argv[1]
    for variant, out_name, family in VARIANTS:
        build(src, variant, out_name, family)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
