"""Off-hours still card: a rendered PNG, and an ffmpeg process that turns it into a live MPEG-TS stream."""
import os
import subprocess
from datetime import datetime, tzinfo
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .espn import Game

BACKGROUND = "#0b1a33"
TITLE = "NCAAF CHANGER"
TITLE_COLOR = "#ffffff"
LINE_COLOR = "#b8c0cc"
ET_NAMES = {"EDT": "ET", "EST": "ET"}


def _fit(draw: ImageDraw.ImageDraw, text: str, size: int, max_w: int) -> ImageFont.ImageFont:
    while True:
        font = ImageFont.load_default(size=size)
        if size <= 16 or draw.textlength(text, font=font) <= max_w:
            return font
        size -= 4


def render_slate(lines: list[str], out_path: Path, size: tuple[int, int] = (1280, 720), title: str = TITLE) -> Path:
    """Title plus up to 3 lines on navy. Same input, same bytes: no timestamps, no metadata."""
    w, h = size
    img = Image.new("RGB", size, BACKGROUND)
    draw = ImageDraw.Draw(img)
    draw.text((w // 2, h * 0.33), title, fill=TITLE_COLOR, font=_fit(draw, title, 96, int(w * 0.9)), anchor="mm")
    for i, line in enumerate(lines[:3]):
        draw.text((w // 2, h * 0.52 + i * 70), line, fill=LINE_COLOR, font=_fit(draw, line, 48, int(w * 0.9)),
                  anchor="mm")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", compress_level=6)
    return out_path


def next_kickoff_lines(games: list[Game], now: datetime, tz: tzinfo) -> list[str]:
    """Slate text: the earliest not-yet-started game after `now`, or a 'none scheduled' card."""
    upcoming = sorted((g for g in games if g.state == "pre" and g.date > now), key=lambda g: g.date)
    if not upcoming:
        return ["No game on", "Next kickoff: none scheduled today"]
    g = upcoming[0]
    local = g.date.astimezone(tz)
    abbr = local.tzname() or ""
    return ["No game on", f"Next kickoff {local:%a %H:%M} {ET_NAMES.get(abbr, abbr)}".rstrip(), g.short_name]


class SlateSource:
    """One ffmpeg process per viewer, looping the PNG as 30 fps H.264 plus silent AAC, MPEG-TS on stdout."""

    def __init__(self, png_path: Path, ffmpeg_bin: str | None = None):
        self.png_path = Path(png_path)
        self.ffmpeg_bin = ffmpeg_bin or os.environ.get("FFMPEG_BIN", "ffmpeg")

    def command(self) -> list[str]:
        return [self.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-re", "-loop", "1", "-framerate", "30",
                "-i", str(self.png_path), "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-g", "60",
                "-c:a", "aac", "-b:a", "64k", "-f", "mpegts", "-"]

    def open(self) -> subprocess.Popen:
        return subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)

    @staticmethod
    def close(proc: subprocess.Popen, grace_s: float = 3.0) -> None:
        """Terminate, kill after `grace_s`, reap, close the pipe."""
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(grace_s)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc.wait()
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
