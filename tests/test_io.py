"""Четене и писане на междинните файлове.

Тези файлове се редактират на ръка, при това предимно на Windows. Затова
тук се проверява точно това, което се чупи там: BOM от Notepad, кирилица и
турски диакритики, и вариантите на формата.
"""

from __future__ import annotations

import json

import pytest

from subs.models import Transcript, Word
from subs.pipeline import load_words, save_words
from subs.styles import get_style, load_style_file

CYRILLIC = {"words": [{"text": "Тази", "start": 0.25, "end": 0.6},
                      {"text": "програма", "start": 0.67, "end": 1.12}],
            "language": "bg"}


def write(path, data, bom: bool = False) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    path.write_bytes((("﻿" if bom else "") + text).encode("utf-8"))


def test_plain_utf8_is_read(tmp_path):
    path = tmp_path / "w.json"
    write(path, CYRILLIC)
    assert [w.text for w in load_words(path).words] == ["Тази", "програма"]


def test_utf8_with_bom_is_read(tmp_path):
    """Notepad на Windows записва точно това."""
    path = tmp_path / "w.json"
    write(path, CYRILLIC, bom=True)
    assert [w.text for w in load_words(path).words] == ["Тази", "програма"]


def test_bare_list_of_words_is_accepted(tmp_path):
    path = tmp_path / "w.json"
    write(path, CYRILLIC["words"])
    assert len(load_words(path).words) == 2


def test_manual_flags_are_read(tmp_path):
    path = tmp_path / "w.json"
    data = {"words": [{"text": "PNG", "start": 0, "end": 1,
                       "emphasis": True, "accent": True}]}
    write(path, data)
    word = load_words(path).words[0]
    assert word.emphasis and word.accent


def test_roundtrip_keeps_text_and_flags(tmp_path):
    path = tmp_path / "w.json"
    original = Transcript(
        words=[Word("Тази", 0.25, 0.6), Word("ığşöü", 0.7, 1.2, emphasis=True)],
        language="bg")
    save_words(original, path)
    restored = load_words(path)
    assert [w.text for w in restored.words] == ["Тази", "ığşöü"]
    assert restored.words[1].emphasis
    assert restored.language == "bg"


def test_saved_file_is_readable_by_a_human(tmp_path):
    """Файлът е за ръчна поправка — кирилицата не бива да е \\u04xx."""
    path = tmp_path / "w.json"
    save_words(Transcript(words=[Word("Тази", 0, 1)], language="bg"), path)
    text = path.read_text(encoding="utf-8")
    assert "Тази" in text and "\\u" not in text


def test_reversed_timings_are_repaired_not_crashed(tmp_path):
    path = tmp_path / "w.json"
    write(path, {"words": [{"text": "тест", "start": 2.0, "end": 1.0}]})
    word = load_words(path).words[0]
    assert word.end >= word.start


def test_style_file_with_bom_is_read(tmp_path):
    path = tmp_path / "s.json"
    write(path, {"extends": "behind", "name": "мой",
                 "behind": {"key_color": "#7ADCF5"}}, bom=True)
    style = load_style_file(str(path))
    assert style.behind.key_color == "#7ADCF5"
    assert style.renderer == "raster_behind", "наследява рендерера на пресета"


def test_style_file_without_extends_is_rejected(tmp_path):
    path = tmp_path / "s.json"
    write(path, {"name": "мой"})
    with pytest.raises(ValueError, match="extends"):
        load_style_file(str(path))


def test_style_file_with_unknown_field_names_it(tmp_path):
    path = tmp_path / "s.json"
    write(path, {"extends": "stack", "stack": {"няма_такова": 1}})
    with pytest.raises(ValueError, match="няма_такова"):
        load_style_file(str(path))


def test_overrides_do_not_touch_the_preset(tmp_path):
    before = get_style("stack").stack.size_normal
    get_style("stack", {"stack": {"size_normal": 0.09}})
    assert get_style("stack").stack.size_normal == before


# --------------------------------------------------------------------------
# Кодировки, писани от Windows
# --------------------------------------------------------------------------


def test_windows_codepage_is_read_with_a_warning(tmp_path, monkeypatch):
    """``Set-Content`` в Windows PowerShell 5.1 записва в таблицата на
    системата, не в UTF-8. На български Windows това е cp1251."""
    import subs.pipeline as pipeline

    monkeypatch.setattr(pipeline.locale, "getpreferredencoding", lambda x=True: "cp1251")
    path = tmp_path / "w.json"
    path.write_bytes(json.dumps(CYRILLIC, ensure_ascii=False).encode("cp1251"))

    result = load_words(path)
    assert [w.text for w in result.words] == ["Тази", "програма"]
    assert any("cp1251" in note for note in result.notes)
    assert any("Set-Content" in note for note in result.notes)


def test_undecodable_file_says_how_to_fix_it(tmp_path, monkeypatch):
    import subs.pipeline as pipeline

    monkeypatch.setattr(pipeline.locale, "getpreferredencoding", lambda x=True: "ascii")
    path = tmp_path / "w.json"
    path.write_bytes(json.dumps(CYRILLIC, ensure_ascii=False).encode("cp1251"))

    with pytest.raises(ValueError, match="UTF8"):
        load_words(path)


def test_plain_utf8_produces_no_encoding_warning(tmp_path):
    path = tmp_path / "w.json"
    write(path, CYRILLIC)
    assert load_words(path).notes == []


def test_broken_json_names_the_file_and_the_position(tmp_path):
    path = tmp_path / "w.json"
    path.write_text('{"words": [{"text": "тест",}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="не е валиден JSON"):
        load_words(path)


def test_style_file_in_windows_codepage_is_read(tmp_path, monkeypatch):
    import subs.pipeline as pipeline

    monkeypatch.setattr(pipeline.locale, "getpreferredencoding", lambda x=True: "cp1251")
    path = tmp_path / "s.json"
    path.write_bytes(json.dumps(
        {"extends": "stack", "name": "мой стил"}, ensure_ascii=False).encode("cp1251"))
    assert load_style_file(str(path)).name == "мой стил"


def test_colour_and_animation_are_written_and_read(tmp_path):
    from subs.models import Transcript as T

    path = tmp_path / "w.json"
    save_words(T(words=[Word("дума", 0, 1, color="#FF3B30", animation="издигане")],
                 language="bg"), path)
    word = load_words(path).words[0]
    assert word.color == "#FF3B30" and word.animation == "издигане"


def test_defaults_are_not_written_out(tmp_path):
    """Файлът се чете на ръка — няма смисъл да е пълен с „animation: няма"."""
    path = tmp_path / "w.json"
    save_words(Transcript(words=[Word("дума", 0, 1)], language="bg"), path)
    text = path.read_text(encoding="utf-8")
    assert "animation" not in text and "color" not in text


def test_unknown_animation_falls_back_to_none(tmp_path):
    path = tmp_path / "w.json"
    write(path, {"words": [{"text": "а", "start": 0, "end": 1, "animation": ""}]})
    assert load_words(path).words[0].animation == "няма"


def test_offset_is_written_and_read(tmp_path):
    path = tmp_path / "w.json"
    save_words(Transcript(words=[Word("дума", 0, 1, dx=0.04, dy=-0.01)],
                          language="bg"), path)
    assert "offset" in path.read_text(encoding="utf-8")
    word = load_words(path).words[0]
    assert word.dx == pytest.approx(0.04) and word.dy == pytest.approx(-0.01)


def test_zero_offset_is_not_written(tmp_path):
    path = tmp_path / "w.json"
    save_words(Transcript(words=[Word("дума", 0, 1)], language="bg"), path)
    assert "offset" not in path.read_text(encoding="utf-8")
