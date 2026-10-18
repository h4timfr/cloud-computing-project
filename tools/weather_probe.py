"""Probe the real Open-Meteo weather feed for the FloodPulse live zones (M4).

Read-only diagnostic: it calls the provider once for the twelve coastal cities and prints
exactly what FloodPulse would ingest, including the REAL / MODEL_DERIVED / SIMULATED
provenance of every metric. It stores nothing and needs no AWS credentials.

Usage::

    python tools/weather_probe.py
    python tools/weather_probe.py --as-json build/weather-probe.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from floodpulse.config import (  # noqa: E402
    HYDRAULICS_NOTE,
    LIVE_ZONE_DEFINITIONS,
    METRIC_SOURCES_LIVE,
    SOURCE_DESCRIPTIONS,
    WEATHER_ATTRIBUTION,
    WEATHER_TIMEOUT_SECONDS,
)
from floodpulse.live_weather import build_live_metrics  # noqa: E402
from floodpulse.weather import WeatherClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe the live Open-Meteo feed for the FloodPulse cities")
    parser.add_argument("--timeout", type=float, default=WEATHER_TIMEOUT_SECONDS)
    parser.add_argument("--as-json", default=None, help="also write the raw result to this file")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    now_epoch = datetime.now(timezone.utc).timestamp()
    client = WeatherClient(timeout=args.timeout)
    result = client.fetch(LIVE_ZONE_DEFINITIONS)

    print(f"{WEATHER_ATTRIBUTION}")
    print(f"provider request: 1 (all {len(LIVE_ZONE_DEFINITIONS)} zones)")
    print(f"provider ok: {result.ok}  error: {result.error}")
    if not result.ok:
        print("No usable weather was returned; FloodPulse would serve its cache or report no_data.")
        return 1

    report = {
        "fetched_at": datetime.fromtimestamp(now_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": "open-meteo",
        "ok": result.ok,
        "zones": [],
    }

    for zone in LIVE_ZONE_DEFINITIONS:
        zone_id = str(zone["zone_id"])
        observation = result.observations[zone_id]
        entry = {
            "zone_id": zone_id,
            "name": zone["name"],
            "ok": observation.ok,
            "error": observation.error,
            "observed_at": observation.observed_at,
            "provider_current_time": observation.provider_current_time,
            "rainfall_mm_per_hr": observation.rainfall_mm_per_hr,
            "soil_moisture_m3m3": observation.soil_moisture_m3m3,
            "soil_saturation_pct": observation.soil_saturation_pct,
            "metrics": None,
        }
        if observation.ok:
            metrics = build_live_metrics(observation, previous=None, now_epoch=now_epoch)
            entry["metrics"] = metrics
        report["zones"].append(entry)

        if args.quiet:
            continue
        print("")
        print(f"{zone['name']} ({zone_id})  lat={zone['latitude']} lon={zone['longitude']}")
        if not observation.ok:
            print(f"  FAILED: {observation.error}")
            continue
        print(f"  provider observed at {observation.observed_at} (current sample {observation.provider_current_time})")
        if entry["metrics"]:
            for metric, value in entry["metrics"].items():
                tag = METRIC_SOURCES_LIVE[metric]
                print(f"    {metric:<20} {value:>8}  [{tag}] {SOURCE_DESCRIPTIONS[tag]}")

    if args.as_json:
        with open(args.as_json, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        if not args.quiet:
            print("")
            print(f"wrote {args.as_json}")

    if not args.quiet:
        print("")
        print(HYDRAULICS_NOTE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
