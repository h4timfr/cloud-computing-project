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
| Dashboard | S3-hosted static UI polling the live API, incident log panel, provenance chips | ✅ **live and verified** (`web/`) |
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
| `web/bg-storm.webp` | locally generated storm backdrop (13.3 KB) |

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

### Two real bugs this uncovered (found by tracing the leak to its source)

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
# full test suite (509 tests, no AWS calls, no network)
python -m unittest discover -s tests -t .

# the demo story, rehearsed end to end: drives the arc, verifies it, exports what the API served
python tools/storm_demo.py --export build/story
node tools/story_check.mjs --dir build/story

# live weather probe: one real Open-Meteo request for all five cities, read-only
python tools/weather_probe.py

# dashboard static checks: pure helpers, rendering from a real payload, provenance, site safety,
# the asset budget, and the information architecture (five sections, router, phone menu, icons,
# empty states, touch targets, methodology vs config.py): 105 checks per mode
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
# the formula or the model's own output line (58 checks live, 56 for the demo fleet), on top of the
# 108 static checks per mode from dashboard_check.mjs.
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
| M10 | Time-to-threshold, CloudWatch alarm, scheduled refresh (EventBridge) | ⛔ planned |

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
| S3 `floodpulse-dashboard-…` | **5 objects / 234 KB** (index, styles, lib, app, backdrop) | a few cents **per year**, no CloudFront |
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
