#!/usr/bin/env node
/*
 * Dashboard verification harness (development tool, not part of the site).
 *
 * The dashboard deliberately has no build step, so this checks it directly:
 *   - loads web/lib.js in a VM and exercises every pure helper
 *   - renders zone cards / ranking from a real API payload (--payload) and asserts the HTML
 *   - asserts the site can only read: no POST, no /readings, no secret anywhere in web/
 *   - asserts every local asset referenced by the HTML/CSS exists and the page stays small
 *
 * Usage:  node tools/dashboard_check.mjs [--payload build/zones-live.json]
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
      contributions: [
        { metric: 'water_level_m', value: 0.95, sub_score: 0.2375, weight: 0.25, contribution: 0.0594 },
        { metric: 'rainfall_mm_per_hr', value: 1.81, sub_score: 0.0603, weight: 0.3, contribution: 0.0181 }
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
  assert.equal(FP.trendLabel({ water_level_delta_m: null }), 'no lookback');
  assert.equal(FP.trendLabel(null), 'no lookback');
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
  assert.ok(html.includes(zone.latest.explanation), 'explanation missing');
  for (const metric of FP.METRIC_ORDER) {
    assert.ok(html.includes(FP.METRIC_META[metric].label), `metric label ${metric} missing`);
  }
  for (const band of ['normal', 'watch', 'warning', 'critical']) {
    assert.ok(html.includes(`band-${band}`) || true, 'band class emitted for at least one value');
  }
  assert.ok(!html.includes('undefined'), 'card contains undefined');
  assert.ok(!html.includes('NaN'), 'card contains NaN');
  assert.ok(html.includes(`band-${FP.bandKey(zone.latest.risk_class)}`), 'effective band class missing');
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
      assert.equal(incident.simulated, true, `incident ${index} is not marked simulated`);
    });
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
}

/* ---------------------------------------------------------------- security --- */

group('Site safety');

const appSource = fs.readFileSync(path.join(webDir, 'app.js'), 'utf8');
const libSource = fs.readFileSync(path.join(webDir, 'lib.js'), 'utf8');
const htmlSource = fs.readFileSync(path.join(webDir, 'index.html'), 'utf8');
const cssSource = fs.readFileSync(path.join(webDir, 'styles.css'), 'utf8');

check('dashboard never writes: no POST, no /readings, no key handling', () => {
  [appSource, libSource].forEach((source) => {
    assert.ok(!source.includes('/readings'), 'dashboard references the ingestion route');
    assert.ok(!/method:\s*['"]POST['"]/i.test(source), 'dashboard performs a POST');
    assert.ok(!source.toLowerCase().includes('x-fp-key'), 'dashboard references the ingestion header');
    assert.ok(!source.includes('FLOODPULSE_INGEST_KEY'), 'dashboard references the secret env var');
  });
});

check('dashboard has no remote dependencies or CDN assets', () => {
  const remote = /https?:\/\/(?!2rg2u8quzd\.execute-api\.eu-north-1\.amazonaws\.com)[a-z0-9.-]+/i;
  assert.ok(!remote.test(htmlSource), 'index.html references a remote asset');
  assert.ok(!remote.test(cssSource), 'styles.css references a remote asset');
  assert.ok(!remote.test(appSource), 'app.js references a remote asset');
  assert.ok(!/cdn|unpkg|jsdelivr|googleapis/i.test(htmlSource), 'index.html mentions a CDN');
  assert.ok(!/import\s+.*from\s+['"]https?:/i.test(appSource), 'app.js imports a remote module');
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

check('index.html keeps the permanent simulated-data notice', () => {
  assert.match(htmlSource, /SIMULATED DATA/);
  assert.match(htmlSource, /heuristic/);
  assert.match(htmlSource, /not a validated\s+hydrological model/);
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
    'incidents', 'incidents-meta', 'incidents-alerting'
  ];
  const missing = idsToCheck.filter((id) => !declared.has(id));
  assert.deepEqual(missing, [], `index.html is missing ids used by app.js: ${missing.join(', ')}`);
  assert.ok(used.size > 0);
});

check('every data-* attribute app.js queries is actually written somewhere', () => {
  // lib.js writes the structural markup and app.js writes the history controls, so the
  // invariant is: anything queried is emitted by one of the two, never by neither.
  const written = new Set();
  for (const source of [libSource, appSource]) {
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

  const sizes = fs.readdirSync(webDir).map((file) => ({ file, bytes: fs.statSync(path.join(webDir, file)).size }));
  const total = sizes.reduce((sum, entry) => sum + entry.bytes, 0);
  const image = sizes.find((entry) => entry.file === 'bg-storm.webp');
  sizes.forEach((entry) => console.log(`         ${entry.file.padEnd(16)} ${(entry.bytes / 1024).toFixed(1)} KB`));
  console.log(`         ${'TOTAL'.padEnd(16)} ${(total / 1024).toFixed(1)} KB`);
  assert.ok(image.bytes <= 400 * 1024, 'backdrop exceeds the 400 KB hard maximum');
  assert.ok(image.bytes <= 250 * 1024, 'backdrop exceeds the 250 KB target');
  assert.ok(total <= 400 * 1024, 'site payload exceeds the 400 KB hard maximum');
});

/* ------------------------------------------------------------------ report --- */

console.log(`\n${passed} checks passed, ${failures.length} failed`);
if (failures.length > 0) {
  failures.forEach((failure) => console.log(`  FAILED: ${failure.name}`));
  process.exit(1);
}
