"""Live AWS acceptance probe for the deployed dashboard (no secrets).

Checks the endpoints the dashboard really uses, the payload fields the new UI depends on
(band counts, provenance, feed freshness, cache age), the documented error shapes, CORS,
and request latency. Writes the payloads it reads into build/ as fixtures.

It is no longer *read-only*: the subscription section posts to the public
``POST /subscriptions`` route, which counts one throttling attempt per address in the existing
table (the item carries a digest, a count and a TTL, never the address) and asks SNS to send a
confirmation email. Every address it uses is on a reserved domain with no mailbox, so the mail
goes nowhere and no visitor's allowance is touched.
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


def _finish(started: float, status: int, raw: str, headers) -> tuple:
    elapsed = time.perf_counter() - started
    try:
        return status, json.loads(raw), elapsed, dict(headers)
    except ValueError:
        return status, {"raw": raw[:200]}, elapsed, dict(headers)


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


def post(path: str, payload: dict | None = None, *, raw: str | None = None,
         origin: str | None = None, headers: dict | None = None, timeout: float = 20.0) -> tuple:
    """POST JSON (or a raw string) to the API. Returns the same tuple shape as `get`."""
    body = (raw if raw is not None else json.dumps(payload)).encode("utf-8")
    request = urllib.request.Request(API + path, data=body, method="POST")
    request.add_header("content-type", "application/json")
    if origin:
        request.add_header("origin", origin)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _finish(started, response.status, response.read().decode("utf-8", "replace"),
                           response.headers)
    except urllib.error.HTTPError as exc:
        return _finish(started, exc.code, exc.read().decode("utf-8", "replace"), exc.headers)


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

print("\nPOST /subscriptions (public, anonymous, SNS-confirmed)")
# A reserved domain with no mailbox: SNS will try to deliver the confirmation and fail, and no
# real person is emailed because a test ran. The address carries this run's own timestamp, so a
# re-run starts with a fresh allowance instead of inheriting the previous run's spent budget.
_reserved = f"floodpulse-acceptance+{time.strftime('%Y%m%d%H%M%S', time.gmtime())}@example.com"

status, bad, _, _ = post("/subscriptions", {"email": "not-an-address"})
check("an invalid address is a 400", status == 400 and bad.get("status") == "invalid",
      f"HTTP {status} {json.dumps(bad)[:90]}")
check("the rejection never echoes the address", "not-an-address" not in json.dumps(bad))

status, bad, _, _ = post("/subscriptions", raw=json.dumps(
    {"email": "victim@example.com\r\nBcc: someone@example.com"}))
check("a CRLF in the address is refused", status == 400,
      f"HTTP {status} {json.dumps(bad)[:90]}")

status, bad, _, _ = post("/subscriptions", raw='[{"email": "person@example.com"}]')
check("an array body is refused (the route wants one object)", status == 400,
      f"HTTP {status} {json.dumps(bad)[:90]}")

status, bad, _, _ = post("/subscriptions", {"email": _reserved, "protocol": "sms",
                                            "TopicArn": "arn:aws:sns:eu-north-1:725934295966:other"})
check("a client cannot add a protocol or a topic to the request", status == 400,
      f"HTTP {status} {json.dumps(bad)[:90]}")

status, bad, _, _ = post("/subscriptions", raw=json.dumps({"email": "a" * 600 + "@example.com"}))
check("an oversized body is refused before parsing", status == 400,
      f"HTTP {status} {json.dumps(bad)[:90]}")

status, ok, elapsed, _ = post("/subscriptions", {"email": _reserved})
check("a valid address is a 202 with the documented body",
      status == 202 and ok.get("status") == "confirmation_required", f"HTTP {status} {json.dumps(ok)[:110]}")
check("the answer asks the visitor to confirm, and never claims a subscription",
      "confirm" in str(ok.get("message", "")).lower()
      and "subscribed" not in str(ok.get("message", "")).lower(),
      str(ok.get("message"))[:110])
check("the answer carries no address, ARN or AWS error",
      _reserved not in json.dumps(ok) and "arn:aws" not in json.dumps(ok)
      and "Traceback" not in json.dumps(ok), f"{elapsed * 1000:.0f} ms")

for attempt in (2, 3):
    status, again, _, _ = post("/subscriptions", {"email": _reserved})
    check(f"attempt {attempt} in the same hour is still accepted", status == 202, f"HTTP {status}")

status, limited, _, _ = post("/subscriptions", {"email": _reserved})
check("the fourth attempt in the hour is rate limited",
      status == 429 and limited.get("status") == "rate_limited",
      f"HTTP {status} {json.dumps(limited)[:90]}")
check("the rate-limit answer carries no address",
      _reserved not in json.dumps(limited))

status, bad, _, headers = post("/subscriptions", {"email": "not-an-address"}, origin=ORIGIN)
allow = {key.lower(): value for key, value in headers.items()}.get("access-control-allow-origin")
check("the subscription route answers the dashboard origin", allow == ORIGIN, f"{allow!r}")

print("\nCORS")
status, _, _, headers = get("/zones", origin=ORIGIN)
allow = {key.lower(): value for key, value in headers.items()}.get("access-control-allow-origin")
check("the dashboard origin is allowed", allow == ORIGIN, f"{allow!r}")
status, _, _, headers = get("/zones", origin="https://evil.example")
allow = {key.lower(): value for key, value in headers.items()}.get("access-control-allow-origin")
check("an unknown origin is not allowed", allow in (None, ""), f"{allow!r}")

print("\nLatency (five polls on one keep-alive connection, the dashboard's own pattern)")
# The dashboard polls on a reused connection every 5 s, so the samples below reuse one, and the
# bound is on the median: a single sample can lose seconds to this workstation (TLS handshake,
# DNS, a busy CPU) without the endpoint being slow, and the Lambda's own durations - read by
# build/lambda_duration_probe.py - are the evidence for the service side. The 10 s ceiling still
# catches an endpoint that is genuinely broken, and the maximum is printed rather than hidden.
samples: list[float] = []
statuses: list[int] = []
connection = http.client.HTTPSConnection(API.removeprefix("https://"), timeout=30.0)
try:
    for _ in range(5):
        started = time.perf_counter()
        try:
            connection.request("GET", "/zones")
            response = connection.getresponse()
            response.read()
            statuses.append(response.status)
        except Exception:  # noqa: BLE001 - a failed poll is reported by the checks, not raised
            statuses.append(0)
        samples.append(time.perf_counter() - started)
        time.sleep(0.4)
finally:
    connection.close()
median = statistics.median(samples)
print(f"       min {min(samples) * 1000:.0f} ms · median {median * 1000:.0f} ms · "
      f"max {max(samples) * 1000:.0f} ms (maximum is a client-side measurement, not a "
      f"service-side guarantee)")
check("every poll answered", all(status == 200 for status in statuses), f"statuses {statuses}")
check("the median poll answered in under 3 s (the dashboard polls every 5 s)", median < 3.0,
      f"median {median:.2f}s")
check("no poll took longer than 10 s", max(samples) < 10.0, f"max {max(samples):.2f}s")

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
