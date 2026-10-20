# FloodPulse

Live AWS-based flash-flood early-warning and risk-prediction system (cloud computing project).

**Current milestone: M4 — India live-weather integration is deployed and verified live.**
M2 remains live: a reading → API Gateway HTTP API → one Lambda → DynamoDB → read back through the
API → **an S3-hosted dashboard that polls the live API every 5 seconds**, which never invents risk
values. M3 added automatic incident records (`GET /incidents`), escalation-only SNS alerts with a
per-zone cooldown, and an incident log panel. M4 adds five **live-weather zones** (Chennai, Mumbai,
Bengaluru, Hyderabad, Kochi) fed by the external Open-Meteo service, keeps the simulator as the
demo/fallback fleet, and tags **every metric** as `REAL`, `MODEL_DERIVED` or `SIMULATED`.

**Dashboard: <http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com>**
**API: <https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com>**

Two honesty rules run through the whole project:

* **Measured vs simulated is never blurred.** In `demo` mode every reading is synthetic and every
  record says `simulated: true` / `data_classification: "simulated"`. In `live` mode rainfall and soil
  saturation are the weather provider's values (`REAL` / `MODEL_DERIVED`) while drain level, water
  level and flow rate stay `SIMULATED`, because no real sensor for them exists; those responses are
  `data_classification: "mixed"` and carry a per-metric `metric_sources` map.
* **Risk scores are always a heuristic.** `heuristic-v1` is deterministic and explainable — *not* a
  validated hydrological model, *not* a forecast and *not* machine learning, and it is not calibrated
  for the Indian cities. It is never presented as an official flood warning.

---

## Architecture status

| Layer | Component | Status |
|---|---|---|
| Simulator | Deterministic phase-driven sensor simulator (demo fleet) | ✅ implemented (`src/floodpulse/simulator.py`) |
| Live weather | Open-Meteo client: one request for all 5 Indian cities, DynamoDB cache, 10-minute refresh rule | ✅ **live** (`src/floodpulse/weather.py`, `live_weather.py`) |
| Provenance | Per-metric `REAL` / `MODEL_DERIVED` / `SIMULATED` tags in storage, API and dashboard | ✅ **live and verified** |
| Ingest | Single Lambda handler, `POST /readings`, shared-secret protected | ✅ **live** on API Gateway |
| Validation | Strict payload validation, server-side epoch/TTL | ✅ implemented |
| Risk engine | Deterministic `heuristic-v1` + explainability + hysteresis | ✅ implemented |
| Storage | In-memory store + DynamoDB store (same interface) | ✅ **live** against table `floodpulse` |
| API | `GET /zones`, `GET /zones/{zone_id}/history`, `GET /incidents` (public reads) | ✅ **live** in the same handler |
| AWS deployment | DynamoDB / Lambda / HTTP API / S3 site / SNS topic | ✅ **deployed and verified** (`infra/deploy.ps1`) |
| Dashboard | S3-hosted static UI polling the live API, incident log panel, provenance chips, city nightscape cards | ✅ **live and verified** (`web/`, deployed 2026-10-18) |
| Incidents | Automatic incident records + lifecycle (open/escalate/close) | ✅ **live and verified** |
| Alerts | One SNS topic, escalation-only publishing with a per-zone cooldown | ✅ **live and verified** (publish proof; no subscriber yet) |
| Time-to-threshold | Estimate time to a threshold | ⛔ **deferred** (still reported as a deferred feature, never faked) |
| ML | Any learned model | ⛔ out of scope until the deterministic system works end to end |

Locked MVP decisions reflected in the code: **one** Lambda handling all routes,
**one** Lambda execution role, **no** aggregate snapshot item (a `LATEST` record per zone
plus timestamped history records), client payload carries `timestamp` only, `sensor_node_id`
(not `device_id`).

## M1 AWS slice (deployed, verified)

Deployed resources (profile `cloud-project`, region `eu-north-1`, account `725934295966`):

| Resource | Identifier | Configuration |
|---|---|---|
| DynamoDB table | `floodpulse` | `pk`(S) HASH + `sk`(S) RANGE, `PAY_PER_REQUEST`, TTL ENABLED on `ttl` |
| Lambda function | `floodpulse-api` | python3.13, 256 MB, **15 s** (raised from 10 s in M4: a weather fetch plus five ingests measured 2.9-6.9 s), handler `floodpulse.handler.lambda_handler`, env `FLOODPULSE_STORE=dynamodb`, `FLOODPULSE_TABLE=floodpulse`, `FLOODPULSE_INGEST_KEY` (set, never displayed), `FLOODPULSE_SNS_TOPIC_ARN`, `FLOODPULSE_ALERT_COOLDOWN_SECONDS=600`, and the M4 vars `FLOODPULSE_ZONE_MODE=live`, `FLOODPULSE_WEATHER=open-meteo`, `FLOODPULSE_WEATHER_REFRESH_SECONDS=600`, `FLOODPULSE_WEATHER_TIMEOUT_SECONDS=4`. **`VpcConfig` is empty** (no VPC), so the function already has outbound HTTPS egress for the weather provider |
| Lambda execution role | `floodpulse-lambda-role` | trust `lambda.amazonaws.com`; `AWSLambdaBasicExecutionRole`; inline `FloodPulseDynamoDBAccess` = `dynamodb:GetItem/PutItem/UpdateItem/Query` on `table/floodpulse` only; inline `FloodPulseSnsPublish` (M3) = `sns:Publish` on the one alert topic ARN only. **Created and edited by the project owner** (`iam:PutRolePolicy` is denied for the `cloud-project` user); the deploy script only *reads* the role's policy list |
| HTTP API | `floodpulse-api` (`2rg2u8quzd`) | `$default` stage, auto-deploy, integration `tcdtda1` (AWS_PROXY, payload format 2.0) |
| Routes | | `POST /readings` (requires `x-fp-key`), `GET /zones`, `GET /zones/{zone_id}/history`, `GET /incidents` (M3) |
| CORS | | origins **restricted** to the dashboard website origin + `http://localhost:8000`; methods `GET,POST,OPTIONS`; headers `content-type,x-fp-key`; max age 300 (verified live for `/incidents` too) |
| SNS topic | `floodpulse-alerts` | `DisplayName = FloodPulse Alerts`, standard topic, **0 subscriptions** (`SubscriptionsConfirmed = 0`) |
| API Gateway invoke permission | `floodpulse-apigateway-invoke` | `apigateway.amazonaws.com`, scoped to `arn:aws:execute-api:eu-north-1:725934295966:2rg2u8quzd/*/*` |
| CloudWatch Logs | `/aws/lambda/floodpulse-api` | retention **7 days** |
| S3 dashboard bucket | `floodpulse-dashboard-725934295966` | static website hosting (`index.html`), public read of objects only (no list, no write), ~85 KB of assets |

Live endpoint: `https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com`

Item model (one table, no GSI, no scan — `LATEST` per zone plus append-only history, extended in M3):

| `sk` | purpose | TTL |
|---|---|---|
| `LATEST` | newest reading + risk for a zone, overwritten each time | 7 days (`ttl`), refreshed on every write: a zone that stops reporting disappears rather than sitting on the board for ever |
| `H#<iso timestamp>` | history point; duplicate timestamps get `#2`, `#3`, … via a conditional write | 7 days (`ttl`) |
| `INCIDENT#<iso timestamp>` | one flood incident (M3), oldest→newest sorted, kept as history | none |
| `ALERT#COOLDOWN` | per-zone alert cooldown marker with the last SNS `MessageId` per severity (M3) | none |

### Verified acceptance run

| Check | Result |
|---|---|
| Local test suite | `python -m unittest discover -s tests -t .` → **153 tests, OK** |
| `POST /readings` (simulated calm reading) over real HTTPS | **202** — `risk_score 0.1703`, `risk_class NORMAL`, `sequence 1`, `stored.latest_sk LATEST`, `stored.history_sk H#2026-10-15T07:37:23Z`, full `explanation` |
| `GET /zones` after that write | **200** — `zone-riverside-north` `live`, `age_seconds 0`, drivers + explanation; other two zones `no_data`; `highest_risk_zone` populated |
| `GET /zones/zone-riverside-north/history?minutes=60` | **200** — 1 point, served from DynamoDB |
| AWS CLI `get-item` / `query` (independent of the API) | `LATEST` + `H#2026-10-15T07:37:23Z` both stored, numbers as DynamoDB numbers, `simulated: true`, `ttl` set |
| `POST /readings` with `rainfall_mm_per_hr = 999` | **400** `{"error":"invalid_payload","field":"readings.rainfall_mm_per_hr", …}` |
| `POST /readings` with `simulated: false` | **400** `{"error":"invalid_payload","field":"simulated", …}` |
| `GET /zones/zone-atlantis/history` | **404** `{"error":"unknown_zone", …}` plus the known zone list |

### Deploying / re-deploying

```powershell
# idempotent: creates only what is missing, never deletes, IAM stays read-only
powershell -ExecutionPolicy Bypass -File infra/deploy.ps1

# API + Lambda only, no dashboard upload
powershell -ExecutionPolicy Bypass -File infra/deploy.ps1 -SkipDashboard
```

The script needs the execution role to exist (it never creates or edits IAM). It builds
`build/floodpulse-api.zip`, so deploying a code change is simply a re-run. Re-running it against the
current stack was verified as a **no-op**: every resource reported as already present.

Since M2 the script also: creates the ingestion secret if `.floodpulse-key` does not exist yet
(`tools/init_ingest_key.py`, value never displayed), applies the Lambda environment from the
gitignored `build/env-prod.json`, restricts CORS to the dashboard origin, and creates/uploads the
S3 dashboard. It **never prints the secret** — only the environment variable names.

### Known M1 limitations (deliberate, tracked for M2)

* ~~`POST /readings` is not yet authenticated~~ → **resolved in M2**: the endpoint requires the
  `x-fp-key` header (see the M2 section). Validation and the `simulated: true` rule were already in
  place and remain.
* ~~CORS is `*`~~ → **resolved in M2**: restricted to the dashboard origin (+ optional localhost).
* Every response and stored record stays labelled `simulated` (permanent, by design).

## M2 live dashboard (deployed, verified)

**<http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com>**

Vanilla HTML/CSS/JS served from S3 — no framework, no build step, no npm, no CDN, no remote font or
image, **no fake data**. The page polls `GET /zones` every 5 s and `GET /zones/{id}/history` every
30 s, and renders whatever AWS returns: risk score, band, per-metric values and thresholds, weighted
contributions, the top drivers, the engine's own explanation, reading age and the zone ranking.

| File | Purpose |
|---|---|
| `web/index.html` | layout, permanent "SIMULATED DATA" banner, `window.FLOODPULSE_API_BASE` (override with `?api=`) |
| `web/styles.css` | storm/command-centre theme, glass cards, band colours, responsive breakpoints |
| `web/lib.js` | pure render/format helpers (`window.FP`) — also loadable in node for checks |
| `web/app.js` | polling controller, DOM patching, session sparkline, canvas history chart |
| `web/bg-storm.webp` | the storm backdrop: a 930×312 lossy WebP, 58.8 KB, painted by `.bg-layer` at 42% opacity |

Behaviour under failure is deliberate: a failed poll keeps the **last good** payload on screen,
marks the connection stale/disconnected, and never fabricates a value. History points that are
`no_data` render an explicit empty state rather than a guess.

### Ingestion security

| Control | Implementation |
|---|---|
| Shared secret | Lambda reads `FLOODPULSE_INGEST_KEY` from its environment; the client sends it in `x-fp-key` |
| Safe comparison | `hmac.compare_digest` on UTF-8 bytes; no length or value is echoed back |
| Fail closed | With `FLOODPULSE_STORE=dynamodb` and no configured secret, `POST /readings` returns **503** `ingest_not_configured` instead of accepting data |
| Local mode | With the in-memory store and no secret, ingestion stays open so the M0/M1 tests and `tools/local_demo.py` keep working |
| Reads stay public | `GET /zones` and history never require or return the secret (the browser dashboard holds no key) |
| Secret hygiene | The key lives only in `.floodpulse-key` and `build/env-prod.json` (both gitignored) and is never printed, logged, committed or sent to the browser |
| Rotation | `python tools/init_ingest_key.py --force --key-file .floodpulse-key --lambda-env build/env-prod.json` mints a new key and prints only a `sha256:` fingerprint; the deploy re-applies the environment. **Rotation history, oldest first:** `sha256:6ba7227085ac` → `sha256:b3b91ff07129` (rotated in M3 after the first value appeared in a terminal transcript) → `sha256:7c130a215cc1` (the value deployed today). Only fingerprints are recorded here: the values themselves exist solely in the two gitignored files |
| Key drift | The deployed environment is the file the operator owns, so a rotation that updated only `.floodpulse-key` would be silently undone by the next deploy. `infra/deploy.ps1` therefore compares the two and **refuses to run** unless `-AllowKeyDrift` is passed; it reports the two fingerprints, never the values |

### Verified M2 acceptance run

| Check | Result |
|---|---|
| Local test suite | `python -m unittest discover -s tests -t .` → **181 tests, OK** (153 M1 + 18 auth + 10 tooling) |
| Dashboard static checks | `node tools/dashboard_check.mjs` → **23 checks pass** (pure helpers, real-payload rendering, escaping, no POST/no key/no CDN, element-id and `data-f` wiring, asset sizes) |
| Same checks against a **live** captured payload + live history | **24 checks pass** (`--payload build/zones-live-critical.json --history build/history-live.json`) |
| `POST /readings` with no `x-fp-key` | **401** `{"error":"unauthorized","detail":"missing required header 'x-fp-key'"}` |
| `POST /readings` with a wrong key | **401** `invalid ingestion key` |
| `POST /readings` with the real key | **202**, full risk payload (no secret echoed in any response body) |
| `GET /zones` and `/history` with no key | **200** — reads stay public, secret never appears in the body |
| Storm scenario against the live API | **180/180 accepted, 0 failures**; runtime ~194 s; all three zones peaked at **CRITICAL 1.0000** |
| Progression observed through `GET /zones` while the storm ran | NORMAL 0.17 → WATCH 0.44 → WARNING 0.69 → CRITICAL 1.00, with hysteresis visible (`risk_class_candidate` ahead of the effective class) |
| DynamoDB proof via the AWS CLI (independent of the API) | `zone-riverside-north` 3 → **44** records, `zone-old-town-canal` 0 → **43**, `zone-harbor-east` 0 → **45**; each `LATEST` shows `risk_class CRITICAL`, `ttl` set, `simulated: true` |
| Live history endpoint | 52 points over 60 min: NORMAL 26, WATCH 4, WARNING 3, CRITICAL 19; scores 0.1692 → 1.0000 |
| CORS preflight from the dashboard origin | **204** with `access-control-allow-origin: http://floodpulse-dashboard-…` and `allow-headers: content-type,x-fp-key` |
| CORS preflight from `http://evil.example` | **204** with **no** `access-control-allow-origin` → the browser blocks it |
| Deployed site vs local build | served `app.js` byte-identical to `web/app.js` (sha256 match); `/`, `index.html`, `styles.css`, `lib.js`, `app.js`, `bg-storm.webp` all **HTTP 200** with correct content types |
| `infra/deploy.ps1` re-run | no-op: every resource reported "already exists", no destructive change |

**Honest scope note:** no browser is available in this environment, so the visual result was not
inspected by eye. What *was* verified: the served files, the dataset contract, the render helpers
and card/history markup driven by real captured AWS payloads, the element-id/`data-*` wiring between
`app.js` and `lib.js`/`index.html`, CORS from the real origin, and WCAG contrast ratios computed for
every text and band colour (all ≥ 4.72:1 on text, ≥ 3.21:1 for graphics) — see
`tools/make_background.py --preview`.

### Reproducing the live demo

```powershell
# leave fresh calm readings so the dashboard shows live data
python tools/live_simulator.py --url https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com --interval 1.0 --steps 12

# full storm: calm -> storm ramp -> flash flood -> recession (about 3 minutes), pressure the UI
python tools/live_simulator.py --url https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com --interval 1.0 --steps 60

# stop as soon as any zone escalates to CRITICAL
python tools/live_simulator.py --url https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com --interval 1.0 --steps 60 --stop-at CRITICAL
```

Open the dashboard while it runs: cards turn amber → orange → red within ~30 s of the ramp.
The simulator reads the secret from `.floodpulse-key` (or `FLOODPULSE_INGEST_KEY`) and never prints it.

### M2 limitations (next milestone candidates)

* Readings are still only produced by `tools/live_simulator.py`; nothing runs on a schedule, so the
  dashboard shows **stale/disconnected** once a run ends (by design, and clearly labelled).
* No time-to-threshold estimate yet — deliberately deferred until after this dashboard, which is now
  done.
* The S3 website endpoint is HTTP-only (no CloudFront, to stay effectively free).

## M3 incidents and SNS alerts (deployed, verified)

M3 adds real flood-incident tracking and one SNS alert topic, without changing what M1/M2 already do.

### Incident rules (as implemented)

| Rule | Behaviour |
|---|---|
| Open | A zone opens **exactly one** incident when its **hysteresis-filtered** class reaches WARNING or CRITICAL (so `ESCALATION_STREAK` still governs it) |
| No duplicates | Only one OPEN incident per zone; `create_incident()` uses a conditional write (`attribute_not_exists(pk)`), so two concurrent requests cannot both open one. A zone that reaches WARNING again while already open just updates the existing record |
| Escalate | WARNING → CRITICAL on the **same** incident: `severity` is raised, `severity_changes` gets an entry, `alert_count` grows. Severity is never downgraded inside an incident |
| Stay open | WATCH (or any non-NORMAL class) keeps the incident OPEN — that is still an elevated state |
| Close | Only once the zone is back to NORMAL per the existing `DEESCALATION_STREAK`, then `closed_at`, `duration_seconds` and `close_reason` are set |
| Silence never closes | A zone that stops reporting leaves its incident OPEN for ever: closing needs a NORMAL reading, and no reading is not NORMAL. The board marks such a row ("zone not reporting") using the zone ages it already holds, and `/zones` lists the zone in `stale_zone_ids`; the incident is not closed for it, because nothing has been measured |
| History | Closed incidents are kept forever (no TTL) and returned by the API newest first |
| Tracking failures | Incident/alert errors are caught: the reading still returns **202** with `incident.action = "tracking_failed"`, so ingestion never breaks because of alerting |

Incident records carry `incident_id` (`inc-<zone>-<UTC stamp>`), `zone_id`, `zone_name`, `severity`,
`opened_at`, `current_status`, `risk_score_at_open`, `top_driver`, `simulated: true` and
`data_classification: "simulated"`, plus the escalation/alert bookkeeping.

### Alerting rules

* **One** SNS topic: `floodpulse-alerts` (created in `eu-north-1`; **0 subscriptions — pending**).
* Published **only** on a WARNING/CRITICAL escalation, never on every reading: an incident opening at
  WARNING sends one alert, reaching CRITICAL sends one more, and the rest of the storm is silent.
* **Cooldown** per zone *and* severity (`FLOODPULSE_ALERT_COOLDOWN_SECONDS`, default 600 s) stored
  outside the incident (`sk = ALERT#COOLDOWN`), so a repeated simulator run cannot spam subscribers
  and the cooldown survives close/reopen cycles.
* A suppressed alert is not even attempted; a **failed** publish is recorded on the incident
  (`published: false`) and **does not** advance the cooldown, so the next escalation retries.
* The topic ARN is never sent to the browser: the API exposes only
  `alerting: { enabled, severities, cooldown_seconds }`, and AWS error strings are redacted
  (`arn:aws…` → `<alert-topic>`) before they are reported.

### Data model additions (same table, no GSI, no scan)

| `sk` | purpose | TTL |
|---|---|---|
| `INCIDENT#<iso timestamp>` | one incident record (sorted chronologically, so the newest is a `Limit=1` reverse query) | none — kept as history |
| `ALERT#COOLDOWN` | per-zone alert cooldown marker: last alert time + `MessageId` per severity | none |

`GET /incidents?limit=50` (max 200, optional `zone_id`) queries those items per zone, merges and sorts
them newest first. The route reuses the existing Lambda and table and is a **public read**;
`POST /readings` keeps its `x-fp-key` protection and there is no client-side write route.

### Dashboard incident panel

A read-only panel between the zone cards and the risk chart: severity, zone, opened time, status,
a duration that ticks live for OPEN incidents, plus score/top-driver/alert facts. It polls
`GET /incidents?limit=20` every 10 s **in addition to** the existing 5 s `/zones` poll, patches only
the duration text between polls, keeps the last good list when a request fails (never inventing an
incident), and shows the honest state of alerting ("not configured yet" / "armed, max 1 per zone and
severity every 10 min"). No part of the storm-blue design was changed.

### Status of this milestone (verified)

| Item | State |
|---|---|
| Incident engine, storage, API route, notifier, dashboard panel | ✅ implemented and covered by unit tests |
| Local test suite | ✅ **250 tests** pass (`python -m unittest discover -s tests -t .`) — the pre-M4 baseline, unchanged by M4 (it is 333 after M4) |
| Dashboard harness | ✅ **36 + 7 checks** pass against the live payloads (`tools/dashboard_check.mjs` for the sources, `tools/dashboard_run.mjs` for actually running them) |
| SNS topic `floodpulse-alerts` | ✅ live in `eu-north-1`, `DisplayName = FloodPulse Alerts` |
| `sns:Publish` for the Lambda role | ✅ inline `FloodPulseSnsPublish` attached by the project owner, scoped to that one topic ARN |
| Lambda code, `GET /incidents` route, dashboard upload | ✅ deployed and verified live |
| Ingestion key | ✅ **rotated** (fingerprint `sha256:6ba7227085ac` → `sha256:b3b91ff07129`) before this deploy, because the earlier value had appeared in a terminal transcript; only a fingerprint is ever printed |
| Email/SMS subscription | ⚠️ **not present**: `list-subscriptions` returns `[]` in every region and the topic reports `SubscriptionsConfirmed = 0`, so publishes succeed (verifiable by `MessageId`) but **nobody is notified** |

Redeploying (idempotent; never deletes anything, never edits IAM — it only *checks* the role policy):

```powershell
powershell -ExecutionPolicy Bypass -File infra\deploy.ps1
# creates/uses the SNS topic, adds FLOODPULSE_SNS_TOPIC_ARN + FLOODPULSE_ALERT_COOLDOWN_SECONDS to the
# gitignored env file (values are never printed), applies the Lambda code + environment, adds the
# GET /incidents route and uploads the dashboard. If iam:PutRolePolicy is denied it says so and continues.
```

To subscribe an address (the subscriber must confirm the AWS email):

```powershell
aws sns subscribe --topic-arn arn:aws:sns:eu-north-1:725934295966:floodpulse-alerts `
  --protocol email --notification-endpoint <address> --profile cloud-project --region eu-north-1
```

### Verified M3 acceptance run (live, 2026-10-15)

| Check | Result |
|---|---|
| Ingest rebuilt and protected | `POST /readings`: no `x-fp-key` → **401**, wrong key → **401**, rotated key → **202** |
| Public reads still work | `GET /zones` **200**, `GET /zones/zone-riverside-north/history?minutes=60` **200**, `GET /incidents?limit=20` **200** |
| Real storm through the live stack | `tools/live_simulator.py --steps 100 --interval 1.0` → **300 accepted, 0 rejected, 0 failed**, all three zones peaked at **1.0000 CRITICAL** and returned to NORMAL |
| Incidents created | **3** (one per zone), each `severity=CRITICAL`, `severity_at_open=WARNING`, `escalation_count=1`, `current_status=CLOSED`, durations 67/69/68 s, `top_driver=water_level_m` |
| No duplicate incidents during a storm | exactly one `INCIDENT#…` item opened per zone for a whole storm (each zone's whole arc produced one incident) |
| Alerts published only on escalation | CloudWatch `AWS/SNS NumberOfMessagesPublished = 6.0` = 3 zones × (WARNING + CRITICAL) — nothing for the other ~290 readings |
| Publish really succeeded | the `ALERT#COOLDOWN` marker stores the returned SNS `MessageId`s: WARNING `18b0fd5d-e6b6-59e2-922b-9e16e06e4ca7`, CRITICAL `3664fbc6-d588-5078-9aef-988dd36bed6e` |
| Duplicate alert suppressed | a second storm for the same zone, inside the cooldown: a **new** incident opened (`inc-riverside-north-20261015T165728Z`) and escalated, with **0 published** and **2 suppressed** (`reason=cooldown`, `cooldown_remaining_seconds` 249 s then 257 s); the publish metric stayed at **6.0** |
| Incident history retained | the incident item has no `ttl`; `GET /incidents` returns 4 incidents (3 CLOSED + 1 OPEN) newest first |
| Dashboard live | `index.html` **200**, contains the incident panel ids; `lib.js`/`app.js`/`styles.css`/`bg-storm.webp` served byte-for-byte identical to the local files; CORS preflight from the dashboard origin returns it verbatim; no `arn:aws` anywhere in the site |
| `/zones` unchanged | **200**, 3 zones, all `live`, ranking `[riverside-north, old-town-canal, harbor-east]`, highest risk zone reported |
| Lambda health | no `ERROR`/`Exception`/`Traceback` lines in `/aws/lambda/floodpulse-api` across both storms |
| Key rotation took effect | the deployed function now carries the new `FLOODPULSE_INGEST_KEY` (compare against the old fingerprint; the value is never printed) |

Not verifiable from here, and stated as such: the dashboard was never opened in a real browser (no
browser on this machine), so its rendering is verified by the node harness against the captured live
payloads plus the live HTML/asset fetches above — not by looking at pixels. Email delivery is also
unverified because the topic has no subscription.

### Dashboard defect found in the browser and fixed: `renderRanking is not defined`

| | |
|---|---|
| Symptom | The browser console reported `renderRanking is not defined`, the **Risk ranking** panel kept its static "waiting for data…" placeholder, and the page fell into the **STALE** state even though every other panel showed live data |
| Root cause | `web/app.js` called its own `renderRanking(state.zones)` at the end of `applyPayload()`, but no such local function was ever defined — the implementation only exists in `web/lib.js` as `FP.renderRanking`. The `ReferenceError` was thrown inside the `/zones` promise chain, so the shared `.catch()` handler set `state.lastError` and repainted the badge as STALE; the panels rendered before that line (summary, hero, cards) were already filled, which is exactly the reported behaviour |
| Introduced by | M2 (commit `2666bf7`), not M3 — `git show 2666bf7:web/app.js` has the same call and no definition; the M3 incident work did not touch it |
| Why the checks missed it | `dashboard_check.mjs` verified element ids, `data-*` attributes and lib.js field selectors, but never that a function app.js *calls* is actually defined somewhere — a static check that only looks at markup cannot see this class of bug |
| Fix | 9 lines in `web/app.js`: the missing local wrapper that writes `FP.renderRanking(zones)` into `#ranking`, keeping the existing placeholder when there are no zones. No backend, API, markup or style change; `lib.js` was already correct and needed no edit |
| Guard | `tools/dashboard_check.mjs` now fails if app.js calls any function that is neither defined in app.js nor namespaced with `FP.`; `tools/dashboard_run.mjs` executes the real assets in a small fake DOM against a real payload and asserts the ranking renders every zone and the page stays LIVE across repeated polls. Verified as a negative control: the runner fails against the pre-fix `app.js` with the browser's exact message |
| Verified after the fix | The single changed asset (`app.js`) was uploaded, the deployed file is byte-identical to the local one (25,891 bytes, new ETag), and the runner fetched **the deployed** `lib.js` + `app.js` from S3 and ran them against **live** API payloads: ranking rendered for all 3 zones (`Riverside North 1.00 CRITICAL`, `Old Town Canal 0.24 NORMAL`, `Harbor East 0.22 NORMAL`) and the badge stayed `LIVE` across three polls |

Still stated honestly: this verification executes the deployed JavaScript in a VM with a stub DOM —
it proves no runtime error, correct ranking markup and correct connection state, but it is not a
substitute for a human opening the page in a real browser.

### M3 limitations (known, deliberate)

* Alerting is publish-proof only until somebody subscribes an email address to the topic.
* No retry queue: a publish that fails is logged on the incident and retried at the *next* escalation,
  not immediately.
* Incidents are per zone; there is no cross-zone "event" that groups simultaneous incidents.
* No incident acknowledgement, assignment or operator notes (read-only log by design).

## M4 live weather for Indian cities (deployed, verified)

M4 adds a second kind of zone: five **Indian cities** whose rainfall and soil moisture come from a
real external provider (Open-Meteo). The simulator is still there — it is now the *fallback/demo*
fleet, selected with one environment variable.

### Zone modes

| `FLOODPULSE_ZONE_MODE` | Fleet | Metrics |
| --- | --- | --- |
| `live` (aliases: `live_weather`) | Chennai, Mumbai, Bengaluru, Hyderabad, Kochi | rainfall + soil moisture **REAL/MODEL-derived**, hydraulics **SIMULATED** |
| `demo` (aliases: `simulated`) | `zone-riverside-north`, `zone-old-town-canal`, `zone-harbor-east` | everything **SIMULATED** |

`demo` is also the library default, so every pre-M4 caller and test keeps its exact behaviour; only
the deployment switches to `live`. An unknown or misspelled value falls back to `demo`, so a typo can
never point the deployment at the network by accident.

| Zone | Coordinates | Country |
| --- | --- | --- |
| `city-chennai` | 13.0827, 80.2707 | IN |
| `city-mumbai` | 19.0760, 72.8777 | IN |
| `city-bengaluru` | 12.9716, 77.5946 | IN |
| `city-hyderabad` | 17.3850, 78.4867 | IN |
| `city-kochi` | 9.9312, 76.2673 | IN |

### What is real and what is not (per metric)

Every metric of every reading carries a provenance tag, and the dashboard renders it as a chip:

| Metric | Tag | Origin |
| --- | --- | --- |
| `rainfall_mm_per_hr` | `REAL` | Open-Meteo hourly precipitation, passed through unchanged |
| `soil_saturation_pct` | `MODEL_DERIVED` | Open-Meteo soil moisture (m³/m³) converted by FloodPulse to a 0-100 % saturation figure |
| `drain_level_m` | `SIMULATED` | deterministic first-order hydraulic response to the real rainfall + soil moisture |
| `water_level_m` | `SIMULATED` | same response (coupled to the drain level) |
| `flow_rate_m3_s` | `SIMULATED` | same response (coupled to the water level + rainfall) |

`REAL` means "the external provider's value, passed through as delivered", **not** "certified
measurement": Open-Meteo precipitation is a model/observation blend at a grid point, and its hourly
figure covers the preceding hour, so it can be up to ~60 minutes old. There is no rain gauge, drain
sensor, water-level sensor or flow meter anywhere in this project, and the five Indian cities have no
FloodPulse hardware at all.

Hydraulics are deterministic and explainable (no randomness), so a demo replays identically:

```
target_drain = 0.15 + 2.35 * clamp(rain / 45 mm/h, 0, 1) + 0.80 * soil_fraction
drain_now    = drain_prev + (target_drain - drain_prev) * (1 - exp(-dt / 900 s))
water_now    = clamp(1.15 * drain_now + 0.25 * soil_fraction)
flow_now     = clamp(2.00 * water_now + 0.05 * rain)
```

Constants live in `config.py` (`HYDRAULICS_*`) and are reported in the API as `hydraulics_note`.

### Caching and the "at most every 10 minutes" rule

* One HTTP request covers **all five** cities (comma-separated coordinates), so a provider blip
  affects all five zones at once and never just one.
* Each zone's provider snapshot is cached in the **existing** table at `sk = WEATHER#CACHE`
  (overwritten, no TTL, no GSI, no new table).
* `GET /zones` refreshes only the zones whose cache is missing or whose **last provider attempt** was
  at least `FLOODPULSE_WEATHER_REFRESH_SECONDS` (default **600 s**) ago. Every other poll is served
  from the cache, so refreshing is bounded by time, not by how often the dashboard polls.
* The window is anchored on the *attempt*, not on success: a failed fetch writes a small marker
  (`last_attempt_epoch`, `last_attempt_ok`, `last_attempt_error`) that keeps the provider values
  untouched while moving the anchor forward. A provider outage therefore costs **one call per
  10 minutes**, not one call per dashboard poll — verified live, and covered by a test that runs 119
  polls inside one window and asserts a single call.
* `FLOODPULSE_WEATHER=off` is a kill switch: the Lambda then never calls the provider.

| `sk` | purpose | TTL |
| --- | --- | --- |
| `WEATHER#CACHE` | newest provider snapshot for a zone: rainfall, soil moisture, observation time, fetch time, derived metrics, plus last-attempt bookkeeping | none (overwritten) |

Degradation is explicit and never invents data: a provider failure is reported in
`weather.last_refresh.error` and per zone in `weather.last_attempt_error`, cached values keep being
served with their true `cache_age_seconds`, the zone card shows `fetch failed`, and a zone that has
never had a successful fetch reports `status: no_data`. Each attempt also writes one structured line
to CloudWatch (`event: weather_refresh`) so a provider problem stays diagnosable after the response
that reported it has scrolled away.

### Live readings use the existing pipeline

A live-weather reading is ingested through the same validation → scoring → storage → incident →
alerting path as a simulated one, so hysteresis, incidents, SNS alerts, history and the dashboard all
work unchanged. The reading is timestamped when FloodPulse sampled it, while the provider's own
observation time is kept alongside it (`provider_observed_at`), and `data_classification` becomes
`mixed` once a fleet contains real data.

### Configuration

| Lambda environment variable | Default | Meaning |
| --- | --- | --- |
| `FLOODPULSE_ZONE_MODE` | `demo` (`live` when deployed) | which fleet is active |
| `FLOODPULSE_WEATHER` | `open-meteo` | `open-meteo` or `off` (kill switch) |
| `FLOODPULSE_WEATHER_REFRESH_SECONDS` | `600` | minimum gap between provider attempts per zone; clamped to 60-86400 |
| `FLOODPULSE_WEATHER_TIMEOUT_SECONDS` | `4.0` | provider HTTP timeout; clamped to 1-8 s (the Lambda itself runs with a 15 s timeout, raised from 10 s so a slow provider fetch plus the five ingests cannot hit the limit) |

No API key is required, and none is stored: Open-Meteo's free tier is key-less. Attribution
(CC BY 4.0) is required and is included in the API response (`weather.attribution`), in the dashboard
footer and in this document.

### AWS changes for M4

* Lambda **code** + four new environment variables (same function, same role, same table).
* DynamoDB: a new **data-only** item type `sk = WEATHER#CACHE` in the existing `floodpulse` table —
  no schema change, no GSI, no TTL change, no new permissions needed.
* **No new AWS resources**: no VPC, no NAT, no EC2, no RDS, no IoT Core, no ML, no new database.
* The function has no VPC configuration, so it already has outbound internet egress for HTTPS.
* IAM untouched: `dynamodb:GetItem/PutItem` on the existing table and `sns:Publish` on the one alert
  topic were already enough.
* Cost: ≤1 provider request per 10 min (~144/day, free tier, no key), ~1,440 tiny DynamoDB writes/day
  (~$0), Lambda shape unchanged (256 MB, 10 s) → effectively **$0/month**; the added latency is one
  HTTPS call per 10 minutes.

### Verified M4 acceptance run (live, 2026-10-15)

| Check | Result |
| --- | --- |
| Real provider feed (read-only probe, no AWS) | `python tools/weather_probe.py` → **1 request for all 5 cities**, HTTP 200, no API key: Chennai 0.4 mm/h + 64.8 % soil, Mumbai 0.0 + 16.8 %, Bengaluru 0.4 + 60.6 %, Hyderabad 0.0 + 15.6 %, Kochi 0.3 + 28.0 % |
| Local test suite | `python -m unittest discover -s tests -t .` → **333 tests, OK** (250 pre-M4 + 83 new), no network access |
| Dashboard static checks | `node tools/dashboard_check.mjs` → **44 checks pass** on the demo payload and on the live payload (provenance, mode-driven banner, "never upgrade to REAL", feed age/failure notes, site safety) |
| Dashboard runtime checks | `node tools/dashboard_run.mjs` → **8 checks pass** in both modes, including the provenance check |
| Deployment | `infra/deploy.ps1` re-run: code updated, environment applied with the 4 new variables, timeout corrected 10 s → **15 s**, every other resource reported "already exists" — **no new AWS resources**, IAM untouched |
| `GET /zones` after deploy | **200**, `zone_mode: live_weather`, `data_classification: mixed`, `simulated: false`, `zone_count: 5`, ranking covers all five cities in descending risk |
| Live weather actually ingested | all five zones `status: live` with scored readings: Chennai 0.2245 NORMAL (1.1 mm/h), Kochi 0.1824 (3.1 mm/h), Bengaluru 0.1683, Mumbai 0.0677, Hyderabad 0.0654 — **different real values per city**, not one shared figure |
| Provenance in the API | every reading: `rainfall_mm_per_hr … REAL`, `soil_saturation_pct … MODEL_DERIVED`, `drain_level_m`/`water_level_m`/`flow_rate_m3_s … SIMULATED`; `weather.attribution` present |
| Provenance in DynamoDB (independent `get-item`) | `ZONE#city-chennai / WEATHER#CACHE`: `record_type WEATHER_CACHE`, `provider open-meteo`, grid point `13.110721, 80.2459` (nearest cell, not the requested coordinate), `observed_at 2026-10-15T18:00:00Z`, `rainfall_mm_per_hr 0.4`, `soil_moisture_m3m3 0.324`, derived metrics, full `metric_sources` map |
| Same for the reading itself | `ZONE#city-chennai / LATEST`: `simulated false`, `data_classification mixed`, `source open-meteo-live`, `provider_observed_at 2026-10-15T18:00:00Z`, `sensor_node_id open-meteo-chennai`, `risk_class NORMAL`, `ttl` set |
| Caching rule (inside the window) | a second `GET /zones` seconds later: `attempted: false`, `reason: cache_fresh`, `already_fresh: 5`, `refreshed: 0` → **no provider call**, per-zone `cache_age_seconds` and `last_attempt_age_seconds` reported |
| Caching rule (window expiry) | a live poll loop over ~10 minutes: **19 polls** (30 s apart) served from cache with no provider call, then the first poll past 600 s: `attempted: true`, `refreshed: 5`, `failed: 0`, cache ages reset to 0, attempt bookkeeping present with `last_attempt_ok: true`, and the very next poll back to `cache_fresh` — **40/40 live checks passed** |
| Degradation is bounded, not a retry storm | unit test drives **119 polls inside one window against a failing provider and asserts exactly 1 provider call**; live-shaped endpoint test asserts each poll after that reports `cache_fresh` with the recorded `last_attempt_error` |
| `FLOODPULSE_WEATHER=off` kill switch | live check: `weather.enabled: false`, `last_refresh.attempted: false`, `reason: "disabled"`, no provider call, and the five zones **still serve their last real readings** (`status: live`) |
| Refresh is traceable in CloudWatch | one structured line per attempt, verified live: `weather refresh outcome: {"attempted": true, "event": "weather_refresh", "failed": [], "mode": "live_weather", "refreshed": [five zone ids], …}` — no secret, no credentials |
| Deployed dashboard vs live API | `node tools/dashboard_run.mjs --site <s3 website> --api <endpoint>` → **11/11**, badge `LIVE`, ranking ordered, a change between two polls named on the page, provenance chips + banner + feed note match the live payload, no panel error |
| Deployed pages carry their contracts | `python tools/verify_deployed_page.py` → **all hold** (live regions, change note, labelled chart, no-script fallback, hidden decoration, attribution) |
| Deployed assets match the repo | `index.html`, `styles.css`, `lib.js`, `app.js` served from S3 are **byte-identical** (sha256) to `web/` |
| Existing routes preserved | `GET /zones/city-chennai/history?minutes=60` **200** (points accumulate at one per 10 min), `GET /incidents?limit=20` **200**, alerting block unchanged, no ARN, key or provider URL in any body |
| Demo fleet still isolated | `GET /zones/zone-riverside-north/history` in live mode → **404** `unknown_zone` + the live zone list |
| Incident/SNS pipeline | unchanged: the 250 pre-M4 tests still pass, and live readings enter the same scoring/hysteresis/incident/alert path (`source: open-meteo-live`) |

### A failure found by that run, and what changed

The live window check caught something the local tests could not: one refresh attempt observed
`attempted: true` with **nothing refreshed and no new cache value** — a single failed provider
request (one HTTP call covers all five cities, so all five zones fail together). The initial design
left the cache stale on failure, which meant a *sustained* outage would have re-attempted the
provider on every 5-second dashboard poll, and the CloudWatch trail showed nothing but durations.

Three changes followed, each verified live afterwards:

1. the refresh window is anchored on the **last attempt**, not the last success, and a failed attempt
   writes a marker that keeps the old values and their true age (outage = 1 call per 10 minutes);
2. every attempt writes one structured CloudWatch line, and the module now configures its logger for
   INFO — Lambda leaves the root logger at WARNING, so the success line had been dropped silently;
3. the Lambda timeout went 10 s → 15 s, since a provider fetch plus five ingests measured
   2.9-6.9 s against the old 10 s limit.

The dashboard text was corrected at the same time: with the provider switched off, a live fleet still
shows previously fetched **real** values, so it must not be described as "every metric is simulated".

### M4 limitations (known, deliberate)

* Rainfall comes from a provider **grid point**, not a gauge: it is a model/observation blend and its
  hourly value can be up to ~60 minutes old, so a live "storm" on this dashboard is a real regional
  rainfall figure, not street-level measurement.
* The 5-minute trend term is usually **unavailable** for live zones: samples arrive every 10 minutes,
  so no reading falls inside `TREND_LOOKBACK_SECONDS ± tolerance` and `trend` reports "no lookback".
* Refreshing is **lazy** (triggered by `GET /zones`) — a scheduled EventBridge refresh is deliberately
  deferred, so the feed only advances while somebody is looking at the dashboard.
* Thresholds are the hand-set heuristic defaults: **not** calibrated for these cities, and not
  official warning thresholds. Risk classes for Indian cities are illustrative, not advisory.
* No provider redundancy: if Open-Meteo is unavailable for longer than the TTL of the cached values,
  live zones go `no_data` (they never fall back to fabricated rainfall).
* The demo zones' M3 data remains in the table (nothing is ever deleted without instruction); it is
  simply not part of the live fleet while `FLOODPULSE_ZONE_MODE=live`.

## M5 dashboard hardening and demo rehearsal (deployed, verified)

The slice that makes the existing system trustworthy in front of an audience: nothing in the AWS
architecture changed (same table, same Lambda, same routes, same bucket), and no new resource was
created. What changed is the dashboard, the verification tooling and the honesty of two API fields.

### What the dashboard now does that it did not

* **Fleet risk distribution strip** — five tiles (NORMAL / WATCH / WARNING / CRITICAL / unscored)
  counted from the payload's *effective* class, with a label ("3 of 5 cities scored in this
  snapshot"). A zone with no reading is never counted as NORMAL: it is counted as *unscored*.
* **Honest failure states** — `CONNECTING` (with the last request error appended), `LIVE`, `STALE`
  (elapsed time keeps moving between polls), `DISCONNECTED`, `is-feed-off` (kill switch) and
  `is-feed-degraded` (provider outage while cached values are served). Stale data is never displayed
  as if it were fresh, and a stale banner says so in words.
* **A documented colour system** — one role per hue (cyan = live/fresh/REAL/NORMAL, amber =
  WATCH/stale/simulated, orange = WARNING, red = CRITICAL/error, slate = unknown) with separate
  `-text` tokens for small text, so 4.5:1 is met against the real glass surface, and band colours
  keep 3:1 against it. Threshold ticks mark the WATCH and CRITICAL cut-offs on every metric bar.
* **History that explains itself** — the tooltip names the five channels it has values for, or says
  plainly that no sensor readings are stored for that point; the chart reports when the API
  downsampled the window.
* **What moved since the last poll** — the board compares the effective class of every zone with the
  previous successful poll and says so in one line under the distribution ("Chennai escalated WATCH →
  WARNING", "2 zones escalated, highest CRITICAL", "Mumbai stopped reporting", "Kochi started
  reporting at WATCH"). It is stated once and then cleared, so a single escalation cannot keep
  claiming attention; a zone that had no reading and now has one has *started reporting*, not
  escalated; and a zone that loses its reading is a WATCH-toned gap, because silence is not safety.
  `FP.fleetChange` is a pure function with seven checks of its own, plus three that drive it through
  the running app.
* **State that is announced, not only coloured** — the connection badge is a polite live region, the
  zone grid and incident list are `aria-live`, the chart is exposed as a labelled image, decorative
  layers are `aria-hidden`, every control is a real `<button>`, and the stylesheet is checked for a
  `:focus-visible` ring (no `outline: none` without one) and a reduced-motion guard. Five checks in
  `dashboard_check.mjs` hold this in place.

### Honesty fix found by the hardening pass (P1)

`GET /zones/{zone_id}/history` and `GET /incidents` used to hard-code `data_classification:
"simulated"` for every fleet, and each incident record was stored (and re-served) with
`simulated: true`, whatever the reading behind it was. While `FLOODPULSE_ZONE_MODE=live` that meant a
live city's chart and incident list *understated* their provenance: the board said "LIVE WEATHER"
while the JSON said everything was simulated.

Now the provenance travels with the data: an incident stores the classification of the reading that
opened it, the history window reports the provenance of the records it actually holds (mixed when a
window holds both, the fleet's when it holds none), both routes carry `zone_mode` and
`metric_sources`, and `provenance_of_records()` is the single rule behind it. A payload that claims
`data_classification`/`metric_sources` from the client is still rejected by validation, so provenance
cannot be spoofed through `POST /readings` (verified live: a simulated reading posted for a live city
is stored and served as `simulated`, never as REAL).

### Demo mode, rehearsed rather than improvised

* `tools/storm_demo.py` drives the demo fleet through the whole arc in-process, **verifies** that
  every stage happened (NORMAL → WATCH → WARNING → CRITICAL → recovery → closure) and exits non-zero
  if it did not. `--export` writes the exact `GET /zones`, `GET /incidents` and history payloads the
  API served at each stage.
* `tools/story_check.mjs` renders those payloads through the real `lib.js` and asserts the story is
  intact from the dashboard's point of view: strip counts, incident rows, simulated labelling, chart
  shape, and that all six stages are the same three-zone fleet (the demo and live fleets can never be
  mixed in one window).
* See `DEMO_RUNBOOK.md` for the minute-by-minute run.

### Demo track, rehearsed against the deployed stack

`build/demo_mode_roundtrip.py` (scratch, not part of the suite) runs the whole demo track on the
deployed API with the same commands the runbook gives an operator, and asserts the fleet switch, the
storm, the incident lifecycle, the closure and the return to the live fleet: **18 checks, 0 failed**.
Measured: switching fleets ~78 s (the deploy script re-verifies every resource), the full 100-reading
arc 498 s, the fast track to CRITICAL ~150 s, and incidents opened and closed again.

That rehearsal found a real bug in `tools/live_simulator.py`: the loop slept the whole cadence *on top
of* the time each step took, so the simulator's clock fell ~3.7 s behind real time per step (three
zones ≈ 4 s of requests) and the API's ±300 s guard would start refusing readings after ~80 steps. It
now sleeps only the remainder of the cadence and reports the drift, so a demo behaves the same at step
1 and step 100.

### Verification for this pass (all commands listed in "Running it locally")

| Check | Result |
|---|---|
| `python -m unittest discover -s tests -t .` | **363 tests, OK** (1.7 s, no AWS, no network) |
| `node tools/dashboard_check.mjs` (demo, live, all six story stages) | **64 checks, 0 failed** (58 without payload fixtures) |
| `node tools/dashboard_run.mjs` (real `lib.js` + `app.js` in a fake DOM) | **11 checks, 0 failed** |
| `node tools/dashboard_scenarios.mjs` (33 failure/edge scenarios) | **33 checks, 0 failed** |
| `node tools/story_check.mjs` (demo arc end to end) | **29 checks, 0 failed** |
| `python tools/live_acceptance.py` (deployed API, read-only) | **35 checks, 0 failed** |
| `python build/live_incident_check.py` (deployed API, incident provenance; local scratch, not committed) | **18 checks, 0 failed** |
| `python build/demo_mode_roundtrip.py` (deployed API, whole demo track and back; local scratch, not committed) | **18 checks, 0 failed** |

No browser was available on this machine, so every visual claim above is checked at the DOM/CSS level
(computed contrast ratios, emitted-class coverage, layout contracts) and never by pixels.

### M5 limitations (known, deliberate)

* Escalation still needs `ESCALATION_STREAK` consecutive readings *of the same candidate class*, so a
  zone that jumps three bands in one step needs several readings to reach CRITICAL and open an
  incident. At the live cadence (one reading per 10 minutes) that is up to ~40 minutes. Retuning it
  is a product decision, not a polish decision, so it was deliberately left alone.
* Anyone holding the ingestion key can post a reading for a live city; it replaces that city's
  `LATEST` until the next weather refresh. It is always labelled `simulated`, but a live board can
  therefore show a fabricated score for up to 10 minutes. Rejecting simulator posts in live mode
  would change the documented ingest contract, so it is reported rather than changed.
* `time_to_threshold` is still reported as deferred rather than guessed.
* The live fleet has no EventBridge schedule: the feed advances only while `GET /zones` is called.

### Local shell notes (Windows, verified)

* `pwsh` is **not** installed here; the shell is Windows PowerShell 5.1, whose execution policy
  blocks unsigned scripts → use the process-scoped `-ExecutionPolicy Bypass` shown above.
* PowerShell's `Invoke-WebRequest` cannot complete the TLS handshake with API Gateway on this machine
  ("connection was closed… on a receive", even with TLS 1.2 forced) → use `curl.exe` or Python's
  `urllib` for HTTPS checks. The AWS CLI is unaffected.
* In PowerShell 5.1, pass DynamoDB JSON via `--key file://…` / `--expression-attribute-values
  file://…` rather than inline quoted JSON, and write those files **without a BOM**:
  `Set-Content -Encoding UTF8` adds one and the CLI then fails with
  `Expected: '=', received: '\ufeff'`. `infra/deploy.ps1` uses a `Write-JsonFile` helper for this.
* `tempfile.TemporaryDirectory()` is unreliable under this sandbox (its cleanup calls `chmod`, which
  is refused), so tests keep scratch files in the gitignored `build/test-scratch/`.

## M6 public-facing information architecture (frontend only, deployed)

The M5 dashboard was technically rich and read like an engineering console: one long page, every
number at the same volume, the model visible before the answer. M6 reorganises the same data into a
product a non-technical visitor can use, **without touching risk logic, the data model, the API
contracts or any AWS resource**. No npm, no framework, no build step, no new dependency: still
`index.html` + `styles.css` + `lib.js` + `app.js` + one background image.

### Four views, one asset set

| View | URL | What it answers |
|---|---|---|
| **Overview** | `#/overview` | Plain-language fleet headline ("2 of 5 cities are at a critical flood risk right now."), highest-risk city, active alerts, cities reporting, weather freshness, a four-band distribution strip, and one status card per city |
| **Cities** | `#/cities` | The full fleet as a grid, plus the ranked list; each card links to its own page |
| **City** | `#/cities/<zone_id>` | Risk score and class as the API reported them, a plain-English reason and advice, five measurement tiles (icon, value, unit, provenance chip, distance to that metric's own watch/flooding level), data freshness, trend/history chart, incident history, and **all** model detail folded into a collapsed `<details>` |
| **Alerts** | `#/alerts` | Live alerts and resolved incidents as separate lists: severity, city, opened/closed times, duration (ticking), top driver, plus the honest SNS alerting state |
| **Data & Methodology** | `#/data` | REAL / MODEL_DERIVED / SIMULATED explained per metric, Open-Meteo attribution and licence, `heuristic-v1` step by step with its fixed weights and band table, why the hydraulics are simulated, and an explicit list of limitations |

Routing is a ~40-line hash router (`#/…`, no `pushState`), so each view is linkable, back/forward
works, and an unknown URL falls back to the Overview instead of blanking the page. The top bar is a
three-cell grid (`brand | nav | status`); under 760 px the links collapse into a labelled `#nav-toggle`
button (`aria-expanded`, `aria-controls`, Escape to close, closes on choosing a section). Every view is
a `<section class="view">` with its own `<h2>`, `tabindex="-1"` and `aria-labelledby`, so switching
views moves focus and announces the new section. All four views are rendered from every successful
poll rather than lazily, so a view switch can never show stale content.

### Four real bugs found while building and verifying this

The redesign's verification was extended first, and all four of these were live on the deployed site:

| Bug | Why nothing caught it | Fix |
|---|---|---|
| The fleet risk distribution strip was frozen at its static "–" placeholders. `renderBandStrip()` guards on `el['band-strip']`, but `cacheDom()` never listed `band-strip`/`band-strip-label`, so it returned immediately and the label stayed "loading fleet…" forever | the existing strip checks were static (does the element exist, is the renderer wired into `app.js`), and no harness asserted the strip's rendered DOM | both ids added to `cacheDom()`; `dashboard_run.mjs` now asserts the strip renders real counts and no placeholder `–` |
| On the Alerts view, an empty incident list left the panel on "loading alerts…". "Nothing loaded yet" and "loaded, and there are none" produced the same render signature, so the guard skipped the write | the empty-state path was never executed — every fixture had incidents | the signature now includes whether the list is loaded; the harness asserts an explicit `data-empty` empty state for both lists |
| A simulated board credited Open-Meteo it had never fetched: `attributionLine()` described the provider regardless of mode | the attribution check only ever ran against a live-weather payload | `attributionLine()` returns nothing unless the feed is live and enabled; the card is hidden when unused, and the harness asserts the per-mode behaviour both ways |
| The Overview's "Weather freshness" figure always read **pending** even with a minutes-old cached reading, because the card asked for `feed.ageSeconds` while `feedStatus()` returns `feedAgeSeconds`. Every other panel was correct, so the page disagreed with itself | nothing static can see a misspelled property on a returned object, and the panel still looked plausible | the word for the feed state is now computed once in `lib.js` (`feedValue`) and the page prints it; the harness asserts the Overview shows exactly what `fleetOverview()` computes, for the payload actually loaded, and never a placeholder |

### Verification for this pass

| Check | Result |
|---|---|
| `python -m unittest discover -s tests -t .` | **363 tests, OK** (~1.8 s, no AWS, no network) |
| `node tools/dashboard_check.mjs` (live fixtures) | **76 checks, 0 failed** |
| `node tools/dashboard_check.mjs` (demo fixtures) | **76 checks, 0 failed** |
| `node tools/dashboard_check.mjs` (no fixtures) | **70 checks, 0 failed** |
| `node tools/dashboard_run.mjs` (deployed assets + live API) | **39 checks, 0 failed** |
| `node tools/dashboard_run.mjs` (live fixtures) | **39 checks, 0 failed** |
| `node tools/dashboard_run.mjs` (demo fixtures, all six story stages) | **37 checks, 0 failed each** |
| `node tools/dashboard_scenarios.mjs` | **33 checks, 0 failed** |
| `node tools/story_check.mjs --dir build/story` | **29 checks, 0 failed** |
| `python tools/live_acceptance.py` (deployed API, read-only) | **35 checks, 0 failed** |
| `python tools/verify_deployed_code.py` | deployed Lambda code **byte-identical** to `src/` (this pass changed no backend file) |
| `python tools/verify_deployed_page.py` | all deployed page contracts hold |

`dashboard_run.mjs` used to be a static-ish runner with its own throwaway DOM that could not dispatch
events. It now executes the real `lib.js` + `app.js` against the shared fake browser
(`tools/dashboard_dom.mjs`) and drives the page the way a person does: clicking each of the four
sections, opening every one of the five India cities and checking its score, class, five metric tiles,
provenance chips, freshness and collapsed technical block; the unknown-city panel; the alerts split and
its empty states; the phone menu (open, Escape, close-on-navigate); a failed poll that must not lose
the values already on screen; and recovery afterwards. `dashboard_check.mjs` gained the matching static
contracts, including one that reads `RISK_BANDS`, `METRIC_WEIGHTS` and `TREND_WEIGHT` out of
`src/floodpulse/config.py` and fails if the methodology page ever states different numbers.

Asset budget after M6 (still one request per file, nothing added): `index.html` 14.6 KB,
`styles.css` 49.9 KB, `lib.js` 74.0 KB, `app.js` 56.4 KB, `bg-storm.webp` 13.3 KB — 208 KB
in total, all served uncompressed from one bucket with `no-cache` on the four text files.

### M6 limitations (known, deliberate)

* No browser exists on this machine: every visual claim above is verified at the DOM/CSS/usability
  level (navigation state, focus movement, emitted classes, contrast ratios, breakpoint rules), never
  by pixels. Nothing here claims to have been seen.
* The history chart is still a `<canvas>` panel inside the City view rather than a separate route.
* There is no per-view lazy loading, so a poll re-renders every view. At five cities and one table
  this costs nothing measurable; it would need revisiting at fleet scale.
* The Overview's "active alerts" count comes from `GET /incidents`, so it is only as fresh as the
  incident poll (10 s) rather than the zone poll (5 s).

## M7 final hardening and product quality pass (no new AWS resources)

An audit of the whole system — backend, front end, tests, documentation, cost, security — then
one fix at a time, each with a test that fails without it. Nothing was rebuilt, no architecture
was added, and no AWS resource was created, deleted or reconfigured. The tables below are the
complete set of defects found and fixed; `DEMO_RUNBOOK.md` records the same findings from the
operator's side.

### Backend correctness

| Defect | Effect it had | Fix |
| --- | --- | --- |
| `records[::stride]` sampled history from the oldest record | A thinned window dropped its **newest** reading — the point the chart's right edge is drawn from (9 records, cap 5: indices 0,2,4,6,8 kept, index 8's successor lost) | The sample is anchored at the newest record, so the first and last points of a window are always real ones and the cap still holds |
| `open_count`/`closed_count` summed the returned slice | "1 open" could not be told apart from "1 open on this page of 50" | `counts_scope: "returned_page"` plus `truncated: true` when the limit was reached |
| `FLOODPULSE_WEATHER_TIMEOUT_SECONDS` never read | Documented and deployed, but the provider client always used its own default | Read, clamped to 1-8 s, and asserted to reach the transport call |
| `FLOODPULSE_WEATHER_REFRESH_SECONDS` logged "clamping" but returned 600 s | An operator setting 30 s or 86400 s got a value the log did not describe | Clamps to the range and logs the value it actually used |
| The top-level `weather` block hardcoded the live sources and note | The demo fleet (no provider at all) reported `metric_sources.rainfall_mm_per_hr == "REAL"` and called generated rainfall "the real rainfall" | With no live zone the block reports the simulated sources, a note saying every channel is generated, and `last_attempt_ok` per zone like the per-zone blocks |
| One unreadable stored reading raised through the fleet view | Any single bad `LATEST` item blanked the whole board | That zone alone reports `status: no_data`, the fleet still renders, and the failure is logged with the zone and the missing fields |
| One provider error raised through the live-weather path | A provider failure could blank the fleet instead of degrading it | Caught per zone, recorded and reported; the last good values keep being served with their true ages |

### Honesty about time and provenance

* `highest_risk_zone` carries `age_seconds` and `stale`; `/zones` carries `stale_zone_ids` and
  `stale_after_seconds` (**1800 s**: three missed provider refreshes). All of it is derived from
  `LATEST` items already in hand, so no extra DynamoDB reads.
* The Overview's highest-risk stat shows the age of the reading behind the score and marks a zone
  that has stopped reporting; the hero card and the zone cards already did.
* An OPEN incident on a zone that went quiet is marked in the incident log and in the alerts view.
* SNS alert subjects state their provenance: `(simulated)`, `(live rainfall, simulated hydraulics)`
  or `(simulated data)` per alert kind, instead of one subject for both fleets.
* A POST whose timestamp is inside the accepted ±300 s skew can move `LATEST` backwards: an
  out-of-order arrival replaces a newer reading with an older one. The stored reading is real and
  keeps its own timestamp, but that zone's "newest" reading is the older one until the next reading
  arrives. Judge a zone by `latest.age_seconds`, not by assuming `LATEST` is the latest arrival.

### Front end

| Defect | Effect it had | Fix |
| --- | --- | --- |
| `role="status" aria-live="polite"` wrapped the whole connection block, whose text carried a seconds-resolution age and countdown | About 60 polite announcements a minute, for ever, to anyone using a screen reader | The live region is one off-screen sentence with no ages in it, written only on a real state change; the visible ticker is unchanged |
| `aria-live="polite"` also sat on `#city-cards`, `#zones`, `#alerts-active` and `#alerts-closed`, which are re-rendered whole on every poll | The same cards and incidents were re-announced every 5 s, for ever: the reading ages and incident durations change with the clock, so the region was never quiet | Those four lists are ordinary named regions now (each is labelled by its own heading). The announcement surfaces are `#change-note` for real band movements - including a zone that stopped reporting - plus the strip summary and the connection state |
| Sparklines drew a dpr-scaled path into the 280x56 bitmap the markup declared | Clipped on a HiDPI screen, stretched on a 1x one | Both canvases size their backing store from their laid-out width (asserted in the runtime harness) |
| A failed history fetch left a blank box labelled `role="img"` | AT heard "chart" over an empty rectangle, and the previous window's numbers stayed on screen | A loading line before the request, an empty state that says why on failure, and a label describing the series actually drawn |
| The plain-language sentence tested `trendLabel()` against `'rising'`/`'falling'` | "Conditions are getting worse" was unreachable dead code on a flood board | Branches on `trendDirection()`; the numbers stay in the technical table |
| `#/cities` was a dead end | Cards lifted on hover but nothing opened a city | Each card title is a link with a stretched hit area |
| With no API base the page sat on CONNECTING for ever | Five panels said "waiting for data..." indefinitely | The badge, the status line and the screen-reader sentence all say the API base is missing |
| `poll()` refreshed incidents on the 10 s boundary whenever the 5 s poll crossed it | `/incidents` cost twice what it needed to (13 requests a minute instead of 6) | The poll no longer triggers incidents, and `loadIncidents` has an in-flight guard |
| `.band-tile.is-zero { opacity: 0.55 }` | The strip's own labels fell to 3.6-4.5:1, on four of five tiles of the live fleet | The tile recedes by losing its gradient and band colour; its labels keep 12:1 |
| `.zone-card .pill { color: currentColor }` | Beat `.pill.band-critical` on source order, so a CRITICAL pill rendered at 4.90:1 while the stylesheet and the checks claim the light variant | Removed; the band rules apply as documented |
| `.ghost`/`.nav-toggle` boundaries at 1.88-1.89:1 and 29.6-34 px tall | A control needs 3:1 and a fingertip needs 40 px | Border strengthened to `rgba(148,163,184,0.6)`, 40 px minimum, and a 40 px floor on the phone breakpoint |
| The six-column technical table (min-content about 410 px) pushed the page sideways below a 500 px viewport | Horizontal page scroll on a phone | It scrolls inside its own focusable box |
| The chart answered `mousemove` on a canvas with no tabindex | The per-point values were unreachable by keyboard or touch | Focusable, arrow keys/Home/End/Escape, a label naming the point being shown, touch handlers, and a line saying the arrow keys work |
| `formatMetric`/`formatValueWithUnit` dereferenced `METRIC_META[metric]` unguarded | One unexpected driver name from the API threw inside the renderer and blanked the city page | A `metricMeta` fallback that uses the raw name with no unit |
| `<ol id="ranking">` preceded its own heading; `#zones` had no accessible name; the missing-city card started at `<h3>` under an `<h1>` | Screen-reader reading order and heading structure | Heading first, both regions labelled by their headings, the card is an `<h2>` |
| The `resize` listener redrew every sparkline and the history chart on every event | Dragging a window edge fires dozens of events a second; each redraw resized two canvases per card, reallocating their backing stores for sizes the browser could not paint | Redraws are coalesced to one per animation frame (`requestAnimationFrame`, falling back to `setTimeout` where there is no frame loop), and the check dispatches twelve resize events and asserts the last one still lands |

### Tests

| Harness | Before this pass | After |
| --- | --- | --- |
| `python -m unittest discover -s tests -t .` | 391 | **405** (the last five check that the committed tooling compiles, names its target and carries no secret-looking literal) |
| `node tools/dashboard_check.mjs` | 78 | **88** on both payloads (the live fixture was re-captured against the deployed API, so it exercises the same checks as the demo one); the live-region rules are negative checks over the real markup |
| `node tools/dashboard_run.mjs` | 41 live, 39 demo (2 demo checks were mode-blind and failing) | **46** live / **44** demo, 0 failed |
| `node tools/dashboard_scenarios.mjs` | 35 | **42** |
| `node tools/story_check.mjs --dir build/story` | 29 | **29** (unchanged: the story arc is untouched) |

Every fixed defect has a check that fails without the fix, phrased as the property that matters
("a simulated zone is never called a city", "seventy clock ticks must not rewrite the announced
sentence", "a reading an hour old must be shown with its age"). The two demo-mode assertions that
failed before this pass were harness bugs rather than product bugs: they hardcoded the live noun.

### M7 acceptance run (live, against the deployed stack)

Everything below was run against the **deployed** resources after this pass: the Lambda code
was updated from `src/`, four dashboard objects were uploaded, no resource was created,
reconfigured or deleted, IAM was untouched and the ingest key is unchanged. Commands are shown
without their secrets.

| Check | Result |
| --- | --- |
| `python tools/verify_deployed_code.py` | deployed Lambda package is **byte-identical** to `src/` (13 files, 0 differing) |
| `python tools/verify_deployed_page.py` | all deployed page contracts hold (22 checks): the quiet live region, the focusable chart, the incident table roles, and the seven negative checks (the ticking line, four volatile lists, the hidden headers, disabled outlines) — the ticking line is not a live region, the column headers are not hidden, focus outlines are not disabled |
| `python tools/live_acceptance.py` | **35 checks, 0 failed**; median `/zones` latency **1.07 s** (max 1.12 s over five polls); no city failing its fetch; unknown zone 404; out-of-range `minutes` 400; no ARN in any response |
| `node tools/dashboard_run.mjs --site <dashboard> --api <api>` | **46 checks, 0 failed** with the *deployed* `lib.js`/`app.js` against the *live* API |
| `GET /zones` (live) | `stale_zone_ids []`, `stale_after_seconds 1800`, `highest_risk_zone.age_seconds 68`, `stale false`, `weather.last_attempt_ok` true for all five cities |
| `GET /incidents` (live) | `counts_scope "returned_page"`, `truncated false`, `open_count 0` of `count 4` |
| CloudWatch, last 24 h of `/aws/lambda/floodpulse-api` | 0 ERROR lines, 0 tracebacks, 0 timeouts, 0 5xx; every `weather_refresh` line reports `error: null` |

These three checks are committed under `tools/` as of this pass — they used to live only in the
gitignored `build/` directory, which meant the commands above were documented but not reproducible
for anyone cloning the repository. `tests/test_tooling.py` now compiles every Python tool, syntax-
checks the node harnesses, and asserts that no tool carries a secret-looking literal.
The history-downsampling fix cannot be observed live: at current volumes `stride` stays 1 (a
24-hour window for one city is about 290 points against the 600-point cap). Two unit tests pin
the property instead — the first and last points of a thinned window are real records, and the
cap still holds.
### The demo track, rehearsed against the deployed stack after this pass

The runbook's Track A was played through end to end, because a hardening pass that leaves the
demo path unexercised has not finished:

1. `infra\deploy.ps1 -ZoneMode demo -WeatherMode off` (existing resources only; two Lambda
   environment variables and the same code uploaded).
2. `python tools\live_simulator.py --url <api> --interval 5.0 --steps 100` — the whole arc: calm,
   watch, warning, critical at all three zones, recovery, closure. Peak 1.0000 CRITICAL on all
   three zones, ~500 s.
3. Read back from the deployed API: all three zones NORMAL again, `age_seconds 14`, `stale false`,
   `stale_zone_ids []`, `stale_after_seconds 1800`, 0 open of 16 incidents, every closed incident
   `close_reason "zone returned to NORMAL"`, durations 335-345 s for this run.
4. The demo fleet's `weather` block, with the provider switched off, reports
   **every metric `SIMULATED`** and a hydraulics note that says so — the live check that this
   pass's provenance fix is in the deployed code (before it, that block claimed `REAL` rainfall).
5. `infra\deploy.ps1` back to the live fleet: `zone_mode live_weather`, 5 cities, `mixed`,
   `tools/live_acceptance.py` 35/35, and `/incidents` still reports only the live fleet's own
   incidents (4, all `city-kochi`) — the two fleets never appear in one payload.

The rehearsal paid for itself: driving the board against a freshly captured demo payload, rather
than the M4 fixture it had always been run against, failed one check — and the check was wrong,
not the page. It asserted that the plain-language sentence contains no `0.42 m`-shaped number,
intending to catch a *trend delta* leaking into the plain copy; the sentence legitimately names the
top driver's value with its unit, and the assertion only ever passed because the leading driver in
that fixture happened to be soil saturation rather than the water level. It now looks for what it
actually means — a signed delta or a trend window — and prints the sentence when it fails.
### Documentation

* `DEMO_RUNBOOK.md` covers the ingest-key tool's `--dry-run` and fingerprint, the env/key
  consistency check in `infra/deploy.ps1`, and what to do when a poll fails mid-demo.
* This README documents the incident-silence rule, the `LATEST` TTL, the staleness fields, the
  scope of the incident counts and the out-of-order-reading caveat.

### Security review (M7)

| Item | State |
| --- | --- |
| Ingestion key | Stored only in the gitignored `.floodpulse-key` and `build/env-prod.json`; compared with `hmac.compare_digest`; absent key in cloud mode fails **closed** (503 `ingest_not_configured`); a missing header is 401 while a wrong key is a differently-worded 401 that reveals nothing |
| Secret hygiene | `tools/init_ingest_key.py` never prints the key (only a `sha256:` fingerprint), merges into the Lambda environment instead of replacing it, validates before writing, and writes atomically; `infra/deploy.ps1` verifies that the local key file and the deployed environment agree *before* it changes anything, and prints no values |
| Repository | No secret, ARN, account id or key appears in the front end, the deployed assets or any response: asserted by the static harness (the page may not mention `x-fp-key`, `FLOODPULSE_INGEST_KEY`, `POST` or `/readings`), by the live acceptance probe (no `arn:aws` in any payload) and by tests |
| Public surface | Read routes are public by design (`/zones`, `/incidents`, `/zones/{id}/history`); the only write route requires the key; unknown routes 404 at API Gateway; query parameters are validated (`minutes` 1-1440, `limit` 1-200) |
| Response headers | CORS is restricted to the dashboard origin and `http://localhost:8000`; an unknown origin gets no allow-origin header. S3 static website hosting cannot send custom headers, so a CSP would need CloudFront, which is out of scope — the page loads no third-party script, stylesheet or font, and every image is local |
| Injection and input handling | Every string that reaches the DOM goes through `escapeHtml`; stored payloads are validated before use and a malformed one degrades to `no_data` rather than throwing; no `eval`, no `innerHTML` with unescaped data |
| Least privilege | The Lambda role has `dynamodb:GetItem/PutItem` on the one table (no scan, no delete, no GSI) and `sns:Publish` on the one topic ARN; `iam:*` is denied to the deployment profile, so no policy was touched |
| Known gaps (accepted, documented) | No API throttling; no CloudWatch alarm; no subscriber on the alerts topic; no CSP header (S3 static hosting); no WAF. All four are reported rather than silently accepted, and none is a defect in the code |
## M8 production hardening (adversarial pass, deployed, verified)

The brief for this pass was not "add features" but "try to break it": treat the M7 system as a
production candidate and look for the ways it could be misused, corrupted, overloaded, confused or
accidentally broken, while keeping the live India experience and demo mode exactly as they were.
The architecture did not change: the same one table, one function, one HTTP API, one bucket, one
topic, the same IAM role and the same ingestion-key model. **No AWS resource was created, deleted
or reconfigured** — with one deliberate exception, the stage throttle below, which is a setting on
the existing API and is reversible in one command.

Each change is one commit with the reasoning in its message; this section is the index to them.

### What was actually broken (found by probing, not by reading)

| Severity | Defect | Fix |
| --- | --- | --- |
| **P0** | A payload string was rendered into the hero hint without escaping. The dashboard can be pointed at any API origin with `?api=`, so an API (or a compromised proxy) could execute script **in the dashboard's own origin** and read everything the page can reach | The value is escaped like every other payload string, and an adversarial scenario asserts the escaped text is what reaches the DOM and that no tag is ever created |
| **P0** | `src/floodpulse/handler.py`: a reading posted by a *client* into the live fleet was served with zone-level **REAL** provenance while its own values were SIMULATED. The board was labelling simulated numbers by the fleet they sat in | Provenance follows the record, not the fleet; a mixed record is labelled `mixed`; the weakest claim present wins |
| **P1** | A payload containing a non-object in `zones[]` (a `null`, a string) threw inside a renderer. Because that renderer ran first, the board stayed frozen on its loading placeholders *while the API was answering 200* — and the poll's own error path was skipped, so the failure was invisible | Elements that are not objects are dropped where the list enters state, each renderer returns `''` for a non-object zone, and a render failure now logs, refreshes the status line and does not masquerade as a transport error |
| **P1** | `#/cities/%` threw a `URIError` out of `start()`, so the page never polled at all | The fragment is decoded defensively and an undecodable one is ignored |
| **P1** | `infra/deploy.ps1` treated *any* non-zero AWS CLI exit as "resource missing" — access denied, an expired token or throttling would send it down the create path | A classifier that separates not-found (`NoSuch*`, `ResourceNotFoundException`, 404) from a real error, which stops the deploy |
| **P1** | `infra/deploy.ps1` rewrote the whole Lambda environment map, so a variable that existed only in AWS disappeared on the next deploy | Read, merge, compare *names*, and refuse to disagree silently |
| **P1** | `infra/deploy.ps1` applied a public-read bucket policy (and disabled the public-access block) to a bucket it had **not** created | Public read is applied only to a bucket this run created; an existing bucket is read, judged and reported, never widened |
| **P2** | `infra/deploy.ps1` took the *first* integration in the listing and retargeted every route at it | Integrations are matched on `IntegrationUri`; an unrecognised state stops the run instead of being overwritten |
| **P2** | The list helper could hand a single item back as a bare object, whose `.Count` is `$null` — read as "nothing there". The integration check asked exactly that question, so on a one-integration account it would have created a **second** integration | The helper returns the array whole; three sizes are pinned in the offline harness, which also refuses the opposite mistake (`@(command)` would make `.Count` always 1) |
| **P2** | Provenance prose could print `undefined` and a bare separator; `METRIC_META[metric]` could resolve to an `Object.prototype` member; a provider link was built from an unvalidated payload URL; a negative age was reported as "just now"; the loading pulse never stopped after a failed first poll | Each has a guard and a check in the static harness |
| **P2** | The incident lifecycle was invisible in the logs: an opened, escalated or closed incident left no trace beyond the SNS publish | One structured line per transition |
| **P2** | Four different clocks were collapsed into a single "age" on the dashboard | They are now named and shown separately (below) |

### API abuse and cost protection

`$default` on the existing HTTP API now carries **burst 40 / steady rate 20 requests per second**.
Measured, not guessed: the busiest minute in the six hours before the change saw **36 requests
(≈0.6/s)**, so the limit leaves roughly 33× headroom over real traffic, and a dashboard page load
(3 requests: `/zones`, `/incidents`, `/history`) fits inside the burst. A flood of requests is
throttled at the edge and answered with 429 before it can reach Lambda, DynamoDB or Open-Meteo.

```powershell
# applied with (existing API only; no new resource)
aws apigatewayv2 update-stage --api-id 2rg2u8quzd --stage-name '$default' `
  --default-route-settings '{\"ThrottlingBurstLimit\":40,\"ThrottlingRateLimit\":20}' `
  --profile cloud-project --region eu-north-1

# reverse it in one command - set values high enough that they never engage
aws apigatewayv2 update-stage --api-id 2rg2u8quzd --stage-name '$default' `
  --default-route-settings '{\"ThrottlingBurstLimit\":5000,\"ThrottlingRateLimit\":10000}' `
  --profile cloud-project --region eu-north-1
```

Every public input is already bounded in the function itself, which is what matters if the
throttle is ever raised: the body is capped at 4 KB, query parameters are validated against a
closed grammar (repeated or boolean-valued parameters are refused rather than guessed), history
windows are clamped to 1440 minutes and 600 points, incident limits to 200, JSON depth and
non-JSON content types are refused before parsing, and a reading more than 5 minutes out of skew is
rejected.

### Correctness under replay and reordering

Already true before this pass and now pinned by adversarial tests: history uniqueness is enforced
by a conditional write, `LATEST` is only replaceable by a reading with `ts_epoch <= :ts`, an older
reading is kept as history but never becomes current, a replayed reading is reported as a duplicate
and is not judged for incidents, and two readings in the same second are both kept. Alert storm
protection is a conditional claim on one item per zone and severity, so ten concurrent callers
produce one alert.

### Weather provider failure

Already handled and now explicit: 429/500/404, a network exception, a timeout (default 4 s,
clamped 1–8), malformed JSON, a missing or absurd value, a missing city and a partial response all
degrade to the last good values rather than a failed poll — and **they never become a REAL label**.
The provider is called at most once per refresh window (600 s) across all five cities, and if the
call fails the previous cache is kept and its age is published as-is.

### Front end: a policy, not just escaping

The page now carries its own `Content-Security-Policy`:

```
default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:;
font-src 'self'; connect-src 'self' https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com
https://*.execute-api.eu-north-1.amazonaws.com http://localhost:* http://127.0.0.1:*;
object-src 'none'; base-uri 'none'; form-action 'none'
```

`script-src 'self'` means an injected string cannot execute even if an escaping mistake slips
through, which is why the page had to lose its inline `<script>`: the API base is now a
`data-api` attribute on `<html>`, read by `app.js`. `'unsafe-inline'` is granted for **styles
only** (the renderers set a few style attributes for bar widths); `object-src`, `base-uri` and
`form-action` are `'none'`. A `referrer` policy is set and external links carry
`rel="noopener noreferrer"`.

#### The one directive that had to go, and the gap it leaves (known limitation)

Browser QA found a console warning: *"The Content Security Policy directive 'frame-ancestors' is
ignored when delivered via a `<meta>` element."* The policy used to end with
`frame-ancestors 'none'`. That is correct, and it was correct on every page load: per the CSP
spec a browser **ignores `frame-ancestors`, `sandbox` and `report-uri`/`report-to` in a
`<meta>`-delivered policy** — only a real HTTP response header can carry them. So the directive
bought no protection at all, while putting a warning in front of every visitor. It has been
removed, and nothing else about the policy changed: every other directive is byte-identical, and
the deployed-page check now asserts each required directive's *exact* value (so a directive that
is present but quietly weakened cannot pass).

**The honest consequence: this page cannot currently prevent itself from being framed, and there
is no meta-tag or JavaScript substitute that would.** Proper anti-framing/clickjacking protection
needs `frame-ancestors 'none'` (plus `X-Frame-Options: DENY` for old browsers) as an **HTTP
response header**. The dashboard is served by **S3 static website hosting**, which emits no custom
headers — an object's `Content-Type`/`Cache-Control` metadata is all it can influence, and there is
no header-injection layer in front of the bucket. Adding one means CloudFront with a
response-headers policy (or a Lambda@Edge/CloudFront Function): **that is a new AWS resource, so it
is deliberately out of scope for this pass** and was not created.

Risk accepted, stated plainly rather than waved away: an attacker could frame this dashboard and
overlay their own UI to mislead a visitor about what the public data says. The practical impact is
low here because the page has **no login, no session, no cookies, no form** (`form-action 'none'`,
`object-src 'none'`, `base-uri 'none'`), no destructive action and no data entry — the worst case
is a misleading *presentation* of public information, not a stolen credential or a state change.
If FloodPulse ever gains accounts, submissions or an operator console, framing protection stops
being optional and the CloudFront header layer becomes a requirement, not a nicety.

The gap is recorded here, in `web/index.html` (next to the policy, with an instruction to put the
directive in a header if a header layer is ever added) and in `DEMO_RUNBOOK.md` item 1 of the
manual visual check — and the harnesses now refuse to let the directive reappear in the meta
policy.

**Operational consequence, worth knowing before demo day:** the policy is what allows the page to
talk to its API. Pointing `?api=` at a *different* origin — a local server, another deployment —
requires adding that origin to `connect-src` first; loopback origins are already there so
`?api=http://localhost:8000` still works. Nothing in the demo depends on this.

Both the served page and the sources are checked for this: `tools/verify_deployed_page.py` asserts
the policy directive by directive (exact values, no meta-unenforceable directive, exactly one
policy), the absence of any inline script and the absence of a `javascript:` URL in the *deployed*
page, and the static harness asserts the same plus that the policy cannot name an
unrelated origin.

### One freshness contract, with every clock named

The dashboard used to answer "how old is this?" with a single number that could be the reading's
age, the provider's observation age or the cache age, depending on which panel you asked. Payloads
now publish each clock separately and the interface names them:

| Published field | Meaning |
| --- | --- |
| `latest.age_seconds` | age of the reading the fleet is serving |
| `latest.provider_observation_age_seconds` | age of the *weather observation* that reading was derived from (`null` for simulated readings) |
| `zone.weather.cache_age_seconds` | age of the last successful provider fetch for that city |
| `zone.weather.last_attempt_age_seconds` | age of the last provider attempt, successful or not |
| `zone.weather.observation_age_seconds` / `observation_interval_seconds` / `observation_is_current` | the provider's own reporting cadence, and whether the observation is on schedule or behind it |
| `stale_after_seconds` | the threshold at which the interface starts calling a reading stale |

A reading can therefore be fresh while the provider observation behind it is behind schedule, and
the page says so instead of printing "updated now" over an hour-old observation.

### Observability without new resources

The incident lifecycle is now visible in the existing log group: one line per transition
(`incident_opened`, `incident_escalated`, `incident_closed` with zone, severity, status, risk class,
observed-at, reason and whether the alert was published), plus a warning when incident tracking
fails, a structured line for each weather refresh (refreshed / already fresh / failed, with the
reason), and the existing ingest-rejection lines. Nothing logs the ingestion key, an authorization
header or a secret value, and the retry/failure paths that could have echoed an AWS CLI request
body are redacted before they can reach the console. Log volume is bounded by the same cooldowns
that bound alerting; the group's stored bytes stayed at ~416 KB with 7-day retention.

### Deployment tooling

`infra/deploy.ps1` is now readable before it is runnable:

```powershell
powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -DryRun           # plan only, writes nothing
powershell -ExecutionPolicy Bypass -File infra\deploy.ps1                   # idempotent deploy
powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -AllowDirtyTree   # ship uncommitted work
powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -AllowKeyDrift    # deploy the env-file key
powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -KeepUnknownEnvVars
```

Every write goes through one guarded helper, so `-DryRun` turns the entire run into a plan — the
reads and comparisons still happen, and each write is printed instead of performed. The script
refuses to deploy a missing ingestion key, key drift between `.floodpulse-key` and the environment
file, a CORS origin the run does not know about, or an uncommitted `src/`+`web/` tree unless the
matching switch is passed; it only ever *lengthens* log retention (shortening it deletes events
irreversibly); and it closes with a verification block that re-reads the deployed state and runs
`tools/verify_deployed_code.py`.

The safety logic is verified offline, in the suite, without credentials:
`tools/deploy_logic_check.ps1` lifts the helper functions out of the script with the PowerShell
parser, replaces every AWS entry point with a stub, and asserts the redaction, the not-found
classifier (including the `NoSuch*` shapes that are *not* errors), the environment-document
validation, the pagination and the dry-run gate. `tests/test_deploy_script.py` runs it and pins the
structural properties a later edit could remove.

### What this pass added to the suite

| Suite | Before | After |
| --- | --- | --- |
| `python -m unittest discover -s tests -t .` | 405 (as published in M7) | **509** |
| `node tools/dashboard_check.mjs` (per mode) | 88 | **96** |
| `node tools/dashboard_scenarios.mjs` | 42 | **44** |
| `node tools/dashboard_run.mjs` | 46 live / 44 demo | 46 live / 44 demo |
| `tools/deploy_logic_check.ps1` | — | offline, all assertions pass |

New test files: `tests/test_observability.py` (the freshness clocks and the incident log lines),
`tests/test_env_hardening.py` (every environment variable driven with an absent, empty,
unparseable, out-of-range, huge and non-ASCII value, and the distinction between "fall back to the
default" and "clamp into range"), `tests/test_deploy_script.py` (the deployment contracts).

### Verified after the change (live, 2026-10-17)

| Check | Result |
| --- | --- |
| `python tools/verify_deployed_code.py` | `RESULT: deployed Lambda code is byte-identical to the repository's src/` |
| `python tools/verify_deployed_page.py` | **all deployed page contracts hold** (including the CSP, the data-attribute API base, no inline script, the referrer policy and `noopener`) |
| `python tools/live_acceptance.py` | **35 checks passed, 0 failed** (max poll latency 1.25 s; no city failing its fetch; no ARN in any payload; throttling observed not to engage) |
| `node tools/dashboard_run.mjs --site <deployed bucket> --api <deployed api>` | **46 runtime checks passed, 0 failed** against the deployed assets and the live API |
| Fleet state after the deploy | `live_weather`, 5 cities, weather enabled (`open-meteo`), all five NORMAL, reading age 33 s |
| Incidents | 4 recorded, **0 open** — no test incident left behind |
| Demo fleet | left calm (NORMAL, risk 0.22–0.24), so a demo run starts from a clean board |
| Table | 1,692 items / 1,172,412 bytes, on-demand, TTL on `ttl`; 8 `LATEST` (5 cities + 3 demo), 5 weather caches, 4 alert markers |

### Known residual gaps (deliberate, recorded rather than hidden)

* `POST /readings` remains open in live mode for the five city zones — isolation is delivered by
  provenance (a client reading can never be served as REAL), not by refusing the write. Closing it
  completely is a one-line change to the zone allowlist; it is left open because the live simulator
  and the demo both depend on it.
* CSP enforcement is the browser's job. The policy is asserted in the served bytes and locally, but
  no browser was available on this machine, so "no violation in the console" is a manual check
  (see `DEMO_RUNBOOK.md`).
* The dashboard polls `/zones` every 5 s per open tab. The throttle bounds the damage, but a room
  full of tabs is still N× the traffic of one.
* SNS has **0 confirmed subscriptions and 1 pending** (an address that never clicked the
  confirmation link). A published alert is therefore delivered to nobody today; the deploy warns
  about exactly this, and only ever says "publish succeeded".

## M9 product experience, information design and responsive polish (frontend only, deployed)

The system was already correct and hardening-tested; what it was not yet was *legible*. A person
who has never heard of FloodPulse could read the board, but they had to work: engineering wording
(`zone_mode`, `age_seconds`, "dominant driver"), five different clock formats, formulas in the
place a summary belongs, an alerts page that was a table under a heading, no About page, and no
answer to the only question a visitor actually has — *is there a flood problem right now?*

M9 is an information-design pass. Nothing in `src/`, `infra/` or AWS changed: no new service, no
IAM change, no threshold change, no CSP relaxation. The whole milestone is `web/` plus the
harnesses that check it.

### The five questions the Overview answers, in order

The heading is the question ("Is any city at risk of flooding right now?"), and the sentence
under it is the answer, announced politely so a screen reader hears it once when the feed moves.
Then four summary cards (highest risk, active alerts, cities reporting, weather freshness), then
the highest-risk spotlight, then the distribution strip, then the city cards, then a recent-change
panel. The model name, the weights and the raw snapshot moved into a collapsed "Technical details"
block: a formula is now one click away instead of being the second thing on the page.

### Plain language, with the number still available

Every user-facing sentence is written for a reader, and every technical value stays reachable:

| Before | Now |
| --- | --- |
| `age_seconds: 123` | "Updated 2 min ago" |
| `zone_mode=live_weather` | "Live weather" |
| "dominant driver: rainfall" | "Rainfall is the main factor" |
| a bare score ring | score + band word + "SIM"/"MODEL"/"REAL" chip + a trend arrow |
| a metric table | tiles with an icon, a unit, a provenance chip and a "?" that explains the metric in one sentence |

The vocabulary is fixed and never mixed: risk `NORMAL / WATCH / WARNING / CRITICAL`; feed
`LIVE / STALE / NOT REPORTING`; source `REAL / MODEL-DERIVED / SIMULATED`; alert
`ACTIVE / RESOLVED`; system `HEALTHY / ATTENTION / DEGRADED`.

### Icons, without an icon library

A local inline-SVG set (about thirty 24×24 paths, one stroke width, `currentColor`, hidden from
assistive tech because the word beside each icon carries the meaning). No Font Awesome, no
Material icons, no sprite request, no webfont — which is what keeps the CSP at `script-src 'self'`
and `img-src 'self' data:` with nothing to fetch.

### Alert delivery, stated only as far as it is known

The API exposes `{enabled, severities, cooldown_seconds}` and no subscriber information at all,
so the delivery panel says exactly that: publishing is "Configured", a published alert says
"published to the notification topic", and subscriber state says "Not reported to this page" with
the reason. `alert_count > 0` on an incident is the only basis for saying an alert went out, and
that is what the per-incident line uses. The page never claims an email arrived, because it cannot
know.

### Flood safety guidance, clearly not an authority

The About page carries a guidance section: avoid walking or driving through floodwater, move to
higher ground when instructed, stay away from drains and fast-moving water, follow local
authorities, monitor official channels — and then links to the India Meteorological Department and
the National Disaster Management Authority, with the sentence that settles the question: FloodPulse
does not issue official warnings, and for official emergency guidance follow local government and
disaster-management authorities. Nothing is scraped and no government API is integrated.

### Responsive behaviour and the checks behind it

Breakpoints at 1180 / 900 / 760 / 560 / 400 px, checked mechanically rather than by eye: the
harness asserts the viewport tag, that each breakpoint exists, that no `grid-template-columns`
demands more width than a phone has, that payload text is told how to wrap
(`overflow-wrap: anywhere`), that grid children may shrink (`min-width: 0`), that the wide incident
table has its own scroll container, and that the controls a phone taps (nav links, empty-state
buttons, help disclosures) are ≥44 px.

The honest limitation: **no browser was available on this machine**, so the layout is verified as
CSS and DOM, not as pixels. The manual viewport checks are listed in `DEMO_RUNBOOK.md`, and they
are the ones that would catch a visual regression the harness cannot see.

### Asset budget (measured, not estimated)

| Asset | Size |
| --- | --- |
| `index.html` | 26.8 KB |
| `styles.css` | 79.3 KB |
| `lib.js` | 116.7 KB |
| `app.js` | 88.5 KB |
| `bg-storm.webp` | 13.3 KB (1760×990, mean luminance 0.20, real photographic detail) |
| **Total front end** | **~317 KB of source, five files, zero dependencies** |

Browser QA after the first deploy found one console warning (the ignored `frame-ancestors`
directive). It is fixed and the reason is documented in the security section above; the asset
sizes here are the post-fix ones (`index.html` grew by ~0.8 KB because the comment next to the
policy explains the gap to the next person who edits it).

No framework, no bundler, no npm package, no CDN, no analytics, no video. The backdrop stays a
local compressed WebP (well inside the 250 KB target) over a dark gradient, so a reader is never
looking at text on a photograph.

### What this pass added to the suite

| Harness | Before → after |
| --- | --- |
| `tools/dashboard_check.mjs` | 96 → **106 static checks** per mode, now including the eight M9 groups (phone-first layout, touch targets, icon consistency, product copy, Overview order, empty/loading/error states, asset budget, About-page scope, heading outline) plus the band-stripe survival check and the meta-policy directive list |
| `tools/dashboard_run.mjs` | 46 → **54 runtime checks** live (44 → 52 for the demo fleet) |
| `tools/dashboard_scenarios.mjs` | 44 → **49 scenarios** |
| `python -m unittest discover -s tests -t .` | 509 tests, unchanged (no backend or test change in this milestone) |

### Verified after the change (live, 2026-10-17)

| Check | Result |
| --- | --- |
| `node tools/dashboard_check.mjs --payload build/zones-live-weather.json …` | **106 checks passed, 0 failed** (same against the demo fixtures) |
| `node tools/dashboard_run.mjs --payload build/zones-live-weather.json …` | **54 runtime checks passed, 0 failed** (52 for the demo fleet) |
| `node tools/dashboard_scenarios.mjs` | **49 scenario checks passed, 0 failed** |
| `node tools/story_check.mjs --dir build/story` | **29 story checks passed, 0 failed** |
| `python -m unittest discover -s tests -t .` | **509 tests, OK** |
| `python tools/verify_deployed_code.py` | `RESULT: deployed Lambda code is byte-identical to the repository's src/` (no backend change was made) |
| `python tools/verify_deployed_page.py` | **all deployed page contracts hold**, including the About page, the guidance links and the CSP compared directive by directive: exact values, exactly one policy, and no directive a `<meta>` element cannot enforce |
| `python tools/live_acceptance.py` | **35 checks passed, 0 failed** |
| `node tools/dashboard_run.mjs --site <bucket> --api <api>` | **54 runtime checks passed, 0 failed** against the deployed assets and the live API |
| Fleet state after the deploy | `live_weather`, 5 cities, all NORMAL, no open incidents, demo fleet calm |

### Known residual gaps from this pass (recorded rather than hidden)

* Layout is verified statically. Screenshots at each width, and the console's CSP report, are
  manual steps with no browser on this machine. The one console entry browser QA did find (the
  ignored `frame-ancestors` directive) was fixed by removing its cause and by asserting that the
  served policy carries no meta-unenforceable directive — but the silence itself was verified as
  bytes, not by listening, so the first F12 after this deploy is still the confirmation.
* The dashboard cannot prevent itself being framed: that needs an HTTP response header, and S3
  static website hosting cannot send one (see the security section; the CloudFront header layer
  that would fix it is a new AWS resource and was deliberately not created).
* `web/bg-storm.webp` was audited numerically (13.3 KB, 1760×990, dark, detailed) but could not be
  *looked at* here, so "it still looks good" is a judgement the reader has to make once, in a
  browser.
* Two adversarial tests (`ModeIsolationTest`) assert on readings the live Open-Meteo provider
  returns, and the suite asks the provider for weather many times in ~30 s. When the provider
  answers with `http_503` or a read timeout for one of those calls, the two tests fail *for that
  run* — recorded with the provider's own log line (`weather refresh outcome: … "error":
  "network_error: The read operation timed out"`). Re-running is the fix; the tests are not
  weakened to hide a third party's bad minute.
* The alerting system still reaches nobody: SNS has 0 confirmed subscriptions, so the honest
  wording on the Alerts page is doing real work rather than being a formality.

## M10 plain language on every public view (frontend only, deployed)

Milestone 9 made the *pages* readable. It did not make the *values* readable: the Overview still
printed the model's own output line, which is what browser QA found on the deployed page —

```
risk_score 0.3973 = weighted base 0.3756 (soil_saturation_pct=0.1793, water_level_m=0.0867,
drain_level_m=0.0510, rainfall_mm_per_hr=0.0340, flow_rate_m3_s=0.0245) + trend 0.0000;
dominant driver: soil_saturation_pct
```

— under the heading "Dominant drivers", with the raw tokens `soil_saturation_pct`,
`water_level_m` and `drain_level_m` rendered as chips beside it. That string is the API's own
`latest.explanation`, and the chips are `latest.top_drivers`; the same fields were printed on every
card of the **Cities** view. Nothing in the deployed page was wrong, exactly: it was the model's
transparency, put in front of someone who wanted to know whether to worry about the rain.

### What the browser sees now

| Where | Before | After |
| --- | --- | --- |
| Overview spotlight | `city-chennai · reading 16:33:03Z · 2 min ago`, then the formula block, then "Dominant drivers: `soil_saturation_pct` `water_level_m` `drain_level_m`", with the ring captioned `heuristic-v1 score` | "Risk is currently NORMAL. Soil saturation is the main contributor, while rainfall, drain level, water level and flow rate remain below their watch levels." plus one facts line: "Main risk factor: Soil saturation · Updated 5 s ago · Trend: holding steady", ring captioned `risk score` |
| Overview city card | "Soil saturation is the main factor" (already plain) + `Risk score 0.39` + `Updated 5 s ago` | unchanged in shape, and the trend line no longer says `no lookback` |
| Cities card | "Dominant driver: `soil_saturation_pct`" (a `Top driver` row), a bare weighted-contribution number beside every measurement, and the raw `explanation` printed verbatim under the sparkline | "Main risk factor: Soil saturation", "Risk is currently NORMAL. Soil saturation is the main contributor, …", and a pointer: "Weights, thresholds and the raw model output for this reading are on the Chennai page, under \"Technical details\"." The unexplained contribution number is gone |
| City page | header, then "What is driving risk?" with the variable names, then the measurements | plain sentence ("Risk is currently WATCH…") → advice → measurements → "What is driving risk?" → "Trend and history" → **the model, folded** |
| Data & Methodology | steps, fixed weights, risk bands | the same, plus a folded worked example built from the live reading: `risk score = weighted base + trend`, one line per channel (`soil_saturation_pct × weight 20% = 0.1807`), the sum next to the API's own score, and the API's output line quoted verbatim |

`dominant_driver`, `soil_saturation_pct`, `water_level_m`, `drain_level_m`, `rainfall_mm_per_hr`
and `flow_rate_m3_s` never reach a public view any more: they are rendered as "Main risk factor",
"Soil saturation", "Water level", "Drain level", "Rainfall" and "Flow rate", and a variable the page
has never seen is humanised (`mystery_sensor` → "Mystery sensor") rather than printed with its
underscores.

### Where the engineering detail went (nothing was hidden)

Transparency is a requirement, so the raw material is *moved*, not deleted, and every path to it is
one click away:

* **City page → "Technical details: how is this risk calculated?"** (a native `<details>`, collapsed
  by default, keyboard reachable with no JavaScript): the exact arithmetic, a per-measurement table
  with the variable name, value, watch level, flooding level, fixed weight and weighted
  contribution, the four clocks behind the reading, and `<code data-f="explanation">` holding the
  API's output line byte for byte.
* **Data & Methodology → "The calculation, number by number"**: the same breakdown for the
  highest-risk reading in the live payload, with the sum of the contributions shown next to the
  score the API reported, and the raw line quoted again.
* The Methodology page still names the model (`heuristic-v1`), prints the five weights, the steps
  and the bands, and states what the score is not — that page is where a reader goes *asking* for
  the model, so it stays visible there.

### Four real bugs this uncovered (found by tracing the leak to its source)

* `contributionMap()` keyed the API's contribution entries on `metric`. The API sends `variable`.
  The map was therefore empty on **every real payload**, which silently emptied the driver shares,
  the weighted-contribution column and the "what is *not* driving this" half of the plain sentence —
  no error, just missing information. Both keys are now accepted, and the static fixture (which used
  the invented `metric` key, which is why the harness never caught it) now carries the shape the API
  actually returns, with one entry keeping the legacy key so the alias stays covered.
* `driverBlock()` sized its bars from `entry.points`, a field the API has never sent, so every
  share was 0 and every bar sat empty beside a non-zero number.  It now reads `entry.contribution`.

Two more were found on the way and fixed: `trendIcon()` compared `trendDirection()`'s `'up'`/`'down'`
against `'rising'`/`'falling'`, so **every** trend arrow on the board was the flat one; and the
no-history trend label read `no lookback`, which is the model's shorthand rather than English, on
two public views.

### The check that would have caught it

`tools/dashboard_check.mjs` reads the *static* `index.html`, and the leaked string is composed at
runtime from the payload — which is exactly why 106 passing checks coexisted with that Overview.
`tools/dashboard_run.mjs` now boots the real page with a real payload and asserts, over every
primary view, that the prose contains no snake_case identifier, no `weighted base`, no
`risk_score 0.`, no `heuristic-v1` and no API route — and, in the other direction, that the city
page's fold and the methodology page *do* still contain the formula, every variable name, the
weights and the API's own output line, collapsed (`no <details … open`). The deployed verifier gained
the same needles plus a forbidden list for the copy that leaked.

### What this pass added to the suite

| Harness | Before → after |
| --- | --- |
| `tools/dashboard_check.mjs` | 106 → **108 static checks** per mode (the Overview renderer's own source is now checked for the fields that leaked, and the plain sentence is derived-claim tested: a channel is only called quiet when the payload's own value and threshold say so) |
| `tools/dashboard_run.mjs` | 54 → **58 runtime checks** live (52 → **56** for the demo fleet) |
| `tools/dashboard_scenarios.mjs` | 49 scenarios, unchanged |
| `python -m unittest discover -s tests -t .` | 509 tests, unchanged (frontend-only milestone) |

### Asset budget after this pass (measured)

| Asset | Size |
| --- | --- |
| `index.html` | 26.9 KB |
| `styles.css` | 81.1 KB |
| `lib.js` | 125.4 KB |
| `app.js` | 91.9 KB |
| `bg-storm.webp` | 13.7 KB |
| **Total front end** | **~339 KB of source, five files, zero dependencies** |

### Verified after the change (live, 2026-10-17)

| Check | Result |
| --- | --- |
| `python build/deployed_asset_diff.py` (reads each object back from S3 and compares MD5) | **0 differences** for all five assets: `index.html` 26,891 B `90049377aa0e3326479a50eb72f816e2`, `styles.css` 81,144 B `e456068734e74e3cbc7842d8b13a9ef7`, `lib.js` 125,409 B `06e7c0cc029427dc7454fe5c0610840d`, `app.js` 91,911 B `186fd2e9578f80efa054d589fa2e3693`, `bg-storm.webp` 13,652 B `2fab1efe47404581b86ab917956f625e` (unchanged, not re-uploaded) |
| `node tools/dashboard_check.mjs --payload build/zones-live-weather.json …` | **108 checks passed, 0 failed** (same against the demo fixtures) |
| `node tools/dashboard_run.mjs --payload build/zones-live-weather.json …` | **58 runtime checks passed, 0 failed** (56 for the demo fleet) |
| `node tools/dashboard_run.mjs --site <bucket> --api <api>` | **58 runtime checks passed, 0 failed** against the deployed assets and the live API — and the reading it rendered was a live WATCH: "Risk is currently WATCH. Soil saturation is the main contributor, while rainfall, drain level, water level and flow rate remain below their watch levels." |
| `node tools/dashboard_scenarios.mjs` | **49 scenario checks passed, 0 failed** |
| `node tools/story_check.mjs --dir build/story` | **29 story checks passed, 0 failed** |
| `python -m unittest discover -s tests -t .` | **509 tests, OK** |
| `python tools/verify_deployed_page.py` | **all deployed page contracts hold** — including the new needles (the humanised driver label, the plain reason sentence, the fold's `weighted base` and `Raw model output`) and the new forbidden list (`hero-explain`, `hero-drivers`, `heuristic-v1 score`, `metric-weight`) |
| `python tools/verify_deployed_code.py` | `RESULT: deployed Lambda code is byte-identical to the repository's src/` (no backend change was made) |
| `python tools/live_acceptance.py` | **35 checks passed, 0 failed**. The first run measured 34/35: its first poll hit a cold start at 4.07 s, and with the function warm the five polls measured 942 ms / 977 ms / 1606 ms. The check is the same one as before this pass; nothing in it was relaxed |
| `powershell -NoProfile -ExecutionPolicy Bypass -File tools\deploy_logic_check.ps1` | **all assertions passed** (no AWS write path changed) |
| Fleet state after the deploy | `zone_mode: live_weather`, `data_classification: mixed`, 5 cities — **Chennai WATCH 0.3534**, the other four NORMAL — 4 incidents recorded / **0 open**, alerting `{enabled: true, severities: [WARNING, CRITICAL], cooldown_seconds: 600}` |

### Known residual gaps from this pass (recorded rather than hidden)

* No browser exists on this machine, so "the Overview is now readable" is verified as rendered
  prose and DOM structure (the harness prints what a reader would read), not by looking at it.
* The provenance chips keep their short form (`REAL` / `MODEL` / `SIM`, `LIVE WEATHER`) with a
  tooltip and a per-card legend. They are defined vocabulary rather than engineering detail, and the
  field names behind them (`model_derived`, `simulated`, `live_weather`) are never shown.
* The Cities card still prints the provider's observation clock inside the mode badge
  (`LIVE WEATHER 16:00:00Z`), with a tooltip naming it as the provider's UTC observation time. That
  is a real timestamp rather than model internals, and it stays where provenance matters most.
* The two adversarial `ModeIsolationTest` tests can still fail for one run when Open-Meteo answers
  `http_503` or times out (recorded in M9; unchanged).

## M10 premium UI polish and public email alert subscriptions (frontend deployed, backend held at an IAM gate)

*A note on the label: an earlier follow-up pass in this file is also headed "M10" (the plain-language
pass directly above). With this brief, **M10 means this milestone** — the visual redesign and the
subscription flow — and the roadmap row that used to be numbered M10 (time-to-threshold, alarm,
scheduled refresh) becomes **M11**.*

Two goals, one delivery: make the front end look like a product rather than a console, and let an
ordinary visitor ask for flood alerts by email without inventing a second notification path.

Constraints honoured: no architecture rebuild, no framework, no build step, no npm, no paid service,
no new AWS resource, **no IAM change made by the agent**, and no touch to the risk algorithm,
thresholds, hysteresis, the weather provider, provenance logic, incident semantics, ingestion auth,
the existing `sns:Publish` permission or the database design. The content-security policy is
byte-identical to before this pass.

### Part A — the visual redesign

The palette, the type scale and the components were already defined in M9; M10 is the pass that makes
them *feel* like one product: a backdrop with a scrim, a header that says what the system's state is
rather than what the model is, a hero that answers the visitor's question before it shows any figure,
and four summary cards that carry the same shapes everywhere.

| Element | Before | After |
| --- | --- | --- |
| Backdrop | `bg-storm.webp` at `opacity: .42`, flat page background behind the cards | the same local WebP, centred, held still under a strong navy radial+linear scrim (`.bg-layer::after`) and an inset black vignette. **No new image was needed**: the shipped WebP is a dark rainy-city frame, is already local, already compressed (13,652 B, well inside the 250 KB target / 400 KB hard cap) and has no third-party licensing question |
| Blur | none | slight blur-like softness comes from the scrim gradients *behind* the content, never from `backdrop-filter` or a `filter` on the panels: the brief's "no blur on the actual content" is a rule, not a preference, and CSS text filtering is the single most common cause of smeared paragraphs |
| Bright image detail | visible under text where the page met the raw image | the scrim's darkest band sits exactly where the hero paragraph is, and the hero itself is a panel, so no paragraph is ever read against raw image detail |
| Header | logo, subtitle, model name (`heuristic-v1` in a meta chip), nav, counters as loose text | three groups: brand on the left, the five sections centred on a desktop (a pill for the current one) and, on the right, the system state — "System healthy", "Live data", "Last updated" — as chips. **The model name is gone from the header** (it lives in the Overview's folded technical panel and on the Methodology page, where a reader goes asking for it). The provider countdown moved to the secondary line |
| Hero | a card rebuilt by `innerHTML` around a large ring gauge, a mono line of the API's own output, driver chips, then the fleet sentence | a static panel in `index.html` that `renderHero()` patches field by field: a weather glyph, the kicker `LIVE FLOOD RISK MONITORING`, the answer ("No city is at flood risk right now."), the monitor line ("We are monitoring 5 Indian cities. All are currently below the alert level."), the plain reason sentence, a facts line and the leading city. A short "What FloodPulse does" explanation and one button — **Learn how risk is calculated** — sit beside it. No formula, no model name, no `latest.explanation` |
| Summary cards | four stat tiles, no icons, mixed wording | four cards with a local inline SVG icon each (`highest risk city` / `active alerts` / `cities reporting` / `weather freshness`), a label, a large value, one supporting sentence and a subtle state accent that follows the risk band. The highest-risk card reads exactly like the brief's example: `Highest risk city / Chennai / NORMAL / score 0.35 · updated 5 min ago` |
| City cards | plain text list | a gradient city plate (an abstract city-water glyph, no photograph — the brief forbids stock imagery and there is no locally licensed city photo), the name, the risk band, the score, a short progress meter whose fill is the score and whose ticks are the three documented band edges, rainfall, update freshness and a click affordance |
| Alert preview | absent on the Overview | a "Recent alerts" panel: the two newest active incidents, or the calm state — `✓ No active flood alerts` / "All monitored cities are currently below the alert level." and a `View alerts` button. It reuses the real alert row, so the preview and the Alerts page can never disagree |
| "How FloodPulse works" | absent | three steps with local icons — Collect ("Live rainfall and weather information."), Analyse ("Combine weather and drainage indicators."), Alert ("Create an alert when risk reaches warning levels.") |
| Technical clutter | the model's formula in the spotlight, the model name in the header, dead hero CSS from the previous design | the formula is folded, the header is clean, and the rules for the hero markup that no longer exists (`.hero-gauge`, `.hero-name`, `.hero-meta`, `.hero-slot`, `.hero-empty`, `.hero .pill`, `.ring-lg`) were deleted — 629 bytes of stylesheet that styled nothing |

Everything the M9 pass deliberately kept is still kept: the provenance chips (`REAL` / `MODEL` /
`SIM`, `LIVE WEATHER`) with their legend, the provider observation clock inside the mode badge, the
"Provenance per metric" footnote, the Methodology page naming `heuristic-v1` with the five weights,
and the model in the Overview's folded "Snapshot and technical summary".

One copy defect found and fixed on the way: the hero's fleet sentence read "1 of 5 cities **are** worth
watching" — the subject is the "1", so the verb now follows the count ("1 of 5 cities is", "2 of 5
cities are"). `tools/dashboard_check.mjs` pins both forms.

### Assets changed

| Asset | Change | Size | Notes |
| --- | --- | --- | --- |
| `bg-storm.webp` | **none** | 13,652 B | the existing local WebP was already the right image: dark, rainy, city, local, licensed-by-omission (generated for this project), far under the 250 KB target and the 400 KB cap |
| `index.html` | rewritten hero, header, summary cards, alert preview, three-step block, subscription card; +6,448 B | 33,339 B | no new script, no inline script, policy unchanged |
| `styles.css` | new M10 layer appended (backdrop scrim, header chips, hero grid, stat icons, city plate and meter, alert preview, three steps, subscription form) minus the dead hero rules; +16,104 B | 97,248 B | |
| `lib.js` | `ICON_PATHS.mail`, `SCORE_BAND_EDGES`, `regionNoun`, `monitorLine`, `cityThumb`, `cityMeter`, `alertPreview`, `subscriptionResult`, `subscriptionKindForStatus`; +9,845 B | 135,254 B | no dependency added |
| `app.js` | `renderHero` rewritten to patch the static panel by id, `statCard` grew an icon, `renderAlertPreview`, `setSubscriptionState`, `submitSubscription`; +8,513 B | 100,424 B | |

**Total front end: 379,917 B (≈371 KB) of source across five files, zero dependencies, zero external
requests other than the project's own API.** No CDN, no webfont, no icon font, no image host.

### Responsiveness

The redesign was built phone-first and then given room, and the width sweep is now a harness check of
its own: every one of the six views is booted and rendered at **320, 360, 390, 430, 768, 1024, 1280
and 1440 px**, and at each width the harness asserts that the phone layout and the compact-header
breakpoint resolve to the intended side, that every view opens and fills, that no controller error
reaches the status line and that no city card asks for a fixed width a 320 px screen cannot give.

* No horizontal overflow by construction: every multi-column track added in the M10 layer is
  `minmax(0, …)` or `auto`-bounded, the hero is a two-column grid above 900 px and one column below
  it, the three steps are a row above 900 px and a column below it, the summary cards use
  `auto-fit` down to one column, the subscription row collapses to a stacked field and a full-width
  button under 560 px, and one check refuses any `min-width` declaration above 360 px in the layer.
* The backdrop stays attached to the fixed `.bg-layer`, not to the body with
  `background-attachment: fixed` — same visual result (cover, centred, held still) without the
  full-page repaint that mobile Safari does on every scroll. The reasoning is in the stylesheet
  comment, and the check reads rules rather than that comment.
* The touch targets added here (the subscribe button, the preview button) inherit the existing
  44 px minimum.

### Part B — the public email subscription flow

A visitor anywhere can enter an address on the Alerts page. **AWS SNS does the whole confirmation
flow.** No custom verification token, no second email, no "verify your address" endpoint:

* **`POST /subscriptions`** takes `{"email": "person@example.com"}`, needs no ingestion key, and
  answers **202** with `{"status":"confirmation_required","message":"Check your email to confirm the
  subscription."}`. Nothing else is exposed: not whether the address was already subscribed, not
  whether a confirmation is still pending, not an ARN, not a subscription ARN.
* The handler then calls **`sns.subscribe(TopicArn=…, Protocol="email", Endpoint=<validated
  address>, ReturnSubscriptionArn=True)`** with the ARN read **only** from the Lambda's own
  environment (`FLOODPULSE_SNS_TOPIC_ARN`). A visitor cannot name a topic or a protocol: the request
  body accepts one key, `email`, and any other key is a 400.
* The visitor must click **Confirm subscription** in the mail *Amazon Web Services* sends. Until they
  do, the subscription is `PendingConfirmation` and no alert can reach them.

### What the visitor sees

The card sits between the resolved incidents and the system status, styled as a utility panel with a
mail glyph rather than an advertisement: one sentence of lead copy, the heading "Get flood alerts by
email", the invitation ("Receive an email when FloodPulse detects WARNING or CRITICAL flood risk."),
a labelled field, the button, and three short notes — the SNS confirmation sentence, the privacy
sentence, and the unsubscribe explanation.

| State | What is shown |
| --- | --- |
| idle | the field, the button, nothing else (`#subscribe-result` ships `hidden`) |
| accepted (202) | **"Check your inbox"** + "We sent a subscription confirmation request through Amazon SNS. Click 'Confirm subscription' in that email to activate FloodPulse alerts." |
| invalid address | "Enter a valid email address.", the field marked `aria-invalid="true"`, focus kept in the field |
| rate limited (429) | "Too many subscription requests. Please try again later." |
| SNS failure / offline | "We couldn't create the subscription request right now. Please try again." |
| in flight | the button is `aria-busy="true"` and a second submit is ignored |

**The page never says "You are subscribed."** It cannot know: only SNS knows, and only after the
visitor clicks the link. `tools/verify_deployed_page.py` and `tools/dashboard_check.mjs` both refuse
that string on the deployed page, and the runtime harness asserts the accepted state names a
*confirmation request*.

### Validation, abuse protection and privacy

* **Validation** (in `src/floodpulse/subscriptions.py`, pure and unit-tested): JSON object only —
  an array, a string or a scalar body is rejected; exactly one allowed key; whitespace stripped;
  case-folded for the dedupe and rate-limit identity *while the original spelling is sent to SNS*;
  254-character maximum; CR, LF, NUL and other control characters rejected (so no
  multiple-address or header-injection trick); a shape check on `local@domain.tld`; empty string
  rejected; the body read from a bounded length so a huge payload is refused before parsing; and no
  client-supplied protocol, topic or endpoint.
* **Abuse protection lives in the existing DynamoDB table** — no new resource, no new table, no
  new index: `PK = SUBRATE#<sha256(normalized email)>`, `SK = WINDOW#<hour bucket>`, attribute
  `count`, attribute `ttl`. Three attempts per address per hour, and the attempt is counted *before*
  the SNS call, so a failure storm cannot be used to hammer SNS. The item holds a **hash**, never the
  address: the rate limit works and the table still contains no one's email.
* **Privacy**: FloodPulse stores no visitor email address anywhere. SNS owns the endpoint and the
  confirmation state; the address exists in this system only inside the `subscribe` call. There is no
  marketing list, no analytics, no third-party processor, and the page says so in one sentence.
* **Logging**: the handler never logs the submitted address in full. A rejected address is logged
  truncated through `redact_addresses`, and authorization headers and the ingest key are never logged
  at all.
* The **outer bound** remains the API Gateway stage throttle (still unset, so account defaults apply)
  plus API Gateway's own request-size limit; the per-address window is the application-level bound on
  top of it.

### Test results after this pass

| Check | Result |
| --- | --- |
| `node tools/dashboard_check.mjs --payload build/zones-live-weather.json …` | **118 checks passed, 0 failed** (was 108; both payload modes) |
| `node tools/dashboard_run.mjs --payload build/zones-live-weather.json …` | **66 runtime checks passed, 0 failed** — 64 against the demo fleet (was 58 / 56) |
| `node tools/dashboard_run.mjs --site http://floodpulse-dashboard-…amazonaws.com --api …` | **66 runtime checks passed, 0 failed** against the *deployed* assets and the live API |
| `node tools/dashboard_scenarios.mjs` | **49 scenario checks passed, 0 failed** |
| `node tools/story_check.mjs --dir build/story` | **29 story checks passed, 0 failed** |
| `python -m unittest discover -s tests -t .` | **550 tests, OK** (includes the 33 subscription unit tests and the DynamoDB throttle tests committed in `b5814f1`) |
| `python tools/verify_deployed_page.py` | **all deployed page contracts hold** — plus 14 new M10 needles and 7 new forbidden strings (the model back in the header, an ARN, the topic name, the account id, any unconfirmed "You are subscribed" claim) |
| `python tools/live_acceptance.py` | **35 checks passed, 0 failed**; five polls 956 / 990 / 996 / 1005 ms |
| `powershell -NoProfile -ExecutionPolicy Bypass -File tools\deploy_logic_check.ps1` | **all assertions passed** |
| `python build/deployed_asset_diff.py` | **0 differences** for all five objects (the four re-uploaded ones match the working tree byte for byte; `bg-storm.webp` was not re-uploaded) |

The subscription tests mock SNS locally and assert that the call receives **exactly**
`Protocol="email"`, the validated endpoint and the configured topic ARN — and that a client-supplied
topic or protocol cannot reach the call at all. A separate test reads the rate-limit item back out of
the store and asserts no plaintext address is in it.

### Deployed state after this pass

| Object | Size | MD5 |
| --- | --- | --- |
| `index.html` | 33,339 B | `592bd6a6d2724e99d3b0e78d2a148d6c` |
| `styles.css` | 97,248 B | `a3c6121e0ac841a71781a41443108cbd` |
| `lib.js` | 135,254 B | `3413f80d93f3e7b4157f50b82a21f966` |
| `app.js` | 100,424 B | `8c54b3d5bf2fc72f0dde23587e6dc497` |
| `bg-storm.webp` | 13,652 B | `2fab1efe47404581b86ab917956f625e` (unchanged, not re-uploaded) |

Content types and cache policy are the ones the deploy script has always used (`text/html; charset=utf-8`,
`text/css; charset=utf-8`, `application/javascript; charset=utf-8` with `no-cache`; `image/webp` with
`public, max-age=86400`). Only those four objects were written; the Lambda, the API, the routes, the
table, the topic, the CORS list and every IAM entity were left exactly as they were.

### Backend deployment status: blocked, awaiting one manual IAM permission

The subscription code, its tests and the `POST /subscriptions` route are implemented, tested and
committed (`b5814f1`), but they are **not deployed**. The deployed execution role carries exactly two
inline policies:

| Policy | Action | Resource |
| --- | --- | --- |
| `FloodPulseDynamoDBAccess` | `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `Query` | `arn:aws:dynamodb:eu-north-1:725934295966:table/floodpulse` |
| `FloodPulseSnsPublish` | `sns:Publish` | `arn:aws:sns:eu-north-1:725934295966:floodpulse-alerts` |

`sns:Subscribe` is **absent**, so a deployed `POST /subscriptions` would answer 500. The brief's rule
is explicit — do not apply IAM changes automatically — so the deploy stopped here, as designed.
`infra/deploy.ps1` documents the one manual step in a comment and deliberately does **not** apply it
(a test fails if a later edit widens the automated policy document beyond `sns:Publish`).

> **Manual approval required: allow sns:Subscribe only on floodpulse-alerts.**

The exact minimal change, for the project owner to apply (not by this agent):

The exact command and the exact document are tracked in
[`infra/iam/`](infra/iam/README.md) — a reference for the operator, applied by nothing in this
repository:

```powershell
aws iam put-role-policy --role-name floodpulse-lambda-role `
  --policy-name FloodPulseSnsPublish `
  --policy-document file://infra/iam/floodpulse-sns-publish-and-subscribe.json `
  --profile cloud-project --region eu-north-1
```

The document is the *existing* statement with one action added — same single ARN, no wildcard, no
other topic, no `sns:*`, no managed policy, no trust-policy change:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "FloodPulseSnsPublish",
      "Effect": "Allow",
      "Action": ["sns:Publish", "sns:Subscribe"],
      "Resource": "arn:aws:sns:eu-north-1:725934295966:floodpulse-alerts"
    }
  ]
}
```

`python tools/verify_deployed_code.py` reports the difference honestly in the meantime:

```
only_in_repo: ["floodpulse/subscriptions.py"]
content_differs: ["floodpulse/config.py", "floodpulse/dynamodb_store.py",
                   "floodpulse/handler.py", "floodpulse/notify.py", "floodpulse/storage.py"]
RESULT: deployed code DIFFERS from the repository
```

Nothing else is waiting on that permission: the front-end deploy, the live verification and the whole
test suite are already done. Once the permission is granted, the backend deploy is one command
(`infra/deploy.ps1` — no new resource, no route change beyond the one already in the script) and the
acceptance run gains a malformed-address probe against the live route.

### Known residual gaps from this pass (recorded rather than hidden)

* **No browser and no image viewer exist on this machine.** "The page looks cinematic" is therefore
  verified as DOM structure, computed CSS and rendered prose — the harness prints what a reader would
  read, and the stylesheet is asserted rule by rule. Whether the backdrop reads as *atmospheric* is a
  judgement call made from the source, the file sizes and the fact that the image was already in use.
* The live fleet currently has **0 confirmed SNS subscriptions**, so an end-to-end "a real person got
  a real alert" demonstration still cannot be shown. The subscription flow is the first step toward
  that, and it stops exactly where it must: at the confirmation mail.
* The dashboard still cannot prevent being framed: S3 static website hosting sends no response
  headers, and CloudFront would be a new AWS resource, deliberately not created.
* API Gateway's stage throttle is still unset, so the account defaults remain the outer bound for
  abuse protection (recorded in M8; unchanged).
* One adversarial test (`ModeIsolationTest`) makes real Open-Meteo calls and can fail for a single
  run when the provider answers `http_503`.

## Image assets: the optimisation pass (2026-10-18)

Nothing about the backend, the API, the risk logic or the routes was touched. This pass covers the
raster assets in `web/`, produced by `tools/optimize_web_assets.py`, which is deterministic, never
upscales, never renames, only ever reads or writes the 13 files it lists, and copies each original
to `build/asset-originals/` (gitignored) before overwriting it.

```
python tools/optimize_web_assets.py --dry-run   # plan, measure, write nothing
python tools/optimize_web_assets.py            # back up, rewrite, verify (re-runs skip finished files)
python tools/optimize_web_assets.py --check    # verify what is on disk against the caps
python tools/optimize_web_assets.py --only bg-storm.webp --from-originals   # re-derive one file
                                               # from its kept original, for when a re-encode has
                                               # already cost a generation
```

### What was there first

Four things were wrong with the files as delivered, and the pass records them rather than
inheriting them:

| Finding | Detail |
| --- | --- |
| **Extensions did not match payloads** | `bg-storm.webp` was a **PNG** (930×312, 566 KB, lossless) served as `image/webp`; all seven files named `*.svg` are **PNG** payloads (90–112 px) |
| **A useless alpha channel on every file** | 13 of 13 carried RGBA with the alpha at a constant 255: bytes paid for, nothing shown |
| **EXIF inside every city card** | embedded camera metadata (including, in principle, location) travelled with the images |
| **The backdrop was stored losslessly** | 566 KB for a 930×312 image, 16 bits per pixel, used at 42% opacity under a `screen` blend |

### Measured result (before → after, from the files on disk and the kept originals)

| File | Old dims | New dims | Old size | New size | Ratio | Fidelity vs the original |
| --- | --- | --- | --- | --- | --- | --- |
| `bg-storm.webp` | 930×312 | 930×312 | 565.9 KB | **58.8 KB** | **9.63×** | 33.2 dB PSNR, dark-region error 2.71/255 |
| `chennai-nightscape.webp` | 612×403 | 612×344 | 22.1 KB | 20.6 KB | 1.07× | 37.3 dB, 2.17/255 |
| `mumbai-nightscape.webp` | 860×573 | 800×450 | 37.2 KB | 30.0 KB | 1.24× | 39.6 dB, 1.59/255 |
| `bengaluru-nightscape.webp` | 612×408 | 612×344 | 31.3 KB | 30.2 KB | 1.04× | 36.7 dB, 0.32/255 |
| `hyderabad-nightscape.webp` | 860×572 | 800×450 | 72.2 KB | 54.7 KB | 1.32× | 36.2 dB, 1.44/255 |
| `kochi-nightscape.webp` | 645×360 | 640×360 | 49.7 KB | 47.2 KB | 1.05× | 35.6 dB, 1.15/255 |
| `weather-rain.svg` (PNG) | 105×85 | 105×85 | 10.8 KB | 8.9 KB | 1.21× | **pixel-identical** |
| `city-icon.svg` (PNG) | 90×84 | 90×84 | 9.5 KB | 7.2 KB | 1.31× | **pixel-identical** |
| `alert-icon.svg` (PNG) | 93×90 | 93×90 | 9.4 KB | 7.5 KB | 1.25× | **pixel-identical** |
| `building-icon.svg` (PNG) | 96×88 | 96×88 | 10.4 KB | 7.9 KB | 1.32× | **pixel-identical** |
| `clock-icon.svg` (PNG) | 93×85 | 93×85 | 9.7 KB | 8.0 KB | 1.22× | **pixel-identical** |
| `check-icon.svg` (PNG) | 93×88 | 93×88 | 10.0 KB | 8.2 KB | 1.22× | **pixel-identical** |
| `chevron-right.svg` (PNG) | 112×91 | 112×91 | 9.4 KB | 7.5 KB | 1.25× | **pixel-identical** |
| **Total** | | | **847.5 KB** | **296.7 KB** | **2.86×** | 551 KB saved |

Cards are 16:9 at or below 800×450 (a 612 px source is **not** upscaled to 800: the brief forbids
inventing pixels), cropped around the centre with a bounded detail search, alpha and EXIF dropped,
WebP q82. Icons are **lossless**: opaque alpha dropped, metadata stripped, and the decoded pixels
are asserted identical to the original, which is why the icons are the only files where a visual
comparison is unnecessary. The background keeps its native 930×312: see the gap below.

### The budget rule, and why it moved

The three checks that police size now measure what a visitor actually downloads — the four code
files plus the one image the stylesheet loads — because `web/` also holds prepared card images and
icons that no view references yet and that the deploy list does not upload.

| Measure | Value | Ceiling | Notes |
| --- | --- | --- | --- |
| Page payload (code + backdrop) | **416.5 KB** | **512 KB** (was 400 KB) | 357.7 KB of it is the four code files; the backdrop adds 58.8 KB. The 400 KB ceiling was set when the backdrop was 13.3 KB, so a real photograph cannot fit under it. Raising it was an explicit owner decision |
| Backdrop alone | 58.8 KB | 250 KB ideal / 350 KB hard | the brief's numbers |
| City card | ≤ 54.7 KB | 90 KB ideal / 120 KB hard | the brief's numbers |
| `web/` directory | 654.3 KB | 1024 KB | a guard for the prepared set, not a download |

### Readiness gaps (recorded, not silently fixed)

1. **The seven files named `*.svg` are PNGs.** They are not SVGs at all, so "minify the SVG icons"
   has no subject: converting them to vector is not possible, and rasterising is what they already
   are. They were therefore optimised **losslessly in place** and keep their names, because the
   rename is a decision for their author: either rename them to `*.png` (nothing references them
   yet, so it is free) or re-export real SVG from the design source.
2. **The backdrop cannot reach the brief's 1920×1080 target from what exists.** The file is
   930×312 (2.98:1); 1920×1080 is 1.78:1 and would need a 2.06× enlargement, which the brief
   forbids and which would add no detail while quadrupling the bytes. It is therefore kept at its
   native size, and on a 1920 px display the browser upscales it 2.06× (4.1× at device resolution).
   Meeting the target needs a larger source; the previously committed backdrop (1760×990) was
   already near it.
3. **Nothing here is deployed.** The five-asset upload list in `infra/deploy.ps1` is unchanged, so
   the live bucket still serves the 579 KB PNG-as-`.webp` and does not carry the twelve prepared
   assets. Deploying is a separate, explicit step. *(Closed by M10.1 below: the bucket now serves the
   58.8 KB WebP backdrop and the five card images.)*
4. **The city cards and icons are not wired into any view.** They are prepared and measured only;
   no layout, CSS or HTML was changed for them. When they are wired up, `object-fit: cover` makes
   the mix of 800×450 and 612×344 cards harmless, and the PNG-under-`.svg` names must be settled
   first. *(Closed by M10.1 below for the five card images, using exactly that `object-fit: cover`;
   the seven `*.svg`-named PNGs are still referenced by nothing and still wait for their author.)*
5. **No image viewer exists on this machine**, so "no obvious banding" and "the skyline stays
   recognisable" are argued from measurements — PSNR, error in the darkest decile, and the fraction
   of distinct dark luminance levels that survive (1.00 everywhere) — and not from looking at the
   pictures. Subjective judgement of the crops is still owed by a human.
6. **One file was encoded twice** before the guard existed: the backdrop was re-encoded from my own
   output, which cost a generation (32.1 dB cumulative instead of 33.2 dB). It was re-derived from
   the kept original with `--from-originals` and re-verified; the tool now skips any file already
   inside its targets so a re-run cannot repeat it.

## M10.1 wiring the city imagery into the dashboard (frontend + S3 objects only, 2026-10-18)

The optimisation pass prepared six raster files and left every one of them unreferenced. This pass
puts them on the screen, and does nothing else: the Lambda, the API Gateway routes, the table, the
risk engine, the alert topic, the subscription backend and every IAM policy are untouched. The
deploy path that published the objects — `infra/deploy.ps1 -DashboardOnly` — cannot reach them.

### The mapping

Five cities, five files, one table in `web/lib.js` (`FP.CITY_IMAGES`), read through
`FP.cityImageFor()`. The lookup is an own-property test, so an id the page has never heard of —
including `constructor` or `toString`, or anything that arrives in a payload — resolves to `null`,
the card keeps its drawn band-tinted plate, and the card's shape never depends on the data. The path
is never assembled from API text; a wrong or hostile `zone_id` cannot choose what the page loads.

| `zone_id` (from the API) | File in `web/` | Size | Frame |
| --- | --- | --- | --- |
| `city-chennai` | `chennai-nightscape.webp` | 20.6 KB | 612×344 |
| `city-mumbai` | `mumbai-nightscape.webp` | 30.0 KB | 800×450 |
| `city-bengaluru` | `bengaluru-nightscape.webp` | 30.2 KB | 612×344 |
| `city-hyderabad` | `hyderabad-nightscape.webp` | 54.7 KB | 800×450 |
| `city-kochi` | `kochi-nightscape.webp` | 47.2 KB | 640×360 |

### What changed on screen

* **City cards carry a photograph band.** One builder (`FP.cityThumb`) serves the Overview grid and
  the Cities page, so the two cards are two compositions of one image element, not two
  implementations that drift: a 16:9 band at `object-fit: cover` (which is what makes the 612×344
  and 800×450 sources equivalent), a bottom gradient so the name never meets a bright skyline, and
  the band-tinted gradient underneath for an unmapped zone or a failed load.
* **The Overview card is now a navigation card**: image, city name with a chevron, the band in
  words, the score in figures, the band meter, then one compact secondary line — rainfall, age, and
  the provenance chips. The headline sentence, the driver sentence, the soil figure and the
  provenance chips are no longer on *this* card; the driver sentence moved to the Cities card, and
  everything remains on the city detail page. The direction the score is moving stayed (the arrow
  sits beside the figure it describes).
* **Grid**: one row of five from 1320 px, three at 1024, two at 768, one at 560 and below. Every
  track is `minmax(0, …)`, so a long city name wraps instead of pushing the page sideways.
* **Surfaces**: the panel gradient dropped from ~72–78% to ~60–66% opacity with a 7 px scrim, the
  borders are a cool blue, and the hero is lighter still, so the photograph reads through the large
  panels instead of sitting behind an opaque navy wall. Text stays opaque. *(Superseded by M10.2:
  the surfaces were re-tuned to 0.74–0.86 once the backdrop was actually visible — at 60% over an
  invisible photograph the panels were translucent against nothing.)*
* **Header**: same three groups in the same order (brand + subtitle, the five views, fleet health /
  Live badge / last update), tightened padding so the bar is mostly navigation. `heuristic-v1` is
  still out of the header and still in the Data & Methodology fold.
* **Backdrop**: `background-size: cover` over the full-bleed layer, anchored `center top` so the
  skyline sits where a reader looks, under the navy ramp and the inset vignette. *(Superseded by
  M10.2: the ramp and the vignette were stacked so heavily — and behind a `screen` blend mode — that
  the photograph came out at roughly 5% of its own brightness. The numbers are in the next
  section.)*

### The two payload numbers, kept apart on purpose

| Measure | Value | Ceiling | Notes |
| --- | --- | --- | --- |
| Primary page payload (code + backdrop) | **429.6 KB** *(M10.1; 438.6 KB after M10.2)* | **512 KB** | 32.6 + 104.0 + 135.8 + 98.5 KB of code, plus the 58.8 KB backdrop |
| Card imagery | **182.7 KB** for five files | 90 KB ideal / 120 KB hard each | the images only: four load as the first row enters the view, the fifth is `loading="lazy"` |
| Worst case, one desktop visit | 600.8 KB | — | page payload plus all five images: a wide desktop shows five cards at once, so "below the fold" is a phone-shaped statement, not a desktop one |
| `web/` directory | 667.5 KB, 17 files | 1024 KB | a guard on the prepared set, not a download |

Imagery does not enter the primary payload: the stylesheet loads the backdrop and nothing else, and
every card image is written by the renderer with `loading="lazy"` except the first four, which are
left to the browser's own eager behaviour because they are the ones on screen. No custom lazy
loader, no intersection observer, no script of ours in the image path at all.

### Publishing them

`infra/deploy.ps1` gained one shared asset table (`Get-DashboardAssets`, ten objects) used by both
the full deploy and a new `-DashboardOnly` path, which re-uploads `web/` and verifies it and leaves
`exit 0` before the build, table, Lambda, API, IAM, SNS and log-retention sections — so publishing a
stylesheet cannot become an excuse to ship a backend change. After uploading, the script reads every
object back and compares its ETag with the local file's MD5 and its stored metadata with what was
asked for; a mismatch fails the run. The five card images are uploaded as `image/webp` with
`public, max-age=86400`: a day, not a year, because the filenames are not content-hashed and an
`immutable` year would keep a replaced nightscape out of a returning visitor's browser. `s3 cp` is
idempotent, no bucket is created, and no policy, website configuration or lifecycle rule is touched.

### What is checked, and what is not

`tools/dashboard_check.mjs` (125 checks) asserts the table itself, that all five files exist, parse
as VP8/VP8X WebP containers at 16:9 inside 800×450 and under the 120 KB cap, that the seven
`*.svg`-named files are PNG payloads referenced by nothing in `index.html`, `styles.css`, `lib.js` or
`app.js`, that the mapping cannot be reached by construction, that `img-src 'self' data:` was not
widened, and that `img-src`'s companion rules still hold. `tools/dashboard_run.mjs` (66 checks)
drives the real renderer and asserts each card's own image and its attributes (`alt=""`, a reserved
800×450 box, `decoding="async"`, lazy except the first four), that the score, the band in words and
the meter survive, and that the demo fleet — invented zones with no nightscape — loads no image at
all. `tools/deploy_logic_check.ps1` asserts the ten-object table, the content types, the cache
policy, and that the front-end-only path contains no AWS write of its own.
`tools/dashboard_scenarios.mjs` keeps the escaping proof: the one `<img>` a card may contain is the
renderer's own, and a payload-injected tag is still a failure.

The pixel-level decode is not in those harnesses: `tools/optimize_web_assets.py --check` does it
with Pillow (dimensions, decode, PSNR and dark-region error against the kept originals), and the
harnesses check the container and frame headers, which is what catches a PNG wearing a `.webp`
name.

### Limitations (recorded, not smoothed over)

1. **The backdrop is soft at large widths, and this is not fixable from what exists.** The file is
   930×312; at a 1920 px viewport the browser upscales it 2.06× (4.1× at device pixel ratio 2). No
   detail is invented on disk — that would be fake detail — so the claim here is "covered, centred,
   atmospheric", not "native 1080p".
2. **No image viewer exists on this machine.** Every judgement above is numeric. Whether the crop
   keeps each skyline recognisable, and whether the dark gradient is too heavy or too light over a
   given card, is still a human's call.
3. **The seven `*.svg`-named PNGs remain unreferenced** and still carry misleading extensions. They
   are not deleted (they were added from outside this work) and not referenced; the inline SVG icon
   set is the icon system. Renaming or re-exporting them is a decision for their author.
4. **Column counts are derived, not observed.** 5/3/2/1 comes from the grid maths over the measured
   page padding and card gap, and the check prints that derivation — but no browser was available
   here, so the actual rendering at each width has not been looked at.

## M10.2 making the night city visible (frontend + S3 objects only, 2026-10-18)

The city cards were working and the storm backdrop was not. The review said the page "looks like a
mostly flat navy background" while the target shows the rainy-city environment around the header,
the hero, the cards and the gaps between panels. The image was deployed, was the right file, and was
loading: `bg-storm.webp`, 60,192 B, `image/webp`, byte-identical to `web/`, and `.bg-layer` still had
`background-image: url("bg-storm.webp")` with `background-size: cover` and nothing later touching
either. Nothing opaque covered the page either — `main`, `.view`, `.view-head` and the section
wrappers declare no background at all, and the only full-viewport box in the stylesheet is
`.bg-layer` itself.

### Why it was invisible

Four darkenings, three of them inside the layer, all multiplying the same photograph:

| # | Rule | What it did |
| --- | --- | --- |
| 1 | `mix-blend-mode: screen` on `.bg-layer` | keeps only the lightest values, so the photograph's darks and midtones — skyline, road, reflections — landed within a couple of levels of the page colour. It also *lightens*, which is why the result read as navy with a haze rather than as a picture |
| 2 | `opacity: 0.55` on the same box | multiplied the already-masked group a second time |
| 3 | `.bg-layer::after`, a navy radial at 0.54 in the middle and 0.90 at the edges | painted *inside* the layer, directly on top of the image, under both multiplications |
| 4 | `box-shadow: inset 0 0 180px 60px rgba(2, 6, 23, 0.78)` | a hard 0.78 vignette on the same box, for the same reason |

At the middle of the viewport those four leave `(1 − 0.585) × 0.55 = 0.23` of the photograph, then
screen-blended, so its darks sat about five 8-bit levels from the flat body gradient underneath:
a 0–5% signal. The page also carried a second, competing atmosphere — a four-stop `168deg` body
gradient with `background-attachment: fixed`, which repainted the whole page on every scroll on
mobile Safari and served no purpose the photograph does not serve better.

### What changed

The overlay stopped being a mask over the photograph and became a ramp in front of it, with each
multiplier kept separate and small. Layer order, top to bottom: the `::after` vignette, then a soft
radial, then one navy wash, then the photograph, then the flat `--navy-900` body that is only a
fallback for the moment before the image decodes. `opacity: 1`, no blend mode, no second darkening
box. The base `.bg-layer` rule's own `opacity: 0.42` and `mix-blend-mode: screen` were **deleted
rather than shadowed** by the new layer: a dead declaration that hides the photograph is exactly how
this comes back, and the live contract below searches the served stylesheet for those two strings.
The full arithmetic — every alpha stop and the reason each one is the number it is — is in the
M10.2 comment in `web/styles.css`, and the transmission it produces is printed by the static
harness on every run:

| Probe (1440×900) | Photograph surviving | |
| --- | --- | --- |
| middle of the screen | **38%** | the band the brief asks for is 30–45% |
| behind the header | 35% | the header surface is 0.72–0.84 over it, so the city is perceptible around the navigation, not behind the labels |
| left edge, mid-height | 33% | |
| bottom-left corner | 11% | before the inset vignette, which takes it under 6% |
| a 760 px phone | 34% | stronger vignette, same wash |
| a 430 px phone | 30% | darker again, never removed |

### Surfaces, and why they are not all the same opacity

The token values were re-tuned once there was something behind them to see. A large panel holding a
paragraph needs more body than a tile holding a number, so the four surfaces form a hierarchy
instead of one value: hero and subscription 0.74–0.78, large containing panels 0.78–0.80, ordinary
cards and rows 0.85–0.86, insets inside a panel 0.78, header 0.72–0.84, footer 0.80. The header's
blur halved from 12 px to 6 px, and the panels keep their 7 px scrim — which now has a real job,
because it softens the skyline behind a paragraph. No CSS filter is applied to the photograph
itself, and no blur touches text.

### Readability, measured against the real pixels

`web/bg-storm.webp` is decoded with Pillow, scaled to `cover` at 1440×900, and the overlay stack is
composited over it stop by stop; text then sits on a panel over that composite. Worst case anywhere
on the screen, including the brightest probe:

| Text | On a panel | On a card tile | On the hero | Straight over the backdrop |
| --- | --- | --- | --- | --- |
| body `--slate-200` | 11.9:1 | 14.1:1 | 13.5:1 | 13.7:1 |
| secondary `--slate-300` | 9.9:1 | 11.7:1 | 11.2:1 | 11.4:1 |
| muted `--slate-400` | 5.7:1 | 6.8:1 | 6.5:1 | 6.6:1 |

AA needs 4.5:1 and AAA 7:1, so every combination clears AA and the muted colour clears AA
comfortably. The rendered backdrop has a mean sRGB luma of 14.8 with p95 at 26.4 and a maximum of
59.6: the brightest reflections are twice the median, which is what makes the scene readable as a
scene rather than as noise, and the city lights out-shine the panels at their brightest points —
the "bright wet-city reflections around panel edges" of the target.

### What is checked, and what is not

Two new checks in `tools/dashboard_check.mjs` (127 checks now) assert the property the review found
missing rather than the numbers that happen to satisfy it: that at least 30% of the photograph
reaches the middle of a desktop screen (and not more than 62%, so the overlay cannot quietly stop
doing its job), that the top edge keeps at least 25%, that the corners are darker than the middle by
at least half, that a phone keeps at least 20% and a small phone at least 15%, that no
`screen`-family blend mode is back on the layer, that the effective layer opacity is 1, that `main`,
`.view`, `.view-head` and `.page-shell` still declare no background at all, that the four surfaces
keep their hierarchy and no card is opaque above 0.90. `tools/verify_deployed_page.py` gained the
live needles for the new wash and tokens and two forbidden strings — `mix-blend-mode: screen` and
`background-attachment: fixed` must not reappear in the served stylesheet; run against the previous
deployment those two entries fail, which is how they were confirmed to bite.

### Limitations

1. **The backdrop is dark, and that is arithmetic, not taste.** The photograph's own mean sRGB luma
   is 40; a night city at 38% transmission lands near 15. If the intent is a brighter scene, the
   knob is the alpha stops in the one `.bg-layer` rule — raising the wash from 0.42–0.62 toward the
   brief's 0.50–0.72 is the single line to change, and the harness prints the resulting transmission.
2. **Still no image viewer on this machine, and still no browser.** Every figure above is computed
   from the stylesheet and from the pixels of the file; whether it *looks* right, whether the crop
   keeps each skyline recognisable, and whether the vignette is too heavy at 1920 px are human
   calls, and the pixel inspection is owed by the reviewer.
3. **The mobile figures are the same arithmetic** with the media queries applied in cascade order —
   no device was held, and no browser repaint behaviour was observed.
4. **The backdrop is still a 930×312 source** upscaled 2.06× at 1920 px, so it is soft before any of
   this is considered. That is unchanged from M10.1 and still not fixed by inventing pixels.

## M15 - Coastal coverage map

The Cities page opens with a coastal coverage map of the monitored fleet: where the cities are, which
coast each one belongs to, which one currently carries the highest monitored risk, which ones match the
active search and filters, and how to jump into a city's detail page. The legend carries the same five
risk states as the rest of the page. The map is a navigation and coverage visual, not a hazard product.

The map visualizes monitored city locations only; it is not a state or national flood-risk surface.

Projection: one bounded equirectangular window, latitude 5.5 to 26 N and longitude 66.5 to 95.5 E, ten
units per degree of latitude, longitude scaled by cos(22 deg) so the peninsula is not stretched
sideways, drawn in a 269 x 205 viewBox. Marker positions come from each zone's own latitude and
longitude in the API payload, so changing the grid's sort order never moves a marker.

Andaman and Nicobar: no inset and no second frame. The window is wide enough to hold Port Blair with a
25-unit margin (about a tenth of the height), so the mainland coast and the islands share one frame and
neither is displaced.

Outline source and licence: the coastline is a schematic drawn for this page from the fleet's own
coordinate range; it is not a survey, a chart or a political boundary. No external map service, tile
server, outline file or third-party script is used, and no attribution is invented for data that was
not used. Web Mercator (EPSG:3857) tiles can also only be read as national/state risk surfaces when a
provider is involved, so they are out of scope here.

Interaction: markers are real links to #/cities/<zone_id> and stand on the same risk colour tokens as
the cards, with the band named in text as well as colour. Keyboard focus and pointer hover both select
a city, and the panel beside the map shows its city, state, coast, risk level, score, rainfall and
freshness, plus a route into the detail page. Stale markers are hollow rather than coloured, matched
markers are solid, filtered-out markers dim, and the highest monitored score is emphasised by size and
a thin ring. If the payload is missing, the map dims, the markers clear and the panel says "City risk
data is temporarily unavailable." - no placeholder markers are drawn.

Limitations: the fleet is twelve monitored coastal cities, so coverage follows the fleet and not the
whole coast; the outline is schematic; the map adds no data of its own, makes no separate API call and
does not predict, forecast or represent inundation, rainfall radar, weather warnings or government
advice.

## M14 overnight quality pass (frontend only, deployed)

Two halves: measure the product properly, and make byte room for what the measuring found.

### Four layouts were hiding their own content

`tools/clipping_scan.mjs` asks every visible element on six routes at 320/390/768/1280 px whether it
holds more content than its own box. Four did, three of them hidden by the card around them, which is
why the page-overflow check (and the browser probe, which by design ignores a box an ancestor clips)
never saw them:

| element | measured | cause |
| --- | --- | --- |
| `.spark-row` | 459 px of content in a 322 px box at 390 px, 155 px out at 1280 px | `display: flex` with no `flex-wrap`, beside a `width: 100%` canvas |
| `.metric` | floors of 130 + 90 + 72 px plus a content-sized chip and two gaps: about 356 px in a 322 px column | floors written for a wide card |
| `.rank-class` | a 49 px word in a 46 px track | the track was sized for the shorter band names |
| `.alert-severity` | the CRITICAL pill needs 95 px, the track gave 92 px | same |

The sparkline caption was cut mid-word at **every** width, not only on a phone. It appears once a
session trend exists, so a probe run in the first seconds after a deploy would not have seen it either.

The static harness had pinned those exact metric floors, so it passed while the browser clipped the
row. It now checks the arithmetic - four tracks, floors plus chip plus gaps inside the 322 px that a
768 px card gives - and fails on the old numbers.

### Verified negatives (measured, not assumed)

* navigation: the phone menu closes on a route change, focus lands on the new view, Back and Forward
  both re-render, scroll returns to the top, `aria-current` follows the route (`build/m14_behaviour.mjs`);
* accessibility: one `main`, one visible `h1`, six polite live regions, no unnamed control, every image
  with an `alt`, `prefers-reduced-motion` handled, and a rejected address sets `aria-invalid` and points
  `aria-describedby` at the live result (`build/m14_a11y.mjs`);
* contrast: the faded resolved rows still measure 4.96:1 for body text and 6.49:1 for the CRITICAL
  label, by compositing page, row and opacity rather than reading tokens (`build/m14_contrast.py`);
* no dead code: no uncalled function, no unwritten id, no styled-but-unemitted class
  (`build/m14_deadcode.py`, `build/m14_ids.py`, `build/m14_dead_css.py`);
* bytes: 322 B of CSS for five never-emitted classes removed, five comments still describing the
  fifteen-city fleet repaired, and the phone menu button raised from a 40 px to a 44 px touch target.

### Bytes

| | before M14 | after M14.1 |
| --- | --- | --- |
| `web/` total | 409,073 B | 399,286 B |
| of that, comment lines (terminators included) | 62,666 B | 61,879 B |
| headroom under the 409,600 B ceiling | 527 B | 10,314 B |

The table above counts terminators, because the ceiling is measured against the bytes the site ships.
M14.1 took the 9,181 B that CRLF was spending; see below.

### Checking it

| command | expectation |
| --- | --- |
| `node tools/clipping_scan.mjs <url>` | 0 elements hidden by an ancestor that clips (exit 0) |
| `node tools/dashboard_check.mjs` | 130 checks passed |
| `node tools/dashboard_scenarios.mjs` | 49 checks passed |
| `node tools/story_check.mjs` | 29 checks passed |
| `python -m pytest tests -q` | 597 passed, 2096 subtests |
### M14.1 line endings

The four shipped text files were stored and served with CRLF. They are LF now, and `.gitattributes`
pins those paths to LF (`web/*.html`, `web/*.css`, `web/*.js`) because `core.autocrlf` is true in
this repository and would otherwise write the CRLF back on any Windows checkout. The 23 images and
icons in `web/` are not matched by those patterns and are byte-identical to before.

| file | before | after | lines |
| --- | --- | --- | --- |
| `index.html` | 38,744 B | 38,127 B | 617 |
| `styles.css` | 116,265 B | 112,941 B | 3,324 |
| `lib.js` | 145,967 B | 142,992 B | 2,975 |
| `app.js` | 107,491 B | 105,226 B | 2,265 |
| **total** | 408,467 B | **399,286 B** | saved 9,181 B |

Terminators are all that moved, and that is checkable rather than asserted: every changed file in the
commit has equal insertions and deletions, `git diff --ignore-cr-at-eol` shows nothing but
`.gitattributes`, and `build/m14_1_proof.py` checks per file that the original with CRLF collapsed to
LF is byte-identical to what the index holds. In the browser, a before/after capture of 14 route and
width pairs finds the same visible view, the same 732 stylesheet rules, the same image sizes and the
same computed layout signature for 15 elements; the rendered text is identical in 1,291 of 1,295
lines, and the four that differ are clock readings sixteen seconds apart, which differ between any
two runs of the same files.

## Repository layout

```
AGENTS.md                     project rules for agents
README.md                     this file
src/floodpulse/
  config.py                   3 demo zones + 5 live city zones, thresholds, weights, bands, sources
  risk.py                     deterministic heuristic-v1 scorer + explanation + hysteresis
  validation.py               strict payload validation (server-derived ts_epoch/ttl)
  auth.py                     x-fp-key ingestion guard (fail closed in cloud mode)
  weather.py                  Open-Meteo client: one request for all cities, injectable transport
  live_weather.py             deterministic hydraulics, weather cache item, 10-minute refresh rule
  storage.py                  DynamoDB-shaped item builders + in-memory LocalStore
  dynamodb_store.py           DynamoDbStore: same interface, boto3 imported lazily
  handler.py                  the single Lambda handler: dispatch() + lambda_handler()
  simulator.py                deterministic phase-driven simulator (demo fleet)
web/
  index.html                  dashboard layout: five sections, hero status, health popover, About page
  styles.css                  storm command-centre theme, type scale, icons, skeletons, breakpoints
  lib.js                      pure render/format helpers, icon set, empty states, provenance rendering
  app.js                      router, 5 s polling controller, patching, sparkline, history chart
  bg-storm.webp               locally generated backdrop (13.3 KB, WebP)
DEMO_RUNBOOK.md               rehearsed demo: what to open, when to say what, how to recover
tests/
  test_risk.py                scoring, bands, contributions, hysteresis (fixed vectors)
  test_validation.py          accepted/rejected payload matrix
  test_storage.py             item shapes + store behaviour (readings, incidents, markers)
  test_dynamodb_store.py      key/expression builders + fake-table store tests (offline)
  test_incidents.py           incident lifecycle: open, escalate, no duplicates, close, alerts, cooldown
  test_incidents_api.py       GET /incidents contract + incident/alert fields on POST /readings
  test_notify.py              SNS notifier: subject hygiene, message content, failure redaction
  test_handler.py             routes, error codes, API Gateway v1/v2 adapter
  test_auth.py                ingestion key: env/header handling, 401/503, reads stay open
  test_simulator.py           determinism, ranges, phase/clock behaviour
  test_end_to_end.py          simulator -> validation -> risk -> store -> API, in-process
  test_tools.py               secret handling (never printed) + live simulator request shape
  test_weather.py             Open-Meteo client: URL, parsing, mismatches, failures (offline)
  test_live_weather.py        hydraulics, provenance tags, cache rule, live-mode /zones contract
  test_provenance_labels.py   every route reports the provenance of the data it actually serves
  support.py                  shared test helpers (recording notifier, scenario driver)
tools/
  local_demo.py               CLI: prints a risk timeline, no AWS calls
  live_simulator.py           drives the deployed API with simulated readings
  storm_demo.py               one-command storm demo; verifies the arc and exports story payloads
  story_check.mjs             replays the exported story through lib.js and asserts it holds up
  weather_probe.py            read-only probe of the live Open-Meteo feed (no AWS credentials)
  verify_deployed_code.py     deployed Lambda package vs src/ (read-only, byte comparison)
  verify_deployed_page.py     deployed page contracts: live regions, chart, table roles, no-script
  live_acceptance.py          live acceptance probe over the deployed API (read-only, no secrets);
                              also writes build/zones-live-weather.json and friends as fixtures
  init_ingest_key.py          creates the local secret + Lambda env file (never prints it)
  dashboard_check.mjs         static checks on the dashboard sources (node, no browser)
  dashboard_run.mjs           executes lib.js + app.js in a fake DOM against a real payload (node)
  dashboard_scenarios.mjs     failure/edge scenarios through the real poller in a fake DOM (node)
  dashboard_dom.mjs           the fake DOM + fetch runtime the two harnesses above share
  make_background.py          generates web/bg-storm.webp + WCAG contrast report
infra/deploy.ps1              idempotent deployment (table + Lambda + HTTP API + S3 dashboard)
```

## Running it locally

Requires Python 3.11+ (tested on 3.13.7). **No dependencies, no Docker, no AWS credentials.**

```powershell
# full test suite (597 tests and 2096 subtests, no AWS calls, no network)
python -m unittest discover -s tests -t .

# the demo story, rehearsed end to end: drives the arc, verifies it, exports what the API served
python tools/storm_demo.py --export build/story
node tools/story_check.mjs --dir build/story

# live weather probe: one real Open-Meteo request for the whole coastal fleet, read-only
python tools/weather_probe.py

# dashboard static checks: pure helpers, rendering from a real payload, provenance, site safety,
# the asset budget, and the information architecture (five sections, router, phone menu, icons,
# empty states, touch targets, methodology vs config.py, the coastal fleet and its copy):
# 130 checks
node tools/dashboard_check.mjs
node tools/dashboard_check.mjs --payload build/zones-demo-m4.json --history build/history-demo-m4.json
node tools/dashboard_check.mjs --payload build/zones-live-weather.json --history build/history-live-weather.json

# dashboard scenario harness: 49 failure and edge cases (first-poll failure, 500s, stale feed,
# hung requests, mode switching, history backoff, a hostile payload, an incident with no fields,
# a half-answering fleet...) through the real poller
node tools/dashboard_scenarios.mjs

# dashboard runtime check: runs the real lib.js + app.js in a fake browser and uses the page the
# way a person does — clicking all five sections, opening each city, the alerts split and its empty
# states, the About page, the retry button, the skeleton, the phone menu, a failed poll, recovery,
# and the plain-language check: with a real payload, no primary view may show a snake_case variable,
# the formula or the model's own output line (80 checks live, 78 for the demo fleet), on top of the
# 130 static checks from dashboard_check.mjs.
node tools/dashboard_run.mjs --payload build/zones-live-weather.json --history build/history-live-weather.json --incidents build/incidents-live-weather.json
#   ...or against the deployed assets and the live API:
node tools/dashboard_run.mjs --site http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com --api https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com

# local demo: risk timeline for all three simulated zones (demo fleet)
python tools/local_demo.py --steps 40

# full scenario arc (calm -> flash flood -> recession) and final GET /zones payload
python tools/local_demo.py --steps 100 --json

# against the deployed API instead of in-process
curl.exe -s -o - -w "HTTP %{http_code}" https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com/zones

# serve the dashboard locally (CORS already allows http://localhost:8000)
python -m http.server 8000 --directory web
```

`tools/local_demo.py` pushes simulated readings through the real handler in-process,
so it exercises validation, scoring, storage and the read model end to end without
touching AWS.

## Fixed zones (MVP scope)

| zone_id | name | latitude | longitude | per-zone threshold overrides |
|---|---|---|---|---|
| `zone-riverside-north` | Riverside North | 59.3422 | 18.0561 | none (defaults) |
| `zone-old-town-canal` | Old Town Canal | 59.3245 | 18.0705 | `water_level_m` 1.8/3.2, `drain_level_m` 1.3/2.2 |
| `zone-harbor-east` | Harbor East | 59.3190 | 18.1010 | `rainfall_mm_per_hr` 18/50, `soil_saturation_pct` 72/92 |

Overrides represent different local drainage capacity. They are hand-set tuning values,
not calibration against measured data.

`demo` mode uses exactly these three zones (that is the library default). `live` mode uses the five
M4 city zones listed under **M4 live weather for Indian cities**; their thresholds are the same
heuristic defaults, which are **not** calibrated for those cities and are not official warning
thresholds.

## Sensor payload (`POST /readings`)

```json
{
  "schema_version": "1.0",
  "simulated": true,
  "zone_id": "zone-riverside-north",
  "sensor_node_id": "sim-riverside-north",
  "timestamp": "2026-02-14T10:15:30Z",
  "readings": {
    "rainfall_mm_per_hr": 42.5,
    "drain_level_m": 1.85,
    "soil_saturation_pct": 78.0,
    "water_level_m": 2.4,
    "flow_rate_m3_s": 8.7
  },
  "meta": { "run_id": "sim-20260214", "seq": 128, "phase": "flash_flood" }
}
```

* the client sends `timestamp` only; `ts_epoch` and `ttl` are **derived server-side**
* `timestamp` must be ISO-8601 UTC ending in `Z`, within ±300 s of server time
* `simulated` must be `true`; unknown fields are rejected (strict)
* metric ranges: rainfall 0–250 mm/h · drain level 0–5 m · soil saturation 0–100 % ·
  water level 0–6 m · flow rate 0–50 m³/s
* body limit 4 KB; rejections return HTTP 400 with `field` and `detail`
* `POST /readings` additionally requires the `x-fp-key` header (see **M2 live dashboard**); the read
  routes (`GET /zones`, `GET /zones/{zone_id}/history`, `GET /incidents`) are public and never
  require or reveal the secret

## Risk model (`heuristic-v1`)

Per variable `x` with zone thresholds `watch (w)` and `critical (c)`:

```
sub_score = 0                                  for x <= 0
          = 0.5 * x / w                        for 0 < x <= w
          = 0.5 + 0.5 * (x - w) / (c - w)      for w < x < c
          = 1.0                                for x >= c

risk = clamp( Σ weight_i · sub_score_i + 0.10 · rise_fraction , 0 , 1 )
rise_fraction = clamp((water_level_now_raw − water_level_5min_ago_raw) / 1.0 m, 0, 1)
```

| variable | unit | default watch | default critical | weight |
|---|---|---|---|---|
| `rainfall_mm_per_hr` | mm/h | 15 | 45 | 0.30 |
| `drain_level_m` | m | 1.5 | 2.5 | 0.15 |
| `soil_saturation_pct` | % | 70 | 90 | 0.20 |
| `water_level_m` | m | 2.0 | 3.5 | 0.25 |
| `flow_rate_m3_s` | m³/s | 6.0 | 12.0 | 0.10 |

* metrics are averaged over the last 3 readings before scoring (noise reduction)
* the trend term compares **raw** water levels (now vs ~5 minutes ago); when no lookback
  reading exists the trend contribution is `null`/`0.0`, never invented
* bands: `< 0.35` NORMAL · `0.35–0.60` WATCH · `0.60–0.80` WARNING · `≥ 0.80` CRITICAL
* **hysteresis**: the *effective* class escalates after 2 consecutive readings in a higher
  band and de-escalates after 3 in a lower band (anti-flicker); the raw `risk_class_candidate`
  is always reported alongside the effective `risk_class`
* **explainability**: every result carries per-variable `value`, `sub_score`, `weight`,
  `contribution`, plus `base_score`, `trend`, `top_drivers` and a one-line `explanation`

Deliberately **not** implemented yet: time-to-threshold (no extrapolation is faked). Incident
creation and SNS alerting are implemented (see the M3 section); the incident lifecycle is driven by
this **effective** class, so the hysteresis streaks above govern both opening and closing.

## Simulator

Four phases over 100 steps (5 s apart): `calm(20) → storm_ramp(15) → flash_flood(20) → recession(45)`.
Seeded (default `20260214`) and keyed by zone, so a given seed/zone pair reproduces the
identical sequence — same input, same output, every time. The default start epoch is a fixed
constant so tests never depend on the wall clock.

## Milestones

| # | Slice | Status |
|---|---|---|
| M0 | Local foundation: simulator, validation, risk engine, store, handler, tests | ✅ done |
| M1 | AWS: DynamoDB table, single Lambda + one execution role, HTTP API routes, DynamoDB-backed store | ✅ **done and verified** |
| M2 | Live dashboard (S3 static site polling `GET /zones`) + protected ingestion + restricted CORS — first live demo | ✅ **done and verified** |
| M3 | Flood incidents + SNS alerts + incident log in the dashboard | ✅ **done and verified live** |
| M4 | India live-weather integration: 5 city zones on real rainfall/soil moisture, per-metric provenance, cached external feed, simulator kept as demo/fallback | ✅ **done and verified live** |
| M5 | Dashboard hardening: fleet risk strip, documented colour system, honest failure/stale states, provenance fix on `/history` + `/incidents`, one-command verified storm demo | ✅ **done and verified live** |
| M6 | Public-facing information architecture: Overview / Cities / City / Alerts / Data & Methodology as four routed views with a phone menu, plus the runtime harness to drive them | ✅ **done, verified, deployed** |
| M7 | Final hardening and product quality pass: full-system audit, per-defect fixes with tests, accessibility and mobile work, cost and security review. No new AWS resources, no architecture change | ✅ **done, verified against the deployed stack** |
| M8 | Production hardening / adversarial reliability pass: API abuse and cost protection, replay and reordering under test, provider-failure behaviour, one freshness contract, observability and deployment tooling | ✅ **done, verified against the deployed stack** |
| M9 | Product experience, information design and responsive polish: the Overview answers in one sentence, five routed sections with an About page and flood-safety guidance, a local icon set, designed empty/loading/error states, honest alert-delivery wording, phone-first layout. Front end only: no backend, IAM, threshold or CSP change | ✅ **done, verified live** |
| M10 | Premium UI polish and public email alert subscriptions: a cinematic dark weather-monitoring front end (scrimmed backdrop, state-carrying header, answering hero, icon summary cards, city plates with a risk meter, recent-alert preview, three-step explainer) and a self-service SNS-confirmed email subscription route with validation, hashed-address rate limiting and no stored visitor email. Front end deployed; **backend blocked at the IAM gate pending `sns:Subscribe` on the one topic** | ✅ done (front end live, backend awaiting one approved permission) |
| M11 | Time-to-threshold, CloudWatch alarm, scheduled refresh (EventBridge) | ⛔ planned |

## AWS state (verified)

* profile `cloud-project`, region `eu-north-1`, identity `arn:aws:iam::725934295966:user/cloud-project-dev`
* created by this project, and nothing else: DynamoDB table `floodpulse`, Lambda `floodpulse-api`
  (+ its invoke permission), HTTP API `floodpulse-api` (`2rg2u8quzd`), log group
  `/aws/lambda/floodpulse-api`, S3 bucket `floodpulse-dashboard-725934295966` (M2), SNS topic
  `floodpulse-alerts` (M3, `DisplayName = FloodPulse Alerts`, **0 subscriptions**)
* the execution role `floodpulse-lambda-role` was created by the project owner, not by this repository;
  the M3 inline policy `FloodPulseSnsPublish` (`sns:Publish` on that one topic ARN) was attached by the
  owner as well, because `iam:PutRolePolicy` is denied for the `cloud-project` user (AWS returns
  `AccessDenied … no identity-based policy allows the iam:PutRolePolicy action`)
* no EC2, RDS, CloudFront, VPC, NAT gateway, ECS/EKS or OpenSearch resource exists for this project
* this pass changed **no** resource definition: the Lambda's code was replaced from `src/`, the four
  text objects in the dashboard bucket were re-uploaded, and the CORS allow-list on the existing API
  was refreshed. No table, function, API, route, topic, policy or IAM entity was created, deleted or
  reconfigured, and the ingest key is unchanged (its fingerprint is checked by the deploy script
  before and after a write)
* `cloudformation:*` is **denied** for this profile (verified earlier: `ListStacks`, `DescribeStacks`,
  `ValidateTemplate`) → SAM/CDK/`cfn deploy` are unusable; deployment is per-service AWS CLI calls
* the unrelated role `Video2Product-EC2-SSM-Role` exists in the account and was not touched

## Cost posture

Measured at the end of this pass (read-only AWS CLI, eu-north-1):

| Resource | Usage now | Cost effect |
| --- | --- | --- |
| Lambda `floodpulse-api` | **1,841 invocations** and **441.6 s** of total duration over the two days CloudWatch reports, 0 errors, 0 throttles, 256 MB | ≈110 GB-s: inside the 400,000 GB-s monthly free tier, **$0** |
| API Gateway HTTP API | ~1,841 requests on those two days | $1.00 per million requests: **$0.002** |
| DynamoDB `floodpulse` | **1,364 items / 947 KB**, on-demand, TTL pruning history at 7 days, no GSI, no scans | 25 GB storage free tier: **$0**; writes are a few thousand a day (on-demand write units cost $1.25/million) |
| S3 `floodpulse-dashboard-…` | **10 objects / 612.3 KB** (index, styles, lib, app, the 60,192 B WebP backdrop and the five city nightscapes — all ten re-uploaded and byte-verified on 2026-10-18, which replaced the 579 KB PNG-as-`.webp`) | a few cents **per year**, no CloudFront |
| CloudWatch Logs | **297 KB stored**, 7-day retention | ingestion ~$0.0002, storage negligible; retention bounds it by design |
| SNS `floodpulse-alerts` | **0 confirmed subscriptions**, 1 pending | 1 M publishes/month free; nothing is delivered to anyone |
| External weather API | at most 1 request per 10 minutes (~144/day), free and key-less | **$0** (attribution is the price: CC BY 4.0) |

Idle cost is effectively **$0**; the two days of development and demo traffic above cost under
**$0.01**. No NAT gateway, CloudFront, IoT Core, EC2, RDS, OpenSearch, provisioned capacity or
scheduled refresh exists. Three things are deliberately absent and would be the first additions if
the budget allowed (all are cheap or free, none was created because adding resources needs the
owner's approval):

1. **API Gateway throttling** — `$default` has no burst or rate limit, so a runaway client could
   spend credits and inflate the bill. A burst/rate limit is a configuration change on the existing
   stage, not a new resource.
2. **CloudWatch alarms** — nothing pages anyone if the error rate or latency spikes. The first ten
   alarms are free; with 0 confirmed SNS subscribers an alarm would need an action target first.
3. **Detailed API metrics** — currently off, so per-route latency is only visible in the Lambda's own
   logs rather than in API Gateway metrics.

## Engineering rules

See `AGENTS.md`. In short: work only in this repository, use profile `cloud-project` in
`eu-north-1`, never commit credentials, never fabricate results, prefer the simplest
complete implementation, and do not provision AWS infrastructure without approval.

## Interface notes (kept out of the shipped stylesheet)

The M9 front-end budget keeps the four shipped files under 400 KB total, and the bytes it
counts are bytes on a visitor's connection. The long narratives below were written in
`web/styles.css` while the look was being arrived at; they are kept here word for word, with a
one-line pointer left at each site. Nothing about the rules or the behaviour changed when they
moved: a comment is the one thing in a shipped stylesheet that carries no behaviour.

### backdrop

```
/* -------------------------------------------------------------- backdrop ---
 *
 * The photograph is the shell's atmosphere, and it already arrives on the fixed
 * `.bg-layer` element (M6). It stays there rather than becoming a `body` background:
 * `background-attachment: fixed` on the body makes a full-page repaint on every scroll
 * for mobile Safari, while an element fixed behind the content is composited once. The
 * look is the one this pass is after - image, cover, centred, held still - and the
 * overlay above it is what keeps every word legible.
 *
 * Order, top to bottom: photograph -> navy ramp -> soft edge vignette -> panels. The
 * ramp is strongest where paragraphs sit (the middle of the viewport) and the vignette
 * darkens the four edges, so the picture is felt at the margins and never fights text.
 */
```

### M10.1 the imagery

```
/* ======================================================= M10.1 - the imagery ===
 *
 * Wiring the prepared raster assets into the interface, and the surfaces they sit on. Three
 * decisions are made here and nowhere else:
 *
 * 1. A city nightscape is the *card's* image: one 16:9 band at the top of `.city-thumb`, which
 *    the Overview card and the city list both render from the same builder, with
 *    `object-fit: cover` so a 612x344 file and an 800x450 file land in the same box the same
 *    way, and a gradient at the band's foot so the city name below never meets a bright skyline.
 *    A zone whose id is not in the mapping keeps the drawn plate - the band-tinted gradient that
 *    was there before - so a card never changes shape with the data, and no image is guessed.
 * 2. The backdrop is the 930x312 photograph: cover, centred on the top edge, at 55% over the
 *    page gradient, with the navy ramp and the inset vignette over it. At 1920 CSS px the
 *    browser upscales it 2.06x and it is visibly soft at that width. The alternative was
 *    inventing pixels, so the softness is accepted and reported rather than hidden.
 * 3. Surfaces get lighter, not darker. The photograph has to be visible through the large
 *    panels - that is what "atmospheric" means here - so the panel gradient drops to ~60%
 *    opacity and the blur stays at 7px: enough to soften the skyline behind a paragraph, far
 *    from the frosting that makes small text work to read.
 */
```

### M10.2

```
/* ================================================================= M10.2 ===
 *
 * WHY THE RAIN WAS INVISIBLE. The photograph was on the page the whole time, was the right
 * file, and was never overridden: `.bg-layer` still had `background-image: url("bg-storm.webp")`
 * and `background-size: cover` with nothing later touching either. It was invisible because four
 * darkenings stacked on top of each other, three of them inside the layer itself:
 *
 *   1. `mix-blend-mode: screen` on the layer. Screen keeps the *lightest* values: over a dark
 *      navy backdrop the photograph's darks and midtones - the skyline, the road, the wet
 *      reflections - came out within a couple of levels of the page colour, so no scene survived.
 *      Worse, it lightens: the result read as navy with a haze, never as a photograph.
 *   2. `opacity: 0.55` on the layer, multiplying the already-masked group a second time.
 *   3. `.bg-layer::after`, a navy radial at 0.54 in the middle and 0.90 at the edges, painted
 *      *inside* the layer, i.e. directly on top of the image and under both multiplications.
 *   4. `box-shadow: inset 0 0 180px 60px rgba(2, 6, 23, 0.78)`, a hard 0.78 vignette on the same
 *      box, for the same reason.
 *
 * The arithmetic those four produce at the middle of the viewport is (1 - 0.585) x 0.55 = 0.23 of
 * the photograph, then screen-blended, so its darks landed about five 8-bit levels away from the
 * flat body gradient underneath: a 0-5% signal. That is the "flat navy" the review saw. Nothing
 * opaque covered the page: `main`, `.view`, `.view-head` and the section wrappers carry no
 * background at all, and the only full-viewport box in the file is `.bg-layer` itself.
 *
 * WHAT CHANGED. The overlay is no longer a mask over the image but a *ramp in front of it*, with
 * each multiplier kept separate and small:
 *
 *   photograph -> one navy wash (linear) -> a soft radial -> the ::after vignette at the edges
 *   -> the panels. `opacity: 1`, no blend mode, no second darkening layer.
 *
 * Transmission of the photograph, computed from these declarations at three points of a 1440x900
 * viewport (this is arithmetic over the alpha stops, not a rendering):
 *
 *   middle of the screen   0.38   the band the review asked for (30-45%)
 *   top edge (behind nav)  0.35
 *   bottom-left corner     0.10   before the inset vignette, which takes it under 0.06
 *
 * So the city is visible around the header, in the page gaps and at the edges of every panel, and
 * the panels stay dark enough to carry text: the surfaces below are 0.74-0.86 - a hierarchy, not
 * one opacity for everything, because a large panel showing 22% of a photograph behind a
 * paragraph needs more body than a stat tile showing a number.
 *
 * Not touched: the image itself (no re-encode, no replacement, still `bg-storm.webp`), the city
 * nightscape mapping, the CSP (`img-src 'self' data:` - the file is same-origin), and every text
 * colour.
 */
```

### M11

```
/* ===================================================================== M11 ===
 *
 * Fifteen cities instead of five. Three things had to appear: where each city is, a way to find
 * one among fifteen, and an Overview that says its cards are the highest-risk few. Almost none of
 * this is new styling - the chips are .ghost, the row is .button-group, the search box is
 * .subscribe-input, the way to the rest is .ghost-link - so what follows is the toolbar's layout,
 * the place lines and the plate's region label, and nothing else.
 */
```

## Interface notes (kept out of the shipped stylesheet)

### M9 - product experience, information design and responsive polish

```
/* ============================================================================
 * M9 - product experience, information design and responsive polish
 * ============================================================================
 *
 * Everything below is additive: the M6-M8 rules above still describe the base look, and this
 * section refines it for a public audience - larger type, calmer surfaces, an icon next to
 * every meaning, honest empty and loading states, and layouts that survive a 320px phone.
 *
 * Colour roles are unchanged and never reused: cyan/teal = live and NORMAL, amber = WATCH and
 * stale, orange = WARNING, red = CRITICAL, slate = unknown. A status colour is always paired
 * with a word, so colour is never the only carrier of meaning.
 */
```

### M10 - premium pass: cinematic shell, grouped header, the hero, city cards

```
/* ============================================================================
 * M10 - premium pass: cinematic shell, grouped header, the hero, city cards,
 *        the alerts preview, the three-step explainer and the subscription form
 * ============================================================================
 *
 * Additive again: the M6-M9 rules above still describe the base, and this layer makes the
 * product read as one deliberate surface rather than a dashboard of separate panels.
 *
 * The three rules kept from the earlier passes, because they are the ones that keep the page
 * readable rather than merely pretty:
 *   * a status colour is never the only carrier of meaning - every one is paired with a word;
 *   * the text always sits on a surface, never directly on the photograph;
 *   * nothing here is animated on a timer, so `prefers-reduced-motion` has almost nothing to
 *     switch off.
 */
```

### backdrop

```
/* -------------------------------------------------------------- backdrop ---
 *
 * The photograph is the shell's atmosphere, and it already arrives on the fixed
 * `.bg-layer` element (M6). It stays there rather than becoming a `body` background:
 * `background-attachment: fixed` on the body makes a full-page repaint on every scroll
 * for mobile Safari, while an element fixed behind the content is composited once. The
 * look is the one this pass is after - image, cover, centred, held still - and the
 * overlay above it is what keeps every word legible.
 *
 * Order, top to bottom: photograph -> navy ramp -> soft edge vignette -> panels. The
 * ramp is strongest where paragraphs sit (the middle of the viewport) and the vignette
 * darkens the four edges, so the picture is felt at the margins and never fights text.
 */
```

### header

```
/* ------------------------------------------------------------------ header ---
 *
 * Three groups, in the order a reader scans them: who this is (left), where to go
 * (centre), and what state the system is in (right). The model name is not here: it is a
 * method detail, and it lives in the folded technical summary on this page and on Data &
 * Methodology, where somebody who wants it goes looking.
 */
```

### hero

```
/* -------------------------------------------------------------------- hero ---
 *
 * The answer card: icon and kicker, one plain sentence, one monitoring line, the reason
 * behind the leading city, and the facts line. The right column says what the product is
 * and points at the method. No formula appears here on purpose - the calculation is one
 * click away, in the collapsed technical section and on the methodology page.
 */
```

### city cards

```
/* ------------------------------------------------------------- city cards ---
 *
 * The thumbnail and the meter are both decoration built from the payload: the plate's hue
 * follows the city's band, and the meter's fill is the score the API reported, with the
 * documented boundaries marked. No photograph is used, because none is licensed here.
 */
```

### alert preview

```
/* ------------------------------------------------------------ alert preview ---
 *
 * "Is anything alerting?" in one block, above the fold of the alerts page itself. The
 * calm state is a state too: it says so in words with a tick, rather than showing an
 * empty list that a reader has to interpret.
 */
```

### responsive

```
/* --------------------------------------------------------------- responsive ---
 *
 * The new layouts collapse in the order the content matters: the aside under the hero,
 * the three steps into a column, the form's button under its field. Every track is
 * `minmax(0, …)`, so a long address or a narrow phone can never introduce a horizontal
 * scrollbar.
 */
```

### M10.1 the imagery

```
/* ======================================================= M10.1 - the imagery ===
 *
 * Wiring the prepared raster assets into the interface, and the surfaces they sit on. Three
 * decisions are made here and nowhere else:
 *
 * 1. A city nightscape is the *card's* image: one 16:9 band at the top of `.city-thumb`, which
 *    the Overview card and the city list both render from the same builder, with
 *    `object-fit: cover` so a 612x344 file and an 800x450 file land in the same box the same
 *    way, and a gradient at the band's foot so the city name below never meets a bright skyline.
 *    A zone whose id is not in the mapping keeps the drawn plate - the band-tinted gradient that
 *    was there before - so a card never changes shape with the data, and no image is guessed.
 * 2. The backdrop is the 930x312 photograph: cover, centred on the top edge, at 55% over the
 *    page gradient, with the navy ramp and the inset vignette over it. At 1920 CSS px the
 *    browser upscales it 2.06x and it is visibly soft at that width. The alternative was
 *    inventing pixels, so the softness is accepted and reported rather than hidden.
 * 3. Surfaces get lighter, not darker. The photograph has to be visible through the large
 *    panels - that is what "atmospheric" means here - so the panel gradient drops to ~60%
 *    opacity and the blur stays at 7px: enough to soften the skyline behind a paragraph, far
 *    from the frosting that makes small text work to read.
 */
```

### M10.2

```
/* ================================================================= M10.2 ===
 *
 * WHY THE RAIN WAS INVISIBLE. The photograph was on the page the whole time, was the right
 * file, and was never overridden: `.bg-layer` still had `background-image: url("bg-storm.webp")`
 * and `background-size: cover` with nothing later touching either. It was invisible because four
 * darkenings stacked on top of each other, three of them inside the layer itself:
 *
 *   1. `mix-blend-mode: screen` on the layer. Screen keeps the *lightest* values: over a dark
 *      navy backdrop the photograph's darks and midtones - the skyline, the road, the wet
 *      reflections - came out within a couple of levels of the page colour, so no scene survived.
 *      Worse, it lightens: the result read as navy with a haze, never as a photograph.
 *   2. `opacity: 0.55` on the layer, multiplying the already-masked group a second time.
 *   3. `.bg-layer::after`, a navy radial at 0.54 in the middle and 0.90 at the edges, painted
 *      *inside* the layer, i.e. directly on top of the image and under both multiplications.
 *   4. `box-shadow: inset 0 0 180px 60px rgba(2, 6, 23, 0.78)`, a hard 0.78 vignette on the same
 *      box, for the same reason.
 *
 * The arithmetic those four produce at the middle of the viewport is (1 - 0.585) x 0.55 = 0.23 of
 * the photograph, then screen-blended, so its darks landed about five 8-bit levels away from the
 * flat body gradient underneath: a 0-5% signal. That is the "flat navy" the review saw. Nothing
 * opaque covered the page: `main`, `.view`, `.view-head` and the section wrappers carry no
 * background at all, and the only full-viewport box in the file is `.bg-layer` itself.
 *
 * WHAT CHANGED. The overlay is no longer a mask over the image but a *ramp in front of it*, with
 * each multiplier kept separate and small:
 *
 *   photograph -> one navy wash (linear) -> a soft radial -> the ::after vignette at the edges
 *   -> the panels. `opacity: 1`, no blend mode, no second darkening layer.
 *
 * Transmission of the photograph, computed from these declarations at three points of a 1440x900
 * viewport (this is arithmetic over the alpha stops, not a rendering):
 *
 *   middle of the screen   0.38   the band the review asked for (30-45%)
 *   top edge (behind nav)  0.35
 *   bottom-left corner     0.10   before the inset vignette, which takes it under 0.06
 *
 * So the city is visible around the header, in the page gaps and at the edges of every panel, and
 * the panels stay dark enough to carry text: the surfaces below are 0.74-0.86 - a hierarchy, not
 * one opacity for everything, because a large panel showing 22% of a photograph behind a
 * paragraph needs more body than a stat tile showing a number.
 *
 * Not touched: the image itself (no re-encode, no replacement, still `bg-storm.webp`), the city
 * nightscape mapping, the CSP (`img-src 'self' data:` - the file is same-origin), and every text
 * colour.
 */
```

### surfaces

```
/* ------------------------------------------------------------------ surfaces ---
 *
 * Four surfaces, four jobs. The photograph is visible between them; through them it is a
 * suggestion. Numbers are percentages of opacity, so a reader can compare them to the brief:
 * hero 0.74-0.78, large panel 0.78-0.80, ordinary card 0.85-0.86, inset row 0.78. */
```

### phones

```
/* ------------------------------------------------------------------- phones ---
 *
 * The photograph stays on the page at every width - it is darker, never gone. A phone is read at
 * arm's length in daylight, so the vignette gets stronger and the picture keeps its job as
 * atmosphere rather than as a picture. Only the vignette and the edge shadow change; the wash
 * above the image is one declaration in this file, shared by every width. */
```

## Interface notes (moved out of the shipped scripts)

The M9 front-end budget counts the bytes of the four shipped files, and a comment
in a shipped file is a byte on a visitor's connection. These narratives were written
beside the code they explain; they are kept here word for word, with the section
banner and a one-line pointer left at each site.

### Plain-language explanation per metric, shown as a help tooltip in the city page.

```javascript
  /* Plain-language explanation per metric, shown as a help tooltip in the city page. A visitor
   * should not have to know what "soil saturation" is to read the page, and a visitor who does
   * know should not have to see the explanation: it is behind a focusable help button.
   * The noun/phrase pairs live with METRIC_PLAIN further down (they predate this table and are
   * used by the sentence builders); the help line belongs here, next to the metric metadata. */

```

### What moved between two polls, in words.

```javascript
  /* What moved between two polls, in words.
   *
   * A board that shows only the current state asks the operator to compare snapshots in their
   * head, which is exactly the work an early-warning display should do for them. Only real
   * movements in the band ladder are reported: a zone that had no reading and now has one has
   * "started reporting" - it has not "escalated". A zone that lost its reading is a data gap
   * and is reported with a WATCH tone, because silence is not safety. */

```

### The API names this field `variable`

```javascript
    // The API names this field `variable`; an earlier fixture (and the harness built on it)
    // called it `metric`. Both are accepted, because reading only one of them left the map
    // empty on every real payload - the weighted contributions, the driver shares and the
    // "what is not driving this" half of the plain sentence all silently disappeared, with no
    // error anywhere. The live payload is checked against this in the runtime harness.

```

### A page that has never reached the API is CONNECTING

```javascript
      // A page that has never reached the API is CONNECTING, and says why it is still waiting.
      // The badge keeps that name on purpose (the detail carries the failure); what must not
      // happen is the loading *pulse* running forever, which is handled by the DOM class the
      // controller adds next to is-connecting.

```

### Everything the page knows about the external weather feed

```javascript
  // Everything the page knows about the external weather feed, derived only from the
  // payload. Missing fields come back as null: nothing here is guessed or assumed fresh.
  //
  // ageOffsetSeconds is the time this page has been holding the payload: the API reports
  // an age that was true when it answered, and adding the local elapsed time is what makes
  // the age (and the countdown) keep moving between polls instead of freezing for 5 s.

```

### The weighted contribution used to sit here as a bare number next to the value. O

```javascript
    // The weighted contribution used to sit here as a bare number next to the value. On a list
    // of cities that is an unexplained figure a visitor cannot use: the measurement, its value
    // and its position against the thresholds are the public facts, and the weighting is part
    // of "how was this calculated" on the city page and the methodology page.

```

### The plain-language reason, not the model's own output string

```javascript
          // The plain-language reason, not the model's own output string. The raw line
          // ("risk_score 0.3877 = weighted base ...") used to be printed here verbatim, which
          // put the formula, the variable names and the arithmetic in front of every visitor on
          // the city list. It stays in the technical disclosures on the city page.

```

### Every engineering identifier the API can send, in words a visitor can read.

```javascript
  /* Every engineering identifier the API can send, in words a visitor can read.
   *
   * The rule for the public views is absolute: a snake_case token never reaches them. A
   * variable this page has not seen before still has to be described somehow, so the fallback
   * strips the unit suffix and the underscores rather than printing the raw name - and the raw
   * name itself stays available in the technical disclosures, where it belongs. */

```

### icons

```javascript
  /* ------------------------------------------------------------------ icons ---
   *
   * One local icon set, defined as SVG path data and inlined into the markup. No icon font, no
   * CDN, no sprite fetch: the page has to satisfy `script-src 'self'` and stay one request per
   * asset, and inline SVG is the only thing that does both. Every icon is drawn on the same
   * 24x24 grid with one stroke width and round caps, inherits `currentColor` so it takes the
   * colour of whatever it sits in, and is hidden from assistive technology by default - the
   * words next to it carry the meaning, and a screen reader should not hear "cloud" twice.
   */

```

### One plain sentence explaining *why* the city is in its band

```javascript
  /* One plain sentence explaining *why* the city is in its band, plus one line of advice.
   *
   * The sentence answers the two questions a visitor actually has: which measurement is pushing
   * the score up, and which ones are not. Both halves come from the API's own contribution
   * entries - the named driver is the one the API listed first, and a measurement is only called
   * quiet when its own value and its own watch threshold say so. Nothing is assumed about the
   * channels that are not driving risk, and no formula, weight or variable name appears here:
   * those live in the collapsed technical section and on the methodology page.
   *
   *   "Risk is currently WATCH. Soil saturation is the main contributor, while rainfall, drain
   *    level and flow rate remain below their watch levels. Conditions are holding steady."
   */

```

### trendLabel() formats the delta ("+0.40 m / 5 min"); trendDirection() is the one 

```javascript
    // trendLabel() formats the delta ("+0.40 m / 5 min"); trendDirection() is the one that
    // answers "which way is this going". Comparing the label to 'rising'/'falling' never
    // matched, so neither sentence could ever appear on a flash-flood board. The numbers stay
    // in the technical table: this sentence is the plain-language one.

```

### '#/cities/city-chennai' -> {view:'city', zoneId:'city-chennai'}

```javascript
  /* '#/cities/city-chennai' -> {view:'city', zoneId:'city-chennai'}, anything unknown ->
   * the default view. Kept tiny and total: the router can never throw on a bad URL, including
   * a fragment that is not valid percent-encoding ('#/cities/%'), where decodeURIComponent
   * throws a URIError that would otherwise abort start() and leave the page never polling. */

```

### One sentence for the top of the page, written for a non-technical reader

```javascript
  /* One sentence for the top of the page, written for a non-technical reader. The noun comes
   * from the fleet's own mode, so demo zones are never described as cities.
   *
   * The verb follows the count, not the fleet: "1 of 5 cities **is** worth watching" is the
   * sentence, because the subject is the "1". It read "are" until M10, which is the kind of
   * detail that makes a monitoring product look translated. */

```

### Overview card: visual navigation, not a mini technical dashboard.

```javascript
  /* Overview card: visual navigation, not a mini technical dashboard.
   *
   * Reading order is deliberate: the city's own nightscape, then its name, then the band in words
   * (the answer), then the score and the meter, then the one line a reader asks next - rain, and
   * how old the reading is. The headline sentence, the driver, the other channels and the
   * provenance chips are all one click away on the city view, which is where somebody who wants
   * them is going anyway; a card carrying them too stops being something you scan. */

```

### The reading order a visitor needs: what is happening (above), the measurements, 

```javascript
      // The reading order a visitor needs: what is happening (above), the measurements, then why
      // the score moved, then how it has been moving - and the model itself last, folded away.
      // What this page does and does not cover, in the API's own words: a visitor reading a
      // city page should not have to infer that the state around it is not being watched

```

### The raw model, folded away by default: native <details>

```javascript
  /* The raw model, folded away by default: native <details>, keyboard accessible, no JS.
   *
   * This is where the engineering detail lives now - the variable names, the weights, the
   * sub-scores, the arithmetic and the model's own output string. Nothing here is hidden from a
   * reader who wants it; it is simply behind a click instead of in front of one. */

```

### The arithmetic behind one reading, written out

```javascript
  /* The arithmetic behind one reading, written out. Every number is the API's own: the weighted
   * contributions are added up and the reported score is quoted beside them, so a reader can
   * check the sum rather than take the ring's word for it. The trend term is only shown when the
   * payload carries one, because the total is not always the sum of the five channels. */

```

### The four different clocks behind one number, named so they cannot be confused.

```javascript
  /* The four different clocks behind one number, named so they cannot be confused.
   *
   * A live reading is stamped when FloodPulse computed it, but the rain it describes was
   * observed by the provider up to an hour earlier, and the fetch that brought it may itself
   * be minutes old. Showing one age and calling it "updated now" hides two of the three. */

```

### loading states

```javascript
  /* -------------------------------------------------------- loading states ---
   *
   * Skeletons rather than the word "loading". The page is polled every few seconds and the
   * first paint happens before the first response, so this is the state a visitor is most
   * likely to meet. The placeholders are aria-hidden: announcing "loading" once per card while
   * the API is answering would be noise, and the polite regions elsewhere already carry the
   * real connection state.
   */

```

### M10

```javascript
  /* ================================================================== M10 ===
   *
   * Presentation pieces for the premium pass, plus the wording of the public email
   * subscription form.
   *
   * Nothing here invents a number: the meter's fill is the score the API reported, the
   * monitoring sentence counts the bands the payload reported, and the alert preview shows
   * the incidents the incidents route returned. The subscription wording is deliberately
   * unable to promise delivery - AWS sends the confirmation email, and until the visitor
   * follows it the subscription is pending, so no state on this page may say "subscribed".
   */

```

### M11

```javascript
  /* ====================================================================== M11 ===
   *
   * Fifteen cities in one fleet. These helpers keep the copy honest - what the payload says the
   * fleet covers, where each city is, and which cities a visitor's own filters leave - and all of
   * them are pure functions of the payload, so nothing here fetches anything.
   */

```

### City nightscape imagery: an explicit, trusted, local mapping

```javascript
  /* City nightscape imagery: an explicit, trusted, local mapping from a zone_id to a file beside
   * this one in web/.
   *
   * It is a lookup table and deliberately not a derived path. A path assembled from API text would
   * let anything that can choose a zone_id address any file on the site - and if such a value ever
   * reached a src, anywhere else. An unknown id therefore gets no image at all rather than a guess,
   * and the card keeps the drawn plate, so the layout never depends on the mapping growing.
   *
   * The five files are the ones prepared by the asset pass (README, "Image assets"). The seven
   * files named *.svg in that directory are PNG payloads with the wrong extension: they are
   * deliberately NOT referenced here, and the icon system stays the inline SVG in this file. */

```

### One image band for every city card - the Overview grid and the Cities page both 

```javascript
  /* One image band for every city card - the Overview grid and the Cities page both call this, so
   * the imagery is a single implementation rather than two that drift.
   *
   * It is decorative: the card names the city in text and labels itself for assistive tech, so the
   * image is alt="" and the band is aria-hidden. A reader hears "Chennai city detail", never
   * "Chennai Chennai city Chennai image", and the risk level never depends on the photograph.
   *
   * The first cards of a grid are left to the browser's default eager loading - they are the ones
   * on screen - and the rest are `loading="lazy"`: native behaviour, no script of ours involved.
   * A zone with no mapped image keeps the drawn plate, band-tinted, so the card shape is constant. */

```

### email alert subscriptions

```javascript
  /* ------------------------------------------------- email alert subscriptions ---
   *
   * A visitor's browser calls POST /subscriptions, which asks SNS to subscribe the address.
   * AWS sends the confirmation message; the browser never sees the topic, the subscription
   * ARN or any AWS error text. These are the only four things the form may say, and none of
   * them claims that anybody is subscribed.
   */

```

### The page states its own API base in a data attribute, not in a script tag

```javascript
  /* The page states its own API base in a data attribute, not in a script tag: the deployed
   * page carries a Content-Security-Policy with no 'unsafe-inline', so there is no inline
   * script allowed anywhere on it. A window global is still honoured for local harnesses, and
   * ?api= still overrides both for testing. */

```

### An API base may be overridden with ?api=<origin> for testing

```javascript
  /* An API base may be overridden with ?api=<origin> for testing, which means it can also be
   * arbitrary text from a link. It is only ever used as a fetch origin and as a display link,
   * so anything that is not plainly an http(s) origin is ignored rather than followed: a
   * javascript:/data: value would otherwise end up in an href, and a relative value would
   * silently fetch from the dashboard host. */

```

### A payload element that is not an object (null, a string, a number) is not a zone

```javascript
  /* A payload element that is not an object (null, a string, a number) is not a zone and not an
   * incident. It is dropped where the list enters state, so every consumer below - the card
   * grid, the ranking, the change note, the session series, the alert view - can rely on
   * objects and one malformed element cannot stop the board updating. The API's own counts are
   * left untouched: they say what the fleet holds, not what this page could render. */

```

### Before the first successful poll the hero holds the copy index.html ships with. 

```javascript
    // Before the first successful poll the hero holds the copy index.html ships with. Once the
    // page knows a request failed it must not keep promising a connection: the headline says
    // what is actually true, and the sentence under it says what happens next. Both are written
    // only from the failure branch, so a still-connecting page keeps its static wording.

```

### The hero: the fleet's answer, then the reason behind the highest-risk city.

```javascript
  /* The hero: the fleet's answer, then the reason behind the highest-risk city.
   *
   * It patches the card that is already in index.html instead of rebuilding it, so the
   * structure (icon, kicker, question, headline, monitoring line, reason, facts, and the way to
   * the method) is on the page before the first poll answers: what a reader sees with scripting
   * off is the same product, just without numbers. Each field is reached by its own id through
   * the same `setText` helper the rest of the page uses, so there is exactly one writer per node
   * and the harness can read each field back the way a browser would read it.
   *
   * Nothing technical goes in here: no formula, no variable name, no model id. The band, the
   * main risk factor, how old the reading is and which way it is going are the whole story,
   * and the calculation is one click away on the city page and Data & Methodology.
   */

```

### The facts line answers "how bad, why, how fresh, which way" in one narrow column

```javascript
    // The facts line answers "how bad, why, how fresh, which way" in one narrow column: it is
    // the same information as the cards below, in the order a reader asks for it. The score and
    // the age are the API's own answer for the leading city (`highest_risk_zone`), not a second
    // reading taken out of the zone table, so this sentence can never disagree with the band the
    // page just announced.

```

### The plain-language reason is patched like every other value on this card

```javascript
    // The plain-language reason is patched like every other value on this card: the card is only
    // rebuilt when the *fleet* changes, so a sentence written once at build time would freeze
    // while the reading behind it moved. The raw model output string is not patched into any
    // visible node: it lives in the city page's technical section, which is redrawn on navigation.

```

### Size the backing store from the laid-out width, like the history chart does. The

```javascript
    // Size the backing store from the laid-out width, like the history chart does. The old
    // code drew a dpr-scaled path into the 280x56 attribute-sized bitmap, so on a HiDPI
    // screen the newest samples were drawn past the right edge and silently clipped, and on
    // a 1x screen a 280 px bitmap was stretched across the card. clientWidth is 0 for a
    // canvas that is not laid out (a hidden view), so fall back to the attribute size.

```

### ``metaHtml`` is deliberately raw HTML, not text

```javascript
  /* ``metaHtml`` is deliberately raw HTML, not text: callers pass markup (a unit, an inline
   * <code>). Every caller must therefore pass markup it built itself or escaped - never a
   * payload string. Anything from the API goes through ``label`` or ``value``, which are
   * escaped here.
   *
   * Each card carries one local SVG icon, its label, the value, and one supporting sentence.
   * The icon is decorative: the label and the sentence are the meaning. */

```

### One public form: an address, a button, and a result that never claims more than 

```javascript
  /* One public form: an address, a button, and a result that never claims more than AWS has
   * done. The browser posts the address to POST /subscriptions; the API asks SNS to subscribe
   * it, and AWS sends the confirmation message. The visitor is only "subscribed" once they
   * follow that link, so the success state says "check your inbox" and nothing else.
   *
   * What the page must never show: the topic ARN, a subscription ARN, an AWS error body, a
   * stack trace, or any HTTP detail. The four states below are the whole vocabulary, and the
   * only thing the form reveals about an address is what the visitor typed themselves. */

```

### Interface notes: the control bar, and one metric as one row (M11.2)

Two layout defects were visible in the M11 dashboard. Both were structural, and both were found by
reading the stylesheet rather than by squinting at it.

**The control bar.** It was a wrapping flex row. The search field asked for `flex: 1 1 260px`, and the
two chip groups were flex children, which cannot shrink below their longest pill. So there was no
width at which the four controls had a defined relationship to each other: they wrapped wherever the
intrinsic widths happened to land. At tablet widths the two chip groups shared a line with nothing to
align to; at 320px the search field's own 260px basis was wider than the content box.

It is one grid now, and the four controls keep the same order and the same names at every width.

| viewport | layout | tracks | minimum + gaps | content box |
| --- | --- | --- | --- | --- |
| 1320px and up | one row: search, region, risk, sort | `minmax(280px, 1.5fr) minmax(360px, 2fr) minmax(300px, 1.5fr) minmax(170px, .8fr)` | 1158px | 1264px |
| 1200-1319px | one row, narrower tracks | `minmax(260px, 1.5fr) minmax(330px, 2fr) minmax(280px, 1.5fr) minmax(160px, .8fr)` | 1060px | 1144px |
| 768-1199px | two rows: search + sort, then region + risk | `minmax(0, 1.15fr) minmax(0, 1fr)` | 0px | anything |
| 767px and below | one stacked column, in reading order | `minmax(0, 1fr)` | 0px | anything |

The content box is the viewport minus two gutters: `--pad-x` is 28px, and 16px at 760px and below.
Each tier's minimum is chosen to fit the viewport that tier starts at, and the arithmetic is
recomputed from the stylesheet itself at eleven widths (320, 360, 390, 430, 600, 768, 900, 1024, 1280,
1440) by `tools/dashboard_check.mjs` - which is also why the widest tracks live *inside* a
`@media (min-width: 1320px)` query: a declaration that only makes sense on a wide screen belongs in
the query that says so, and the M9 guard that asks "can this column shrink?" can then answer correctly.

Every field carries `min-width: 0`; the chip groups are `flex-wrap: wrap` with a `gap`; no pill is
`nowrap`; the pill is sized by its label and only the padding shrinks on smaller screens; every
control is at least 44px tall, the touch target the rest of the site uses. The labels, the search
landmark, the `aria-pressed` state and the keyboard path are unchanged.

**One metric, one row.** A metric row declared four columns - `104px 84px minmax(0, 1fr) 44px` - and
rendered three children, with the provenance chip nested *inside* the label span. So the chip was laid
out as part of a fixed 104px label column, the value sat in the 84px column meant for it, and the
progress bar, the intended fourth child, took the flexible column. That is why "Soil saturation" broke
across its chip, and why values crowded the bar.

The row is `minmax(130px, 1fr) auto minmax(90px, auto) minmax(72px, 1.1fr)` with four sibling cells:
name, chip, value, bar.

* The chip gets a column of its own, so the chips line up down a card and can never land on a value.
* The value is right-aligned, `tabular-nums` and `nowrap`, so numbers line up on one edge and a unit
  never separates from its number.
* The name column is `min-width: 0` with `overflow-wrap: break-word`, so the longest label wraps
  inside its own cell instead of pushing the chip or the value out of the card.
* At 767px and below the row becomes two lines - name and chip, then value and bar - which keeps a
  number with its unit and gives a long label the whole width.

The city-detail tiles took the same treatment: their header wraps instead of squeezing, and the chip
keeps the end of the line. The provenance vocabulary is unchanged and still short: `REAL`, `MODEL`,
`SIM` (and `?` when the API reports nothing), each with its explanation as a tooltip; the raw enum the
API sends is never the visible label outside the technical details.

## M11 - India coverage: fifteen monitored cities, five regions, no new architecture

> **Superseded by M13.** The fleet, the region names and the coverage wording described below were
> replaced: M13 monitors twelve selected coastal cities grouped as West Coast, East Coast and Island.
> The section is kept as the record of what M11 did and why.

FloodPulse monitored five Indian cities. It now monitors **fifteen**, across **fourteen states or
union territories** and five regions used for navigation (North 3, South 5, East 3, West 3,
Northeast 1). Nothing else changed: same Lambda, same HTTP API, same DynamoDB table, same S3 site,
same SNS topic, same IAM, same heuristic and the same bands. No new AWS service, no new resource,
no new IAM statement, and no per-city API route.

| | M10 | M11 |
| --- | --- | --- |
| Monitored cities | 5 | 15 |
| States / union territories | 5 | 14 |
| Regions (navigation only) | - | 5 |
| Authoritative city table | `src/floodpulse/config.py` | `LIVE_ZONE_DEFINITIONS`, 15 rows |
| `/zones` payload | 20,931 B | 59,230 B (11.3% of the 512 KB response cap) |
| Provider requests per 600 s refresh | 1 | 1 (batched, unchanged) |
| Dashboard round trip, warm median | 1.13 s | 1.13 s |
| Lambda duration, weighted average | 380.6 ms | 428.5 ms (cold starts dominate the window) |
| AWS resources | - | none added, none changed |

### One table decides what is monitored

`LIVE_ZONE_DEFINITIONS` in `src/floodpulse/config.py` is the single authoritative list: zone id,
city, state or union territory, region, display name, coordinates, the city's own thresholds and a
short description. The live fleet, the allow-list the validator enforces, the coverage block the
API publishes, the alerts' geography and every sentence on the dashboard are all derived from it,
so a city can never be monitored in one place and absent from another.

**The one that is not obvious:** "monitored" always means a
city, never a state, a region or the country. A card, a sentence or an email may say *Chennai,
Tamil Nadu*; nothing may say *Tamil Nadu is monitored*, and nothing may say *Tamil Nadu is safe*,
because a monitored city says nothing about the rest of its state. `coverage.scope_note` states
that where the fleet is described, the API carries only counts and city names in `coverage`, and
no region gets a risk score of its own.

### Weather: one batched request for the whole fleet

The strategy did not change; its scale did. Before, one refresh covered five cities with one
Open-Meteo request. Now it covers fifteen cities with **one** request - the coordinates are
requested in a single call and the response is split per city - so the number of provider requests
per refresh is still one, the refresh interval is still 600 s, the timeout is still 4 s, and the
per-city cache in DynamoDB (`sk = WEATHER#CACHE`) is unchanged. Measured against the live provider
during M11: 15 cities, **1 request**, 491-character URL, 29,340 B response, 1.25 s, 15 of 15 cities
delivered, 5.6% of the response cap.

Failure is still isolated per city. A city the provider does not answer for keeps its previous
reading and appears in `weather.last_refresh.failed`; every other city is refreshed normally, and a
city whose reading is older than `stale_after_seconds` (1800 s) appears in `stale_zone_ids` and is
marked on the page rather than silently scored. The live fleet at the time of writing reports
`failed: []`, `error: null`, and all fifteen cities `already_fresh` - the normal state when the
dashboard polls faster than the refresh interval.

Hydraulics are unchanged and still global: drain level, water level and flow rate are simulated by
the same deterministic response to real rainfall, with the same constants and the same thresholds,
for all fifteen cities. New cities are not tuned individually and no band or hysteresis value was
retouched; rainfall is still REAL, soil saturation MODEL_DERIVED, and the three hydraulic
measurements SIMULATED, per metric, in every view.

### What the API publishes now

`GET /zones` keeps every field it had and gains:

* per city: `city`, `state`, `region`, `display_name`;
* `highest_risk_zone` with the same four fields, alongside its score;
* `coverage`: `city_count`, `state_count`, `region_count`, `states`, `regions` (each with its
  `city_count` and city names) and `scope_note`.

The change is additive and backwards compatible: a client that reads only `zones`, `zone_count` or
`stale_zone_ids` is unaffected, no route was added or removed, `/zones/<unknown>/history` still
answers 404 `unknown_zone`, and demo mode still serves three simulated zones and omits `state` and
`region` rather than inventing them.

### The Cities page, and what the Overview deliberately stopped doing

The Overview used to show one card per city, which is why it stopped doing that: fifteen cards
would push the alerts, the form and the status panel off the fold. It now shows the highest-risk
few, states the fleet's own total next to them, and links to the rest.

* Overview: fleet totals, reporting count, highest-risk city with its state, alert count,
  freshness, the risk distribution, the top five cards by risk, the dynamic copy saying so, and a
  link to the Cities page.
* Cities page: all fifteen cities. A labelled search over city and state, region chips, risk-floor
  chips (All / WATCH+ / WARNING+ / CRITICAL), three orders (highest risk, A-Z, freshness), a polite
  result count, and an empty state with a way back. A card carries city, state, region, band,
  score, rainfall, main risk factor, "Updated X ago", the provenance chips and a way in.
* Both are client-side. Search, filtering and sorting read the payload the page already has, so
  filtering costs no request: the page's request count is the same at five cities and at fifteen,
  and it is checked that way.
* Every monitored city has its own photograph: the five that shipped with M10 and ten that came
  with this milestone for the new cities. Each was centre-cropped to the card slot's 16:9 and
  re-encoded without the EXIF metadata it arrived with; the originals are kept in
  `build/asset-originals/`, which is not shipped. All fifteen are declared the same way, in the
  deploy script's upload table and in the prepared-asset cap table, and they are checked the same
  way - a real WebP, 16:9, inside the 800x450 slot, under 120 KB, no embedded metadata. A zone
  with no entry in the table draws a plate from its own region and band with an inline icon rather
  than loading an image that does not exist, so there is still no broken image and no 404; with the
  live fleet fully mapped, that path is what the simulated demo fleet gets. The image table stays a
  literal in `web/lib.js` read through an own-property test - payload text never chooses a file.
* Alerts name the city and the state, in the list and in the email; SNS is untouched. The
  subscription flow is untouched.

### Bytes, and the ceiling that did not move

M11.3 removed the CSS nothing renders: fifteen rules whose class names appear in no markup, no
script, no harness and no test, plus eight adjacent rules with identical bodies, which became four.
The `:root` block was left alone even where a custom property looks unread, because it is not private
to the stylesheet: `dashboard_check.mjs` reads `--navy-800` through `token()` for a contrast ratio and
fails if the declaration disappears. A palette entry no rule reads is still part of the palette
contract, so it stays.

The M9 check keeps the four shipped front-end files under 400 KB of source, and M11 adds roughly
25 KB of UI. Rather than raise a ceiling whose own comment says it exists to catch "a framework or
a build step", the bytes came out of shipped commentary: the long design narratives that had
accumulated in `web/styles.css`, `web/lib.js` and `web/app.js` now live in this README, word for
word, each with a one-line pointer at the site they came from. Comments are bytes on every
visitor's connection; the explanation is still one search away, and the stylesheet's rules were
proved byte-identical across the move (89,761 B of rules before and after). Nothing was
minified, no build step was added, and no new file was added to `web/`.

### Verification

* `python -m unittest discover -s tests -t .` - **593 tests**, OK. One test in this suite is
  flaky for a pre-existing reason (it passes in most runs and fails in some): it was reproduced at
  the pre-M11 commit `ccafc9d`, so it is not caused by M11 and was not touched.
* `node tools/dashboard_check.mjs` - **126 static checks**, including five M11 checks for what a
  fake DOM cannot see: the markup's label and landmark, the polite count line, the empty state
  outside the grid, chips built from the payload, the region model staying navigation, and no page
  hardcoding the fleet size.
* `node tools/dashboard_run.mjs` - **82 runtime checks**, including the M11 group that drives the
  search box, the chips and the sort, and pins that filtering costs no request. Also run against
  the deployed site and API.
* `node tools/dashboard_scenarios.mjs` - **49 scenario checks**.
* `node tools/story_check.mjs` - **29 checks**.
* `python tools/live_acceptance.py` - **57 checks** against the deployed API, 0 failed, including
  the fleet's geography and the coverage block.
* `python tools/verify_deployed_page.py` - all deployed page contracts hold, including 30 M11
  needles; `python tools/verify_deployed_code.py` - the deployed Lambda code is byte-identical to
  `src/`.

### What M11 does not claim

* Fifteen cities is not national coverage. India has thousands of urban local bodies; this is a
  fifteen-city fleet and every sentence says so.
* No state, region or national risk score exists, and none is planned without a real reason.
* The hydraulic measurements remain simulated for every city, and the risk score remains the same
  documented heuristic - not a validated hydrological model, not calibrated per city, not an
  official warning.
* There is no map. The design for one (a low-cost, offline-safe India outline with city markers
  placed from the coordinates the API already publishes) is recorded in the M11 report; it was not
  built, because it is not needed to find a city and the milestone's rule was to change as little
  as possible.

## M13 - coastal India: a selected coastal fleet, and the copy that explains it

FloodPulse is now **FloodPulse Coastal India**: a deliberately scoped monitoring prototype for a *selected*
fleet of coastal cities, not a national service. The fifteen-city fleet of M11 is replaced by twelve coastal
cities; the architecture, the cost posture, the risk algorithm, the alerting path and the subscription flow
are untouched.

Two sentences carry the scope, and they are the only ones used:

* "Monitoring selected flood-exposed coastal cities across India."
* "Selected for documented exposure to coastal flooding, extreme rainfall, cyclone/storm-surge hazards, or
  related urban flood vulnerability."

Neither says the fleet is the most exposed, the worst, or complete. The copy states instead that this is
"a selected monitoring fleet, not complete national coverage", and that the cities are "listed in coast order
rather than ranked".

### The twelve cities, and what each one rests on

Coast groups are navigation, in this order: West Coast, East Coast, Island.

| City | State / UT | Coast group | Latitude | Longitude | What it rests on | Source |
| --- | --- | --- | --- | --- | --- | --- |
| Mumbai | Maharashtra | West Coast | 19.0760 | 72.8777 | Named among coastal megacities in an era of sea level rise; coastal-flood hazard rated high for its district; a coastal station recorded 944.2 mm | S1, S8, S2 |
| Surat | Gujarat | West Coast | 21.1702 | 72.8311 | A World Bank project document records exposure to sea level rise, urban flooding and saltwater intrusion; coastal-flood hazard rated high | S6, S8 |
| Kochi | Kerala | West Coast | 9.9312 | 76.2673 | Named among coastal megacities; urban and river flood hazard rated high for its district; a coastal station recorded 253.2 mm | S1, S8, S2 |
| Mangaluru | Karnataka | West Coast | 12.9141 | 74.8560 | A coastal station recorded 360.9 mm, the city's documented extreme | S2 |
| Kozhikode | Kerala | West Coast | 11.2588 | 75.7804 | Urban and river flood hazard rated high for its district; cyclone hazard rated high; a coastal station recorded 283.2 mm | S8, S2 |
| Kolkata | West Bengal | East Coast | 22.5726 | 88.3639 | Named among coastal megacities and studied in a World Bank coastal-city report | S1, S5 |
| Puri | Odisha | East Coast | 19.8135 | 85.8312 | District coastal-flood hazard rated high; named in World Bank project documents for cyclone exposure and for local resilience planning; a coastal station recorded 394 mm | S8, S7, S2 |
| Paradip | Odisha | East Coast | 20.3164 | 86.6115 | A very high risk storm-surge zone of roughly 5-7 m is recorded for the Paradip-Balasore stretch; a coastal station recorded 412 mm | S4, S2 |
| Visakhapatnam | Andhra Pradesh | East Coast | 17.6868 | 83.2185 | Named among coastal megacities; coastal and urban flood hazard rated medium, cyclone hazard rated high; a coastal station recorded 400.6 mm | S1, S8, S2 |
| Chennai | Tamil Nadu | East Coast | 13.0827 | 80.2707 | Named among coastal megacities; coastal-flood hazard rated medium, cyclone hazard rated high; ten coastal stations with extreme rainfall records | S1, S8, S2 |
| Puducherry | Puducherry | East Coast | 11.9416 | 79.8083 | A coastal station recorded 538.1 mm, the city's documented extreme | S2 |
| Port Blair | Andaman and Nicobar Islands | Island | 11.6234 | 92.7265 | An island capital on an island group with a documented extreme-rainfall record of 374.3 mm | S2 |

### The sources, with what each one actually says

Every source below was fetched and read for this milestone. The per-city `selection_reason` in
`src/floodpulse/config.py` is worded to the source it cites; no reason says more than its source does.

* **S1 - Ministry of Environment and Forests, *Final Frontier* (Swaminathan Committee report on coastal
  regulation, 16 July 2009).**
  <https://moef.gov.in/uploads/2018/04/cmz_report_2.pdf>
  Names, in one sentence, "megacities along the coast like Mumbai, Kolkata, Chennai, Kochi, Vishakapatnam,
  etc in an era of sea level rise."
* **S2 - India Meteorological Department, *Rainfall Extremes over Coastal Stations*.**
  <https://rsmcnewdelhi.imd.gov.in/uploads/climatology/RainfallExtremesoverCoastalStations.pdf>
  A station table of record 24-hour rainfall. Rows read for this project include Mumbai/Santacruz 944.2 mm
  (27-7-2005), Surat 459.2 mm (2/7/1941), Kochi Fort 253.2 mm (28-5-1933), Visakhapatnam AP 400.6 mm
  (13-10-2014), Puri 394 mm (7/8/2018), Mangalore 360.9 mm (8/5/1909), Puducherry 538.1 mm (5/10/1991),
  Paradeep CWR 412 mm (6/9/2018), Port Blair 374.3 mm (31-12-1976), Kozhikode 283.2 mm (17-7-2009).
  **A rainfall record is not a flood record**, and no reason in this project treats one as the other.
  Kolkata is not in the fetched text of this table, so Kolkata's basis is S1 and S5 and says so.
* **S3 - World Bank Documents & Reports API** (`search.worldbank.org/api/v3/wds`), used to find and confirm
  document metadata for S5, S6 and S7.
* **S4 - Indian National Centre for Ocean Information Services (INCOIS), *About Storm Surge*.**
  <https://tsunami.incois.gov.in/TEWS/AboutStormSurge.jsp>
  Records a very high risk zone, "East coast of India between Paradip and Balasore in Orissa (~5-7m)".
  This is why Paradip is in the fleet: the source names Paradip itself.
* **S5 - The World Bank (with ADB and JICA), *Climate Risks and Adaptation in Asian Coastal Megacities: A
  Synthesis Report*, September 2010.**
  <https://documents1.worldbank.org/curated/en/866821468339644916/pdf/571100WP0REPLA1egacities01019110web.pdf>
  A multi-city coastal statement that names Indian cities alongside others, and a World Bank-led Kolkata
  coastal city case study in Annex A. The report's main body covers Bangkok, Manila and Ho Chi Minh City, so
  it is **not** used as an Indian city ranking, and the "top ten cities exposed to flooding" line sometimes
  attributed to it is not in it.
* **S6 - The World Bank, *Gujarat Resilient Cities Partnership: Surat Resilience Enhancement Project*,
  Project Information Document (Concept Stage), Report No. PIDC33801, 1 August 2022.**
  <https://documents1.worldbank.org/curated/en/099650108022233180/pdf/P1777991d64d3b7e155e11420d18711110619a142700.pdf>
  Documents Surat as a coastal city exposed to sea level rise, urban flooding and saltwater intrusion.
* **S7 - The World Bank, *NLTA to Support Implementation of Orissa State Climate Change Action Plan -
  Summary Report*, Report No. ACS15742, 27 June 2017, and *Odisha Disaster Recovery Project (P148868)
  Implementation Completion and Results Report*, Report No. ICR00005567, 15 January 2021.**
  <https://documents1.worldbank.org/curated/en/765331498601425141/pdf/P147522-Output-NLTA-Odisha-State-Climate-Change-Action-Plan.pdf>
  and
  <https://documents1.worldbank.org/curated/en/437771643051341119/pdf/India-Odisha-Disaster-Recovery-Project.pdf>
  The first names Puri among three Odisha cities with local resilience action plans; the second names Puri
  district as a coastal area severely affected by Cyclone Phailin in 2013.
* **S8 - ThinkHazard!, a Global Facility for Disaster Reduction and Recovery / World Bank product.**
  <https://thinkhazard.org/>
  Hazard levels for the district or city a monitored place falls in, read for this milestone: Mumbai coastal
  flood high, Surat high, Puri district coastal flood high, Kozhikode urban and river flood high with cyclone
  high, Ernakulam (the district Kochi is in) urban and river flood high, Visakhapatnam coastal and urban
  medium with cyclone high, Chennai coastal medium with cyclone high, Thiruvananthapuram coastal flood very
  low. ThinkHazard reports at district level, which is recorded here rather than smoothed over.

### Selection methodology (city selection only)

This is a method for deciding which cities to monitor. It is not a risk algorithm, not a vulnerability
score, and not a state-level rating; the risk score on the dashboard is unchanged and knows nothing about
it.

1. Candidate places must be on the coast or on an estuary immediately behind it, so that coastal and
   storm-surge hazards apply to them.
2. A place is only selected if at least one authoritative, non-commercial source documents a hazard for that
   place or its district: a national ministry report, the meteorological department, the ocean information
   service, a World Bank project or study document, or a GFDRR/World Bank hazard product.
3. Sources must be fetched and read, not recalled. Where a source rates a hazard at district level, the
   reason says district.
4. The fleet keeps a balance across the coast groups, at least two cities in the largest states that face the
   sea, and only one city per hazard cluster where two cities would duplicate the same evidence.
5. Where a place's evidence is weaker than another place's in the same region, the weaker place is replaced
   and both the replacement and the reason are recorded.
6. Twelve cities: enough to be a real fleet on one screen, few enough that one batched weather request covers
   the whole fleet inside the unchanged provider budget.

What this method deliberately does not do: it does not rank cities against each other, does not compute a
coast-wide or national score, does not average anything per coast, and does not claim that a monitored city
is safer or less safe than an unmonitored one.

### What left active monitoring, and why

Ten cities that M11 monitored are no longer in the fleet: Bengaluru, Hyderabad, Delhi, Guwahati, Patna,
Bhubaneswar, Pune, Ahmedabad, Jaipur and Lucknow. Each is inland, so coastal and storm-surge hazards do not
apply to it, and none of them had a source of the kind the method above requires for a coastal fleet.

Two swaps are worth stating plainly:

* **Bhubaneswar -> Puri.** Bhubaneswar is about 30 km inland. Puri is the Odisha coast, its district coastal
  flood hazard is rated high, and it is named in World Bank project documents. Odisha keeps one monitored
  city, on the coast.
* **Thiruvananthapuram -> Kozhikode.** ThinkHazard rates Thiruvananthapuram's coastal flood hazard *very
  low* - the weakest rating of any candidate. Kozhikode is rated high for urban and river flooding and high
  for cyclone. Kerala keeps two monitored cities either way, and the fleet stays at twelve.

Paradip was kept rather than dropped, even though it sits in the same coastal stretch as Puri: INCOIS names
Paradip itself for a 5-7 m storm-surge zone, which is stronger evidence for that place than a district-level
hazard level.

The image files for the retired cities stay in `web/` (they cost nothing, they are not deleted, and the
deploy manifest is unchanged). The page references only the fleet's own imagery.

### Coast groups are navigation, not a score

`region` is gone from the API, the frontend and the copy. `COAST_GROUPS` is `("West Coast", "East Coast",
"Island")` in `src/floodpulse/config.py`, and each city carries its `coast_group`, `display_name` and
`selection_reason` in that same table, which remains the single source of truth: the frontend holds no city
data of its own and builds the Coast filter from the payload's own grouping.

A coast group carries a name, a city count and its cities. It carries no risk score, no average, and no
safety claim - the runtime and live checks assert the exact key set, so a score cannot creep in. Incidents
are stamped `zone_coast_group`; readers still fall back to the pre-M13 `zone_region` column so an incident
recorded before this milestone keeps its geography.

### What the pages say now

* **Overview**: "Why these cities?" under the hero, with the approved exposure sentence and a "Selection
  methodology" link - deliberately *not* repeating the five highest-risk cities the page already shows. The
  block's limitation line is filled from the API's own coverage scope note, so a cached payload cannot widen
  the claim.
* **Cities**: the control bar is Search city or state / Coast: All, West Coast, East Coast, Island / Risk:
  All, WATCH+, WARNING+, CRITICAL / Sort. Cards name the city, state/UT, coast group, band, score, rainfall,
  freshness, main factor and the way in.
* **City page**: "Why this city is monitored", rendered from that city's own `selection_reason`. A zone with
  no reason - the simulated demo fleet - renders no block at all.
* **Data & Methodology**: a "City selection" section: what a city is selected on, that this is a selected
  monitoring fleet and not complete national coverage, that selection is separate from the live risk score,
  and the five sources with links.
* **About**: "FloodPulse Coastal India is an academic cloud-computing prototype focused on selected coastal
  urban flood-risk locations", with the AWS architecture, provenance and the disclaimer unchanged.
* **Alerts**: unchanged and still city-specific, on the same SNS topic.

### Bytes, and the ceiling that did not move

The shipped-source ceiling stays 409,600 B and was never raised.

| | bytes |
| --- | --- |
| M11.3 baseline | 405,444 |
| after M13 | 409,073 |
| headroom | 527 |

The reclaim was looked for before anything was added, and came up short in a way worth recording: the dead
CSS scan's thirteen candidates are all class names composed at runtime (`'conn-' + state` and friends), so
real dead CSS was zero bytes; the only byte-identical duplicate rules sit in different scoping contexts and
were left alone. The bill was paid out of this milestone's own prose instead - a shorter source list, two
paragraphs where three were drafted, and four-line comments on two-line helpers cut to one line - and every
claim, source and URL survived.

### Weather: one batched request for the coastal fleet

The batching rule is untouched: the whole fleet travels in **one** Open-Meteo request, refreshed at most every
600 s, with a bounded response, partial-failure isolation, a stale fallback, a last-good cache and no retry
storm. Only the coordinate list changed. Measured after the M13 deploy:

| | M11 (15 cities) | M13 (12 cities) |
| --- | --- | --- |
| provider requests per refresh | 1 | 1 |
| request URL length | 491 characters | 431 characters |
| live `/zones` response | - | 53,446 B |
| cities with a cache age | 15 | 12, oldest 0 s |
| cities failing their fetch | 0 | 0 |

The provider's own response size and refresh duration could **not** be re-measured for this milestone: direct
requests from this machine were answered `http_429` (Open-Meteo rate-limiting this IP) while the Lambda's own
refresh was succeeding and serving the cache. That is recorded rather than estimated. The same throttling is
what exposed two tests that had been passing on a live provider call rather than on the code.

### The harness, and the one number that changed

| check | before M13 | after M13 |
| --- | --- | --- |
| `tools/dashboard_check.mjs` (static) | 129 | 130, 0 failed |
| `tools/dashboard_run.mjs` (real page in a fake browser) | 82 | 80, 0 failed |
| `tools/dashboard_scenarios.mjs` | 49 | 49, 0 failed |
| pytest | 593 passed / 2099 subtests | 597 passed / 2096 subtests |

The runtime count moved from 82 to 80 because incident-rendering checks are conditional on the live payload
having an open incident, and the coastal fleet served by the API currently has none (4 closed). Nothing was
removed.

One check's *number* changed: the cap on how many external sites the page may link to rose from 3 to 8, so
the five cited sources fit. The five hosts were added to the explicit allowlist by name, and the rule that
matters is untouched - an external host may still appear only as an `<a href>` destination with
`rel`/`target`/`https`, never as something the browser loads.

Two tests were found to be passing on a live Open-Meteo call rather than on the code: measured while the
provider was throttling, they failed with `http_429` and no reading. They now inject a provider stub that
answers for exactly the coordinates it is asked about, so they test provenance rather than connectivity.

### Verification (commands, and what they reported)

```
node tools/dashboard_check.mjs                  -> 130 checks passed, 0 failed
node tools/dashboard_run.mjs --site <local> --api <endpoint>
                                                -> 80 runtime checks passed, 0 failed
node tools/dashboard_scenarios.mjs              -> 49 scenario checks passed, 0 failed
python -m pytest tests -q                       -> 597 passed, 2096 subtests passed
python tools/live_acceptance.py                 -> 62 checks passed, 0 failed
node tools/dashboard_run.mjs --site <deployed>  -> 80 runtime checks passed, 0 failed
python tools/verify_deployed_page.py            -> all deployed page contracts hold
node tools/browser_probe.mjs <deployed>         -> 351 check lines, 0 failures, 0 script errors,
                                                   0 requests that 404'd, 11 widths, 5 routes
```

The deployed API reports `zone_mode: live_weather`, `zone_count: 12`, coverage 12 cities / 10 states and
union territories / 3 coast groups (5 West Coast, 6 East Coast, 1 Island), every city carrying a
`coast_group`, a `display_name` and a `selection_reason`, and each coast group carrying exactly
`{coast_group, city_count, cities}`.

### What M13 does not claim

* Not complete national coverage, and not a ranking. The cities are listed in coast order and the reason each
  one is monitored is stated; no city is called the most flood-prone, the most vulnerable or the worst.
* No coast-wide and no national risk score, and no average per coast.
* No government affiliation. INCOIS, the India Meteorological Department and the Ministry of Environment and
  Forests are cited as sources, not as partners, and the site still says it issues no official warnings.
* Rainfall records are not flood records, ThinkHazard levels are district-level where noted, and the
  hydraulic measurements remain simulated for every monitored city.
* The World Bank 2010 synthesis is not used as an Indian city ranking, and the "top ten cities exposed to
  flooding" line sometimes attributed to it is not in the document.

## M16 - retrospective temporal intelligence (backend and frontend deployed; no new AWS resources)

The city page has always answered "what is happening now?". M16 makes it answer six more questions, and all
six are about the past: what changed, by how much, when did it peak, how much history do we actually have,
were there meaningful gaps, and which incidents happened during that history.

Nothing in this milestone predicts anything. There is no forecast, no flood probability, no machine
learning, no change to the risk model, no synthetic history, and no invented 24-hour series. Every number is
computed from history rows the API has already stored, and the page says which window it used.

### The quantities, defined once

A **sample** is one stored history row: `ts_epoch`, `risk_score` (heuristic-v1, 0..1), `risk_class` and the
readings stored with it, including `readings.rainfall_mm_per_hr`. A **window** is a slice of the stored
series, and it is measured back from the **newest stored sample**, never from the wall clock, so opening the
page late cannot slide the newest row out of a one-hour window.

| Quantity | Definition |
| --- | --- |
| `risk change` | `r_last − r_first`, the net change across the window |
| duration | `t_last − t_first`, the span the window actually covers |
| peak, minimum | the largest and smallest valid score in the window; the earlier row wins a tie |
| slope | `sum((t_i − t_mean)(r_i − r_mean)) / sum((t_i − t_mean)^2)`, with `t` in hours and `r` the score |
| trend | `RISING`, `FALLING`, `STEADY` or `INSUFFICIENT`, from the slope and the minimums below |
| median interval | the median of the positive intervals between consecutive samples |
| gap | any interval longer than `max(3 x median interval, 900 s)` |
| largest gap | the longest interval in the window, reported whether or not it qualifies as a gap |
| history sufficiency | a window is offered only if the available span covers it; a direction needs at least 3 valid samples spanning at least 600 s |

The net change and the slope are computed over the **valid** rows in the window, after the filtering
described below, and both are rounded only for display (two decimals for the score, one for rainfall in
mm/h).

### The trend tolerance, stated so it can be argued with

A direction is only claimed when the least-squares slope leaves a band of **± 0.02 score per hour**; inside
that band the page says *steady*. The tolerance exists because a stored series that drifts by 0.01 per hour
is indistinguishable on screen from one that does not move, and describing either as "rising" would be a
claim the data does not support. It is a presentation choice, it is disclosed here and on the Data page, and
it is the same number in the browser module, the analysis tool and the tests. A window needs at least three
valid samples and at least ten minutes of span before any direction is named; below that the page says how
many samples it has instead of guessing.

A window that rose and then eased is still reported by its slope, with its peak and its net change printed
beside it, rather than being bent to agree with the shape. A single mid-window peak that closes lower is
neither "rising" nor "falling" in plain language, and the page shows the peak, the close and the slope
separately instead of picking one word for all three.

### Windows

`1H`, `6H`, `24H` and `AVAILABLE` are offered per city, and each button is enabled only when the stored
history actually covers it. Depth moves with ingestion, so the reason is read off the payload and never
hard-coded: on 2026-10-20 at 03:05 IST every one of the twelve monitored cities held **21 h 43 min** of
history (33–35 rows) and `24H` was disabled; at 19:00 IST the same endpoint held **23 h 25 min** (19–21
rows, after the ingestion gap below) and `24H` was still disabled, one hour short of switching itself on.
The button carries the data's own sentence ("only 23 h 25 min of history so far"), never a hidden or
hard-coded rule. `AVAILABLE` is whatever the endpoint returns.

Narrower windows are served from the series already in memory. The page makes **exactly one** history
request per city view; switching from `AVAILABLE` to `1H` does not make a second one. No request is made on
the Overview, Cities, Alerts, Data or About routes.

### Rows that are not usable

Before anything is measured, the series is cleaned deterministically:

* a row whose timestamp cannot be read, or whose score is not a finite number inside `0..1`, is **dropped**,
  counted in the quality line and never interpolated - a gap stays a gap;
* rows are sorted by timestamp before measurement, and the count of out-of-order arrivals is reported;
* two rows sharing a timestamp keep the later one in arrival order, and the merge is reported.

No smoothing, no filling and no averaging across cities is applied to any of these numbers, in the browser
or in the analysis tool. History is stored and read for one city at a time.

### What it costs to load (measured)

`GET /zones/{zone_id}/history?minutes=N` returns a compact row per sample. Measured against the deployed API
on 2026-10-19: **672 B** for the default 60-minute window (1 row, Chennai) and **9,131 B / 9,357 B** for
`minutes=1440` (33 rows Chennai, 34 rows Port Blair). At roughly 277 B per row, a full day at the observed
cadence would be about 40 KB, so **downsampling is unnecessary and is not used**: the endpoint's existing
`downsampled`/`stride` fields stay in the payload, but no M16 feature turns thinning on. The earlier
"thinning dropped the newest sample" bug class is covered by a regression test rather than by avoiding the
code path.

### A worked example

Using the synthetic fixture case `rising` (fixture data, not live data): samples at `+0 s` score `0.10`,
`+600 s` `0.16`, `+1200 s` `0.22`, `+1800 s` `0.31`.

* net change `0.31 − 0.10 = +0.21`, duration 30 min, peak `0.31` at `+1800 s`, minimum `0.10`.
* mean time `t_mean = 0.25 h`, mean score `r_mean = 0.1975`.
* `sum((t_i − t_mean)(r_i − r_mean)) = 0.0575`, `sum((t_i − t_mean)^2) = 0.138889`, so the slope is
  `+0.414` per hour.
* `+0.414` is far outside the ± 0.02 band, so the trend is `RISING`; the sentence under the chart reports
  the range, the ending value, the net change, the slope and the duration.

### What M16 does not claim

* No forecast, prediction, probability, time-to-threshold or expected peak. The trend describes the stored
  past and stops there.
* No trend is named from fewer than three valid samples or less than ten minutes of span, and no direction
  is named at all for a series inside the tolerance band.
* No interpolation across a gap, and no estimate of what happened inside one.
* No accumulation of rainfall: the rainfall chart is an instantaneous rate in mm/h, and the page says so.
* No causal claim: a rising score is not evidence that rain caused it. The current driver is shown as the
  weighted contribution the model already publishes, not as a diagnosis.
* No claim that a stored series is complete. It is what the API stored, and the quality line says how much
  of it there is.

### Limitations (recorded, not smoothed over)

* **Provider observation time is not in the payload.** A history row carries the score timestamp and the
  readings, but no weather-observation timestamp, so whether a reading was a cached provider response
  cannot be determined from the API. What can be said is that the engine already suppresses an exact replay
  at write time, so an unchanged reading does not silently become a second row; and that two samples with
  identical rainfall are *not* presented as proof of a repeated observation.
* **History depth is shallow, and it is the binding constraint.** 21 h 43 min of history at 2026-10-20 03:05
  IST across all twelve cities, 33 to 35 stored rows each; 23 h 25 min at 19:00 IST after the ingestion gap
  below had aged older rows out of the window. The 24-hour window stays disabled until the stored span
  reaches it, and nothing in the page pretends otherwise.
* **The series is not evenly sampled, and ingestion can stop.** Every city showed the same ~20,113 s
  (5 h 35 min) interval in the 03:05 audit — the largest of **9 intervals that exceed the gap threshold**
  in Chennai's window (the threshold there is `max(3 x 601 s, 900 s) = 1,803 s`), with a median interval of
  601 s and no duplicate timestamps. A second audit at 19:00 IST measured a single **33,845 s (9 h 24 min)**
  interval common to all twelve cities: ingestion had stopped at 09:01 IST and resumed when the probe called
  the endpoint, which is why the milestone reports gaps instead of assuming a cadence. The gap is surfaced
  rather than filled, and M16 does not assert why the ingestion paused; M4 and M8 record how it is triggered.
* **Scores are heuristic, not validated.** `heuristic-v1` is an explainable weighted heuristic, not a
  calibrated flood model, and the hydraulic readings remain simulated for every monitored city. A trend
  computed from it inherits that status.
* **The tolerance is a judgement.** ± 0.02 per hour is disclosed rather than hidden, and a reader who
  disagrees can read the slope itself, which the page prints.

### How it is checked

The temporal rules live in exactly one implementation per language and are asserted against a single shared
fixture, `tests/fixtures/m16_temporal_cases.json`, so the browser module and the Python analysis tool cannot
drift apart. Measured on 2026-10-20 against this commit:

| Command | Result |
| --- | --- |
| `python -m pytest tests -q` | **683 passed, 2,275 subtests** |
| `node tools/temporal_check.mjs` | **138 assertion groups, 0 failed** (fixture cases, chart geometry, window slicing, incidents) |
| `node tools/dashboard_check.mjs` | **132 checks, 0 failed** (including the four shipped files' byte ceiling, and the public Data page's information hygiene) |
| `node tools/dashboard_run.mjs --site … --api …` | **80 runtime checks, 0 failed** |
| `node tools/dashboard_scenarios.mjs --site … --api …` | **58 scenario checks, 0 failed** (0/1/2 samples, empty and flat windows, a 40-minute gap, a failing history route, a city switch with a request in flight, no history on other routes) |
| `node tools/m16_browser_check.mjs --site …` | **32 checks, 0 failed** in a real browser, against the frozen local API *and* the deployed site: one history request per city view, none on the other four routes, a re-entered city re-using its loaded series, window switching re-slicing with no second request, the disabled window explaining itself, both charts and summaries agreeing with the change block, in/out-of-window incidents, and no console error, failed request, sideways scroll or clipped text at 390/768/1280 px. Four of the checks are the public Data page's information hygiene: the rendered page (collapsed blocks removed) names no API host, no shared secret and no write route, says in plain words where its data comes from, links nowhere near the API origin, and still lists the three read routes once the technical block is expanded |
| `node tools/layout_scan.mjs` / `node tools/clipping_scan.mjs` | 12 widths (320…1440), **0 contract failures**, locally and against the deployed site |
| `node tools/browser_probe.mjs <deployed>` | everything checked: **0 script errors, 0 failed requests** |
| `python tools/verify_deployed_code.py` | deployed Lambda code **byte-identical** to `src/` |
| `python tools/verify_deployed_page.py` | **all deployed page contracts hold**, including the four public-copy ones: the backend is named in plain words, and the write route, the ingest header and the shared secret are absent from the deployed file |
| `python tools/live_acceptance.py` | **62 checks, 0 failed**; no poll over 1.75 s |
| `python tools/history_audit.py --api … --minutes 1440` | 12/12 cities audited; 0 duplicates, 0 unusable rows, 0 missing readings in 235 rows |
| `python tools/history_analysis.py --api … --minutes 1440` | the per-city table quoted above; **0 rising, 12 steady, 0 falling** |
| live history edge cases | `minutes=1` → 200 with 0 rows; `0`, `-60`, `abc`, `100000` → **400** `invalid_query`; unknown zone → **404** with the known-zone list; no response carried `NaN` or `Infinity` |

Two defects were found by running things rather than reading them, and both are fixed: the chart's band
shading never applied (the stylesheet selected `.chart-fill-band-*` while the markup emitted
`class="chart-fill band-normal"`), and `verify_deployed_page.py` had six needles that had gone stale before
M16 — an old `<canvas>` element and AWS-branded subscription wording that the page had already replaced.
Those needles were repointed at the shipped wording rather than deleted, so each check still asks for the
same thing: a focusable chart, a confirmation that arrives, no marketing, a way out of every email.

**Bytes.** The four shipped files total **406,760 B** against the unchanged 409,600 B ceiling — headroom
**2,840 B**, a net **+3,905 B** over M15.3. The room came out of shipped commentary, least informative
first and in whole blocks only: decoration banners, then section banners, then paragraphs that only
pointed at this README, then the longest remaining narratives. Rules, copy and behaviour were not touched,
`dashboard_check.mjs` still finds the stylesheet section markers it locates sections by, and everything
removed is still in git history and in this README. The first pass at this wrote the files back in text
mode, which silently turned every newline into CRLF and added 9,024 B of terminators to the total — caught
by measuring the files it had just written, and fixed by writing bytes with LF, the ending
`.gitattributes` pins for `web/`.

### Information hygiene on the public Data page

The Data page is a visitor-facing page, so the default view now says what it needs to and no more. This is
a front-end copy change only: no route was renamed, no API Gateway setting was touched, ingestion
authentication was not changed, the key was not rotated, CORS was not altered and the Lambda was not
redeployed.

* **Default view.** "Where the live data comes from" now carries one sentence: *"FloodPulse dashboard data
  is served through its backend API. The dashboard reads current city state, recent historical readings and
  incident information."* The raw `execute-api` hostname, the `POST /readings` route and the shared-secret
  wording are gone from the page. The data-source row in the folded technical block reads "the FloodPulse
  backend API" instead of linking to the host.
* **Technical block.** The folded *Architecture and endpoints (technical)* disclosure — which a reader has
  to open, and which is not page text until they do — still lists the three read routes the dashboard
  actually calls (`GET /zones`, `GET /zones/{zone_id}/history`, `GET /incidents`) and says they are
  read-only and public, so the architecture stays legible without a route dump. The write path stays
  documented where it belongs — in this README (the route table, the accepted/rejected payloads, the
  `x-fp-key` requirement) and in `tools/live_simulator.py`, the ingest client that uses it.
* **The page still needs its origin.** The origin stays where it was always carried as *data*, in the
  `<html data-api="…">` attribute and the policy's `connect-src`; nothing renders it as copy. `app.js` no
  longer writes it into the page at all.
* **What now enforces it.** `dashboard_check.mjs` has a check that strips the folded blocks out of
  `#view-data` and asserts the remaining copy carries none of it (and that no occurrence of the hostname
  survives below `<body>`); `m16_browser_check.mjs` makes the same assertions against the *rendered* page
  in a real browser, on top of the deployed URL; and `verify_deployed_page.py` fetches the deployed file and
  requires the plain-words sentence while forbidding the write route, the ingest header and the shared
  secret. Each of these replaced a check that asserted the opposite, so nothing was quietly deleted:
  `dashboard_run.mjs` now asserts the data-source row is *not* a link, where it used to require one.
* **Unchanged behaviour, as evidence.** `verify_deployed_code.py` reports the deployed Lambda
  byte-identical to `src/`, and `live_acceptance.py` passes 62/0 against the live API afterwards, including
  `POST /readings` still answering 401 without its key. The front-end deploy reported Lambda, API Gateway,
  DynamoDB, IAM, SNS and log groups untouched.
