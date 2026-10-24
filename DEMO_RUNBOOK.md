# FloodPulse demo runbook

**Two ways to present this.** Pick one before you start; do not mix them mid-demo.

| | Path A - live cloud (default) | Path B - local, offline |
|---|---|---|
| What the audience sees | the deployed dashboard at the S3 URL, backed by the live API | the same four files served from `web/`, against payloads exported from the real handler |
| Needs | internet, and a browser | Python only: no network, no AWS |
| Strength | it is the real thing: real Open-Meteo rainfall, DynamoDB, SNS | cannot fail on stage; the storm arc is instant and deterministic |
| Use it when | the network is reliable | the venue Wi-Fi is not, or a reviewer wants to see it twice |

Everything below assumes **Path A** unless a step says otherwise; Path B is at the end.

Be honest out loud, at the start and if anyone asks:

* FloodPulse has **no real sensors**. Drain level, water level and flow rate are simulated everywhere;
  rainfall is real and comes from a provider grid point, not a street gauge.
* The score comes from a hand-tuned `heuristic-v1` heuristic that is **not** a validated hydrological
  model and is **not** calibrated for these cities. It is a risk indicator, never a flood probability.

---

## Before the clock starts (2 minutes, off-stage)

```powershell
# 1. the API answers, and says what it is: HTTP 200, zone_mode "live_weather", zone_count 12
curl.exe -s -o - -w "`nHTTP %{http_code}`n" https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com/zones

# 2. the deployed stack is the code in this repository (both read-only; both print their verdict)
python tools\verify_deployed_code.py
python tools\verify_deployed_page.py

# 3. open the dashboard and leave the tab open
start http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com
```

**Warm the history first.** The live fleet stores a sample when the weather is refreshed - at most once
every 10 minutes (`WEATHER_REFRESH_SECONDS = 600`) - and history expires on a TTL. Open the site cold and
the city page may honestly say *"Only 1 usable historical sample is available, so no direction is
described."* That is the system being truthful, not broken, but it is a poor thing to present. **Open the
site 20-30 minutes before you present and leave the tab open**, or present the trend from Path B, where
the arc is complete by construction.

Also keep open: the repo at `docs/FINAL_TECHNICAL_SUMMARY.md` and `docs/FINAL_PROJECT_STATUS.md`, and the
API base URL <https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com/zones> in a second tab.

**Never** paste a real subscriber address into the form on stage, and never open `.floodpulse-key`.

---

## The demo (7-10 minutes, 17 steps)

Each step gives you: **Click** (exactly what to do), **Say** (what to tell the audience),
**Technical point** (the answer if someone probes).

### 1. The dashboard, as loaded (0:00)

* **Click:** the dashboard tab. It is already on the Overview.
* **Say:** "This is FloodPulse Coastal India. It watches twelve selected Indian coastal cities, and the
  question at the top is the one a visitor actually asks: *is any city at risk of flooding right now?*"
* **Technical point:** four shipped files - HTML, CSS and two JavaScript files, no framework, no
  dependencies - served from an S3 website bucket; the page polls the API every 5 seconds.

### 2. The one-sentence answer in the hero (0:20)

* **Click:** nothing; point at the hero sentence.
* **Say:** "The answer comes first, in one sentence, with the number of cities and the band that matters.
  When nothing is elevated, it says so plainly instead of inventing drama."
* **Technical point:** that sentence is generated from the same payload the cards use, so it cannot drift
  from the numbers below it.

### 3. Fleet summary (0:45)

* **Click:** scroll to **Fleet summary**.
* **Say:** "Twelve cities, counted by risk band, with how fresh the data is and which mode is serving it."
* **Technical point:** the counts come from `GET /zones`; freshness is the age of the newest stored
  reading, not the age of the page.

### 4. Cities by risk level (1:05)

* **Click:** **Cities by risk level**.
* **Say:** "A ranking by the heuristic score, so the top of this list is where I look first - and it is a
  risk indicator, not a flood forecast."
* **Technical point:** the ranking is computed client-side from the same scores the API serves, so there
  is one source of truth rather than a second ranked endpoint.

### 5. The city shortlist (1:25)

* **Click:** any city card on the Overview (the shortlist; the full fleet is under **Cities**).
* **Say:** "Each card carries the current score, the band, the top driver, and a risk meter drawn to the
  same scale as every other card."
* **Technical point:** the meter's band edges are the model's own 0.35 / 0.60 / 0.80, printed on the
  scale, so the picture cannot flatter the score.

### 6. Into the fleet: the Cities page (1:50)

* **Click:** **Cities** in the top navigation.
* **Say:** "Every monitored city, with live values from DynamoDB via the API - twelve of them, grouped
  West Coast, East Coast and Island."
* **Technical point:** nothing is pre-baked in the HTML. With the API unavailable the page shows its
  failure state rather than stale numbers it cannot vouch for.

### 7. Filters are navigation, not scoring (2:15)

* **Click:** **West Coast**, then **East Coast**, then **Island**; clear it and click **WATCH+**.
* **Say:** "The coast filter is for finding a city. The system computes no state, coast or national risk
  figure - one city being normal tells you nothing about its neighbour."
* **Technical point:** `coast_group` is per-city configuration used only for grouping and filtering; the
  band filter is a client-side view of the same fields.

### 8. One city in full (2:35)

* **Click:** any city card, e.g. **Kochi**.
* **Say:** "One city in full: the sentence at the top, the band, then the measurements that produced it."
* **Technical point:** every city view is reachable by URL (`#/cities/city-kochi`), so a map marker and a
  grid card land on the same place, and a city page can be linked to directly.

### 9. Per-metric provenance (2:55)

* **Click:** the **Measurements** panel; point at **Rainfall**, **Drain level**, **Soil saturation**,
  **Water level**, **Flow rate**.
* **Say:** "Every metric says where it came from. Rainfall is real, from Open-Meteo. Soil saturation is
  derived from the provider's soil moisture. Drain level, water level and flow rate are simulated,
  because there is no flow meter or drain sensor anywhere in this project."
* **Technical point:** the labels ride with the data - stored per record, returned per metric by the API,
  printed by the page. They are not a caption written once.

### 10. Why the score is what it is (3:20)

* **Click:** **What is driving risk?**
* **Say:** "This is why the model is deterministic: the score decomposes into the weights that produced
  it, in plain language, so I can explain any number on this page."
* **Technical point:** `heuristic-v1` is a bounded weighted sum over five normalised metrics, classified
  against fixed band edges, with a 2-reading escalation streak and a 3-reading de-escalation streak, so a
  single noisy poll cannot move a city on its own.

### 11. Trend and history - the honest part (3:45)

* **Click:** **Trend and history**; switch the window chips (**1H → 6H → 24H**).
* **Say:** "The window is anchored on the newest stored sample, not on the clock, and the note under the
  chart states the span the stored history actually covers. Net change is last minus first; the trend is
  a least-squares slope per hour, and anything inside ±0.02 per hour is called steady. When the stored
  history is too thin to describe, it says so instead of drawing a line."
* **Technical point:** below 3 samples or under 600 seconds of span the trend is `INSUFFICIENT`; an
  interval longer than `max(3 × median, 900 s)` is disclosed as a gap instead of being smoothed away;
  rainfall is an instantaneous rate in mm/h, never an accumulation. It describes stored samples only -
  nothing is extrapolated.

### 12. The coast map (4:15)

* **Click:** back to **Cities**, scroll to **Coastal coverage**.
* **Say:** "Twelve markers for twelve cities, each a link with its own name, so the map is navigation
  rather than decoration."
* **Technical point:** markers are in the keyboard order and carry accessible names; the map is one
  labelled graphic rather than twelve unlabelled images.

### 13. Incidents (4:40)

* **Click:** **Alerts** in the top navigation.
* **Say:** "A city at Warning or Critical opens an incident; it escalates if the city gets worse, and
  closes when it de-escalates. At most one incident is open per city, enforced by an atomic pointer item,
  so two concurrent readings cannot both open one."
* **Technical point:** an incident records that *this system's threshold was crossed*, not that a flood
  was observed. Severity, window, status and the band path are all on the record.

### 14. Email alerts, and what "sent" means (5:05)

* **Click:** **Get flood-risk alerts by email**; do **not** submit an address on stage.
* **Say:** "Anyone can subscribe. The form validates the address, rate-limits it to three per hour, and
  AWS SNS sends a confirmation mail - only a confirmed subscription receives alerts. Nothing is published
  unless a city escalates, and the system never claims an e-mail was delivered, only that it published to
  the topic."
* **Technical point:** the rate-limit record stores a hash of the address, not the address; the response
  is deliberately generic so it cannot be used to test whether an address is already subscribed.

### 15. Where the numbers come from (5:30)

* **Click:** **Data & Methodology** in the footer (or **Data** in the top navigation).
* **Say:** "This page is the project's contract with its reader: three kinds of number - real, derived,
  simulated - where the live data comes from, and the calculation quoted number by number."
* **Technical point:** the thresholds and weights on that page are generated from `config.py`, so the
  public methodology cannot drift from the code that runs.

### 16. What it is not (6:00)

* **Click:** **About & safety guidance** → **Limitations, stated plainly**.
* **Say:** "And here is the part I care about most: what this is not. It is not a flood forecast, not a
  validated hydrological model, not a national warning service, and the thresholds are not calibrated for
  these cities. The score is a risk indicator."
* **Technical point:** the project's own scientific rule is that heuristic scores are never described as
  validated predictions, and the same wording appears in the README, the Data page and the About page.

### 17. Close on the architecture (6:30)

* **Click:** nothing, or switch to the repo.
* **Say:** "Underneath: one Python Lambda behind an HTTP API, DynamoDB with a fixed-partition-key model
  and no scans on any read path, SNS for escalation alerts, CloudWatch with 7-day retention, and a static
  S3 site. No server, no database instance, no Kubernetes, no NAT gateway. Deployment is one idempotent
  PowerShell script that creates what is missing, never deletes, and can dry-run."
* **Technical point:** five routes - read paths public, ingestion key-gated - and an IAM role scoped to
  this one table and this one topic with no `DeleteItem`. The repository carries the verification: 773
  tests, eleven harnesses, byte-identity checks against the deployed Lambda, and
  `docs/FINAL_PROJECT_STATUS.md` for the state of every part.

**With 10 minutes rather than 7**, spend the extra time on step 11 (windows and gaps) and step 16
(limitations).

---

## Path B - local, offline, deterministic (5 minutes)

```powershell
# 1. the storm arc, no AWS: calm -> flash flood -> recession, verified stage by stage
python tools/storm_demo.py --export build/story

# 2. check the exported story, then watch it in the real front end
node tools/story_check.mjs --dir build/story
python -m http.server 8000 --directory web        # then open http://localhost:8000
```

* **Say:** "This runs the real handler in-process - validation, scoring, storage, incidents - with no AWS
  calls, and exports what the API would have served at each stage."
* **Technical point:** the same four front-end files render both these local payloads and the live API,
  so what the audience sees locally is the code that is deployed.

---

## If something breaks on stage

| Symptom | What is happening | What to do |
|---|---|---|
| "Only 1 usable historical sample…" under a chart | history is thin: the weather refresh stores a sample at most every 10 minutes, and old samples expire by TTL | say why - it is the honest state - then continue; use Path B if a full trend is required |
| The page says the feed is stale | the provider fetch failed and the last good cached weather is being served | point at the disclosure: the page says which data it is replaying instead of pretending it is new |
| A panel shows a failure note | that one read failed; the panel keeps its last good data and states the reason | refresh once - the retry backs off instead of hammering the API |
| No network at all | Path A cannot work | switch to Path B, and run the proof commands below |
| Someone asks for proof | - | run the commands in the next section, live |

## Proof you can run on demand

```powershell
python -m pytest -q tests                                     # 773 tests / 2,333 subtests
node tools/dashboard_check.mjs                                # 138 static checks (144 with live payloads)
node tools/dashboard_run.mjs --site <site> --api <api>        # 80 runtime checks against the deployed site
node tools/dashboard_scenarios.mjs                            # 66 failure and edge scenarios
node tools/temporal_check.mjs                                 # 141 assertion groups on the temporal maths
python tools/live_acceptance.py                               # 62 checks against the deployed API
python tools/verify_deployed_code.py                          # deployed Lambda == src/, byte for byte
python build/m16_bytes.py                                     # 409,501 / 409,600 bytes, no CRLF
```

Browser checks need one running browser with a debugging port; the tools never start one:

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless=new `
  --remote-debugging-port=9222 --user-data-dir=build\edge-verify-m18 about:blank
node tools/m16_browser_check.mjs --site <site> --api <api>     # 32 checks
node tools/keyboard_check.mjs <site>                           # 8 keyboard checks
node tools/layout_scan.mjs <site>                              # 4 routes x 4 widths
```

## Likely questions

`docs/VIVA_CHEATSHEET.md` holds the 33 answers, `docs/PRESENTATION_FACTS.md` the numbers to quote, and
`docs/FINAL_PROJECT_STATUS.md` what is finished, what is deliberately not built, and what is known to be
limited.
