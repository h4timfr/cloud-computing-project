#!/usr/bin/env node
/*
 * Story verification harness (development tool, not part of the site).
 *
 * tools/storm_demo.py exports the payloads the API really served at each stage of the
 * storm arc. This checks the other half of the promise: that the dashboard *renders*
 * each of those stages as the demo story describes — the right band counts, the right
 * open/closed incidents, and never a claim that simulated data is real.
 *
 * Usage:  node tools/story_check.mjs [--dir build/story]
 */

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const args = process.argv.slice(2);
const dirIndex = args.indexOf('--dir');
const storyDir = dirIndex >= 0 ? args[dirIndex + 1] : path.join('build', 'story');

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

/* --------------------------------------------------------------- load lib.js --- */

const libSource = fs.readFileSync(path.join('web', 'lib.js'), 'utf8');
const sandbox = { window: {}, console, Math, Date, Number, String, isFinite, parseInt, parseFloat };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(libSource, sandbox, { filename: 'web/lib.js' });
const FP = sandbox.window.FP;

const readJson = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));

const storyPath = path.join(storyDir, 'story.json');
if (!fs.existsSync(storyPath)) {
  console.log(`story.json not found in ${storyDir}`);
  console.log('run:  python tools/storm_demo.py --export build/story');
  process.exit(1);
}

const story = readJson(storyPath);
const stages = story.stages || [];
const ORDER = ['NORMAL', 'WATCH', 'WARNING', 'CRITICAL', 'RECOVERY', 'CLOSED'];
const RANK = { NORMAL: 0, WATCH: 1, WARNING: 2, CRITICAL: 3 };
const nowMs = Date.parse(story.stages[0].timestamp);
const expectedFleetHas = (data, zoneId) => (data.fleet || []).includes(zoneId);

console.log(`story: ${story.steps} steps, seed ${story.seed}, arc peak zone ${story.arc_peak_zone}`);
console.log(`fleet: ${(story.fleet || []).join(', ')}`);
console.log(`stages: ${stages.map((stage) => `${stage.stage}@${stage.step}`).join(' -> ')}\n`);

console.log('The arc is complete and ordered');

check('every stage of the demo story was reproduced', () => {
  const names = stages.map((stage) => stage.stage);
  assert.deepEqual(names, ORDER, `expected ${ORDER.join(', ')} but the story has ${names.join(', ')}`);
});

check('the fleet escalates to CRITICAL and comes back to NORMAL', () => {
  const byStage = Object.fromEntries(stages.map((stage) => [stage.stage, stage]));
  assert.ok(byStage.WATCH.step > byStage.NORMAL.step, 'WATCH did not follow NORMAL');
  assert.ok(byStage.WARNING.step >= byStage.WATCH.step, 'WARNING did not follow WATCH');
  assert.ok(byStage.CRITICAL.step >= byStage.WARNING.step, 'CRITICAL did not follow WARNING');
  assert.ok(byStage.RECOVERY.step > byStage.CRITICAL.step, 'recovery did not follow the peak');
  assert.ok(byStage.CLOSED.step >= byStage.RECOVERY.step, 'closure did not follow recovery');
  assert.equal(byStage.CRITICAL.counts.CRITICAL >= 1, true, 'no zone was CRITICAL at the peak');
  assert.equal(byStage.CLOSED.counts.NORMAL, byStage.CLOSED.counts.NORMAL, 'closure stage has no data');
  assert.ok(!byStage.CLOSED.counts.CRITICAL && !byStage.CLOSED.counts.WARNING,
    'a zone was still WARNING or worse once the story says everything closed');
});

console.log('\nThe rendered dashboard matches each stage');

for (const stage of stages) {
  const slug = stage.stage.toLowerCase();
  const zones = readJson(path.join(storyDir, `zones-${slug}.json`));
  const incidents = readJson(path.join(storyDir, `incidents-${slug}.json`));
  const history = readJson(path.join(storyDir, `history-${slug}.json`));

  check(`${stage.stage}: the strip shows the counts the story reports`, () => {
    const counts = Object.assign({ NO_READING: 0 }, FP.bandCounts(zones));
    counts.NO_READING = counts.UNSET;
    assert.equal(counts.NORMAL, stage.counts.NORMAL, 'NORMAL count differs from the story');
    assert.equal(counts.WATCH, stage.counts.WATCH, 'WATCH count differs from the story');
    assert.equal(counts.WARNING, stage.counts.WARNING, 'WARNING count differs from the story');
    assert.equal(counts.CRITICAL, stage.counts.CRITICAL, 'CRITICAL count differs from the story');
    assert.equal(counts.NO_READING, stage.counts.NO_READING, 'no-reading count differs from the story');

    const html = FP.renderBandStrip(zones);
    for (const [band, count] of Object.entries({ normal: counts.NORMAL, watch: counts.WATCH,
      warning: counts.WARNING, critical: counts.CRITICAL, unknown: counts.NO_READING })) {
      assert.ok(html.includes(`>${count}</span>`), `${band} tile does not show ${count}`);
    }
    assert.match(FP.bandStripLabel(zones), /^3 of 3 zones scored in this snapshot$/);
  });

  check(`${stage.stage}: the incident panel shows the story's open and closed counts`, () => {
    const html = FP.renderIncidents(incidents, nowMs, { noun: 'zone' });
    const open = (html.match(/data-status="OPEN"/g) || []).length;
    const closed = (html.match(/data-status="CLOSED"/g) || []).length;
    assert.equal(open, stage.open_incidents, `expected ${stage.open_incidents} open rows, rendered ${open}`);
    assert.equal(closed, stage.closed_incidents, `expected ${stage.closed_incidents} closed rows, rendered ${closed}`);
    assert.equal(incidents.open_count, stage.open_incidents);
  });

  check(`${stage.stage}: the banner still calls the data simulated`, () => {
    assert.match(FP.modeTitle(zones), /SIMULATED/);
    assert.match(FP.dataNotice(zones), /simulat/i);
    assert.equal(FP.dataAttribution(zones), '', 'a simulated fleet must not display a weather attribution');
    assert.match(zones.data_classification || '', /simulated/);
    // the strip must never describe the demo fleet as the India live fleet
    assert.equal(FP.zoneNounPlural(zones), 'zones');
  });

  check(`${stage.stage}: the history chart has the arc behind it`, () => {
    assert.ok(history.point_count > 0, 'the exported history window is empty');
    assert.equal(history.points.length, history.point_count);
    const values = history.points.map((point) => point.risk_score);
    const shape = FP.sparklinePath(values, 900, 220, 10);
    assert.equal(shape.points.length, values.length);
    assert.ok(shape.max >= shape.min, 'axis range inverted');
    values.forEach((value) => assert.ok(value >= 0 && value <= 1, `score ${value} is outside 0..1`));
  });
}

console.log('\nThe story never mixes fleets');

check('every stage is the same simulated fleet', () => {
  const payloads = stages.map((stage) => readJson(path.join(storyDir, `zones-${stage.stage.toLowerCase()}.json`)));
  const expectedFleet = story.fleet.slice().sort();
  payloads.forEach((zones, index) => {
    const name = stages[index].stage;
    assert.equal(zones.zone_mode, 'simulated', `${name} is not a simulated fleet`);
    assert.equal(zones.zone_count, expectedFleet.length, `${name} does not hold the whole fleet`);
    assert.deepEqual(zones.zones.map((zone) => zone.zone_id).sort(), expectedFleet,
      `${name} holds a different fleet, which would mix the demo and live fleets`);
  });
});

check('each stage leads with the zone its history was exported for', () => {
  stages.forEach((stage) => {
    const zones = readJson(path.join(storyDir, `zones-${stage.stage.toLowerCase()}.json`));
    const history = readJson(path.join(storyDir, `history-${stage.stage.toLowerCase()}.json`));
    assert.equal(zones.highest_risk_zone.zone_id, stage.leader,
      `${stage.stage} leads with a zone the story does not name`);
    assert.equal(history.zone_id, stage.leader,
      `${stage.stage}: the chart shows ${history.zone_id} while the board highlights ${stage.leader}`);
    assert.ok(expectedFleetHas(story, history.zone_id), `${history.zone_id} is not part of the demo fleet`);
  });
});

check('the leader at the peak stage is genuinely CRITICAL', () => {
  const critical = stages.find((stage) => stage.stage === 'CRITICAL');
  const zones = readJson(path.join(storyDir, 'zones-critical.json'));
  const leader = zones.zones.find((zone) => zone.zone_id === critical.leader);
  assert.equal(leader.latest.risk_class, 'CRITICAL', 'the highlighted zone is not CRITICAL at the peak');
  assert.ok(leader.latest.risk_score >= 0.80, `peak score ${leader.latest.risk_score} is below 0.80`);
});

console.log(`\n${passed} story checks passed, ${failures.length} failed`);
if (failures.length > 0) {
  failures.forEach((failure) => console.log(`  FAILED: ${failure.name}`));
  process.exit(1);
}
