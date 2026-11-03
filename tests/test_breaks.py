"""Break detection: ffmpeg line parsing, the pure rule on a real measurement, ESPN signals, and the detector against a
fake ffmpeg (a real subprocess with real pipes; a leaked pipe or thread fails the suite)."""
import dataclasses
import os
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from whiparound.breaks import (ArmedWindow, BreakParams, VideoBreakDetector, VideoEvent, break_state,
                                is_break_signal, parse_ffmpeg_line, parse_input_start)
from conftest import make_game

BLACK = "[Parsed_blackdetect_0 @ 0x6000012a4000] black_start:189.736333 black_end:192.722333 black_duration:2.986"
SIL_START = "[Parsed_silencedetect_0 @ 0x6000012a8000] silence_start: 117.53175"
SIL_END = "[Parsed_silencedetect_0 @ 0x6000012a8000] silence_end: 117.814479 | silence_duration: 0.282729"
INPUT = "  Duration: N/A, start: 6302.614667, bitrate: N/A"


def test_parse_lines():
    assert parse_ffmpeg_line(BLACK) == ("black", 189.736333, 192.722333)
    assert parse_ffmpeg_line(SIL_START) is None  # the pair resolves on the end line
    kind, start, end = parse_ffmpeg_line(SIL_END)
    assert (kind, end) == ("silence", 117.814479) and abs(start - 117.53175) < 1e-6
    assert parse_ffmpeg_line("frame=  1 fps=0.0 q=-0.0") is None
    assert parse_ffmpeg_line("black_start:oops black_end:1 black_duration:1") is None
    assert parse_input_start(INPUT) == 6302.614667
    assert parse_input_start(BLACK) is None


T0 = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc)


def ev(kind, t, dur):
    return VideoEvent(kind, T0 + timedelta(seconds=t), T0 + timedelta(seconds=t + dur), dur)


MEASURED = [ev("silence", 117.5, 0.28), ev("black", 189.7, 3.0), ev("silence", 207.5, 0.3), ev("silence", 237.3, 0.3),
            ev("silence", 273.9, 0.3), ev("black", 285.6, 0.17)]


def state_at(t):
    return break_state(MEASURED, T0 + timedelta(seconds=t))


def test_break_state_on_measured_sequence():
    assert not state_at(150).in_break  # a lone silence
    s = state_at(193)
    assert s.in_break and s.reason == "long_black" and s.since == T0 + timedelta(seconds=189.7)
    s = state_at(240)  # black is >45 s old; 207.5 -> 237.3 is a 30-s pair
    assert s.in_break and s.reason == "ad_cadence" and s.event_count == 2
    assert state_at(310).in_break  # held: last event 285.8 is not yet quiet 60 s
    assert not state_at(350).in_break  # quiet
    assert not state_at(350).since and state_at(350).event_count == 0


def test_break_state_params_and_ignored_future_events():
    assert not break_state([], T0).in_break
    two = [ev("silence", 0, 0.3), ev("silence", 30, 0.3)]
    assert break_state(two, T0 + timedelta(seconds=31)).in_break
    assert not break_state(two, T0 + timedelta(seconds=15)).in_break  # the second has not happened yet
    tight = [ev("silence", 0, 0.3), ev("silence", 10, 0.3)]
    assert not break_state(tight, T0 + timedelta(seconds=11)).in_break  # 10 s gap: not ad cadence
    assert break_state(tight, T0 + timedelta(seconds=11), dataclasses.replace(BreakParams(), gap_min_s=5)).in_break


def test_is_break_signal():
    base = make_game()
    assert is_break_signal(None, base) is None
    assert is_break_signal(base, base) is None
    scored = make_game(home_score=28)
    assert is_break_signal(base, scored) == "score"
    assert is_break_signal(make_game(period=1), make_game(period=2)) == "period_end"
    assert is_break_signal(None, make_game(status_type_id="23")) == "halftime"
    for play, reason in [("Timeout", "timeout"), ("Official Timeout", "timeout"), ("End Period", "end_period"),
                         ("End of Half", "end_period"), ("End of Game", "end_period"), ("Rush", None)]:
        assert is_break_signal(base, dataclasses.replace(base, last_play_type=play)) == reason


def test_armed_window_expiry():
    w = ArmedWindow()
    assert not w.armed(T0)
    w.arm(T0, "timeout")
    assert w.reason == "timeout" and w.armed(T0 + timedelta(seconds=119))
    assert not w.armed(T0 + timedelta(seconds=120))
    w.arm(T0 + timedelta(seconds=100), "score", duration_s=10)
    assert w.armed(T0 + timedelta(seconds=109)) and not w.armed(T0 + timedelta(seconds=110))


def fake_ffmpeg(tmp_path, body):
    path = tmp_path / "fake-ffmpeg"
    path.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


EMIT = f"""
for line in ({INPUT.replace('6302.614667', '100.0')!r}, {BLACK.replace('189.736333', '101.0').replace('192.722333', '104.0')!r}, {SIL_START.replace('117.53175', '110.0')!r}, {SIL_END.replace('117.814479', '110.3').replace('0.282729', '0.3')!r}):
    print(line, file=sys.stderr, flush=True)
    time.sleep(0.05)
"""


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_detector_events_restart_and_clean_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("FFMPEG_BIN", fake_ffmpeg(tmp_path, EMIT + "sys.exit(0)"))
    logs = []
    before = datetime.now(timezone.utc)
    det = VideoBreakDetector(lambda event, **f: logs.append((event, f)), backoff_min_s=0.05, backoff_max_s=0.1)
    det.start("http://user:secret@example.invalid/stream.ts")
    assert wait_for(lambda: len(det.events(before)) >= 2 and any(e == "detector_restart" for e, _ in logs))
    first = det.events(before)[:2]
    assert [e.kind for e in first] == ["black", "silence"]
    black, silence = first
    # stream t0 = 100.0: black at +1.0 s for 3.0 s, silence 110.0 - 110.3 -> +10.0 s
    assert black.duration_s == 3.0 and (black.end_wall - black.start_wall).total_seconds() == 3.0
    assert 0.9 < (black.start_wall - before).total_seconds() < 3.0
    assert abs((silence.start_wall - black.start_wall).total_seconds() - 9.0) < 1e-6
    assert det.events(datetime.now(timezone.utc) + timedelta(hours=1)) == []
    assert det.running
    det.stop()
    assert not det.running
    assert not [t for t in threading.enumerate() if t.name == "video-break-detector"]
    names = [e for e, _ in logs]
    assert "detector_start" in names and any(e == "detector_exit" and f["returncode"] == 0 for e, f in logs)
    assert not any("secret" in repr(f) or "example.invalid" in repr(f) for _, f in logs)  # URL never logged


def test_stop_kills_a_running_ffmpeg_promptly(tmp_path, monkeypatch):
    monkeypatch.setenv("FFMPEG_BIN", fake_ffmpeg(tmp_path, "print('x', file=sys.stderr, flush=True)\ntime.sleep(60)"))
    logs = []
    det = VideoBreakDetector(lambda event, **f: logs.append(event))
    det.start("http://example.invalid/s")
    assert wait_for(lambda: "detector_start" in logs)
    t = time.monotonic()
    det.stop()
    assert time.monotonic() - t < 4 and not det.running
    assert "detector_restart" not in logs  # a stop is not an exit to recover from


def test_missing_binary_backs_off_without_leaking(monkeypatch):
    monkeypatch.setenv("FFMPEG_BIN", "/nonexistent/ffmpeg")
    logs = []
    det = VideoBreakDetector(lambda event, **f: logs.append((event, f)), backoff_min_s=0.02, backoff_max_s=0.05)
    det.start("http://example.invalid/s")
    assert wait_for(lambda: sum(e == "detector_error" for e, _ in logs) >= 2)
    det.stop()
    assert ("detector_error", {"error": "FileNotFoundError"}) in logs
