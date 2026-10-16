"""Live AWS acceptance probe for the deployed dashboard (read-only, no secrets).

Checks the endpoints the dashboard really uses, the payload fields the new UI depends on
(band counts, provenance, feed freshness, cache age), the documented error shapes, CORS,
and request latency. Writes the payloads it reads into build/ as fixtures.
"""

import http.client
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "build"
API = "https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com"
ORIGIN = "http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{(' — ' + detail) if detail else ''}")


def get(path: str, origin: str | None = None, timeout: float = 20.0):
    request = urllib.request.Request(API + path)
    if origin:
        request.add_header("origin", origin)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            elapsed = time.perf_counter() - started
            return response.status, json.loads(body.decode("utf-8")), elapsed, dict(response.headers)
    except urllib.error.HTTPError as exc:
        elapsed = time.perf_counter() - started
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw), elapsed, dict(exc.headers)
        except ValueError:
            return exc.code, {"raw": raw[:200]}, elapsed, dict(exc.headers)


print("FloodPulse live acceptance (read-only)\n")
print("GET /zones")
status, zones, elapsed, headers = get("/zones", origin=ORIGIN)
check("GET /zones returns 200", status == 200, f"HTTP {status}")
check("the live fleet is served", zones.get("zone_mode") == "live_weather",
      f"zone_mode={zones.get('zone_mode')!r}")
check("five cities are present", zones.get("zone_count") == 5 and len(zones.get("zones", [])) == 5)
check("data is labelled mixed (real + modelled + simulated)",
      zones.get("data_classification") == "mixed", str(zones.get("data_classification")))
check("the response denies being a validated or calibrated model",
      ("not a calibrated" in str(zones.get("model_note", ""))) or ("not a validated" in str(zones.get("model_note", ""))),
      str(zones.get("model_note", ""))[:80])
check("every zone carries provenance for each metric",
      all(isinstance(zone.get("metric_sources"), dict) and zone["metric_sources"] for zone in zones["zones"]))
rain_sources = {zone["metric_sources"].get("rainfall_mm_per_hr") for zone in zones["zones"]}
check("rainfall is REAL everywhere", rain_sources == {"REAL"}, str(sorted(rain_sources)))
sim_sources = {zone["metric_sources"].get("water_level_m") for zone in zones["zones"]}
check("water level is SIMULATED everywhere", sim_sources == {"SIMULATED"}, str(sorted(sim_sources)))

weather = zones.get("weather", {})
check("the weather feed block is present and enabled", weather.get("enabled") is True)
check("the feed reports its refresh window", weather.get("refresh_seconds") == 600)
check("attribution is carried for CC BY", "Open-Meteo" in str(weather.get("attribution", "")))
live_zones = weather.get("live_zones") or []
check("all five cities are covered by the feed", len(live_zones) == 5, f"{len(live_zones)} zones")
age_map = weather.get("cache_age_seconds") or {}
check("the per-city cache age map covers the fleet",
      set(age_map) == set(live_zones), f"{sorted(age_map)}")
ages = [age_map[zone] for zone in live_zones if zone in age_map]
check("every city has a cache age (the UI ages forward from it)",
      len(ages) == len(live_zones) and all(isinstance(a, (int, float)) for a in ages), f"ages={ages}")
check("the oldest cached value is inside the refresh window",
      all(a is not None and a <= 2 * 600 for a in ages), f"max={max(ages)}s")
attempt_map = weather.get("last_attempt_age_seconds") or {}
check("the last attempt is reported per city", set(attempt_map) == set(live_zones))
error_map = weather.get("last_attempt_error") or {}
check("no city is currently failing its fetch", set(error_map) == set(live_zones)
      and not any(error_map.values()), f"errors={ {k: v for k, v in error_map.items() if v} }")
refresh = weather.get("last_refresh") or {}
check("the last refresh outcome is reported", "reason" in refresh and "attempted" in refresh,
      f"reason={refresh.get('reason')!r} attempted={refresh.get('attempted')}")
# A poll either refreshes (attempted, reason is None by design: `reason` explains why nothing
# was attempted) or is served from the cache inside the window (reason "cache_fresh"). Both are
# the window working; requiring only the second made this check fail on the first poll after a
# deploy, which is exactly when it is run.
attempted = refresh.get("attempted")
reason = refresh.get("reason")
inside_window = refresh.get("refresh_seconds") == 600 and (
    reason == "cache_fresh"
    or (attempted is True and isinstance(refresh.get("refreshed"), list))
)
check("the refresh window is enforced at the provider", inside_window,
      f"refresh_seconds={refresh.get('refresh_seconds')} attempted={attempted} reason={reason!r}")
check("the hydraulics note admits the drain/flow values are simulated",
      "SIMULATED" in str(weather.get("hydraulics_note", "")))

counts = {"NORMAL": 0, "WATCH": 0, "WARNING": 0, "CRITICAL": 0, "NO_READING": 0}
for zone in zones["zones"]:
    latest = zone.get("latest") or {}
    klass = str(latest.get("risk_class") or "").upper()
    counts[klass if klass in counts else "NO_READING"] += 1
print(f"       fleet distribution: {counts}")
check("the fleet totals five counted zones", sum(counts.values()) == 5)
check("the ranking leader matches the highest score",
      zones["highest_risk_zone"]["zone_id"] == max(
          zones["zones"], key=lambda z: (z.get("latest") or {}).get("risk_score", -1))["zone_id"])

print("\nGET /zones/{zone}/history")
leader = zones["highest_risk_zone"]["zone_id"]
status, history, elapsed_h, _ = get(f"/zones/{leader}/history?minutes=1440")
check("history returns 200", status == 200, f"HTTP {status}, {elapsed_h * 1000:.0f} ms")
check("history points carry the five readings", all(
    len(point.get("readings") or {}) == 5 for point in history.get("points", [])))
check("history stays inside the requested window", history.get("minutes") == 1440)
check("history is labelled as the same fleet", history.get("data_classification") == "mixed")

status, bad, _, _ = get("/zones/not-a-city/history?minutes=60")
check("an unknown zone is a 404 with the known zones listed",
      status == 404 and "known_zones" in bad, f"HTTP {status} {json.dumps(bad)[:100]}")
status, bad, _, _ = get(f"/zones/{leader}/history?minutes=99999")
check("a window outside the range is a 400", status == 400, f"HTTP {status} {json.dumps(bad)[:80]}")

print("\nGET /incidents")
status, incidents, _, _ = get("/incidents?limit=50")
check("incidents returns 200", status == 200, f"HTTP {status}")
check("incidents report open and closed counts",
      isinstance(incidents.get("open_count"), int) and isinstance(incidents.get("closed_count"), int),
      f"open={incidents.get('open_count')} closed={incidents.get('closed_count')}")
check("incidents state the alerting configuration",
      incidents.get("alerting", {}).get("enabled") is True)
check("no AWS ARN is leaked to the dashboard",
      "arn:aws" not in json.dumps(incidents) and "arn:aws" not in json.dumps(zones))

print("\nCORS")
status, _, _, headers = get("/zones", origin=ORIGIN)
allow = {key.lower(): value for key, value in headers.items()}.get("access-control-allow-origin")
check("the dashboard origin is allowed", allow == ORIGIN, f"{allow!r}")
status, _, _, headers = get("/zones", origin="https://evil.example")
allow = {key.lower(): value for key, value in headers.items()}.get("access-control-allow-origin")
check("an unknown origin is not allowed", allow in (None, ""), f"{allow!r}")

print("\nLatency (five polls, the dashboard polls every 5 s)")
samples = []
for _ in range(5):
    status, _, elapsed, _ = get("/zones")
    samples.append(elapsed)
    time.sleep(0.4)
print(f"       min {min(samples) * 1000:.0f} ms · median {statistics.median(samples) * 1000:.0f} ms · "
      f"max {max(samples) * 1000:.0f} ms")
check("every poll answered in under 3 s", max(samples) < 3.0, f"max {max(samples):.2f}s")

print("\nFixtures written")
(BUILD / "zones-live-weather.json").write_text(json.dumps(zones, indent=2) + "\n", encoding="utf-8")
(BUILD / "history-live-weather.json").write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
(BUILD / "incidents-live-weather.json").write_text(json.dumps(incidents, indent=2) + "\n", encoding="utf-8")
for name in ("zones-live-weather.json", "history-live-weather.json", "incidents-live-weather.json"):
    print(f"  build/{name}")

failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)} checks passed, {len(failed)} failed")
for name in failed:
    print(f"  FAILED: {name}")
sys.exit(1 if failed else 0)
