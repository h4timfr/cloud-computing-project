/* M16 temporal semantics: the browser module must satisfy tests/fixtures/m16_temporal_cases.json exactly.
 *
 *   node tools/temporal_check.mjs            # all cases
 *   node tools/temporal_check.mjs --case flat
 *
 * The Python side (tests/test_temporal_m16.py) reads the same fixture, so the analysis tool and the page
 * cannot drift apart. Read-only: it loads web/lib.js into a VM and measures.
 */

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const fixturePath = path.join('tests', 'fixtures', 'm16_temporal_cases.json');
const libPath = path.join('web', 'lib.js');
const only = process.argv.indexOf('--case') >= 0 ? process.argv[process.argv.indexOf('--case') + 1] : null;

const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

/* lib.js is an IIFE that assigns its API onto the global object, so a minimal sandbox is enough: it
 * defines functions at load time and touches the DOM only when a caller asks it to render. */
function loadTemporal() {
  const sandbox = { console, setTimeout, clearTimeout, Date, Math, JSON, isFinite, parseInt, parseFloat, Number, String, Array, Object, Boolean, RegExp, Error };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(libPath, 'utf8'), sandbox, { filename: libPath });
  const temporal = sandbox.FP && sandbox.FP.temporal;
  if (!temporal) throw new Error('web/lib.js did not export FP.temporal');
  return temporal;
}

const temporal = loadTemporal();

let passed = 0;
const failures = [];
const check = (name, run) => {
  try {
    run();
    passed++;
  } catch (error) {
    failures.push(`${name}: ${error.message}`);
  }
};
const near = (actual, expected, tolerance, label) => {
  if (typeof actual !== 'number' || !isFinite(actual)) throw new Error(`${label}: expected ${expected}, got ${actual}`);
  if (Math.abs(actual - expected) > tolerance) throw new Error(`${label}: expected ${expected}, got ${actual}`);
};
const equal = (actual, expected, label) => {
  if (actual !== expected) throw new Error(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
};

const toPoints = (rows) => rows.map(([offset, score, rainfall]) => ({
  ts_epoch: fixture.base_epoch + offset,
  risk_score: score,
  rainfall_mm_per_hr: rainfall
}));

const rules = fixture.rules;
const fpRules = temporal.rules;
check('rules', () => {
  equal(fpRules.minSamples, rules.min_samples, 'min samples');
  equal(fpRules.minSeconds, rules.min_duration_seconds, 'min duration');
  equal(fpRules.tolerancePerHour, rules.slope_tolerance_per_hour, 'slope tolerance');
  equal(fpRules.gapFactor, rules.gap_median_multiplier, 'gap factor');
  equal(fpRules.gapFloorSeconds, rules.gap_floor_seconds, 'gap floor');
});

for (const testCase of fixture.cases) {
  if (only && testCase.name !== only) continue;
  const points = toPoints(testCase.points);
  const cleaned = temporal.clean(points);
  const expect = testCase.expect;
  const lastTs = cleaned.points.length ? cleaned.points[cleaned.points.length - 1].ts : null;
  const nowSeconds = typeof expect.newest_age_seconds === 'number' ? lastTs + expect.newest_age_seconds : lastTs;
  const summary = temporal.summary(cleaned, { nowSeconds });

  check(`${testCase.name} count`, () => equal(summary.count, expect.count, 'count'));
  if (expect.status) check(`${testCase.name} status`, () => equal(summary.status, expect.status, 'status'));
  if (expect.reason) check(`${testCase.name} reason`, () => equal(summary.reason, expect.reason, 'reason'));
  if (expect.trend) check(`${testCase.name} trend`, () => equal(summary.trend, expect.trend, 'trend'));
  if (expect.net_change !== undefined) {
    if (expect.net_change === null) check(`${testCase.name} net`, () => equal(summary.netChange, null, 'net change'));
    else check(`${testCase.name} net`, () => near(summary.netChange, expect.net_change, 1e-9, 'net change'));
  }
  for (const key of ['peak', 'min']) {
    const expected = expect[key];
    if (expected === undefined) continue;
    const actual = key === 'peak' ? summary.maxScore : summary.minScore;
    check(`${testCase.name} ${key}`, () => {
      if (expected === null) equal(actual, null, key);
      else near(actual, expected, 1e-9, key);
    });
  }
  if (expect.duplicates_removed !== undefined) {
    check(`${testCase.name} duplicates`, () => equal(cleaned.duplicatesRemoved, expect.duplicates_removed, 'duplicates removed'));
  }
  if (expect.invalid_dropped !== undefined) {
    check(`${testCase.name} invalid`, () => equal(cleaned.invalidDropped, expect.invalid_dropped, 'invalid dropped'));
  }
  if (expect.out_of_order_pairs !== undefined) {
    check(`${testCase.name} order`, () => equal(cleaned.outOfOrderPairs, expect.out_of_order_pairs, 'out of order pairs'));
  }
  if (expect.gap_count !== undefined) check(`${testCase.name} gaps`, () => equal(summary.gapCount, expect.gap_count, 'gap count'));
  if (expect.largest_gap !== undefined) {
    check(`${testCase.name} largest gap`, () => near(summary.largestGapSeconds, expect.largest_gap, 1e-9, 'largest gap'));
  }
  if (expect.median_interval !== undefined) {
    check(`${testCase.name} median interval`, () => near(summary.medianIntervalSeconds, expect.median_interval, 1e-9, 'median interval'));
  }
  if (expect.newest_age_seconds !== undefined) {
    check(`${testCase.name} newest age`, () => near(summary.newestAgeSeconds, expect.newest_age_seconds, 1e-9, 'newest age'));
  }
  if (expect.last_rainfall !== undefined) {
    check(`${testCase.name} last rainfall`, () => near(summary.lastRainfall, expect.last_rainfall, 1e-9, 'last rainfall'));
  }
  if (expect.max_rainfall !== undefined) {
    check(`${testCase.name} max rainfall`, () => near(summary.maxRainfall, expect.max_rainfall, 1e-9, 'max rainfall'));
  }
  if (expect.slope_sign) {
    check(`${testCase.name} slope sign`, () => {
      const slope = summary.slopePerHour;
      if (expect.slope_sign === 'positive' && !(slope > 0)) throw new Error(`slope should be positive, got ${slope}`);
      if (expect.slope_sign === 'negative' && !(slope < 0)) throw new Error(`slope should be negative, got ${slope}`);
    });
  }
  if (expect.classes) {
    check(`${testCase.name} classes`, () => equal(summary.classes.join(','), expect.classes.join(','), 'band classes'));
  }
  check(`${testCase.name} sentences`, () => {
    const describe = temporal.describe(summary, 'Risk score');
    const rain = temporal.rainfallSentence(summary);
    const quality = temporal.quality(summary);
    for (const [label, text] of [['describe', describe], ['rainfall', rain], ['quality', quality]]) {
      if (typeof text !== 'string' || !text.length) throw new Error(`${label} produced nothing`);
      if (/NaN|undefined|Infinity|null/.test(text)) throw new Error(`${label} leaked a raw value: ${text}`);
    }
    if (/forecast|predict|expect|likely|probab/i.test(describe)) throw new Error(`describe must stay retrospective: ${describe}`);
  });
}

/* Parity with the analysis tool on the two row-level rules the fixture cannot express, because its
 * points are already numbers: a stored class is trimmed, and an unusable top-level rainfall falls
 * back to the nested reading. tests/test_temporal_m16.py reads the same rows the same way. */
check('a stored class is trimmed, as the server trims it', () => {
  const cleaned = temporal.clean([
    { ts_epoch: 1792400000, risk_score: 0.2, risk_class: ' WATCH ' },
    { ts_epoch: 1792400600, risk_score: 0.24, risk_class: 'watch' },
    { ts_epoch: 1792401200, risk_score: 0.26, risk_class: '   ' }
  ]);
  equal(cleaned.points[0].klass, 'WATCH', 'surrounding whitespace is not part of the label');
  equal(cleaned.points[1].klass, 'watch', 'an unknown label is kept as stored, not re-cased');
  equal(cleaned.points[2].klass, null, 'a blank label is no label');
  const summary = temporal.summary(cleaned, {});
  equal(summary.classes.join(','), 'WATCH,watch,NORMAL', 'a missing label falls back to the score band');
});

check('an unusable top-level rainfall falls back to the nested reading', () => {
  const cleaned = temporal.clean([
    { ts_epoch: 1792400000, risk_score: 0.2, rainfall_mm_per_hr: 'n/a',
      readings: { rainfall_mm_per_hr: 12.5 } },
    { ts_epoch: 1792400600, risk_score: 0.22, readings: { rainfall_mm_per_hr: 3.5 } },
    { ts_epoch: 1792401200, risk_score: 0.24, rainfall_mm_per_hr: 7.5 }
  ]);
  equal(cleaned.points[0].rainfall, 12.5, 'the nested field is the fallback when the top-level is unusable');
  equal(cleaned.points[1].rainfall, 3.5, 'the nested field is the fallback when the top-level is absent');
  equal(cleaned.points[2].rainfall, 7.5, 'a usable top-level value wins');
  equal(temporal.summary(cleaned, {}).maxRainfall, 12.5, 'and the fallback reaches the window');
});

check('window 1h keeps a one-hour series', () => {
  // Newest sample at +6000 s: a one-hour window therefore starts at +2400 s and keeps three of the six.
  const cleaned = temporal.clean(toPoints([[0, 0.2, 0], [600, 0.3, 0], [1200, 0.4, 0], [4800, 0.45, 0], [5400, 0.5, 0], [6000, 0.55, 0]]));
  equal(temporal.window(cleaned, 60).length, 3, 'the last hour holds the three rows inside it');
  equal(temporal.window(cleaned, 360).length, 6, 'six hours holds all six rows');
  equal(temporal.window(cleaned, null).length, 6, 'available holds all six rows');
});
check('window is anchored on the newest sample, not on now', () => {
  const cleaned = temporal.clean(toPoints([[0, 0.2, 0], [600, 0.3, 0]]));
  equal(temporal.window(cleaned, 60).length, 2, 'both rows are inside an hour of the newest row');
});

/* Chart geometry: no NaN, no Infinity, no off-canvas point, endpoints preserved. */
const chartCases = [
  ['no points', [], 0],
  ['one point', [0.3], 1],
  ['two points', [0.2, 0.4], 2],
  ['many points', Array.from({ length: 40 }, (unused, index) => index / 39), 40],
  ['same value series', [0.5, 0.5, 0.5], 3],
  ['minimum value', [0, 0, 0], 3],
  ['maximum value', [1, 1, 1], 3],
  ['invalid samples removed', [0.2, null, NaN, undefined, Infinity, 0.8], 2]
];
for (const [name, values, expectedLength] of chartCases) {
  check(`chart ${name}`, () => {
    const points = temporal.linePoints(values, { width: 240, height: 56 });
    equal(points.length, expectedLength, 'point count');
    for (const [x, y] of points) {
      if (!isFinite(x) || !isFinite(y)) throw new Error(`non-finite coordinate ${x},${y}`);
      if (x < 0 || x > 240) throw new Error(`x outside the canvas: ${x}`);
      if (y < 0 || y > 56) throw new Error(`y outside the canvas: ${y}`);
    }
    if (points.length && expectedLength > 1) {
      equal(points[0][0], 0, 'first x');
      equal(points[points.length - 1][0], 240, 'last x');
    }
  });
}
check('chart maps the score range onto the canvas', () => {
  const points = temporal.linePoints([0, 1], { width: 100, height: 40, min: 0, max: 1 });
  equal(points[0][1], 40, 'the lowest score sits on the floor');
  equal(points[1][1], 0, 'the highest score sits on the ceiling');
});

/* Incidents: the timeline must never invent a severity or a duration. */
for (const incidentCase of fixture.incident_cases) {
  if (only && incidentCase.name !== only) continue;
  const expect = incidentCase.expect;
  check(`incident ${incidentCase.name}`, () => {
    const view = temporal.incident(incidentCase.incident, { nowSeconds: 1792407200 });
    equal(view.open, expect.open, 'open');
    equal(view.severity, expect.severity, 'severity');
    if (expect.escalated !== undefined) equal(view.escalated, expect.escalated, 'escalated');
    if (expect.duration_seconds !== undefined) {
      if (expect.duration_seconds === null) equal(view.durationSeconds, null, 'duration');
      else near(view.durationSeconds, expect.duration_seconds, 1e-9, 'duration');
    }
  });
}


/* ---------------------------------------------------------------- the panel --- */

/* The window controls are computed from what the stored history covers, never hard-coded: a window
 * the history cannot cover is offered disabled, with the reason in the data's own words, and a window
 * the document was asked for is offered even when its samples do not span its full width (M17). */
check('windows: a short history only offers what it can cover', () => {
  const short = temporal.clean(toPoints(Array.from({ length: 12 }, (unused, index) => [index * 600, 0.4, 0])));
  const offered = temporal.windows(short);
  equal(offered.map((option) => option.label).join(','), '1H,6H,24H,AVAILABLE', 'the labels stay in order');
  equal(offered[0].enabled, true, 'an hour of history covers the one-hour window');
  equal(offered[1].enabled, false, 'six hours is not covered by 110 minutes');
  equal(offered[2].enabled, false, 'a day is not covered');
  equal(offered[3].enabled, true, 'available is always offered');
  if (!/covers .*so far/.test(offered[2].reason)) throw new Error(`the reason does not name the span: ${offered[2].reason}`);
  if (/NaN|undefined|null/.test(offered.map((option) => option.reason).join(' '))) throw new Error('a reason leaked a raw value');
  const none = temporal.windows(null);
  equal(none.filter((option) => option.enabled).length, 1, 'with no history only available is offered');
});
check('windows: a window the document asked for is offered even when its samples are short', () => {
  // M17 audit finding: the panel asks for the widest window the API keeps, so a sampled window never
  // spans its full width (measured live: 1414 of 1440 minutes) and comparing the sample span to the
  // window left the 24H control permanently disabled - a control no live city could ever reach.
  const short = temporal.clean(toPoints(Array.from({ length: 12 }, (unused, index) => [index * 600, 0.4, 0])));
  const offered = temporal.windows(short, 1440);
  equal(offered.filter((option) => option.enabled).length, 4, 'a 1440-minute document covers every window');
  if (offered.some((option) => option.reason)) throw new Error('an offered window carries a refusal reason');
  const narrow = temporal.windows(short, 60);
  equal(narrow[1].enabled, false, 'a 60-minute document cannot offer six hours');
  equal(narrow[2].enabled, false, 'a 60-minute document cannot offer a day');
});
check('windows: a long history offers every window', () => {
  const long = temporal.clean(toPoints(Array.from({ length: 60 }, (unused, index) => [index * 1800, 0.3 + index / 200, 0])));
  const offered = temporal.windows(long);
  equal(offered.filter((option) => option.enabled).length, 4, 'thirty hours covers all four windows');
  if (offered.some((option) => option.reason)) throw new Error('an offered window carries a refusal reason');
});

/* The chart is the one SVG both charts come from: fixed 0..1 score axis, band edges as guides, the
 * window's peak and newest sample marked. */
const crossing = fixture.cases.find((entry) => entry.name === 'band_crossings');
const crossed = temporal.clean(toPoints(crossing.points));
const crossedSummary = temporal.summary(crossed, { nowSeconds: fixture.base_epoch + 100000 });

check('chart: the score axis is 0..1 with every band edge drawn', () => {
  const drawn = temporal.chart(crossedSummary, { width: 480, height: 180 });
  equal(drawn.viewBox, '0 0 480 180', 'the viewBox matches the requested size');
  const rects = drawn.body.match(/<rect class="chart-fill/g) || [];
  equal(rects.length, 4, 'four score bands are shaded');
  const guides = drawn.body.match(/<line class="chart-guide"/g) || [];
  equal(guides.length, 5, 'the 0, 0.35, 0.60, 0.80 and 1.00 edges are drawn');
  if (!/0\.35<\/text>/.test(drawn.body) || !/0\.80<\/text>/.test(drawn.body)) {
    throw new Error(`the guides are not labelled with the engine's own edges: ${drawn.body.slice(0, 120)}`);
  }
  const line = /<polyline class="chart-line" points="([^"]+)"/.exec(drawn.body);
  if (!line) throw new Error('the chart has no line');
  equal(line[1].trim().split(/\s+/).length, crossedSummary.count, 'one coordinate per stored point');
  if (!/class="chart-dot is-last"/.test(drawn.body)) throw new Error('the newest sample is not marked');
  if (!/class="chart-dot is-peak"/.test(drawn.body)) throw new Error('the window peak is not marked');
  for (const pair of line[1].trim().split(/\s+/)) {
    const [x, y] = pair.split(',').map(Number);
    if (!isFinite(x) || !isFinite(y)) throw new Error(`non-finite coordinate ${pair}`);
    if (x < 0 || x > 480 || y < 0 || y > 180) throw new Error(`coordinate outside the viewBox: ${pair}`);
  }
});
check('chart: an empty window says so instead of drawing an empty axis', () => {
  const empty = temporal.summary(null, {});
  const drawn = temporal.chart(empty, { width: 320, height: 160, note: 'History could not be loaded.' });
  if (/<polyline/.test(drawn.body)) throw new Error('an empty window drew a line');
  if (!/History could not be loaded\./.test(drawn.body)) throw new Error(`the note is missing: ${drawn.body}`);
  equal(drawn.viewBox, '0 0 320 160', 'the viewBox still matches the box');
});
check('chart: the rainfall series gets its own ceiling, not the score axis', () => {
  const rain = temporal.summary(crossed, {});
  const drawn = temporal.chart(rain, { kind: 'rain', width: 400, height: 160 });
  equal((drawn.body.match(/<rect class="chart-fill"/g) || []).length, 1, 'rainfall draws one plot area, no score bands');
  equal((drawn.body.match(/<line class="chart-guide"/g) || []).length, 2, 'zero and the ceiling are the only guides');
  const line = /<polyline class="chart-line" points="([^"]+)"/.exec(drawn.body);
  if (!line) throw new Error('the rainfall chart has no line');
  for (const pair of line[1].trim().split(/\s+/)) {
    const y = Number(pair.split(',')[1]);
    if (!isFinite(y) || y < 0 || y > 160) throw new Error(`rainfall coordinate outside the viewBox: ${pair}`);
  }
});

/* "What changed": every figure the reader needs, and no direction when the data cannot name one. */
check('facts: what changed, how much and when it peaked', () => {
  const facts = temporal.facts(crossedSummary, { windowLabel: '6H' });
  ['Risk now', 'Window start', 'Net change', 'Direction', 'Peak', 'Rainfall'].forEach((term) => {
    if (!facts.includes(term)) throw new Error(`the change block lost "${term}"`);
  });
  if (new RegExp(crossedSummary.lastScore.toFixed(2)).test(facts) === false) throw new Error('the risk now figure is missing');
  if (!new RegExp(temporal.clock(crossedSummary.peakTs)).test(facts)) throw new Error('the peak time is missing');
  if (!/data-dir="(up|down)"/.test(facts) && crossedSummary.netChange !== 0) {
    throw new Error(`the direction is stated but not marked: ${facts}`);
  }
  if (/NaN|undefined|Infinity/.test(facts)) throw new Error(`the change block leaked a raw value: ${facts}`);
});
check('facts: an insufficient window names no direction', () => {
  const one = temporal.summary(temporal.clean(toPoints([[0, 0.4, 0]])), {});
  const facts = temporal.facts(one, { windowLabel: '1H' });
  if (!/not named/.test(facts)) throw new Error(`an insufficient window claimed a direction: ${facts}`);
  const empty = temporal.facts(temporal.summary(null, {}), { windowLabel: '1H' });
  if (!/nothing to compare/.test(empty)) throw new Error(`the empty state does not say why: ${empty}`);
});

/* The sentence under the chart: the same facts, in words, and honest about gaps. */
check('describe: a gap is disclosed, never smoothed over', () => {
  const gapped = fixture.cases.find((entry) => entry.name === 'large_gap');
  const summary = temporal.summary(temporal.clean(toPoints(gapped.points)), {});
  if (summary.gapCount < 1) throw new Error('the fixture no longer has a gap');
  const text = temporal.describe(summary);
  if (!/Read with care/.test(text)) throw new Error(`a gapped window is not disclosed: ${text}`);
  const quality = temporal.quality(summary);
  if (!/largest gap/.test(quality)) throw new Error(`the history line hides the gap: ${quality}`);
  const length = /largest gap ([^·.]+)/.exec(quality);
  if (!length) throw new Error(`the gap length is missing from the history line: ${quality}`);
  if (!text.includes(length[1].trim())) {
    throw new Error(`the two sentences disagree about the gap length: ${length[1]} vs ${text}`);
  }
});
check('describe: a windowed summary is the sentence for that window, not for the whole series', () => {
  const series = temporal.clean(toPoints(Array.from({ length: 30 }, (unused, index) => [index * 1800, 0.2 + index / 100, 0])));
  const hour = temporal.summary(series, { minutes: 60 });
  const all = temporal.summary(series, { minutes: null });
  if (hour.count >= all.count) throw new Error('the one-hour slice kept the whole series');
  if (!/1 h/.test(temporal.quality(hour)) && !/min/.test(temporal.quality(hour))) {
    throw new Error(`the quality line does not describe the slice: ${temporal.quality(hour)}`);
  }
  equal(hour.lastScore, all.lastScore, 'both windows end at the newest stored sample');
  equal(hour.firstTs >= all.lastTs - 3600, true, 'the slice starts inside its own hour');
});


console.log(`temporal check: ${passed} assertion group(s) passed, ${failures.length} failed`);
for (const failure of failures) console.log(`  FAIL ${failure}`);
process.exit(failures.length ? 1 : 0);
