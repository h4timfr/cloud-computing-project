"""Run the simulated sensor fleet against the **live** AWS ingestion endpoint (M2).

    python tools/live_simulator.py --url https://<api-id>.execute-api.eu-north-1.amazonaws.com
    python tools/live_simulator.py --interval 1.0 --steps 60 --stop-at CRITICAL

The shared secret is read from ``FLOODPULSE_INGEST_KEY`` or from a gitignored local file
(``.floodpulse-key``, see ``tools/init_ingest_key.py``). It is never printed, logged or
written to the repository.

Readings advance in step with the wall clock (``interval_seconds`` matches the real
cadence), so the server-side ±300 s clock-skew check always passes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from floodpulse.config import ZONE_IDS, ZONE_DEFINITIONS  # noqa: E402
from floodpulse.simulator import SensorSimulator  # noqa: E402

KEY_ENV = "FLOODPULSE_INGEST_KEY"
KEY_HEADER = "x-fp-key"
DEFAULT_KEY_FILE = ".floodpulse-key"


def resolve_key(key_file: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    from_env = os.environ.get(KEY_ENV, "").strip()
    if from_env:
        return from_env
    if key_file.exists():
        value = key_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise SystemExit(
        f"no ingestion secret found. Set {KEY_ENV} or create {key_file} with\n"
        "  python tools/init_ingest_key.py"
    )


def post_reading(url: str, key: str, payload: dict, timeout: float) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url.rstrip("/") + "/readings", data=body, method="POST")
    request.add_header("content-type", "application/json")
    request.add_header(KEY_HEADER, key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": "non_json_response", "detail": raw[:200]}
    except urllib.error.URLError as exc:
        return 0, {"error": "connection_failed", "detail": str(exc.reason)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Drive the deployed FloodPulse API with simulated readings")
    parser.add_argument("--url", required=True, help="API base URL, e.g. https://xxx.execute-api.eu-north-1.amazonaws.com")
    parser.add_argument("--steps", type=int, default=100, help="readings per zone (default: the full 100-step arc)")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between readings (also the simulated cadence)")
    parser.add_argument("--seed", type=int, default=20260214)
    parser.add_argument("--zones", default=",".join(ZONE_IDS))
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    parser.add_argument("--key", default=None, help="supply the secret directly (prefer the file or the env var)")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--stop-at", choices=["NORMAL", "WATCH", "WARNING", "CRITICAL"], default=None,
                        help="stop as soon as any zone reaches this class")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    key = resolve_key(Path(args.key_file), args.key)
    zones = [zone.strip() for zone in args.zones.split(",") if zone.strip()]
    unknown = [zone for zone in zones if zone not in ZONE_IDS]
    if unknown:
        raise SystemExit(f"unknown zone ids: {unknown} (known: {list(ZONE_IDS)})")

    names = {zone["zone_id"]: zone["name"] for zone in ZONE_DEFINITIONS}
    start_epoch = int(time.time())
    simulators = {
        zone: SensorSimulator(zone, seed=args.seed, start_epoch=start_epoch, interval_seconds=args.interval)
        for zone in zones
    }

    accepted = 0
    rejected = 0
    failures = 0
    peak: dict[str, tuple[float, str]] = {zone: (0.0, "NORMAL") for zone in zones}
    reached: str | None = None
    first_error: dict | None = None
    started = time.time()

    print(f"driving {len(zones)} zone(s) against {args.url}/readings")
    print(f"cadence {args.interval}s · up to {args.steps} readings per zone · seed {args.seed}")

    for index in range(args.steps):
        line = []
        for zone in zones:
            reading = simulators[zone].step()
            status, payload = post_reading(args.url, key, reading, args.timeout)
            if status == 202:
                accepted += 1
                risk = payload.get("risk", {})
                score = float(risk.get("risk_score", 0.0))
                effective = payload.get("hysteresis", {}).get("risk_class") or risk.get("risk_class")
                candidate = risk.get("risk_class_candidate")
                if score >= peak[zone][0]:
                    peak[zone] = (score, str(effective))
                line.append(f"{zone.split('-')[-1]}={score:.3f}/{effective}" + (f"({candidate})" if candidate != effective else ""))
                if args.stop_at and str(effective) == args.stop_at and reached is None:
                    reached = zone
            else:
                failures += 1
                if status == 0:
                    failures += 0
                if status in (400, 401, 403, 404, 409, 422, 500, 503):
                    rejected += 1
                if first_error is None:
                    first_error = {"status": status, "payload": payload, "zone": zone, "step": index}
                line.append(f"{zone.split('-')[-1]}=HTTP {status}")
        if not args.quiet:
            phase = simulators[zones[0]].phase_at(index)
            print(f"[{index + 1:>3}/{args.steps}] {phase:<12} " + "  ".join(line))
        if reached:
            print(f"stop-at reached: {reached} reported {args.stop_at}")
            break
        if index + 1 < args.steps:
            time.sleep(max(0.0, args.interval))

    elapsed = time.time() - started
    print("\nsummary")
    print(f"  accepted            : {accepted}")
    print(f"  rejected (HTTP 4xx/5xx): {rejected}")
    print(f"  failed requests     : {failures}")
    print(f"  elapsed             : {elapsed:.1f}s")
    for zone in zones:
        print(f"  peak {names.get(zone, zone):<18}: {peak[zone][0]:.4f} {peak[zone][1]}")
    if first_error:
        print(f"  first error    : HTTP {first_error['status']} on {first_error['zone']} "
              f"(step {first_error['step'] + 1}) {json.dumps(first_error['payload'])[:200]}")
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
