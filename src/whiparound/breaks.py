"""Commercial-break detection: an ffmpeg black/silence detector on the live stream, a pure rule over its events, and
ESPN break signals that arm the detector. Deterministic, no ML. The stream URL is a credential: it is never logged and
ffmpeg's stderr is never logged (only counts)."""
import os
import shutil
import re
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from .espn import Game

RETENTION_S = 15 * 60
BACKOFF_MIN_S, BACKOFF_MAX_S = 5.0, 60.0
STOP_GRACE_S = 5.0
FFMPEG_ARGS = ["-hide_banner", "-nostats", "-loglevel", "info"]
VIDEO_FILTER = "blackdetect=d=0.08:pix_th=0.10"
AUDIO_FILTER = "silencedetect=n=-45dB:d=0.25"

_BLACK_RE = re.compile(r"black_start:\s*(\S+)\s+black_end:\s*(\S+)\s+black_duration:\s*(\S+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(\S+)\s*\|\s*silence_duration:\s*(\S+)")
_INPUT_START_RE = re.compile(r"Duration:.*\bstart:\s*(-?\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class VideoEvent:
    kind: Literal["black", "silence"]
    start_wall: datetime
    end_wall: datetime
    duration_s: float


def parse_ffmpeg_line(line: str) -> tuple[str, float, float] | None:
    """(kind, start_s, end_s) in stream time, or None. `silence_start` lines return None: the matching `silence_end`
    line carries the duration, so the pair is resolved there (start = end - duration)."""
    try:
        if "blackdetect" in line and (m := _BLACK_RE.search(line)):
            return "black", float(m.group(1)), float(m.group(2))
        if "silencedetect" in line and (m := _SILENCE_END_RE.search(line)):
            end = float(m.group(1))
            return "silence", end - float(m.group(2)), end
    except ValueError:
        return None
    return None


def parse_input_start(line: str) -> float | None:
    """The `start:` stream time on ffmpeg's `Duration: N/A, start: 6302.614667, bitrate: N/A` line."""
    m = _INPUT_START_RE.search(line)
    return float(m.group(1)) if m else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _noop_log(event: str, **fields) -> None:
    pass


class VideoBreakDetector:
    def __init__(self, log: Callable[..., None] = _noop_log, *, clock: Callable[[], datetime] = _utcnow,
                 backoff_min_s: float = BACKOFF_MIN_S, backoff_max_s: float = BACKOFF_MAX_S):
        self._log = log
        self._clock = clock
        self._backoff = (backoff_min_s, backoff_max_s)
        self._events: deque[VideoEvent] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._url = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def start(self, url: str) -> None:
        if self._thread is not None:
            raise RuntimeError("detector already started")
        self._url = url
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="video-break-detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            if self._proc is not None:
                self._proc.terminate()
        thread, self._thread = self._thread, None
        if thread is None:
            return
        thread.join(STOP_GRACE_S)
        if thread.is_alive():  # terminate was ignored: kill, then wait for the thread to close its pipes
            with self._lock:
                if self._proc is not None:
                    self._proc.kill()
            thread.join()
        self._url = ""

    def events(self, since: datetime) -> list[VideoEvent]:
        with self._lock:
            return [e for e in self._events if e.end_wall >= since]

    def _add(self, ev: VideoEvent) -> None:
        with self._lock:
            self._events.append(ev)
            cutoff = ev.end_wall - timedelta(seconds=RETENTION_S)
            while self._events and self._events[0].end_wall < cutoff:
                self._events.popleft()

    def _run(self) -> None:
        delay = self._backoff[0]
        while not self._stop.is_set():
            started = self._clock()
            returncode, lines, n_events = self._run_once(started)
            if self._stop.is_set():
                break
            self._log("detector_exit", returncode=returncode, stderr_lines=lines, events=n_events)
            if (self._clock() - started).total_seconds() >= self._backoff[1]:
                delay = self._backoff[0]  # it ran a while: not a crash loop
            self._log("detector_restart", backoff_s=delay)
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self._backoff[1])

    def _run_once(self, started: datetime) -> tuple[int | None, int, int]:
        cmd = [os.environ.get("FFMPEG_BIN", "ffmpeg"), *FFMPEG_ARGS, "-threads", "2", "-i", self._url,
               "-vf", VIDEO_FILTER, "-af", AUDIO_FILTER, "-f", "null", "-"]
        nice = os.environ.get("FFMPEG_NICE", "10")
        if nice and nice != "0" and shutil.which("nice") and shutil.which(cmd[0]):
            cmd = ["nice", "-n", nice, *cmd]  # yield to Emby and Dispatcharr; detection is not latency-critical
        with self._lock:
            if self._stop.is_set():
                return None, 0, 0
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, text=True, errors="replace")
            except OSError as e:
                self._log("detector_error", error=type(e).__name__)  # not str(e): it can carry argv
                return None, 0, 0
            self._proc = proc
        self._log("detector_start", pid=proc.pid)
        t0: float | None = None
        lines = n_events = 0
        try:
            for line in proc.stderr:
                lines += 1
                if t0 is None and (s := parse_input_start(line)) is not None:
                    t0 = s
                    continue
                parsed = parse_ffmpeg_line(line)
                if parsed is None:
                    continue
                kind, start_s, end_s = parsed
                if t0 is None:
                    t0 = start_s
                self._add(VideoEvent(kind, started + timedelta(seconds=start_s - t0),  # type: ignore[arg-type]
                                     started + timedelta(seconds=end_s - t0), end_s - start_s))
                n_events += 1
        finally:
            if self._stop.is_set() and proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stderr.close()
            with self._lock:
                self._proc = None
        return proc.returncode, lines, n_events


# ---- the rule: pure -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BreakParams:
    window_s: float = 75
    min_events: int = 2
    gap_min_s: float = 20
    gap_max_s: float = 40
    long_black_s: float = 1.0
    long_black_window_s: float = 45
    end_quiet_s: float = 60


@dataclass(frozen=True)
class BreakState:
    in_break: bool
    since: datetime | None
    reason: str
    event_count: int


NOT_IN_BREAK = BreakState(False, None, "", 0)


def _trigger(events: list[VideoEvent], at: datetime, p: BreakParams) -> BreakState:
    seen = sorted((e for e in events if e.end_wall <= at), key=lambda e: e.start_wall)
    blacks = [e for e in seen if e.kind == "black" and e.duration_s >= p.long_black_s
              and (at - e.end_wall).total_seconds() <= p.long_black_window_s]
    if blacks:
        return BreakState(True, blacks[0].start_wall, "long_black", len(blacks))
    best: list[VideoEvent] = []
    run: list[VideoEvent] = []
    for e in (e for e in seen if (at - e.start_wall).total_seconds() <= p.window_s):
        # longest chain of events whose consecutive start gaps are ad-boundary spaced (~30 s)
        if run and p.gap_min_s <= (e.start_wall - run[-1].start_wall).total_seconds() <= p.gap_max_s:
            run.append(e)
        else:
            run = [e]
        if len(run) > len(best):
            best = list(run)
    if len(best) >= p.min_events:
        return BreakState(True, best[0].start_wall, "ad_cadence", len(best))
    return NOT_IN_BREAK


def break_state(events: list[VideoEvent], now: datetime, p: BreakParams = BreakParams()) -> BreakState:
    """In a break while a trigger holds; once triggered, stays in it until no event for `end_quiet_s`."""
    state = _trigger(events, now, p)
    if state.in_break:
        return state
    past = [e.end_wall for e in events if e.end_wall <= now]
    if past and (now - max(past)).total_seconds() < p.end_quiet_s:
        return _trigger(events, max(past), p)  # triggered at the last event and not yet quiet long enough
    return NOT_IN_BREAK


# ---- ESPN side: pure ------------------------------------------------------------------------------------------

END_PLAYS = {"End Period", "End of Half", "End of Game"}


def is_break_signal(prev: Game | None, cur: Game, end_plays: frozenset | set = END_PLAYS) -> str | None:
    if prev is not None:
        if (prev.home.score, prev.away.score) != (cur.home.score, cur.away.score):
            return "score"
        if prev.period != cur.period:
            return "period_end"
    if cur.halftime:
        return "halftime"
    if cur.last_play_type and "timeout" in cur.last_play_type.lower():
        return "timeout"
    if cur.last_play_type in end_plays:
        return "end_period"
    return None


class ArmedWindow:
    """ESPN signals arm the video detector's attention for a while; they never declare a break themselves."""

    def __init__(self):
        self._until: datetime | None = None
        self.reason = ""

    def arm(self, now: datetime, reason: str, duration_s: float = 120) -> None:
        self._until = now + timedelta(seconds=duration_s)
        self.reason = reason

    def armed(self, now: datetime) -> bool:
        return self._until is not None and now < self._until
