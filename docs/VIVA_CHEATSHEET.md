# FloodPulse viva cheatsheet

Thirty-three questions a reviewer is likely to ask, with answers that are true as of **2026-10-24**.
Answers are 2-5 sentences; where a command or file proves the claim, it is named in italics.

**Never claim, under any question:** that FloodPulse predicts floods, that the thresholds are calibrated,
that any value is measured by a physical sensor, that an e-mail was delivered, that a dollar cost was
measured, or that a feature exists which the answer below says is deferred.

---

**1. What does FloodPulse do, in one sentence?**
It watches twelve selected Indian coastal cities, scores their current and recent flood risk with a
deterministic heuristic built on real weather data, stores the history, raises incidents when the risk
crosses band edges, e-mails confirmed subscribers, and shows all of it in a dashboard that labels the
source of every number. It is a monitoring and risk-indication system, not a flood forecast.

**2. What is real, and what is simulated?**
Rainfall is real, fetched from Open-Meteo for each city; soil saturation is derived from the provider's
soil-moisture value; drain level, water level and flow rate are simulated, because this project has no
physical sensors. Every metric carries its label in storage, in the API response and on screen.
*Evidence: `data_classification: mixed` in `GET /zones`; the Data page's "Three kinds of number".*

**3. Why twelve cities, and how were they chosen?**
They are a **selected** coastal fleet with documented flood exposure - five west coast, six east coast,
one island - not a ranked or exhaustive list, and the README says outright that it is not a claim about
the twelve most at-risk cities. Each city's entry in `config.py` carries a selection reason, and the coast
group exists for navigation only.
*Evidence: `src/floodpulse/config.py`, `LIVE_ZONE_DEFINITIONS`.*

**4. Which AWS services does this use, and why those?**
Lambda for compute, API Gateway HTTP API for the public surface, DynamoDB for state, S3 for the static
dashboard, SNS for alerts, CloudWatch for logs, and IAM for a scoped execution role. Every one is
serverless or pay-per-use, so the idle cost is effectively zero. No EC2, RDS, ECS/EKS, OpenSearch,
CloudFront, VPC or NAT gateway exists.

**5. Why one Lambda instead of a function per route?**
One function keeps the deployment, the role and the cold-start surface small, and the routes share almost
all of their code: the same validation, storage access and read model. Splitting them would multiply IAM
and deployment complexity without buying anything at this scale. The handler dispatches on route and
returns `405` with the allowed methods when the method is wrong.

**6. Why DynamoDB, and what is the access pattern?**
Every read is a single-partition `Query` on a known key, which is exactly DynamoDB's strength: the city id
is the partition key, so there is no scan anywhere on the read path. The table holds the latest reading,
timestamped history samples, the weather cache, incidents and the
rate-limit windows for each city. It runs on-demand (`PAY_PER_REQUEST`) with TTL pruning old history.
*Evidence: `src/floodpulse/dynamodb_store.py`; table described at `1,772 items / 1.49 MB`, no GSI.*

**7. Why not a relational database?**
The model is a handful of item types keyed by city with a time dimension, with no joins and no
transactions spanning entities, so a relational engine would add a managed instance to pay for and
operate. DynamoDB gives the same queries with conditional writes, which is what the one-open-incident
guarantee needs. The storage layer is behind an interface, so swapping it would be a contained change.

**8. Why an HTTP API rather than a REST API?**
HTTP APIs are cheaper per request and the features this project does not need - API keys, usage plans,
request validators at the gateway - are implemented in the handler where they can be tested. Authorisation
lives in the handler (the ingestion key check), so the gateway's own auth model adds nothing here.

**9. What are the five routes, and which are public?**
`GET /zones`, `GET /zones/{zone_id}/history?minutes=N` and `GET /incidents` are public reads;
`POST /readings` requires the `x-fp-key` header; `POST /subscriptions` is public but validated and
rate-limited. History accepts 1 to 1,440 minutes and refuses anything else with a described error rather
than clamping silently. Unknown zone ids return a handler `404`, while unknown paths are answered by API
Gateway itself.

**10. How is ingestion protected?**
A shared key compared in constant time from the `x-fp-key` header, with the key held in the gitignored
`.floodpulse-key` locally and in the Lambda's environment in the cloud. Anonymous calls get
`401 missing required header 'x-fp-key'`, and the key is never printed, logged or committed - this pass
verified by digest that it appears in none of the 112 tracked files.
*Evidence: `src/floodpulse/auth.py`; `build/m18_secret_scan.py` over 16,578 quoted literals.*

**11. How does the risk model work, exactly?**
`heuristic-v1` normalises five metrics to `[0, 1]`, multiplies each by a documented weight, bounds the
contributions and sums them into a score, then classifies it against the band edges 0.35 / 0.60 / 0.80
into NORMAL, WATCH, WARNING and CRITICAL. It is fully deterministic: identical inputs always produce an
identical score and band. The dashboard can show which metric contributed most, and the Data page quotes
the weights line by line.

**12. Where do the thresholds come from?**
They are hand-set, from judgement about coastal drainage behaviour and the shape of the metrics - and the
project states plainly that they are **not** calibrated against observed floods for these cities. They
live in one place in `config.py`, they appear on the public Data page (generated from the same constants,
so they cannot drift), and they are not official warning levels.

**13. Is this a prediction? What is the difference?**
No. A prediction would attach a probability or a time to a future flood; FloodPulse describes the current
state and the recent past and maps them to a risk band. It produces no probability, no forecast horizon
and no extrapolated line, and a time-to-threshold estimate was deliberately deferred rather than faked.

**14. What is the hysteresis for, and why those numbers?**
A single noisy poll should not move a city, so an escalation requires 2 consecutive readings and a
de-escalation 3 - asymmetric on purpose, because a false all-clear is worse than a late one. Three
readings are also averaged before scoring, which damps spikes further. The effect is a score that is
stable enough to alert on.

**15. How is the score explainable?**
Each contribution is computed separately, so the response can name the dominant driver and the page can
print it in plain language next to the total. The methodology page shows the calculation number by
number, generated from the same constants the engine uses. Nothing in the score path is hidden inside a
model that cannot be opened.

**16. What does `data_classification: mixed` mean?**
It means the answer contains more than one kind of number: real provider rainfall, a value derived from
the provider's soil moisture, and simulated hydraulic values. The field rides on the payload so a client
cannot consume a mixed response as if it were uniformly real. Per-metric labels are the finer-grained
truth underneath it.

**17. What happens when the weather provider is unreachable?**
The system serves the last good cached weather, marks the data as stale beyond a threshold of three
missed refresh cycles, and says so in the payload and on the page - the poll still succeeds, because the
dashboard is more useful with clearly-labelled old data than with an error. A single city whose cache is
corrupt, foreign or future-dated degrades alone; the other eleven keep serving.
*Evidence: `STALE_READING_SECONDS = 1800`, `WEATHER_REFRESH_SECONDS = 600` in `config.py`.*

**18. How does the dashboard avoid showing data it cannot vouch for?**
It polls every 5 seconds with one request in flight at a time, and a failed read leaves the last good
values on screen with the reason stated, rather than clearing the panel or inventing a value. It never
silently falls back to the frozen demo payloads, and a late history response for a city the visitor has
left is discarded so it cannot repaint the wrong page. Retries back off instead of hammering the API.

**19. Why no framework and no build step?**
The interface is six views over one JSON document; React or a bundler would add a toolchain, a dependency
tree and an integrity surface for no functional gain. Plain modules also mean the reviewer can read the
shipped source and the deployed bytes are the repository bytes. It costs some manual DOM management,
which the harnesses test directly.

**20. What is the byte budget, and why does it exist?**
The four shipped front-end files must stay under **409,600 bytes** total, and today they total
**409,501** - 99 bytes of headroom. The ceiling was never raised; when a change needed space, code or
assets were made smaller instead. It is a discipline against unbounded front-end growth, checked by a
script rather than by memory.
*Evidence: `python build/m16_bytes.py`.*

**21. How does the temporal (trend and history) layer work?**
It analyses stored samples only: net change is last minus first inside the window, and the trend is a
least-squares slope per hour with RISING above +0.02/h, FALLING below -0.02/h and STEADY in between. The
window is anchored on the newest stored sample rather than the clock, so a quiet feed cannot produce an
empty chart. The page states the span the stored history actually covers.

**22. When does the page say INSUFFICIENT, and why does that matter?**
When a window holds fewer than 3 usable samples or spans less than 600 seconds, the honest answer is that
no direction can be described, and the page says exactly that instead of drawing a line through two
points. The same word is produced by the server and the page, so the two cannot disagree. It is the
clearest example of the project's rule that a refusal is better than a guess.

**23. How are gaps in the data handled?**
An interval longer than `max(3 × median interval, 900 s)` is treated as a gap and disclosed rather than
interpolated, and the chart shows the discontinuity. Splitting or smoothing would hide the fact that the
system was not collecting data. When history is thin, the note under the chart says how thin.

**24. What is an incident, and what is it not?**
It is a record that a city crossed this system's WARNING or CRITICAL band edge, with its severity, window,
status and band path; it escalates or closes as the city moves. It is **not** an observed flood report and
must never be presented as one. Incidents are kept indefinitely because they are small and are the
historical record.

**25. How do you guarantee only one incident is open per city?**
A single pointer item at a fixed sort key (`INCIDENT-OPEN`) is written with a conditional expression, so
concurrent readings race on one item and exactly one wins. The loser reports the stored state, skips the
alert and lets the next reading re-decide, which is safe because the state is re-derived from storage
every time. There is no lock held across a request.

**26. How do subscriptions work, and what stops abuse?**
`POST /subscriptions` validates the address shape, rejects header-injection attempts, rate-limits to three
per hour per address (fourth attempt: HTTP 429), publishes a subscription request to SNS, and replies with
a deliberately generic message. The rate-limit record stores a hash of the address, never the address
itself, and the source is tracked separately so one client cannot burn other people's budget. AWS SNS
performs the double opt-in confirmation.
*Evidence: `src/floodpulse/subscriptions.py`; `python tools/live_acceptance.py` (62 checks).*

**27. What does "alert published" actually mean?**
It means the system published a message to the SNS topic, and that is all the project claims - delivery
depends on a confirmed subscription and the subscriber's mail provider, which the system cannot observe.
Where the interface discusses alerts, it uses "published", never "delivered" or "sent".

**28. Describe the data model.**
One DynamoDB table with `pk = ZONE#<zone_id>` for per-city state, `sk = LATEST` for the current reading,
`H#<timestamp>` for history samples, `WEATHER#CACHE` for the provider response, plus incident, cooldown
and rate-limit items. TTL is enabled on the `ttl` attribute, with 7-day history retention and window-based
expiry for rate-limit counters. There is no GSI and no scan on any read path.

**29. How do you know the deployed system is the code in the repository?**
The deployment script re-uploads the function and the four dashboard objects, and a verifier compares the
deployed Lambda package to `src/` file by file, reporting byte-identical or not for all 15 files. A second
verifier re-checks the deployed page's contracts, and the front-end hashing covers the four files against
the byte ceiling. The numbers are produced by the tools, not asserted by hand.
*Evidence: `python tools/verify_deployed_code.py`, `python tools/verify_deployed_page.py`.*

**30. How is the dashboard tested without a browser test framework?**
A small shared fake DOM and fetch runtime drives the real `app.js` through 66 failure and edge scenarios,
from a first-poll failure to a half-answering fleet. On top of that, eleven harnesses check the static
logic, the runtime behaviour against the live API, the offline story, the temporal maths, and the layout
at four widths. The final layer is a real headless browser: 32 live checks plus 8 keyboard checks.
*Evidence: `tools/dashboard_dom.mjs`, `tools/dashboard_scenarios.mjs`, `tools/m16_browser_check.mjs`.*

**31. What did you do about accessibility, and what remains unresolved?**
Landmarks, one heading hierarchy, a skip link, named controls, visible focus, the incident table as a
labelled focusable scroll region, and every map marker as a link with its own accessible name. In the
final headless-browser pass the accessibility tree exposed only the on-screen markers because the map sat
outside the 463-pixel viewport in that run, and Tab did not reach the incident table within 60 stops in
that same run - both are recorded as unresolved observations rather than claims.
*Evidence: `node tools/keyboard_check.mjs <site>` (8 checks passed, both notes printed).*

**32. What are the security weaknesses you know about?**
The known one is that a meta-element content-security policy cannot enforce `frame-ancestors`, so
clickjacking protection is not claimed while the dashboard is served from an S3 website endpoint; a
CloudFront response-header policy is the fix and was not created. Beyond that: read paths are
intentionally unauthenticated (they are public data), API Gateway access logging and alarms are off, and
the ingestion key is a shared secret rather than per-client credentials. Each is a documented boundary,
not an unknown.

**33. What would you do next, and what would you do differently?**
First, calibrate the band edges against observed flooding for these cities - until then the score stays an
indicator. Then add a scheduled EventBridge refresh so sampling continues without a visitor, CloudFront
with real security headers, and CloudWatch alarms for ingestion failure. If I started again I would make
the sampling schedule independent of page visits from the beginning, because the honest thin-history
message is a consequence of that coupling, and I would set the city images' licensing before wiring the
imagery.
