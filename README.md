# FloodPulse

Live AWS-based flash-flood early-warning and risk-prediction system (cloud computing project).

**Current milestone: M0 — local foundation only. Nothing is deployed to AWS yet.**

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
| Ingest | Single Lambda handler, `POST /readings` | ✅ implemented (runs locally, **not deployed**) |
| Validation | Strict payload validation, server-side epoch/TTL | ✅ implemented |
| Risk engine | Deterministic `heuristic-v1` + explainability + hysteresis | ✅ implemented |
| Storage | In-memory store + DynamoDB-shaped item builders | ✅ local only (no boto3, no table) |
| API | `GET /zones`, `GET /zones/{zone_id}/history` | ✅ implemented in the same handler |
| AWS deployment | DynamoDB / Lambda / HTTP API / S3 | ⛔ **not created** (M1) |
| Dashboard | Web UI | ⛔ not built (M2, after live API) |
| Time-to-threshold | Estimate time to a threshold | ⛔ **deferred until after the live dashboard** |
| Alerts | SNS email/SMS notifications | ⛔ **deferred until after the live dashboard** |
| Incidents | Automatic incident records | ⛔ deferred (M4) |
| ML | Any learned model | ⛔ out of scope until the deterministic system works end to end |

Locked MVP decisions reflected in the code: **one** Lambda handling all routes,
**one** Lambda execution role, **no** aggregate snapshot item (a `LATEST` record per zone
plus timestamped history records), client payload carries `timestamp` only, `sensor_node_id`
(not `device_id`).

## Repository layout

```
AGENTS.md                     project rules for agents
README.md                     this file
src/floodpulse/
  config.py                   3 fixed zones, thresholds, weights, bands, hysteresis, limits
  risk.py                     deterministic heuristic-v1 scorer + explanation + hysteresis
  validation.py               strict payload validation (server-derived ts_epoch/ttl)
  storage.py                  DynamoDB-shaped item builders + in-memory LocalStore
  handler.py                  the single Lambda handler: dispatch() + lambda_handler()
  simulator.py                deterministic phase-driven simulator
tests/
  test_risk.py                scoring, bands, contributions, hysteresis (fixed vectors)
  test_validation.py          accepted/rejected payload matrix
  test_storage.py             item shapes + store behaviour
  test_handler.py             routes, error codes, API Gateway v1/v2 adapter
  test_simulator.py           determinism, ranges, phase/clock behaviour
  test_end_to_end.py          simulator -> validation -> risk -> store -> API, in-process
tools/local_demo.py           CLI: prints a risk timeline, no AWS calls
```

## Running it locally

Requires Python 3.11+ (tested on 3.13.7). **No dependencies, no Docker, no AWS credentials.**

```powershell
# full test suite (131 tests)
python -m unittest discover -s tests -t . -v

# local demo: risk timeline for all three zones
python tools/local_demo.py --steps 40

# full scenario arc (calm -> flash flood -> recession) and final GET /zones payload
python tools/local_demo.py --steps 100 --json
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

Deliberately **not** implemented at M0: time-to-threshold (no extrapolation is faked),
SNS alerts, incidents, dashboard.

## Simulator

Four phases over 100 steps (5 s apart): `calm(20) → storm_ramp(15) → flash_flood(20) → recession(45)`.
Seeded (default `20260214`) and keyed by zone, so a given seed/zone pair reproduces the
identical sequence — same input, same output, every time. The default start epoch is a fixed
constant so tests never depend on the wall clock.

## Milestones

| # | Slice | Status |
|---|---|---|
| M0 | Local foundation: simulator, validation, risk engine, store, handler, tests | ✅ done |
| M1 | AWS: DynamoDB table, deploy the single Lambda with one execution role, HTTP API routes, DynamoDB-backed store | ⛔ next |
| M2 | Live dashboard (S3 static site polling `GET /zones`) — first live demo | ⛔ |
| M3 | Multi-zone tuning, ranking polish, historical chart | ⛔ |
| M4 | Incidents + SNS alerts (after the dashboard works) | ⛔ |
| M5 | Time-to-threshold, log retention, CloudWatch alarm | ⛔ |
| M6 | Demo hardening, deterministic run-book | ⛔ |

## AWS readiness notes (verified, read-only)

* profile `cloud-project`, region `eu-north-1`, identity `arn:aws:iam::725934295966:user/cloud-project-dev`
* account currently has **0** buckets, DynamoDB tables, Lambdas, SNS topics and HTTP APIs
* `cloudformation:*` is **denied** for this profile (verified: `ListStacks`, `DescribeStacks`,
  `ValidateTemplate`) → SAM/CDK/`cfn deploy` are unusable; M1 must deploy with per-service
  AWS CLI calls
* no Lambda-capable IAM role exists yet; `iam:CreateRole`/`PassRole` are unverified and are
  the first thing M1 must prove
* **no AWS resource has been created by this repository**

## Cost posture

Planned M1+ services stay within DynamoDB on-demand + Lambda + HTTP API + S3 + SNS +
CloudWatch free/low tiers (well under $0.10 for a demo day). No NAT gateway, CloudFront,
IoT Core, EC2, RDS or OpenSearch.

## Engineering rules

See `AGENTS.md`. In short: work only in this repository, use profile `cloud-project` in
`eu-north-1`, never commit credentials, never fabricate results, prefer the simplest
complete implementation, and do not provision AWS infrastructure without approval.
