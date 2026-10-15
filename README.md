# FloodPulse

Live AWS-based flash-flood early-warning and risk-prediction system (cloud computing project).

**Current milestone: M3 — flood incidents and SNS alerts (implemented, tested locally, deploy held).**
M2 remains live and verified: a simulated reading → API Gateway HTTP API → one Lambda → DynamoDB →
read back through the API → **an S3-hosted dashboard that polls the live API every 5 seconds**, which
never invents risk values. M3 adds automatic incident records (`GET /incidents`) and escalation-only
SNS alerts; its AWS deploy is waiting on one IAM grant described in the M3 section below.

**Dashboard: <http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com>**
**API: <https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com>**

All sensor data produced by this repository is **SIMULATED**. Risk scores come from
`heuristic-v1`, a deterministic and explainable **heuristic** — it is *not* a validated
hydrological model, *not* a statistical forecast and *not* a machine-learning prediction.
Every stored record and every API response carries `simulated: true` and
`data_classification: "simulated"`.

---

## Architecture status

| Layer | Component | Status |
|---|---|---|
| Simulator | Deterministic phase-driven sensor simulator | ✅ implemented (`src/floodpulse/simulator.py`) |
| Ingest | Single Lambda handler, `POST /readings`, shared-secret protected | ✅ **live** on API Gateway |
| Validation | Strict payload validation, server-side epoch/TTL | ✅ implemented |
| Risk engine | Deterministic `heuristic-v1` + explainability + hysteresis | ✅ implemented |
| Storage | In-memory store + DynamoDB store (same interface) | ✅ **live** against table `floodpulse` |
| API | `GET /zones`, `GET /zones/{zone_id}/history`, `GET /incidents` (public reads) | ✅ **live** in the same handler |
| AWS deployment | DynamoDB / Lambda / HTTP API / S3 site / SNS topic | ⏳ M3 code and deploy script ready; **AWS deploy paused** pending one IAM grant (see M3 section) |
| Dashboard | S3-hosted static UI polling the live API, incident log panel | ⏳ M3 panel written and checked locally; **not yet uploaded** |
| Incidents | Automatic incident records + lifecycle (open/escalate/close) | ✅ implemented and unit-tested; not yet deployed |
| Alerts | One SNS topic, escalation-only publishing with a per-zone cooldown | ✅ implemented and unit-tested; publishing needs `sns:Publish` on the role (denied so far) |
| Time-to-threshold | Estimate time to a threshold | ⛔ **deferred until after M3** |
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
| Lambda function | `floodpulse-api` | python3.13, 256 MB, 10 s, handler `floodpulse.handler.lambda_handler`, env `FLOODPULSE_STORE=dynamodb`, `FLOODPULSE_TABLE=floodpulse`, `FLOODPULSE_INGEST_KEY` (set, never displayed) |
| Lambda execution role | `floodpulse-lambda-role` | trust `lambda.amazonaws.com`; `AWSLambdaBasicExecutionRole`; inline `FloodPulseDynamoDBAccess` = `dynamodb:GetItem/PutItem/UpdateItem/Query` on `table/floodpulse` only. **Created by the project owner**; the deploy script never creates or edits IAM |
| HTTP API | `floodpulse-api` (`2rg2u8quzd`) | `$default` stage, auto-deploy, integration `tcdtda1` (AWS_PROXY, payload format 2.0) |
| Routes | | `POST /readings` (requires `x-fp-key`), `GET /zones`, `GET /zones/{zone_id}/history` |
| CORS | | origins **restricted** to the dashboard website origin + `http://localhost:8000`; methods `GET,POST,OPTIONS`; headers `content-type,x-fp-key`; max age 300 |
| Invoke permission | `floodpulse-apigateway-invoke` | `apigateway.amazonaws.com`, scoped to `arn:aws:execute-api:eu-north-1:725934295966:2rg2u8quzd/*/*` |
| CloudWatch Logs | `/aws/lambda/floodpulse-api` | retention **7 days** |
| S3 dashboard bucket | `floodpulse-dashboard-725934295966` | static website hosting (`index.html`), public read of objects only (no list, no write), ~72 KB of assets |

Live endpoint: `https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com`

Item model (one table, no GSI, no scan — `LATEST` per zone plus append-only history):

| `sk` | purpose | TTL |
|---|---|---|
| `LATEST` | newest reading + risk for a zone, overwritten each time | none |
| `H#<iso timestamp>` | history point; duplicate timestamps get `#2`, `#3`, … via a conditional write | 7 days (`ttl`) |

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

## M3 incidents and SNS alerts (implemented and tested locally; AWS deploy paused)

M3 adds real flood-incident tracking and one SNS alert topic, without changing what M1/M2 already do.

### Incident rules (as implemented)

| Rule | Behaviour |
|---|---|
| Open | A zone opens **exactly one** incident when its **hysteresis-filtered** class reaches WARNING or CRITICAL (so `ESCALATION_STREAK` still governs it) |
| No duplicates | Only one OPEN incident per zone; `create_incident()` uses a conditional write (`attribute_not_exists(pk)`), so two concurrent requests cannot both open one. A zone that reaches WARNING again while already open just updates the existing record |
| Escalate | WARNING → CRITICAL on the **same** incident: `severity` is raised, `severity_changes` gets an entry, `alert_count` grows. Severity is never downgraded inside an incident |
| Stay open | WATCH (or any non-NORMAL class) keeps the incident OPEN — that is still an elevated state |
| Close | Only once the zone is back to NORMAL per the existing `DEESCALATION_STREAK`, then `closed_at`, `duration_seconds` and `close_reason` are set |
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

### Status of this milestone (honest)

| Item | State |
|---|---|
| Incident engine, storage, API route, notifier, dashboard panel | ✅ implemented, covered by unit tests, verified locally |
| Local test suite | ✅ **250 tests** pass (`python -m unittest discover -s tests -t .`) |
| Dashboard harness | ✅ **33 checks** pass (`node tools/dashboard_check.mjs --payload build/zones-live.json`) |
| SNS topic `floodpulse-alerts` | ✅ created in `eu-north-1`, `DisplayName = FloodPulse Alerts` |
| `sns:Publish` for the Lambda role | ⛔ **blocked**: `iam:PutRolePolicy` is denied for the `cloud-project` user, so the operator must attach `FloodPulseSnsPublish` (documented below) |
| Lambda code, `GET /incidents` route, dashboard upload | ⏳ **not deployed**: the deploy is deliberately held until that policy exists, so the milestone is verified in one pass instead of shipping unverifiable alerting |
| Email/SMS subscription | ⏳ **pending**: the topic has 0 subscriptions, so publishing (once allowed) proves the publish path only |

Operator step required before the deploy (one command, granted on this topic only):

```powershell
# the policy document is written by infra/deploy.ps1 (and is already at build/iam-sns-publish.json):
#   { "Action": "sns:Publish", "Resource": "arn:aws:sns:eu-north-1:725934295966:floodpulse-alerts" }
aws iam put-role-policy --role-name floodpulse-lambda-role --policy-name FloodPulseSnsPublish `
  --policy-document file://build/iam-sns-publish.json --profile cloud-project --region eu-north-1
```

Then the whole milestone is deployed and verified with one idempotent command:

```powershell
powershell -ExecutionPolicy Bypass -File infra\deploy.ps1
# creates/uses the SNS topic, adds FLOODPULSE_SNS_TOPIC_ARN + FLOODPULSE_ALERT_COOLDOWN_SECONDS to the
# gitignored env file (values never printed), adds the GET /incidents route, uploads the dashboard
```

### M3 limitations (known, deliberate)

* Alerting is publish-proof only until somebody subscribes an email address to the topic.
* No retry queue: a publish that fails is logged on the incident and retried at the *next* escalation,
  not immediately.
* Incidents are per zone; there is no cross-zone "event" that groups simultaneous incidents.
* No incident acknowledgement, assignment or operator notes (read-only log by design).

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

## Repository layout

```
AGENTS.md                     project rules for agents
README.md                     this file
src/floodpulse/
  config.py                   3 fixed zones, thresholds, weights, bands, hysteresis, limits
  risk.py                     deterministic heuristic-v1 scorer + explanation + hysteresis
  validation.py               strict payload validation (server-derived ts_epoch/ttl)
  auth.py                     x-fp-key ingestion guard (fail closed in cloud mode)
  storage.py                  DynamoDB-shaped item builders + in-memory LocalStore
  dynamodb_store.py           DynamoDbStore: same interface, boto3 imported lazily
  handler.py                  the single Lambda handler: dispatch() + lambda_handler()
  simulator.py                deterministic phase-driven simulator
web/
  index.html                  dashboard layout + permanent simulated-data banner
  styles.css                  storm command-centre theme, glass cards, band colours
  lib.js                      pure render/format helpers (browser + node)
  app.js                      5 s polling controller, patching, sparkline, history chart
  bg-storm.webp               locally generated backdrop (13.3 KB)
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
  support.py                  shared test helpers (recording notifier, scenario driver)
tools/
  local_demo.py               CLI: prints a risk timeline, no AWS calls
  live_simulator.py           drives the deployed API with simulated readings
  init_ingest_key.py          creates the local secret + Lambda env file (never prints it)
  dashboard_check.mjs         static checks on the dashboard sources (node, no browser)
  make_background.py          generates web/bg-storm.webp + WCAG contrast report
infra/deploy.ps1              idempotent deployment (table + Lambda + HTTP API + S3 dashboard)
```

## Running it locally

Requires Python 3.11+ (tested on 3.13.7). **No dependencies, no Docker, no AWS credentials.**

```powershell
# full test suite (250 tests, no AWS calls)
python -m unittest discover -s tests -t .

# dashboard checks: pure helpers, rendering from a real payload, incidents, site safety (node)
node tools/dashboard_check.mjs
node tools/dashboard_check.mjs --payload build/zones-live.json --history build/history-live.json
node tools/dashboard_check.mjs --payload build/zones-live.json --incidents build/incidents-live.json

# local demo: risk timeline for all three zones
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
| M3 | Flood incidents + SNS alerts + incident log in the dashboard | ✅ implemented and tested locally; ⏳ AWS deploy held pending one IAM grant (see M3 section) |
| M4 | Historical chart polish, session trends, zone drill-down | ⛔ |
| M5 | Time-to-threshold, CloudWatch alarm | ⛔ |
| M6 | Demo hardening, deterministic run-book, scheduled simulator | ⛔ |

## AWS state (verified)

* profile `cloud-project`, region `eu-north-1`, identity `arn:aws:iam::725934295966:user/cloud-project-dev`
* created by this project, and nothing else: DynamoDB table `floodpulse`, Lambda `floodpulse-api`
  (+ its invoke permission), HTTP API `floodpulse-api` (`2rg2u8quzd`), log group
  `/aws/lambda/floodpulse-api`, S3 bucket `floodpulse-dashboard-725934295966` (M2), SNS topic
  `floodpulse-alerts` (M3, `DisplayName = FloodPulse Alerts`, **0 subscriptions**)
* the execution role `floodpulse-lambda-role` was created by the project owner, not by this repository;
  adding the M3 `sns:Publish` policy is an operator action because `iam:PutRolePolicy` is denied here
  (exact error recorded in the M3 section)
* no EC2, RDS, CloudFront, VPC, NAT gateway, ECS/EKS or OpenSearch resource exists for this project
* `cloudformation:*` is **denied** for this profile (verified earlier: `ListStacks`, `DescribeStacks`,
  `ValidateTemplate`) → SAM/CDK/`cfn deploy` are unusable; deployment is per-service AWS CLI calls
* the unrelated role `Video2Product-EC2-SSM-Role` exists in the account and was not touched

## Cost posture

Live now: DynamoDB on-demand + Lambda (free tier) + HTTP API ($1 per million requests) + 7-day log
retention + one S3 bucket holding ~85 KB (a few cents at most per month) + one SNS topic with no
subscriptions (idle topics cost nothing). No NAT gateway, CloudFront, IoT Core, EC2, RDS or
OpenSearch. Estimated cost while the stack sits idle: effectively $0 (all on-demand/free-tier, log
retention bounded at 7 days); a demo day of thousands of API requests, tens of thousands of DynamoDB
writes and a handful of SNS publishes stays far below $0.05.

## Engineering rules

See `AGENTS.md`. In short: work only in this repository, use profile `cloud-project` in
`eu-north-1`, never commit credentials, never fabricate results, prefer the simplest
complete implementation, and do not provision AWS infrastructure without approval.
