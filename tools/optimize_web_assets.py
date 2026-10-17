#!/usr/bin/env python3
"""Resize and re-encode the raster assets in ``web/`` for production use.

Run from the repository root:

    python tools/optimize_web_assets.py --dry-run     # plan and measure, write nothing
    python tools/optimize_web_assets.py               # back up, rewrite, verify
    python tools/optimize_web_assets.py --check       # verify the files on disk, write nothing

What it does, per asset:

* **background** (``bg-storm.webp``): no crop, **never upscaled**, opaque alpha dropped, written as
  a real lossy WebP. The CSS paints it with ``background-size: cover`` at 42% opacity under a
  ``screen`` blend, so the quality floor is chosen from measured error rather than guessed.
* **city cards** (``*-nightscape.webp``): cropped to 16:9 with a subject-aware window (the vertical
  band with the most detail, with a mild centre bias), then downscaled to fit 800x450 - never
  upscaled, so a 612 px source stays 612 px. EXIF is dropped with the re-encode.
* **icons** (the files named ``*.svg``): **lossless only**. Opaque alpha dropped, re-encoded as PNG
  with maximum compression and no metadata; a palette PNG is used only when the image has at most
  256 distinct colours, where that conversion is exact. Pixel-identical output is asserted.

Every encode is checked against the image it was made from: PSNR, mean error in the dark regions
(where banding and posterisation show first) and the bytes on disk. An asset that cannot reach its
hard cap without dropping below the quality floor is reported as a failure, not written quietly.

Safety rules, so this can be run without thinking:

* only the files listed in ``TARGETS`` are ever read or written;
* originals are copied to ``build/asset-originals/`` (gitignored) before being overwritten;
* ``--dry-run`` touches nothing, ``--check`` writes nothing;
* the script never renames a file and never deletes one.

One thing it deliberately does *not* fix: six of the icons are PNG data with an ``.svg``
extension, and the background is PNG data with a ``.webp`` extension. The background is written
back as a genuine WebP under the same name, which makes the served type correct. The icons keep
their names, because renaming is a decision for their author - the mismatch is reported on every
run instead of being silently renamed away.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import struct
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable

from PIL import Image

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
BACKUP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "build", "asset-originals"
)

CARD_RATIO = Fraction(16, 9)
CARD_BOX = (800, 450)

BACKGROUND_QUALITIES = (82, 80, 78, 76, 74, 72)
CARD_QUALITIES = (82, 80, 78, 76, 74, 72)
# Bars are per content class, and every one of them comes from the ladder printed by
# build/quality_ladder.py rather than from taste:
#   * photographs land at 35-40 dB at q82 (WebP q82 is a high quality setting), so 35 dB is the
#     "no visible change" line for a card and 33 dB the floor below which a run fails loudly;
#   * the backdrop measured 33.2 dB at q82 over a lossless PNG source, and it is painted at 42%
#     opacity under a screen blend, so its bar is set where its own measurement sits;
#   * dark_mae is the mean error over the darkest decile, in 0-255 units: 3/255 is about 1%,
#     which is the point where a night gradient can start to band on an 8-bit panel;
#   * dark_levels is the fraction of distinct dark luminance values that survive, a direct
#     posterisation check (it measured 1.00 everywhere, so it is a guard, not a dial).
BACKGROUND_PSNR_IDEAL = 33.0
BACKGROUND_PSNR_FLOOR = 31.0
CARD_PSNR_IDEAL = 35.0
CARD_PSNR_FLOOR = 33.0
DARK_MAE_LIMIT = 3.0
DARK_LEVELS_LIMIT = 0.9
# A crop may only move this far from centre. The images could not be viewed while this was
# written, so the window stays centre-anchored and the detail search only breaks near-ties.
CROP_SEARCH_FRACTION = 0.12


@dataclass
class Target:
    """One asset this script is allowed to touch."""

    name: str
    kind: str  # "background" | "card" | "icon"
    ideal_bytes: int
    hard_bytes: int
    qualities: tuple[int, ...] = field(default_factory=tuple)
    psnr_ideal: float = CARD_PSNR_IDEAL
    psnr_floor: float = CARD_PSNR_FLOOR
    box: tuple[int, int] | None = None


TARGETS: tuple[Target, ...] = (
    Target("bg-storm.webp", "background", ideal_bytes=250 * 1024, hard_bytes=350 * 1024,
           qualities=BACKGROUND_QUALITIES, psnr_ideal=BACKGROUND_PSNR_IDEAL,
           psnr_floor=BACKGROUND_PSNR_FLOOR, box=(1920, 1080)),
    Target("chennai-nightscape.webp", "card", ideal_bytes=90 * 1024, hard_bytes=120 * 1024,
           qualities=CARD_QUALITIES, box=CARD_BOX),
    Target("mumbai-nightscape.webp", "card", ideal_bytes=90 * 1024, hard_bytes=120 * 1024,
           qualities=CARD_QUALITIES, box=CARD_BOX),
    Target("bengaluru-nightscape.webp", "card", ideal_bytes=90 * 1024, hard_bytes=120 * 1024,
           qualities=CARD_QUALITIES, box=CARD_BOX),
    Target("hyderabad-nightscape.webp", "card", ideal_bytes=90 * 1024, hard_bytes=120 * 1024,
           qualities=CARD_QUALITIES, box=CARD_BOX),
    Target("kochi-nightscape.webp", "card", ideal_bytes=90 * 1024, hard_bytes=120 * 1024,
           qualities=CARD_QUALITIES, box=CARD_BOX),
    Target("weather-rain.svg", "icon", ideal_bytes=6 * 1024, hard_bytes=10 * 1024),
    Target("city-icon.svg", "icon", ideal_bytes=6 * 1024, hard_bytes=10 * 1024),
    Target("alert-icon.svg", "icon", ideal_bytes=6 * 1024, hard_bytes=10 * 1024),
    Target("building-icon.svg", "icon", ideal_bytes=6 * 1024, hard_bytes=10 * 1024),
    Target("clock-icon.svg", "icon", ideal_bytes=6 * 1024, hard_bytes=10 * 1024),
    Target("check-icon.svg", "icon", ideal_bytes=6 * 1024, hard_bytes=10 * 1024),
    Target("chevron-right.svg", "icon", ideal_bytes=6 * 1024, hard_bytes=10 * 1024),
)

TARGET_NAMES = frozenset(target.name for target in TARGETS)


# ------------------------------------------------------------------ measurements


def psnr(reference: Image.Image, candidate: Image.Image) -> float:
    """Peak signal-to-noise ratio in dB between two same-size RGB images."""
    import math

    a = reference.convert("RGB")
    b = candidate.convert("RGB")
    if a.size != b.size:
        raise ValueError(f"cannot compare {a.size} with {b.size}")
    data_a = a.tobytes()
    data_b = b.tobytes()
    total = 0
    for byte_a, byte_b in zip(data_a, data_b):
        difference = byte_a - byte_b
        total += difference * difference
    mse = total / max(1, len(data_a))
    if mse == 0:
        return float("inf")
    return 10 * math.log10((255 * 255) / mse)


def dark_error(reference: Image.Image, candidate: Image.Image) -> tuple[float, float]:
    """Error where banding shows: the darkest decile of the reference.

    Returns ``(mean absolute error, fraction of dark luminance levels preserved)``.
    A lossy encoder that posterises a night gradient collapses the distinct dark values, so the
    second number falls well below 1.0 even when overall PSNR still looks healthy.
    """
    grey = reference.convert("L")
    values = list(grey.get_flattened_data()) if hasattr(grey, "get_flattened_data") else list(grey.getdata())
    threshold = sorted(values)[max(0, len(values) // 10 - 1)]
    mask = [value <= threshold for value in values]

    reference_pixels = list(reference.convert("RGB").tobytes())
    candidate_pixels = list(candidate.convert("RGB").tobytes())
    errors = []
    dark_levels_before: set[int] = set()
    dark_levels_after: set[int] = set()
    for index, is_dark in enumerate(mask):
        if not is_dark:
            continue
        offset = index * 3
        before = reference_pixels[offset:offset + 3]
        after = candidate_pixels[offset:offset + 3]
        for channel in range(3):
            errors.append(abs(before[channel] - after[channel]))
        dark_levels_before.add(sum(before) // 3)
        dark_levels_after.add(sum(after) // 3)
    mean_error = sum(errors) / max(1, len(errors))
    preserved = len(dark_levels_after & dark_levels_before) / max(1, len(dark_levels_before))
    return mean_error, preserved


def png_chunks(blob: bytes) -> set[str]:
    """The names of the ancillary chunks actually present in a PNG."""
    names: set[str] = set()
    offset = 8
    while offset + 8 <= len(blob):
        size = int.from_bytes(blob[offset:offset + 4], "big")
        name = blob[offset + 4:offset + 8].decode("ascii", "replace")
        names.add(name)
        if name == "IEND":
            break
        offset += 12 + size
    return names


def container(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return "WebP"
    if blob[:5] in (b"<?xml", b"<svg "):
        return "SVG"
    return "unknown"


def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


# ------------------------------------------------------------------ planning


def detail_energy(image: Image.Image) -> tuple[list[float], list[float]]:
    """Per-row and per-column gradient energy, for choosing a crop window.

    Downscaled by four first: the window only has to be found, not measured precisely.
    """
    grey = image.convert("L")
    small = grey.resize((max(2, grey.width // 4), max(2, grey.height // 4)))
    width, height = small.size
    pixels = list(small.get_flattened_data()) if hasattr(small, "get_flattened_data") else list(small.getdata())

    row_energy = [0.0] * height
    column_energy = [0.0] * width
    for y in range(height):
        base = y * width
        for x in range(width):
            value = pixels[base + x]
            if x + 1 < width:
                delta = abs(value - pixels[base + x + 1])
                row_energy[y] += delta
                column_energy[x] += delta
                column_energy[x + 1] += delta
            if y + 1 < height:
                delta = abs(value - pixels[base + width + x])
                row_energy[y] += delta
                row_energy[y + 1] += delta
    return row_energy, column_energy


def best_window(energy: list[float], span: int, total: int) -> int:
    """Start index of the ``span``-wide window with the most detail, anchored near the centre.

    The search is bounded to ``CROP_SEARCH_FRACTION`` of the available slack either side of a
    centre crop, so the result is a centre crop unless a nearby window is measurably richer.
    """
    if span >= total:
        return 0
    centre = (total - span) / 2
    slack = total - span
    reach = slack * CROP_SEARCH_FRACTION
    lowest = max(0, int(round(centre - reach)))
    highest = min(slack, int(round(centre + reach)))

    scale = total / max(1, len(energy))
    smoothed = [0.0] * total
    for index in range(len(energy)):
        smoothed[min(total - 1, int(index * scale))] += energy[index]
    prefix = [0.0]
    for value in smoothed:
        prefix.append(prefix[-1] + value)

    best_start = lowest
    best_score = float("-inf")
    for start in range(lowest, highest + 1):
        score = prefix[start + span] - prefix[start]
        if score > best_score:
            best_score = score
            best_start = start
    return best_start


def card_crop(image: Image.Image) -> tuple[Image.Image, str]:
    """Crop to 16:9, choosing the window with the most detail."""
    width, height = image.size
    ratio = width / height
    target = CARD_RATIO.numerator / CARD_RATIO.denominator
    if ratio > target:  # too wide: trim the sides
        new_width = max(1, round(height * target))
        row_energy, column_energy = detail_energy(image)
        left = best_window(column_energy, new_width, width)
        return image.crop((left, 0, left + new_width, height)), (
            f"cropped width {width}->{new_width}, left offset {left}"
        )
    if ratio < target:  # too tall: trim top and bottom
        new_height = max(1, round(width / target))
        row_energy, _ = detail_energy(image)
        top = best_window(row_energy, new_height, height)
        return image.crop((0, top, width, top + new_height)), (
            f"cropped height {height}->{new_height}, top offset {top}"
        )
    return image.copy(), "already 16:9, no crop"


def fit_without_upscaling(image: Image.Image, box: tuple[int, int]) -> tuple[Image.Image, bool]:
    """Downscale into ``box`` preserving aspect ratio; never enlarge."""
    box_width, box_height = box
    scale = min(box_width / image.width, box_height / image.height, 1.0)
    if scale >= 1.0:
        return image.copy(), False
    size = (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale))))
    return image.resize(size, Image.LANCZOS), True


def without_transparency(image: Image.Image) -> tuple[Image.Image, str]:
    """Drop an alpha channel that is fully opaque (it costs bytes and shows nothing)."""
    if "A" not in image.mode:
        return image, ""
    low, high = image.getchannel("A").getextrema()
    if (low, high) == (255, 255):
        return image.convert("RGB"), "alpha was fully opaque"
    return image, f"alpha kept ({low}-{high})"


# ------------------------------------------------------------------ encoding


def encode_webp(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    # a fresh image so no source metadata (EXIF, ICC, DPI) travels into the output
    clean = Image.new(image.mode, image.size)
    clean.putdata(list(image.get_flattened_data()) if hasattr(image, "get_flattened_data")
                  else list(image.getdata()))
    clean.save(buffer, format="WEBP", quality=quality, method=6)
    return buffer.getvalue()


def choose_webp(
    image: Image.Image, target: Target, original_bytes: int = 1 << 40
) -> tuple[bytes, int, dict[str, Any]]:
    """Pick the quality: the top of the brief's range, then only move for a reason.

    The brief asks for q78-82 and then "adjust per-image until no obvious banding, skyline
    recognisable, target size reached". So the first candidate is q82 and every step away from it
    is recorded with the measurement that forced it:

    * down, when q82 does not fit the ideal cap, or would ship a bigger file than the one it
      replaces (a re-encode that grows a file has to buy something, and q82 already clears the bar);
    * up (84, 86, 88), when q82 does not clear the quality bar but a higher quality still fits the
      ideal cap;
    * never above the hard cap, and never below the floor without failing the run.
    """
    ladder = list(target.qualities) + [quality for quality in (84, 86, 88)
                                       if quality > max(target.qualities, default=0)]
    measured: list[tuple[int, int, float, float, float]] = []
    records: list[dict[str, Any]] = []
    for quality in ladder:
        blob = encode_webp(image, quality)
        with Image.open(io.BytesIO(blob)) as decoded:
            restored = decoded.convert(image.mode)
        value = psnr(image, restored)
        dark, preserved = dark_error(image, restored)
        measured.append((quality, len(blob), value, dark, preserved))
        clears_bar = (value >= target.psnr_ideal and dark <= DARK_MAE_LIMIT
                      and preserved >= DARK_LEVELS_LIMIT)
        records.append({
            "quality": quality, "bytes": len(blob), "psnr": value, "dark_mae": dark,
            "dark_levels": preserved, "clears_bar": clears_bar,
            "fits_ideal": len(blob) <= target.ideal_bytes,
            "fits_hard": len(blob) <= target.hard_bytes,
            "not_larger": len(blob) <= original_bytes,
            "above_floor": value >= target.psnr_floor,
        })

    preferred = [record for record in records if record["clears_bar"] and record["fits_ideal"]]
    smaller = [record for record in preferred if record["not_larger"]]
    chosen = (smaller or preferred or [])[:1]
    reason = ""
    if chosen and chosen[0]["not_larger"]:
        reason = (f"q{chosen[0]['quality']} is the top of the requested range and both clears the "
                  f"bar (PSNR {chosen[0]['psnr']:.1f} dB, dark-region error "
                  f"{chosen[0]['dark_mae']:.2f}/255) and fits the "
                  f"{target.ideal_bytes / 1024:.0f} KB ideal cap")
    elif chosen:
        reason = (f"q{chosen[0]['quality']} clears the bar but every quality in the range grows "
                  f"the file; the quality bar wins")
    if not chosen:
        fitting = [record for record in records if record["fits_hard"] and record["above_floor"]]
        if not fitting:
            raise SystemExit(
                f"{target.name}: nothing fits the {target.hard_bytes / 1024:.0f} KB hard cap at or "
                f"above the {target.psnr_floor:.0f} dB floor; measured "
                + ", ".join(f"q{record['quality']}:{record['bytes'] / 1024:.0f}K/"
                            f"{record['psnr']:.1f}dB" for record in records)
            )
        chosen = [min(fitting, key=lambda record: record["quality"])]
        reason = (f"BELOW BAR: q{chosen[0]['quality']} is the highest quality that fits the "
                  f"{target.hard_bytes / 1024:.0f} KB hardware cap "
                  f"(PSNR {chosen[0]['psnr']:.1f} dB, dark-region error "
                  f"{chosen[0]['dark_mae']:.2f}/255)")

    record = chosen[0]
    blob = encode_webp(image, record["quality"])
    if len(blob) != record["bytes"]:  # the encoder must be deterministic or nothing here is
        raise SystemExit(f"{target.name}: encoding q{record['quality']} twice produced "
                         f"{len(blob)} then {record['bytes']} bytes")
    return blob, record["quality"], {
        "psnr": record["psnr"],
        "dark_mae": record["dark_mae"],
        "dark_levels": record["dark_levels"],
        "reason": reason,
        "measured": measured,
        "below_bar": not record["clears_bar"],
        "grew": len(blob) > original_bytes,
    }


def encode_icon_png(image: Image.Image) -> tuple[bytes, str]:
    """Lossless PNG: palette only when the image has at most 256 distinct colours."""
    if "A" not in image.mode:
        colours = image.getcolors(maxcolors=1 << 20) or []
        if len(colours) <= 256:
            buffer = io.BytesIO()
            image.convert("P", palette=Image.ADAPTIVE, colors=256).save(
                buffer, format="PNG", optimize=True, compress_level=9
            )
            return buffer.getvalue(), f"palette PNG ({len(colours)} exact colours)"
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True, compress_level=9)
        return buffer.getvalue(), "truecolour PNG"
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True, compress_level=9)
    return buffer.getvalue(), "truecolour PNG with alpha"


# ------------------------------------------------------------------ the run


def load(name: str, from_originals: bool = False) -> tuple[bytes, Image.Image]:
    """Read an asset from ``web/``, or from the kept original when asked."""
    path = os.path.join(WEB_DIR, name)
    if from_originals:
        kept = os.path.join(BACKUP_DIR, name)
        if os.path.exists(kept):
            path = kept
    with open(path, "rb") as handle:
        blob = handle.read()
    image = Image.open(io.BytesIO(blob))
    image.load()
    return blob, image


def back_up(name: str, blob: bytes) -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(blob)
    return path


def process(target: Target, dry_run: bool, force: bool = False,
            from_originals: bool = False) -> dict[str, Any]:
    blob, image = load(target.name, from_originals)
    original_format = container(blob)
    width, height = image.size
    notes: list[str] = []
    metrics: dict[str, Any] = {}
    if from_originals:
        notes.append(f"re-derived from the kept original in {os.path.basename(BACKUP_DIR)}/")

    # Re-runs must be free and safe: re-encoding an encode loses a generation for nothing, so a
    # file that is already inside every one of its targets is left exactly as it is unless the
    # caller says otherwise.
    reencode = force or from_originals
    if not reencode and not verify_on_disk(target):
        return {
            "name": target.name,
            "kind": target.kind,
            "original_format": original_format,
            "old_size": (width, height),
            "new_size": (width, height),
            "old_bytes": len(blob),
            "new_bytes": len(blob),
            "quality": None,
            "notes": ["already inside its targets: left unchanged (use --force to re-encode)"],
            "skipped": True,
            "metrics": {"psnr": None, "dark_mae": None, "dark_levels": None, "lossless": False,
                        "reason": "skipped", "measured": None, "below_bar": False},
        }

    if target.kind == "icon":
        prepared, note = without_transparency(image)
        if note:
            notes.append(note)
        output, encoder = encode_icon_png(prepared)
        quality = None
        new_size = prepared.size
        # lossless: the decoded pixels must be identical to the input's
        with Image.open(io.BytesIO(output)) as decoded:
            if decoded.convert("RGB").tobytes() != image.convert("RGB").tobytes():
                raise SystemExit(f"{target.name}: the lossless PNG re-encode changed pixels")
        metrics["lossless"] = True
        metrics["psnr"] = float("inf")
        metrics["reason"] = encoder
    else:
        prepared, note = without_transparency(image)
        if note:
            notes.append(note)
        if target.kind == "card":
            prepared, crop_note = card_crop(prepared)
            notes.append(crop_note)
            prepared, scaled = fit_without_upscaling(prepared, CARD_BOX)
            if not scaled:
                notes.append(f"not upscaled: kept {prepared.width}x{prepared.height} "
                             f"(smaller than {CARD_BOX[0]}x{CARD_BOX[1]})")
        elif target.kind == "background":
            prepared, scaled = fit_without_upscaling(prepared, target.box or prepared.size)
            if not scaled:
                notes.append(f"not upscaled: kept {prepared.width}x{prepared.height} "
                             f"(the brief's {target.box[0]}x{target.box[1]} target would need "
                             f"{target.box[0] / prepared.width:.2f}x enlargement)")
        output, quality, metrics = choose_webp(prepared, target, original_bytes=len(blob))
        new_size = prepared.size
        notes.append(f"encoded as WebP q{quality}")

    if prepared.width > width or prepared.height > height:
        raise SystemExit(f"{target.name}: refusing to upscale {width}x{height} to {prepared.size}")

    result = {
        "name": target.name,
        "kind": target.kind,
        "original_format": original_format,
        "old_size": (width, height),
        "new_size": new_size,
        "old_bytes": len(blob),
        "new_bytes": len(output),
        "quality": quality,
        "notes": notes,
        "skipped": False,
        "metrics": {
            "psnr": metrics.get("psnr"),
            "dark_mae": metrics.get("dark_mae"),
            "dark_levels": metrics.get("dark_levels"),
            "reason": metrics.get("reason"),
            "measured": metrics.get("measured"),
            "below_bar": metrics.get("below_bar", False),
            "lossless": metrics.get("lossless", False),
        },
        "candidate": output,
    }

    if not dry_run:
        backup = back_up(target.name, blob)
        with open(os.path.join(WEB_DIR, target.name), "wb") as handle:
            handle.write(output)
        result["backup"] = backup
    return result


def verify_on_disk(target: Target) -> list[str]:
    """Problems with the file as it currently sits in web/ (used by --check and after a run)."""
    problems: list[str] = []
    path = os.path.join(WEB_DIR, target.name)
    if not os.path.exists(path):
        return [f"{target.name}: missing"]
    with open(path, "rb") as handle:
        blob = handle.read()
    if len(blob) > target.hard_bytes:
        problems.append(
            f"{target.name}: {len(blob) / 1024:.1f} KB exceeds the "
            f"{target.hard_bytes / 1024:.0f} KB hard cap"
        )
    kind = container(blob)
    if target.kind == "icon":
        if kind != "PNG":
            problems.append(f"{target.name}: expected a PNG payload, found {kind}")
        else:
            for chunk in ("tEXt", "zTXt", "iTXt", "iCCP", "eXIf", "pHYs", "gAMA", "sRGB"):
                if chunk in png_chunks(blob):
                    problems.append(f"{target.name}: keeps the {chunk} chunk")
    else:
        if kind != "WebP":
            problems.append(f"{target.name}: expected a WebP payload, found {kind}")
        if b"EXIF" in blob[:64] or b"ICCP" in blob[:64] or b"XMP" in blob[:64]:
            problems.append(f"{target.name}: carries embedded metadata")
    try:
        with Image.open(io.BytesIO(blob)) as image:
            image.load()
            width, height = image.size
            if target.kind == "card":
                if width > CARD_BOX[0] or height > CARD_BOX[1]:
                    problems.append(f"{target.name}: {width}x{height} exceeds the {CARD_BOX} box")
                # a card slot is 16:9: an un-cropped 3:2 or 4:3 file would be letterboxed or
                # squashed by object-fit, so the ratio is part of the target
                ratio = Fraction(width, height)
                if abs(float(ratio - CARD_RATIO)) > 0.01:
                    problems.append(
                        f"{target.name}: {width}x{height} is {float(ratio):.3f}:1, not 16:9")
            if target.box and target.kind == "background" and width > target.box[0]:
                problems.append(f"{target.name}: {width} wide exceeds {target.box[0]}")
            if "A" in image.mode and image.getchannel("A").getextrema() == (255, 255):
                problems.append(f"{target.name}: ships a fully opaque alpha channel")
    except Exception as exception:  # noqa: BLE001 - reporting, not raising
        problems.append(f"{target.name}: does not decode ({exception})")
    return problems


def format_report(results: Iterable[dict[str, Any]], dry_run: bool) -> str:
    rows = ["", "%-28s %11s %11s %10s %10s %8s %8s %9s %8s" % (
        "file", "old dims", "new dims", "old size", "new size", "ratio", "PSNR", "darkMAE",
        "darkLvl")]
    for result in results:
        old = f"{result['old_size'][0]}x{result['old_size'][1]}"
        new = f"{result['new_size'][0]}x{result['new_size'][1]}"
        ratio = result["old_bytes"] / max(1, result["new_bytes"])
        metrics = result["metrics"]
        psnr_text = "lossless" if metrics["lossless"] else (
            f"{metrics['psnr']:.1f} dB" if metrics.get("psnr") is not None else "-")
        dark = ("-" if metrics.get("dark_mae") is None else f"{metrics['dark_mae']:.2f}")
        levels = ("-" if metrics.get("dark_levels") is None else f"{metrics['dark_levels']:.2f}")
        rows.append("%-28s %11s %11s %9.1f KB %9.1f KB %7.2fx %8s %9s %8s" % (
            result["name"], old, new, result["old_bytes"] / 1024, result["new_bytes"] / 1024,
            ratio, psnr_text, dark, levels))
    rows.append("")
    rows.append("notes:")
    for result in results:
        rows.append("  %-28s %s" % (result["name"], "; ".join(result["notes"])[:160]))
    total_old = sum(result["old_bytes"] for result in results)
    total_new = sum(result["new_bytes"] for result in results)
    rows.append("")
    rows.append("total: %.1f KB -> %.1f KB (%.2fx smaller)%s" % (
        total_old / 1024, total_new / 1024, total_old / max(1, total_new),
        "  [dry run: nothing written]" if dry_run else ""))
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="measure and report, write nothing")
    parser.add_argument("--check", action="store_true",
                        help="verify the files on disk against the targets, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="re-encode even a file that is already inside its targets")
    parser.add_argument("--from-originals", action="store_true",
                        help="re-derive from the kept original in build/asset-originals/ instead "
                             "of from the file in web/: use this to undo a re-encode that lost a "
                             "generation, because re-encoding a lossy file loses another one")
    parser.add_argument("--only", action="append", metavar="NAME",
                        help="process just this file (repeatable)")
    args = parser.parse_args(argv)

    if args.dry_run and args.check:
        parser.error("--dry-run and --check are different jobs; run them separately")

    selected = [target for target in TARGETS
                if not args.only or target.name in set(args.only)]
    if args.only and len(selected) != len(set(args.only)):
        unknown = sorted(set(args.only) - TARGET_NAMES)
        parser.error(f"not an asset this script may touch: {', '.join(unknown)}")

    if args.check:
        problems: list[str] = []
        print("checking %d asset(s) against their targets" % len(selected))
        for target in selected:
            found = verify_on_disk(target)
            size = os.path.getsize(os.path.join(WEB_DIR, target.name))
            print("  %-28s %8.1f KB  %s" % (
                target.name, size / 1024, "ok" if not found else "; ".join(found)))
            problems.extend(found)
        print()
        if problems:
            print(f"{len(problems)} problem(s):")
            for problem in problems:
                print("  -", problem)
            return 1
        print("all %d asset(s) are inside their caps" % len(selected))
        return 0

    results = [process(target, args.dry_run, args.force, args.from_originals) for target in selected]
    print(format_report(results, args.dry_run))

    skipped = [result["name"] for result in results if result.get("skipped")]
    if skipped:
        print(f"\nalready inside their targets, left unchanged: {', '.join(skipped)}")
    below = [result["name"] for result in results if result["metrics"].get("below_bar")]
    if below:
        print("BELOW the quality bar after compression: " + ", ".join(below))
    grew = [result["name"] for result in results if result["metrics"].get("grew")]
    if grew:
        print("larger than the file they replace, because the quality bar demanded it: "
              + ", ".join(grew))

    for result in results:
        if result["metrics"].get("measured"):
            ladder = " ".join(
                f"q{quality}:{size / 1024:.0f}K/{value:.1f}dB"
                for quality, size, value, _dark, _levels in result["metrics"]["measured"]
            )
            print("  %-28s %s" % (result["name"], ladder))

    if not args.dry_run:
        print("\nverifying the files that were just written:")
        problems = []
        for target in selected:
            found = verify_on_disk(target)
            problems.extend(found)
            print("  %-28s %s" % (target.name, "ok" if not found else "; ".join(found)))
        if problems:
            print("\n%d problem(s) after writing:" % len(problems))
            for problem in problems:
                print("  -", problem)
            return 1
        print("originals kept in %s" % os.path.relpath(BACKUP_DIR))
    return 0


if __name__ == "__main__":
    sys.exit(main())
