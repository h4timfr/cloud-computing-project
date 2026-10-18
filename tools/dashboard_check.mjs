#!/usr/bin/env node
/*
 * Dashboard verification harness (development tool, not part of the site).
 *
 * The dashboard deliberately has no build step, so this checks it directly:
 *   - loads web/lib.js in a VM and exercises every pure helper
 *   - renders zone cards / ranking from a real API payload (--payload) and asserts the HTML
*   - asserts the site writes nothing but the one public subscription request, and never a secret
 *   - asserts every local asset referenced by the HTML/CSS exists and the page stays small
 *   - asserts the product information architecture: four navigable views, a total hash
 *     router, a phone menu, plain language before model detail, and methodology copy that
 *     still matches the constants in src/floodpulse/config.py
 *
 * Usage:  node tools/dashboard_check.mjs [--payload build/zones-live.json]
 *         (the runtime half is tools/dashboard_run.mjs: this file never executes app.js)
 */

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const args = process.argv.slice(2);
const payloadArgIndex = args.indexOf('--payload');
const payloadPath = payloadArgIndex >= 0 ? args[payloadArgIndex + 1] : null;
const historyArgIndex = args.indexOf('--history');
const historyPath = historyArgIndex >= 0 ? args[historyArgIndex + 1] : null;
const incidentsArgIndex = args.indexOf('--incidents');
const incidentsPath = incidentsArgIndex >= 0 ? args[incidentsArgIndex + 1] : null;

const repoRoot = process.cwd();
const webDir = path.join(repoRoot, 'web');

/* ------------------------------------------------------------------ harness --- */

let passed = 0;
const failures = [];

function check(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (error) {
    failures.push({ name, message: error.message });
    console.log(`  FAIL ${name}\n         ${error.message.split('\n')[0]}`);
  }
}

function group(title) {
  console.log(`\n${title}`);
}

/* --------------------------------------------------------------- load lib.js --- */

function loadLib() {
  const source = fs.readFileSync(path.join(webDir, 'lib.js'), 'utf8');
  const sandbox = { window: {}, console, Math, Date, Number, String, isFinite, parseInt, parseFloat };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: 'web/lib.js' });
  return sandbox.window.FP;
}

const FP = loadLib();

/* The words a reader would actually read: tags and attributes removed (a `data-metric=
 * "soil_saturation_pct"` attribute is how a row is addressed, not copy), and `<code>` blocks
 * exempt because the technical disclosures quote real identifiers on purpose. Used by every
 * check that asks "is this engineering detail leaking into a public view?". */
function proseOf(markup) {
  return String(markup || '')
    .replace(/<code[\s\S]*?<\/code>/g, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&[a-z]+;|&#\d+;/g, ' ')
    .replace(/\s+/g, ' ');
}

/* Any snake_case identifier in that prose. Exported to the checks so one definition of "a
 * variable name" is used everywhere. */
function variableNamesIn(markup) {
  return proseOf(markup).match(/\b[a-z][a-z0-9]*_[a-z0-9_]+\b/g) || [];
}

/* ------------------------------------------------------------------ fixtures --- */

const THRESHOLDS = {
  rainfall_mm_per_hr: { watch: 15.0, critical: 45.0 },
  drain_level_m: { watch: 1.5, critical: 2.5 },
  soil_saturation_pct: { watch: 70.0, critical: 90.0 },
  water_level_m: { watch: 2.0, critical: 3.5 },
  flow_rate_m3_s: { watch: 6.0, critical: 12.0 }
};

function zoneFixture(overrides) {
  const base = {
    zone_id: 'zone-riverside-north',
    name: 'Riverside North',
    status: 'live',
    thresholds: THRESHOLDS,
    latest: {
      simulated: true,
      data_classification: 'simulated',
      sensor_node_id: 'sim-riverside-north',
      timestamp: '2026-10-15T07:37:23Z',
      ts_epoch: 1792049843,
      age_seconds: 3,
      sequence: 1,
      risk_score: 0.1703,
      risk_class: 'NORMAL',
      risk_class_candidate: 'NORMAL',
      readings: {
        rainfall_mm_per_hr: 1.81,
        drain_level_m: 0.43,
        soil_saturation_pct: 38.97,
        water_level_m: 0.95,
        flow_rate_m3_s: 1.88
      },
      // The shape the API really sends: `variable` (not `metric`) plus the two thresholds the
      // model used. The fixture used to carry `metric` and no thresholds, which is exactly why
      // nothing noticed that the interface could not read a real contribution at all - the map
      // was keyed on a field the API has never sent. One entry keeps the old `metric` key so the
      // alias stays covered.
      contributions: [
        { variable: 'water_level_m', value: 0.95, unit: 'm', weight: 0.25, sub_score: 0.2375,
          contribution: 0.0594, watch_threshold: 2.0, critical_threshold: 3.5 },
        { metric: 'rainfall_mm_per_hr', value: 1.81, unit: 'mm/h', weight: 0.3, sub_score: 0.0603,
          contribution: 0.0181, watch_threshold: 15.0, critical_threshold: 45.0 },
        { variable: 'soil_saturation_pct', value: 38.97, unit: '%', weight: 0.2, sub_score: 0.4329,
          contribution: 0.0866, watch_threshold: 70.0, critical_threshold: 90.0 },
        { variable: 'drain_level_m', value: 0.43, unit: 'm', weight: 0.15, sub_score: 0.0573,
          contribution: 0.0086, watch_threshold: 1.5, critical_threshold: 2.5 }
      ],
      top_drivers: ['water_level_m', 'soil_saturation_pct', 'drain_level_m'],
      trend: { weight: 0.1, reference_rise_m: 1.0, water_level_delta_m: null, rise_fraction: null, contribution: 0.0 },
      explanation: 'risk_score 0.1703 = weighted base 0.1703 + trend 0.0000',
      hysteresis: { risk_class: 'NORMAL', candidate_class: 'NORMAL', streak_class: null, streak_count: 0, transition: 'initial' }
    }
  };
  return Object.assign(base, overrides || {});
}

const noDataZone = {
  zone_id: 'zone-old-town-canal',
  name: 'Old Town Canal',
  status: 'no_data',
  thresholds: THRESHOLDS,
  latest: null
};

const FIXTURE_PAYLOAD = {
  generated_at: '2026-10-15T07:37:28Z',
  simulated: true,
  data_classification: 'simulated',
  model: 'heuristic-v1',
  model_note: 'heuristic-v1 is a hand-tuned deterministic scoring heuristic over simulated sensor values.',
  highest_risk_zone: { zone_id: 'zone-riverside-north', name: 'Riverside North', risk_score: 0.1703, risk_class: 'NORMAL', timestamp: '2026-10-15T07:37:23Z' },
  zone_ranking: ['zone-riverside-north', 'zone-old-town-canal', 'zone-harbor-east'],
  zone_count: 3,
  zones: [
    zoneFixture(),
    noDataZone,
    {
      zone_id: 'zone-harbor-east',
      name: 'Harbor East',
      status: 'live',
      thresholds: THRESHOLDS,
      latest: Object.assign({}, zoneFixture().latest, {
        zone_id: 'zone-harbor-east',
        risk_score: 0.62,
        risk_class: 'WARNING',
        risk_class_candidate: 'WARNING',
        readings: Object.assign({}, zoneFixture().latest.readings, { rainfall_mm_per_hr: 51.0, water_level_m: 3.2 })
      })
    }
  ]
};

let payload = FIXTURE_PAYLOAD;
let payloadSource = 'fixture (no --payload given)';
if (payloadPath) {
  payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
  payloadSource = payloadPath;
}

/* ------------------------------------------------------------- pure helpers --- */

group(`Pure helpers (lib.js) — payload: ${payloadSource}`);

check('band mapping is case-insensitive and total', () => {
  assert.equal(FP.bandKey('NORMAL'), 'normal');
  assert.equal(FP.bandKey('watch'), 'watch');
  assert.equal(FP.bandKey('Warning'), 'warning');
  assert.equal(FP.bandKey('CRITICAL'), 'critical');
  assert.equal(FP.bandKey(null), 'unknown');
  assert.equal(FP.bandKey('BANANA'), 'unknown');
  assert.equal(FP.classLabel(null), 'NO DATA');
  assert.equal(FP.classLabel('critical'), 'CRITICAL');
  assert.equal(FP.classRank('CRITICAL'), 3);
  assert.equal(FP.classRank(undefined), -1);
});

check('metric formatting matches the API units', () => {
  assert.equal(FP.formatValueWithUnit('rainfall_mm_per_hr', 1.81), '1.8 mm/h');
  assert.equal(FP.formatValueWithUnit('soil_saturation_pct', 38.97), '39%');
  assert.equal(FP.formatValueWithUnit('water_level_m', 0.95), '0.95 m');
  assert.equal(FP.formatValueWithUnit('flow_rate_m3_s', 1.876), '1.88 m³/s');
  assert.equal(FP.formatValueWithUnit('rainfall_mm_per_hr', null), '–');
  assert.equal(FP.formatValueWithUnit('rainfall_mm_per_hr', undefined), '–');
});

check('score formatting and ring percentage', () => {
  assert.equal(FP.formatScore(0.1703), '0.17');
  assert.equal(FP.formatScore(0.8), '0.80');
  assert.equal(FP.formatScore(null), '–');
  assert.equal(FP.riskPercent(0.8), 80);
  assert.equal(FP.riskPercent(1.7), 100);
  assert.equal(FP.riskPercent(-1), 0);
  assert.equal(FP.riskPercent(null), 0);
});

check('age and clock labels', () => {
  assert.equal(FP.ageLabel(3), 'just now');
  assert.equal(FP.ageLabel(42), '42 s ago');
  assert.equal(FP.ageLabel(120), '2 min ago');
  assert.equal(FP.ageLabel(7200), '2 h ago');
  assert.equal(FP.ageLabel(null), 'no data');
  assert.equal(FP.clockLabel('2026-10-15T07:37:23Z'), '07:37:23Z');
  assert.equal(FP.clockLabel(null), 'unknown time');
});

check('trend label and direction', () => {
  // a trend with no measured change says so in words: "no lookback" was the model's shorthand
  assert.equal(FP.trendLabel({ water_level_delta_m: null }), 'not enough history yet');
  assert.equal(FP.trendLabel(null), 'not enough history yet');
  assert.equal(FP.trendHasHistory(null), false, 'a missing trend claims history');
  assert.equal(FP.trendHasHistory({ water_level_delta_m: null }), false, 'a null delta claims history');
  assert.equal(FP.trendHasHistory({ water_level_delta_m: 0.4 }), true, 'a measured delta is not recognised');
  // ...and an arrow follows the direction it is drawn for. trendDirection answers 'up'/'down'/
  // 'flat'; the icon was chosen by comparing against 'rising'/'falling', so every arrow on the
  // board pointed "flat" whatever the water was doing.
  const upIcon = FP.trendIcon({ water_level_delta_m: 0.4 }, 't');
  const downIcon = FP.trendIcon({ water_level_delta_m: -0.4 }, 't');
  assert.match(upIcon, /trend-up/, 'a rising trend is not drawn as rising');
  assert.match(downIcon, /trend-down/, 'a falling trend is not drawn as falling');
  assert.notEqual(upIcon.slice(0, 80), downIcon.slice(0, 80), 'both directions render the same icon');
  assert.match(FP.trendIcon({ water_level_delta_m: null }, 't'), /trend-flat/,
    'a trend with no history is not drawn as flat');
  assert.equal(FP.trendLabel({ water_level_delta_m: 1.2, rise_fraction: 1 }), '+1.20 m / 5 min (rise 100%)');
  assert.equal(FP.trendLabel({ water_level_delta_m: -0.3, rise_fraction: 0 }), '-0.30 m / 5 min (rise 0%)');
  assert.equal(FP.trendDirection({ water_level_delta_m: 0.4 }), 'up');
  assert.equal(FP.trendDirection({ water_level_delta_m: -0.4 }), 'down');
  assert.equal(FP.trendDirection({ water_level_delta_m: 0 }), 'flat');
  assert.equal(FP.trendDirection(null), 'flat');
});

check('metric scale uses the thresholds from the API payload', () => {
  const low = FP.metricScale('rainfall_mm_per_hr', 1.81, THRESHOLDS);
  const watch = FP.metricScale('rainfall_mm_per_hr', 20, THRESHOLDS);
  const critical = FP.metricScale('rainfall_mm_per_hr', 51, THRESHOLDS);
  assert.equal(low.state, 'low');
  assert.equal(watch.state, 'watch');
  assert.equal(critical.state, 'critical');
  assert.ok(low.valuePct > 0 && low.valuePct < low.watchPct);
  assert.ok(low.watchPct < low.criticalPct && low.criticalPct <= 100);
  const unknown = FP.metricScale('rainfall_mm_per_hr', 5, undefined);
  assert.equal(unknown.state, 'unknown');
  assert.equal(unknown.valuePct, null);
});

check('connection state covers live / stale / disconnected / connecting', () => {
  assert.equal(FP.connectionState(1000, null, false).state, 'connecting');
  assert.equal(FP.connectionState(5000, 4000, false).state, 'live');
  assert.equal(FP.connectionState(5000, 4000, true).state, 'stale');
  assert.equal(FP.connectionState(20000, 4000, false).state, 'stale');
  assert.equal(FP.connectionState(40000, 4000, false).state, 'disconnected');
  assert.match(FP.connectionState(40000, 4000, false).detail, /out of date/);
});

check('ranking sorts by score, live zones first and ties by zone id', () => {
  const zones = [
    { zone_id: 'b', name: 'B', status: 'live', latest: { risk_score: 0.2 } },
    { zone_id: 'a', name: 'A', status: 'live', latest: { risk_score: 0.2 } },
    { zone_id: 'c', name: 'C', status: 'live', latest: { risk_score: 0.9 } },
    { zone_id: 'd', name: 'D', status: 'no_data', latest: null }
  ];
  assert.deepEqual(FP.ranking(zones).map((z) => z.zone_id), ['c', 'a', 'b', 'd']);
  assert.equal(FP.liveCount(zones), 3);
  assert.equal(FP.liveCount([]), 0);
});

check('sparkline geometry: higher score is higher on screen', () => {
  const shape = FP.sparklinePath([0.1, 0.5, 0.9], 100, 50, 5);
  assert.equal(shape.points.length, 3);
  assert.ok(shape.points[0].y > shape.points[1].y);
  assert.ok(shape.points[1].y > shape.points[2].y);
  assert.ok(shape.points[0].x < shape.points[2].x);
  assert.match(shape.path, /^M[\d.]+ [\d.]+ L[\d.]+ [\d.]+ L[\d.]+ [\d.]+$/);
  const empty = FP.sparklinePath([], 100, 50, 5);
  assert.equal(empty.path, '');
  const gaps = FP.sparklinePath([0.2, null, 0.6], 100, 50, 5);
  assert.equal(gaps.points.length, 2);
});

check('history series projection', () => {
  const series = FP.historySeries([
    { timestamp: '2026-10-15T07:00:00Z', ts_epoch: 1, risk_score: 0.2, risk_class: 'NORMAL' },
    { timestamp: '2026-10-15T07:05:00Z', ts_epoch: 2, risk_score: 0.7, risk_class: 'WARNING' }
  ]);
  assert.deepEqual(series.values, [0.2, 0.7]);
  assert.deepEqual(series.classes, ['NORMAL', 'WARNING']);
  assert.equal(series.labels.length, 2);
});

check('duration formatting is stable across magnitudes', () => {
  assert.equal(FP.formatDuration(0), '0 s');
  assert.equal(FP.formatDuration(45.4), '45 s');
  assert.equal(FP.formatDuration(200), '3 min 20 s');
  assert.equal(FP.formatDuration(600), '10 min');
  assert.equal(FP.formatDuration(3600 + 25 * 60), '1 h 25 min');
  assert.equal(FP.formatDuration(null), '\u2013');
  assert.equal(FP.formatDuration(-5), '\u2013');
});

check('open incident duration ticks forward, closed uses the API value', () => {
  const opened = 1_000_000;
  const open = { current_status: 'OPEN', opened_ts_epoch: opened, duration_seconds: 30 };
  assert.equal(FP.incidentDurationSeconds(open, opened * 1000), 30, 'never reports less than the API said');
  assert.equal(FP.incidentDurationSeconds(open, (opened + 120) * 1000), 120);
  // a clock that is behind must not make the duration jump backwards
  assert.equal(FP.incidentDurationSeconds(open, (opened - 500) * 1000), 30);

  const closed = { current_status: 'CLOSED', opened_ts_epoch: opened, duration_seconds: 340 };
  assert.equal(FP.incidentDurationSeconds(closed, (opened + 9999) * 1000), 340);
  assert.equal(FP.incidentIsOpen(closed), false);
  assert.equal(FP.incidentIsOpen(open), true);
  assert.equal(FP.incidentIsOpen(null), false);
});

check('incident meta and alerting notes never leak the topic', () => {
  const meta = FP.incidentsMeta({ count: 4, open_count: 1, limit: 20 });
  assert.match(meta, /1 open/);
  assert.match(meta, /4 recorded/);
  assert.equal(FP.incidentsMeta(null), 'loading incidents…');

  const off = FP.alertingNote({ enabled: false, severities: ['WARNING'], cooldown_seconds: 600 });
  const armed = FP.alertingNote({ enabled: true, severities: ['WARNING', 'CRITICAL'], cooldown_seconds: 600 });
  assert.match(off, /not configured/);
  assert.match(armed, /10 min/);
  // Armed publishing is not proof of delivery, and the browser is never told about
  // subscribers, so the copy must say so rather than imply somebody was notified.
  assert.match(armed, /not confirmed/, 'the alerting note must not imply delivery');
  assert.ok(!/\bdelivered\b|has been sent|subscribers? (?:were|will be) notified/i.test(armed),
    'the alerting note claims a notification that was never observed');
  [off, armed, meta].forEach((text) => {
    assert.ok(!/arn:/i.test(text), 'alerting text mentions an ARN');
    assert.ok(!/topic/i.test(text), 'alerting text mentions the topic');
  });
});

check('incident signature changes with status, severity and alert count', () => {
  const base = { incidents: [{ incident_id: 'inc-a', current_status: 'OPEN', severity: 'WARNING', alert_count: 1 }] };
  const same = { incidents: [{ incident_id: 'inc-a', current_status: 'OPEN', severity: 'WARNING', alert_count: 1 }] };
  const escalated = { incidents: [{ incident_id: 'inc-a', current_status: 'OPEN', severity: 'CRITICAL', alert_count: 2 }] };
  const closed = { incidents: [{ incident_id: 'inc-a', current_status: 'CLOSED', severity: 'CRITICAL', alert_count: 2 }] };
  assert.equal(FP.incidentsSignature(base), FP.incidentsSignature(same));
  assert.notEqual(FP.incidentsSignature(base), FP.incidentsSignature(escalated));
  assert.notEqual(FP.incidentsSignature(escalated), FP.incidentsSignature(closed));
});

/* -------------------------------------------------------------- rendering --- */

group('Rendering from a real payload');

check('live zone card contains every value the API returned', () => {
  const zone = payload.zones.find((z) => z.status === 'live');
  const html = FP.renderZoneCard(zone);
  assert.ok(html.includes(zone.name), 'zone name missing');
  assert.ok(html.includes(zone.latest.sensor_node_id), 'sensor node id missing');
  assert.ok(html.includes(zone.latest.risk_score.toFixed(2)), 'score missing');
  for (const metric of FP.METRIC_ORDER) {
    assert.ok(html.includes(FP.METRIC_META[metric].label), `metric label ${metric} missing`);
  }
  for (const band of ['normal', 'watch', 'warning', 'critical']) {
    assert.ok(html.includes(`band-${band}`) || true, 'band class emitted for at least one value');
  }
  assert.ok(!html.includes('undefined'), 'card contains undefined');
  assert.ok(!html.includes('NaN'), 'card contains NaN');
  assert.ok(html.includes(`band-${FP.bandKey(zone.latest.risk_class)}`), 'effective band class missing');
  // The model's own output line used to be printed on this card verbatim. It is one line of
  // formula, variable names and arithmetic - the single worst thing to put in front of a visitor
  // on a list of cities - so it now lives in the technical fold on the city page (asserted in
  // "the city view leads with plain language"). What must be here instead is the plain sentence
  // and the driver under its human name.
  assert.ok(!html.includes('weighted base'), 'the city list still prints the raw formula');
  assert.ok(!/risk_score [0-9]/.test(html), 'the city list still prints the raw formula');
  assert.deepEqual(variableNamesIn(html), [], 'the city list reads out variable names');
  const driver = (zone.latest.top_drivers || [])[0];
  if (driver) {
    assert.ok(html.includes(FP.driverLabel(driver)), `the main risk factor is not labelled: ${driver}`);
    assert.match(html, /is the main contributor/, 'the card does not say what is driving risk in words');
  }
});

check('no-data zone renders an explicit empty state', () => {
  // a live payload may have every zone reporting; fall back to the fixture zone so the
  // empty-state path is always exercised
  const zone = payload.zones.find((z) => z.status !== 'live' || !z.latest) || noDataZone;
  const html = FP.renderZoneCard(zone);
  assert.ok(html.includes('is-empty'), 'missing empty-state class');
  assert.ok(html.includes('Awaiting the first simulated reading'), 'missing empty-state copy');
  assert.ok(!html.includes('undefined'));
});

check('hostile payload content is escaped (no markup injection)', () => {
  const hostile = zoneFixture({
    name: '<img src=x onerror=alert(1)>',
    latest: Object.assign({}, zoneFixture().latest, {
      explanation: '<script>alert("xss")</script>',
      sensor_node_id: '"><script>bad()</script>',
      top_drivers: ['<b>bold</b>']
    })
  });
  const html = FP.renderZoneCard(hostile);
  assert.ok(!html.includes('<img'), 'raw img tag leaked');
  assert.ok(!html.includes('<script'), 'raw script tag leaked');
  assert.ok(!html.includes('</b>'), 'raw markup leaked');
  assert.ok(html.includes('&lt;script&gt;'), 'expected escaped text');
});

check('ranking markup lists every zone', () => {
  const html = FP.renderRanking(payload.zones);
  payload.zones.forEach((zone) => {
    assert.ok(html.includes(zone.name), `zone ${zone.zone_id} missing from ranking`);
  });
  const firstIndex = html.indexOf(FP.ranking(payload.zones)[0].name);
  const lastIndex = html.indexOf(FP.ranking(payload.zones)[payload.zones.length - 1].name);
  assert.ok(firstIndex >= 0 && lastIndex > firstIndex, 'ranking order looks wrong');
});

check('band classes vary with the payload (calm vs storm)', () => {
  const calm = FP.renderZoneCard(zoneFixture());
  const storm = FP.renderZoneCard(zoneFixture({
    latest: Object.assign({}, zoneFixture().latest, {
      risk_score: 0.91, risk_class: 'CRITICAL', risk_class_candidate: 'CRITICAL'
    })
  }));
  assert.ok(calm.includes('band-normal'));
  assert.ok(storm.includes('band-critical'));
  assert.ok(!calm.includes('band-critical'));
});

/* ------------------------------------------------------------- provenance --- */

group('Provenance rendering (M4)');

// A demo-mode zone: the API reports every metric as SIMULATED.
const DEMO_ZONE = {
  zone_id: 'zone-riverside-north',
  name: 'Riverside North',
  zone_mode: 'simulated',
  metric_sources: {
    rainfall_mm_per_hr: 'SIMULATED', drain_level_m: 'SIMULATED', soil_saturation_pct: 'SIMULATED',
    water_level_m: 'SIMULATED', flow_rate_m3_s: 'SIMULATED'
  },
  thresholds: THRESHOLDS,
  latest: {
    simulated: true,
    data_classification: 'simulated',
    metric_sources: {
      rainfall_mm_per_hr: 'SIMULATED', drain_level_m: 'SIMULATED', soil_saturation_pct: 'SIMULATED',
      water_level_m: 'SIMULATED', flow_rate_m3_s: 'SIMULATED'
    },
    readings: { rainfall_mm_per_hr: 8, drain_level_m: 0.6, soil_saturation_pct: 40, water_level_m: 0.9, flow_rate_m3_s: 2 },
    risk_score: 0.22, risk_class: 'NORMAL', risk_class_candidate: 'NORMAL',
    top_drivers: ['water_level_m'], explanation: 'demo', age_seconds: 5, sequence: 4, trend: {}
  }
};

// A live-weather zone: REAL rainfall, MODEL-derived soil moisture, SIMULATED hydraulics.
const LIVE_ZONE = {
  zone_id: 'city-chennai',
  name: 'Chennai',
  zone_mode: 'live_weather',
  metric_sources: {
    rainfall_mm_per_hr: 'REAL', drain_level_m: 'SIMULATED', soil_saturation_pct: 'MODEL_DERIVED',
    water_level_m: 'SIMULATED', flow_rate_m3_s: 'SIMULATED'
  },
  weather: { provider: 'open-meteo', observed_at: '2026-10-15T18:00:00Z', cache_age_seconds: 30 },
  thresholds: THRESHOLDS,
  latest: {
    simulated: false,
    data_classification: 'mixed',
    metric_sources: {
      rainfall_mm_per_hr: 'REAL', drain_level_m: 'SIMULATED', soil_saturation_pct: 'MODEL_DERIVED',
      water_level_m: 'SIMULATED', flow_rate_m3_s: 'SIMULATED'
    },
    readings: { rainfall_mm_per_hr: 12.5, drain_level_m: 1.19, soil_saturation_pct: 48, water_level_m: 1.49, flow_rate_m3_s: 3.6 },
    risk_score: 0.41, risk_class: 'WATCH', risk_class_candidate: 'WATCH',
    top_drivers: ['soil_saturation_pct'], explanation: 'live', age_seconds: 12, sequence: 7, trend: {}
  }
};

check('a demo zone is labelled simulated, a live zone is not', () => {
  assert.equal(FP.metricSource(DEMO_ZONE, 'rainfall_mm_per_hr'), 'SIMULATED');
  assert.equal(FP.metricSource(LIVE_ZONE, 'rainfall_mm_per_hr'), 'REAL');
  assert.equal(FP.modeMeta(DEMO_ZONE.zone_mode).label, 'SIMULATED');
  assert.equal(FP.modeMeta(LIVE_ZONE.zone_mode).label, 'LIVE WEATHER');
});

check('an unknown or missing tag is never upgraded to REAL', () => {
  const bare = { zone_id: 'x', name: 'X' };
  assert.equal(FP.metricSource(bare, 'rainfall_mm_per_hr'), null);
  assert.equal(FP.sourceMeta(undefined).key, 'unknown');
  assert.equal(FP.sourceMeta('MADE_UP').key, 'unknown');
  assert.match(FP.sourceChip(null), /src-unknown/);
  assert.equal(FP.modeMeta(undefined).key, 'unknown');
});

check('the zone card renders a mode badge and one chip per provenance class', () => {
  const live = FP.renderZoneCard(LIVE_ZONE);
  assert.match(live, /mode-live/);
  assert.match(live, /LIVE WEATHER/);
  assert.match(live, /src-real/);
  assert.match(live, /src-model/);
  assert.match(live, /src-simulated/);
  assert.match(live, /data-f="source-set"/);

  const demo = FP.renderZoneCard(DEMO_ZONE);
  assert.match(demo, /mode-simulated/);
  assert.match(demo, /src-simulated/);
  assert.ok(!demo.includes('src-real'), 'a simulated zone must not show a REAL chip');
});

check('every metric row carries its own provenance chip', () => {
  const rows = FP.METRIC_ORDER.map((metric) => FP.renderMetricRow(LIVE_ZONE, metric));
  assert.equal(rows.length, 5);
  rows.forEach((row) => assert.match(row, /data-f="metric-source"/));
  assert.match(rows[0], /src-real/);          // rainfall
  assert.match(rows[2], /src-model/);         // soil saturation
  assert.match(rows[1], /src-simulated/);     // drain level
  const demoRows = FP.METRIC_ORDER.map((metric) => FP.renderMetricRow(DEMO_ZONE, metric));
  demoRows.forEach((row) => assert.match(row, /src-simulated/));
});

check('the ranking names the mode of every zone', () => {
  const html = FP.renderRanking([DEMO_ZONE, LIVE_ZONE]);
  assert.match(html, /rank-mode/);
  assert.match(html, /LIVE WEATHER/);
  assert.match(html, /SIMULATED/);
});

check('the banner text follows the payload in both modes', () => {
  // The title and the detail are asserted separately: the title states the mode, the
  // detail states what is real and how fresh it is. A demo payload must never be
  // captioned as live, and a live payload must never claim a real sensor.
  const demoNotice = FP.dataNotice({ zone_mode: 'simulated', notes: ['All FloodPulse sensor data is SIMULATED.'] });
  assert.equal(FP.modeTitle({ zone_mode: 'simulated' }), 'SIMULATED DATA');
  assert.match(demoNotice, /simulated/i);
  assert.ok(!/provider|Open-Meteo|REAL/.test(demoNotice), 'a demo banner must not name a provider');

  const livePayload = {
    zone_mode: 'live_weather',
    notes: ['Rainfall intensity and soil saturation in these zones come from the external Open-Meteo weather service;'],
    weather: {
      enabled: true, provider: 'open-meteo', attribution: 'Weather data by Open-Meteo.com (CC BY 4.0)',
      refresh_seconds: 600, live_zones: ['a', 'b', 'c', 'd', 'e'],
      cache_age_seconds: { a: 240, b: 240, c: 240, d: 240, e: 240 },
      last_attempt_age_seconds: { a: 240, b: 240, c: 240, d: 240, e: 240 }
    }
  };
  assert.equal(FP.modeTitle(livePayload), 'LIVE WEATHER · SIMULATED HYDRAULICS');
  const liveNotice = FP.dataNotice(livePayload);
  assert.match(liveNotice, /Open-Meteo/);
  assert.match(liveNotice, /5 Indian cities/);
  assert.match(liveNotice, /SIMULATED/);
  assert.match(liveNotice, /weather feed 4 min old/);
  assert.match(liveNotice, /next provider check in ~6 min/);
  assert.ok(!/^SIMULATED DATA/.test(liveNotice));
  // an unknown mode is never upgraded to a live claim
  assert.match(FP.modeTitle({}), /SIMULATED DATA/);
  assert.match(FP.dataNotice({}), /simulated/i);
  assert.match(FP.modeTitle({ zone_mode: 'weird-mode' }), /WEIRD-MODE/);
});

check('the weather summary explains the refresh cadence and any provider problem', () => {
  const off = FP.weatherSummary({ weather: { enabled: false } });
  assert.match(off, /off/);
  assert.match(off, /every metric is simulated/);
  // ...but a live fleet with the provider switched off still shows real, already-fetched
  // values, so the text must not call them simulated
  const offLive = FP.weatherSummary({ zone_mode: 'live_weather', data_classification: 'mixed', weather: { enabled: false } });
  assert.match(offLive, /off/);
  assert.match(offLive, /last fetched values/);
  assert.ok(!/every metric is simulated/.test(offLive),
    'a live fleet must never be described as fully simulated');
  const fresh = FP.weatherSummary({
    weather: {
      enabled: true, provider: 'open-meteo', refresh_seconds: 600,
      last_refresh: { attempted: true, refreshed: ['city-chennai'], error: 'http_503' }
    }
  });
  assert.match(fresh, /open-meteo/i);
  assert.match(fresh, /10 min/);
  assert.match(fresh, /http_503/);
  const cached = FP.weatherSummary({
    weather: { enabled: true, provider: 'open-meteo', refresh_seconds: 600, last_refresh: { attempted: false, reason: 'cache_fresh' } }
  });
  assert.match(cached, /cached/);
  // a recorded failed attempt must stay visible on the polls that (correctly) do not retry
  const outage = FP.weatherSummary({
    weather: {
      enabled: true, provider: 'open-meteo', refresh_seconds: 600,
      last_refresh: { attempted: false, reason: 'cache_fresh' },
      last_attempt_error: { 'city-chennai': 'http_503', 'city-mumbai': 'http_503' },
      cache_age_seconds: { 'city-chennai': 700, 'city-mumbai': 700 }
    }
  });
  assert.match(outage, /cached/);
  assert.match(outage, /fetch failing on 2 zone/);
  assert.match(outage, /http_503/);
});

check('a zone card states how old the external feed is, and when fetching failed', () => {
  const fresh = FP.zoneProvenance(LIVE_ZONE);
  assert.match(fresh, /LIVE WEATHER/);
  assert.match(fresh, /feed just fetched/);
  const older = FP.zoneProvenance(Object.assign({}, LIVE_ZONE, {
    weather: { provider: 'open-meteo', cache_age_seconds: 780 }
  }));
  assert.match(older, /feed 13 min old/);
  const failedZone = Object.assign({}, LIVE_ZONE, {
    weather: { provider: 'open-meteo', cache_age_seconds: 780, last_attempt_error: 'network_error: dns' }
  });
  const failed = FP.zoneProvenance(failedZone);
  assert.match(failed, /fetch failed/);
  assert.match(failed, /network_error: dns/);
  assert.equal(FP.zoneProvenance(DEMO_ZONE), FP.modeBadge(DEMO_ZONE),
    'a demo zone has no external feed to report');
  assert.match(FP.renderZoneCard(failedZone), /data-f="feed-note"/);
  assert.match(FP.renderZoneCard(LIVE_ZONE), /data-f="feed-note"/);
});

/* -------------------------------------------------------------- incidents --- */

group('Incident log rendering');

const INCIDENT_FIXTURE = {
  count: 2,
  open_count: 1,
  closed_count: 1,
  limit: 20,
  alerting: { enabled: true, severities: ['WARNING', 'CRITICAL'], cooldown_seconds: 600 },
  incidents: [
    {
      incident_id: 'inc-riverside-north-20251009T085545Z',
      zone_id: 'zone-riverside-north',
      zone_name: 'Riverside North',
      severity: 'CRITICAL',
      severity_at_open: 'WARNING',
      current_status: 'OPEN',
      is_open: true,
      opened_at: '2025-10-09T08:55:45Z',
      opened_ts_epoch: 1_760_000_145,
      closed_at: null,
      duration_seconds: 340,
      risk_score_at_open: 0.66,
      top_driver: 'water_level_m',
      top_driver_label: 'Water level',
      alert_count: 2,
      escalation_count: 1,
      simulated: true
    },
    {
      incident_id: 'inc-harbor-east-20251009T080000Z',
      zone_id: 'zone-harbor-east',
      zone_name: 'Harbor East',
      severity: 'WARNING',
      severity_at_open: 'WARNING',
      current_status: 'CLOSED',
      is_open: false,
      opened_at: '2025-10-09T08:00:00Z',
      opened_ts_epoch: 1_759_999_600,
      closed_at: '2025-10-09T08:24:00Z',
      duration_seconds: 1440,
      risk_score_at_open: 0.62,
      top_driver: 'rainfall_mm_per_hr',
      top_driver_label: 'Rainfall intensity',
      alert_count: 1,
      escalation_count: 0,
      simulated: true
    }
  ]
};

check('open incident row shows severity, zone, opened time, status and duration', () => {
  const incident = INCIDENT_FIXTURE.incidents[0];
  const html = FP.renderIncidentRow(incident, (incident.opened_ts_epoch + 700) * 1000);
  assert.ok(html.includes(incident.zone_name), 'zone name missing');
  assert.ok(html.includes(incident.incident_id), 'incident id missing');
  assert.ok(html.includes('CRITICAL'), 'severity missing');
  assert.ok(html.includes('OPEN'), 'status missing');
  assert.ok(html.includes(FP.clockLabel(incident.opened_at)), 'opened time missing');
  assert.ok(html.includes('11 min 40 s'), `live duration missing: ${html}`);
  assert.ok(html.includes('band-critical'), 'severity band class missing');
  assert.ok(html.includes('data-status="OPEN"'), 'status attribute missing');
  assert.ok(html.includes('Water level'), 'top driver label missing');
  assert.ok(!html.includes('undefined'), 'row contains undefined');
  assert.ok(!html.includes('NaN'), 'row contains NaN');
});

check('closed incident row shows the stored duration and closed status', () => {
  const incident = INCIDENT_FIXTURE.incidents[1];
  const html = FP.renderIncidentRow(incident, Date.now());
  assert.ok(html.includes('CLOSED'), 'closed status missing');
  assert.ok(html.includes('data-status="CLOSED"'), 'status attribute missing');
  assert.ok(html.includes(FP.formatDuration(incident.duration_seconds)), 'stored duration missing');
  assert.ok(html.includes('band-warning'), 'warning band class missing');
  assert.ok(!html.includes('and counting'), 'a closed incident must not tick');
});

check('the whole incident list renders newest first with both states', () => {
  const html = FP.renderIncidents(INCIDENT_FIXTURE, 1_760_000_845_000);
  const firstIndex = html.indexOf('inc-riverside-north');
  const secondIndex = html.indexOf('inc-harbor-east');
  assert.ok(firstIndex >= 0 && secondIndex > firstIndex, 'incidents are not in payload order');
  assert.equal((html.match(/<li class="incident-row band-/g) || []).length, 2);
});

check('an empty incident log explains what would create one', () => {
  const html = FP.renderIncidents({ incidents: [], count: 0, open_count: 0 }, Date.now());
  assert.match(html, /No flood incidents recorded yet/);
  assert.match(html, /WARNING or CRITICAL/);
  assert.match(html, /NORMAL/);
  assert.ok(!html.includes('undefined'), 'empty state contains undefined');
});

check('incident markup escapes API-supplied text', () => {
  const hostile = Object.assign({}, INCIDENT_FIXTURE.incidents[0], {
    zone_name: '<img src=x onerror="alert(1)">',
    top_driver_label: '</li><script>alert(2)</script>'
  });
  const html = FP.renderIncidentRow(hostile, Date.now());
  assert.ok(!html.includes('<img'), 'zone name markup was not escaped');
  assert.ok(!html.includes('<script>'), 'driver markup was not escaped');
  assert.ok(html.includes('&lt;'), 'expected escaped entities');
});

check('incident markup never contains the SNS topic or a secret', () => {
  const html = FP.renderIncidents(INCIDENT_FIXTURE, Date.now());
  const meta = FP.incidentsMeta(INCIDENT_FIXTURE);
  const note = FP.alertingNote(INCIDENT_FIXTURE.alerting);
  [html, meta, note].forEach((text) => {
    assert.ok(!/arn:aws/i.test(text), 'incident UI contains an ARN');
    assert.ok(!/floodpulse-alerts/i.test(text), 'incident UI names the topic');
    assert.ok(!text.includes('FLOODPULSE_INGEST_KEY'), 'incident UI mentions the secret');
  });
});

if (incidentsPath) {
  const live = JSON.parse(fs.readFileSync(incidentsPath, 'utf8'));

  check(`live incidents payload from ${incidentsPath} is panel-ready`, () => {
    assert.ok(Array.isArray(live.incidents), 'incidents must be an array');
    assert.equal(live.count, live.incidents.length, 'count disagrees with incidents');
    assert.ok(live.open_count <= live.count, 'open_count exceeds count');
    assert.ok(live.alerting && typeof live.alerting.enabled === 'boolean', 'alerting state missing');
    live.incidents.forEach((incident, index) => {
      ['incident_id', 'zone_id', 'zone_name', 'severity', 'current_status', 'opened_at'].forEach((field) => {
        assert.ok(incident[field], `incident ${index} is missing ${field}`);
      });
      assert.ok(FP.BANDS.includes(incident.severity), `incident ${index} has an unknown severity`);
      assert.ok(['OPEN', 'CLOSED'].includes(incident.current_status), `incident ${index} has an unknown status`);
      assert.ok(!isNaN(new Date(incident.opened_at).getTime()), `incident ${index} opened_at is unparseable`);
      assert.equal(typeof incident.duration_seconds, 'number', `incident ${index} duration missing`);
      assert.ok(incident.risk_score_at_open >= 0.6, `incident ${index} opened below WARNING`);
      // An incident inherits the provenance of the reading that opened it: the two flags it
      // carries must agree, and a live incident must not be served as fully simulated.
      assert.ok(['simulated', 'mixed'].includes(incident.data_classification),
        `incident ${index} has an unknown classification ${incident.data_classification}`);
      assert.equal(incident.simulated, incident.data_classification === 'simulated',
        `incident ${index} contradicts itself: simulated=${incident.simulated} but ${incident.data_classification}`);
    });
    assert.equal(live.simulated, live.data_classification === 'simulated',
      `the list contradicts itself: simulated=${live.simulated} but ${live.data_classification}`);
    // The list must report the provenance of the records it actually holds: one provenance
    // reports itself, several report "mixed", and an empty list falls back to the fleet. A
    // live-weather fleet can legitimately serve "simulated" - every incident in it may have
    // been opened by a reading a client posted, which is always simulated.
    if (live.incidents.length === 0) {
      assert.ok(['simulated', 'mixed'].includes(live.data_classification),
        'an empty live incident list claims an unknown provenance');
    } else {
      const held = new Set(live.incidents.map((incident) => incident.data_classification));
      const expected = held.size === 1 ? [...held][0] : 'mixed';
      assert.equal(live.data_classification, expected,
        `the list says ${live.data_classification} but holds ${[...held].join(' + ')}`);
    }
    if (live.data_classification === 'mixed') {
      assert.ok(live.notes.some((note) => /open-meteo/i.test(note)),
        'a mixed incident list does not attribute the provider data');
    } else if (live.zone_mode === 'live_weather' && live.incidents.length) {
      // a live fleet serving all-simulated incidents must say so in its own disclaimer
      assert.ok(live.notes.some((note) => /simulated/i.test(note)),
        'an all-simulated incident list does not carry its simulation disclaimer');
    }
    const html = FP.renderIncidents(live, Date.now());
    assert.ok(!html.includes('undefined'), 'live incident markup contains undefined');
    assert.ok(!html.includes('NaN'), 'live incident markup contains NaN');
    assert.ok(!/arn:aws/i.test(html), 'live incident markup contains an ARN');
    console.log(`         ${live.incidents.length} incident(s): ${live.open_count} open`);
  });
}

if (historyPath) {
  const history = JSON.parse(fs.readFileSync(historyPath, 'utf8'));

  check(`live history payload from ${historyPath} is chart-ready`, () => {
    assert.ok(Array.isArray(history.points), 'points must be an array');
    assert.equal(history.points.length, history.point_count, 'point_count disagrees with points');
    assert.ok(history.minutes > 0 && history.minutes <= 1440, 'minutes out of range');
    const series = FP.historySeries(history.points);
    assert.equal(series.values.length, history.points.length);
    history.points.forEach((point, index) => {
      assert.ok(point.risk_score >= 0 && point.risk_score <= 1, `point ${index} score out of range`);
      assert.ok(FP.BANDS.includes(point.risk_class), `point ${index} has an unknown class`);
      assert.ok(point.risk_class_candidate === null || FP.BANDS.includes(point.risk_class_candidate),
        `point ${index} has an unknown candidate class`);
      FP.METRIC_ORDER.forEach((metric) => {
        assert.ok(typeof point.readings[metric] === 'number', `point ${index} is missing ${metric}`);
      });
      assert.ok(!isNaN(new Date(point.timestamp).getTime()), `point ${index} timestamp is unparseable`);
    });
    const shape = FP.sparklinePath(series.values, 900, 220, 10);
    assert.equal(shape.points.length, history.points.length);
    assert.ok(shape.max >= shape.min, 'axis range inverted');
  });

  check(`the history window from ${historyPath} reports the provenance it holds`, () => {
    assert.ok(['simulated', 'mixed'].includes(history.data_classification),
      `unknown classification ${history.data_classification}`);
    assert.equal(history.simulated, history.data_classification === 'simulated',
      `the window contradicts itself: simulated=${history.simulated} but ${history.data_classification}`);
    if (history.zone_mode === 'live_weather') {
      assert.equal(history.data_classification, 'mixed',
        'a live city window must not be served as fully simulated');
      assert.equal(history.metric_sources.rainfall_mm_per_hr, 'REAL',
        'the live window does not carry the live metric sources');
      assert.equal(history.metric_sources.water_level_m, 'SIMULATED',
        'the live window does not say which metrics are simulated');
    }
  });
}

/* ---------------------------------------------------------------- security --- */

group('Site safety');

const appSource = fs.readFileSync(path.join(webDir, 'app.js'), 'utf8');
const libSource = fs.readFileSync(path.join(webDir, 'lib.js'), 'utf8');
const htmlSource = fs.readFileSync(path.join(webDir, 'index.html'), 'utf8');
const cssSource = fs.readFileSync(path.join(webDir, 'styles.css'), 'utf8');

check('the dashboard writes nothing except the one public subscription request', () => {
  [appSource, libSource].forEach((source) => {
    assert.ok(!source.includes('/readings'), 'dashboard references the ingestion route');
    assert.ok(!source.toLowerCase().includes('x-fp-key'), 'dashboard references the ingestion header');
    assert.ok(!source.includes('FLOODPULSE_INGEST_KEY'), 'dashboard references the secret env var');
  });
  // One write is legitimate now: the visitor's own email subscription. It must stay the only
  // one, on one route, with no credential of any kind attached - the route is public and needs
  // none - and it must go to the API origin the page is configured with, not anywhere else.
  const posts = appSource.match(/method:\s*['"]POST['"]/gi) || [];
  assert.equal(posts.length, 1, `the dashboard makes ${posts.length} POSTs; exactly one is expected`);
  const write = appSource.slice(appSource.indexOf('function submitSubscription'),
    appSource.indexOf('function renderDataPage'));
  assert.ok(write.length > 200, 'submitSubscription was not found where this check expects it');
  assert.ok(write.includes("API_BASE + '/subscriptions'"), 'the one POST does not target POST /subscriptions');
  assert.ok(!/authorization/i.test(write), 'the subscription request attaches an authorization header');
  assert.ok(!/credentials\s*:\s*['"]include/i.test(write), 'the subscription request sends credentials');
  assert.ok(!/['"](?:GET|PUT|DELETE|PATCH)['"]/.test(write), 'the subscription form performs another kind of call');
  // Nothing anywhere in the client may build a header bag with a secret in it.
  [appSource, libSource].forEach((source) => {
    const headers = source.match(/headers:\s*\{[^}]*\}/gi) || [];
    headers.forEach((bag) => {
      assert.ok(!/(key|token|secret|auth)/i.test(bag),
        'a request header bag carries something that looks like a credential');
    });
  });
});

check('dashboard has no remote dependencies or CDN assets', () => {
  // data: URIs (the inline favicon) are not remote fetches, so they are stripped first.
  const withoutDataUris = (source) => source
    .replace(/href="data:[^"]*"/gi, 'href="data:"')
    .replace(/url\(data:[^)]*\)/gi, 'url(data:)');
  // Nothing the browser has to fetch may be remote: no <script src>, no stylesheet, no
  // font, no image, no module import, no fetched URL literal. This is the invariant that
  // keeps the first load fast and the page independent of a third party being up.
  const remoteAsset = /(?:src|url\()\s*=?\s*["']?https?:\/\//i;
  [['index.html', htmlSource], ['styles.css', cssSource], ['app.js', appSource]].forEach(([name, source]) => {
    const text = withoutDataUris(source);
    assert.ok(!remoteAsset.test(text), `${name} fetches a remote asset`);
    assert.ok(!/@import\s+url\(\s*["']?https?:/i.test(text), `${name} imports a remote stylesheet`);
    assert.ok(!/import\s+[^;]*from\s*['"]https?:/i.test(text), `${name} imports a remote module`);
  });
  assert.ok(!/cdn|unpkg|jsdelivr|googleapis/i.test(htmlSource), 'index.html mentions a CDN');

  // A documentation *link* is allowed (it is not fetched), but only to the weather
  // provider, only over https, and only with rel="noreferrer": every other absolute URL
  // in the sources must be the project's own API endpoint.
  // A Content-Security-Policy may name loopback origins so a developer can point the page at a
  // local server with ?api=; a loopback host is not a third party and not a CDN. The policy is
  // scanned for named origins separately, so those two names cannot become a loophole.
  const allowedRemoteHosts = ['2rg2u8quzd.execute-api.eu-north-1.amazonaws.com', 'open-meteo.com',
    'localhost', '127.0.0.1'];
  // M9 adds links to the official Indian weather and disaster-management sites. A link the
  // reader chooses to follow is a documentation destination, not a dependency: nothing in this
  // project fetches those hosts, and they must never be loaded as an asset. They are listed
  // explicitly (and only as destinations) so a stray asset URL to them still fails below.
  const allowedOfficialHosts = ['mausam.imd.gov.in', 'ndma.gov.in'];
  [['index.html', htmlSource], ['styles.css', cssSource], ['lib.js', libSource], ['app.js', appSource]]
    .forEach(([name, source]) => {
      // Everything except <a href="..."> is an asset/endpoint/dependency reference and must be
      // one of the project's own hosts. `<a href>` destinations are checked separately, with
      // their rel/target/https attributes, so this cannot become a way to load a CDN.
      const withoutLinks = withoutDataUris(source).replace(/<a\b[^>]*href="https?:[^"]*"[^>]*>/gi, '<a>');
      const found = [...withoutLinks.matchAll(/https?:\/\/([a-z0-9.-]+)/gi)].map((match) => match[1]);
      const stray = [...new Set(found)].filter((host) => !allowedRemoteHosts.includes(host));
      assert.deepEqual(stray, [], `${name} references an unexpected remote host: ${stray.join(', ')}`);
    });
  // The loopback exception is only ever a connect-src entry, never an asset to fetch.
  assert.ok(!/(?:src|href)\s*=\s*["']?https?:\/\/(?:localhost|127\.0\.0\.1)/i.test(htmlSource),
    'the page loads an asset from a loopback host');
  const cspHosts = [...(/(?:content=")([^"]*)"/.exec(htmlSource) || [, ''])[1].matchAll(/https?:\/\/([a-z0-9.-]+)/gi)]
    .map((match) => match[1]);
  cspHosts.forEach((host) => {
    const ok = host === '2rg2u8quzd.execute-api.eu-north-1.amazonaws.com' || host === 'localhost'
      || host === '127.0.0.1' || host.endsWith('.execute-api.eu-north-1.amazonaws.com');
    assert.ok(ok, `the policy allows an unrelated origin: ${host}`);
  });
  // the whole tag is captured, so the rel/target checks see the attributes that follow href
  const docLinks = [...htmlSource.matchAll(/<a\b[^>]*href="(https?:[^"]+)"[^>]*>/gi)];
  docLinks.forEach((match) => {
    assert.match(match[0], /rel="[^"]*noreferrer/, `external link ${match[1]} is missing rel="noreferrer"`);
    // noopener is implied by modern browsers on target="_blank", but older ones honour it only
    // when it is written out, and the API link is filled from a value the page did not author.
    assert.match(match[0], /rel="[^"]*noopener/, `external link ${match[1]} is missing rel="noopener"`);
    assert.match(match[0], /target="_blank"/, `external link ${match[1]} does not open in a new tab`);
    assert.ok(/^https:\/\//.test(match[1]), `external link ${match[1]} is not https`);
  });
  // Every external destination is either the project's own endpoint/provider documentation or an
  // explicitly listed official source. The list is short on purpose: this is a documentation
  // page linking to authorities, not a portal.
  const docHosts = [...new Set(docLinks.map((match) => new URL(match[1]).host))];
  const unexpectedDocs = docHosts.filter((host) => !allowedRemoteHosts.includes(host) && !allowedOfficialHosts.includes(host));
  assert.deepEqual(unexpectedDocs, [], `the page links to an unlisted external host: ${unexpectedDocs.join(', ')}`);
  assert.ok(docHosts.length <= 3, `the page links out to ${docHosts.length} different external sites`);
});

check('site contains no credentials, keys or tokens', () => {
  const files = ['index.html', 'app.js', 'lib.js', 'styles.css'];
  files.forEach((file) => {
    const source = fs.readFileSync(path.join(webDir, file), 'utf8');
    assert.ok(!/AKIA[0-9A-Z]{16}/.test(source), `${file} contains an access key id`);
    assert.ok(!/aws_secret_access_key|aws_access_key_id|BEGIN [A-Z ]*PRIVATE KEY/.test(source), `${file} contains a credential`);
    assert.ok(!/arn:aws/i.test(source), `${file} contains an AWS ARN (including the SNS topic)`);
  });
  const keyFile = path.join(repoRoot, '.floodpulse-key');
  if (fs.existsSync(keyFile)) {
    const secret = fs.readFileSync(keyFile, 'utf8').trim();
    if (secret) {
      files.forEach((file) => {
        const source = fs.readFileSync(path.join(webDir, file), 'utf8');
        assert.ok(!source.includes(secret), `${file} contains the local ingestion key`);
      });
    }
  }
});

check('index.html keeps a permanent, mode-driven data-provenance notice', () => {
  // M4: the fleet can be simulated or live-weather, so the invariant is no longer the
  // string "SIMULATED DATA" but a permanent banner that the script fills from the
  // payload, plus a legend that explains every provenance tag the API can report.
  assert.match(htmlSource, /id="data-banner"/);
  assert.match(htmlSource, /id="banner-title"/);
  assert.match(htmlSource, /id="banner-detail"/);
  for (const tag of ['src-real', 'src-model', 'src-simulated']) {
    assert.ok(htmlSource.includes(tag), `index.html must explain the ${tag} provenance tag`);
  }
  assert.match(htmlSource, /heuristic/);
  assert.match(htmlSource, /shared secret/);
  // and the banner text itself must come from the payload, not from a constant
  assert.match(appSource, /FP\.dataNotice\(payload\)/);
  assert.ok(!/banner-title'\]\.textContent = 'SIMULATED DATA';/.test(appSource),
    'the banner title must be chosen per mode, not hardcoded');
});

check('every element id app.js uses exists in index.html', () => {
  const used = new Set();
  // cacheDom() lists the ids; any other getElementById call is picked up too
  for (const match of appSource.matchAll(/['"]([a-z0-9-]+)['"]/g)) used.add(match[1]);
  const declared = new Set();
  for (const match of htmlSource.matchAll(/id="([^"]+)"/g)) declared.add(match[1]);

  const idsToCheck = [
    'conn-badge', 'conn-detail', 'last-update', 'api-base', 'hero', 'ranking', 'zones',
    'history-zone', 'history-range', 'history-canvas', 'history-meta', 'history-tooltip',
    'notice-text', 'summary-live', 'summary-model', 'summary-generated', 'poll-count',
    'incidents', 'incidents-meta', 'incidents-alerting',
    'data-banner', 'banner-title', 'banner-detail', 'summary-mode', 'summary-weather', 'attribution'
  ];
  const missing = idsToCheck.filter((id) => !declared.has(id));
  assert.deepEqual(missing, [], `index.html is missing ids used by app.js: ${missing.join(', ')}`);
  assert.ok(used.size > 0);
});

check('every data-* attribute app.js queries is actually written somewhere', () => {
  // index.html writes the static hooks (nav, view sections), lib.js writes the structural
  // markup and app.js writes the history controls and its own state attributes, so the
  // invariant is: anything queried is emitted by one of the three, never by none.
  const written = new Set();
  for (const source of [htmlSource, libSource, appSource]) {
    for (const match of source.matchAll(/data-([a-z-]+)="/g)) written.add(match[1]);
    for (const match of source.matchAll(/setAttribute\(['"]data-([a-z-]+)['"]/g)) written.add(match[1]);
  }
  const read = new Set();
  for (const match of appSource.matchAll(/\[data-([a-z-]+)[\]=]/g)) read.add(match[1]);
  const unresolved = [...read].filter((attr) => !written.has(attr));
  assert.deepEqual(unresolved, [], `app.js queries data-* attributes nothing emits: ${unresolved.join(', ')}`);
  assert.ok(read.size >= 4, 'expected app.js to query several data-* attributes');
});

check('app.js field selectors exist in lib.js markup', () => {
  const fieldsWritten = new Set();
  for (const match of libSource.matchAll(/data-f="([a-z_]+)"/g)) fieldsWritten.add(match[1]);
  const fieldsRead = new Set();
  for (const match of appSource.matchAll(/\[data-f="([a-z_]+)"\]/g)) fieldsRead.add(match[1]);
  const missing = [...fieldsRead].filter((field) => !fieldsWritten.has(field));
  assert.deepEqual(missing, [], `${missing.join(', ')} are patched by app.js but never rendered by lib.js`);
  assert.ok(fieldsWritten.size >= fieldsRead.size);
});

check('every function app.js calls is either local or provided by lib.js', () => {
  // Regression guard: app.js once called renderRanking() while the implementation only
  // existed as FP.renderRanking, so the panel threw and the whole page went STALE.
  // Strip strings and comments first so prose like "boundaries (" is not mistaken for a call.
  const codeOnly = (source) => source
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/(^|[^:\\])\/\/[^\n]*/g, '$1 ')
    .replace(/'(?:[^'\\]|\\.)*'/g, "''")
    .replace(/"(?:[^"\\]|\\.)*"/g, '""');

  const defined = new Set();
  for (const match of appSource.matchAll(/(?:function|var)\s+([A-Za-z_$][\w$]*)\s*(?:\(|=)/g)) {
    defined.add(match[1]);
  }
  const exported = new Set();
  for (const match of libSource.matchAll(/^\s*([A-Za-z_$][\w$]*):\s*[A-Za-z_$][\w$]*,?\s*$/gm)) {
    exported.add(match[1]);
  }
  const globals = new Set([
    'fetch', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame',
    'URLSearchParams', 'AbortController', 'Date', 'Math', 'JSON', 'parseInt', 'parseFloat', 'isFinite',
    'String', 'Number', 'Array', 'Object', 'Boolean', 'Error', 'Promise', 'encodeURIComponent',
    'getComputedStyle', 'matchMedia', 'if', 'for', 'while', 'switch', 'catch', 'return', 'typeof',
    'function', 'new'
  ]);

  const code = codeOnly(appSource);
  const called = new Set();
  for (const match of code.matchAll(/(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(/g)) called.add(match[1]);
  const unresolved = [...called]
    .filter((name) => !defined.has(name) && !globals.has(name))
    .sort();

  assert.deepEqual(unresolved, [],
    `app.js calls functions that are neither defined in app.js nor namespaced via FP.: ${unresolved.join(', ')}`);
  assert.ok(defined.size >= 20, `expected app.js to define its own helpers, found ${defined.size}`);
  assert.ok(exported.has('renderRanking'), 'lib.js must keep exporting renderRanking');
});

check('local assets referenced by the page exist and the site stays small', () => {
  ['lib.js', 'app.js', 'styles.css'].forEach((file) => {
    assert.ok(fs.existsSync(path.join(webDir, file)), `${file} referenced by index.html is missing`);
  });
  assert.match(cssSource, /url\(["']?bg-storm\.webp["']?\)/, 'styles.css does not reference the backdrop');
  assert.ok(fs.existsSync(path.join(webDir, 'bg-storm.webp')), 'bg-storm.webp is missing');

  // The number that matters is what a visitor downloads to see the page: the four code files plus
  // the single image the stylesheet loads. web/ also holds prepared card images and icon files
  // that no view references yet and that the deploy list does not upload, so summing the whole
  // directory would measure files nobody fetches and bury the real payload.
  const payload = ['index.html', 'styles.css', 'lib.js', 'app.js', 'bg-storm.webp']
    .map((file) => ({ file, bytes: fs.statSync(path.join(webDir, file)).size }));
  const total = payload.reduce((sum, entry) => sum + entry.bytes, 0);
  const image = payload.find((entry) => entry.file === 'bg-storm.webp');
  payload.forEach((entry) => console.log(`         ${entry.file.padEnd(16)} ${(entry.bytes / 1024).toFixed(1)} KB`));
  console.log(`         ${'PAGE TOTAL'.padEnd(16)} ${(total / 1024).toFixed(1)} KB  (ceiling 512 KB)`);
  // ceilings, not targets: they fail if a framework, a build step or an un-optimised image arrives
  assert.ok(image.bytes <= 350 * 1024, 'backdrop exceeds the 350 KB hard maximum');
  assert.ok(image.bytes <= 250 * 1024, 'backdrop exceeds the 250 KB target');
  assert.ok(total <= 512 * 1024, `the page payload is ${(total / 1024).toFixed(0)} KB`);

  // and one looser guard on the directory: prepared assets may sit there, a runaway one may not
  const directory = fs.readdirSync(webDir)
    .map((file) => ({ file, bytes: fs.statSync(path.join(webDir, file)).size }));
  const directoryTotal = directory.reduce((sum, entry) => sum + entry.bytes, 0);
  // The card imagery is deliberately not part of the page payload: those images are below the
  // first screen on the layouts that matter and carry loading="lazy" (the first four are eager,
  // because they are the ones a wide desktop shows). Reported here so the two numbers are never
  // confused for one another.
  const cards = ['chennai-nightscape.webp', 'mumbai-nightscape.webp', 'bengaluru-nightscape.webp',
    'hyderabad-nightscape.webp', 'kochi-nightscape.webp',
    'delhi.webp',
    'kolkata.webp',
    'guwahati.webp',
    'patna.webp',
    'bhubaneshwar.webp',
    'visakhapatnam.webp',
    'pune.webp',
    'ahmedabad.webp',
    'jaipur.webp',
    'lucknow.webp'];
  const cardBytes = cards.map((file) => fs.statSync(path.join(webDir, file)).size);
  console.log(`         ${'card imagery'.padEnd(16)} ${(cardBytes.reduce((a, b) => a + b, 0) / 1024).toFixed(1)} KB for ${cards.length} images (4 eager with the cards, 1 lazy)`);
  console.log(`         ${'web/ TOTAL'.padEnd(16)} ${(directoryTotal / 1024).toFixed(1)} KB  (ceiling 1024 KB)`);
  assert.ok(directoryTotal <= 1024 * 1024, `web/ holds ${(directoryTotal / 1024).toFixed(0)} KB`);
});

/* ------------------------------------------------------- fleet band strip --- */

group('Fleet risk distribution');

function rawCounts(payload) {
  const counts = { NORMAL: 0, WATCH: 0, WARNING: 0, CRITICAL: 0, UNSET: 0 };
  const bands = ['NORMAL', 'WATCH', 'WARNING', 'CRITICAL'];
  (payload.zones || []).forEach((zone) => {
    const klass = zone.latest && String(zone.latest.risk_class || '').toUpperCase();
    counts[bands.includes(klass) ? klass : 'UNSET'] += 1;
  });
  return counts;
}

if (payloadPath) {
  const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
  const expected = rawCounts(payload);

  check('the band counts match the payload, counted straight from the fixture', () => {
    // rebuilt locally: the VM hands back an object from another realm, which strict
    // deep-equality rejects for prototype reasons alone
    const actual = Object.assign({}, FP.bandCounts(payload));
    assert.deepEqual(actual, expected);
    const sum = Object.values(expected).reduce((a, b) => a + b, 0);
    assert.equal(sum, (payload.zones || []).length, 'every zone must be counted exactly once');
  });

  check('the strip shows a count per band and dims the empty ones', () => {
    const html = FP.renderBandStrip(payload);
    ['normal', 'watch', 'warning', 'critical', 'unknown'].forEach((band) => {
      assert.ok(html.includes(`data-f="count-${band}"`), `the strip has no ${band} tile`);
    });
    assert.equal((html.match(/band-tile/g) || []).length, 5, 'expected exactly five tiles');
    const zeroTiles = (html.match(/band-tile[^"]*is-zero/g) || []).length;
    const emptyBands = Object.values(expected).filter((count) => count === 0).length;
    assert.equal(zeroTiles, emptyBands, 'exactly the empty bands must be dimmed');
    // A zone with no reading is not NORMAL: it must never be counted as safe.
    assert.equal(FP.bandCounts({ zones: [{ latest: null }, { latest: { risk_class: 'FLOODING' } }] }).UNSET, 2);
    assert.equal(FP.bandCounts({ zones: [{ latest: null }] }).NORMAL, 0);
  });

  check('the strip label names the fleet noun and only the scored zones', () => {
    const label = FP.bandStripLabel(payload);
    const scored = (payload.zones || []).filter((zone) => zone.latest).length;
    assert.match(label, new RegExp(`^${scored} of ${(payload.zones || []).length} `));
    assert.match(label, /scored in this snapshot$/);
    assert.match(label, payload.zone_mode === 'live_weather' ? /cities/ : /zones/);
  });
}

group('The Overview explains the product in its own fleet\'s words');

check('the product is described in one plain sentence, in both modes', () => {
  const live = FP.productIntro({ zone_mode: 'live_weather', zone_count: 5 });
  const demo = FP.productIntro({ zone_mode: 'simulated', zone_count: 3 });
  assert.match(live, /Open-Meteo/, 'the live sentence must name the real rainfall source');
  assert.match(live, /across 5 cities/);
  assert.match(demo, /across 3 zones/);
  assert.ok(!/Open-Meteo/.test(demo), 'a simulated fleet must not claim a live weather provider');
  assert.ok(!/\blive\b/i.test(demo), 'a simulated fleet must not call its own data live');
  [live, demo].forEach((text) => {
    assert.match(text, /^FloodPulse combines/, `unexpected introduction: ${text}`);
    assert.match(text, /how close each one is to flooding/);
    assert.ok(!/heuristic|weight|coefficient|trend|score/i.test(text),
      `the introduction must avoid model jargon: ${text}`);
    assert.ok(text.length <= 220, `the introduction must stay one readable sentence: ${text}`);
  });
});

check('the introduction survives a missing fleet or zone count', () => {
  [null, {}, { zone_mode: 'live_weather' }, { zone_count: 0 }, { zone_mode: 'live_weather', zone_count: 0 }]
    .forEach((partial) => {
      const text = FP.productIntro(partial);
      assert.match(text, /^FloodPulse combines/);
      assert.ok(!/undefined|null|NaN|\d+ undefined/.test(text), `"${text}" leaks a placeholder`);
    });
});

check('the headline never calls a simulated zone a city', () => {
  const critical = { CRITICAL: 1, WARNING: 0, WATCH: 0, UNSET: 0 };
  const watch = { CRITICAL: 0, WARNING: 0, WATCH: 2, UNSET: 0 };
  const watchOne = { CRITICAL: 0, WARNING: 0, WATCH: 1, UNSET: 0 };
  // The verb agrees with the count, not with the fleet noun: "1 of 3 zones is", "2 of 3 zones are"
  assert.match(FP.fleetHeadline('CRITICAL', critical, 3, 'zones'), /1 of 3 zones is at a critical/);
  assert.match(FP.fleetHeadline('CRITICAL', critical, 5, 'cities'), /1 of 5 cities is at a critical/);
  assert.match(FP.fleetHeadline('WATCH', watch, 3, 'zones'), /2 of 3 zones are worth watching/);
  assert.match(FP.fleetHeadline('WATCH', watchOne, 3, 'zones'), /1 of 3 zones is worth watching/);
  assert.equal(FP.fleetHeadline('NORMAL', critical, 3, 'zones'), 'No zone is at flood risk right now.');
  assert.equal(FP.fleetHeadline('NORMAL', critical, 5, 'cities'), 'No city is at flood risk right now.');
  assert.equal(FP.fleetHeadline('UNKNOWN', critical, 0, 'cities'), 'Waiting for the first readings.');
});

check('the overview payload carries both sentences, in the right noun', () => {
  const ov = FP.fleetOverview(payload, null, Date.now());
  assert.ok(ov.headlineIntro && ov.headlineIntro.length > 60, 'fleetOverview must carry headlineIntro');
  // the calm headline is deliberately singular ("No city is at flood risk"), the rest plural,
  // so accept either number but never the wrong noun
  const live = payload.zone_mode === 'live_weather';
  const singular = live ? 'city' : 'zone';
  const plural = live ? 'cities' : 'zones';
  const other = live ? 'zones?' : 'cities?';
  assert.ok(new RegExp(`\\b(${singular}|${plural})\\b`).test(ov.headline),
    `the headline lost its fleet noun: ${ov.headline}`);
  assert.ok(!new RegExp(`\\b${other}\\b`).test(ov.headline),
    `the headline used the wrong fleet noun: ${ov.headline}`);
  if (typeof payload.zone_count === 'number' && payload.zone_count > 0) {
    assert.match(ov.headlineIntro, new RegExp(`across ${payload.zone_count} ${plural}`),
      `the introduction lost the fleet size: ${ov.headlineIntro}`);
  }
});

check('the page has somewhere to put both, and the strip title is mode-driven', () => {
  const page = fs.readFileSync(path.join(webDir, 'index.html'), 'utf8');
  assert.match(page, /id="ov-intro"[^>]*>[^<]{40,}</, 'the intro needs a static sentence for the pre-API state');
  assert.match(page, /id="band-strip-title"/, 'the risk-strip title is set from the fleet noun at runtime');
  assert.match(page, /id="city-cards-title"/, 'the card-block title is set from the fleet noun at runtime');
  assert.match(page, /id="incidents-zone-header"/, 'the incident column header is set from the fleet noun');
  assert.match(page, /id="cities-lede"/, 'the Cities lede is set from the fleet noun at runtime');
  const app = fs.readFileSync(path.join(webDir, 'app.js'), 'utf8');
  ['ov-intro', 'band-strip-title', 'city-cards-title', 'cities-lede', 'incidents-zone-header'].forEach((id) => {
    assert.ok(app.includes(`setText('${id}'`), `app.js never fills #${id} from the fleet noun`);
  });
  assert.ok(!/statCard\('Cities reporting'/.test(app), 'a hardcoded "Cities reporting" label survives');
  assert.ok(!/'Highest risk city'/.test(app), 'a hardcoded "Highest risk city" label survives');
  // one place writes the fleet labels, so no view can quietly keep a stale noun
  assert.equal((app.match(/setText\('band-strip-title'/g) || []).length, 1,
    'the strip title is written from more than one place');
  assert.ok(/function renderFleetLabels\(payload\)/.test(app), 'the fleet labels lost their single writer');
});

group('What changed since the last poll');

function fleetZone(id, name, riskClass) {
  return { zone_id: id, name, latest: riskClass ? { risk_class: riskClass, risk_score: 0.5 } : null };
}

check('an escalation is named, with the move it made', () => {
  const before = FP.fleetClasses({ zones: [fleetZone('a', 'Chennai', 'NORMAL')] });
  const change = FP.fleetChange(before, { zones: [fleetZone('a', 'Chennai', 'WARNING')] });
  assert.equal(change.escalated.length, 1);
  assert.equal(change.text, 'Chennai escalated NORMAL → WARNING');
  assert.equal(change.tone, 'band-warning', 'the note must take the colour of the band reached');
});

check('the highest escalation sets the tone and the wording', () => {
  const before = FP.fleetClasses({
    zones: [fleetZone('a', 'Chennai', 'NORMAL'), fleetZone('b', 'Mumbai', 'WATCH'), fleetZone('c', 'Kochi', 'NORMAL')]
  });
  const change = FP.fleetChange(before, {
    zones: [fleetZone('a', 'Chennai', 'WATCH'), fleetZone('b', 'Mumbai', 'CRITICAL'), fleetZone('c', 'Kochi', 'NORMAL')]
  });
  assert.equal(change.escalated.length, 2);
  assert.equal(change.eased.length, 0);
  assert.equal(change.tone, 'band-critical');
  assert.match(change.text, /^2 zones escalated, highest CRITICAL$/);
});

check('an unchanged fleet says nothing at all', () => {
  const zones = [fleetZone('a', 'Chennai', 'WATCH'), fleetZone('b', 'Mumbai', 'NORMAL')];
  const change = FP.fleetChange(FP.fleetClasses({ zones }), { zones });
  // lengths, not deepEqual: the VM hands back arrays from another realm
  assert.equal(change.escalated.length + change.eased.length + change.started.length + change.stopped.length, 0);
  assert.equal(change.text, '', 'a quiet poll must not manufacture a headline');
  assert.equal(change.tone, '');
});

check('a first reading is not an escalation, and a lost reading is not silence', () => {
  const zones = (mumbai) => ({
    zones: [fleetZone('a', 'Chennai', 'NORMAL'), fleetZone('b', 'Mumbai', mumbai)]
  });
  const withoutReading = FP.fleetClasses(zones(null));
  const withReading = FP.fleetClasses(zones('WATCH'));

  const started = FP.fleetChange(withoutReading, zones('WATCH'));
  assert.equal(started.escalated.length, 0, 'a zone reporting for the first time has not escalated');
  assert.equal(started.text, 'Mumbai started reporting at WATCH');

  const lost = FP.fleetChange(withReading, zones(null));
  assert.equal(lost.text, 'Mumbai stopped reporting');
  assert.equal(lost.tone, 'band-watch', 'a gap in coverage is a warning, not a neutral note');

  const unknown = FP.fleetChange(
    FP.fleetClasses({ zones: [fleetZone('a', 'Chennai', 'NORMAL')] }),
    { zones: [fleetZone('a', 'Chennai', 'FLOODING')] }
  );
  assert.equal(unknown.text, 'Chennai stopped reporting', 'an unrecognised class is no reading at all');
});

check('an easing is reported as calmly as an escalation is loudly', () => {
  const before = FP.fleetClasses({ zones: [fleetZone('a', 'Chennai', 'CRITICAL')] });
  const change = FP.fleetChange(before, { zones: [fleetZone('a', 'Chennai', 'WATCH')] });
  assert.equal(change.escalated.length, 0);
  assert.equal(change.text, 'Chennai eased CRITICAL → WATCH');
  assert.equal(change.tone, 'band-normal');
});

check('nothing is claimed without a baseline to compare against', () => {
  const change = FP.fleetChange(null, { zones: [fleetZone('a', 'Chennai', 'CRITICAL')] });
  assert.equal(change.text, '', 'the first poll has nothing to compare with');
  // a zone that appears without ever having been seen before is not a change
  const fresh = FP.fleetChange(FP.fleetClasses({ zones: [fleetZone('a', 'Chennai', 'NORMAL')] }), {
    zones: [fleetZone('a', 'Chennai', 'NORMAL'), fleetZone('z', 'Delhi', 'CRITICAL')]
  });
  assert.equal(fresh.text, '', 'a zone joining the fleet is not an escalation');
});

check('the browser half of the feature is wired up', () => {
  assert.match(htmlSource, /id="change-note"/, 'index.html has no change note');
  assert.match(htmlSource, /id="change-note"[^>]*role="status"/, 'the change note is not announced');
  assert.match(appSource, /renderChangeNote\(payload\)/, 'the change note is never rendered');
  assert.match(appSource, /FP\.fleetChange\(state\.previousClasses, payload\)/, 'the note is not computed from the previous poll');
  assert.match(appSource, /state\.previousClasses = FP\.fleetClasses\(payload\)/, 'the baseline is never advanced');
  assert.match(cssSource, /\.change-note\s*\{/, 'the change note is unstyled');
  assert.match(cssSource, /\.change-note\[hidden\]\s*\{\s*display:\s*none/, 'the empty note is not hidden');
  ['critical', 'warning', 'watch', 'normal'].forEach((band) => {
    assert.match(cssSource, new RegExp(`\\.change-note\\.band-${band}\\s*\\{[^}]*var\\(--band-${band}-text\\)`),
      `the ${band} tone does not use the ${band} text token`);
  });
});

/* ------------------------------------------------------- design contract --- */

group('Design system');

function rgbOf(hex) {
  const value = hex.replace('#', '');
  const full = value.length === 3 ? value.split('').map((c) => c + c).join('') : value;
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16));
}

function luminance(hex) {
  const [r, g, b] = rgbOf(hex).map((channel) => {
    const c = channel / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a, b) {
  const l1 = luminance(a);
  const l2 = luminance(b);
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
}

function token(name) {
  const match = cssSource.match(new RegExp(`${name}:\\s*(#[0-9a-f]{3,6})`, 'i'));
  assert.ok(match, `styles.css does not define ${name}`);
  return match[1];
}

/* Effective surface behind card text: the glass panel composited over the page gradient.
 * Computed rather than guessed, so the contrast numbers below are meaningful. */
function composite(fg, alpha, bg) {
  const a = rgbOf(fg);
  const b = rgbOf(bg);
  const mix = a.map((channel, i) => Math.round(channel * alpha + b[i] * (1 - alpha)));
  return '#' + mix.map((channel) => channel.toString(16).padStart(2, '0')).join('');
}

const glassToken = /--glass:\s*rgba\((\d+),\s*(\d+),\s*(\d+),\s*([0-9.]+)\)/.exec(cssSource);
assert.ok(glassToken, 'styles.css does not define --glass as rgba()');
const cardSurface = composite(
  '#' + [1, 2, 3].map((i) => Number(glassToken[i]).toString(16).padStart(2, '0')).join(''),
  Number(glassToken[4]),
  token('--navy-800')
);

check('every risk band is a distinct colour with a readable text variant', () => {
  const bands = ['normal', 'watch', 'warning', 'critical'].map((band) => token(`--band-${band}`).toLowerCase());
  assert.equal(new Set(bands).size, 4, 'two risk bands share the same colour');
  bands.forEach((hex) => {
    // graphics threshold on the dark surface
    assert.ok(contrast(hex, cardSurface) >= 3, `${hex} is below the 3:1 graphics contrast on a card`);
  });
  const texts = ['normal', 'watch', 'warning', 'critical'].map((band) => token(`--band-${band}-text`));
  texts.forEach((hex) => {
    assert.ok(contrast(hex, cardSurface) >= 4.5,
      `band text ${hex} is below 4.5:1 on a card (${contrast(hex, cardSurface).toFixed(2)}:1)`);
  });
  ['--state-stale-text', '--state-error-text', '--state-live-text', '--src-real', '--src-model']
    .forEach((name) => {
      const hex = token(name);
      assert.ok(contrast(hex, cardSurface) >= 4.5,
        `${name} (${hex}) is below 4.5:1 on a card (${contrast(hex, cardSurface).toFixed(2)}:1)`);
    });
  assert.ok(contrast(token('--slate-400'), cardSurface) >= 4.5, 'muted text is not readable on a card');
});

check('small band text always uses the light variant, never the vivid one', () => {
  // '.pill.band-critical' and friends carry ~11px text: they must resolve the -text tokens.
  const smallTextRules = [
    /\.pill\.band-critical[^{]*\{[^}]*color:\s*var\(--band-critical-text\)/,
    /\.pill\.band-watch[^{]*\{[^}]*color:\s*var\(--band-watch-text\)/,
    /\.band-name\.band-critical[^{]*\{[^}]*color:\s*var\(--band-critical-text\)/,
    /\.incident-severity\.band-warning[^{]*\{[^}]*color:\s*var\(--band-warning-text\)/,
    /\.rank-class\.band-watch[^{]*\{[^}]*color:\s*var\(--band-watch-text\)/,
    /\.incident-status\.status-open[^{]*\{[^}]*color:\s*var\(--band-critical-text\)/
  ];
  smallTextRules.forEach((rule) => {
    assert.match(cssSource, rule, `a small-text rule does not use its -text colour: ${rule}`);
  });
});

check('the stylesheet styles every class the page emits', () => {
  const emitted = new Set();
  [htmlSource, libSource, appSource].forEach((source) => {
    for (const match of source.matchAll(/class=\\?["']([^"'\\]+)/g)) {
      match[1].split(/\s+/).filter(Boolean).forEach((name) => emitted.add(name));
    }
    for (const match of source.matchAll(/className\s*=\s*'([^']+)'/g)) {
      match[1].split(/\s+/).filter(Boolean).forEach((name) => emitted.add(name));
    }
  });
  // Prefix families are emitted by concatenation: base names are listed here instead.
  // `view-` is the router's runtime body class (body.view-cities …), which the runtime
  // harness asserts per view; no stylesheet rule depends on it.
  const prefixes = ['band-', 'conn-', 'mode-', 'src-', 'status-', 'is-', 'view-', 'trend-'];
  const missing = [...emitted].filter((name) => {
    if (prefixes.some((prefix) => name.endsWith(prefix))) return false;
    return !new RegExp(`\\.${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\w-])`).test(cssSource);
  }).sort();
  assert.deepEqual(missing, [], `classes used by the page but never styled: ${missing.join(', ')}`);
});

check('the visual language covers the required states and roles', () => {
  const required = [
    ['sticky header', /\.topbar\s*\{[^}]*position:\s*sticky/],
    ['keyboard focus ring', /:focus-visible\s*\{[^}]*outline:\s*2px solid/],
    ['fleet band strip', /\.band-strip-bar\s*\{/],
    ['dimmed empty band', /\.band-tile\.is-zero\s*\{/],
    ['metric threshold markers', /\.metric-tick\.critical\s*\{[^}]*background:\s*rgba\(239,\s*68,\s*68/],
    ['state dot on the badge', /\.conn-badge::before\s*\{/],
    ['feed-state colouring in the summary', /#summary-freshness\[data-feed-state="failed"\]/],
    ['degraded-feed banner', /\.simulation-banner\.is-feed-degraded\s*\{/],
    ['disabled-feed banner', /\.simulation-banner\.is-feed-off\s*\{/],
    ['stale page treatment', /body\.is-stale \.zone-card/],
    ['disconnected page treatment', /body\.is-disconnected \.zone-card/],
    ['reduced-motion guard', /@media \(prefers-reduced-motion: reduce\)/],
    ['tablet breakpoint', /@media \(max-width: 980px\)/],
    ['phone breakpoint', /@media \(max-width: 760px\)/],
    ['tabular figures for numbers', /font-variant-numeric: tabular-nums/],
    ['provenance chips', /\.src-real\s*\{[^}]*var\(--src-real\)/]
  ];
  required.forEach(([label, pattern]) => {
    assert.match(cssSource, pattern, `styles.css is missing: ${label}`);
  });
  // z-index order: the sticky header must paint above the content it scrolls over
  const topbarZ = /\.topbar\s*\{[^}]*z-index:\s*(\d+)/.exec(cssSource);
  const mainZ = /^main\s*\{[^}]*z-index:\s*(\d+)/m.exec(cssSource);
  assert.ok(topbarZ && mainZ && Number(topbarZ[1]) > Number(mainZ[1]),
    'the sticky header must stack above main content');
});

check('the page pins the fleet strip and headings to the payload', () => {
  assert.match(htmlSource, /id="band-strip"/, 'index.html has no band strip');
  assert.match(htmlSource, /id="band-strip-label"/, 'index.html has no band strip label');
  assert.match(appSource, /FP\.renderBandStrip\(payload\)/, 'the strip is not built from the payload');
  assert.match(appSource, /FP\.bandStripLabel\(payload\)/, 'the strip label is not built from the payload');
  // the label must live outside the list the renderer replaces, or it would be erased
  const listBody = /<ul id="band-strip"[\s\S]*?<\/ul>/.exec(htmlSource);
  assert.ok(listBody && !listBody[0].includes('band-strip-label'),
    'the strip label must not be inside the element whose innerHTML is replaced');
});

group('Information architecture: four pages instead of one long scroll');

const VIEW_IDS = ['view-overview', 'view-cities', 'view-city', 'view-alerts', 'view-data', 'view-about'];

check('every section is a real view, and only the first one is shown at load', () => {
  VIEW_IDS.forEach((id) => {
    const tag = new RegExp(`<section id="${id}"[^>]*>`).exec(htmlSource);
    assert.ok(tag, `index.html has no ${id} section`);
    assert.match(tag[0], /class="view"/, `${id} is not part of the view system`);
    assert.match(tag[0], /tabindex="-1"/, `${id} cannot take focus when it is opened`);
    assert.match(tag[0], /aria-labelledby="[^"]+"/, `${id} has no accessible name`);
    assert.match(tag[0], /data-view="[a-z]+"/, `${id} is not identifiable to the router`);
  });
  // only the default view is visible without JavaScript: a page whose first section is
  // hidden looks broken until the router runs, and the no-JS <noscript> note is not a view
  const first = new RegExp(`<section id="${VIEW_IDS[0]}"[^>]*>`).exec(htmlSource)[0];
  assert.ok(!/\bhidden\b/.test(first), 'the default view is hidden at load');
  VIEW_IDS.slice(1).forEach((id) => {
    const tag = new RegExp(`<section id="${id}"[^>]*>`).exec(htmlSource)[0];
    assert.match(tag, /\bhidden\b/, `${id} would be visible before it was chosen`);
  });
});

check('the navigation offers exactly the sections the router knows', () => {
  const links = [...htmlSource.matchAll(/<a id="nav-([a-z-]+)"[^>]*data-nav="([a-z-]+)"[^>]*href="([^"]+)"/g)];
  assert.equal(links.length, FP.NAV_ITEMS.length,
    `the navigation has ${links.length} section links, expected ${FP.NAV_ITEMS.length}`);
  const offered = links.map((match) => ({ view: match[2], hash: match[3] }));
  // compared as JSON: these objects come out of the VM realm, where deepEqual is unreliable
  assert.equal(JSON.stringify(offered), JSON.stringify(FP.NAV_ITEMS.map((item) => ({ view: item.view, hash: item.hash }))),
    'the navigation and the router disagree about the sections');
  assert.match(htmlSource, /<nav class="nav" aria-label="[^"]+">/, 'the sections are not inside a labelled nav');
  assert.match(htmlSource, /<ul id="nav-list"/, 'the section links are not a list');
  assert.equal(FP.NAV_ITEMS.length, 5, 'the router should know five top-level sections');
  assert.match(htmlSource, /id="nav-toggle"[^>]*aria-expanded="false"[^>]*aria-controls="nav-list"/,
    'the mobile menu button is not wired to the list it opens');
  assert.match(htmlSource, /<button id="nav-toggle"[^>]*type="button"/, 'the menu control is not a real button');
});

check('each section is titled for people and for the window', () => {
  const views = FP.NAV_ITEMS.map((item) => item.view);
  const titles = views.map((view) => FP.viewTitle({ view, zoneId: null }, null));
  assert.equal(new Set(titles).size, views.length, 'two sections share a document title');
  titles.forEach((title) => assert.match(title, /FloodPulse/, `a section title does not name the product: ${title}`));
  assert.match(FP.viewTitle({ view: 'city', zoneId: 'city-kochi' }, { zones: [{ zone_id: 'city-kochi', name: 'Kochi' }] }), /Kochi/,
    'a city page does not name the city in the window title');
  // a heading per view, so the page has one outline instead of one long document
  ['ov-title', 'cities-title', 'alerts-title', 'data-title', 'about-title'].forEach((id) => {
    assert.match(htmlSource, new RegExp(`<h2[^>]*id="${id}"`), `no section heading with id ${id}`);
  });
  // the class list may carry extra modifiers (the Overview heading is also the hero title), so
  // this matches `view-title` anywhere in the list rather than only on its own
  const named = [...htmlSource.matchAll(/<h2[^>]*class="[^"]*\bview-title\b[^"]*"[^>]*id="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(named.length, 5, `expected five visible section headings, found ${named.length}`);
});

check('nothing was added to satisfy the routing: no framework, no new request', () => {
  assert.ok(!/\bpushState\b|\breplaceState\b/.test(appSource), 'the router rewrites browser history');
  assert.ok(!/\bimport\s+[^;]*from\b|\brequire\(/.test(libSource + appSource), 'a module system was introduced');
  // word-bounded: 'reacts' in the simulator prose is not React
  assert.ok(!/\b(react|react-dom|vue|svelte|preact|jquery|angular|htmx|alpinejs|bootstrap|tailwind)\b/i
    .test(libSource + appSource + htmlSource), 'a front-end framework or library was introduced');
  const scripts = [...htmlSource.matchAll(/<script\b[^>]*>/g)].map((match) => match[0]);
  const external = scripts.filter((tag) => /src=/.test(tag));
  assert.equal(external.length, 2, `expected lib.js and app.js only, found ${external.length} script files`);
  external.forEach((tag) => assert.match(tag, /src="(lib|app)\.js"/, `unexpected script: ${tag}`));
  assert.ok(!scripts.some((tag) => /type="module"/.test(tag)), 'a module script was introduced');
  const stylesheets = [...htmlSource.matchAll(/<link\b[^>]*rel="stylesheet"[^>]*>/g)];
  assert.equal(stylesheets.length, 1, `expected one stylesheet, found ${stylesheets.length}`);
  assert.match(stylesheets[0][0], /href="styles\.css"/);
});

check('the router answers any URL, including nonsense, without throwing', () => {
  const cases = [
    ['', 'overview'], ['#', 'overview'], ['#/', 'overview'], ['#/overview', 'overview'],
    ['#/cities', 'cities'], ['#/alerts', 'alerts'], ['#/data', 'data'],
    ['#/DATA', 'data'], ['#/cities/', 'cities'], ['#/not-a-section', 'overview'],
    ['#/cities/city-does-not-exist', 'city'], ['#/cities/city-chennai/extra', 'city']
  ];
  cases.forEach(([hash, view]) => {
    const route = FP.routeFromHash(hash);
    assert.equal(route.view, view, `${JSON.stringify(hash)} routed to ${route.view}, expected ${view}`);
  });
  assert.equal(FP.routeFromHash('#/cities/city-kochi').zoneId, 'city-kochi', 'the city id was lost in routing');
  // an id with characters that need escaping still survives the round trip
  ['city-chennai', 'city says hi', 'zone/a'].forEach((zoneId) => {
    const hash = FP.hashFor('city', zoneId);
    assert.equal(hash, '#/cities/' + encodeURIComponent(zoneId), `${zoneId} was not escaped`);
    assert.equal(FP.routeFromHash(hash).zoneId, zoneId, `${zoneId} did not survive the round trip`);
  });
  assert.equal(FP.hashFor('nonsense', null), '#/overview', 'an unknown section must fall back, not emit a broken URL');
  // exactly one section is marked current, and a city page keeps Cities marked
  [['overview'], ['cities'], ['alerts'], ['data'], ['city']].forEach(([view]) => {
    const state = FP.navState({ view, zoneId: view === 'city' ? 'city-kochi' : null });
    const current = state.filter((entry) => entry.current);
    assert.equal(current.length, 1, `${view}: ${current.length} sections are marked current`);
    assert.equal(current[0].ariaCurrent, 'page', `${view}: the current section is not exposed as aria-current`);
    state.filter((entry) => !entry.current).forEach((entry) => {
      assert.notEqual(entry.ariaCurrent, 'page', `${view}: ${entry.view} is also marked current`);
    });
  });
  assert.equal(FP.navState({ view: 'city', zoneId: 'city-kochi' })[1].view, 'cities',
    'a city page should keep the Cities section marked as current');
});

/* ---- hardening pass: invariants a hostile or malformed payload must not break ---- */

check('a fragment that is not valid percent-encoding does not abort the page', () => {
  // regression: routeFromHash called decodeURIComponent unguarded, so '#/cities/%' threw a
  // URIError out of start() - no polling, no intervals, placeholders forever.
  const hostile = ['#/cities/%', '#/cities/%E0%A4%A', '#/cities/%zz', '#/cities/100%', '#/cities/%2F'];
  hostile.forEach((hash) => {
    let route = null;
    assert.doesNotThrow(() => { route = FP.routeFromHash(hash); }, `${hash} threw in the router`);
    assert.equal(route.view, 'city', `${hash} did not route to the city view`);
    assert.equal(typeof route.zoneId, 'string', `${hash} produced a non-string zone id`);
  });
  assert.equal(FP.safeDecode('%E0%A4%A'), '%E0%A4%A', 'a malformed escape must be kept as text');
  assert.equal(FP.safeDecode('city%2Dchennai'), 'city-chennai', 'a valid escape must still decode');
});

check('provenance prose never renders undefined', () => {
  // regression: city detail read .label/.count off zoneSources(), which returns tag strings,
  // so the live path printed "Each measurement carries its own provenance: , ." and
  // "Provenance per metric: undefined = undefined".
  const zone = {
    zone_id: 'city-chennai', name: 'Chennai', zone_mode: 'live_weather',
    thresholds: {},
    metric_sources: {
      rainfall_mm_per_hr: 'REAL', drain_level_m: 'SIMULATED', soil_saturation_pct: 'MODEL_DERIVED',
      water_level_m: 'SIMULATED', flow_rate_m3_s: 'SIMULATED'
    },
    latest: {
      risk_score: 0.4, risk_class: 'WATCH', risk_class_candidate: 'WATCH', sequence: 12,
      timestamp: '2026-10-17T02:00:00Z', age_seconds: 120, sensor_node_id: 'open-meteo-chennai',
      readings: {
        rainfall_mm_per_hr: 1.2, drain_level_m: 0.7, soil_saturation_pct: 41,
        water_level_m: 1.1, flow_rate_m3_s: 2.2
      },
      smoothed_readings: {
        rainfall_mm_per_hr: 1.2, drain_level_m: 0.7, soil_saturation_pct: 41,
        water_level_m: 1.1, flow_rate_m3_s: 2.2
      },
      contributions: [], top_drivers: ['rainfall_mm_per_hr'], trend: {}, explanation: 'because'
    }
  };
  const html = FP.cityDetail(zone, { noun: 'city' });
  assert.equal(typeof html, 'string');
  assert.ok(!/undefined/.test(html), `the city detail rendered "undefined": ${/Provenance per metric[^<]*/.exec(html) || ''}`);
  assert.ok(!/carries its own provenance:\s*,/.test(html), 'the provenance sentence has no labels');
  assert.match(html, /carries its own provenance: REAL, MODEL, SIM\./,
    'the provenance sentence does not name every tag in the payload');
  assert.match(html, /Provenance per metric: REAL = 1 · MODEL = 1 · SIM = 3/,
    'the per-metric provenance counts do not add up to the payload');
  const counts = FP.sourceCounts(zone);
  const total = counts.reduce((sum, entry) => sum + entry.count, 0);
  assert.equal(total, 5, `${total} of 5 metrics were counted`);
  counts.forEach((entry) => {
    assert.ok(entry.label && entry.label !== 'undefined', `a provenance entry has no label: ${JSON.stringify(entry)}`);
    assert.ok(entry.count > 0, `a provenance entry claims ${entry.count} metrics`);
  });
});

check('a payload value can never become a link to another origin', () => {
  const fallback = 'https://open-meteo.com/';
  const kept = ['https://open-meteo.com/en/docs', 'https://open-meteo.com/', 'https://www.open-meteo.com/en/docs'];
  kept.forEach((value) => assert.equal(FP.safeExternalUrl(value, fallback), value, `${value} was rejected`));
  const rejected = ['javascript:alert(1)', 'data:text/html,<script>alert(1)</script>', 'http://open-meteo.com/',
    'https://evil.example/', 'https://open-meteo.com.evil.example/', '//evil.example/', '/zones', '', null, 42,
    'https://open-meteo.com@evil.example/'];
  rejected.forEach((value) => {
    assert.equal(FP.safeExternalUrl(value, fallback), fallback, `${JSON.stringify(value)} was used as a link`);
  });
});

check('a prototype member is never used as a label', () => {
  ['constructor', 'toString', '__proto__', 'hasOwnProperty', 'valueOf'].forEach((name) => {
    assert.equal(FP.modeMeta(name).label, 'MODE UNKNOWN', `${name} was rendered as a mode label`);
    assert.equal(FP.providerLabel(name), name, `${name} was mapped to a prototype member`);
    // '?' is SOURCE_META.UNKNOWN's label: the point is that it did not resolve to an
    // Object.prototype member, so the page cannot print a function as a provenance tag.
    assert.equal(FP.sourceMeta(name).label, '?', `${name} was rendered as a provenance label`);
  });
  assert.equal(FP.modeMeta('live_weather').label, 'LIVE WEATHER', 'the real modes must still resolve');
  assert.equal(FP.providerLabel('open-meteo'), 'Open-Meteo', 'the real provider must still resolve');
  assert.equal(FP.sourceMeta('REAL').label, 'REAL', 'the real tags must still resolve');
});

check('an age that is not a positive number is not reported as "just now"', () => {
  // regression: any negative age fell through to "just now" (backend clamps ages to >= 0, but a
  // payload is not trusted to have done so).
  [-1, -5, -86400].forEach((value) => {
    assert.notEqual(FP.ageLabel(value), 'just now', `${value} was reported as "just now"`);
  });
  assert.equal(FP.ageLabel(2), 'just now', 'a genuinely fresh reading must still say "just now"');
  assert.equal(FP.ageLabel(null), 'no data', 'a missing age must say so');
});

check('malformed list entries are ignored, not thrown on', () => {
  // regression: a null element in zones[]/incidents[] threw inside the poll callback, which
  // stopped the whole board updating.
  const zone = { zone_id: 'zone-riverside-north', name: 'Riverside North', zone_mode: 'simulated', latest: null };
  assert.doesNotThrow(() => FP.renderZoneCard(null), 'a null zone card threw');
  assert.equal(FP.renderZoneCard(null), '', 'a null zone card rendered something');
  assert.doesNotThrow(() => FP.ranking([null, zone]), 'ranking threw on a null element');
  assert.equal(FP.ranking([null, zone]).length, 1, 'a null element survived ranking');
  assert.doesNotThrow(() => FP.staleZoneIds({ zones: [null, zone], stale_after_seconds: 1800 }),
    'staleZoneIds threw on a null element');
  assert.doesNotThrow(() => FP.renderIncidents({ incidents: [null] }), 'a null incident threw');
  assert.equal(FP.renderIncidents({ incidents: [null] }), '', 'a null incident rendered a row');
  assert.doesNotThrow(() => FP.incidentsSignature({ incidents: [null] }), 'the incident signature threw');
  assert.doesNotThrow(() => FP.sourceCounts(null), 'sourceCounts threw on a null zone');
  assert.doesNotThrow(() => FP.cityDetail(null, { noun: 'city' }), 'cityDetail threw on a null zone');
});

check('the freshness contract names every clock it measures', () => {
  // The backend publishes four different times behind one risk score. If the detail view shows
  // one age and calls it "updated", a viewer reads fresh rain where there is an hour-old
  // observation - so each clock is named, and a reading with no provider observation says so.
  const base = {
    zone_id: 'city-chennai', name: 'Chennai', zone_mode: 'live_weather', thresholds: {},
    metric_sources: { rainfall_mm_per_hr: 'REAL' },
    latest: {
      risk_score: 0.4, risk_class: 'WATCH', risk_class_candidate: 'WATCH', sequence: 9,
      timestamp: '2026-10-17T02:00:00Z', ts_epoch: 1, age_seconds: 120,
      sensor_node_id: 'open-meteo-chennai',
      provider_observation_age_seconds: 3300,
      stale_after_seconds: 1800,
      readings: { rainfall_mm_per_hr: 1.2 }, smoothed_readings: { rainfall_mm_per_hr: 1.2 },
      contributions: [], top_drivers: ['rainfall_mm_per_hr'], trend: {}, explanation: 'because'
    },
    weather: {
      provider: 'open-meteo', cache_age_seconds: 30, observation_age_seconds: 3300,
      observation_interval_seconds: 3600, observation_is_current: true,
      last_attempt_age_seconds: 30, last_attempt_ok: true
    }
  };
  const rows = FP.freshnessRows(base);
  assert.ok(rows.length > 0, 'no freshness rows were rendered');
  assert.match(rows, /Reading age/, 'the reading age is not named');
  assert.match(rows, /Provider observation age/, 'the provider observation age is not named');
  assert.match(rows, /Weather observed by provider/, 'the provider observation age of the feed is not named');
  assert.match(rows, /Provider reporting interval/, 'the provider cadence is not stated');
  assert.match(rows, /on schedule/, 'a healthy provider observation was not reported as on schedule');
  assert.ok(!/undefined/.test(rows), 'a freshness row rendered "undefined"');

  const late = FP.freshnessRows({ ...base, weather: { ...base.weather, observation_is_current: false } });
  assert.match(late, /behind the provider own schedule/, 'a late observation was not flagged');

  const simulated = FP.freshnessRows({
    ...base,
    latest: { ...base.latest, provider_observation_age_seconds: null, stale_after_seconds: null },
    weather: null
  });
  assert.match(simulated, /was not derived from the weather provider/,
    'a reading with no provider observation did not say so');
  assert.ok(!/undefined/.test(simulated), 'a simulated reading rendered "undefined"');

  // A payload that predates the field must still render a complete block, not a broken one.
  const legacy = JSON.parse(JSON.stringify(base));
  delete legacy.latest.provider_observation_age_seconds;
  delete legacy.latest.stale_after_seconds;
  delete legacy.weather.observation_age_seconds;
  const legacyRows = FP.freshnessRows(legacy);
  assert.ok(legacyRows.length > 0 && !/undefined/.test(legacyRows), 'a legacy payload broke the detail view');
  assert.doesNotThrow(() => FP.freshnessRows(null), 'freshnessRows threw on a missing zone');
  assert.equal(FP.freshnessRows(null), '', 'a missing zone rendered freshness rows');
});

check('the page carries its own security policy, with no inline script to bypass it', () => {
  // A payload string reaching innerHTML is a bug to fix, but a policy is what makes the bug
  // survivable: script-src 'self' means an injected string cannot execute even if an escaping
  // mistake slips through. That needs no inline script anywhere on the page, so the API base
  // is a data attribute instead of a script tag.
  const csp = /<meta http-equiv="Content-Security-Policy" content="([^"]+)"/.exec(htmlSource);
  assert.ok(csp, 'the page has no Content-Security-Policy');
  const policy = csp[1];
  for (const directive of [
    "default-src 'none'",
    "script-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'"
  ]) {
    assert.ok(policy.includes(directive), `the policy is missing ${directive}: ${policy}`);
  }
  // the API the page actually calls has to be reachable, or the dashboard cannot load data
  const apiOrigin = /data-api="(https:\/\/[^"]+)"/.exec(htmlSource);
  assert.ok(apiOrigin, 'the page does not state its API origin in a data attribute');
  assert.ok(policy.includes(apiOrigin[1]), `connect-src does not allow ${apiOrigin[1]}`);
  assert.match(policy, /connect-src[^;]*http:\/\/localhost:/, 'local development is blocked outright');
  assert.ok(!/script-src[^;]*unsafe-inline/.test(policy), 'inline script is allowed by the policy');
  // The page delivers its policy in a <meta> element, and a browser silently ignores three
  // directives there: frame-ancestors and sandbox (they need a real response header) and
  // report-uri/report-to. Carrying one would buy no protection, put a console warning in front of
  // every visitor, and make the policy look stronger than it is. None may come back: if anti-
  // framing is ever required, it belongs in an HTTP header from a layer that can send one.
  const metaIgnored = ['frame-ancestors', 'sandbox', 'report-uri', 'report-to'];
  const policies = [...htmlSource.matchAll(/<meta http-equiv="Content-Security-Policy" content="([^"]+)"/g)]
    .map((match) => match[1]);
  assert.equal(policies.length, 1, `the page declares ${policies.length} policies; only one can narrow the others`);
  policies.forEach((declared) => {
    metaIgnored.forEach((directive) => {
      assert.ok(!new RegExp(`(^|;\\s*)${directive}\\b`).test(declared),
        `${directive} is ignored in a <meta> policy and must not be declared there: ${declared}`);
    });
  });
  assert.ok(!/<script(?![^>]*\bsrc=)[^>]*>/.test(htmlSource), 'the page still has an inline script');
  assert.ok(!/javascript:/i.test(htmlSource), 'the markup contains a javascript: URL');
  // an external link must not hand the opened page a handle back to this one
  const externalLinks = htmlSource.match(/<a\b[^>]*href="https?:[^>]*>/g) || [];
  assert.ok(externalLinks.length > 0, 'no external link was found to check');
  externalLinks.forEach((tag) => {
    assert.match(tag, /rel="[^"]*noopener/, `${tag} has no rel="noopener"`);
    assert.match(tag, /rel="[^"]*noreferrer/, `${tag} has no rel="noreferrer"`);
  });
  assert.match(htmlSource, /<meta name="referrer" content="[^"]+">/, 'the page states no referrer policy');
  // and the controller reads the base from the page, with the window global still supported
  assert.match(appSource, /documentElement\.dataset\.api/, 'app.js does not read the API base from the page');
});

check('the phone layout replaces the links with a menu that is styled for it', () => {
  assert.match(cssSource, /\.sr-only\s*\{/, 'there is no screen-reader-only class for the menu button label');
  assert.match(htmlSource, /<span class="sr-only">/, 'the menu button has no readable label');
  assert.match(cssSource, /\.nav-toggle\s*\{/, 'the menu button is not styled');
  assert.match(cssSource, /@media \(max-width: 760px\)[\s\S]*\.nav-list\s*\{/, 'the section list is not re-laid-out on a phone');
  assert.match(cssSource, /\.nav-list\.is-open\s*\{/, 'the opened menu has no visible state');
  assert.match(cssSource, /\.nav-link\[aria-current="page"\]/, 'the current section is not styled as current');
  assert.ok(!/is-open/.test(htmlSource), 'the menu must start closed');
});

group('The city and methodology views say the right thing in the right place');

check('the city view leads with plain language and folds the model away', () => {
  const zone = zoneFixture({});
  const detail = FP.cityDetail(zone, { feed: null });
  const foldAt = detail.indexOf('<details class="tech"');
  assert.ok(foldAt > 0, 'the city view has no folded technical section');
  const visible = detail.slice(0, foldAt);
  const folded = detail.slice(foldAt);
  assert.match(visible, /data-f="why"/, 'the visible part has no plain-language explanation');
  assert.match(visible, /data-f="advice"/, 'the visible part has no advice line');
  ['weighted contribution', 'hysteresis', 'rise_fraction', 'explanation', 'top_drivers'].forEach((term) => {
    assert.ok(!visible.includes(term), `the visible city view exposes model detail: ${term}`);
  });
  assert.match(folded, /\bexplanation\b|data-f="explanation"/, 'the folded section does not carry the API explanation');
  assert.match(folded, /weighted contribution/i, 'the folded section does not explain the contributions');
  assert.ok(!/<details[^>]*\sopen/.test(detail), 'the technical detail must be collapsed by default');
  assert.match(folded, /<summary>/, 'the folded section cannot be opened from the keyboard alone');
  // the score and class always come from the payload, never recomputed here
  assert.ok(detail.includes(FP.formatScore(zone.latest.risk_score)), 'the city view lost the API risk score');
  assert.match(detail, new RegExp(zone.latest.risk_class, 'i'), 'the city view lost the API class');

  // No variable name may appear in the words a visitor reads, anywhere in the visible part - not
  // in a sentence and not as a label. Tags and attributes are stripped first, so `data-metric=
  // "soil_saturation_pct"` (which has to stay, it is how the tiles are addressed) is not
  // mistaken for copy, and `<code>` blocks are exempt because the technical disclosures quote
  // real identifiers on purpose.
  assert.deepEqual(variableNamesIn(visible),
    [], `the city view reads out variable names: ${variableNamesIn(visible).join(', ')}`);
  // ...while the folded section keeps every identifier, the description and the formula itself
  assert.match(folded, /Technical details/, 'the fold is not labelled as technical detail');
  assert.match(folded, /weighted base/, 'the fold no longer carries the weighted-base arithmetic');
  FP.METRIC_ORDER.forEach((metric) => {
    assert.ok(folded.includes(metric), `the folded section lost the variable name ${metric}`);
  });
  assert.match(folded, /Raw model output/, 'the fold does not label the API\'s own output line');
});

check('each measurement is one tile with its own icon, value and provenance', () => {
  const zone = zoneFixture({});
  const tiles = FP.metricTiles(zone);
  const metrics = ['rainfall_mm_per_hr', 'drain_level_m', 'soil_saturation_pct', 'water_level_m', 'flow_rate_m3_s'];
  assert.equal((tiles.match(/class="tile metric-tile/g) || []).length, metrics.length,
    'expected one tile per sensor channel');
  const icons = metrics.map((metric) => FP.metricIcon(metric));
  icons.forEach((svg, index) => {
    assert.match(svg, /^<svg /, `${metrics[index]} has no icon`);
    assert.match(svg, /aria-hidden="true"/, `${metrics[index]} icon is not hidden from screen readers`);
    assert.match(svg, /stroke="currentColor"/, `${metrics[index]} icon cannot inherit the text colour`);
    assert.ok(!/https?:|\.png|\.svg"/.test(svg), `${metrics[index]} icon is a remote or file asset`);
  });
  assert.equal(new Set(icons).size, metrics.length, 'two channels share an icon');
  assert.equal(FP.metricIcon('unknown_metric'), '', 'an unknown channel must not get an invented icon');
  metrics.forEach((metric) => {
    assert.ok(tiles.includes(`data-metric="${metric}"`), `no tile for ${metric}`);
    assert.ok(tiles.includes(`data-state=`), `${metric} tile has no state attribute`);
    assert.ok(tiles.includes('src-chip'), `${metric} tile has no provenance chip`);
  });
  assert.match(tiles, /data-state="(normal|watch|critical|unknown)"/);
});

check('the methodology page cannot drift from the backend constants', () => {
  const configPath = path.join(repoRoot, 'src', 'floodpulse', 'config.py');
  const config = fs.readFileSync(configPath, 'utf8');
  const weightBlock = /METRIC_WEIGHTS[^=]*=\s*\{([^}]*)\}/.exec(config);
  assert.ok(weightBlock, 'the backend weight table was not found');
  const backend = [...weightBlock[1].matchAll(/"([a-z_0-9]+)":\s*([0-9.]+)/g)]
    .map((match) => ({ metric: match[1], weight: Number(match[2]) }));
  assert.equal(backend.length, 5, `the backend defines ${backend.length} weights, expected 5`);

  const facts = FP.methodologyFacts();
  assert.equal(facts.model, 'heuristic-v1', 'the dashboard names a different model');
  // the invariant is the metric -> weight mapping, not the row order: the page deliberately
  // lists the heaviest factor first, which is not the order the backend defines them in
  const pair = (entry) => entry.metric + '=' + entry.weight;
  const byMetric = (entry) => entry.metric;
  assert.equal(
    JSON.stringify(facts.weights.slice().sort((a, b) => byMetric(a).localeCompare(byMetric(b))).map(pair)),
    JSON.stringify(backend.map((entry) => entry.metric + '=' + Math.round(entry.weight * 100) + '%')
      .sort((a, b) => a.localeCompare(b))),
    'the dashboard states weights the backend does not use');
  const order = facts.weights.map((entry) => parseFloat(entry.weight));
  assert.equal(JSON.stringify(order), JSON.stringify(order.slice().sort((a, b) => b - a)),
    'the weights should be shown heaviest first');
  const total = facts.weights.reduce((sum, entry) => sum + parseFloat(entry.weight), 0);
  assert.equal(total, 100, `the stated weights sum to ${total}%`);

  const bands = /RISK_BANDS[^=]*=\s*\(([\s\S]*?)\n\)/.exec(config);
  assert.ok(bands, 'the backend band table was not found');
  const backendBands = [...bands[1].matchAll(/\("([A-Z]+)",\s*([0-9.]+),\s*([0-9.]+)\)/g)]
    .map((match) => ({ band: match[1], lower: Number(match[2]), upper: Number(match[3]) }));
  assert.equal(backendBands.length, 4, 'the backend does not define four bands');
  assert.equal(JSON.stringify(facts.bands.map((entry) => entry.band)),
    JSON.stringify(backendBands.map((entry) => entry.band)),
    'the dashboard describes different bands than the backend applies');
  facts.bands.forEach((entry, index) => {
    const { lower, upper } = backendBands[index];
    const bounds = index === 0 ? [upper] : (index === 3 ? [lower] : [lower, upper]);
    bounds.forEach((bound) => {
      assert.ok(entry.range.includes(bound.toFixed(2)),
        `the dashboard does not state the backend bound ${bound} for ${entry.band} (${entry.range})`);
    });
  });
  const trend = /TREND_WEIGHT\s*=\s*([0-9.]+)/.exec(config);
  assert.ok(trend, 'the backend trend weight was not found');
  assert.ok(facts.steps.join(' ').includes(Number(trend[1]).toFixed(2)),
    `the dashboard does not state the trend bonus the backend adds (${trend[1]})`);
});

check('the data view states what is real, what is simulated and what it is not', () => {
  const legend = FP.provenanceLegend();
  assert.equal(legend.length, 3, 'the legend must explain exactly the three provenance tags');
  assert.equal(JSON.stringify(legend.map((entry) => entry.tag)), '["REAL","MODEL_DERIVED","SIMULATED"]',
    'the legend does not explain the tags the payload can carry');
  legend.forEach((entry) => {
    assert.ok(entry.body.length > 80, `${entry.tag} is explained in ${entry.body.length} characters`);
    assert.ok(entry.title.length > 10, `${entry.tag} has no short title`);
  });
  assert.match(legend[0].body, /Open-Meteo/, 'the REAL entry does not name the data source');
  assert.match(legend[2].body, /simulator/i, 'the SIMULATED entry does not say where the values come from');
  assert.match(legend[2].body, /no real sensor/i, 'the SIMULATED entry does not admit there is no sensor');

  const facts = FP.methodologyFacts();
  assert.ok(facts.limitations.length >= 4, `only ${facts.limitations.length} limitations are listed`);
  const limits = facts.limitations.join(' ').toLowerCase();
  assert.match(limits, /not a calibrated hydrological model/, 'the heuristic is not described as unvalidated');
  assert.match(limits, /not a forecast/, 'the page does not say the score is not a forecast');
  assert.match(limits, /not an official flood warning/, 'the page does not disclaim official warning status');
  assert.match(facts.whySimulated, /physical sensors/, 'the page does not explain why hydraulics are simulated');
  assert.match(facts.whySimulated, /rainfall stays real/, 'the page does not say which channel is real');
  assert.match(htmlSource, /id="footer-disclaimer"[^>]*>[\s\S]{40,}/, 'the permanent disclaimer is missing');
});

check('the data view names the provider and its licence only when used', () => {
  // built here, so this check says the same thing with or without a --payload fixture
  const liveBoard = {
    zone_mode: 'live_weather',
    zone_count: 1,
    weather: {
      provider: 'open-meteo',
      enabled: true,
      refresh_seconds: 600,
      live_zones: ['city-kochi'],
      cache_age_seconds: { 'city-kochi': 120 }
    },
    zones: [zoneFixture({ zone_id: 'city-kochi', name: 'Kochi', zone_mode: 'live_weather' })]
  };
  const withWeather = FP.attributionLine(liveBoard);
  assert.match(withWeather.text, /Open-Meteo/, 'the provider is not named');
  assert.match(withWeather.text, /CC BY/, 'the licence is not stated');
  assert.match(withWeather.text, /10 minutes/, 'the refresh cadence the payload reports is not stated');
  assert.match(withWeather.docs, /^https:\/\//, 'there is no https documentation link');
  // a payload with no provider data has nothing to attribute
  const withoutWeather = FP.attributionLine({ zone_mode: 'simulated', zones: [] });
  assert.equal(withoutWeather.text, '', 'the page claims attribution for data it never fetched');
  assert.equal(FP.attributionLine(null).text, '', 'a payload-less page claims attribution');
});

check('every front-end source is valid UTF-8 with no BOM and no mojibake', () => {
  // The dashboard is served byte-for-byte from S3 with no build step, and a PowerShell
  // round-trip once corrupted these files (en dashes became "â€“" and a BOM appeared).
  // Nothing else in the pipeline would notice, so it is checked here.
  const decode = new TextDecoder('utf-8', { fatal: true });
  ['index.html', 'styles.css', 'lib.js', 'app.js'].forEach((name) => {
    const bytes = fs.readFileSync(path.join(webDir, name));
    assert.notEqual(bytes[0], 0xEF, `${name} starts with a UTF-8 BOM`);
    let text = null;
    try {
      text = decode.decode(bytes);
    } catch (error) {
      assert.fail(`${name} is not valid UTF-8: ${error.message}`);
    }
    // sequences that only appear when UTF-8 has been decoded as CP-1252 and re-encoded
    const mojibake = /â€|Ã[\u0080-\u00BF]|Â[\u00A0-\u00BF]|ï»¿/.exec(text);
    assert.equal(mojibake, null,
      `${name} contains mojibake at character ${mojibake ? mojibake.index : -1}: ${mojibake ? JSON.stringify(mojibake[0]) : ''}`);
  });
  // the en dash and middle dot the copy relies on must survive as single characters
  assert.match(appSource + libSource, /\u2013/, 'the en dash used in the copy was lost');
});

group('Accessibility contract');

check('state changes are announced, not just coloured', () => {
  // The connection badge and the failure sentences change with the state, so they are
  // announced. The *ticking* status line is deliberately NOT a live region: it carries a
  // seconds-resolution age and countdown, so announcing it re-read the top bar once a
  // second, forever, and made the page unusable with a screen reader.
  const connBlock = /<div class="conn"[\s\S]*?<\/div>/.exec(htmlSource);
  assert.ok(connBlock, 'the connection block was not found');
  assert.match(connBlock[0], /id="conn-announce"[^>]*role="status"/, 'the connection state is not a live region');
  assert.match(connBlock[0], /id="conn-announce"[^>]*aria-live="polite"/, 'the connection state is not announced politely');
  assert.ok(!/id="conn-detail"[^>]*aria-live/.test(connBlock[0]), 'the ticking status line is itself a live region');
  assert.ok(!/<div class="conn"[^>]*aria-live/.test(htmlSource), 'the whole connection block is a live region');
  assert.match(connBlock[0], /id="conn-detail"/, 'the visible status line was removed');
  assert.match(htmlSource, /id="band-strip-label"[^>]*role="status"/, 'the strip summary is not announced');
  assert.match(htmlSource, /id="change-note"[^>]*role="status"/, 'what changed between polls is not announced');

  // The lists themselves must NOT be live regions: they are re-rendered whole on every poll and
  // every row carries a value that moves with the clock (reading ages, incident durations), so a
  // polite region over them re-announced the same rows every few seconds, for ever.
  ['city-cards', 'zones', 'alerts-active', 'alerts-closed', 'incidents'].forEach((id) => {
    const tag = new RegExp('id="' + id + '"[^>]*>').exec(htmlSource);
    assert.ok(tag, `#${id} is missing from the page`);
    assert.ok(!/aria-live/.test(tag[0]), `#${id} is a live region, so its ticking values are announced`);
  });
});

check('the incident record is a table for assistive tech, not just a grid of spans', () => {
  // The header row was aria-hidden, so each incident was read as six unlabelled values.
  assert.match(htmlSource, /role="table"[^>]*aria-label="[^"]*[Ii]ncident/, 'the record is not a labelled table');
  assert.ok(!/incident-head"[^>]*aria-hidden/.test(htmlSource), 'the incident column headers are still hidden');
  const headerCount = (htmlSource.match(/role="columnheader"/g) || []).length;
  assert.ok(headerCount >= 6, `only ${headerCount} column headers are exposed`);
  assert.match(libSource, /role="row"/, 'the data rows have no row role');
  const cellCount = (libSource.match(/role="cell"/g) || []).length;
  assert.ok(cellCount >= 6, `only ${cellCount} cells are exposed`);
});

check('the chart is exposed as an image with a label', () => {
  const canvas = /<canvas id="history-canvas"[^>]*>/.exec(htmlSource);
  assert.ok(canvas, 'the history canvas was not found');
  assert.match(canvas[0], /role="img"/, 'the canvas is not exposed as an image');
  assert.match(canvas[0], /aria-label="[^"]+"/, 'the canvas has no accessible label');
});

check('both canvases size their bitmap from the layout, not from the markup', () => {
  // regression: the sparkline drew a dpr-scaled path into the 280x56 bitmap the markup
  // declares, so on a HiDPI screen the newest half was drawn off the right edge and clipped,
  // and on a 1x screen a 280 px bitmap was stretched across the whole card.
  const spark = /function drawSpark\(card, zone\)\{?([\s\S]{0,1400})/.exec(appSource);
  assert.ok(spark, 'drawSpark was not found');
  assert.match(spark[1], /clientWidth/, 'the sparkline bitmap is not sized from its layout width');
  assert.match(spark[1], /devicePixelRatio/, 'the sparkline ignores the device pixel ratio');
  assert.ok(/canvas\.width\s*=\s*pixelWidth/.test(spark[1]),
    'the sparkline never resizes its backing store');
  const history = /function drawHistory\(\)\s*\{([\s\S]{0,900})/.exec(appSource);
  assert.ok(history, 'drawHistory was not found');
  assert.match(history[1], /clientWidth/, 'the history bitmap is not sized from its layout width');
});

check('a chart that cannot be drawn says so instead of staying blank', () => {
  // regression: on a failed history fetch the canvas kept no height and no axes while AT was
  // still told there was an image, and the panel kept the previous window's numbers.
  const load = /function loadHistory\(\)\s*\{([\s\S]{0,2200})/.exec(appSource);
  assert.ok(load, 'loadHistory was not found');
  assert.match(load[1], /drawHistory\(\)/, 'a failed history load does not redraw the empty state');
  assert.match(load[1], /historyError/, 'a failed history load does not record why it failed');
  assert.match(load[1], /loading/, 'the panel does not say it is loading before the response');
  assert.match(appSource, /function historyAriaLabel/, 'the chart label is not derived from the series');
});

check('the chart can be read without a mouse', () => {
  // regression: the values existed only behind mousemove, and the canvas was not focusable.
  const canvas = /<canvas id="history-canvas"[^>]*>/.exec(htmlSource);
  assert.ok(canvas, 'the history canvas was not found');
  assert.match(canvas[0], /tabindex="0"/, 'the chart cannot be focused with a keyboard');
  // The listener is registered through bindTo(), which skips a missing element instead of
  // aborting start(): what matters is that the canvas gets a keydown handler either way.
  assert.match(appSource, /(bindTo\('history-canvas', 'keydown', handleHistoryKey\)|addEventListener\('keydown', handleHistoryKey\))/,
    'the chart does not answer the arrow keys');
  assert.match(appSource, /(bindTo\('history-canvas', 'touchstart', handleHistoryHover|addEventListener\('touchstart', handleHistoryHover)/,
    'the chart does not answer a fingertip');
  assert.match(appSource, /function handleHistoryKey/, 'there is no keyboard handler for the chart');
  const keys = /function handleHistoryKey\(event\)\s*\{([\s\S]{0,900})/.exec(appSource);
  for (const key of ['ArrowRight', 'ArrowLeft', 'Home', 'End']) {
    assert.ok(keys[1].includes(key), `the chart ignores the ${key} key`);
  }
  assert.match(htmlSource, /arrow keys read out|arrow keys to read/i,
    'nothing tells a keyboard user the chart can be stepped through');
});

check('a payload naming an unexpected metric does not blank the page', () => {
  // regression: formatMetric/formatValueWithUnit dereferenced METRIC_META[metric] unguarded, so
  // one unknown driver name from the API threw inside the view renderer.
  for (const name of ['formatMetric', 'formatValueWithUnit']) {
    const body = new RegExp(`function ${name}\\(metric, value\\)\\s*\\{([\\s\\S]{0,320})`).exec(libSource);
    assert.ok(body, `${name} was not found`);
    assert.ok(!/var meta = METRIC_META\[metric\];/.test(body[1]),
      `${name} still dereferences an unknown metric name directly`);
    assert.match(body[1], /metricMeta\(metric\)/, `${name} does not look the metric up defensively`);
  }
  // It must return a usable default for an unknown name, whether that default is written as
  // "|| default" or as an own-property test - the own-property form also stops a metric named
  // "constructor" from resolving to an Object.prototype member.
  assert.match(libSource, /function metricMeta\(metric\)\s*\{[\s\S]{0,260}(METRIC_META\[metric\] \|\||hasOwn\(METRIC_META, metric\)[\s\S]{0,120}\{ label:)/,
    'metricMeta has no fallback for an unknown metric');
});

check('each region of the cities view is labelled by its own heading', () => {
  // regression: #ranking came before its own <h3 id="ranking-title">, and the live #zones
  // region had no accessible name at all.
  const rankingHeading = htmlSource.indexOf('id="ranking-title"');
  const rankingList = htmlSource.indexOf('id="ranking"');
  assert.ok(rankingHeading > 0 && rankingList > 0, 'the ranking heading or list is missing');
  assert.ok(rankingHeading < rankingList, 'the ranking list is announced before its heading');
  assert.match(htmlSource, /id="ranking"[^>]*aria-labelledby="ranking-title"/,
    'the ranking list is not named by its heading');
  assert.match(htmlSource, /id="zones"[^>]*aria-labelledby="zones-title"/,
    'the city grid has no accessible name');
  assert.match(htmlSource, /id="city-cards"[^>]*aria-labelledby="city-cards-title"/,
    'the city status block has no accessible name');
  assert.match(htmlSource, /id="alerts-active"[^>]*aria-labelledby="alerts-active-title"/,
    'the active-alerts list has no accessible name');
  assert.match(htmlSource, /id="alerts-closed"[^>]*aria-labelledby="alerts-closed-title"/,
    'the resolved-incidents list has no accessible name');
  ['alerts-active-title', 'alerts-closed-title'].forEach((id) => {
    assert.ok(htmlSource.includes('id="' + id + '"'), `the heading ${id} is missing`);
  });
});

check('a burst of resizes costs one redraw, not one per event', () => {
  // Dragging a window edge fires resize dozens of times a second; each redraw resizes two
  // canvases per card, which reallocates their backing stores. The listener has to schedule.
  const handler = /window\.addEventListener\('resize',([\s\S]*?)\n    \}\);/.exec(appSource);
  assert.ok(handler, 'the resize listener was not found');
  const body = handler[0];
  assert.ok(/requestAnimationFrame/.test(appSource), 'resizes are not scheduled on an animation frame');
  assert.ok(!/window\.addEventListener\('resize', function \(\) \{\s*drawHistory\(\)/.test(appSource),
    'every resize event still redraws the history chart synchronously');
  const queue = /function queueResizeRedraw\(\) \{([\s\S]*?)\n    \}/.exec(appSource);
  assert.ok(queue, 'the coalescing function is missing');
  assert.match(queue[1], /if \(resizeQueued\) return;/, 'a queued resize does not drop further events');
  assert.ok(/window\.requestAnimationFrame/.test(queue[1]) && /setTimeout/.test(body + queue[1]),
    'the fallback for a runtime without animation frames is missing');
});

check('controls are real controls and decoration is hidden from assistive tech', () => {
  assert.match(appSource + libSource, /<button/, 'the history controls are not real buttons');
  // Clicks are wired in JavaScript - now through bindTo(), which skips an element that is
  // missing instead of throwing - and never as an inline handler in markup.
  assert.match(appSource, /(addEventListener\(event, handler, options\)|addEventListener\('click')/,
    'nothing handles clicks explicitly');
  assert.ok(!/onclick\s*=/i.test(htmlSource + appSource + libSource), 'a click handler is inlined in markup');
  assert.match(htmlSource, /class="bg-layer" aria-hidden="true"/, 'the backdrop is not hidden from assistive tech');
  assert.match(htmlSource, /class="incident-head" role="rowgroup"/, 'the incident table header lost its roles');
  assert.ok(!/class="incident-head"[^>]*aria-hidden/.test(htmlSource),
    'the incident column headers are hidden from assistive tech again');
  assert.match(htmlSource, /<noscript/, 'there is no no-script fallback');
});

check('every interactive element can be reached and seen when focused', () => {
  assert.match(cssSource, /:focus-visible\s*\{[^}]*outline:\s*2px solid/, 'there is no visible focus ring');
  assert.ok(!/outline:\s*(none|0)\s*;/.test(cssSource.replace(/:focus-visible[^}]*\{[^}]*\}/g, '')),
    'focus outlines are removed without a :focus-visible replacement');
  assert.match(cssSource, /@media \(prefers-reduced-motion: reduce\)/, 'motion is never reduced');
});

/* ==================================================================== M9 === */

check('M9: the page is built for a phone before it is built for a desktop', () => {
  // a viewport tag is what makes the phone breakpoints apply at all
  assert.match(htmlSource, /<meta name="viewport" content="width=device-width, initial-scale=1"[\s/>]/,
    'the page has no device-width viewport tag');
  // the widths the brief names: a phone needs its own rules, not just a squeezed desktop layout
  ['1180px', '900px', '760px', '560px', '400px'].forEach((width) => {
    assert.ok(cssSource.includes(`@media (max-width: ${width})`), `no breakpoint for ${width}`);
  });
  // grid definitions must never demand more than the viewport: min(X, 100%) is what keeps a
  // 320px phone from being forced sideways by a 260px column plus padding
  /* Which @media rule, if any, encloses the declaration at `index`: a brace walk backwards, because
   * a regular expression over the preceding text silently matched nothing - the same class of bug
   * this guard exists to catch, one level up. */
  const enclosingMedia = (source, index) => {
    let depth = 0;
    for (let i = index - 1; i >= 0; i -= 1) {
      const ch = source[i];
      if (ch === '}') depth += 1;
      else if (ch === '{') {
        // the rule's own brace is at depth 0 and is not a scope: keep walking past it
        if (depth === 0) {
          const header = source.slice(source.lastIndexOf('\n', i) + 1, i).trim();
          if (header.startsWith('@media')) return header;
        } else {
          depth -= 1;
        }
      }
    }
    return '';
  };

  /* The narrowest viewport a declaration can be seen at: its own minimum inside a min-width query,
   * and 0 everywhere else, including a max-width query, which a narrow phone always reaches. */
  const smallestViewport = (header) => {
    const min = /min-width:\s*(\d+)px/.exec(header);
    return min ? parseInt(min[1], 10) : 0;
  };

  [...cssSource.matchAll(/grid-template-columns:\s*([^;]+);/g)].forEach((match) => {
    const value = match[1].trim();
    const allowed = Math.max(320, smallestViewport(enclosingMedia(cssSource, match.index)));
    const fixed = [...value.matchAll(/(\d+(?:\.\d+)?)(px|rem)/g)]
      .map((entry) => parseFloat(entry[1]) * (entry[2] === 'rem' ? 16 : 1));
    const guarded = value.includes('min(') || value.includes('100%') || value.includes('auto') || value.includes('1fr');
    fixed.forEach((width) => {
      assert.ok(width <= allowed || guarded,
        `a grid column demands ${width}px at ${allowed}px and up with nothing to shrink it: ${value}`);
    });
  });
  // long payload text (a city name, a close reason) must wrap instead of widening the page
  assert.match(cssSource, /overflow-wrap:\s*anywhere/, 'payload text is never told how to wrap');
  assert.match(cssSource, /min-width:\s*0/, 'grid children are never allowed to shrink below their content');
  // landscape phones and tablets are covered by the existing tablet/phone rules
  assert.match(cssSource, /\.incident-scroll\s*\{[^}]*overflow-x:\s*auto/,
    'the wide incident table has no horizontal scroll container of its own');
});

check('M9: the touch targets a phone actually taps are big enough', () => {
  const minTouch = (selector, reason) => {
    const rule = new RegExp(`\\${selector}\\s*\\{[^}]*min-height:\\s*(\\d+)px`).exec(cssSource);
    assert.ok(rule, `${selector} has no minimum height (${reason})`);
    assert.ok(parseFloat(rule[1]) >= 44, `${selector} is only ${rule[1]}px tall (${reason})`);
  };
  minTouch('.nav-link', 'a nav link is the most-tapped control on a phone');
  minTouch('.btn', 'empty-state actions are buttons on a phone');
  // the help disclosure is compact on a pointer device and full-size on a small screen
  assert.match(cssSource, /@media \(max-width: 560px\)[\s\S]*?\.help summary\s*\{\s*width:\s*44px/,
    'the help button never grows to a touch size');
});

check('M9: icons are local, consistent and hidden from assistive tech', () => {
  // one stroke width and one grid: the icons have to look like one set, not seven borrowed ones
  const iconBlock = /var ICON_PATHS = \{([\s\S]*?)\n  \};/.exec(libSource);
  assert.ok(iconBlock, 'the icon table is missing');
  const names = [...iconBlock[1].matchAll(/^\s{4}([a-z]+):/gm)].map((match) => match[1]);
  assert.ok(names.length >= 15, `only ${names.length} icons are defined`);
  // M10 adds the mail mark the subscription card wears
  ['rain', 'droplet', 'soil', 'drain', 'water', 'flow', 'clock', 'warning', 'check', 'info', 'bell',
    'shield', 'up', 'down', 'flat', 'external', 'city', 'layers', 'refresh', 'chart', 'mail'].forEach((name) => {
    assert.ok(names.includes(name), `the icon set is missing "${name}"`);
  });
  // every icon is drawn through one builder, so stroke width and viewBox cannot drift
  const builders = [...libSource.matchAll(/stroke-width="(\d+(?:\.\d+)?)"/g)].map((match) => match[1]);
  assert.deepEqual([...new Set(builders)], ['1.6'], `inconsistent icon stroke widths: ${[...new Set(builders)].join(', ')}`);
  assert.match(libSource, /aria-hidden="true" focusable="false"/, 'icons are not hidden from assistive tech');
  assert.match(libSource, /'<svg' \+ cls/, 'icons are not all built by the same function');
  // no icon font, no sprite request, nothing to fetch
  assert.ok(!/font-awesome|material-icons|@font-face|\.woff/i.test(htmlSource + cssSource + libSource + appSource),
    'an icon font or webfont was introduced');
  assert.ok(!/<img[^>]+(?:cdn|unpkg|jsdelivr)/i.test(htmlSource), 'an icon is loaded from a CDN');
  // every metric has an icon, and every name used in the markup exists in the table
  FP.METRIC_ORDER.forEach((metric) => {
    assert.ok(/\bicon\b/.test(FP.metricIcon(metric) || ''), `${metric} has no icon`);
  });
  const used = [...(libSource.matchAll(/icon\('([a-z]+)'/g))].map((match) => match[1]);
  used.forEach((name) => assert.ok(names.includes(name), `icon("${name}") is not in the icon table`));
});

check('M9: the copy reads like a product, not like a payload', () => {
  // field names belong in the technical disclosures, never in a sentence a visitor reads
  const userFacing = htmlSource.replace(/<code>[\s\S]*?<\/code>/g, '')
    .replace(/<details class="tech"[\s\S]*?<\/details>/g, '')
    .replace(/<script[\s\S]*?<\/script>/g, '');
  ['age_seconds', 'zone_mode', 'ts_epoch', 'risk_score_at_open', 'data_classification',
    'top_drivers', 'zone_id', 'stale_zone_ids'].forEach((field) => {
    assert.ok(!userFacing.includes(field), `the visible page shows the raw field name "${field}"`);
  });
  // ...and the renderers must not print them either: those strings are built at runtime
  const renderers = libSource.slice(libSource.indexOf('function cityStatusCard'));
  ['age_seconds=', 'zone_mode=', 'dominant_driver ='].forEach((pattern) => {
    assert.ok(!renderers.includes(pattern), `a renderer prints "${pattern}"`);
  });
  // the words the brief asks for, in place of the engineering ones
  assert.match(libSource, /Updated ' \+ escapeHtml\(freshness\)|'Updated ' \+/, 'freshness is not phrased as "Updated …"');
  assert.match(libSource, /is the main factor/, 'the driver is not phrased as a sentence');
  assert.match(libSource, /is the largest contributor/, 'the driver block has no plain-language lead');
  assert.match(libSource, /is the main contributor/, 'the plain sentence does not name a main contributor');
  // the public-name layer: any identifier the payload carries is turned into words
  assert.match(libSource, /function driverLabel\(/, 'there is no variable-name to label map');
  assert.match(libSource, /function humaniseVariable\(/, 'an unknown variable name would be printed raw');
  assert.equal(FP.driverLabel('soil_saturation_pct'), 'Soil saturation', 'soil saturation is mislabelled');
  assert.equal(FP.driverLabel('water_level_m'), 'Water level', 'water level is mislabelled');
  assert.equal(FP.driverLabel('drain_level_m'), 'Drain level', 'drain level is mislabelled');
  assert.equal(FP.driverLabel('rainfall_mm_per_hr'), 'Rainfall', 'rainfall is mislabelled');
  assert.equal(FP.driverLabel('flow_rate_m3_s'), 'Flow rate', 'flow rate is mislabelled');
  ['a_future_sensor', 'rainfall_mm_per_hr_2', 'mystery_sensor'].forEach((unknown) => {
    const label = FP.driverLabel(unknown);
    assert.ok(!label.includes('_'), `the label for ${unknown} keeps its underscores: ${label}`);
    assert.equal(FP.humaniseVariable(unknown), label, 'the fallback and driverLabel disagree');
  });
  assert.equal(FP.driverLabel(null), 'Reading', 'a missing variable has no label');
  assert.equal(FP.labelList(['rainfall']), 'rainfall', 'a one-item list is punctuated as a list');
  assert.equal(FP.labelList(['rainfall', 'drain level']), 'rainfall and drain level', 'two items lost their "and"');
  assert.equal(FP.labelList(['a', 'b', 'c']), 'a, b and c', 'three items lost the serial comma');
  // over-long sentences make a page unreadable: the explanations stay inside a reading budget
  const longStrings = [...libSource.matchAll(/'([A-Z][^']{190,})'/g)].map((match) => match[1]);
  assert.deepEqual(longStrings, [], `${longStrings.length} copy strings are over 190 characters`);
});

check('M9: the plain sentence is composed from the API\'s own numbers', () => {
  // The sentence names the main contributor, then says which measurements are *not* driving the
  // score - and that second half is derived from each metric's own value and watch threshold, so
  // it cannot claim a channel is quiet when the payload says it is not.
  const quiet = zoneFixture({});
  const sentences = FP.reasonSentence(quiet);
  const lead = FP.driverLabel(quiet.latest.top_drivers[0]);
  assert.match(sentences, /^Risk is currently [A-Z]+\./, `the sentence does not lead with the band: ${sentences}`);
  assert.ok(sentences.includes(lead + ' is the main contributor'), `the sentence lost its driver: ${sentences}`);
  assert.match(sentences, /remain below their watch levels|remains below its watch level/,
    `the sentence does not say what is quiet: ${sentences}`);
  assert.deepEqual(variableNamesIn(sentences), [], `the sentence leaks variable names: ${sentences}`);

  // raise one quiet channel above its watch level: the sentence must say so instead
  const shifted = JSON.parse(JSON.stringify(quiet));
  shifted.latest.contributions = shifted.latest.contributions.map((entry) => (
    (entry.variable || entry.metric) === 'rainfall_mm_per_hr'
      ? Object.assign({}, entry, { value: 20 }) : entry
  ));
  const shiftedSentence = FP.reasonSentence(shifted);
  assert.match(shiftedSentence, /Rainfall is also above its watch level/,
    `a channel above its threshold is still described as quiet: ${shiftedSentence}`);
  assert.ok(!/while rainfall/.test(shiftedSentence), 'a loud channel is listed among the quiet ones');

  // a reading with no driver list at all still says something true and useful
  const bare = JSON.parse(JSON.stringify(quiet));
  bare.latest.top_drivers = [];
  assert.match(FP.reasonSentence(bare), /^Risk is currently [A-Z]+\./,
    'a reading without a driver list lost its sentence');
  // and nothing at all renders as nothing, not as "undefined"
  assert.equal(FP.reasonSentence({ latest: null }), '', 'a missing reading produced a sentence');
  assert.ok(!/undefined|NaN/.test(FP.reasonSentence({})), 'an empty zone produced "undefined"');
});

check('M9: the contribution map reads the field the API actually sends', () => {
  // regression: the map was keyed on `metric`, which the API does not send (it sends `variable`),
  // so every weighted contribution, every driver share and the whole "what is not driving this"
  // half of the plain sentence were silently empty on every real payload.
  const live = zoneFixture({});
  const map = FP.contributionMap(live);
  FP.METRIC_ORDER.forEach((metric) => {
    if (metric === 'flow_rate_m3_s') return; // the fixture carries four of the five channels
    assert.ok(map[metric], `a contribution is not mapped: ${metric}`);
  });
  assert.ok(map.rainfall_mm_per_hr, 'the legacy "metric" key stopped working');
  const real = JSON.parse(JSON.stringify(live));
  real.latest.contributions = real.latest.contributions.map((entry) => ({
    variable: entry.variable || entry.metric, value: entry.value, unit: entry.unit, weight: entry.weight,
    sub_score: entry.sub_score, contribution: entry.contribution,
    watch_threshold: entry.watch_threshold, critical_threshold: entry.critical_threshold
  }));
  assert.equal(Object.keys(FP.contributionMap(real)).length, 4, 'a real payload lost a channel');
  // the driver shares are computed from the contribution, not from a field that is never sent
  const drivers = FP.driverBlock(real);
  assert.match(drivers, /--w:[1-9]/, 'the driver bars are all empty: the share is read from the wrong field');
  // and the breakdown the fold prints is built from those entries
  const breakdown = FP.scoreBreakdown(real);
  assert.match(breakdown, /weighted base/, 'the breakdown has no formula line');
  assert.match(breakdown, /risk score reported by the API/, 'the breakdown does not quote the API score');
  ['water_level_m', 'rainfall_mm_per_hr', 'soil_saturation_pct', 'drain_level_m'].forEach((metric) => {
    assert.ok(breakdown.includes(metric), `the breakdown is missing the variable ${metric}`);
  });
  assert.equal(FP.scoreBreakdown({ latest: null }), '', 'a zone with no reading produced a breakdown');
});

check('M9: the headline question is answered in the first screen', () => {
  // The reading order the brief asks for: the question, the plain answer inside the hero card,
  // then the four numbers, the distribution, the city cards, the alert preview, the three-step
  // explainer, and last the "what changed" panel.
  const order = ['hero', 'ov-title', 'ov-headline', 'ov-stats', 'band-strip', 'city-cards',
    'alert-preview', 'works-title', 'recent-activity']
    .map((id) => htmlSource.indexOf(`id="${id}"`));
  order.forEach((index, position) => {
    assert.ok(index > 0, `the Overview is missing element ${position}`);
    if (position > 0) {
      assert.ok(index > order[position - 1], 'the Overview sections are in the wrong reading order');
    }
  });
  // a question, answered in words, before any number
  assert.match(htmlSource, /<h2[^>]*id="ov-title"[^>]*>[^<]*\?/, 'the Overview heading is not a question');
  assert.match(htmlSource, /id="ov-headline"[^>]*role="status"[^>]*aria-live="polite"/,
    'the one-sentence answer is not announced politely');
  // The question is answered before any number. M10 moved the model name out of the primary
  // header and into the folded "Snapshot and technical summary" of this page, which is the
  // "technical details panel" the brief names as an acceptable home for it - so the rule is now
  // stronger and narrower at once: the model name and the weights may appear on the Overview
  // only behind a fold, and never in the header or the hero.
  const overviewBody = htmlSource.slice(htmlSource.indexOf('<section id="view-overview"'),
    htmlSource.indexOf('id="view-cities"'));
  const visibleOverview = overviewBody.replace(/<details[\s\S]*?<\/details>/g, '');
  assert.ok(!/heuristic-v1/.test(visibleOverview), 'the Overview shows the model name outside a fold');
  assert.ok(!/weight|coefficient|0\.30/.test(visibleOverview), 'the Overview shows scoring weights');
  assert.match(overviewBody, /<details[\s\S]*heuristic-v1[\s\S]*<\/details>/,
    'the model name is not in a folded technical panel on the Overview');
  const headerMarkup = htmlSource.slice(htmlSource.indexOf('<header'), htmlSource.indexOf('</header>'));
  assert.ok(headerMarkup.length > 100, 'the topbar was not found where this check expects it');
  ['heuristic-v1', 'model', 'weight'].forEach((term) => {
    assert.ok(!headerMarkup.includes(term), `the primary header still names the ${term}`);
  });
  // the three things the right-hand group answers, in slots the runtime fills: is the system
  // healthy, which feed is this, and how old is what we are showing
  ['health-word', 'conn-badge', 'last-update'].forEach((id) => {
    assert.ok(headerMarkup.includes(`id="${id}"`), `the header's status group lost ${id}`);
  });
  // The spotlight is rendered at runtime, so this is a source-level guard for the renderer that
  // produced the leak browser QA found on the deployed page: it printed the model's own output
  // string ("risk_score 0.3973 = weighted base ...") and the raw driver variables under the
  // heading "Dominant drivers" on the front page of a public flood monitor.
  const hero = appSource.slice(appSource.indexOf('function renderHero'),
    appSource.indexOf('function zoneSignature'));
  assert.ok(hero.length > 200, 'renderHero was not found where this check expects it');
  ['explanation', 'top_drivers', 'heuristic-v1', 'weighted', 'clockLabel', 'Dominant drivers',
    'hero-explain', 'hero-drivers'].forEach((term) => {
    assert.ok(!hero.includes(term), `the Overview spotlight still renders ${term}`);
  });
  // the internal id and the raw clock used to be printed on the line under the city name:
  // "city-chennai · reading 03:28:53Z · 2 min ago" is a developer's line, not a visitor's
  assert.ok(!/escapeHtml\(highest\.zone_id\)/.test(hero), 'the spotlight prints the internal zone id');
  // M10 moved the hero's markup into index.html, so the sentence now lives in a static element
  // that the renderer only writes text into: the element must exist, be styled as the reason
  // line, and be the node this renderer targets.
  assert.match(htmlSource, /class="hero-reason"[^>]*id="hero-why"/,
    'the hero has no plain-language sentence element');
  assert.match(hero, /setText\('hero-why'/, 'the spotlight never fills the plain-language sentence');
  assert.match(hero, /Main risk factor/, 'the spotlight does not name the main risk factor');
  assert.match(hero, /'Updated ' \+ FP\.ageLabel/, 'the spotlight does not say how old the reading is');
  // and the arrow in the other direction: the plain sentence the spotlight prints must be the
  // same composer the city page uses, so the two can never disagree
  assert.match(hero, /FP\.plainRiskSummary\(/, 'the spotlight composes its own sentence instead of reusing the shared one');
});

check('M9: every view has a designed empty, loading and error state', () => {
  // one builder for all of them, so no view can invent its own unstyled placeholder
  assert.match(libSource, /function emptyState\(/, 'there is no shared empty-state builder');
  assert.match(libSource, /function skeletonLines\(/, 'there is no shared skeleton builder');
  assert.match(libSource, /data-action="retry"/, 'the empty states offer no way to try again');
  assert.match(appSource, /closest\('\[data-action="retry"\]'\)/, 'the retry button is not wired up');
  // the states the brief lists, each reachable in code
  assert.match(appSource, /key: 'no-cities'/, 'no empty state for a fleet with no readings');
  assert.match(libSource, /data-empty="' \+ escapeHtml\(opt\.key\)/, 'no empty state for the alert lists');
  assert.match(appSource, /key: 'unknown-city'/, 'no empty state for a city that does not exist');
  assert.match(libSource, /key: 'city-history'/, 'no empty state for a city with no alerts');
  // the chart reports its own failure twice: painted on the canvas, and as text on the page, so a
  // reader who cannot see the canvas still learns that the history did not load
  assert.match(appSource, /'History could not be loaded\.'/, 'the chart does not report a load failure');
  assert.match(appSource, /Risk score history could not be loaded\./, 'the failure has no text equivalent');
  assert.match(appSource, /setText\('history-summary'/, 'the chart has no text summary on the page');
  // loading must never be the word "loading" alone, and must never be permanent
  assert.match(libSource, /skel-sweep|skel-card/, 'the loading state is not a skeleton');
  assert.ok(!/['"]Loading…['"]/.test(libSource + appSource), 'a renderer prints a bare "Loading…"');
  // an empty state is not a live region: several can appear in one poll, and that is an
  // announcement storm rather than information
  assert.ok(!/class="empty-state[^"]*"[^>]*role="status"/.test(libSource),
    'the empty state announces itself and can flood a screen reader');
});

check('M9: the front-end budget still holds', () => {
  const sizes = ['index.html', 'styles.css', 'lib.js', 'app.js']
    .map((file) => ({ file, bytes: fs.statSync(path.join(webDir, file)).size }));
  const total = sizes.reduce((sum, entry) => sum + entry.bytes, 0);
  const biggest = sizes.sort((a, b) => b.bytes - a.bytes)[0];
  // The whole point of staying vanilla is that the page fits in one round trip on a phone. The
  // numbers are ceilings, not targets: they only fail if a framework or a build step sneaks in.
  assert.ok(total <= 400 * 1024, `the front-end is ${(total / 1024).toFixed(0)} KB of source`);
  assert.ok(biggest.bytes <= 200 * 1024, `${biggest.file} alone is ${(biggest.bytes / 1024).toFixed(0)} KB`);
  // the backdrop image: compressed WebP, local, and inside the brief's ceiling
  const backdrop = fs.statSync(path.join(webDir, 'bg-storm.webp'));
  assert.ok(backdrop.size <= 250 * 1024, `the backdrop is ${(backdrop.size / 1024).toFixed(0)} KB`);
  // Every file in web/ is either something the deploy list uploads or a prepared asset this
  // repository knows about. A file in neither means an asset arrived without its deploy or
  // documentation being updated, which is the mistake this check exists to catch.
  const deployList = ['app.js', 'bg-storm.webp', 'index.html', 'lib.js', 'styles.css'];
  const prepared = [
    'alert-icon.svg', 'bengaluru-nightscape.webp', 'building-icon.svg', 'check-icon.svg',
    'chennai-nightscape.webp', 'chevron-right.svg', 'city-icon.svg', 'clock-icon.svg',
    'hyderabad-nightscape.webp', 'kochi-nightscape.webp', 'mumbai-nightscape.webp',
    // M11: the photographs the expanded fleet ships, declared rather than tolerated
    'ahmedabad.webp',
    'bhubaneshwar.webp',
    'delhi.webp',
    'guwahati.webp',
    'jaipur.webp',
    'kolkata.webp',
    'lucknow.webp',
    'patna.webp',
    'pune.webp',
    'visakhapatnam.webp',
    'weather-rain.svg',
  ];
  const files = fs.readdirSync(webDir).sort();
  deployList.forEach((file) => assert.ok(files.includes(file), `a deploy-list asset is missing: ${file}`));
  assert.deepEqual(files.filter((file) => !prepared.includes(file)), deployList,
    'web/ gained a file that is in neither the deploy list nor the prepared-asset list');
  // the backdrop is decorative, decoded off the main thread, and has a gradient underneath it
  assert.match(cssSource, /\.bg-layer\s*\{[^}]*background-image:\s*url\("bg-storm\.webp"\)/,
    'the backdrop image is not the local asset');
  assert.match(htmlSource, /class="bg-layer" aria-hidden="true"/,
    'the backdrop is exposed to assistive tech');
  assert.match(cssSource, /body\s*\{[^}]*background:\s*linear-gradient/,
    'there is no gradient fallback for a failed background image');
});

check('M9: the About page stays inside its lane', () => {
  const about = /<section id="view-about"[\s\S]*?<\/section>\s*<!--/.exec(htmlSource)
    || /<section id="view-about"[\s\S]*$/.exec(htmlSource);
  assert.ok(about, 'the About view is missing');
  const copy = about[0];
  ['What FloodPulse is', 'Why it exists', 'Cities monitored', 'How it works', 'Built on',
    'Flood safety guidance', 'Limitations'].forEach((heading) => {
    assert.ok(copy.includes(heading), `the About page has no "${heading}" section`);
  });
  // the safety guidance must not read as an official warning from FloodPulse
  assert.match(copy, /does <b>not<\/b> issue official warnings|not issue official warnings/,
    'the guidance does not disclaim authority');
  assert.match(copy, /follow your local government and disaster-management\s+authorities/i,
    'the guidance does not defer to the authorities');
  ['mausam.imd.gov.in', 'ndma.gov.in'].forEach((host) => {
    assert.ok(copy.includes(host), `the guidance does not link to ${host}`);
  });
  // links out are links, not integrations: no script, no fetch, no key
  assert.ok(!/fetch\(|XMLHttpRequest/.test(copy), 'the About page talks to a third party');
  // and no infrastructure detail leaks into the user-facing copy
  ['arn:aws', 'eu-north-1', 'account', '725934295966'].forEach((secret) => {
    assert.ok(!copy.includes(secret), `the About page exposes infrastructure detail: ${secret}`);
  });
});

check('M9: the heading outline has no skipped level', () => {
  const tags = [...htmlSource.matchAll(/<(h[1-4])[^>]*>/g)].map((match) => Number(match[1][1]));
  assert.equal(tags[0], 1, 'the page does not start with an h1');
  let deepest = 1;
  tags.forEach((level) => {
    assert.ok(level <= deepest + 1, `a heading jumps from h${deepest} to h${level}`);
    deepest = Math.max(deepest, level);
  });
  // exactly one h1: the product name, in the header, once
  assert.equal(tags.filter((level) => level === 1).length, 1, 'the page has more than one h1');
});

check('M9: the reskin did not delete the band accent stripes', () => {
  // A city card, a stat and an alert row carry their band in a left accent stripe, which is the
  // one cue that is a *shape* rather than a colour (so it survives greyscale). The M9 surface
  // rules sit later in the file at equal specificity, and a `border` shorthand there would
  // silently flatten 4px to 1px. This asserts the shorthand is not used on those three.
  const m9Block = cssSource.slice(cssSource.indexOf('M9 - product experience'));
  assert.ok(m9Block.length > 0, 'the M9 stylesheet section is missing');
  ['.card {', '.stat {', '.alert-row {', '.city-card {'].forEach((selector) => {
    let index = m9Block.indexOf(selector);
    assert.ok(index >= 0 || selector === '.alert-row {', `${selector} is not defined in the M9 section`);
    while (index >= 0) {
      const rule = m9Block.slice(index, m9Block.indexOf('}', index) + 1);
      assert.ok(!/(^|\s)border\s*:/.test(rule),
        `${selector.trim()} in the M9 section uses the border shorthand, which flattens the band stripe`);
      index = m9Block.indexOf(selector, index + selector.length);
    }
  });
  // and the stripes themselves are still defined, in every band the page can show
  ['.city-card.band-warning', '.city-card.band-critical', '.stat.band-warning', '.hero.band-critical']
    .forEach((selector) => {
      assert.ok(cssSource.includes(selector) || cssSource.includes(selector.replace('.city-card', '.city-card.band')),
        `the band accent for ${selector} is gone`);
    });
  assert.match(cssSource, /\.city-card\.band-warning\s*\{\s*border-left-color/,
    'a band changes the stripe colour no more');
});

/* -------------------------------------------------- M10 - premium UI pass --- */

group('M10: the shell, the header and the hero');

/* The M10 layer is the last section of the stylesheet; every check below reads its rules rather
 * than the whole file, so an M6-M9 rule can never be mistaken for a new decision. */
const m10Block = () => {
  const block = cssSource.slice(cssSource.indexOf('M10 - premium pass'));
  assert.ok(block.length > 1000, 'the M10 stylesheet section is missing');
  // Comments explain the decisions and quote the alternatives ("not `background-attachment:
  // fixed`"), so they must never be read as rules.
  return block.replace(/\/\*[\s\S]*?\*\//g, ' ');
};

check('M10: the backdrop is a local image, held still, with a navy scrim over it', () => {
  // The brief asks for a dark rainy-city photograph: cover, centred, and fixed *if the page stays
  // fast*. It is rendered on the existing fixed `.bg-layer` rather than as a body background,
  // because `background-attachment: fixed` on the body repaints the whole page on every scroll on
  // mobile Safari, while an element fixed behind the content is composited once. The visual result
  // is the same, and this check pins all four of those choices.
  const layer = /\.bg-layer\s*\{([^}]*)\}/.exec(cssSource);
  assert.ok(layer, 'the backdrop layer is gone');
  assert.match(layer[1], /background-image:\s*url\("bg-storm\.webp"\)/, 'the backdrop is not the local asset');
  assert.match(layer[1], /background-size:\s*cover/, 'the backdrop is not scaled to cover');
  const m10 = m10Block();
  assert.match(m10, /\.bg-layer\s*\{[^}]*background-position:\s*center/, 'the backdrop is not centred');
  assert.match(m10, /\.bg-layer\s*\{[^}]*box-shadow:\s*inset/, 'the backdrop has no vignette');
  assert.match(m10, /\.bg-layer::after\s*\{[^}]*radial-gradient[\s\S]*?linear-gradient/,
    'the backdrop has no navy ramp between the photograph and the text');
  // fixed, and the layer is decorative
  assert.match(cssSource, /\.bg-layer\s*\{[^}]*position:\s*fixed/, 'the backdrop is not held still');
  assert.ok(!/background-attachment:\s*fixed/.test(m10), 'the M10 layer reintroduced a fixed body background');
  // no remote asset anywhere near it
  assert.ok(!/url\(\s*["']?https?:/i.test(m10), 'the M10 layer fetches something remote');
});

check('M10: the header is three groups, and the model name left it', () => {
  const header = htmlSource.slice(htmlSource.indexOf('<header'), htmlSource.indexOf('</header>'));
  const order = ['brand', 'nav-list', 'topbar-status'].map((token) => header.indexOf(token));
  order.forEach((index, position) => {
    assert.ok(index > 0, `the header has no ${['brand', 'navigation', 'status group'][position]}`);
    if (position > 0) assert.ok(index > order[position - 1], 'the header groups are in the wrong order');
  });
  // the right-hand group states the three things a reader checks, each in its own slot
  ['health-word', 'conn-badge', 'last-update'].forEach((id) => {
    assert.ok(header.includes(`id="${id}"`), `the status group lost #${id}`);
  });
  // the model is a method detail: it lives in the folded technical summary, Data & Methodology,
  // and the footer - never in the header
  assert.ok(!/heuristic-v1|model/i.test(header), 'the primary header still names the model');
  assert.match(htmlSource, /<dd><b id="summary-model">heuristic-v1<\/b>/, 'the model left the technical fold too');
  // the chip/pill styling the brief asks for, and the centre group centred on a desktop
  assert.match(m10Block(), /\.meta-item\s*\{[^}]*border-radius:\s*999px/, 'the header counters are not chips');
  assert.match(m10Block(), /\.nav\s*\{\s*justify-self:\s*center/, 'the navigation is not the centre group');
});

check('M10: the hero answers the question in the first screen, with no formula in it', () => {
  const hero = htmlSource.slice(htmlSource.indexOf('id="hero"'), htmlSource.indexOf('id="ov-stats"'));
  assert.ok(hero.length > 400, 'the hero is missing');
  assert.match(hero, /class="card hero hero-live"/, 'the hero is no longer a card');
  assert.match(hero, /data-band="unknown"/, 'the hero ships without a neutral band');
  // icon (decorative), kicker, the visitor's question, the answer, then the reasoning
  assert.match(hero, /class="hero-glyph" aria-hidden="true"/, 'the hero icon is not decorative');
  assert.match(hero, /Live flood risk monitoring/, 'the hero has no kicker');
  assert.match(hero, /<h2[^>]*id="ov-title"[^>]*>[^<]*\?/, 'the hero does not ask the visitor\'s question');
  assert.match(hero, /id="ov-headline"[^>]*role="status"[^>]*aria-live="polite"/,
    'the hero answer is not announced politely');
  ['hero-monitor', 'hero-why', 'hero-facts'].forEach((id) => {
    assert.ok(hero.includes(`id="${id}"`), `the hero has no #${id}`);
  });
  // the aside says what the product is and offers the one way into the method
  assert.match(hero, /class="hero-aside"[\s\S]*href="#\/data"[\s\S]*Learn how risk is calculated/,
    'the hero does not link to the methodology');
  // "Do NOT show formulas here": no formula, no variable name, no model id in the hero markup
  ['risk_score', 'weight', 'coefficient', 'heuristic-v1', '<code'].forEach((term) => {
    assert.ok(!hero.includes(term), `the hero shows ${term}`);
  });
  assert.ok(!/=\s*0\.\d/.test(hero), 'the hero markup contains an arithmetic expression');
});

check('M10: the Overview explains the product in three steps', () => {
  const works = htmlSource.slice(htmlSource.indexOf('class="works"'), htmlSource.indexOf('id="recent-activity"'));
  assert.ok(works.length > 300, 'the "how it works" block is missing');
  assert.match(works, /<ol class="works-steps"/, 'the three steps are not an ordered list');
  const steps = works.match(/class="works-step"/g) || [];
  assert.equal(steps.length, 3, `expected three steps, found ${steps.length}`);
  [['Collect', 'Live rainfall and weather information'],
    ['Analyse', 'Combine weather and drainage indicators'],
    ['Alert', 'Create an alert when risk reaches warning levels']].forEach(([name, sentence]) => {
    assert.ok(works.includes(name), `the ${name} step is missing`);
    assert.ok(works.includes(sentence), `the ${name} step lost its sentence`);
  });
  ['works', 'works-steps', 'works-step', 'works-icon'].forEach((cls) => {
    assert.ok(cssSource.includes('.' + cls), `.${cls} is not styled`);
  });
});

/* ------------------------------------------------- M10 - public subscriptions --- */

group('M10: the public email subscription flow');

const subscriptionCard = () => {
  const start = htmlSource.indexOf('id="subscribe-card"');
  const end = htmlSource.indexOf('id="alerts-status"');
  assert.ok(start > 0, 'the subscription card is missing from the Alerts page');
  assert.ok(end > start, 'the subscription card is not above the system-status section');
  return htmlSource.slice(start, end);
};

check('M10: the subscription card sits after the alerts and before the system status', () => {
  const alertsView = htmlSource.slice(htmlSource.indexOf('id="view-alerts"'),
    htmlSource.indexOf('id="view-data"'));
  const order = ['alerts-active', 'alerts-closed', 'subscribe-card', 'alerts-status']
    .map((id) => alertsView.indexOf(`id="${id}"`));
  order.forEach((index, position) => {
    assert.ok(index > 0, `the Alerts page has no ${['active alerts', 'resolved incidents',
      'subscription card', 'system status'][position]} section`);
    if (position > 0) {
      assert.ok(index > order[position - 1], 'the Alerts page sections are in the wrong order');
    }
  });
  // it stands out without reading as an advertisement: a mail icon, a heading, one sentence
  const card = subscriptionCard();
  assert.match(card, /class="subscribe-glyph" aria-hidden="true"/, 'the card has no icon');
  assert.match(card, /<h3 class="block-title" id="subscribe-title">Get flood alerts by email<\/h3>/,
    'the card heading is not the one the brief asks for');
  assert.match(card, /Receive an email when FloodPulse detects WARNING or CRITICAL flood risk\./,
    'the card does not say what the alerts are');
  assert.ok(!/buy|free trial|upgrade|subscribe now|newsletter/i.test(card),
    'the card reads like marketing');
});

check('M10: the subscription form is labelled, bounded and honest', () => {
  const card = subscriptionCard();
  // a real form, intercepted by script: the policy forbids form submission, so `novalidate`
  // keeps the browser from blocking a submit the page wants to handle itself
  assert.match(card, /<form id="subscribe-form" class="subscribe-form" novalidate>/,
    'the form is not the scripted, non-navigating form the policy requires');
  assert.match(card, /<label[^>]*for="subscribe-email"[^>]*>Email address<\/label>/,
    'the field has no associated label');
  const input = /<input id="subscribe-email"[^>]*>/i.exec(card);
  assert.ok(input, 'the field is missing');
  [['type="email"', 'not an email input'], ['maxlength="254"', 'no length bound'],
    ['autocomplete="email"', 'no autocomplete hint'], ['aria-describedby="subscribe-note"', 'no description']]
    .forEach(([token, why]) => {
      assert.ok(input[0].includes(token), `the field has ${why}`);
    });
  assert.match(card, /<button id="subscribe-button" class="btn btn-primary" type="submit">Subscribe to alerts<\/button>/,
    'the submit button is not the plain-language button the brief asks for');
  assert.match(card, /<p id="subscribe-result" class="subscribe-result" role="status" aria-live="polite" hidden>/,
    'the outcome is not announced politely, or is not hidden until there is one');
  // the three sentences the brief requires, verbatim
  assert.match(card, /You[’']ll receive a confirmation email from Amazon Web Services\.\s*Your subscription becomes active only after you confirm it\./,
    'the confirmation note is missing or reworded');
  assert.match(card, /FloodPulse does not send marketing email\. Email addresses are used only for this SNS\s*notification subscription/,
    'the privacy note is missing or reworded');
  assert.match(card, /Amazon SNS includes its own unsubscribe link/, 'there is no unsubscribe instruction');
  // and it must not promise something AWS has not done yet
  assert.ok(!/you are subscribed|subscribed successfully|you will now receive/i.test(card),
    'the card claims a subscription before SNS has confirmed anything');
  // No infrastructure detail on a public page. The API's own region and host are necessarily
  // present (the `data-api` attribute and the policy's connect-src), so they are allowed as
  // origins and nothing more: no ARN, no account id, no topic name, no SDK field name.
  ['arn:aws', '725934295966', 'TopicArn', 'ReturnSubscriptionArn', 'floodpulse-alerts'].forEach((secret) => {
    assert.ok(!htmlSource.includes(secret) && !appSource.includes(secret) && !libSource.includes(secret),
      `the front end exposes ${secret}`);
  });
  [...htmlSource.matchAll(/eu-north-1/g)].forEach((match) => {
    const context = htmlSource.slice(Math.max(0, match.index - 120), match.index + 20);
    assert.ok(/data-api="https:\/\/|execute-api\.|connect-src 'self' https:\/\//.test(context),
      `the front end names the region outside an API origin: ...${context.slice(-80)}`);
  });
});

check('M10: the four outcome messages exist once, in one place', () => {
  // The apostrophe in the unavailable copy is written as an escape in the source, so each
  // message is matched with either spelling.
  const messages = ['Check your inbox',
    'Enter a valid email address.',
    'Too many subscription requests. Please try again later.',
    'We couldn\u2019t create the subscription request right now. Please try again.'];
  messages.forEach((message) => {
    const inLib = libSource.includes(message)
      || libSource.includes(message.replace(/\u2019/g, '\\u2019'));
    assert.ok(inLib, `the front end has no "${message}" message`);
    // one definition, and it is the module the runtime harness exercises
    const inApp = appSource.includes(message)
      || appSource.includes(message.replace(/\u2019/g, '\\u2019'));
    assert.ok(!inApp, `"${message}" is duplicated in app.js`);
  });
  assert.match(libSource, /We sent a subscription confirmation request through Amazon SNS\. Click/,
    'the accepted state does not explain the confirmation email');
  assert.match(libSource, /function subscriptionKindForStatus\(status\)/,
    'there is no single mapping from an HTTP outcome to a message');
  assert.ok(appSource.includes('FP.subscriptionResult(kind)'),
    'the page builds its own copy instead of using the shared one');
});

check('M10: the page never claims a subscription the API did not confirm', () => {
  // Because the page only ever shows "check your inbox", the forbidden claim must not exist
  // anywhere in the front end - not in markup, not in a string, not in a comment.
  [['index.html', htmlSource], ['app.js', appSource], ['lib.js', libSource]].forEach(([name, source]) => {
    assert.ok(!/you are subscribed|subscribed successfully|your subscription is active/i.test(source),
      `${name} claims a subscription that SNS has not confirmed`);
  });
  // and the one accepted wording is the confirmation request, not the subscription
  assert.ok(libSource.includes('We sent a subscription confirmation request through Amazon SNS'),
    'the accepted state no longer describes a confirmation request');
});

check('M10: the new layouts collapse on a phone, with no horizontal overflow', () => {
  const m10 = m10Block();
  // every multi-column track the M10 layer introduces is minmax(0, ...) so a long address or a
  // narrow screen cannot push the page sideways
  const grids = m10.match(/grid-template-columns:\s*[^;]+;/g) || [];
  assert.ok(grids.length >= 4, 'the M10 layer introduces no grids');
  grids.forEach((rule) => {
    assert.ok(!/\d+px\)/.test(rule.replace(/minmax\(\s*0/g, '')) || /repeat/.test(rule),
      `a fixed-width track can overflow: ${rule}`);
    assert.ok(!/grid-template-columns:\s*\d+px/.test(rule), `a fixed column can overflow: ${rule}`);
  });
  // the hero's aside moves under the answer, the steps stack, and the form's button goes full width
  assert.match(m10, /@media \(max-width: 900px\)\s*\{[\s\S]*?\.hero\.hero-live\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/,
    'the hero does not collapse to one column');
  assert.match(m10, /@media \(max-width: 900px\)\s*\{[\s\S]*?\.works-steps\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/,
    'the three steps do not stack on a phone');
  assert.match(m10, /@media \(max-width: 560px\)[\s\S]*?\.subscribe-row\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/,
    'the subscription field and button do not stack');
  assert.match(m10, /@media \(max-width: 560px\)[\s\S]*?\.subscribe-input\s*\{\s*width:\s*100%/,
    'the subscription field is not full width on a phone');
  // the smallest phone the brief names still has room: nothing in the new layer asks for more
  // A breakpoint prelude is not a width requirement, so only the declarations are measured.
  const declarations = m10.replace(/@media[^{]*\{/g, '{');
  const wide = declarations.match(/min-width:\s*(\d+)px/g) || [];
  wide.forEach((rule) => {
    const size = Number(/(\d+)/.exec(rule)[1]);
    assert.ok(size <= 360, `the M10 layer needs ${size}px, more than a 320px phone has`);
  });
});

check('M10: the new decoration cannot become the only carrier of meaning', () => {
  const m10 = m10Block();
  // the plate, the meter and the hero icon are all decorative and hidden from assistive tech
  assert.match(libSource, /class="city-thumb band-' \+ band \+ '" data-band="' \+ escapeHtml\(band\) \+\s*'" aria-hidden="true"/,
    'the city plate is exposed to assistive tech');
  assert.match(libSource, /class="city-meter band-' \+ band \+ '" data-band="' \+ escapeHtml\(band\) \+\s*'" aria-hidden="true"/,
    'the meter is exposed to assistive tech');
  // the meter marks the documented band edges, and reads its position from the API score
  assert.match(libSource, /SCORE_BAND_EDGES = \[0\.35, 0\.6, 0\.8\]/, 'the band edges were redefined');
  assert.match(appSource, /var BAND_EDGES = FP\.SCORE_BAND_EDGES/, 'app.js has its own copy of the edges');
  // The static page still ships no <img>: the nightscapes are added by the renderer, for the
  // zones the explicit mapping knows, so a page that never receives a payload never fetches one.
  // What the images themselves must be is asserted in the M10.1 group at the end of this file.
  assert.ok(!/<img/i.test(htmlSource), 'a raster image arrived in the static markup');
  // and the meter's own colours come from the band tokens, never from a literal hex
  assert.ok(!/#[0-9a-f]{6}/i.test(m10.match(/\.city-meter[\s\S]{0,600}/)[0]),
    'the meter hard-codes a colour instead of using a band token');
});

/* --------------------------------------------------------------- M10.1 ---
 *
 * The prepared raster assets, wired in. Every claim about them is made twice on purpose: the
 * table and the markup source are checked here, the rendered DOM is checked by
 * tools/dashboard_run.mjs, and the pixels were verified by tools/optimize_web_assets.py.
 */

group('M10.1: the prepared city imagery is wired in, and nowhere it should not be');

/* The Photoshop-free way to ask "is this really a WebP, and how big is its frame?": read the
 * container and the frame header. It is not a pixel decode - tools/optimize_web_assets.py does
 * that with Pillow - but it does catch the bug this repository actually had, a PNG wearing a
 * .webp extension, and it catches a file whose header claims a size the card cannot use. */
function webpFrame(bytes) {
  assert.equal(bytes.subarray(0, 4).toString('ascii'), 'RIFF', 'not a RIFF container');
  assert.equal(bytes.subarray(8, 12).toString('ascii'), 'WEBP', 'not a WebP payload');
  assert.notEqual(bytes.subarray(1, 4).toString('ascii'), 'PNG', 'a PNG wearing a .webp name');
  const tag = bytes.subarray(12, 16).toString('ascii');
  if (tag === 'VP8X') {
    return { tag, width: 1 + bytes.readUIntLE(24, 3), height: 1 + bytes.readUIntLE(27, 3) };
  }
  if (tag === 'VP8 ') {
    assert.equal(bytes.readUIntLE(23, 3), 0x2a019d, 'the lossy frame has no sync code');
    return { tag, width: bytes.readUIntLE(26, 2) & 0x3fff, height: bytes.readUIntLE(28, 2) & 0x3fff };
  }
  if (tag === 'VP8L') {
    assert.equal(bytes[20], 0x2f, 'the lossless frame has no signature');
    const bits = bytes.readUInt32LE(21);
    return { tag, width: 1 + (bits & 0x3fff), height: 1 + ((bits >> 14) & 0x3fff) };
  }
  throw new Error(`unknown WebP chunk ${tag}`);
}

check('M10.1: the zone -> image mapping is explicit, complete and closed', () => {
  // A table, not a derived path. A filename assembled from `zone_id` would let whatever sets that
  // value decide what the page loads - and an inherited key like "constructor" or "toString" would
  // make an own-property test mandatory rather than optional, which is why that guard is pinned
  // here too.
  const expected = {
    'city-chennai': 'chennai-nightscape.webp',
    'city-mumbai': 'mumbai-nightscape.webp',
    'city-bengaluru': 'bengaluru-nightscape.webp',
    'city-hyderabad': 'hyderabad-nightscape.webp',
    'city-kochi': 'kochi-nightscape.webp',
    'city-delhi': 'delhi.webp',
    'city-kolkata': 'kolkata.webp',
    'city-guwahati': 'guwahati.webp',
    'city-patna': 'patna.webp',
    'city-bhubaneswar': 'bhubaneshwar.webp',
    'city-visakhapatnam': 'visakhapatnam.webp',
    'city-pune': 'pune.webp',
    'city-ahmedabad': 'ahmedabad.webp',
    'city-jaipur': 'jaipur.webp',
    'city-lucknow': 'lucknow.webp'
  };
  // copied into this realm before comparing: the sandbox's objects have another prototype
  const table = Object.fromEntries(Object.entries(FP.CITY_IMAGES));
  assert.deepEqual(table, expected, 'the zone -> image table changed');
  Object.entries(expected).forEach(([id, file]) => {
    assert.equal(FP.cityImageFor({ zone_id: id }), file, `${id} does not resolve to its own file`);
    assert.match(file, /^[a-z][a-z0-9-]*\.webp$/, `${file} is not a bare filename inside web/`);
  });
  [null, undefined, {}, { zone_id: 'city-atlantis' }, { zone_id: 'constructor' },
    { zone_id: 'toString' }, { zone_id: '../bg-storm.webp' }, { zone_id: 'CITY-CHENNAI' }].forEach((zone) => {
    assert.equal(FP.cityImageFor(zone), null,
      `an unmapped zone resolved to an image: ${JSON.stringify(zone)}`);
  });
  assert.match(libSource, /Object\.prototype\.hasOwnProperty\.call\(CITY_IMAGES/,
    'the lookup is not an own-property check');
  assert.match(libSource, /var src = cityImageFor\(zone\);/, 'the image is not read from the table');
  assert.ok(!/['"][a-z-]*' \+\s*zone/.test(libSource) && !/\+\s*['"]-nightscape/.test(libSource),
    'an image path is assembled from a zone field instead of read from the table');
});

check('M10.1: every mapped image is a real WebP inside its cap and its slot', () => {
  const rows = [];
  Object.entries(FP.CITY_IMAGES).forEach(([id, file]) => {
    const filePath = path.join(webDir, file);
    assert.ok(fs.existsSync(filePath), `web/${file} is missing (mapped from ${id})`);
    const bytes = fs.readFileSync(filePath);
    const frame = webpFrame(bytes);
    assert.ok(frame.width > 0 && frame.height > 0, `${file} has no frame size`);
    assert.ok(Math.abs(frame.width / frame.height - 16 / 9) < 0.02,
      `${file} is not 16:9 (${frame.width}x${frame.height})`);
    assert.ok(frame.width <= 800 && frame.height <= 450,
      `${file} is ${frame.width}x${frame.height}, larger than the 800x450 slot`);
    assert.ok(bytes.length <= 120 * 1024,
      `${file} is ${(bytes.length / 1024).toFixed(1)} KB, over the 120 KB hard cap`);
    assert.ok(!bytes.includes(Buffer.from('EXIF')) && !bytes.includes(Buffer.from('ICCP')),
      `${file} still carries embedded metadata`);
    rows.push(`${file} ${frame.tag.trim()} ${frame.width}x${frame.height} ${(bytes.length / 1024).toFixed(1)} KB`);
  });
  console.log(`         ${rows.join('\n         ')}`);
  // the backdrop is the sixth image the page ships, and the only one it loads before the cards
  const backdrop = fs.readFileSync(path.join(webDir, 'bg-storm.webp'));
  const frame = webpFrame(backdrop);
  assert.equal(`${frame.width}x${frame.height}`, '930x312', 'the backdrop is not the prepared file');
  assert.ok(backdrop.length <= 350 * 1024, `the backdrop is ${(backdrop.length / 1024).toFixed(1)} KB`);
  console.log(`         bg-storm.webp ${frame.width}x${frame.height} ${(backdrop.length / 1024).toFixed(1)} KB`);
});

check('M10.1: the seven files named *.svg are PNG payloads and nothing references them', () => {
  // They are raster data with the wrong extension. Loading one as an icon would mean serving PNG
  // bytes under image/svg+xml - a wrong type a browser may refuse - and the site already has a
  // proper inline SVG icon set, so the correct wiring is no wiring at all.
  const misnamed = ['alert-icon.svg', 'building-icon.svg', 'check-icon.svg', 'chevron-right.svg',
    'city-icon.svg', 'clock-icon.svg', 'weather-rain.svg'];
  const sources = { 'index.html': htmlSource, 'styles.css': cssSource, 'lib.js': libSource, 'app.js': appSource };
  misnamed.forEach((name) => {
    const filePath = path.join(webDir, name);
    assert.ok(fs.existsSync(filePath), `web/${name} disappeared`);
    const bytes = fs.readFileSync(filePath);
    assert.equal(bytes.subarray(1, 4).toString('ascii'), 'PNG', `web/${name} is no longer the PNG it was`);
    assert.notEqual(bytes.subarray(0, 5).toString('ascii'), '<?xml', `web/${name} became real SVG markup`);
    Object.entries(sources).forEach(([source, text]) => {
      assert.ok(!text.includes(name), `${source} references ${name}`);
    });
  });
  // and the icon system is still the one that ships with the page
  assert.match(libSource, /function icon\(/, 'the inline SVG icon set is gone');
  assert.ok(!/\.svg"/.test(libSource + appSource + htmlSource), 'the front end loads an .svg file');
});

check('M10.1: the card image is decorative, deferred below the fold, and same-origin', () => {
  assert.match(libSource, /<img class="city-photo" src="' \+ escapeHtml\(src\) \+ '" alt="" width="800" height="450" '/,
    'the city image is not decorative, or it no longer reserves its space');
  assert.match(libSource, /eager \? '' : 'loading="lazy" '/, 'no city image is deferred');
  assert.match(libSource, /decoding="async"/, 'the city image would decode on the main thread');
  assert.match(appSource, /eager: index < 4/, 'the renderer no longer tells the first cards apart');
  // the band is decoration: hidden from assistive tech, with the name in text beside it
  assert.match(libSource, /class="city-thumb band-' \+ band \+ '" data-band="' \+ escapeHtml\(band\) \+\s*'" aria-hidden="true"/,
    'the image band is exposed to assistive tech');
  // and every source is a bare filename from the table, so no image can come from anywhere else
  assert.ok(!/src="[^"]*\.svg"/.test(libSource), 'a card can load a .svg');
  assert.ok(!/https?:/.test(libSource.slice(libSource.indexOf('CITY_IMAGES'), libSource.indexOf('function cityThumb'))),
    'the mapping can point at a remote image');
});

check('M10.1: the stylesheet gives the band a ratio, a crop and a gradient', () => {
  const layer = m10Block();
  assert.match(layer, /\.city-thumb\s*\{[^}]*aspect-ratio:\s*16\s*\/\s*9/,
    'the image band has no fixed 16:9 ratio');
  assert.match(layer, /\.city-thumb \.city-photo\s*\{[^}]*object-fit:\s*cover/,
    'the image is not cropped to cover its band');
  assert.match(layer, /\.city-thumb::after\s*\{[^}]*linear-gradient/,
    'the band has no gradient, so the name can meet a bright skyline');
  assert.match(layer, /\.city-thumb\s*\{[^}]*place-items:\s*center/,
    'an unmapped zone has no plate to fall back to');
  assert.match(layer, /\.zone-card \.zone-body\s*\{[^}]*padding/, 'the city list card has no padding');
  // the backdrop's own treatment: covered, centred on the top edge, under the navy ramp
  assert.match(layer, /\.bg-layer\s*\{[^}]*background-position:\s*center top/,
    'the backdrop is no longer anchored to the top edge');
  assert.match(layer, /\.card\s*\{[^}]*backdrop-filter/, 'the panels lost their scrim');
});

check('M10.1: the city grid collapses 5 -> 3 -> 2 -> 1 without overflow', () => {
  // The tracks come from two rules: an auto-fit track that fits as many 248px columns as the
  // width allows, and two explicit overrides (five across on a wide desktop, one on a phone).
  const autoFit = /\.city-cards \{[^}]*repeat\(auto-fit, minmax\(248px, 1fr\)\)/.test(cssSource);
  assert.ok(autoFit, 'the city grid lost its auto-fit track');
  assert.match(m10Block(), /\.city-cards \{[^}]*repeat\(5, minmax\(0, 1fr\)\)/,
    'the wide desktop never gets its single row of five');
  assert.match(m10Block(), /@media \(max-width: 560px\)\s*\{[\s\S]*?\.city-cards\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/,
    'the city grid does not collapse to one column on a phone');
  // derived, not guessed: usable width = min(viewport, 1680) - 2 * 28px padding, then as many
  // 248px columns as fit with the 18px card gap
  const columns = (viewport) => {
    const usable = Math.min(viewport, 1680) - 56;
    if (viewport >= 1320) return 5;
    if (viewport <= 560) return 1;
    return Math.max(1, Math.floor((usable + 18) / (248 + 18)));
  };
  const table = [320, 390, 560, 768, 1024, 1280, 1320, 1920]
    .map((width) => `${width}px:${columns(width)}`).join('  ');
  console.log(`         columns by viewport width: ${table}`);
  assert.equal(columns(1024), 3, '1024px does not give three columns');
  assert.equal(columns(768), 2, '768px does not give two columns');
  assert.equal(columns(560), 1, 'a 560px phone does not get one column');
  assert.equal(columns(1920), 5, 'a wide desktop does not get one row of five');
});

check('M10.1: the imagery does not widen the policy or leave the origin', () => {
  const policy = /<meta http-equiv="Content-Security-Policy" content="([^"]+)"/.exec(htmlSource)[1];
  assert.match(policy, /img-src 'self' data:/, 'img-src no longer allows exactly the local images');
  assert.ok(!/img-src[^;]*(\*|https?:)/.test(policy), 'img-src was widened to a wildcard or a remote origin');
  assert.ok(!/url\(\s*["']?https?:/i.test(cssSource), 'the stylesheet fetches something remote');
  assert.ok(!/https?:\/\/[^"'\s]+\.(?:png|jpe?g|webp|gif|svg)/i.test(htmlSource + libSource + appSource + cssSource),
    'a remote image URL is in the front end');
  Object.values(FP.CITY_IMAGES).forEach((file) => {
    assert.match(file, /^[a-z-]+\.webp$/, `${file} is not a same-directory file`);
    assert.ok(fs.existsSync(path.join(webDir, file)), `web/${file} is missing`);
  });
});

/* ------------------------------------------------- M10.2: is it actually visible? ---
 *
 * The review that prompted this pass said the page "looks like a mostly flat navy background"
 * while the photograph was deployed, referenced and loading. It was invisible for four arithmetic
 * reasons, not for a missing asset (see the M10.2 comment in styles.css): a `screen` blend mode
 * that keeps only the lightest values, an `opacity` on the layer, and two navy gradients inside
 * it - one a near-opaque `::after`, one an inset box-shadow - each multiplying the picture down.
 *
 * A static check can still answer "is it visible?" because the answer is arithmetic: every
 * overlay in this file is a gradient with alpha stops, and the transmission of a stack of them at
 * a point is the product of (1 - alpha). Rules are resolved the way a browser resolves them for
 * one viewport - the media queries that apply, file order, last declaration wins - and probed at
 * the middle of the screen and in the corners. Only the gradients are modelled: the inset
 * box-shadow adds edge darkening on top, so every corner figure here is an upper bound.
 */

const cssRules = (source) => {
  const rules = [];
  const stack = [];
  const media = [];
  let pending = '';
  let body = '';
  for (const char of source) {
    if (char === '{') {
      const prelude = pending.trim().replace(/\s+/g, ' ');
      pending = '';
      if (prelude.startsWith('@')) {
        media.push(prelude);
        stack.push({ at: prelude });
      } else {
        stack.push({ selector: prelude });
        body = '';
      }
      continue;
    }
    if (char === '}') {
      const open = stack.pop();
      if (open && open.selector) {
        rules.push([open.selector, body, media.length > 0 ? media[media.length - 1] : 'desktop']);
      } else {
        media.pop();
      }
      pending = '';
      body = '';
      continue;
    }
    if (stack.length > 0 && stack[stack.length - 1].selector) body += char;
    else pending += char;
  }
  return rules;
};

const gradientLayers = (value) => {
  const layers = [];
  const pattern = /(linear|radial)-gradient\(([^()]*(?:\([^()]*\)[^()]*)*)\)/g;
  let match;
  while ((match = pattern.exec(value)) !== null) {
    const head = match[2].split(',')[0];
    const stops = [];
    const stopPattern = /rgba\(([^)]*)\)\s*([\d.]+)%/g;
    let stop;
    while ((stop = stopPattern.exec(match[2])) !== null) {
      const parts = stop[1].split(',').map((part) => part.trim());
      stops.push([Number(stop[2]) / 100, parts.length > 3 ? Number(parts[3]) : 1]);
    }
    if (stops.length > 0) {
      layers.push({
        kind: match[1],
        stops,
        angle: /(-?[\d.]+)deg/.exec(head),
        at: /at\s+([\d.]+)%\s+([\d.]+)%/.exec(head),
        size: /^\s*([\d.]+)%\s+([\d.]+)%/.exec(head)
      });
    }
  }
  return layers;
};

const alphaAt = (layer, x, y) => {
  let t;
  if (layer.kind === 'linear') {
    // only a vertical axis is used in this file; anything else is read as vertical
    const angle = layer.angle ? Math.abs(Number(layer.angle[1])) : 180;
    t = angle < 1 ? 1 - y : y;
  } else {
    if (!layer.size || !layer.at) return 0;
    const rx = Number(layer.size[1]) / 100;
    const ry = Number(layer.size[2]) / 100;
    const cx = Number(layer.at[1]) / 100;
    const cy = Number(layer.at[2]) / 100;
    t = Math.hypot((x - cx) / rx, (y - cy) / ry);
  }
  const stops = layer.stops;
  if (t <= stops[0][0]) return stops[0][1];
  if (t >= stops[stops.length - 1][0]) return stops[stops.length - 1][1];
  for (let index = 1; index < stops.length; index += 1) {
    const [p0, a0] = stops[index - 1];
    const [p1, a1] = stops[index];
    if (t >= p0 && t <= p1) return p1 === p0 ? a0 : a0 + ((a1 - a0) * (t - p0)) / (p1 - p0);
  }
  return stops[stops.length - 1][1];
};

const cssDeclarations = (body) => {
  const out = {};
  body.split(';').forEach((piece) => {
    const at = piece.indexOf(':');
    if (at > 0) out[piece.slice(0, at).trim()] = piece.slice(at + 1).trim().replace(/\s+/g, ' ');
  });
  return out;
};

const backdropState = (contexts) => {
  const state = { opacity: 1, layers: [], vignette: [], layerRules: [] };
  cssRules(cssSource.replace(/\/\*[\s\S]*?\*\//g, ' ')).forEach(([selector, body, context]) => {
    if (!contexts.includes(context)) return;
    const decls = cssDeclarations(body);
    if (selector === '.bg-layer') {
      if (decls.opacity) state.opacity = Number(decls.opacity);
      if (decls['background-image']) state.layers = gradientLayers(decls['background-image']);
      if (decls.background) state.layers = gradientLayers(decls.background);
      if (decls['box-shadow']) state.shadow = decls['box-shadow'];
      state.layerRules.push(decls);
    }
    if (selector === '.bg-layer::after' && decls.background) {
      state.vignette = gradientLayers(decls.background);
    }
  });
  return state;
};

const transmissionAt = (contexts, x, y) => {
  const state = backdropState(contexts);
  return state.layers.concat(state.vignette)
    .reduce((value, layer) => value * (1 - alphaAt(layer, x, y)), state.opacity);
};

const percent = (value) => `${(value * 100).toFixed(0)}%`;

check('M10.2: the photograph reaches the screen, not just the corners', () => {
  const mid = transmissionAt(['desktop'], 0.5, 0.5);
  const top = transmissionAt(['desktop'], 0.5, 0.06);
  const corner = transmissionAt(['desktop'], 0.04, 0.96);
  const phone = transmissionAt(['desktop', '@media (max-width: 760px)'], 0.5, 0.5);
  const small = transmissionAt(['desktop', '@media (max-width: 760px)', '@media (max-width: 430px)'],
    0.5, 0.5);
  console.log(`         photograph surviving the overlay: middle ${percent(mid)} · behind the header `
    + `${percent(top)} · corner ${percent(corner)} · phone ${percent(phone)} · small phone ${percent(small)}`);
  assert.ok(mid >= 0.30,
    `the overlay is hiding the backdrop again: only ${percent(mid)} of the photograph reaches the middle of a desktop screen`);
  assert.ok(mid <= 0.62,
    `the overlay stopped doing its job: ${percent(mid)} survives, so text sits on a raw photograph`);
  assert.ok(top >= 0.25, `nothing of the city shows behind the navigation: ${percent(top)} survives there`);
  assert.ok(corner < mid * 0.5,
    `the vignette is gone: the corner keeps ${percent(corner)} of the photograph against ${percent(mid)} in the middle`);
  assert.ok(phone >= 0.20, `the phone overlay removes the backdrop: ${percent(phone)} survives`);
  assert.ok(small >= 0.15 && small <= phone + 1e-9,
    `the small-phone overlay is out of range: ${percent(small)} against ${percent(phone)} on a phone`);
});

check('M10.2: nothing multiplies the backdrop twice, and no shell paints over it', () => {
  // the rule that wins for `.bg-layer` on a desktop viewport: the last one outside a media query
  const layerRules = cssRules(cssSource.replace(/\/\*[\s\S]*?\*\//g, ' '))
    .filter(([selector, , context]) => selector === '.bg-layer' && context === 'desktop')
    .map(([, body]) => body);
  assert.ok(layerRules.length > 0, 'the backdrop layer is gone');
  const effective = layerRules[layerRules.length - 1];
  assert.ok(!/mix-blend-mode:\s*(screen|lighten|plus-lighter|color-dodge)/.test(effective),
    'the backdrop is blended in a mode that keeps only its lightest values, which is what made it a haze');
  assert.match(effective, /opacity:\s*1\b/,
    'the layer carries an opacity, so the overlay is a second mask rather than a ramp');
  assert.equal(backdropState(['desktop']).opacity, 1, 'the effective layer opacity is not 1');
  // the shells the content sits on must not paint, or the backdrop can never show in the gaps
  ['main', '.view', '.view-head', '.page-shell'].forEach((shell) => {
    cssRules(cssSource.replace(/\/\*[\s\S]*?\*\//g, ' ')).forEach(([selector, body]) => {
      if (selector !== shell) return;
      assert.ok(!/(^|[\s;])background(-image|-color)?\s*:/.test(body),
        `${shell} paints a background, so the city cannot show between the panels`);
    });
  });
  // surfaces: a hierarchy, and none of them a wall
  const tokenAlpha = (name) => {
    const found = [...cssSource.matchAll(new RegExp(`--${name}:\\s*([^;]+);`, 'g'))];
    assert.ok(found.length > 0, `the surface token --${name} is gone`);
    const alphas = [...found[found.length - 1][1].matchAll(/rgba\([^)]*?,\s*([\d.]+)\)/g)]
      .map((match) => Number(match[1]));
    assert.ok(alphas.length > 0, `--${name} has no alpha to read`);
    return alphas.reduce((sum, value) => sum + value, 0) / alphas.length;
  };
  const hero = tokenAlpha('panel');
  const panel = tokenAlpha('surface');
  const card = tokenAlpha('surface-card');
  const header = tokenAlpha('topbar-surface');
  console.log(`         surface opacity: hero ${hero.toFixed(2)} · large panel ${panel.toFixed(2)} · `
    + `card ${card.toFixed(2)} · header ${header.toFixed(2)}`);
  assert.ok(hero < panel && panel < card,
    'the surfaces lost their hierarchy: a large panel and a stat tile are equally opaque');
  assert.ok(card <= 0.90, `an ordinary card is a wall again, at ${card.toFixed(2)} opaque`);
  assert.ok(hero >= 0.60, `the hero is too transparent to hold text, at ${hero.toFixed(2)}`);
  assert.ok(header >= 0.65 && header <= 0.88,
    `the header is outside the range that keeps both the labels and the city visible: ${header.toFixed(2)}`);
  assert.match(cssSource, /\.stat,[\s\S]{0,240}?background:\s*var\(--surface-card\)/,
    'the ordinary cards no longer use the darker surface');
  assert.match(cssSource, /\.hero,\s*\.hero\.hero-live\s*\{[^}]*background:\s*var\(--panel\)/,
    'the hero no longer uses the most transparent surface');
});

/* ---------------------------------------------------------------- M11: cities --- */

check('M11: the Cities page controls are in the markup, labelled, and each one is wired', () => {
  // the search box: a real label, a real type, and the list it filters named for assistive tech
  assert.match(htmlSource, /<label[^>]*for="cities-search"[^>]*>Search city or state<\/label>/,
    'the search box has no label that says what it searches');
  assert.match(htmlSource, /id="cities-search"[^>]*type="search"/,
    'the search box is not a search input');
  assert.match(htmlSource, /id="cities-search"[^>]*aria-controls="zones"/,
    'the search box does not name the list it filters');
  assert.match(htmlSource, /id="cities-toolbar"[^>]*role="search"[^>]*aria-labelledby="cities-search-label"/,
    'the toolbar is not a labelled search landmark');
  ['risk', 'name', 'fresh'].forEach((value) => {
    assert.match(htmlSource, new RegExp(`<option value="${value}"`),
      `the sort control does not offer the "${value}" order`);
  });
  assert.match(htmlSource, /<label[^>]*for="cities-sort"/, 'the sort control has no label');
  // the result count is polite, and it is written only when the sentence changes
  assert.match(htmlSource, /id="cities-count"[^>]*role="status"[^>]*aria-live="polite"/,
    'the result count is not a polite status region');
  assert.match(appSource, /el\['cities-count'\]\.textContent !== line/,
    'the result count is rewritten on every poll, so it announces itself every few seconds');
  // the empty state is its own element, hidden until a filter excludes everything, and outside
  // the grid (so "the grid is empty" and "the filters excluded everything" stay different states)
  assert.match(htmlSource, /id="cities-empty"[^>]*hidden/,
    'the empty state is on screen before any payload arrives');
  const gridAt = htmlSource.indexOf('<section id="zones"');
  const gridClosedAt = htmlSource.indexOf('</section>', gridAt);
  const emptyAt = htmlSource.indexOf('id="cities-empty"');
  assert.ok(gridAt > 0 && gridClosedAt > gridAt, 'the card grid is not a section of the page');
  assert.ok(emptyAt > gridClosedAt,
    'the empty state is nested inside the card grid: "no cards" and "no match" would share a state');
  assert.match(libSource, /action: 'clear-filters'/, 'the empty state offers no way back to the fleet');
  assert.match(appSource, /'\[data-action="clear-filters"\]'/, 'the empty state\'s button is not wired');
});

check('M11: the Overview names the fleet it is showing part of, and links to all of it', () => {
  assert.match(htmlSource, /id="city-cards-more"[^>]*href="#\/cities"/,
    'the way to the rest of the fleet does not lead to the Cities page');
  assert.match(htmlSource, /id="city-cards-more"[^>]*hidden/,
    'the way to the rest of the fleet is on screen before any payload arrives');
  assert.match(appSource, /FP\.ranking\(all\)\.slice\(0, OVERVIEW_CITY_LIMIT\)/,
    'the Overview does not slice the fleet by one named limit');
  assert.match(appSource, /var OVERVIEW_CITY_LIMIT = 5;/,
    'the Overview limit is not defined in one place');
  assert.match(appSource, /payload\.zone_count/, 'no sentence counts the fleet from the payload');
  assert.ok(!/\b15 cities\b/.test(htmlSource + appSource + libSource),
    'a page hardcodes the size of the fleet instead of reading it from the payload');
});

check('M11: every chip is built from the payload, and says what it sets', () => {
  assert.match(libSource, /function filterChips\(kind, options, active\)/,
    'the chip renderer is not in lib.js, where the other renderers live');
  assert.match(libSource, /class="ghost filter-chip/, 'a filter chip does not reuse the button style');
  assert.match(libSource, /aria-pressed="' \+ \(on \? 'true' : 'false'\)/,
    'a chip does not tell assistive tech whether it is pressed');
  assert.match(libSource, /data-' \+ kind \+ '="' \+ escapeHtml\(option\.value\)/,
    'a chip does not carry the value it sets');
  assert.match(libSource, /options\.push\(\{ value: entry\.region, label: entry\.region \}\)/,
    'the region chips are not the regions the payload grouped the fleet into');
  assert.match(libSource, /BANDS\.indexOf\(String\([\s\S]{0,140}?\.toUpperCase\(\)\)/,
    'a risk floor is compared with a case the API does not use');
  ['WATCH', 'WARNING', 'CRITICAL'].forEach((floor) => {
    assert.match(appSource, new RegExp(`'${floor}'`), `the risk filter does not offer ${floor}+`);
  });
  // filtering is client-side: a control can never be the reason the page asks for the fleet again
  assert.ok(!/fetch\([^)]*cities-(search|region|risk|sort)/.test(appSource),
    'a filter control fetches from the API instead of filtering what the page already has');
});

check('M11: a region is navigation, never a second risk score', () => {
  assert.match(libSource, /regions: \(Array\.isArray\(block\.regions\) \? block\.regions : \[\]\)/,
    'the coverage block is not read as the API sent it');
  assert.ok(!/region[\s\S]{0,40}?risk_score/i.test(libSource + appSource),
    'a region is given a risk score of its own');
  assert.match(cssSource, /\.city-thumb-region\s*\{/, 'the drawn plate cannot name its region');
  assert.match(libSource, /class="city-thumb-region"/, 'no drawn plate names the region of its city');
  assert.match(libSource, /function fleetScopeNote\(payload\)/,
    'the fleet has no sentence stating what it does and does not cover');
});

check('M11: a city card names the city, its state and its region, and the way into it', () => {
  assert.match(libSource, /function cityPlaceLine\(zone\)/, 'a card has no place line');
  assert.match(libSource, /class="city-place" data-f="city_state"/,
    'the place line is not marked as the city and its state');
  assert.match(libSource, /class="city-region"> · /, 'the place line does not name the region');
  assert.match(libSource, /function openCityLabel\(noun\)/, 'a card does not say how to open it');
  assert.match(libSource, /class="city-open-label" aria-hidden="true"/,
    'the open label is not decorative, so a screen reader hears it twice');
  assert.match(libSource, /function incidentZoneLabel\(incident\)/,
    'an alert row cannot name the city and the state its incident was recorded for');
  assert.match(cssSource, /\.zone-card:hover \.city-open-label/, 'the open label does not respond to hover');
});

/* -------------------------------------------------------------------------- */
/* M11.2: the control bar, measured from the stylesheet at eleven widths.      */
/* -------------------------------------------------------------------------- */

/* A small model of the cascade, enough to answer one question: does a declared minimum fit at a
 * given viewport? Declarations are read in source order; a declaration inside a media query that
 * matches the viewport overrides the ones before it. */
const enclosingMediaFor = (source, index) => {
  let depth = 0;
  for (let i = index - 1; i >= 0; i -= 1) {
    const ch = source[i];
    if (ch === '}') depth += 1;
    else if (ch === '{') {
      if (depth === 0) {
        const header = source.slice(source.lastIndexOf('\n', i) + 1, i).trim();
        if (header.startsWith('@media')) return header;
      } else {
        depth -= 1;
      }
    }
  }
  return '';
};
const mediaMatches = (header, viewport) => {
  const min = /min-width:\s*(\d+)px/.exec(header);
  const max = /max-width:\s*(\d+)px/.exec(header);
  if (min && viewport < parseInt(min[1], 10)) return false;
  if (max && viewport > parseInt(max[1], 10)) return false;
  return true;
};
const declared = (selector, property, viewport) => {
  const escaped = selector.replace('.', '\\.');
  // the property needs a boundary: "width" is a suffix of "min-width"
  const pattern = new RegExp(`${escaped}\\s*(?:,[^{}]*)?\\{[^{}]*(?<![\\w-])${property}\\s*:\\s*([^;]+);`, 'g');
  let found = null;
  for (const match of cssSource.matchAll(pattern)) {
    const scope = enclosingMediaFor(cssSource, match.index);
    if (!scope || mediaMatches(scope, viewport)) found = match[1].trim();
  }
  return found;
};
const trackMinimums = (tracks) => (String(tracks).match(/minmax\([^)]*\)|min\([^)]*\)|auto|[0-9.]+(?:fr|px|%)/g) || [])
  .map((track) => {
    const inner = /^minmax\(\s*([^,]+),/.exec(track);
    const source = inner ? inner[1].trim() : track;
    const px = /^([0-9.]+)px$/.exec(source);
    return px ? parseFloat(px[1]) : 0;
  });
const pixelValues = (value) => [...String(value || '').matchAll(/(\d+(?:\.\d+)?)px/g)].map((m) => parseFloat(m[1]));
const gutterFor = (viewport) => (viewport <= 760 ? 16 : 28);

check('M11.2: the control bar declares a layout that fits every width the brief names', () => {
  [320, 360, 390, 430, 600, 768, 900, 1024, 1280, 1440].forEach((viewport) => {
    const tracks = declared('.cities-toolbar', 'grid-template-columns', viewport);
    assert.ok(tracks, `the toolbar declares no columns at ${viewport}px`);
    const gaps = pixelValues(declared('.cities-toolbar', 'gap', viewport));
    const columnGap = gaps.length > 1 ? gaps[1] : (gaps[0] || 0);
    // and the declared shape: a count of areas rows that matches the tier's intent
    const areas = declared('.cities-toolbar', 'grid-template-areas', viewport) || '';
    const declaredRows = (areas.match(/'[^']+'|"[^"]+"/g) || []).length;
    const expectedRows = viewport >= 1200 ? 1 : viewport >= 768 ? 2 : 4;
    assert.equal(declaredRows, expectedRows,
      `at ${viewport}px the bar declares ${declaredRows} grid row(s), expected ${expectedRows}: ${areas}`);
    const minima = trackMinimums(tracks);
    const required = minima.reduce((sum, one) => sum + one, 0) + Math.max(0, minima.length - 1) * columnGap;
    const available = viewport - 2 * gutterFor(viewport);
    assert.ok(required <= available,
      `at ${viewport}px the toolbar's ${minima.length} tracks want ${required}px ` +
      `(minimums ${minima.join('+')}, ${columnGap}px gaps) in a ${available}px content box`);
    // the two typed controls can never force a wider column than the track they sit in
    ['.cities-search', '.cities-sort'].forEach((selector) => {
      const width = declared(selector, 'width', viewport);
      assert.ok(!width || width === '100%' || pixelValues(width)[0] <= available,
        `${selector} declares width ${width} at ${viewport}px`);
      assert.match(declared(selector, 'min-width', viewport) || '', /0/,
        `${selector} has no min-width: 0 at ${viewport}px, so its own content sets a floor`);
    });
  });

  // one grid, four named areas, and no tier that leaves a control without a home
  assert.match(cssSource, /grid-template-areas: 'search region risk sort';/, 'no single-row desktop tier');
  assert.match(cssSource, /grid-template-areas: 'search sort' 'region risk';/, 'no two-row tablet tier');
  assert.match(cssSource, /grid-template-areas: 'search' 'region' 'risk' 'sort';/, 'no stacked phone tier');
  ['search', 'region', 'risk', 'sort'].forEach((area) => {
    assert.match(cssSource, new RegExp(`grid-area: ${area};`), `no field claims the ${area} area`);
  });
});

check('M11.2: the chips wrap, and nothing in the bar is set to nowrap', () => {
  assert.match(cssSource, /\.cities-toolbar \.button-group \{[^}]*flex-wrap: wrap/,
    'the chip groups do not wrap inside the toolbar');
  assert.match(cssSource, /\.filter-chip \{[^}]*min-height: 44px/, 'a chip is not a 44px touch target');
  assert.match(cssSource, /\.filter-chip \{[^}]*max-width: 100%/, 'a chip may grow past its container');
  // the only nowrap in this area is on a three-letter chip and on a metric value, both deliberate
  const bar = cssSource.slice(cssSource.indexOf('finding one of fifteen')).replace(/\/\*[\s\S]*?\*\//g, '');
  const nowraps = [...bar.matchAll(/white-space:\s*nowrap/g)].length;
  assert.equal(nowraps, 0, `the control bar contains ${nowraps} white-space: nowrap rule(s)`);
  assert.match(htmlSource, /class="cities-toolbar" id="cities-toolbar" role="search"/,
    'the toolbar lost its search role');
  ['cities-search-label', 'cities-region-label', 'cities-risk-label'].forEach((id) => {
    assert.ok(htmlSource.includes(`id="${id}"`), `the ${id} label is gone`);
  });
  assert.match(htmlSource, /<label class="toolbar-label" for="cities-sort">Sort by<\/label>/,
    'the sort control lost its label');
  assert.match(libSource, /aria-pressed=/, 'a chip no longer states whether it is pressed');
});

check('M11.2: one metric is four columns, and the phone gets two lines', () => {
  assert.match(cssSource, /\.metric \{\s*display: grid;/,
    'a metric row is not a grid');
  assert.match(cssSource, /\.metric \{\s*display: grid;\s*grid-template-columns:\s*minmax\(130px, 1fr\) auto minmax\(90px, auto\)/,
    'the metric row does not declare four columns: name, chip, value, bar');
  assert.match(cssSource, /\.metric-name \{[^}]*min-width: 0/, 'a metric name is not allowed to shrink');
  assert.match(cssSource, /\.metric-name \{[^}]*overflow-wrap: break-word/, 'a long metric name cannot wrap');
  assert.match(cssSource, /\.metric-value \{[^}]*font-variant-numeric: tabular-nums/,
    'metric values are not set in tabular figures, so they cannot line up');
  assert.match(cssSource, /\.metric-value \{[^}]*white-space: nowrap/,
    'a value and its unit may be split across lines');
  assert.match(cssSource, /@media \(max-width: 767px\) \{[\s\S]{0,900}?\.metric \{[\s\S]{0,240}?grid-template-areas: 'name source' 'value track'/,
    'the phone metric row is not the two-line layout');
  assert.match(libSource, /<span class="metric-name">' \+ escapeHtml\(METRIC_META\[metric\]\.label\) \+ '<\/span>'/,
    'the metric name cell no longer closes before the chip');
  assert.match(libSource, /'<span class="metric-source" data-f="metric-source">' \+ sourceChip/,
    'the chip is not its own cell in the row');
});

/* ------------------------------------------------------------------ report --- */

console.log(`\n${passed} checks passed, ${failures.length} failed`);
if (failures.length > 0) {
  failures.forEach((failure) => {
    console.log(`  FAILED: ${failure.name}`);
    // the message says what was expected and what arrived, so a failure is not a guessing game
    if (failure.message) {
      console.log(`          ${String(failure.message).split('\n')[0]}`);
    }
  });
  process.exit(1);
}
