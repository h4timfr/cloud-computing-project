"""One-command storm demo: the whole NORMAL -> WATCH -> WARNING -> CRITICAL -> recovery
-> closure arc for the simulated fleet, in process, with no AWS calls and no network.

    python tools/storm_demo.py                      # storyboard + verification
    python tools/storm_demo.py --export build/story # also write dashboard payloads
    python tools/storm_demo.py --json               # machine-readable story

Why this exists: a demo that only usually works is not a demo. This runs the real handler
(validation, risk scoring, hysteresis, incidents) against the deterministic simulator and
*verifies* that every stage of the story actually happened, exiting non-zero if one did
not. ``--export`` writes the exact ``GET /zones``, ``GET /incidents`` and history payloads
for each stage, so the dashboard can be checked against the same story the talk describes.

Everything here is SIMULATED for the "Stockholm" demo fleet; the risk score is the
``heuristic-v1`` heuristic, not a validated hydrological model. Alerts are not published
in a local run (no SNS topic is configured for the demo fleet).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from floodpulse.config import SIMULATION_DISCLAIMER, ZONE_DEFINITIONS  # noqa: E402
from floodpulse.handler import dispatch, set_store  # noqa: E402
from floodpulse.simulator import DEFAULT_INTERVAL_SECONDS, DEFAULT_SEED, SensorSimulator, scenario_summary  # noqa: E402
from floodpulse.storage import LocalStore  # noqa: E402

#: The demo fleet is the simulated one. The live-weather fleet is never mixed in: the
#: handler is given an explicit environment so no ambient setting can change the fleet.
DEMO_ENV: dict[str, str] = {
    "FLOODPULSE_ZONE_MODE": "demo",
    "FLOODPULSE_ALERT_COOLDOWN_SECONDS": "600",
}

STAGES: tuple[str, ...] = ("NORMAL", "WATCH", "WARNING", "CRITICAL", "RECOVERY", "CLOSED")
RANK = {"NORMAL": 0, "WATCH": 1, "WARNING": 2, "CRITICAL": 3}


class StageMissing(Exception):
    """Raised when the arc did not contain a stage the demo depends on."""


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:%M:%S")


def band_counts(payload: dict[str, Any]) -> dict[str, int]:
    """Same rule the dashboard strip uses: count the *effective* class, and never count a
    zone with no reading as NORMAL."""
    counts = {"NORMAL": 0, "WATCH": 0, "WARNING": 0, "CRITICAL": 0, "NO_READING": 0}
    for zone in payload.get("zones", []):
        latest = zone.get("latest")
        klass = str((latest or {}).get("risk_class") or "").upper()
        counts[klass if klass in RANK else "NO_READING"] += 1
    return counts


def run_arc(
    *,
    steps: int = 100,
    seed: int = DEFAULT_SEED,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    start_epoch: int | None = None,
    zone_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Drive the demo fleet through the storm arc and return the recorded story.

    Returns a dict with ``snapshots`` (per step: classes, scores, incident counts and the
    full payload), ``stages`` (the representative snapshot for each stage) and ``peak``.
    """
    definitions = [zone for zone in ZONE_DEFINITIONS if zone_ids is None or zone["zone_id"] in zone_ids]
    zone_list = tuple(str(zone["zone_id"]) for zone in definitions)
    if start_epoch is None:
        start_epoch = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())

    store = LocalStore()
    set_store(store)
    simulators = {
        zone_id: SensorSimulator(zone_id, seed=seed, start_epoch=start_epoch, interval_seconds=interval_seconds)
        for zone_id in zone_list
    }

    snapshots: list[dict[str, Any]] = []
    try:
        for index in range(steps):
            for zone_id in zone_list:
                reading = simulators[zone_id].step()
                epoch = int(datetime.fromisoformat(reading["timestamp"].replace("Z", "+00:00")).timestamp())
                status, body = dispatch(
                    "POST", "/readings", json.dumps(reading), store, epoch, env=DEMO_ENV
                )
                if status != 202:
                    raise SystemExit(f"ingest failed at step {index} for {zone_id}: HTTP {status} {body}")

            # The snapshot the dashboard would poll right after this step: read it back
            # through the same handler route, so the story is what the API really serves.
            epoch = simulators[zone_list[0]].reading_epoch(index)
            status, zones = dispatch("GET", "/zones", None, store, epoch, env=DEMO_ENV)
            if status != 200:
                raise SystemExit(f"GET /zones failed at step {index}: HTTP {status} {zones}")
            _inc_status, incidents = dispatch("GET", "/incidents", None, store, epoch, env=DEMO_ENV)
            episodes = incidents.get("incidents", [])
            snapshots.append(
                {
                    "step": index,
                    "epoch": epoch,
                    "phase": str(simulators[zone_list[0]].phase_at(index)),
                    "classes": {
                        view["zone_id"]: str(((view.get("latest") or {}).get("risk_class") or "NO_READING"))
                        for view in zones.get("zones", [])
                    },
                    "scores": {
                        view["zone_id"]: float(((view.get("latest") or {}).get("risk_score") or 0.0))
                        for view in zones.get("zones", [])
                    },
                    "counts": band_counts(zones),
                    "open_incidents": int(incidents.get("open_count", 0)),
                    "closed_incidents": int(incidents.get("closed_count", 0)),
                    "incident_ids_open": sorted(
                        str(item.get("incident_id")) for item in episodes if item.get("status") == "OPEN"
                    ),
                    "payload": zones,
                    "incidents": incidents,
                }
            )
    finally:
        set_store(None)

    peak = max(snapshots, key=lambda snap: max(snap["scores"].values()) if snap["scores"] else 0.0)
    stages = _locate_stages(snapshots, peak)
    return {
        "zone_ids": list(zone_list),
        "zone_names": {str(zone["zone_id"]): str(zone["name"]) for zone in definitions},
        "steps": steps,
        "seed": seed,
        "interval_seconds": interval_seconds,
        "start_epoch": start_epoch,
        "snapshots": snapshots,
        "stages": stages,
        "peak": peak,
        # kept so --export can read history out of the same run instead of an empty store
        "store": store,
    }


def _first(snapshots: list[dict[str, Any]], predicate) -> dict[str, Any] | None:
    for snapshot in snapshots:
        if predicate(snapshot):
            return snapshot
    return None


def _locate_stages(snapshots: list[dict[str, Any]], peak: dict[str, Any]) -> dict[str, Any]:
    """Find the step that represents each stage of the demo story."""
    fleet_max = lambda snap: max((RANK.get(name, -1) for name in snap["classes"].values()), default=-1)  # noqa: E731

    stages: dict[str, Any] = {}
    for name in ("NORMAL", "WATCH", "WARNING", "CRITICAL"):
        stages[name] = _first(snapshots, lambda snap, want=RANK[name]: fleet_max(snap) >= want)

    after_peak = [snap for snap in snapshots if snap["step"] > peak["step"]]
    stages["RECOVERY"] = _first(
        after_peak, lambda snap: fleet_max(snap) <= RANK["WATCH"] and snap["closed_incidents"] >= 1
    )
    if stages["RECOVERY"] is None:
        stages["RECOVERY"] = _first(after_peak, lambda snap: fleet_max(snap) <= RANK["WATCH"])
    stages["CLOSED"] = _first(
        snapshots, lambda snap: snap["open_incidents"] == 0 and snap["closed_incidents"] >= 1
    )
    return stages


def verify_arc(story: dict[str, Any]) -> list[str]:
    """Return the list of ways this arc is not a demo-ready story (empty means ready)."""
    problems: list[str] = []
    stages = story["stages"]
    for stage in STAGES:
        if stages.get(stage) is None:
            problems.append(f"stage {stage} never occurred in {story['steps']} steps")

    peak = story["peak"]
    if peak["scores"] and max(peak["scores"].values()) < 0.80:
        problems.append(f"peak score {max(peak['scores'].values()):.3f} never reached CRITICAL (0.80)")

    critical = stages.get("CRITICAL")
    if critical is not None and critical["open_incidents"] < 1:
        problems.append("CRITICAL was reached but no incident was open, so the alert story is broken")

    recovery = stages.get("RECOVERY")
    if recovery is not None:
        if recovery["step"] <= peak["step"]:
            problems.append("recovery was not after the peak")
        if max(recovery["scores"].values()) > 0.60:
            problems.append("recovery snapshot is still WARNING or worse")

    closed = stages.get("CLOSED")
    if closed is not None and closed["open_incidents"] != 0:
        problems.append("an incident was still open at the closure stage")

    for stage in ("NORMAL", "WATCH", "WARNING", "CRITICAL", "RECOVERY", "CLOSED"):
        first = stages.get(stage)
        if first is None:
            continue
        if story["steps"] and not first["counts"]:
            problems.append(f"{stage} snapshot counted no zones")
    return problems


def _storyboard(story: dict[str, Any]) -> str:
    lines: list[str] = []
    names = story["zone_names"]
    lines.append("stage storyboard (what the dashboard shows at each stage)")
    header = f"  {'stage':<9} {'step':>4} {'clock':>8} {'phase':<11} {'max score':>9}  {'fleet classes':<34} incidents"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for stage in STAGES:
        snapshot = story["stages"].get(stage)
        if snapshot is None:
            lines.append(f"  {stage:<9} {'--':>4} {'--':>8} {'--':<11} {'--':>9}  {'MISSING':<34} --")
            continue
        classes = ", ".join(f"{names.get(zone, zone).split()[0]}={klass}" for zone, klass in sorted(snapshot["classes"].items()))
        top = max(snapshot["scores"].values()) if snapshot["scores"] else 0.0
        lines.append(
            f"  {stage:<9} {snapshot['step']:>4} {_iso(snapshot['epoch']):>8} {snapshot['phase']:<11} "
            f"{top:>9.3f}  {classes:<34} open={snapshot['open_incidents']} closed={snapshot['closed_incidents']}"
        )
    lines.append("")
    lines.append("dashboard strip at each stage (Normal/Watch/Warning/Critical/No reading)")
    for stage in STAGES:
        snapshot = story["stages"].get(stage)
        if snapshot is None:
            continue
        counts = snapshot["counts"]
        lines.append(
            f"  {stage:<9} {counts['NORMAL']:>2} {counts['WATCH']:>2} {counts['WARNING']:>2} "
            f"{counts['CRITICAL']:>2} {counts['NO_READING']:>2}"
        )
    return "\n".join(lines)


def export_story(story: dict[str, Any], directory: Path) -> list[Path]:
    """Write the dashboard payloads for every stage, so the UI can be checked against the
    same story the demo narrates.

    The history window is exported for the zone that *leads the ranking at that stage*,
    which is the zone the dashboard highlights, so the chart and the cards tell one story.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    peak_zone = max(story["peak"]["scores"], key=lambda zone: story["peak"]["scores"][zone])
    stage_rows: list[dict[str, Any]] = []

    for stage in STAGES:
        snapshot = story["stages"].get(stage)
        if snapshot is None:
            continue
        slug = stage.lower()
        for name, payload in (("zones", snapshot["payload"]), ("incidents", snapshot["incidents"])):
            path = directory / f"{name}-{slug}.json"
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            written.append(path)

        leader = str(snapshot["payload"]["highest_risk_zone"]["zone_id"])
        status, history = dispatch(
            "GET", f"/zones/{leader}/history", None, story.get("store"), snapshot["epoch"],
            query={"minutes": "1440"}, env=DEMO_ENV,
        )
        if status != 200:
            raise SystemExit(f"history export failed for {leader} at {stage}: HTTP {status}")
        if not history.get("points"):
            raise SystemExit(f"exported history for {leader} at {stage} is empty")
        if history["zone_id"] != leader:
            raise SystemExit(f"history export returned {history['zone_id']} instead of {leader}")
        path = directory / f"history-{slug}.json"
        path.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
        written.append(path)

        stage_rows.append({
            "stage": stage,
            "step": snapshot["step"],
            "timestamp": datetime.fromtimestamp(snapshot["epoch"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "phase": snapshot["phase"],
            "leader": leader,
            "leader_name": str(snapshot["payload"]["highest_risk_zone"]["name"]),
            "history_points": history["point_count"],
            "counts": snapshot["counts"],
            "open_incidents": snapshot["open_incidents"],
            "closed_incidents": snapshot["closed_incidents"],
        })

    summary = {
        "generated_by": "tools/storm_demo.py",
        "model": "heuristic-v1",
        "data_classification": "simulated",
        "zone_mode": "simulated",
        "disclaimer": SIMULATION_DISCLAIMER,
        "steps": story["steps"],
        "seed": story["seed"],
        "interval_seconds": story["interval_seconds"],
        "fleet": sorted({str(zone) for zone in story["zone_ids"]}),
        "arc_peak_zone": peak_zone,
        "stages": stage_rows,
    }
    path = directory / "story.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FloodPulse storm demo: the full risk arc, no AWS")
    parser.add_argument("--steps", type=int, default=100, help="readings per zone (100 is the full arc)")
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS, dest="interval_seconds")
    parser.add_argument("--export", default=None, help="directory for per-stage dashboard payloads")
    parser.add_argument("--json", action="store_true", help="print the story as JSON")
    parser.add_argument("--quiet", action="store_true", help="only print the verification result")
    args = parser.parse_args(argv)

    story = run_arc(steps=args.steps, seed=args.seed, interval_seconds=args.interval_seconds)
    problems = verify_arc(story)

    if args.json:
        print(json.dumps({
            "steps": story["steps"],
            "seed": story["seed"],
            "stages": {
                stage: (None if story["stages"].get(stage) is None else {
                    "step": story["stages"][stage]["step"],
                    "phase": story["stages"][stage]["phase"],
                    "counts": story["stages"][stage]["counts"],
                    "open_incidents": story["stages"][stage]["open_incidents"],
                    "closed_incidents": story["stages"][stage]["closed_incidents"],
                })
                for stage in STAGES
            },
            "problems": problems,
        }, indent=2))
        return 0 if not problems else 1

    print("FloodPulse storm demo — simulated fleet, in-process, NO AWS calls")
    print(SIMULATION_DISCLAIMER)
    phases = " -> ".join(f"{phase['name']}({phase['steps']})" for phase in scenario_summary()["phases"])
    print(f"seed={story['seed']} steps={story['steps']} interval={story['interval_seconds']}s "
          f"zones={len(story['zone_ids'])}")
    print(f"scenario: {phases}")
    print("alerts: not published in a local run (no SNS topic configured for the demo fleet)")
    print()
    if not args.quiet:
        print(_storyboard(story))
        print()

    if args.export:
        written = export_story(story, Path(args.export))
        print(f"exported {len(written)} payload file(s) to {args.export}")
        for path in written:
            print(f"  {path.as_posix()}")
        print()

    if problems:
        print("demo reliability check: FAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("demo reliability check: PASSED — the full arc was reproduced end to end")
    print("  NORMAL -> WATCH -> WARNING -> CRITICAL -> recovery -> incident closure")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
