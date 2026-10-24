# FloodPulse Coastal India - final technical summary

**Author:** Hatim Maula (registration 23BCE2238)
**Date:** 2026-10-24
**Live dashboard:** <http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com>
**Live API:** <https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com>
**Repository:** <https://github.com/h4timfr/floodpulse>

---

## 1. What this project is

FloodPulse is a working, serverless cloud application that monitors flood risk for **twelve selected
Indian coastal cities**. For each city it ingests weather data on a schedule, combines it with a
deterministic risk heuristic, stores the history, opens and closes incidents as the risk crosses band
edges, e-mails confirmed subscribers when a city escalates, and presents all of it in a dashboard that
labels every number with where it came from.

It is a **monitoring and risk-indication system**. It is not a flood forecast, not a validated
hydrological model, and not a warning service. That distinction is not a footnote here: it is enforced in
the code, printed on the public pages, and repeated in every artifact of the project.

## 2. The problem it addresses

Coastal Indian cities flood from a combination of intense rainfall, saturated ground and drainage that is
already full. Public weather data for these cities is available, but it is not posed as a risk question:
a resident cannot easily ask "how close is my city to trouble right now, and which factor is driving it?"
FloodPulse answers that narrow question end to end, with one honest caveat attached to every number.

The design goal was therefore not accuracy beyond what the data can support. It was **a complete,
inspectable pipeline**: real data where real data exists, clearly simulated data where it does not, a
deterministic model that can be explained line by line, and a presentation that never overstates what the
system knows.

## 3. What was built

| Layer | Implementation |
|---|---|
| Data acquisition | one batched Open-Meteo request covering all twelve cities, cached in DynamoDB with a 10-minute refresh rule |
| Ingestion | `POST /readings` (shared-secret header) for simulated readings, plus an internal live-weather pipeline |
| Validation | strict payload validation, 4,096-byte body cap, timestamp sanity and clock-skew checks, unknown fields ignored |
| Risk engine | `heuristic-v1`: bounded weighted sum over five normalised metrics, band edges 0.35 / 0.60 / 0.80 into NORMAL / WATCH / WARNING / CRITICAL, with escalation and de-escalation streaks |
| Storage | one DynamoDB table, fixed partition key per city (`ZONE#<id>`), sort keys for latest, history, weather cache, incidents and rate-limit windows; no read path scans |
| API | one Lambda behind an HTTP API: `GET /zones`, `GET /zones/{id}/history?minutes=N`, `GET /incidents`, `POST /readings`, `POST /subscriptions` |
| Incidents | automatic open / escalate / close, with an atomic one-open-incident-per-city guard |
| Alerts | SNS topic, escalation-only publishing with a per-city cooldown, public double opt-in subscriptions |
| Front end | four files (HTML, CSS, two JS) with no framework and no dependencies: Overview, Cities, City, Alerts, Data & Methodology, About |
| Operations | CloudWatch logs with 7-day retention; one idempotent deployment script; eleven verification harnesses |
| Reproducibility | 773 unit tests, an offline demo story, byte-identity checks against the deployed Lambda, and a byte budget that is enforced in the tooling |

## 4. Data provenance: the core of the honesty claim

Every metric carries its source with it - stored per record, returned per metric by the API, and printed
next to the number in the interface.

| Metric | Source | Label |
|---|---|---|
| Rainfall intensity | Open-Meteo, live | **REAL** |
| Soil saturation | derived from the provider's soil-moisture reading | **MODEL_DERIVED** |
| Drain level, water level, flow rate | deterministic hydraulic response to the real rainfall | **SIMULATED** |

There is no rain gauge, drain sensor, water-level sensor or flow meter in this project. Nothing in the
product pretends otherwise, and the Data page explains the same thing in public-facing words. The
provider does not expose an observation time for the reading, so the interface names the **fetch** time
and its age rather than claiming when the weather was observed.

## 5. The risk model, and why it is a heuristic

`heuristic-v1` is a weighted sum of five normalised measurements, each bounded, producing a score in
`[0, 1]` that is classified against three fixed band edges. It is **deterministic**: the same inputs
produce the same score and the same band, which is what makes the system testable and explainable. Two
mechanisms keep it from over-reacting: an escalation requires 2 consecutive readings and a de-escalation
3, and a message-smoothing window averages the last three readings before scoring.

What it is **not**: calibrated for these cities, validated against observed floods, or a probability of
anything. There is no training data set, no fitted parameters and no flood inventory behind those
thresholds; they are hand-set, documented as hand-set, and reachable in one place in the configuration.
The model's value is that it is transparent and complete, not that it is accurate.

## 6. Temporal analysis: describing the past, not predicting the future

The city page answers "what has this score been doing?" from stored samples only:

* the analysis **window is anchored on the newest stored sample**, not on the wall clock, so a quiet
  feed cannot produce an empty chart;
* **net change** is the last sample minus the first in the window;
* **trend** is a least-squares slope per hour, classified as RISING above +0.02/h, FALLING below
  -0.02/h, STEADY inside that band, and **INSUFFICIENT** below 3 samples or 600 seconds of span;
* a gap - an interval longer than `max(3 × median interval, 900 s)` - is **disclosed** rather than
  interpolated, so a data outage looks like an outage;
* the page states the span the stored history actually covers, so a thin window is visible instead of
  implied;
* rainfall is always presented as an **instantaneous rate (mm/h)**, never as an accumulation.

Nothing is extrapolated. There is no time-to-threshold estimate and no forecast line, because the
project refuses to show a projection it cannot defend; that feature is reported as deferred instead.

## 7. Incidents and alerts

A city at WARNING or CRITICAL opens an incident; the open incident escalates when the city moves to a
more severe band and closes when the city de-escalates. At most one incident can be open per city, and
that is enforced by a conditional write on a single pointer item, so two concurrent readings cannot both
open one.

Escalation publishes to SNS. Subscriptions are public, validated, rate-limited to three per hour per
address (fourth attempt: HTTP 429) and answered with a deliberately generic response that reveals nothing
about existing subscriptions. The rate-limit record stores a hash of the address, not the address. AWS
SNS handles the double opt-in, so only a confirmed subscriber receives anything.

Two claims are kept deliberately narrow: an incident means **this system's threshold was crossed**, not
that a flood was observed; and "published to SNS" means exactly that, never "an e-mail was delivered",
which only the subscriber's provider can establish.

## 8. Security and operational design

Ingestion is protected by a shared key held in a gitignored file and in the Lambda's environment; the key
is never printed or logged, and this pass verified by digest comparison that it appears in none of the 112
tracked files. The IAM role is scoped to one table and one topic, with no delete permission and no
wildcard resource. All external payloads are validated before use. The front end escapes all payload text,
loads images only through an explicit trusted map, and ships a content-security policy with no inline
script and no wildcard origin.

Known limitation, stated rather than hidden: because the dashboard is served from an S3 website endpoint,
the CSP travels as a meta element, so `frame-ancestors` cannot be enforced and no clickjacking protection
is claimed.

Failure behaviour is designed rather than accidental: a failed provider fetch falls back to the last good
cache and says so; a corrupt cache degrades one city instead of the fleet; a malformed payload element is
ignored rather than thrown; a failed history read keeps the last good series and states the reason;
retries back off instead of hammering the API; and API Gateway throttling (20 req/s, burst 40) bounds
abuse at the edge.

## 9. Cost

Every component is pay-per-use and nothing is always-on: static S3 hosting, request-based Lambda,
`PAY_PER_REQUEST` DynamoDB, per-publish SNS, seven-day logs. No EC2, no RDS, no Kubernetes, no NAT
gateway, no CloudFront, no provisioned capacity. **No dollar figure is claimed anywhere in this project**,
because no billing data were queried; the project states its billing model rather than inventing a spend.

## 10. Verification

Everything claimed here was measured. The verification surface, all re-run in this final pass:

| Evidence | Result |
|---|---|
| `python -m pytest -q tests` | 773 passed / 2,333 subtests |
| `python -m unittest discover -s tests -t .` | 773 tests, OK |
| `node tools/dashboard_check.mjs` | 138 checks, 144 with live payloads |
| `node tools/dashboard_run.mjs --site … --api …` | 80 runtime checks against the deployed site |
| `node tools/dashboard_scenarios.mjs` | 66 failure and edge scenarios |
| `node tools/story_check.mjs` | 29 checks on the offline demo arc |
| `node tools/temporal_check.mjs` | 141 assertion groups on the temporal maths |
| `node tools/m16_browser_check.mjs --site … --api …` | 32 checks in a real browser |
| `node tools/keyboard_check.mjs <site>` | 8 keyboard checks in a real browser |
| `node tools/layout_scan.mjs <site>` | 4 routes × 4 widths, 0 contract failures |
| `node tools/clipping_scan.mjs <site>` | 72 measured overflows, 0 hidden by an ancestor |
| `python tools/live_acceptance.py` | 62 checks against the deployed API |
| `python tools/verify_deployed_code.py` | deployed Lambda byte-identical to `src/` (15 files) |
| `python tools/verify_deployed_page.py` | every deployed page contract holds |
| `python build/m16_bytes.py` | 409,501 / 409,600 bytes, zero CRLF |
| browser smoke (this pass) | 18 live checks: 12 cities, 12 photos decoded, map, city page, alerts, Data, About, no console error |

## 11. Limitations

1. **Twelve selected coastal cities** - not national coverage, and no claim they are the twelve most
   at-risk coastal cities in India.
2. **No state, coast or national risk figure** - a city being NORMAL says nothing about its neighbours;
   coast groups are navigation only.
3. **Heuristic, uncalibrated thresholds** - a risk indicator, never a probability, never official.
4. **Simulated hydraulic values** - drain level, water level and flow rate are generated from real
   rainfall because no such sensors exist here.
5. **External provider dependency** - an outage degrades the system to its last good cache, disclosed in
   the interface.
6. **No provider observation timestamp** - fetch time and age are shown instead of an observation claim.
7. **History can be thin** - ingestion is event-driven and history expires after 7 days, so a window can
   hold few samples; the page states the span rather than drawing a misleading line.
8. **Retrospective only** - no forecast, no time-to-threshold, no extrapolation.
9. **Incident ≠ observed flood** - it records a threshold crossing by this system.
10. **Meta-element CSP** - `frame-ancestors` cannot be enforced on an S3 website endpoint.
11. **City image licensing** - the twelve photographs are used under the terms recorded in the README's
    M13 section; replace any of them whose provenance is unresolved before external reuse.
12. **Not an emergency tool** - it must never be used for real evacuation or safety decisions.

## 12. Deliberately not built

City-to-city comparison, machine-learned or statistical prediction, time-to-threshold estimates, scheduled
refresh via EventBridge, CloudWatch alarms, API Gateway access logging and detailed metrics, multi-region
or multi-account deployment, authentication for read paths, and any state- or coast-level aggregation.
Each was considered and left out to keep the system complete, honest and inexpensive; each is recorded as
an open item rather than presented as a feature.

## 13. Future work, in the order that would help most

1. **Calibration** - collect observed flood or waterlogging reports for the twelve cities and use them to
   fit and validate the band edges; until then the score stays a heuristic indicator.
2. **EventBridge scheduled refresh** - sampling every 10 minutes regardless of whether anyone has the
   dashboard open, which would give continuous history without visitor traffic.
3. **CloudFront with a response-header policy** - a real CSP header including `frame-ancestors`, plus TLS
   and caching in front of the static site.
4. **CloudWatch alarms** - page someone when ingestion stops or the error rate spikes.
5. **Real sensor ingestion** - the ingestion route already accepts external readings with per-metric
   provenance, so hardware would slot in beside the simulated values without a redesign.
