#!/usr/bin/env node
/*
 * Dashboard scenario harness (development tool, not part of the site).
 *
 * dashboard_run.mjs proves the page loads and polls. This harness drives the real
 * web/lib.js + web/app.js through the states a live demo actually hits: a provider/api
 * outage, an HTTP 500, a malformed payload, an empty fleet, a zone with no reading yet, a
 * broken history route, a failing incidents route, a partial provider failure, a stale
 * feed, a mode switch and recovery.
 *
 * It asserts the properties that matter for an honest board:
 *   - the page never throws and never raises an unhandled rejection
 *   - a failure keeps the last good values instead of blanking or inventing them
 *   - the failure itself is stated where a viewer can see it
 *   - neither mode is ever labelled with the other fleet's wording
 *
 * Every dashboard bug fixed during the M4 hardening pass has a negative control here.
 *
 * Usage: node tools/dashboard_scenarios.mjs [--payload f] [--history f] [--incidents f]
 */

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createFakeDom, createRuntime } from './dashboard_dom.mjs';

const args = process.argv.slice(2);
const option = (name) => {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : null;
};

const paths = {
  live: {
    payload: option('--payload') || path.join('build', 'zones-live-weather.json'),
    history: option('--history') || path.join('build', 'history-live-weather.json'),
    incidents: option('--incidents') || path.join('build', 'incidents-live-weather.json')
  },
  demo: {
    payload: option('--demo-payload') || path.join('build', 'zones-demo-m4.json'),
    history: option('--demo-history') || path.join('build', 'history-demo-m4.json'),
    incidents: option('--demo-incidents') || path.join('build', 'incidents-demo-m4.json')
  }
};

const readJson = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));
const libSource = fs.readFileSync(path.join('web', 'lib.js'), 'utf8');
const appSource = fs.readFileSync(path.join('web', 'app.js'), 'utf8');
const live = { payload: readJson(paths.live.payload), history: readJson(paths.live.history), incidents: readJson(paths.live.incidents) };
const demo = { payload: readJson(paths.demo.payload), history: readJson(paths.demo.history), incidents: readJson(paths.demo.incidents) };

let passed = 0;
const failures = [];
const unhandled = [];

/* Routes are matched exactly (query string ignored), so '/zones' can never answer a
 * '/zones/{zone}/history' request — a prefix collision that would hide real bugs. */
const zonesRoute = '/zones';
const incidentsRoute = '/incidents';
const historyRoute = /\/history$/;

process.on('unhandledRejection', (reason) => {
  unhandled.push(reason);
});

async function check(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (error) {
    failures.push({ name, message: error.message });
    console.log(`  FAIL ${name}\n         ${error.message.split('\n')[0]}`);
  }
}

const group = (title) => console.log(`\n${title}`);

function boot(runtime) {
  const dom = createFakeDom();
  dom.window.location.search = '?api=https://floodpulse-api.example.invalid';
  dom.window.FLOODPULSE_API_BASE = 'https://floodpulse-api.example.invalid';
  const sandbox = runtime.sandbox(dom);
  vm.createContext(sandbox);
  vm.runInContext(libSource, sandbox, { filename: 'web/lib.js' });
  vm.runInContext(appSource, sandbox, { filename: 'web/app.js' });
  return dom;
}

/* Rewrite the feed ages in a live payload, so freshness/staleness can be driven without
 * touching the fixture files. */
function withFeedAge(payload, seconds, attemptSeconds) {
  const copy = JSON.parse(JSON.stringify(payload));
  (copy.zones || []).forEach((zone) => {
    if (!zone.weather) return;
    zone.weather.cache_age_seconds = seconds;
    if (attemptSeconds !== undefined) zone.weather.last_attempt_age_seconds = attemptSeconds;
  });
  const ids = Object.keys((copy.weather || {}).cache_age_seconds || {});
  ids.forEach((id) => {
    copy.weather.cache_age_seconds[id] = seconds;
    if (attemptSeconds !== undefined) copy.weather.last_attempt_age_seconds[id] = attemptSeconds;
  });
  return copy;
}

async function main() {
  console.log(`  live fixtures: ${paths.live.payload}`);
  console.log(`  demo fixtures: ${paths.demo.payload}`);

  /* --------------------------------------------------------- outage/recovery --- */

  group('API and provider outage, then recovery');

  await (async () => {
    const runtime = createRuntime();
    let zonesAnswer = () => ({ throw: new TypeError('Failed to fetch') });
    runtime.on(zonesRoute, () => zonesAnswer());
    runtime.on(historyRoute, () => ({ body: live.history }));
    runtime.on(incidentsRoute, () => ({ body: live.incidents }));

    const dom = boot(runtime);
    await runtime.settle();
    await runtime.driveTimers();
    await runtime.settle();

    await check('a failing first poll does not throw and the page stays CONNECTING', () => {
      assert.equal(dom.text('conn-badge'), 'CONNECTING');
      assert.ok(dom.body.classList.contains('is-connecting'), 'body is not in the connecting state');
      assert.match(dom.text('conn-detail'), /last request failed: Failed to fetch/);
      assert.ok(!/LIVE WEATHER|REAL/.test(dom.text('banner-detail')),
        `a payload-less page must not claim live data (banner: "${dom.text('banner-detail')}")`);
    });

    zonesAnswer = () => ({ body: withFeedAge(live.payload, 120, 120) });
    await runtime.driveTimers();
    await runtime.settle();

    await check('recovery returns the page to LIVE with the fleet rendered', () => {
      assert.equal(dom.text('conn-badge'), 'LIVE');
      assert.ok(dom.body.classList.contains('is-live'), 'body did not return to is-live');
      const ranking = dom.html('ranking');
      live.payload.zones.forEach((zone) => {
        assert.ok(ranking.includes(zone.name), `ranking is missing ${zone.name}`);
      });
      assert.match(dom.text('brand-subtitle'), /India live-weather monitoring/);
    });

    zonesAnswer = () => ({ status: 500, body: { error: 'boom' } });
    runtime.advance(11000); // one failed poll, still inside the stale window
    await runtime.driveTimers();
    await runtime.settle();

    await check('a 500 keeps the last good values, marks the page STALE and invents nothing', () => {
      const ranking = dom.html('ranking');
      live.payload.zones.forEach((zone) => {
        assert.ok(ranking.includes(zone.name), `the last good ranking lost ${zone.name}`);
      });
      assert.equal(dom.text('conn-badge'), 'STALE', `badge reads "${dom.text('conn-badge')}"`);
      assert.match(dom.text('conn-detail'), /last good poll/);
      assert.match(dom.text('conn-detail'), /HTTP 500/, 'the failure reason is not stated');
      assert.ok(dom.body.classList.contains('is-stale'), 'body is not stale');
    });

    runtime.advance(120000);
    await runtime.driveTimers();
    await runtime.settle();

    await check('a long outage reports DISCONNECTED and keeps restating the failure', () => {
      assert.equal(dom.text('conn-badge'), 'DISCONNECTED');
      assert.match(dom.text('conn-detail'), /may be out of date/);
      assert.match(dom.text('conn-detail'), /HTTP 500/);
      assert.ok(dom.body.classList.contains('is-disconnected'));
      // regression: the outage detail used to freeze on the very first error message and
      // never mention how long the board had been failing
      assert.match(dom.text('conn-detail'), /no successful poll for \d+ min/);
    });
  })();

  /* -------------------------------------------------------- malformed input --- */

  group('Malformed and partial payloads fail safely');

  await (async () => {
    const runtime = createRuntime();
    let zonesAnswer = { body: {} };
    runtime.on(zonesRoute, () => zonesAnswer);
    runtime.on(historyRoute, () => ({ body: { point_count: 0, minutes: 60, model: 'heuristic-v1', points: [], downsampled: false } }));
    runtime.on(incidentsRoute, () => ({ body: { count: 0, open_count: 0, closed_count: 0, incidents: [] } }));

    const dom = boot(runtime);
    await runtime.settle();

    await check('an empty payload renders empty states instead of throwing', () => {
      assert.equal(dom.text('conn-badge'), 'LIVE', 'the API answered, so the API is live');
      assert.match(dom.html('hero'), /Awaiting live data/);
      assert.match(dom.html('ranking'), /waiting for data/);
      assert.match(dom.text('brand-subtitle'), /Simulated sensor fleet|demo mode/,
        'an unknown fleet must not be captioned as the live India fleet');
      assert.match(dom.text('banner-detail'), /simulat/i);
      assert.equal(dom.text('attribution'), '', 'an unknown fleet must not display an attribution');
    });

    zonesAnswer = { body: { zones: [] } };
    await runtime.advance(6000);
    await runtime.driveTimers();
    await runtime.settle();

    await check('an empty zone list keeps the empty states and adds no fake card', () => {
      assert.match(dom.html('hero'), /Awaiting live data/);
      assert.equal(dom.html('zones'), '', 'the zone grid should be empty');
    });

    zonesAnswer = {
      body: {
        zone_mode: 'live_weather', data_classification: 'mixed', zone_count: 2, simulated: false,
        weather: { enabled: true, provider: 'open-meteo', refresh_seconds: 600, live_zones: ['city-chennai', 'city-kochi'] },
        highest_risk_zone: null,
        zones: [
          { zone_id: 'city-chennai', name: 'Chennai', zone_mode: 'live_weather', status: 'no_data', latest: null,
            thresholds: {}, metric_sources: {} },
          { zone_id: 'city-kochi', name: 'Kochi', zone_mode: 'live_weather', status: 'live',
            metric_sources: { rainfall_mm_per_hr: 'REAL' },
            thresholds: { rainfall_mm_per_hr: { watch: 7.5, critical: 30 } },
            latest: {
              risk_score: 0.42, risk_class: 'WATCH', risk_class_candidate: 'WATCH', sequence: 3,
              sensor_node_id: 'open-meteo-kochi', age_seconds: 12, explanation: 'rain rising',
              top_drivers: ['rainfall_mm_per_hr'], contributions: [], readings: { rainfall_mm_per_hr: 9.5 }
            } }
        ]
      }
    };
    await runtime.advance(6000);
    await runtime.driveTimers();
    await runtime.settle();

    await check('a zone with no reading renders a no-data card without breaking its neighbour', () => {
      const grid = dom.html('zones');
      assert.match(grid, /Chennai/);
      assert.match(grid, /Kochi/);
      const empty = dom.stub('zones', '[data-zone="city-chennai"]');
      assert.ok(empty, 'the no-data card was never patched');
      assert.equal(empty.querySelector('[data-f="risk_class"]').textContent, 'NO DATA');
      assert.equal(empty.querySelector('[data-f="age"]').textContent, 'no data');
      assert.equal(empty.querySelector('[data-f="risk_score"]').textContent, '', 'a missing score must stay blank');
      const scored = dom.stub('zones', '[data-zone="city-kochi"]');
      assert.equal(scored.querySelector('[data-f="risk_class"]').textContent, 'WATCH');
      assert.equal(scored.querySelector('[data-f="risk_score"]').textContent, '0.42');
      assert.equal(scored.querySelector('[data-f="age"]').textContent, '12 s ago');
    });

    await check('a malformed JSON body is reported, not silently swallowed', async () => {
      const broken = createRuntime();
      broken.on(zonesRoute, () => ({ raw: true }));
      broken.on(historyRoute, () => ({ body: live.history }));
      broken.on(incidentsRoute, () => ({ body: live.incidents }));
      const brokenDom = boot(broken);
      await broken.settle();
      assert.equal(brokenDom.text('conn-badge'), 'CONNECTING');
      assert.match(brokenDom.text('conn-detail'), /last request failed/);
    });
  })();

  /* ------------------------------------------------------------- freshness --- */

  group('Feed freshness, staleness and provider failure are visible');

  await (async () => {
    const runtime = createRuntime();
    let zonesAnswer = { body: withFeedAge(live.payload, 120, 120) };
    runtime.on(zonesRoute, () => zonesAnswer);
    runtime.on(historyRoute, () => ({ body: live.history }));
    runtime.on(incidentsRoute, () => ({ body: live.incidents }));

    const dom = boot(runtime);
    await runtime.settle();

    await check('a fresh live feed states its age and counts down to the next provider check', () => {
      assert.match(dom.text('conn-detail'), /weather feed 2 min old/);
      assert.match(dom.text('conn-detail'), /next provider check in \d+:\d\d/);
      assert.match(dom.text('summary-freshness'), /weather feed 2 min old/);
      assert.equal(dom.node('summary-freshness').getAttribute('data-feed-state'), 'fresh');
      assert.ok(!dom.body.classList.contains('is-stale'));
    });

    await check('the age and countdown keep moving between polls', async () => {
      // regression: the freshness line used to be re-derived from the payload on every
      // poll only, so between polls (and during an outage) it froze and the live board
      // looked broken. Local elapsed time is now added to the reported age.
      const before = dom.text('conn-detail');
      runtime.advance(60000);
      await runtime.fireDelay(1000); // one second ticker, no new payload
      const after = dom.text('conn-detail');
      assert.notEqual(after, before, `the freshness line never changed (still "${after}")`);
      assert.match(after, /weather feed 3 min old/);
      assert.match(dom.text('summary-freshness'), /weather feed 3 min old/);
    });

    zonesAnswer = { body: withFeedAge(live.payload, 1500, 1500) };
    await runtime.advance(6000);
    await runtime.driveTimers();
    await runtime.settle();

    await check('a stale feed is reported while the API itself stays live', () => {
      assert.equal(dom.text('conn-badge'), 'LIVE', 'the API is still answering');
      assert.match(dom.text('conn-detail'), /weather feed 25 min old/);
      assert.match(dom.text('conn-detail'), /missed a full refresh cycle/);
      assert.equal(dom.node('summary-freshness').getAttribute('data-feed-state'), 'stale');
      assert.ok(dom.node('data-banner').classList.contains('is-feed-degraded'),
        'the banner does not flag a stale feed');
    });

    const partial = withFeedAge(live.payload, 700, 700);
    const failedZone = partial.zones[0].zone_id;
    partial.zones[0].weather.last_attempt_error = 'http_503';
    partial.weather.last_attempt_error[failedZone] = 'http_503';
    zonesAnswer = { body: partial };
    await runtime.advance(6000);
    await runtime.driveTimers();
    await runtime.settle();

    await check('a partial provider failure is per zone and never blanks the fleet', () => {
      assert.match(dom.text('conn-detail'), /latest weather fetch failed \(http_503\)/);
      const ranking = dom.html('ranking');
      live.payload.zones.forEach((zone) => {
        assert.ok(ranking.includes(zone.name), `the ranking lost ${zone.name} because one zone failed`);
      });
      assert.match(dom.text('summary-weather'), /fetch failing on 1 zone|http_503/);
      assert.equal(dom.node('summary-freshness').getAttribute('data-feed-state'), 'failed');
      const card = dom.stub('zones', '[data-zone="' + failedZone + '"]');
      assert.match(card.querySelector('[data-f="provenance"]').innerHTML, /fetch failed/,
        'the card must show the failed fetch in its provenance line');
    });

    const off = JSON.parse(JSON.stringify(live.payload));
    off.weather.enabled = false;
    zonesAnswer = { body: off };
    await runtime.advance(6000);
    await runtime.driveTimers();
    await runtime.settle();

    await check('with the provider switched off the page says so and keeps the last real values', () => {
      assert.match(dom.text('conn-detail'), /provider disabled/);
      assert.match(dom.text('summary-weather'), /last fetched values/);
      assert.ok(!/every metric is simulated/.test(dom.text('summary-weather')),
        'a live fleet with the provider off must not be described as fully simulated');
      assert.equal(dom.node('data-banner').classList.contains('is-feed-off'), true);
    });
  })();

  /* ---------------------------------------------------------------- history --- */

  group('History failures are contained instead of retried on every poll');

  await (async () => {
    const runtime = createRuntime();
    runtime.on(zonesRoute, () => ({ body: withFeedAge(live.payload, 120, 120) }));
    runtime.on(incidentsRoute, () => ({ body: live.incidents }));
    runtime.on(historyRoute, () => ({ status: 404, body: { error: 'unknown_zone' } }));

    const dom = boot(runtime);
    await runtime.settle();

    await check('a failing history route reports the reason and the retry backoff', () => {
      assert.equal(runtime.count(historyRoute), 1, `expected one history attempt, saw ${runtime.count(historyRoute)}`);
      assert.match(dom.text('history-meta'), /history unavailable \(HTTP 404\)/);
      assert.match(dom.text('history-meta'), /retrying in \d+ s/);
    });

    await check('eight further polls inside the backoff window do not re-request history', async () => {
      for (let i = 0; i < 8; i += 1) {
        runtime.advance(5000); // 40 s total, still inside HISTORY_RETRY_MS
        await runtime.driveTimers();
        await runtime.settle();
      }
      assert.equal(runtime.count(historyRoute), 1,
        `history was requested ${runtime.count(historyRoute)} times inside one backoff window`);
      assert.ok(runtime.count(zonesRoute) >= 6, 'the zone poll itself must continue');
    });

    await check('an explicit city click retries history immediately', async () => {
      const before = runtime.count(historyRoute);
      dom.fire(dom.node('history-zone'), 'click', {
        target: dom.targetWith('[data-zone-pick]', 'data-zone-pick', 'city-kochi')
      });
      await runtime.settle();
      assert.equal(runtime.count(historyRoute), before + 1, 'a user action must not be throttled');
      assert.match(runtime.lastRequest(), /city-kochi\/history/);
    });

    await check('a backoff window that expires does retry on its own', async () => {
      const before = runtime.count(historyRoute);
      runtime.advance(50000);
      await runtime.driveTimers();
      await runtime.settle();
      assert.equal(runtime.count(historyRoute), before + 1, 'history never retried after the backoff expired');
    });
  })();

  await (async () => {
    const runtime = createRuntime();
    let historyAnswer = { body: { point_count: 0, minutes: 60, model: 'heuristic-v1', points: [], downsampled: false } };
    runtime.on(zonesRoute, () => ({ body: withFeedAge(live.payload, 120, 120) }));
    runtime.on(incidentsRoute, () => ({ body: live.incidents }));
    runtime.on(historyRoute, () => historyAnswer);

    const dom = boot(runtime);
    await runtime.settle();

    await check('an empty history window states 0 points instead of showing a broken chart', () => {
      assert.match(dom.text('history-meta'), /0 points/);
      assert.match(dom.text('history-meta'), /full resolution/);
    });

    historyAnswer = {
      body: {
        point_count: 1, minutes: 60, model: 'heuristic-v1', downsampled: false,
        points: [{ timestamp: '2026-10-15T17:00:00Z', risk_score: 0.5, risk_class: 'WATCH' }]
      }
    };
    runtime.advance(31000);
    await runtime.driveTimers();
    await runtime.settle();

    await check('hovering a history point that carries no readings does not throw', () => {
      // regression: an unguarded point.readings.X dereference threw inside mousemove
      const canvas = dom.node('history-canvas');
      const before = dom.html('history-tooltip');
      dom.fire(canvas, 'mousemove', { clientX: 43, clientY: 100 });
      const tooltip = dom.html('history-tooltip');
      assert.ok(tooltip.length > 0, 'the tooltip was never filled');
      assert.match(tooltip, /score 0.50/);
      // a point with no stored readings must not invent dashes for five channels
      assert.match(tooltip, /no sensor readings stored for this point/);
      assert.ok(!/rain –|water –/.test(tooltip), 'the tooltip padded out values it does not have');
      assert.ok(before !== undefined);
      assert.equal(dom.node('history-tooltip').hidden, false);
      dom.fire(canvas, 'mouseleave', {});
      assert.equal(dom.node('history-tooltip').hidden, true, 'the tooltip did not hide on mouseleave');
    });
  })();

  /* ---------------------------------------------------- hostile payloads --- */

  group('Hostile and malformed payload content cannot break or inject into the board');

  await (async () => {
    // The provider name is a payload string that lands in the hero's empty-fleet hint. It used
    // to be interpolated into innerHTML unescaped, so a provider of "<img src=x onerror=…>"
    // executed in the dashboard's origin. The route is reachable through the documented
    // ?api= override, which points the page at any JSON the viewer names.
    const hostile = createRuntime();
    hostile.on(zonesRoute, () => ({
      body: {
        zone_mode: 'live_weather',
        zone_count: 0,
        zones: [],
        highest_risk_zone: null,
        weather: {
          enabled: true,
          provider: '<img src=x onerror="window.__xss = true">',
          refresh_seconds: 600,
          live_zones: []
        }
      }
    }));
    hostile.on(incidentsRoute, () => ({ body: live.incidents }));
    hostile.on(historyRoute, () => ({ body: live.history }));

    const dom = boot(hostile);
    await hostile.settle();

    await check('an injection attempt in a payload string is escaped, not executed', () => {
      const hero = dom.html('hero');
      assert.ok(hero.length > 0, 'the hero rendered nothing');
      // The payload text must be present as *text*: an escaped '<img' is the fix, a live
      // '<img src=x' would be markup the browser is free to run.
      assert.match(hero, /&lt;img src=x onerror=/, 'the provider name was not escaped');
      assert.ok(!/<img\b/i.test(hero), `the hero rendered a live tag: ${hero.slice(0, 200)}`);
      assert.ok(!/<[a-z]+[^>]*\son[a-z]+=/i.test(hero), 'an event handler attribute survived into the DOM');
      assert.equal(dom.window.__xss, undefined, 'the injected handler ran');
    });

    // A null in a list is the same class of problem without the injection: it must be skipped,
    // not thrown on, and the rest of the fleet has to keep rendering.
    const nulls = createRuntime();
    nulls.on(zonesRoute, () => ({
      body: {
        ...live.payload,
        zones: [null, ...(live.payload.zones || [])],
        zone_ranking: [null, ...(live.payload.zone_ranking || [])]
      }
    }));
    nulls.on(incidentsRoute, () => ({ body: { ...live.incidents, incidents: [null, ...(live.incidents.incidents || [])] } }));
    nulls.on(historyRoute, () => ({ body: live.history }));

    const nullDom = boot(nulls);
    await nulls.settle();

    await check('a null entry in a list is skipped and the rest of the board still renders', () => {
      assert.equal(nullDom.text('conn-badge'), 'LIVE',
        `a null entry stopped the board updating (badge "${nullDom.text('conn-badge')}", detail "${nullDom.text('conn-detail')}")`);
      assert.ok(nullDom.html('zones').length > 0, 'the zone grid went blank');
      assert.ok(nullDom.html('incidents').length > 0, 'the incident panel went blank');
      assert.ok(!/undefined/.test(nullDom.html('zones')), 'the zone grid printed "undefined"');
      assert.ok(!/undefined/.test(nullDom.html('incidents')), 'the incident panel printed "undefined"');
    });
  })();

  /* -------------------------------------------------------------- incidents --- */

  group('The incident panel degrades honestly');

  await (async () => {
    const runtime = createRuntime();
    runtime.on(zonesRoute, () => ({ body: withFeedAge(live.payload, 120, 120) }));
    runtime.on(historyRoute, () => ({ body: live.history }));
    let incidentsAnswer = { body: live.incidents };
    runtime.on(incidentsRoute, () => incidentsAnswer);

    const dom = boot(runtime);
    await runtime.settle();

    await check('the loaded incident list is rendered from the payload', () => {
      assert.match(dom.text('incidents-meta'), /incident/);
      assert.ok(dom.html('incidents').length > 0, 'the incident list is empty');
    });

    const emptyIncidents = { count: 0, open_count: 0, closed_count: 0, incidents: [], alerting: live.incidents.alerting };
    incidentsAnswer = { body: emptyIncidents };
    runtime.advance(11000);
    await runtime.driveTimers();
    await runtime.settle();

    await check('an empty incident list shows an explicit empty state, never a blank panel', () => {
      const html = dom.html('incidents');
      assert.ok(html.length > 0, 'the incident panel went blank');
      assert.match(html, /No flood incidents recorded yet/);
      // and it uses the fleet's own noun
      assert.match(html, /when a city's effective risk class/);
    });

    incidentsAnswer = { status: 503, body: { error: 'unavailable' } };
    runtime.advance(11000);
    await runtime.driveTimers();
    await runtime.settle();

    await check('a failing incidents route keeps the last good list and says it is unavailable', () => {
      assert.match(dom.text('incidents-meta'), /incidents unavailable \(HTTP 503\)/);
      assert.match(dom.text('incidents-meta'), /last loaded list/);
      assert.ok(dom.html('incidents').length > 0, 'the panel was blanked by the failure');
    });
  })();

  /* ------------------------------------------------------------ mode switch --- */

  group('Switching the fleet re-labels the whole board');

  await (async () => {
    const runtime = createRuntime();
    let zonesAnswer = { body: withFeedAge(live.payload, 120, 120) };
    let incidentsAnswer = { body: live.incidents };
    let historyAnswer = { body: live.history };
    runtime.on(zonesRoute, () => zonesAnswer);
    runtime.on(incidentsRoute, () => incidentsAnswer);
    runtime.on(historyRoute, () => historyAnswer);

    const dom = boot(runtime);
    await runtime.settle();

    await check('live mode labels the board as an India live-weather fleet', () => {
      assert.match(dom.text('brand-subtitle'), /India live-weather monitoring · 5 cities/);
      assert.match(dom.text('ranking-title'), /Risk ranking by city/);
      assert.match(dom.html('zones-title'), /City detail/);
      assert.match(dom.text('banner-title'), /LIVE WEATHER/);
      assert.match(dom.text('banner-detail'), /Open-Meteo/);
      assert.match(dom.text('attribution'), /Open-Meteo\.com/);
      assert.match(dom.text('footer-disclaimer'), /not a calibrated hydrological model/);
    });

    zonesAnswer = { body: demo.payload };
    incidentsAnswer = { body: demo.incidents };
    historyAnswer = { body: demo.history };
    runtime.advance(6000);
    await runtime.driveTimers();
    await runtime.settle();

    await check('demo mode re-labels the board and never renders a REAL claim', () => {
      assert.match(dom.text('brand-subtitle'), /Simulated sensor fleet/);
      assert.match(dom.text('banner-title'), /SIMULATED DATA/);
      assert.match(dom.text('banner-detail'), /simulated by the FloodPulse simulator/);
      assert.equal(dom.text('attribution'), '', 'a demo payload has no attribution to show');
      assert.match(dom.text('summary-freshness'), /simulator feed/);
      demo.payload.zones.forEach((zone) => {
        const card = dom.stub('zones', '[data-zone="' + zone.zone_id + '"]');
        if (!card) return;
        const provenance = card.querySelector('[data-f="provenance"]').innerHTML;
        assert.ok(!/REAL/.test(provenance), `a demo zone (${zone.zone_id}) rendered a REAL chip`);
        assert.match(provenance, /SIMULATED/);
      });
    });

    zonesAnswer = { body: withFeedAge(live.payload, 300, 300) };
    incidentsAnswer = { body: live.incidents };
    historyAnswer = { body: live.history };
    runtime.advance(6000);
    await runtime.driveTimers();
    await runtime.settle();

    await check('switching back to live mode restores the live labels and the feed line', () => {
      assert.match(dom.text('brand-subtitle'), /India live-weather monitoring/);
      assert.match(dom.text('banner-title'), /LIVE WEATHER/);
      assert.equal(dom.text('conn-badge'), 'LIVE');
      assert.match(dom.text('summary-freshness'), /weather feed 5 min old/);
    });

    await check('a fleet change rebuilds the cards instead of leaving stale ones', () => {
      const shrunken = JSON.parse(JSON.stringify(withFeedAge(live.payload, 300, 300)));
      const dropped = shrunken.zones[2].name;
      shrunken.zones = shrunken.zones.slice(0, 2);
      shrunken.zone_count = 2;
      zonesAnswer = { body: shrunken };
      runtime.advance(6000);
      return runtime.driveTimers().then(() => runtime.settle()).then(() => {
        const grid = dom.html('zones');
        assert.ok(grid.includes(shrunken.zones[0].name));
        assert.ok(!grid.includes(dropped), `a removed zone (${dropped}) is still on the board`);
      });
    });
  })();

  /* ----------------------------------------------------------- interactions --- */

  group('Interactions still work');

  await (async () => {
    const runtime = createRuntime();
    runtime.on(zonesRoute, () => ({ body: withFeedAge(live.payload, 120, 120) }));
    runtime.on(incidentsRoute, () => ({ body: live.incidents }));
    runtime.on(historyRoute, () => ({ body: live.history }));

    const dom = boot(runtime);
    await runtime.settle();

    await check('the history panel defaults to the highest-risk city', () => {
      const expected = live.payload.highest_risk_zone ? live.payload.highest_risk_zone.zone_id : live.payload.zones[0].zone_id;
      assert.match(runtime.lastRequest(), new RegExp('/zones/' + expected + '/history'),
        `history should default to ${expected}, saw ${runtime.lastRequest()}`);
    });

    await check('clicking a range button switches the window and reloads', async () => {
      const before = runtime.count(historyRoute);
      dom.fire(dom.node('history-range'), 'click', {
        target: dom.targetWith('[data-range]', 'data-range', '360')
      });
      await runtime.settle();
      assert.equal(runtime.count(historyRoute), before + 1);
      assert.match(runtime.lastRequest(), /minutes=360/);
    });

    await check('resizing the window repaints both canvases without throwing', () => {
      dom.fire(dom.window, 'resize', {});
      dom.fire(dom.node('history-canvas'), 'mousemove', { clientX: 120, clientY: 90 });
      dom.fire(dom.window, 'resize', {});
    });

    await check('returning to the tab triggers an immediate poll and refresh', async () => {
      const beforeZones = runtime.count(zonesRoute);
      const beforeHistory = runtime.count(historyRoute);
      dom.fire(dom.document, 'visibilitychange', {});
      await runtime.settle();
      assert.ok(runtime.count(zonesRoute) > beforeZones, 'no poll was issued on visibilitychange');
      assert.ok(runtime.count(historyRoute) > beforeHistory, 'history was not refreshed on visibilitychange');
    });

    await check('a hung request is not duplicated while it is still pending', async () => {
      // freeze the zone route: the pending poll cannot complete, so the in-flight guard has
      // to collapse every further tick instead of stacking duplicate requests
      const frozen = createRuntime();
      frozen.on(zonesRoute, () => ({ body: withFeedAge(live.payload, 120, 120) }));
      frozen.on(incidentsRoute, () => ({ body: live.incidents }));
      frozen.on(historyRoute, () => ({ body: live.history }));
      const frozenDom = boot(frozen);
      await frozen.settle();
      const settled = frozen.count(zonesRoute);

      frozen.hold(zonesRoute);
      for (let i = 0; i < 3; i += 1) {
        await frozen.advance(2000); // 6 s, still inside the 9 s fetch timeout
        await frozen.driveTimers();
      }
      assert.equal(frozen.count(zonesRoute) - settled, 1,
        `the hung poll was duplicated (${frozen.count(zonesRoute) - settled} requests)`);
      // six seconds without an answer is not yet a problem, so the board stays LIVE
      assert.equal(frozenDom.text('conn-badge'), 'LIVE');
    });

    await check('a hung request is aborted by the page timeout and polling recovers', async () => {
      // Same frozen route, now taken past the 9 s fetch timeout: the AbortController fires,
      // the failure surfaces, and the next tick polls again instead of waiting forever.
      const frozen = createRuntime();
      frozen.on(zonesRoute, () => ({ body: withFeedAge(live.payload, 120, 120) }));
      frozen.on(incidentsRoute, () => ({ body: live.incidents }));
      frozen.on(historyRoute, () => ({ body: live.history }));
      const frozenDom = boot(frozen);
      await frozen.settle();
      const settled = frozen.count(zonesRoute);

      frozen.hold(zonesRoute);
      await frozen.advance(2000);
      await frozen.driveTimers();
      assert.equal(frozen.count(zonesRoute) - settled, 1, 'the poll was retried before the timeout');

      await frozen.advance(10000); // the 9 s fetch timeout is now due, so the abort fires
      assert.match(frozenDom.text('conn-detail'), /aborted/,
        `the timeout is not reported in the status line ("${frozenDom.text('conn-detail')}")`);

      frozen.release();
      await frozen.advance(6000);
      await frozen.driveTimers();
      await frozen.settle();
      assert.ok(frozen.count(zonesRoute) - settled >= 2, 'polling did not resume after the abort');
      assert.equal(frozenDom.text('conn-badge'), 'LIVE', 'the page did not recover after the abort');
    });
  })();

  /* ----------------------------------------------------- simulated fleet --- */

  group('The simulated demo fleet is described as zones, never as cities');

  await (async () => {
    // The fallback demo path: three simulated zones, one of them not reporting. The copy has
    // to follow the fleet, or the page promises Indian cities while showing demo zones.
    const runtime = createRuntime();
    const demo = {
      zone_mode: 'simulated', data_classification: 'simulated', zone_count: 3, simulated: true,
      generated_at: new Date().toISOString(),
      weather: { enabled: false, provider: null, refresh_seconds: null, live_zones: [] },
      highest_risk_zone: { zone_id: 'zone-old-town-canal', name: 'Old Town Canal', risk_class: 'WARNING' },
      zones: [
        { zone_id: 'zone-old-town-canal', name: 'Old Town Canal', zone_mode: 'simulated', status: 'live',
          simulated: true, metric_sources: {},
          latest: { risk_score: 0.71, risk_class: 'WARNING', sequence: 8, sensor_node_id: 'sim-oldtown',
            age_seconds: 30, readings: { rainfall_mm_per_hr: 22.5, water_level_m: 2.4 }, top_drivers: [] } },
        { zone_id: 'zone-harbor-east', name: 'Harbor East', zone_mode: 'simulated', status: 'live',
          simulated: true, metric_sources: {},
          latest: { risk_score: 0.38, risk_class: 'WATCH', sequence: 8, sensor_node_id: 'sim-harbor',
            age_seconds: 30, readings: { rainfall_mm_per_hr: 6.0, water_level_m: 1.1 }, top_drivers: [] } },
        { zone_id: 'zone-riverside-north', name: 'Riverside North', zone_mode: 'simulated', status: 'no_data',
          simulated: true, metric_sources: {}, latest: null }
      ]
    };
    runtime.on(zonesRoute, () => ({ body: demo }));
    runtime.on(historyRoute, () => ({ body: { point_count: 0, minutes: 60, model: 'heuristic-v1', points: [], downsampled: false } }));
    runtime.on(incidentsRoute, () => ({ body: { count: 0, open_count: 0, closed_count: 0, incidents: [] } }));

    const dom = boot(runtime);
    await runtime.settle();

    await check('the overview speaks about zones when the fleet is simulated', () => {
      const surfaces = {
        intro: dom.text('ov-intro'),
        headline: dom.text('ov-headline'),
        subline: dom.text('ov-subline'),
        stripTitle: dom.text('band-strip-title'),
        cardsTitle: dom.text('city-cards-title'),
        citiesLede: dom.text('cities-lede'),
        incidentHeader: dom.text('incidents-zone-header'),
        stats: dom.html('ov-stats').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ')
      };
      Object.entries(surfaces).forEach(([where, text]) => {
        assert.ok(!/\bcit(y|ies)\b/i.test(text), `the ${where} calls a simulated zone a city: "${text.trim()}"`);
      });
      assert.match(surfaces.intro, /3 zones/);
      assert.ok(!/Open-Meteo|\blive rainfall\b/.test(surfaces.intro),
        'a simulated fleet must not name a live weather provider');
      assert.equal(surfaces.stripTitle, 'Zones by risk level');
      assert.equal(surfaces.cardsTitle, 'Zone status');
      assert.equal(surfaces.incidentHeader, 'Zone');
      assert.match(surfaces.citiesLede, /^Every monitored zone /);
      assert.match(surfaces.stats, /Zones reporting/);
      assert.match(surfaces.stats, /Highest risk zone/);
      assert.match(surfaces.subline, /of 3 zones are reporting/);
      assert.match(surfaces.headline, /\bzones?\b/i);
    });

    await check('the simulated cards and banner keep saying they are simulated', () => {
      assert.match(dom.text('banner-detail'), /simulat/i);
      assert.match(dom.html('zones'), /Old Town Canal/);
      const quiet = dom.stub('zones', '[data-zone="zone-riverside-north"]');
      assert.equal(quiet.querySelector('[data-f="risk_class"]').textContent, 'NO DATA',
        'the zone without a reading must not look like a scored zone');
      assert.equal(quiet.querySelector('[data-f="age"]').textContent, 'no data');
    });
  })();

  /* ------------------------------------------------- announcements and cost --- */

  group('The status line ticks without a screen reader reading it every second');

  await (async () => {
    const runtime = createRuntime();
    let zonesAnswer = { body: withFeedAge(live.payload, 120, 120) };
    runtime.on(zonesRoute, () => zonesAnswer);
    runtime.on(historyRoute, () => ({ body: live.history }));
    runtime.on(incidentsRoute, () => ({ body: live.incidents }));

    const dom = boot(runtime);
    await runtime.settle();

    await check('seventy clock ticks move the visible line and never the announced one', async () => {
      // regression: the whole `.conn` block was `role="status" aria-live="polite"` and its
      // text carried a seconds-resolution age and countdown, so the top bar was re-announced
      // about sixty times a minute, forever, to anyone using a screen reader. The polls keep
      // succeeding here (LIVE_MS is 10 s), so nothing should be announced during the loop.
      const announcedBefore = dom.text('conn-announce');
      assert.ok(announcedBefore.length > 0, 'the page announced nothing at all on a healthy poll');
      for (let second = 1; second <= 70; second += 1) {
        await runtime.advance(1000);
        await runtime.fireDelay(1000); // the one-second ticker
        if (second % 5 === 0) await runtime.fireDelay(5000); // a healthy poll, so the API stays live
      }
      assert.equal(dom.text('conn-badge'), 'LIVE', 'the API was answering, so the badge must say LIVE');
      assert.equal(dom.text('conn-announce'), announcedBefore,
        'the polite region was rewritten while only the clock moved');
      // The visible line must still tick between polls: sampling it right after a poll and
      // three seconds later is where the seconds-resolution countdown shows up.
      await runtime.advance(1000);
      await runtime.fireDelay(5000);
      const afterPoll = dom.text('conn-detail');
      for (let second = 0; second < 3; second += 1) {
        await runtime.advance(1000);
        await runtime.fireDelay(1000);
      }
      assert.notEqual(dom.text('conn-detail'), afterPoll, 'the visible status line is frozen');
      assert.match(dom.text('conn-detail'), /next provider check in \d+:\d\d/);
    });

    await check('a real state change is still announced, with the reason', async () => {
      const before = dom.text('conn-announce');
      zonesAnswer = { throw: new TypeError('Failed to fetch') };
      await runtime.advance(60000 * 2);
      await runtime.driveTimers();
      await runtime.settle();
      assert.equal(dom.text('conn-badge'), 'DISCONNECTED');
      assert.notEqual(dom.text('conn-announce'), before, 'the disconnection was never announced');
      assert.match(dom.text('conn-announce'), /no successful poll/);
      assert.match(dom.text('conn-announce'), /Failed to fetch/);
    });
  })();

  group('A reading that is too old to describe now is never presented as current');

  await (async () => {
    const staleId = live.payload.zones[0].zone_id;
    const staleName = live.payload.zones[0].name;
    const aged = JSON.parse(JSON.stringify(live.payload));
    aged.stale_zone_ids = [staleId];
    aged.stale_after_seconds = 1800;
    aged.highest_risk_zone.age_seconds = 3600;
    aged.highest_risk_zone.stale = true;
    aged.highest_risk_zone.zone_id = staleId;
    aged.highest_risk_zone.name = staleName;
    aged.zones = aged.zones.map((zone) => {
      if (zone.zone_id !== staleId) return zone;
      zone.latest.age_seconds = 3600;
      return zone;
    });
    const openIncidents = JSON.parse(JSON.stringify(live.incidents));
    const open = (openIncidents.incidents || []).find((incident) => incident.current_status === 'OPEN');
    if (open) {
      open.zone_id = staleId;
      open.zone_name = staleName;
    }

    const runtime = createRuntime();
    runtime.on(zonesRoute, () => ({ body: aged }));
    runtime.on(historyRoute, () => ({ body: live.history }));
    runtime.on(incidentsRoute, () => ({ body: openIncidents }));
    const dom = boot(runtime);
    await runtime.settle();

    await check('the highest-risk card carries the age of the reading, and flags it', () => {
      const stats = dom.html('ov-stats');
      assert.match(stats, /Highest risk/);
      assert.match(stats, /reading 60 min ago/,
        `the leading score has no age next to it: ${stats.slice(0, 400)}`);
      assert.match(stats, /not reporting recently/,
        'a reading an hour old is presented without any hint that the zone is silent');
    });

    await check('an open incident on a silent zone says the zone stopped reporting', () => {
      const rows = dom.html('alerts-active');
      if (!open) {
        assert.ok(rows.length >= 0, 'the fixture has no open incident to check');
        return;
      }
      assert.match(rows, /zone not reporting/,
        `the alert does not mention the silent zone: ${rows.slice(0, 400)}`);
    });
  })();

  await (async () => {
    const runtime = createRuntime();
    runtime.on(zonesRoute, () => ({ body: live.payload }));
    runtime.on(historyRoute, () => ({ body: live.history }));
    runtime.on(incidentsRoute, () => ({ body: live.incidents }));
    const dom = boot(runtime);
    await runtime.settle();

    await check('a fresh fleet carries neither marker', () => {
      const stats = dom.html('ov-stats');
      assert.ok(!/not reporting recently/.test(stats),
        'the marker appears on a healthy fleet, so it means nothing');
      const rows = dom.html('alerts-active');
      assert.ok(!/zone not reporting/.test(rows),
        'the silent-zone marker appears on a healthy fleet');
    });
  })();

  group('One idle tab does not fetch the same thing twice');

  await (async () => {
    const runtime = createRuntime();
    runtime.on(zonesRoute, () => ({ body: withFeedAge(live.payload, 60, 60) }));
    runtime.on(historyRoute, () => ({ body: live.history }));
    runtime.on(incidentsRoute, () => ({ body: live.incidents }));

    const dom = boot(runtime);
    await runtime.settle();

    await check('a simulated minute fetches each route at its own cadence', async () => {
      // regression: poll() refreshed incidents whenever the 5 s poll crossed the 10 s
      // boundary, exactly when the incidents interval fired, so /incidents cost double.
      for (let second = 1; second <= 60; second += 1) {
        await runtime.advance(1000);
        await runtime.fireDelay(1000);
        if (second % 5 === 0) await runtime.fireDelay(5000);
        if (second % 10 === 0) await runtime.fireDelay(10000);
        if (second % 30 === 0) await runtime.fireDelay(30000);
      }
      const zones = runtime.count(zonesRoute);
      const incidents = runtime.count(incidentsRoute);
      const history = runtime.count(historyRoute);
      assert.ok(zones >= 11 && zones <= 14, `the 5 s poll fired ${zones} times in a minute`);
      assert.ok(incidents <= 8, `the 10 s incidents cadence cost ${incidents} requests: it is doubled`);
      assert.ok(incidents >= 6, `incidents were only fetched ${incidents} times in a minute`);
      assert.ok(history <= 4, `the 30 s history cadence cost ${history} requests`);
      assert.ok(dom.text('banner-detail').length > 0);
    });
  })();

  group('A page with no API base says so instead of loading for ever');

  await (async () => {
    const runtime = createRuntime();
    runtime.on(zonesRoute, () => ({ body: live.payload }));
    const dom = createFakeDom();
    dom.window.location.search = '';
    dom.window.FLOODPULSE_API_BASE = '';
    const sandbox = runtime.sandbox(dom);
    vm.createContext(sandbox);
    vm.runInContext(libSource, sandbox, { filename: 'web/lib.js' });
    vm.runInContext(appSource, sandbox, { filename: 'web/app.js' });
    await runtime.settle();
    await runtime.advance(30000);
    await runtime.driveTimers();
    await runtime.settle();

    await check('the badge and the sentence explain the missing API base', () => {
      // regression: start() returned before refreshConnection() and before every interval,
      // so the badge sat on CONNECTING and five panels on "waiting for data…" indefinitely.
      assert.equal(runtime.requests.length, 0, 'the page issued a request without an API base');
      assert.equal(dom.text('conn-badge'), 'DISCONNECTED', `the badge reads "${dom.text('conn-badge')}"`);
      assert.match(dom.text('conn-detail'), /no API base configured/);
      assert.match(dom.text('conn-announce'), /no API base configured/);
      assert.equal(dom.text('summary-freshness'), 'not connected');
    });
  })();

  await check('no scenario produced an unhandled promise rejection', () => {
    assert.equal(unhandled.length, 0,
      `unhandled rejections: ${unhandled.map((reason) => String(reason && reason.message || reason)).join(' | ')}`);
  });

  console.log(`\n${passed} scenario checks passed, ${failures.length} failed`);
  if (failures.length) {
    for (const failure of failures) console.log(`  - ${failure.name}: ${failure.message}`);
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(`scenario harness failed to run: ${error.stack || error.message}`);
  process.exitCode = 1;
});
