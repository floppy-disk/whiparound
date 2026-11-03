from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from PIL import Image

from conftest import T0, make_game
from whiparound.slate import SlateSource, next_kickoff_lines, render_slate

ET = ZoneInfo("America/New_York")
LINES = ["No game on", "Next kickoff Sat 12:00 ET", "Pitt at Syracuse"]


def test_render_slate_size_and_determinism(tmp_path):
    a = render_slate(LINES, tmp_path / "a.png")
    b = render_slate(LINES, tmp_path / "b.png")
    assert a.read_bytes() == b.read_bytes()
    with Image.open(a) as img:
        assert img.size == (1280, 720) and img.format == "PNG"
        assert img.getpixel((5, 5)) == (0x0b, 0x1a, 0x33)
    render_slate(["x"] * 5, tmp_path / "c.png", size=(640, 360))
    with Image.open(tmp_path / "c.png") as img:
        assert img.size == (640, 360)


def pre(event_id, when, short):
    return replace(make_game(event_id, state="pre"), date=when, short_name=short)


def test_next_kickoff_picks_earliest_future_pre_game():
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    sat_noon_et = datetime(2026, 10, 17, 12, 0, tzinfo=ET).astimezone(timezone.utc)
    games = [pre("2", sat_noon_et + timedelta(hours=4), "LSU @ UGA"),
             pre("1", sat_noon_et, "PITT @ SYR"),
             pre("3", now - timedelta(hours=1), "PAST @ GAME"),
             replace(make_game("4", state="in"), date=sat_noon_et - timedelta(hours=1))]
    assert next_kickoff_lines(games, now, ET) == ["No game on", "Next kickoff Sat 12:00 ET", "PITT @ SYR"]
    winter = datetime(2026, 12, 5, 12, 0, tzinfo=ET).astimezone(timezone.utc)
    assert next_kickoff_lines([pre("5", winter, "A @ B")], now, ET)[1] == "Next kickoff Sat 12:00 ET"


def test_next_kickoff_none_scheduled():
    assert next_kickoff_lines([make_game("1", state="post")], T0, ET) == [
        "No game on", "Next kickoff: none scheduled today"]
    assert next_kickoff_lines([], T0, ET)[1] == "Next kickoff: none scheduled today"


def test_slate_command(tmp_path, monkeypatch):
    monkeypatch.setenv("FFMPEG_BIN", "/opt/ffmpeg")
    cmd = SlateSource(tmp_path / "s.png").command()
    assert cmd[0] == "/opt/ffmpeg" and cmd[-3:] == ["-f", "mpegts", "-"] and str(tmp_path / "s.png") in cmd
