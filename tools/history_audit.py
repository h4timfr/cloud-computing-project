"""History audit: what the stored history for each active city actually contains.

Read-only. It never writes to DynamoDB; it reads the public history endpoint and measures what came back.

    python tools/history_audit.py                      # live API, default window, terminal table
    python tools/history_audit.py --minutes 1440       # ask for a day
    python tools/history_audit.py --json out.json      # machine-readable copy of the same numbers
    python tools/history_audit.py --api http://127.0.0.1:8199

Reported per city: item count, oldest/newest timestamp, total duration, median/min/max interval,
duplicate timestamps, out-of-order pairs, missing risk score, how many rows are missing each
individual reading, whether the newest sample is stale, how many bytes the response took, and how
many distinct weather observation timestamps appear (0 while the payload carries none, which it
currently does).

The readings of a history point live under ``point["readings"]`` - ``rainfall_mm_per_hr``,
``soil_saturation_pct``, ``drain_level_m``, ``water_level_m``, ``flow_rate_m3_s``. A flattened point
is tolerated so an older payload shape audits as "present" rather than "missing".
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

DEFAULT_API = "https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com"
METRICS = ("rainfall_mm_per_hr", "soil_saturation_pct", "drain_level_m", "water_level_m", "flow_rate_m3_s")
RAINFALL = "rainfall_mm_per_hr"


def fetch(url: str, timeout: float = 25.0) -> tuple[dict, int]:
    """The decoded body, and how many bytes the endpoint actually delivered."""
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed https endpoint
        raw = response.read()
    return json.loads(raw), len(raw)


def readings_of(point: dict) -> dict:
    """The metric readings of one history point.

    BUG CLASS (M16): the missing-rainfall and missing-metric counters read the metric names at the
    *top level* of each point, where they never appear, so every row was reported as missing every
    reading - a false alarm that made the audit useless as evidence. The payload nests them under
    ``readings``; the point itself is only used as a fallback for an older flattened shape.
    """
    nested = point.get("readings")
    if isinstance(nested, dict):
        return nested
    return point


def parse_stamp(point: dict) -> float | None:
    """Accept the timestamp spellings the endpoint has used, and refuse anything else rather than guess."""
    for key in ("epoch", "timestamp_epoch", "ts_epoch"):
        value = point.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    for key in ("timestamp", "time", "observed_at"):
        value = point.get(key)
        if isinstance(value, str):
            text = value.replace("Z", "+00:00")
            try:
                import datetime

                return datetime.datetime.fromisoformat(text).timestamp()
            except ValueError:
                continue
    return None


def observation_stamp(point: dict) -> str | None:
    """The provider observation time, if the row carries one. Repeated values mean cached weather."""
    for key in ("weather_observed_at", "observation_time", "observed_at", "weather_observation", "observation_epoch"):
        value = point.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return None


def audit_city(api: str, zone: dict, minutes: int) -> dict:
    zone_id = zone.get("zone_id") or zone.get("id")
    url = f"{api}/zones/{zone_id}/history?minutes={minutes}"
    started = time.time()
    try:
        body, payload_bytes = fetch(url)
    except urllib.error.HTTPError as error:  # keep going: one city must not stop the audit
        return {"zone_id": zone_id, "name": zone.get("name"), "error": f"HTTP {error.code}", "url": url}
    except Exception as error:  # noqa: BLE001 - report, never raise, so the table always prints
        return {"zone_id": zone_id, "name": zone.get("name"), "error": type(error).__name__, "url": url}
    elapsed_ms = round((time.time() - started) * 1000)

    points = body.get("points") or []
    stamps: list[float] = []
    unparsed = 0
    missing_score = 0
    missing_rainfall = 0
    missing_metrics = {name: 0 for name in METRICS}
    scores: list[float] = []
    observations: list[str] = []
    keys_seen: set[str] = set()

    for point in points:
        if not isinstance(point, dict):
            unparsed += 1
            continue
        keys_seen.update(point.keys())
        stamp = parse_stamp(point)
        if stamp is None:
            unparsed += 1
        else:
            stamps.append(stamp)
        score = point.get("risk_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            scores.append(float(score))
        else:
            missing_score += 1
        rainfall = readings_of(point).get(RAINFALL)
        if not isinstance(rainfall, (int, float)) or isinstance(rainfall, bool):
            missing_rainfall += 1
        readings = readings_of(point)
        for name in METRICS:
            if not isinstance(readings.get(name), (int, float)) or isinstance(readings.get(name), bool):
                missing_metrics[name] += 1
        observed = observation_stamp(point)
        if observed:
            observations.append(observed)

    intervals = [round(b - a) for a, b in zip(stamps, stamps[1:])]
    duplicates = len(stamps) - len(set(stamps))
    out_of_order = sum(1 for value in intervals if value < 0)
    ascending = sorted(stamps)
    duration = round(ascending[-1] - ascending[0]) if len(ascending) > 1 else 0
    positive = [value for value in intervals if value > 0]
    now = time.time()
    repeated_observations = 0
    for previous, current in zip(observations, observations[1:]):
        if previous == current:
            repeated_observations += 1

    return {
        "zone_id": body.get("zone_id", zone_id),
        "name": body.get("name", zone.get("name")),
        "point_count": body.get("point_count", len(points)),
        "rows_returned": len(points),
        "downsampled": body.get("downsampled"),
        "stride": body.get("stride"),
        "minutes_requested": body.get("minutes", minutes),
        "payload_bytes": payload_bytes,
        "elapsed_ms": elapsed_ms,
        "oldest": min(ascending) if ascending else None,
        "newest": max(ascending) if ascending else None,
        "duration_seconds": duration,
        "median_interval": statistics.median(positive) if positive else None,
        "min_interval": min(positive) if positive else None,
        "max_interval": max(positive) if positive else None,
        "duplicate_timestamps": duplicates,
        "out_of_order": out_of_order,
        "unparsed_rows": unparsed,
        "missing_risk_score": missing_score,
        "missing_rainfall": missing_rainfall,
        "missing_metrics": missing_metrics,
        "score_min": min(scores) if scores else None,
        "score_max": max(scores) if scores else None,
        "score_last": scores[-1] if scores else None,
        "newest_age_seconds": round(now - max(ascending)) if ascending else None,
        "distinct_observations": len(set(observations)),
        "repeated_observations": repeated_observations,
        "rows_with_observation": len(observations),
        "point_keys": sorted(keys_seen),
        "metric_sources": body.get("metric_sources"),
        "data_classification": body.get("data_classification"),
        "zone_mode": body.get("zone_mode"),
    }


def human_duration(seconds: float | None) -> str:
    if not seconds:
        return "-"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit stored FloodPulse history for every active city.")
    parser.add_argument("--api", default=DEFAULT_API, help="API base URL")
    parser.add_argument("--minutes", type=int, default=60, help="history window to request")
    parser.add_argument("--json", dest="json_path", help="write the same numbers to this JSON file")
    parser.add_argument("--quiet", action="store_true", help="JSON only")
    args = parser.parse_args(argv)

    api = args.api.rstrip("/")
    zones_body, _bytes = fetch(f"{api}/zones")
    zones = zones_body.get("zones") or []
    active = [zone for zone in zones if zone.get("zone_mode") == "live_weather"] or zones

    rows = [audit_city(api, zone, args.minutes) for zone in active]
    rows = [row for row in rows if row]

    payload = {
        "generated_at_epoch": time.time(),
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

    header = f"{'city':<16}{'rows':>5}{'dur':>9}{'med':>7}{'gap':>7}{'dup':>5}{'ooo':>5}{'nos':>5}{'ms':>8}{'obs':>6}{'rep':>5}{'age':>7}"
    print(f"history audit: {len(rows)} cities, window {args.minutes} min, api {api}")
    print(header)
    print("-" * len(header))
    for row in rows:
        if row.get("error"):
            print(f"{str(row.get('name') or row['zone_id']):<16}{'ERROR':>5}  {row['error']}")
            continue
        print(
            f"{str(row['name'])[:15]:<16}{row['rows_returned']:>5}"
            f"{human_duration(row['duration_seconds']):>9}"
            f"{str(row['median_interval'] or '-'):>7}{str(row['max_interval'] or '-'):>7}"
            f"{row['duplicate_timestamps']:>5}{row['out_of_order']:>5}{row['missing_risk_score']:>5}"
            f"{row['missing_rainfall']:>8}{row['distinct_observations']:>6}{row['repeated_observations']:>5}"
            f"{human_duration(row['newest_age_seconds']):>7}"
        )
    depths = [row["duration_seconds"] for row in rows if not row.get("error")]
    counts = [row["rows_returned"] for row in rows if not row.get("error")]
    if depths:
        print("-" * len(header))
        print(
            f"depth: min {human_duration(min(depths))}, median {human_duration(statistics.median(depths))}, "
            f"max {human_duration(max(depths))}; rows: min {min(counts)}, max {max(counts)}"
        )
        for window in (3600, 21600, 86400):
            enough = sum(1 for value in depths if value >= window)
            print(f"  cities with >= {human_duration(window)} of history: {enough}/{len(depths)}")
    if rows and rows[0].get("point_keys"):
        print("point keys:", ", ".join(rows[0]["point_keys"]))
    served = [row for row in rows if not row.get("error")]
    if served:
        reading_totals = {
            name: sum(row["missing_metrics"][name] for row in served) for name in METRICS
        }
        rows_total = sum(row["rows_returned"] for row in served)
        print(f"readings missing across {rows_total} rows: " + ", ".join(
            f"{name}={reading_totals[name]}" for name in METRICS
        ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
