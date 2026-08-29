"""Тестове за командния ред.

Проверява се най-вече кое кога се изисква: ``styles`` трябва да работи и
преди ffmpeg да е инсталиран, а ``render`` — не.
"""

from __future__ import annotations

import pytest

from subs import cli
from subs.burn import ToolsMissing


@pytest.fixture
def no_ffmpeg(monkeypatch):
    def missing() -> None:
        raise ToolsMissing("липсва ffmpeg и ffprobe в PATH.")

    monkeypatch.setattr(cli, "check_tools", missing)


def test_styles_works_without_ffmpeg(no_ffmpeg, capsys):
    assert cli.main(["styles"]) == 0
    printed = capsys.readouterr().out
    assert "stack" in printed and "behind" in printed


def test_styles_json_works_without_ffmpeg(no_ffmpeg, capsys):
    assert cli.main(["styles", "--json"]) == 0
    assert '"renderer"' in capsys.readouterr().out


def test_render_still_demands_ffmpeg(no_ffmpeg, capsys, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"")
    assert cli.main(["render", str(video)]) == 1
    assert "ffmpeg" in capsys.readouterr().err


def test_transcribe_still_demands_ffmpeg(no_ffmpeg, capsys, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"")
    assert cli.main(["transcribe", str(video)]) == 1
    assert "ffmpeg" in capsys.readouterr().err


def test_missing_ffmpeg_message_tells_you_how_to_install_it(monkeypatch):
    """Съобщението идва от истинския ``check_tools``, не от двойника —
    иначе тестът щеше да проверява само себе си."""
    import subs.burn as burn

    monkeypatch.setattr(burn.shutil, "which", lambda name: None)
    with pytest.raises(ToolsMissing) as caught:
        burn.check_tools()
    message = str(caught.value)
    assert "ffmpeg" in message and "ffprobe" in message
    assert "winget" in message and "brew" in message and "apt" in message


def test_only_one_tool_missing_is_still_reported(monkeypatch):
    import subs.burn as burn

    monkeypatch.setattr(burn.shutil, "which",
                        lambda name: None if name == "ffprobe" else "/usr/bin/ffmpeg")
    with pytest.raises(ToolsMissing, match="ffprobe"):
        burn.check_tools()


def test_render_without_a_video_argument_is_a_usage_error():
    with pytest.raises(SystemExit):
        cli.main(["render"])
