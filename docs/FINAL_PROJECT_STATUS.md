# FloodPulse - final project status and handoff

**Date:** 2026-10-24 (M18 closeout)
**Author of the project:** Hatim Maula (23BCE2238)
**State of this document:** the single place to find out what is finished, what is deployed, what is
deliberately absent, what is known to be limited, and how to take the project over.

---

## 1. Short version

FloodPulse is **complete, deployed and verified** against its own scope: twelve Indian coastal cities
monitored on real weather data, a deterministic explainable risk model, stored history with a
retrospective temporal layer, incidents with an atomic one-open-per-city guard, escalation-only SNS
alerting with public double opt-in subscriptions, and a dependency-free dashboard served from S3 that
labels the source of every number.

It is **not** finished as a flood-forecasting system, and it never claimed to be: no probability, no
forecast, no calibration, no physical sensors. Those are recorded below as limitations and open items, not
as loose ends.

**Nothing was pushed by the automation in this pass.** The repository is clean and committed locally; the
owner pushes.

---

## 2. What is deployed (measured, read-only, 2026-10-24)

| Resource | Key facts |
|---|---|
| AWS identity | account `725934295966`, region `eu-north-1`, profile `cloud-project`, user `cloud-project-dev` |
| Lambda `floodpulse-api` | python3.13, 256 MB, 15 s timeout, x86_64, Active, handler `floodpulse.handler.handler`, package 106,673 B, last modified 2026-10-22T07:11:39Z (now: M18 touched no deployed code) |
| HTTP API `2rg2u8quzd` | 5 routes, all `AuthType NONE` at the gateway (authorisation lives in the handler), `$default` stage auto-deploy, throttling 20 req/s with a 40 burst, detailed metrics off, **access logging off** |
| DynamoDB `floodpulse` | `PAY_PER_REQUEST`, keys `pk`/`sk`, **no GSI**, TTL enabled on `ttl`, **1,772 items / 1,492,158 B** |
| S3 `floodpulse-dashboard-725934295966` | static website hosting, index and error document `index.html`, **27 objects / 965,225 B** |
| SNS `floodpulse-alerts` | 1 topic, **1 confirmed subscription** (addresses are never printed or recorded anywhere in this repository) |
| CloudWatch `/aws/lambda/floodpulse-api` | 7-day retention, 4,013,428 B stored |
| IAM `floodpulse-lambda-role` | `AWSLambdaBasicExecutionRole` + inline `FloodPulseDynamoDBAccess` (`GetItem`, `PutItem`, `UpdateItem`, `Query` - **no `DeleteItem`**) + inline `FloodPulseSnsPublish` (`sns:Publish`, `sns:Subscribe`) |
| Absent by design | no EC2, RDS, ECS/EKS, OpenSearch, CloudFront, VPC, NAT gateway, EventBridge rule, alarm, or provisioned capacity |

Front-end shipped source: `index.html` 37,425 + `styles.css` 105,828 + `lib.js` 160,518 + `app.js`
105,730 = **409,501 B** against the unchanged **409,600 B** ceiling (99 B headroom, zero CRLF).
Images: 23 `.webp` files in `web/`, of which the 12 active city photographs total 213,462 B; the whole
`web/` tree is 34 files / 1,021,710 B.

---

## 3. Deliverables in the repository

### Code
* `src/floodpulse/` - 15 modules: `handler.py` (routing, read model, live pipeline), `risk.py`,
  `temporal.py`, `validation.py`, `storage.py`, `dynamodb_store.py`, `incidents.py`, `notify.py`,
  `subscriptions.py`, `auth.py`, `weather.py`, `live_weather.py`, `simulator.py`, `config.py`.
* `web/` - the four shipped files plus the image set.
* `infra/deploy.ps1` - idempotent deployment (create only what is missing, never delete, IAM read-only,
  `-DryRun`, `-SkipDashboard`, `-DashboardOnly`).

### Verification (11 harnesses, all runnable by a reviewer)
| Tool | What it answers |
|---|---|
| `python -m pytest -q tests` | 773 tests / 2,333 subtests, no AWS, no network |
| `tools/dashboard_check.mjs` | 138 static checks (144 with live payloads) |
| `tools/dashboard_run.mjs` | 80 runtime checks against a site (local or deployed) |
| `tools/dashboard_scenarios.mjs` | 66 failure and edge scenarios through the real poller |
| `tools/story_check.mjs` | 29 checks on the offline storm arc |
| `tools/temporal_check.mjs` | 141 assertion groups on the temporal maths |
| `tools/m16_browser_check.mjs` | 32 checks in a real headless browser against the live site |
| `tools/keyboard_check.mjs` | 8 keyboard and accessibility-structure checks |
| `tools/layout_scan.mjs`, `tools/clipping_scan.mjs` | geometry contracts at four widths |
| `tools/live_acceptance.py` | 62 checks against the deployed API |
| `tools/verify_deployed_code.py`, `tools/verify_deployed_page.py` | deployed artefacts vs repository |

### Documentation
| Document | Purpose |
|---|---|
| `README.md` | current state at the top, then the full build history (M1-M17) with each pass's measurements |
| `docs/ARCHITECTURE.md` | diagram, deployed inventory, data model, security, failure behaviour, cost model |
| `docs/FINAL_TECHNICAL_SUMMARY.md` | the system in 3-5 pages for a reader with no context |
| `docs/PRESENTATION_FACTS.md` | every number to quote, with its evidence and its strength |
| `docs/VIVA_CHEATSHEET.md` | 33 likely questions with 2-5 sentence answers |
| `docs/FINAL_PROJECT_STATUS.md` | this file: status, limitations, open items, handoff |
| `DEMO_RUNBOOK.md` | the 7-10 minute presentation, 17 steps, plus an offline path and fallbacks |

---

## 4. Verification summary for this pass

| Gate | Result | Type |
|---|---|---|
| `python -m pytest -q tests` | 773 passed / 2,333 subtests | tested |
| `python -m unittest discover -s tests -t .` | 773 tests, OK | tested |
| `tools/dashboard_check.mjs` | 138 checks, 0 failed (144 with live payloads) | tested |
| `tools/dashboard_run.mjs --site … --api …` | 80 checks, 0 failed, against the live site | live-verified |
| `tools/dashboard_scenarios.mjs` | 66 checks, 0 failed | tested |
| `tools/story_check.mjs` | 29 checks, 0 failed | tested |
| `tools/temporal_check.mjs` | 141 assertion groups, 0 failed | tested |
| `tools/m16_browser_check.mjs --site … --api …` | 32 checks, 0 failed | browser-verified |
| `tools/keyboard_check.mjs <site>` | 8 checks, 0 failed (2 observations recorded) | browser-verified |
| `tools/layout_scan.mjs <site>` | 4 routes × 4 widths, 0 contract failures | browser-verified |
| `tools/clipping_scan.mjs <site>` | 72 measured overflows, 0 hidden by an ancestor | browser-verified |
| live smoke (this pass) | 18 checks: 12 cities, 12 photos decoded, map, city page, alerts, Data, About, no console error | browser-verified |
| `tools/live_acceptance.py` | 62 checks, 0 failed, against the deployed API | live-verified |
| API smoke (this pass) | 15 checks including refusals, CORS and gzip | live-verified |
| `tools/verify_deployed_code.py` | 15 files byte-identical to `src/` | tested |
| `tools/verify_deployed_page.py` | every deployed page contract holds | tested |
| `tools/deploy_logic_check.ps1` | all deployment assertions passed | tested |
| `build/m16_bytes.py` | 409,501 / 409,600 B, no CRLF | tested |
| secret scan (this pass) | 112 tracked files, 16,578 literals, no key material | tested |
| repository hygiene | no `build/`, log, profile, zip or credential file tracked | measured |

Two tool defects were found and fixed while producing these numbers, both in the harnesses rather than
the product: `tools/dashboard_scenarios.mjs` assumed a history document with more than one sample, and
`tools/m16_browser_check.mjs` matched the page's "usable historical sample**s**" wording only in the
plural. Both now accept every honest state the product can display, and the live checks pass on the real
— currently thin — history.

---

## 5. Known limitations (canonical list)

1. **Selected fleet of twelve coastal cities** - documented selection reasons, no claim of being the most
   at-risk twelve, no national coverage.
2. **No state, coast or national risk figure** - coast groups are navigation only.
3. **Uncalibrated heuristic thresholds** - a risk indicator, never a probability, never official.
4. **Simulated hydraulic values** - drain level, water level, flow rate are generated from real rainfall.
5. **External provider dependency** - an outage degrades to the last good cache, disclosed on screen.
6. **No provider observation time** - fetch time and age are shown instead of an observation claim.
7. **History can be thin** - sampling is tied to weather refreshes (≤ 1 per 600 s) and history expires
   after 7 days, so a demo opened cold may honestly report a single usable sample.
8. **Retrospective analysis only** - no forecast, no time-to-threshold, no extrapolation.
9. **Incidents record threshold crossings**, not observed floods.
10. **Meta-element CSP** - `frame-ancestors` cannot be enforced from an S3 website endpoint; no
    clickjacking protection is claimed.
11. **No API Gateway access logging, no detailed metrics, no alarms, no tracing** - deliberately not
    added in this pass; the operational trail is the Lambda's own CloudWatch logs.
12. **Read paths are unauthenticated** - public data by design; ingestion is key-gated with a shared
    secret rather than per-client credentials.
13. **City photograph licensing** - the twelve images are used under the terms recorded in the README's
    M13 section; replace any image whose provenance is unresolved before external reuse.
14. **Not an emergency tool** - it must never inform real evacuation or safety decisions.

## 6. Open items (small, and each with its fix)

| Item | Fix |
|---|---|
| History thins out between demos | add an EventBridge scheduled refresh (a new resource; needs owner approval) |
| No page when ingestion stops | add CloudWatch alarms with an SNS action target |
| No per-route gateway metrics | enable detailed API metrics on the existing stage |
| No access log for requests that never reach the Lambda | enable API Gateway access logging (a log group + stage setting) |
| Clickjacking unprotected | CloudFront distribution with a response-header policy carrying a real CSP |
| Three browser observations unresolved from M17: the city-card layout box measurement, the accessibility tree exposing only on-screen map markers, and Tab not reaching the incident table within 60 stops | re-run `tools/keyboard_check.mjs` with the map inside the viewport and a longer tab budget; both are harness conditions, not product defects observed so far |
| Front-end source is 99 bytes under its ceiling | any further front-end change must make space, not raise the ceiling |

## 7. Handoff: how to do the common jobs

**Run everything locally, no AWS.**
```powershell
python -m pytest -q tests
node tools/dashboard_check.mjs
node tools/dashboard_scenarios.mjs
node tools/temporal_check.mjs
node tools/dashboard_run.mjs --payload build/zones-live-weather.json --history build/history-live-weather.json `
     --incidents build/incidents-live-weather.json
```

**Check the deployed system.**
```powershell
python tools/verify_deployed_code.py
python tools/verify_deployed_page.py
python tools/live_acceptance.py
node tools/dashboard_run.mjs --site <site> --api <api>
```

**Browser checks** (the tools never launch a browser):
```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless=new --disable-gpu `
  --remote-debugging-port=9222 --user-data-dir=build\edge-verify-m18 about:blank
node tools/m16_browser_check.mjs --site <site> --api <api>
node tools/keyboard_check.mjs <site>
```

**Deploy a backend change.** `infra/deploy.ps1` (add `-DryRun` first). It creates only what is missing,
never deletes, and re-verifies the ingestion key fingerprint around the write.

**Add or rename a city.** Edit `LIVE_ZONE_DEFINITIONS` in `src/floodpulse/config.py` (id, name, state,
coast group, coordinates, selection reason), add its photograph under `web/` plus an entry in the
`CITY_IMAGES` map in `web/lib.js`, then raise the several checks that assert the fleet count (dashboard
checks, live acceptance, layout expectations) - the failing checks are the correct checklist. Re-deploy
the Lambda and the dashboard objects.

**Change a threshold or a weight.** `config.py` only; the public Data page is generated from the same
constants, so the documentation cannot drift. Re-run `python -m pytest -q tests` and
`node tools/dashboard_check.mjs`.

**Rotate the ingestion key.** `python tools/init_ingest_key.py` writes the gitignored key file; update the
Lambda environment through the owner (the deploy script verifies the fingerprint, it does not create
secrets).

**Front-end change.** Keep the four files under 409,600 B total and LF-only; run
`python build/m16_bytes.py`, `node tools/dashboard_check.mjs`, `node tools/dashboard_scenarios.mjs`, then
the layout and clipping scans, then redeploy with `infra/deploy.ps1 -DashboardOnly`.

### Traps worth knowing
* The shipped files must stay **LF**; `.gitattributes` pins `web/*.html|css|js`, and the byte gate counts
  bytes exactly.
* One failed browser page can wedge a harness: close it and open `about:blank` rather than re-running
  blind.
* `Select-Object -First N` on a native command corrupts `$LASTEXITCODE` in this shell; check output, not
  merely the exit code.
* Local fixtures under `build/` are rewritten by `tools/live_acceptance.py`, so scenario harnesses are
  data-shape sensitive by design: pin the shape you need in the fixture instead of relaxing an assertion.
* Never commit `build/` (gitignored scratch), and never print `.floodpulse-key`, subscriber addresses, or
  account credentials.

## 8. Claims discipline

Everything asserted in the README, in `docs/` and in this file is labelled by how it was established:
**measured** (read from running resources), **tested** (produced by an automated check),
**browser-verified** (a real browser drove it), **live-verified** (observed on the deployed stack), or
**inferred** (follows from the design). No dollar figure is claimed anywhere, because no billing data were
queried. Where something could not be established, this document says so instead of filling the gap - which
is the same rule the product itself follows when a chart has too few samples to describe.

## 9. Post-handoff: the first three things to do

1. **Calibrate.** Collect observed flooding or waterlogging reports for the twelve cities and use them to
   fit and validate the band edges. This is the single biggest improvement to the project's usefulness.
2. **Decouple sampling from traffic.** An EventBridge scheduled refresh every 10 minutes would make
   history continuous and remove the thin-history case entirely.
3. **Put a real edge in front of the dashboard.** CloudFront with a response-header policy would provide
   the CSP header the meta element cannot, plus TLS and caching.
