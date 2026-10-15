"""Generate the dashboard's decorative backdrop: ``web/bg-storm.webp``.

Run once and commit the result — the website itself has no build step and no remote
image dependencies:

    python tools/make_background.py

The output is deliberately dark and low-contrast: it sits behind the dashboard under a
``mix-blend-mode: screen`` layer at 42% opacity, so it must never compete with text.

Pillow is the only dependency and it is only needed to regenerate the asset, never to
serve or test the dashboard.
"""

from __future__ import annotations

import argparse
import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

DEFAULT_WIDTH = 1760
DEFAULT_HEIGHT = 990
DEFAULT_QUALITY = 74
DEFAULT_SEED = 20260214

# Storm palette (kept in sync with web/styles.css by eye, not by code).
STOPS = [
    (0.00, (3, 7, 20)),
    (0.28, (6, 18, 40)),
    (0.55, (11, 33, 66)),
    (0.78, (8, 22, 48)),
    (1.00, (3, 8, 22)),
]
GLOW_COLOR = (34, 211, 238)
GLOW_CENTER = (0.5, 0.06)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _gradient_slice(height: int) -> Image.Image:
    """A 1-pixel-wide vertical gradient that is later stretched to full size."""
    column = Image.new("RGB", (1, height))
    pixels = column.load()
    for y in range(height):
        position = y / max(1, height - 1)
        lower = STOPS[0]
        upper = STOPS[-1]
        for index in range(len(STOPS) - 1):
            if STOPS[index][0] <= position <= STOPS[index + 1][0]:
                lower, upper = STOPS[index], STOPS[index + 1]
                break
        span = max(1e-6, upper[0] - lower[0])
        t = (position - lower[0]) / span
        pixels[0, y] = tuple(
            int(round(_lerp(lower[1][channel], upper[1][channel], t))) for channel in range(3)
        )
    return column


def _radial_mask(size: int, center: tuple[float, float], radius: float, gamma: float = 1.6) -> Image.Image:
    """Soft radial falloff mask, built small and then scaled up (cheap and smooth)."""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    cx, cy = center[0] * size, center[1] * size
    limit = radius * size
    steps = max(2, int(limit))
    for step in range(steps, 0, -1):
        fraction = step / steps
        value = int(round(255 * max(0.0, (1.0 - fraction) ** gamma)))
        draw.ellipse(
            [cx - limit * fraction, cy - limit * fraction, cx + limit * fraction, cy + limit * fraction],
            fill=value,
        )
    return mask.filter(ImageFilter.GaussianBlur(size * 0.04))


def build(width: int, height: int, seed: int) -> Image.Image:
    rng = random.Random(seed)

    base = _gradient_slice(max(8, height // 4)).resize((width, height), Image.BICUBIC)

    # storm-cell glow near the top
    glow_mask = _radial_mask(256, GLOW_CENTER, 0.95).resize((width, height), Image.BICUBIC)
    glow = Image.new("RGB", (width, height), GLOW_COLOR)
    base = Image.composite(Image.blend(base, glow, 0.62), base, glow_mask.point(lambda v: int(v * 0.95)))

    # a second, cooler pool of light low-left for depth
    pool_mask = _radial_mask(256, (0.18, 0.92), 0.80).resize((width, height), Image.BICUBIC)
    pool = Image.new("RGB", (width, height), (56, 132, 255))
    base = Image.composite(Image.blend(base, pool, 0.34), base, pool_mask.point(lambda v: int(v * 0.55)))

    # driven rain: faint diagonal streaks
    streaks = Image.new("L", (width, height), 0)
    streak_draw = ImageDraw.Draw(streaks)
    for _ in range(320):
        start_x = rng.uniform(-width * 0.2, width)
        start_y = rng.uniform(-height * 0.1, height)
        length = rng.uniform(height * 0.12, height * 0.5)
        angle = math.radians(rng.uniform(14, 22))
        end_x = start_x + math.sin(angle) * length
        end_y = start_y + math.cos(angle) * length
        streak_draw.line(
            [(start_x, start_y), (end_x, end_y)],
            fill=rng.randint(60, 150),
            width=1,
        )
    streaks = streaks.filter(ImageFilter.GaussianBlur(1.6))
    rain_layer = Image.composite(Image.new("RGB", (width, height), (170, 220, 255)), base, streaks)
    base = Image.blend(base, rain_layer, 0.30)

    # fine grain, added as blurred noise at low strength
    noise_small = Image.new("L", (width // 8, height // 8))
    noise_small.putdata([rng.randint(96, 160) for _ in range(noise_small.width * noise_small.height)])
    noise = noise_small.resize((width, height), Image.BICUBIC).filter(ImageFilter.GaussianBlur(0.6))
    base = ImageChops.add(base, Image.merge("RGB", (noise, noise, noise)).point(lambda v: int(v * 0.06)))

    # vignette so the corners stay dark behind content
    vignette = _radial_mask(256, (0.5, 0.44), 1.25, gamma=0.9).resize((width, height), Image.BICUBIC)
    dark = Image.new("RGB", (width, height), (2, 5, 14))
    base = Image.composite(base, dark, vignette.point(lambda v: int(min(255, v * 1.35))))

    return base.filter(ImageFilter.GaussianBlur(0.4))


def _relative_luminance(rgb: tuple[float, float, float]) -> float:
    channels = []
    for value in rgb:
        srgb = value / 255.0
        channels.append(srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_ratio(foreground: tuple[int, int, int], background: tuple[int, int, int]) -> float:
    a = _relative_luminance(foreground) + 0.05
    b = _relative_luminance(background) + 0.05
    return max(a, b) / min(a, b)


PALETTE = {
    "body #e2e8f0": (226, 232, 240),
    "muted #94a3b8": (148, 163, 184),
    "NORMAL #22d3ee": (34, 211, 238),
    "WATCH #fbbf24": (251, 191, 36),
    "WARNING bar #f97316": (249, 115, 22),
    "WARNING text #fdba74": (253, 186, 116),
    "CRITICAL bar #ef4444": (239, 68, 68),
    "CRITICAL text #fca5a5": (252, 165, 165),
}


def preview(background: Image.Image, output: Path) -> None:
    """Approximate the browser's CSS stack so legibility can be checked offline.

    Mirrors web/styles.css: body gradient, the backdrop at 42% opacity under
    ``mix-blend-mode: screen``, then the radial darkening overlay, then sample text in
    the same colour and size the dashboard uses. It also prints objective numbers: how
    much the backdrop actually changes the background, and WCAG contrast ratios for the
    dashboard palette on top of it.
    """
    width, height = background.size
    base = _gradient_slice(256).resize((width, height), Image.BICUBIC).convert("RGB")

    scaled = background.point(lambda v: int(v * 0.42))
    screened = ImageChops.screen(base, scaled)

    overlay = _radial_mask(384, (0.5, -0.1), 1.35, gamma=1.15).resize((width, height), Image.BICUBIC)
    dark = Image.new("RGB", (width, height), (4, 9, 26))
    composed = Image.composite(screened, Image.blend(screened, dark, 0.94), overlay.point(lambda v: int(v * 1.6)))

    plain = Image.composite(base, Image.blend(base, dark, 0.94), overlay.point(lambda v: int(v * 1.6)))

    # how much does the backdrop change the background? (measured before any sample text)
    diff = ImageChops.difference(composed.convert("L"), plain.convert("L"))
    pixels = diff.tobytes()
    mean_delta = sum(pixels) / len(pixels)
    max_delta = max(pixels)

    draw = ImageDraw.Draw(composed)
    draw.rectangle([0, 0, width, 74], fill=(7, 17, 41))
    draw.text((28, 18), "FloodPulse   LIVE   model heuristic-v1", fill=(226, 232, 240))
    draw.text((28, 48), "SIMULATED DATA - heuristic risk scoring, not a validated model", fill=(253, 230, 138))

    from PIL import ImageFont

    try:
        font = ImageFont.load_default(size=26)
        small = ImageFont.load_default(size=18)
    except TypeError:  # older Pillow without size support
        font, small = ImageFont.load_default(), ImageFont.load_default()
    draw.text((28, 132), "Riverside North", fill=(226, 232, 240), font=font)
    draw.text((28, 176), "risk score 0.1703   NORMAL   3 s ago", fill=(34, 211, 238), font=small)
    draw.text((28, 214), "rainfall 42.5 mm/h   water level 2.40 m   drain 1.85 m", fill=(203, 213, 225), font=small)
    draw.text((28, 252), "explanation: weighted base 0.1703 + trend 0.0000", fill=(148, 163, 184), font=small)
    draw.rectangle([28, 300, 620, 430], fill=(15, 33, 64), outline=(148, 163, 184))
    draw.text((44, 318), "card body text over a glass panel", fill=(226, 232, 240), font=small)
    draw.text((44, 350), "CRITICAL WARNING WATCH NORMAL", fill=(239, 68, 68), font=small)
    draw.text((44, 382), "muted caption 11px", fill=(148, 163, 184), font=small)

    output.parent.mkdir(parents=True, exist_ok=True)
    composed.save(output)

    # WCAG contrast of the dashboard palette on the composed background
    worst = None
    for name, colour in PALETTE.items():
        ratio = min(
            _contrast_ratio(colour, composed.getpixel((int(width * fx), int(height * fy))))
            for fx in (0.02, 0.25, 0.5, 0.75, 0.98)
            for fy in (0.02, 0.3, 0.6, 0.95)
        )
        if worst is None or ratio < worst[1]:
            worst = (name, ratio)
        print(f"  contrast {name:<18} min {ratio:5.2f}:1")

    print(f"wrote preview {output}")
    print(f"  backdrop contribution: mean +{mean_delta:.1f} / max +{max_delta} luminance levels")
    print(f"  weakest palette contrast: {worst[0]} at {worst[1]:.2f}:1 (WCAG AA body text needs 4.5:1)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate web/bg-storm.webp")
    parser.add_argument("--out", default="web/bg-storm.webp")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--quality", type=int, default=DEFAULT_QUALITY)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--preview", default="build/background-preview.png",
                        help="write an approximate browser composite (offline legibility check)")
    args = parser.parse_args()

    image = build(args.width, args.height, args.seed)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="WEBP", quality=args.quality, method=6)

    pixels = image.load()
    sample = [pixels[x, y] for y in range(0, args.height, 97) for x in range(0, args.width, 101)]
    mean = tuple(round(sum(channel[index] for channel in sample) / len(sample), 1) for index in range(3))
    brightest = max(sum(channel) for channel in sample) / 3

    size_kb = output.stat().st_size / 1024
    print(f"wrote {output} — {args.width}x{args.height}, {size_kb:.1f} KB")
    print(f"mean RGB {mean}, brightest sampled pixel {brightest:.0f}/255")
    if size_kb > 250:
        print("WARNING: above the 250 KB target for the dashboard backdrop")
    if args.preview:
        preview(image, Path(args.preview))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
