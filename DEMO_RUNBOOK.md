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

# 2. the deployed stack is the code in this repository (expect 'RESULT: ... identical' and
#    'all deployed page contracts hold'; both are read-only)
python tools\verify_deployed_code.py
python tools\verify_deployed_page.py
Get-ChildItem web | ForEach-Object { "{0,-16} {1,8} {2}" -f $_.Name, $_.Length, (Get-FileHash $_.FullName -Algorithm MD5).Hash.ToLower() }
aws s3api list-objects-v2 --bucket floodpulse-dashboard-725934295966 --query "Contents[].[Key,Size,ETag]" --output text --profile cloud-project --region eu-north-1

# 3. the demo story still works (about 20 seconds; expect 'demo reliability check: PASSED')
python tools\storm_demo.py --export build\story
node tools\story_check.mjs --dir build\story

# 4. the deployed page is the hardened page (expect the policy to be served and no inline script;
#    add ?api=<origin> for a local server only if you also allow that origin in the CSP)
python -c "import urllib.request; h=urllib.request.urlopen('http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com/index.html').read().decode('utf-8'); print('policy:', 'Content-Security-Policy' in h, '| api base as data attribute:', 'data-api=' in h, '| inline script:', '<script>' in h)"

# 5. open the dashboard now, not in front of the audience
Start-Process "http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com"
```

With the page open, press **F12** once and check the Console tab is **completely silent**: the page
carries a Content-Security-Policy, and a violation would be reported there (it is how a bad
`connect-src` would show itself). No browser was available when this policy was written, so this is
the manual check that replaces an automated one.

Browser QA of the deployed page found exactly one entry here — *"The Content Security Policy
directive 'frame-ancestors' is ignored when delivered via a `<meta>` element"* — and it has been
fixed by removing that directive, which no meta element can enforce anyway (see the security
section of `README.md` for the anti-framing gap this leaves and why closing it properly would need
a new AWS resource). **Expected state now: no output at all.** If you see that warning again, the
deployed page is stale or the directive came back; if you see anything else, treat it as a defect
and write it down rather than dismissing it as noise. Close the console afterwards — nothing about
the demo needs it.

On the dashboard, confirm with your own eyes (it opens on **Overview**): the connection badge reads
**LIVE**, the headline answers the question in one sentence (e.g. "2 of 5 cities are at a critical
flood risk right now."), the four summary cards are filled, the highest-risk spotlight names a city
with its band and says **why** in a sentence ("Risk is currently NORMAL. Soil saturation is the main
contributor, while rainfall, drain level, water level and flow rate remain below their watch
levels.") with one facts line under it (main risk factor, how old the reading is, which way it is
going), the fleet strip shows five tiles with real counts, and the freshness line counts up between
polls. There is no formula, no variable name and no model name anywhere on this screen — if you can
see `weighted base`, `soil_saturation_pct` or `heuristic-v1` outside a fold, that is the bug this
pass was about. Then open **Cities → any city**: five measurement tiles carry per-metric provenance
chips (`REAL` / `MODEL` / `SIM`) and a small "?" that explains the metric in one sentence, the
"What is driving risk?" block names the primary factor, and *Technical details* is folded away —
open it and the variable names, weights, arithmetic and the API's own output line are all still
there.

Then check the page the way a phone would (F12 → the device toolbar, or just drag the window narrow):
at ~390 px the nav collapses to a menu button that closes on Escape, the cards stack, nothing scrolls
sideways, and the tap targets are finger-sized. Widths worth a glance: 320, 360, 390, 430, 768, 1024,
1280, 1440+.

### The 60-second tour (five sections)

| Click | What it shows | The line to say |
|---|---|---|
| **Overview** | Plain-language fleet answer, highest-risk spotlight, active alerts, cities reporting, weather freshness, band strip, one status card per city | "This is the whole fleet in one screen, and it says in words what the numbers mean." |
| **Cities → a city** | Score and class **as the API reported them**, a plain-English reason, five measurement tiles with thresholds, provenance and per-metric help, what is driving risk with shares, freshness, history chart (click it and use the arrow keys), then folded *Technical details* and the city's own alert history | "Same city, detail on demand. The model is one click away, not in the visitor's face — open it and you get the variable names, the weights and the arithmetic." |
| **Alerts** | Active and resolved as separate sections, then System status: reporting count, weather freshness, and an alert-delivery panel that separates *publishing* from *delivery* | "Nothing is open right now — and look at the wording: publishing is configured, but this page will not pretend an email arrived. It cannot know that." |
| **Data & Methodology** | REAL / MODEL_DERIVED / SIMULATED as three cards, Open-Meteo attribution, `heuristic-v1` broken down visually, **the calculation number by number** as a folded worked example of the highest-risk reading, the simulated hydraulics, and the limitations | "This is the honest page: what is measured, what is simulated, and what this score is **not** — and if you want the arithmetic, here it is, one fold down." |
| **About** | What FloodPulse is, why it exists, the five cities, how it works, what it is built on, flood safety guidance with links to IMD and NDMA, and the limitations | "And this is the part a professor or a resident should read: it is an academic prototype, it is not an official warning service, and for official guidance follow the authorities it links to." |

The URL follows the section (`#/overview`, `#/cities/city-chennai`, `#/alerts`, `#/data`, `#/about`),
so you can jump straight to a city, the browser back button works, and a mistyped fragment falls back
to the Overview. On a phone-sized window the links collapse into a menu button.

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
| closure | ~98 | all three NORMAL again, three closed incidents in the **Alerts** view |

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
python tools\live_acceptance.py   # 35 asserts over the live API, read-only
```

## 3. Recovery moves (if something looks wrong)

| Symptom | Cause / fix |
|---|---|
| Badge shows **STALE** or **DISCONNECTED** | the API stopped answering. Check the endpoint with the `curl.exe` line from the deploy summary; the board keeps the last values, says they are out of date, and retries every 5 s by itself. Nothing to restart. |
| Badge shows **CONNECTING** with a reason | the last poll failed; the reason is printed under the badge. A 500 from Lambda usually means a transient AWS error — it clears on its own. |
| Cities show "Awaiting the first live weather refresh" | the fleet is live but nobody has polled for over 10 minutes; the next poll triggers the refresh. |
| A city card says "not reporting recently" (or the highest-risk card shows a reading age next to the score) | that city's newest reading is older than 30 minutes, so the score on the card describes a past moment, not now. Say so if it is on screen: it is the board being honest, not a fault. The age comes back down by itself on the next successful provider refresh. |
| Everything is simulated and no city cards appear | the Lambda is still in demo mode: re-run `infra\deploy.ps1` with no flags. |
| The board is frozen at a value from 10 minutes ago | by design: the feed refreshes at most once per 600 s, and only while the page is polling. Press refresh in the browser. |
| The page shows nothing at all | you are serving it from the wrong origin: only `http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com` and `http://localhost:8000` are allow-listed for CORS. `127.0.0.1:8000` and any other port are blocked by design — use the S3 website URL, or start the local server with `python -m http.server 8000 --directory web` and open `http://localhost:8000`. |
| Lambda errors during the demo | `aws logs tail /aws/lambda/floodpulse-api --since 10m --profile cloud-project --region eu-north-1`. Since the hardening pass this log also carries one line per incident opened/escalated/closed, so "what did it decide and when" is answerable after the fact. |
| The Console (F12) shows a message about the *payload could not be fully rendered* | the renderer refused something the API sent and the status line was refreshed instead of the failure being hidden. The board keeps the last good values; note the zone id from the message. |
| A poll is answered with 429 | the stage throttle (burst 40 / rate 20 per second) engaged — that means something is generating far more request volume than the demo does. It clears within a second; if it persists, check for a looping script. |
| You need the throttle out of the way entirely | `aws apigatewayv2 update-stage --api-id 2rg2u8quzd --stage-name '$default' --default-route-settings '{\"ThrottlingBurstLimit\":5000,\"ThrottlingRateLimit\":10000}' --profile cloud-project --region eu-north-1` (read it back with `get-stage`) |

## 3b. Handling the ingestion key safely (only if the key actually needs changing)

The key is only needed to **write** readings; every read route is public, so a demo that only shows
the dashboard never touches it. If it does need rotating, do it like this and nothing can be left in
a half-written state:

```powershell
# 1. dry run: prints the fingerprint and what it would write, writes nothing
#    (--reuse means 'keep the key that exists'; without it the tool refuses to act on an
#     existing key file, which is the safe default)
python tools\init_ingest_key.py --lambda-env build\env-prod.json --reuse --dry-run
python tools\init_ingest_key.py --reuse --show-fingerprint

# 2. rotate for real: atomic write, and every other Lambda variable is left untouched
#    (measured: 'dry run: ... already carries this key: 8 other variable(s) left untouched')
python tools\init_ingest_key.py --lambda-env build\env-prod.json --force

# 3. push the new value to the Lambda and re-verify the deployed code
powershell -NoProfile -File infra\deploy.ps1
python tools\verify_deployed_code.py
```

The deploy script refuses to run if the environment file and `.floodpulse-key` disagree about the
key (it prints both fingerprints and stops), because applying the environment file is what a deploy
does — a rotation that only touched the key file would be silently undone. If the drift is
deliberate, the flag is `-AllowKeyDrift`. See the deploy switches in `README.md` (M8) for the other
deliberate opt-outs and for `-DryRun`, which reads and compares everything and changes nothing.

* Both scripts print a `sha256:` **fingerprint** (12 hex characters), never the key. Compare the
  fingerprint from `.floodpulse-key` with the one in the deploy output: if they differ, stop and
  re-run step 2 rather than assuming which side is stale.
* `infra\deploy.ps1` checks that the local key file and the deployed environment agree **before** it
  changes anything, and prints the recovery command if they do not.
* After rotating, an old key must answer HTTP 401 on `POST /readings` (verified in earlier passes).
* Never put the key in a command line, a screenshot, a slide, or this repository.

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
  topic with no subscribers). Measured at the end of the last pass: 1,841 Lambda invocations and
  ~1.7 MB of DynamoDB over two days, which is under $0.01. A demo day stays far below $0.05.
* **What if a city stops reporting?** Its last reading stays on the board with its age until it is
  more than 30 minutes old, then the card, the highest-risk stat and any open incident say "not
  reporting recently" and `/zones` lists it in `stale_zone_ids`. Nothing is invented to fill the
  gap, and an incident on that city stays OPEN: closing one needs a real NORMAL reading, and no
  reading is not NORMAL.

## 4b. The five-minute visual check (M9 — the parts no harness can see)

The harnesses verify the CSS and the DOM; none of them can see pixels, and this machine had no
browser. So this is the list that closes that gap. Do it once, on the deployed site, before the
demo, with F12 open.

| # | Check | What good looks like |
|---|---|---|
| 1 | **Console is empty** | no output *at all*: no CSP violation, no ignored-directive warning, no 404 for any asset, no uncaught error. A missing `bg-storm.webp` shows as a 404 here and the page still reads well (the gradient underneath is the fallback). |
| 2 | **Overview in five seconds** | the question, then the sentence that answers it, then four cards. If you have to read a formula to understand the page, that is the bug this pass was about — the spotlight must say *why* in words ("Soil saturation is the main contributor, while rainfall … remain below their watch levels"), and the only numbers on the screen are the score, the band, the age and the trend. |
| 3 | **The backdrop** | the storm image is atmosphere behind the content, not something the text sits on. Anywhere a heading competes with the image is a defect: say so rather than squinting at it. |
| 4 | **Widths** | 320, 360, 390, 430, 768, 1024, 1280, 1440+. No sideways scrolling at any of them; cards stack logically; the hero does not fill a phone screen; long city names wrap. |
| 5 | **Keyboard only** | Tab through: skip link, nav, every card, the chart, the "?" explanations, the *Technical details* folds, the footer. Focus is always visible, never lost, and the mobile menu closes on Escape. |
| 6 | **Reduced motion** | Windows Settings → Accessibility → Visual effects → Animation effects **off**, then reload: the skeleton shimmer stops and nothing else moves. |
| 7 | **Zoom** | 200% browser zoom: nothing overlaps, nothing is clipped, no horizontal scrollbar appears. |
| 8 | **Screen reader, briefly** | with NVDA or Narrator: the fleet state is announced once when it changes (not every five seconds), each chart has its text summary, and icons are silent because the words beside them carry the meaning. |
| 9 | **Empty and error states** | stop the API (`?api=` pointing at a dead host) and reload: every section says what is wrong and offers "Check again now" — never a blank panel, never a raw error. |
| 10 | **Trust wording** | on Alerts, the delivery panel must never read as "email sent". If a sentence implies a subscriber received something, that sentence is a defect. |

## 4c. The M10 look, and the subscription form you can show (five more minutes)

### What changed on screen

| Where | What to look at |
|---|---|
| Backdrop | the rainy city is *atmosphere*: a strong navy scrim sits between the image and the text, so no paragraph is ever read against bright detail. If a heading competes with the photograph, that is a defect worth reporting |
| Header | three groups — brand, the five sections (centred, current one in a pill), and the system state on the right ("System healthy", "Live data", "Last updated") as chips. **The model name is not here any more**; it is in the Overview's folded "Snapshot and technical summary" and on Methodology |
| Hero | the question, then the answer in words ("No city is at flood risk right now."), the monitor line ("We are monitoring 5 Indian cities. All are currently below the alert level."), the plain reason sentence, one facts line, and the leading city. Beside it: a short explanation and one button, **Learn how risk is calculated**. No formula |
| Summary cards | four cards with an icon each; the highest-risk one reads `Highest risk city / <city> / <BAND> / score … · updated … ago` |
| City cards | a gradient city plate (an abstract city-water mark — there is no licensed city photograph in this project, and none is fetched from anywhere), the band, the score, a short meter whose fill is the score and whose ticks are the three documented band edges, rainfall, freshness, and a click affordance |
| Recent alerts | on the Overview: the two newest active incidents, or the calm state — "✓ No active flood alerts / All monitored cities are currently below the alert level." with a *View alerts* button |
| How FloodPulse works | three steps: Collect · Analyse · Alert |

### The subscription form — rehearse this once, exactly

The card is on the **Alerts** page, between the resolved incidents and the system status. Local part
only: **use an address you actually own and can open**, because the whole point is that AWS emails it.

1. Open Alerts. The card reads "Get flood alerts by email" / "Receive an email when FloodPulse
   detects WARNING or CRITICAL flood risk." — and the three short notes: the confirmation sentence,
   the privacy sentence, and the SNS unsubscribe explanation.
2. Type `not-an-address` and submit → **"Enter a valid email address."**, field marked invalid, focus
   stays in the field. Say the words: *the browser and the API both refuse it; nothing is sent.*
3. Type your own address and submit → **"Check your inbox"** / "We sent a subscription confirmation
   request through Amazon SNS. Click 'Confirm subscription' in that email to activate FloodPulse
   alerts." It never says "You are subscribed", because the page cannot know that yet — that is the
   honest state, and it is the sentence to dwell on if anyone asks about delivery.
4. Open the mailbox: the message is from *Amazon Web Services*, and the link is Amazon's own. Click
   **Confirm subscription** → SNS now holds a confirmed subscription. *Nothing in FloodPulse stores
   the address*: SNS owns the endpoint, and the DynamoDB rate-limit item holds a SHA-256 digest, a
   count and a TTL, never the address.
5. Submit the same address three more times within the hour → **"Too many subscription requests.
   Please try again later."** Show the item that is doing it (`build/` script or the console):
   `pk = SUBRATE#<sha256>`, `sk = WINDOW#<hour bucket>`, `count`, `ttl`. Note for the audience that it
   contains no email address.
6. If the permission has not been approved yet, step 3 answers **"We couldn't create the subscription
   request right now. Please try again."** — and that is the honest, correct behaviour of an
   undeployed backend, not a bug to hide. Say so, and say why: the Lambda is not allowed to call
   `sns:Subscribe`, and that permission is a deliberate manual approval.

**Do not demo the confirmation step with somebody else's address**, and never read a real address
aloud. If you cannot open a mailbox during the demo, show steps 1–3 and the rate-limit item instead:
the flow is provable end to end without a mailbox, and the last step is Amazon's, not ours.

### Two commands that make the above fail loudly if it breaks

```powershell
python tools/verify_deployed_page.py   # all deployed page contracts hold (14 M10 needles, 7 forbidden strings)
node tools/dashboard_run.mjs --site http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com \
  --api https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com   # 66 runtime checks against the live API
```

## 5. What not to claim

* Do not say the cities are "at risk" as a forecast — the bands are illustrative.
* Do not say the score is a prediction, and do not present the plain sentence as science: it repeats
the model's own contribution list in words. The model itself, with its weights and its arithmetic,
is one fold down on the city page and on Data & Methodology — point there rather than paraphrasing
it from memory.
* Do not say "real-time sensor data" for drain/water/flow.
* Do not say the dashboard is "fully live" while a stale banner is showing: the board says what it is.
* Do not say alerts reach anyone until a subscription is confirmed on `floodpulse-alerts`. The
  Alerts page is deliberately phrased around this: "Alert publishing: Configured" and, for an
  incident with a recorded publish, "published to the notification topic". Subscriber state reads
  "Not reported to this page", because the API does not expose it.
* Do not say the dashboard is an official warning service, and do not let the safety guidance read
  as if it were: it says FloodPulse does not issue official warnings and points to IMD and NDMA for
  those. The About page is the right place for that sentence in the demo.
* Do not claim the dashboard is protected against clickjacking or framing. It is not, and it cannot
  be from a `<meta>` policy: `frame-ancestors` is ignored there, and S3 website hosting cannot send
  the HTTP response header that would work. The security section of `README.md` records the gap and
  why it is acceptable for a page with no login, no session and no form.
* **Do not say a visitor "is subscribed" after they submit the form.** Until they follow the link in
  Amazon's confirmation email, the subscription is pending, and the page says exactly that: "Check
  your inbox … Click 'Confirm subscription' in that email". Saying more would be a claim this system
  cannot verify. (Same rule for the delivery panel, above.)
* Do not promise a rate limit as a security boundary: the per-address window is three confirmation
  requests an hour, and the outer bound is still API Gateway's stage throttle, which is unset.
* Do not mix the two fleets in one sentence: `FLOODPULSE_ZONE_MODE` selects exactly one fleet, and the
  demo story is rehearsed on the demo fleet only.
* Do not read a city that stopped reporting as a calm city: its score is an old one, and the board
  says exactly that next to it.
