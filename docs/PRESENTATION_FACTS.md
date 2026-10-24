# FloodPulse presentation facts

Numbers and statements for slides, the viva and the demo, each with the evidence behind it and how firmly
it is established. **Nothing here is an estimate dressed as a measurement.** If a fact is not in this
file, do not claim it.

**Student:** Hatim Maula
**Registration:** 23BCE2238
**Live dashboard:** `http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com`
**Live API:** `https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com`
**Repository:** `https://github.com/h4timfr/floodpulse`
**AWS:** account `725934295966`, region `eu-north-1`, profile `cloud-project`
**Evidence date:** 2026-10-24

Evidence labels used below: **measured** (read from the running system), **tested** (produced by an
automated check), **browser-verified** (a real headless browser drove it), **live-verified** (observed on
the deployed stack), **inferred** (follows from the design; not directly observed), **not claimed** (no
evidence - do not say it).

---

## 1. The three sentences to open with

1. "FloodPulse is a live, serverless system that monitors flood risk for twelve Indian coastal cities on
   real weather data, with a deterministic, explainable risk model."
2. "Rainfall is real; soil saturation is derived; the hydraulic values are simulated - and every number on
   screen says which of the three it is."
3. "It is a risk-indication and monitoring system, not a forecast, and it never claims to be one."

## 2. Numbers to quote

| Fact | Value | Evidence |
|---|---|---|
| Coastal cities monitored | **12** (5 west, 6 east, 1 island) | measured - `GET /zones`, `zone_count: 12` |
| API routes | **5** (`GET /zones`, `GET /zones/{id}/history`, `GET /incidents`, `POST /readings`, `POST /subscriptions`) | measured - API Gateway |
| Lambda | Python **3.13**, **256 MB**, **15 s** timeout, package **106,673 B** | measured - `get-function-configuration` |
| DynamoDB table | `floodpulse`, **1,772 items / 1,492,158 B**, on-demand, **no GSI**, TTL on | measured - `describe-table` |
| Dashboard objects | **27 objects / 965,225 B** in the S3 bucket | measured - `list-objects-v2` |
| Front-end shipped source | **409,501 / 409,600 B** (99 B headroom), zero dependencies, zero CRLF | tested - `build/m16_bytes.py` |
| City photographs | **12** active, **213,462 B** total (`web/` tree: 34 files, 1,021,710 B) | measured |
| Unit tests | **773 tests / 2,333 subtests** | tested - `pytest` |
| Verification harnesses | **11** (static, runtime, scenarios, story, temporal, browser, keyboard, layout, clipping, acceptance, verifiers) | tested |
| Live browser checks in this pass | **32** diagnostics + **8** keyboard + **18** smoke, all passing | browser-verified |
| Live API checks | **62** acceptance + **15** smoke + **80** runtime | live-verified |
| Alert cooldown | **600 s** per city, escalation-only publishing | measured - `config.py`, behaviour |
| Weather refresh rule | at most once per **600 s**; stale after **1,800 s** | measured - `config.py`, `/zones` |
| History | `minutes` 1..1440, ≤ 600 points, 7-day TTL, downsampled for the chart | measured - handler + table |
| Subscriptions | validated, **3 per hour** per address (4th → 429), double opt-in | live-verified - acceptance run |
| Logs | CloudWatch, **7-day** retention, ~**3.8 MB** stored | measured |
| SNS | one topic, escalation-only, **1 confirmed subscription** (never name it) | measured |
| Cost model | pay-per-use only; no always-on resource | inferred - resource list is the evidence |

## 3. Claims ledger

| Claim you may make | Support | Strength |
|---|---|---|
| "The system is live on AWS and serving real weather data." | `/zones` returns `zone_mode: live_weather`, `data_classification: mixed` | live-verified |
| "Every metric is labelled with its source." | labels per record, per metric in the API, printed in the UI | measured |
| "The deployed code is the repository code." | `verify_deployed_code.py`: 15 files byte-identical | tested |
| "The dashboard works in a real browser." | 32 + 8 + 18 checks in headless Edge against the live site | browser-verified |
| "Incidents are threshold crossings, not floods." | incident records store band path and status only | measured |
| "Only a confirmed subscriber receives an alert." | SNS double opt-in; the code only ever publishes | live-verified |
| "The score is deterministic and explainable." | same inputs → same score; per-metric contributions returned | tested |
| "Nothing is extrapolated." | temporal layer describes stored samples only | measured |
| "No secret is committed." | digest scan over 112 tracked files, 16,578 literals: no match | tested |
| "Nothing is always-on, so idle cost is ~zero." | resource inventory contains no server, instance or cluster | inferred |

## 4. What you must not claim

* **Not** a flood forecast, flood probability, or validated hydrological model.
* **Not** calibrated thresholds, and **not** official warning levels.
* **Never** "measured by sensors" - the only real measurement is the provider's rainfall/soil moisture.
* **Never** "e-mail delivered" or "alert sent" - only "published to the topic".
* **Never** a dollar or rupee figure - no billing data were queried.
* **Not** national coverage, and **not** "the twelve most at-risk cities".
* **Not** a state, coast or national risk score - coast groups are navigation.
* **Not** city comparison; it is deliberately unbuilt.
* **Not** an emergency tool: it must not be used for real safety decisions.

## 5. Three pitches

**Thirty seconds.** "FloodPulse monitors flood risk for twelve Indian coastal cities on real weather data.
It runs entirely serverless on AWS - one Python Lambda behind an HTTP API, DynamoDB for state, SNS for
alerts - and serves a dependency-free dashboard that labels every number with whether it is real, derived
or simulated. The risk model is deterministic and explainable, and it is explicitly not a forecast."

**Two minutes.** Add: the twelve cities in three coast groups and why the selection rule is written down;
what is real versus simulated per metric; the band model (0.35 / 0.60 / 0.80 into NORMAL / WATCH /
WARNING / CRITICAL) with the 2-up/3-down hysteresis; incidents with the atomic one-open-per-city guard;
subscriptions with hashed rate limiting and SNS double opt-in; the temporal layer that describes stored
samples, states the true span, discloses gaps and says INSUFFICIENT rather than drawing a line through two
points; and the verification surface - 773 tests, eleven harnesses, byte-identity checks, a 409,600-byte
budget respected to within 99 bytes.

**Five minutes.** Add: the data model (one table, `ZONE#<id>` partitions, `LATEST` / `H#<timestamp>` /
`WEATHER#CACHE` / incident and rate-limit items, no scans); the failure design (stale cache served and
disclosed, one corrupt city degrades alone, retries back off, throttling at 20 req/s with a 40 burst); the
security posture (constant-time key check, scoped IAM with no `DeleteItem`, escaped output, CSP without
inline script) including the honest meta-CSP limitation; the cost model with no number attached; and the
four things deliberately left out - city comparison, ML, time-to-threshold, and scheduled refresh.

## 6. Numbers that make good slide material

* **12** cities, **5** routes, **1** Lambda, **1** table, **0** servers.
* **409,501 / 409,600** bytes of front-end source; ceiling never raised.
* **773** tests and **2,333** subtests; **11** harnesses; **62** live API checks.
* **3** labels - REAL, MODEL_DERIVED, SIMULATED - attached to every metric.
* **2 / 3** - readings needed to escalate / de-escalate.
* **600 s** provider refresh; **1,800 s** before data is called stale; **7 days** of history.
* **0** scans on any read path; **0** dependencies in the front end.
* **99** bytes of headroom under the budget, measured, not assumed.

## 7. Useful live commands for a projector

```powershell
curl.exe -s https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com/zones      # the whole fleet, one call
curl.exe -s "https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com/zones/city-kochi/history?minutes=60"
curl.exe -s -i https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com/incidents
curl.exe -s -o NUL -w "%{http_code}`n" -X POST https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com/readings
#   ^ 401 without the ingestion key: the write path is protected, demonstrated live
python -m pytest -q tests
node tools/dashboard_run.mjs --site http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com `
     --api https://2rg2u8quzd.execute-api.eu-north-1.amazonaws.com
python build/m16_bytes.py
```

## 8. If asked for something you have not measured

Say: "I have not measured that; the tools in `tools/` can, and the closest number I do have is …"
Inventing a figure - even a plausible one - would contradict the project's own rule, which is that a
refusal is better than a guess, and that principle is visible in the product itself.
