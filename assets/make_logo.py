"""Generate the channel logo (512x512): assets/logo.png (NCAAF) or, with --sport nhl, assets/logo-nhl.png.
Run: .venv/bin/python assets/make_logo.py [--sport ncaaf|nhl]"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from whiparound.sport import SPORTS

SIZE = 512
NAVY = (12, 26, 58)
WHITE = (245, 245, 245)
ORANGE = (255, 122, 0)


def font(size: int):
    for name in ("Helvetica-Bold.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="ncaaf", choices=sorted(SPORTS))
    sport = SPORTS[ap.parse_args().sport]
    img = Image.new("RGB", (SIZE, SIZE), NAVY)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((16, 16, SIZE - 16, SIZE - 16), radius=48, outline=ORANGE, width=8)
    big, small = font(108), font(80)
    for text, f, y in ((sport.channel_word, big, 118), ("CHANGER", small, 248)):
        w = d.textlength(text, font=f)
        d.text(((SIZE - w) / 2, y), text, font=f, fill=WHITE)
    # two arrows: left-to-right above, right-to-left below
    y1, y2 = 392, 436
    d.line((150, y1, 362, y1), fill=ORANGE, width=12)
    d.polygon([(362, y1 - 22), (400, y1), (362, y1 + 22)], fill=ORANGE)
    d.line((150, y2, 362, y2), fill=ORANGE, width=12)
    d.polygon([(150, y2 - 22), (112, y2), (150, y2 + 22)], fill=ORANGE)
    out = Path(__file__).parent.parent / sport.logo_file
    img.save(out, optimize=True)
    print(out, img.size)


if __name__ == "__main__":
    main()
