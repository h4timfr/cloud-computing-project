# FloodPulse demo runbook

A rehearsed, tested script for showing FloodPulse. Everything here was run against the deployed
stack; the numbers quoted are the ones it actually produced, not aspirations.

Two things to be honest about, out loud, at the start and if anyone asks:

* FloodPulse has **no real sensors**. Drain level, water level and flow rate are simulated
  everywhere; risk scores come from a hand-tuned `heuristic-v1` heuristic that is **not** a
  validated hydrological model and **not** calibrated for these cities.
* In the live India fleet, **rainfall and soil moisture are real** (Open-Meteo, CC BY 4.0) — but from
  a provider grid point, not a street gauge. Each metric on the board carries its own provenance tag,
  so the screen says which is which.

---

## 0. Two minutes of pre-flight (do this before anyone is watching)

```powershell
# 1. the fleet is live and the weather feed is healthy (expect HTTP 200, zone_mode "live_weather",
#    five cities, REAL rainfall, cache ages well under 600 s)
python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com/zones')); print(d['zone_mode'], d['zone_count'], d['data_classification'], d['weather']['cache_age_seconds'])"

# 2. the deployed dashboard is byte-identical to this repository (expect 'RESULT: ... identical')
Get-ChildItem web | ForEach-Object { "{0,-16} {1,8} {2}" -f $_.Name, $_.Length, (Get-FileHash $_.FullName -Algorithm MD5).Hash.ToLower() }
aws s3api list-objects-v2 --bucket floodpulse-dashboard-725934295966 --query "Contents[].[Key,Size,ETag]" --output text --profile cloud-project --region eu-north-1

# 3. the demo story still works (about 20 seconds; expect 'demo reliability check: PASSED')
python tools\storm_demo.py --export build\story
node tools\story_check.mjs --dir build\story

# 4. open the dashboard now, not in front of the audience
Start-Process "http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com"
```

On the dashboard, confirm with your own eyes: the connection badge reads **LIVE**, the fleet strip
shows five tiles, each city card shows a provenance chip per metric (`REAL`, `MODEL_DERIVED`,
`SIMULATED`) and the freshness line counts up between polls.

---

## 1. Track A — the storm story (the 90-second version, recommended)

Tells the whole product story: calm → watch → warning → critical → recovery → incidents closed.
It needs the deployed fleet switched to demo mode, which is a normal re-run of the deploy script
(the same one that always deploys) — no new AWS resource, and it is flipped back at the end.

```powershell
# switch the deployed Lambda to the three simulated demo zones, with the weather feed off
# (WeatherMode off means the demo cannot depend on any external service being up)
# measured: ~78 s, because the deploy script re-verifies every resource it manages
powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -ZoneMode demo -WeatherMode off

# the whole arc, 100 readings per zone at the library cadence: about 8 minutes, ends with the
# incidents closed again (measured: 498 s for the run, fleet back to NORMAL)
python tools\live_simulator.py --url https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com --interval 5.0 --steps 100

# fast track instead (about 2.5 minutes): calm -> CRITICAL, then stop
# (from a calm fleet the zone reaches CRITICAL around reading 32)
python tools\live_simulator.py --url https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com --interval 2.0 --steps 40 --stop-at CRITICAL
```

**Cadence rule, learned the hard way:** a step posts one reading per zone and takes ~1.2 s per
request, so one step of three zones costs ~4 s. Use a cadence at least that long (`--interval 5.0`
is the safe default; `2.0` works for the short fast track because the drift stays inside the API's
±300 s guard). A 1 s cadence walks the simulator's clock ~3 s per step ahead of real time and the API
eventually answers `clock_skew`. The tool now measures this and warns:

```
warning: the simulated clock is 140s behind real time after 60 steps; the API refuses readings
beyond its clock-skew guard...
```

While the arc runs, the board plays out (verified numbers, `--steps 100 --seed 20260214`, from a
calm demo fleet):

| Stage | Reading # | What the audience sees |
|---|---|---|
| calm | 0 | three NORMAL tiles, flat history, "the system is quiet" |
| watch | ~24 | one zone turns amber, strip reads 2 NORMAL / 1 WATCH |
| warning | ~29 | the zone turns orange, the incident opens, an SNS alert is published |
| critical | ~32 | red tile + red banner, strip reads 0/0/1/2, peak score 0.925 on the leader |
| recovery | ~96 | tiles fall back to NORMAL/WATCH, first incident closes |
| closure | ~98 | all three NORMAL again, three closed incidents in the incident panel |

To stop early instead of playing the whole arc:

```powershell
# reach CRITICAL and stop there (about 40 s once the fleet is already escalating)
python tools\live_simulator.py --url https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com --interval 2.0 --steps 40 --stop-at CRITICAL
```

Note that the demo fleet keeps whatever state the last run left: after a full arc it is NORMAL, but if
a previous run stopped at CRITICAL the fast track will exit on its first reading. Run the full arc
once before the audience if you want a clean start.

Afterwards — **always** — flip the fleet back to live:

```powershell
powershell -ExecutionPolicy Bypass -File infra\deploy.ps1      # defaults: -ZoneMode live -WeatherMode open-meteo (~78 s)
python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com/zones')); print(d['zone_mode'], d['zone_count'], d['data_classification'])"
# expect: live_weather 5 mixed
```

The whole round trip — switch, storm, closure, switch back — was rehearsed against the deployed stack
and passed 18/18 assertions, including "the demo incidents are not part of the live board" (verified
2026-10-15, evidence in `build/demo_roundtrip_evidence.json`).

Why the switch is safe: `-ZoneMode demo` only changes two Lambda environment variables of the
existing function (`FLOODPULSE_ZONE_MODE`, `FLOODPULSE_WEATHER`) and re-uploads the same code. The
live cities' stored readings are untouched, the demo zones were already in the table from M3, and the
two fleets never appear in the same payload (the API answers for exactly one fleet at a time).

## 2. Track B — the live India fleet (no changes at all)

Nothing to switch: this is the deployed default. Best used when the audience cares more about "is it
real" than about drama.

* Five cities — Chennai, Mumbai, Bengaluru, Hyderabad, Kochi — with **REAL** rainfall and
  **MODEL_DERIVED** soil moisture from Open-Meteo, and **SIMULATED** drain/water/flow.
* The weather feed block shows the provider, the attribution (CC BY 4.0), the 600 s refresh window
  and, per city, how old the cached value is. The dashboard adds local elapsed time, so the age keeps
  moving between polls instead of freezing at the last poll.
* At least one provider request every 10 minutes is made **only while somebody calls `GET /zones`**
  (the refresh is lazy); if the provider fails, the previous real values are served with the feed
  marked degraded — FloodPulse never invents rainfall.
* Be explicit that a "storm" in a city would be a real regional rainfall figure, and that a live
  storm cannot be commanded on cue.

Useful extras while talking:

```powershell
# what the live feed actually contains right now (one real request, read-only, no AWS credentials)
python tools\weather_probe.py

# what the API serves, with provenance (pretty-printed)
python build\live_acceptance_hardening.py   # 35 asserts over the live API, read-only
```

## 3. Recovery moves (if something looks wrong)

| Symptom | Cause / fix |
|---|---|
| Badge shows **STALE** or **DISCONNECTED** | the API stopped answering. Check the endpoint with the `curl.exe` line from the deploy summary; the board keeps the last values, says they are out of date, and retries every 5 s by itself. Nothing to restart. |
| Badge shows **CONNECTING** with a reason | the last poll failed; the reason is printed under the badge. A 500 from Lambda usually means a transient AWS error — it clears on its own. |
| Cities show "Awaiting the first live weather refresh" | the fleet is live but nobody has polled for over 10 minutes; the next poll triggers the refresh. |
| Everything is simulated and no city cards appear | the Lambda is still in demo mode: re-run `infra\deploy.ps1` with no flags. |
| The board is frozen at a value from 10 minutes ago | by design: the feed refreshes at most once per 600 s, and only while the page is polling. Press refresh in the browser. |
| The page shows nothing at all | you are serving it from the wrong origin: only `http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com` and `http://localhost:8000` are allow-listed for CORS. `127.0.0.1:8000` and any other port are blocked by design — use the S3 website URL, or start the local server with `python -m http.server 8000 --directory web` and open `http://localhost:8000`. |
| Lambda errors during the demo | `aws logs tail /aws/lambda/floodpulse-api --since 10m --profile cloud-project --region eu-north-1` |

## 4. Questions you should expect, with the verified answers

* **Is any of this real?** Rainfall (and soil moisture, model-derived) for the five Indian cities, from
  Open-Meteo, tagged `REAL`/`MODEL_DERIVED` per metric. Everything else is simulated by FloodPulse,
  and every response says so in its own fields.
* **Is the risk score a prediction?** No. It is a documented weighted heuristic with hysteresis; the
  API's own `model_note` says it is not a calibrated hydrological model.
* **How fresh is the data?** The feed refreshes at most every 600 s per city; the provider's hourly
  rainfall value can itself be up to ~60 minutes old. The dashboard shows both the cache age and the
  provider's observation time.
* **What about alerts?** SNS publishes on a WARNING/CRITICAL escalation, rate-limited per zone and
  severity (600 s cooldown). The topic currently has **no confirmed subscription**, so nothing is
  emailed — say that plainly rather than implying somebody gets paged.
* **Where does the history come from?** One DynamoDB table (`floodpulse`), 7-day TTL on reading
  history, one item per reading plus `LATEST`, `INCIDENT#…`, `ALERT#COOLDOWN` and `WEATHER#CACHE`
  items in the same table. No GSI, no scans.
* **Cost?** Idle: effectively $0 (on-demand DynamoDB, Lambda free tier, one small S3 bucket, one SNS
  topic with no subscribers). A demo day stays far below $0.05.

## 5. What not to claim

* Do not say the cities are "at risk" as a forecast — the bands are illustrative.
* Do not say "real-time sensor data" for drain/water/flow.
* Do not say the dashboard is "fully live" while a stale banner is showing: the board says what it is.
* Do not say alerts reach anyone until a subscription is confirmed on `floodpulse-alerts`.
* Do not mix the two fleets in one sentence: `FLOODPULSE_ZONE_MODE` selects exactly one fleet, and the
  demo story is rehearsed on the demo fleet only.
