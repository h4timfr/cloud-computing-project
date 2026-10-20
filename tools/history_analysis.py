"""Retrospective temporal analysis of every active city's stored history (M16).

Read-only: it reads the public ``GET /zones/{zone_id}/history`` endpoint and describes what the
stored risk scores did. It is **not** a forecast, and it is not a prediction model: nothing is
extrapolated, and no value is invented. Every number below is computed by
``floodpulse.temporal`` - the same Python contract the dashboard mirrors in ``web/lib.js`` - so
this tool and the page cannot drift apart. A city whose window cannot produce a value reports it
as ``-`` here and as ``null`` in ``--json`` output.

    python tools/history_analysis.py                     # live API, 24 h window, terminal table
    python tools/history_analysis.py --minutes 60        # a single hour
    python tools/history_analysis.py --json out.json     # machine-readable copy of the same numbers
    python tools/history_analysis.py --api http://127.0.0.1:8199

Methodology, in the words the dashboard uses
--------------------------------------------

* **sample** - one stored history row: a timestamp, a risk score and the readings stored with it.
* **window** - a slice by age: the newest sample plus everything at most ``--minutes`` older, i.e.
  the window is anchored on the newest sample, not on the wall clock.
* **duration** - newest timestamp minus oldest timestamp inside the window.
* **valid** - a row whose timestamp is usable and whose score is a finite number inside 0..1.
  A row that is not valid is dropped, never guessed at: an out-of-range score is dropped, a
  non-finite score (``NaN``/``Infinity``) is dropped, and a row with no usable timestamp cannot
  enter the window at all. Such rows are counted in ``invalid samples``.
* **net change** - last valid score minus first valid score.
* **peak, minimum** - the largest and smallest valid score in the window; on a tie the earlier row wins.
* **trend** - the direction of the least-squares slope of score against elapsed hours, described
  with an explicit tolerance of plus or minus 0.02 per hour: a slope above that is RISING, below
  minus that is FALLING, and inside it is STEADY. It is retrospective - what the stored scores
  did, never what they will do. A window that rose and then eased is still reported by its slope,
  and the peak and net change are reported beside it rather than bent to agree.
* **gap** - an interval longer than ``max(3 x median interval, 900 s)``, which qualifies how far
  the direction can be trusted. Reported as the largest gap, plus how many gaps there were.
* Rows that arrive out of order are sorted; when two rows share a timestamp the later one wins
  (duplicate last-wins).
* Trend needs at least 3 valid samples spanning at least 600 s; below those minimums it is
  ``INSUFFICIENT`` and the trend is unknown.
* Band labels come from the engine's own ``risk.classify`` (the frozen ``heuristic-v1`` edges
  0.35 / 0.60 / 0.80), never from a second copy of those numbers.

Columns: samples (valid samples used), duration, median interval, max gap, current risk (last
valid score), min risk, max risk, net change, trend, current rainfall, max rainfall, invalid
samples (rows dropped as unusable).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:  # pragma: no cover - import bootstrap for direct invocation
    sys.path.insert(0, str(SRC))

from floodpulse import temporal  # noqa: E402

DEFAULT_API = "https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com"
DEFAULT_MINUTES = 1440
PLACEHOLDER = "-"

#: Printed columns: (JSON key, heading, decimals for a number, or ``None`` for a label).
#: Scores are stored at a 4-decimal resolution, so they are printed at that resolution rather
#: than rounded to a value that could print a non-zero change as ``0``.
COLUMNS = (
    ("samples", "samples", None),
    ("duration_seconds", "dur", 0),
    ("median_interval", "med", 0),
    ("largest_gap", "gap", 0),
    ("current_risk", "risk", 4),
    ("min_risk", "min", 4),
    ("max_risk", "max", 4),
    ("net_change", "net", 4),
    ("trend", "trend", None),
    ("current_rainfall", "rain", 2),
    ("max_rainfall", "rain-max", 2),
    ("invalid_samples", "invalid", None),
)

#: Smallest width of a printed column.
COLUMN_WIDTH = 7


def fetch(url: str, timeout: float = 25.0) -> dict:
    """Read one JSON body. Read-only: this tool issues GETs and nothing else."""
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed https endpoint
        return json.loads(response.read())


def city_row(api: str, zone: dict, minutes: int) -> dict:
    """One city's row, computed entirely from the shared temporal contract.

    On a failed read the row carries ``error`` and every metric is ``None``, which prints as
    ``-``: a city that could not be read is never given numbers.
    """
    zone_id = str(zone.get("zone_id") or zone.get("id") or "")
    name = str(zone.get("name") or zone_id)
    row = {"name": name, "zone_id": zone_id, "error": None}
    row.update({key: None for key, _heading, _decimals in COLUMNS})
    url = f"{api}/zones/{zone_id}/history?minutes={minutes}"
    try:
        body = fetch(url)
    except urllib.error.HTTPError as error:  # one city must not stop the table
        row["error"] = f"HTTP {error.code}"
        return row
    except Exception as error:  # noqa: BLE001 - report, never raise
        row["error"] = type(error).__name__
        return row

    try:
        since_epoch = int(body.get("since_epoch") or 0)
        # The server's window is exactly [since_epoch, since_epoch + minutes], so the moment the
        # response describes is the end of its own window: no local clock, no clock skew.
        window_end = since_epoch + int(body.get("minutes") or minutes) * 60
        summary = temporal.analyse(body.get("points") or [], now_epoch=window_end)
    except Exception as error:  # noqa: BLE001 - a broken payload must not stop the table
        row["error"] = type(error).__name__
        return row

    row.update(
        {
            "samples": summary.count,
            "status": summary.status,
            "reason": summary.reason,
            "duration_seconds": summary.duration_seconds,
            "median_interval": summary.median_interval,
            "largest_gap": summary.largest_gap,
            "gap_count": summary.gap_count,
            "current_risk": summary.last_score,
            "current_risk_class": summary.classes[-1] if summary.classes else None,
            "min_risk": summary.min,
            "max_risk": summary.peak,
            "net_change": summary.net_change,
            "trend": summary.trend,
            "slope_per_hour": summary.slope_per_hour,
            "current_rainfall": summary.newest_rainfall,
            "max_rainfall": summary.max_rainfall,
            "invalid_samples": summary.invalid_dropped,
            "duplicates_removed": summary.duplicates_removed,
            "out_of_order_pairs": summary.out_of_order_pairs,
            "first_score": summary.first_score,
            "first_ts": summary.first_ts,
            "last_ts": summary.last_ts,
            "newest_age_seconds": summary.newest_age_seconds,
            "window_end_epoch": window_end,
            "point_count": body.get("point_count"),
            "rows_returned": len(body.get("points") or []),
            "downsampled": body.get("downsampled"),
            "stride": body.get("stride"),
            "data_classification": body.get("data_classification"),
            "zone_mode": body.get("zone_mode"),
            "model": body.get("model"),
        }
    )
    return row


def cell(row: dict, key: str, decimals: int | None) -> str:
    """One table cell: the value, or ``-`` when the window cannot produce it."""
    value = row.get(key)
    if value is None:
        return PLACEHOLDER
    if decimals is None:
        return str(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return PLACEHOLDER
    return f"{value:.{decimals}f}"


def active_zones(api: str) -> list[dict]:
    """/zones, filtered to the live-weather fleet when the API marks one."""
    zones = fetch(f"{api}/zones").get("zones") or []
    live = [zone for zone in zones if zone.get("zone_mode") == "live_weather"]
    return live or zones


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retrospective trend analysis of stored history.")
    parser.add_argument("--api", default=DEFAULT_API, help="API base URL")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES, help="history window to request")
    parser.add_argument("--json", dest="json_path", help="write the same numbers to this JSON file")
    parser.add_argument("--quiet", action="store_true", help="JSON only")
    args = parser.parse_args(argv)

    api = args.api.rstrip("/")
    rows = [city_row(api, zone, args.minutes) for zone in active_zones(api)]

    payload = {
        "api": api,
        "minutes": args.minutes,
        "city_count": len(rows),
        "cities": rows,
    }
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=False)
            handle.write("\n")

    if args.quiet:
        return 0

    widths = [max(COLUMN_WIDTH, len(heading)) for _key, heading, _decimals in COLUMNS]
    headings = " ".join(
        f"{heading:>{width}}" for (_key, heading, _decimals), width in zip(COLUMNS, widths)
    )
    print(f"history analysis: {len(rows)} cities, window {args.minutes} min, api {api}")
    print(f"{'city':<18}{headings}")
    print("-" * (18 + len(headings)))
    for row in rows:
        label = row["name"][:17]
        if row.get("error"):
            print(f"{label:<18}{row['error']}")
            continue
        cells = " ".join(
            f"{cell(row, key, decimals):>{width}}"
            for (key, _heading, decimals), width in zip(COLUMNS, widths)
        )
        print(f"{label:<18}{cells}")
    usable = [row for row in rows if not row.get("error")]
    if usable:
        rising = sum(1 for row in usable if row["trend"] == temporal.TREND_RISING)
        falling = sum(1 for row in usable if row["trend"] == temporal.TREND_FALLING)
        steady = sum(1 for row in usable if row["trend"] == temporal.TREND_STEADY)
        unknown = len(usable) - rising - falling - steady
        print("-" * 88)
        print(
            f"trend: {rising} rising, {steady} steady, {falling} falling, {unknown} unknown "
            f"(fewer than {temporal.TEMPORAL_MIN_SAMPLES} valid samples or under "
            f"{temporal.TEMPORAL_MIN_DURATION_SECONDS} s); "
            f"gap rule max(3 x median, {temporal.TEMPORAL_GAP_FLOOR_SECONDS} s)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
