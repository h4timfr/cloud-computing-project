"""Local demo: run the deterministic simulator through the handler in-process.

Usage::

    python tools/local_demo.py --steps 40
    python tools/local_demo.py --steps 100 --json

Prints a risk timeline and a final ``GET /zones`` summary. Everything happens
locally: **no AWS calls, no credentials, no network**.

All data shown is SIMULATED and the risk score is a heuristic (heuristic-v1),
not a validated prediction model.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from floodpulse.config import SIMULATION_DISCLAIMER, ZONE_DEFINITIONS  # noqa: E402
from floodpulse.handler import dispatch, set_store  # noqa: E402
from floodpulse.simulator import DEFAULT_SEED, SensorSimulator, scenario_summary  # noqa: E402
from floodpulse.storage import LocalStore  # noqa: E402

COLUMNS = (
    ("step", 4),
    ("zone", 22),
    ("phase", 11),
    ("rain", 6),
    ("water", 6),
    ("soil", 6),
    ("drain", 6),
    ("flow", 6),
    ("risk", 6),
    ("class", 8),
    ("effective", 9),
    ("transition", 9),
)


def _epoch_of(iso_timestamp: str) -> int:
    return int(datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00")).timestamp())


def _header() -> str:
    return " ".join(f"{name:>{width}}" for name, width in COLUMNS)


def _row(step: int, body: dict, reading: dict, phase: str) -> str:
    metrics = reading["readings"]
    values = {
        "step": step,
        "zone": body["zone_id"].replace("zone-", ""),
        "phase": phase,
        "rain": f"{metrics['rainfall_mm_per_hr']:.1f}",
        "water": f"{metrics['water_level_m']:.1f}",
        "soil": f"{metrics['soil_saturation_pct']:.0f}",
        "drain": f"{metrics['drain_level_m']:.1f}",
        "flow": f"{metrics['flow_rate_m3_s']:.1f}",
        "risk": f"{body['risk']['risk_score']:.3f}",
        "class": body["risk"]["risk_class_candidate"],
        "effective": body["risk"]["risk_class"],
        "transition": body["hysteresis"]["transition"],
    }
    return " ".join(f"{values[name]:>{width}}" for name, width in COLUMNS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FloodPulse local simulator demo (no AWS).")
    parser.add_argument("--steps", type=int, default=40, help="simulated readings per zone")
    parser.add_argument("--seed", default=DEFAULT_SEED, help="deterministic seed")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between readings")
    parser.add_argument("--zones", nargs="*", default=None, help="subset of zone ids")
    parser.add_argument("--json", action="store_true", help="print the final GET /zones payload")
    parser.add_argument("--quiet", action="store_true", help="only print the final summary")
    args = parser.parse_args(argv)

    summary = scenario_summary()
    store = LocalStore()
    set_store(store)

    simulators = [
        SensorSimulator(zone_id, seed=args.seed, interval_seconds=args.interval)
        for zone_id in (args.zones or [str(zone["zone_id"]) for zone in ZONE_DEFINITIONS])
    ]

    print("FloodPulse local demo (M0, in-process, NO AWS calls)")
    print(SIMULATION_DISCLAIMER)
    print(f"seed={args.seed} steps={args.steps} interval={args.interval}s zones={len(simulators)}")
    phases = " -> ".join(f"{phase['name']}({phase['steps']})" for phase in summary["phases"])
    print(f"scenario: {phases}")
    print()

    if not args.quiet:
        print(_header())
        print("-" * len(_header()))

    peak: dict[str, tuple[float, str]] = {}
    for index in range(args.steps):
        for simulator in simulators:
            payload = simulator.step()
            status, body = dispatch(
                "POST", "/readings", json.dumps(payload), store, _epoch_of(payload["timestamp"])
            )
            if status != 202:
                print(f"ERROR step={index} zone={simulator.zone_id} status={status} body={body}", file=sys.stderr)
                return 1
            zone_id = body["zone_id"]
            score = float(body["risk"]["risk_score"])
            if zone_id not in peak or score > peak[zone_id][0]:
                peak[zone_id] = (score, body["risk"]["risk_class_candidate"])
            if not args.quiet:
                print(_row(index, body, payload, str(payload["meta"]["phase"])))

    end_epoch = simulators[0].reading_epoch(args.steps - 1)
    _status, zones = dispatch("GET", "/zones", None, store, end_epoch)

    print()
    print("Final summary")
    print(f"  highest_risk_zone : {zones['highest_risk_zone']['zone_id']} "
          f"({zones['highest_risk_zone']['risk_class']} {zones['highest_risk_zone']['risk_score']:.3f})")
    for view in zones["zones"]:
        latest = view["latest"]
        if latest is None:
            print(f"  {view['zone_id']:<24} no_data")
            continue
        top = ", ".join(latest["top_drivers"])
        print(
            f"  {view['zone_id']:<24} {latest['risk_class']:<9} "
            f"score={latest['risk_score']:.3f} peak={peak[view['zone_id']][0]:.3f} "
            f"records={store.history_count(view['zone_id'])} drivers=[{top}]"
        )
    print()
    print(f"  history records stored : {store.item_count() - len(store.all_latest())}")
    print(f"  deferred (not built)   : {', '.join(zones['deferred_features'])}")

    if args.json:
        print()
        print(json.dumps(zones, indent=2))

    set_store(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
