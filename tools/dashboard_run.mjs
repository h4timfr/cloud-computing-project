#!/usr/bin/env node
/*
 * Dashboard runtime harness (development tool, not part of the site).
 *
 * dashboard_check.mjs only inspects the sources; this file actually EXECUTES the real
 * web/lib.js + web/app.js in a VM against the shared fake DOM (tools/dashboard_dom.mjs)
 * and drives the page the way a person does: polling, clicking the navigation, opening a
 * city, checking the alerts list, resizing to a phone. A runtime error inside a view fails
 * here instead of silently marking the deployed page STALE.
 *
 * Usage:
 *   node tools/dashboard_run.mjs --payload build/zones-live-weather.json \
 *        [--history f] [--incidents f]
 *   node tools/dashboard_run.mjs --site http://bucket.s3-website.eu-north-1.amazonaws.com \
 *        --api https://api-id.execute-api.eu-north-1.amazonaws.com --payload build/zones-live.json
 *
 *   --app / --lib   use other asset files (e.g. an older build) to prove the check bites
 *   --site          load lib.js and app.js over HTTP from the deployed dashboard instead
 *   --api           fetch the payloads from the live API; falls back to the payload files
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

const siteUrl = option('--site');
const apiBase = option('--api');
// app.js returns early when no API base is configured, so always give it one; the fetch
// stub serves the payloads by route, which is what the page actually consumes.
const apiBaseForRun = apiBase || 'https://floodpulse-api.example.invalid';
const payloadPath = option('--payload');
const historyPath = option('--history');
const incidentsPath = option('--incidents');
const appPath = option('--app') || path.join('web', 'app.js');
const libPath = option('--lib') || path.join('web', 'lib.js');

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

async function checkAsync(name, fn) {
  try {
    await fn();
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

/* -------------------------------------------------------------------- assets --- */

async function readSource(file, label) {
  if (siteUrl) {
    const url = `${siteUrl.replace(/\/+$/, '')}/${path.basename(file)}`;
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) throw new Error(`GET ${url} -> HTTP ${response.status}`);
    const text = await response.text();
    console.log(`  loaded ${label} from ${url} (${text.length} bytes)`);
    return text;
  }
  const text = fs.readFileSync(file, 'utf8');
  console.log(`  loaded ${label} from ${file} (${text.length} bytes)`);
  return text;
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

async function loadPayloads() {
  if (apiBase) {
    try {
      const grab = async (route) => {
        const response = await fetch(apiBase.replace(/\/+$/, '') + route, { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return await response.json();
      };
      const payload = await grab('/zones');
      const incidents = await grab('/incidents?limit=20');
      const zoneId = payload.highest_risk_zone ? payload.highest_risk_zone.zone_id : null;
      const history = zoneId ? await grab(`/zones/${zoneId}/history?minutes=60`) : null;
      console.log(`  fetched live payloads from ${apiBase}`);
      return { payload, incidents, history, source: 'live API' };
    } catch (error) {
      console.log(`  live API fetch failed (${error.message}) — falling back to the payload files`);
    }
  }
  return {
    payload: readJson(payloadPath),
    incidents: incidentsPath ? readJson(incidentsPath) : null,
    history: historyPath ? readJson(historyPath) : null,
    source: 'payload files'
  };
}

/* ------------------------------------------------------------------- running --- */

const libSourceCache = { lib: null, app: null };

/*
 * Boot the real page in a fresh fake browser. `overrides` lets a scenario replace what a
 * route answers (or make it fail) without touching the harness's own fixtures.
 */
function boot({ payload, incidents, history, hash = '', viewport = 1280, overrides = {} }) {
  const dom = createFakeDom();
  const runtime = createRuntime();
  dom.setViewport(viewport);
  dom.window.FLOODPULSE_API_BASE = apiBaseForRun;
  dom.window.location.hash = hash;
  dom.window.location.search = `?api=${encodeURIComponent(apiBaseForRun)}`;

  runtime.on('/zones', () => overrides.zones
    ? overrides.zones(payload)
    : { body: payload });
  runtime.on(/^\/zones\/[^/]+\/history$/, () => overrides.history
    ? overrides.history(history)
    : { body: history });
  runtime.on('/incidents', () => overrides.incidents
    ? overrides.incidents(incidents)
    : { body: incidents });

  const sandbox = runtime.sandbox(dom);
  vm.createContext(sandbox);
  let loadError = null;
  try {
    vm.runInContext(libSourceCache.lib, sandbox, { filename: libPath });
    vm.runInContext(libSourceCache.app, sandbox, { filename: appPath });
  } catch (error) {
    loadError = error;
  }
  return { dom, runtime, sandbox, loadError };
}

/* Click a nav link the way a browser resolves event.target.closest('[data-nav]'). */
function clickNav(dom, view) {
  dom.fire(dom.node('nav-list'), 'click', {
    target: dom.targetWith('[data-nav]', 'data-nav', view),
    preventDefault() {}
  });
}

function clickCityCard(dom, zoneId) {
  dom.fire(dom.node('city-cards'), 'click', {
    target: dom.targetWith('[data-zone]', 'data-zone', zoneId),
    preventDefault() {}
  });
}

function openCity(dom, zoneId) {
  dom.window.location.hash = `#/cities/${zoneId}`;
  dom.fire(dom.window, 'hashchange', {});
}

const cityCardsIn = (html) => (html.match(/class="card city-card/g) || []).length;
const tilesIn = (html) => (html.match(/class="tile metric-tile/g) || []).length;
const alertRowsIn = (html) => (html.match(/class="alert-row/g) || []).length;

async function main() {
  libSourceCache.lib = await readSource(libPath, 'lib.js');
  libSourceCache.app = await readSource(appPath, 'app.js');
  const { payload, incidents, history, source } = await loadPayloads();
  console.log(`  payload source: ${source}`);

  const app = boot({ payload, incidents, history });
  const { dom, runtime } = app;
  await runtime.settle();

  group('Executing the dashboard assets against the payload');
  check('lib.js and app.js load and start without throwing', () => {
    assert.equal(app.loadError, null, app.loadError ? String(app.loadError.message) : '');
    assert.ok(runtime.requests.length > 0, 'the dashboard never issued a request');
  });

  check('the first render issued exactly the three read routes this page needs', () => {
    assert.equal(runtime.count('/zones'), 1, `GET /zones called ${runtime.count('/zones')} times`);
    assert.equal(runtime.count('/incidents'), 1, 'GET /incidents was not called once');
    assert.ok(runtime.countMatching('/history') <= 1, 'history was fetched more than once at startup');
    runtime.requests.forEach((url) => {
      assert.ok(!/\/readings/.test(url), `the page called the ingestion route: ${url}`);
    });
  });

  check('the first poll renders the risk ranking for every zone', () => {
    const html = dom.html('ranking');
    assert.match(html, /rank-row/, 'ranking panel is still the static placeholder');
    for (const zone of payload.zones) {
      assert.ok(html.includes(zone.name), `ranking omitted ${zone.name}`);
    }
    const rows = (html.match(/class="rank-row/g) || []).length;
    assert.equal(rows, payload.zones.length, `expected ${payload.zones.length} ranking rows, found ${rows}`);
  });

  check('the ranking is ordered by risk score, highest first', () => {
    const html = dom.html('ranking');
    const order = [...html.matchAll(/class="rank-row[^"]*" data-zone="([^"]+)"/g)].map((match) => match[1]);
    assert.equal(order.length, payload.zones.length);
    const scores = new Map(payload.zones.map((zone) => [zone.zone_id, zone.latest ? zone.latest.risk_score : -1]));
    for (let i = 1; i < order.length; i += 1) {
      assert.ok(scores.get(order[i - 1]) >= scores.get(order[i]),
        `ranking order ${order[i - 1]} before ${order[i]} is not descending`);
    }
  });

  check('the page reports LIVE after a successful poll', () => {
    assert.equal(dom.text('conn-badge'), 'LIVE', `badge reads "${dom.text('conn-badge')}"`);
    assert.ok(dom.body.classList.contains('is-live'), 'body is not marked is-live');
    assert.ok(!dom.body.classList.contains('is-stale'), 'body is marked is-stale');
  });

  check('no panel raised a runtime error', () => {
    const message = dom.text('conn-detail');
    assert.ok(!/is not defined|not a function|Cannot read/i.test(message),
      `the controller reported: ${message}`);
  });

  check('the summary and hero were filled from the payload', () => {
    assert.ok(dom.text('summary-live') && dom.text('summary-live') !== '–',
      'fleet summary was not updated');
    assert.ok(/hero-name|Awaiting live data/.test(dom.html('hero')), 'hero panel was not rendered');
  });

  check('the provenance banner and chips match the payload mode', () => {
    // DOM-level proof that the page states what is real and what is not, for whichever
    // mode the API is serving. A payload without zone_mode must degrade to unknown —
    // never to a REAL claim.
    const liveMode = payload.zone_mode === 'live_weather';
    const bannerTitle = dom.text('banner-title');
    const bannerDetail = dom.text('banner-detail');
    assert.ok(bannerTitle && bannerDetail, 'the data banner is empty');
    if (liveMode) {
      assert.match(bannerTitle, /LIVE WEATHER/);
      assert.match(bannerDetail, /Open-Meteo/);
    } else {
      assert.match(bannerTitle, /SIMULATED/);
      assert.match(bannerDetail, /SIMULATED/);
    }

    const zonesHtml = dom.html('zones');
    assert.ok(/mode-badge/.test(zonesHtml), 'zone cards carry no mode badge');
    assert.ok(/src-chip/.test(zonesHtml), 'zone cards carry no provenance chips');

    const claimsReal = /src-real/.test(zonesHtml);
    const payloadSaysReal = payload.zones.some((zone) => {
      const sources = (zone.latest && zone.latest.metric_sources) || zone.metric_sources || {};
      return Object.values(sources).indexOf('REAL') >= 0;
    });
    assert.equal(claimsReal, payloadSaysReal,
      claimsReal ? 'the page claims REAL data the payload never reported'
        : 'the payload reported REAL data the page did not show');

    if (liveMode) {
      assert.match(zonesHtml, /src-model/);
    } else if (payload.zone_mode === 'simulated') {
      assert.match(zonesHtml, /src-simulated/);
    } else {
      assert.match(zonesHtml, /src-unknown/, 'a payload without provenance must render unknown chips');
    }
    assert.ok(dom.text('summary-mode') && dom.text('summary-mode') !== '–',
      'the fleet summary does not report the mode');
  });

  group('Staying LIVE while fresh data keeps arriving');
  let polls = 0;
  for (let tick = 1; tick <= 3 && polls < 2; tick += 1) {
    await runtime.advance(5000);
    if (await runtime.driveUntilRequest(1)) polls += 1;
  }
  check('two further polls (5s apart) keep the page LIVE', () => {
    assert.equal(app.loadError, null, app.loadError ? String(app.loadError.message) : '');
    assert.equal(polls, 2, `only ${polls} additional poll(s) were driven`);
    assert.equal(dom.text('conn-badge'), 'LIVE', `badge reads "${dom.text('conn-badge')}"`);
    assert.ok(dom.body.classList.contains('is-live'), 'body lost is-live');
    assert.ok(runtime.requests.length >= 3, `expected repeated polling, saw ${runtime.requests.length} requests`);
    const html = dom.html('ranking');
    for (const zone of payload.zones) assert.ok(html.includes(zone.name), `ranking lost ${zone.name}`);
  });

  /* The board has to say what moved between polls, so drive a real change through the real
   * app: the stub serves the same objects, so mutating the payload between polls is exactly
   * what a changed API response looks like to this code. A stage fixture may already sit at
   * the top band, in which case the movement to check is the other direction. */
  const ladder = ['NORMAL', 'WATCH', 'WARNING', 'CRITICAL'];
  const rankOf = (value) => ladder.indexOf(String(value === null || value === undefined ? '' : value).toUpperCase());
  const movable = payload.zones.find((zone) => zone.latest && rankOf(zone.latest.risk_class) >= 0
    && rankOf(zone.latest.risk_class) < ladder.length - 1);
  const mover = movable || payload.zones.find((zone) => zone.latest && rankOf(zone.latest.risk_class) > 0);
  const originalClass = mover ? mover.latest.risk_class : null;
  const movingUp = Boolean(movable);
  const nextClass = movingUp ? ladder[rankOf(originalClass) + 1] : ladder[rankOf(originalClass) - 1];

  group('Saying what changed between polls');
  check('a quiet poll shows no change note', () => {
    assert.equal(dom.text('change-note'), '', `the note claims "${dom.text('change-note')}" with nothing changed`);
    assert.equal(dom.node('change-note').hidden, true, 'the empty note is still visible');
  });

  mover.latest.risk_class = nextClass;
  mover.latest.risk_score = Math.max(Number(mover.latest.risk_score) || 0, 0.9);
  await runtime.advance(5000);
  const moved = await runtime.driveUntilRequest(1);

  check('a change between two polls is named on the page', () => {
    assert.ok(moved, 'the poll after the change never ran');
    const verb = movingUp ? 'escalated' : 'eased';
    assert.match(dom.text('change-note'), new RegExp(`^${mover.name} ${verb} .+ → ${nextClass}$`),
      `the note reads "${dom.text('change-note')}" instead of naming the move`);
    assert.equal(dom.node('change-note').hidden, false, 'the note stayed hidden while the fleet moved');
    const expectedTone = movingUp ? nextClass : 'NORMAL';
    assert.equal(dom.node('change-note').className, 'change-note band-' + expectedTone.toLowerCase(),
      `the note reads "${dom.text('change-note')}" but is styled "${dom.node('change-note').className}"`);
  });

  await runtime.advance(5000);
  const settled = await runtime.driveUntilRequest(1);
  check('the change is stated once, not repeated forever', () => {
    assert.ok(settled, 'the follow-up poll never ran');
    assert.equal(dom.text('change-note'), '', `the note still claims "${dom.text('change-note')}"`);
    assert.equal(dom.node('change-note').hidden, true, 'the stale note is still on screen');
  });
  mover.latest.risk_class = originalClass;  // leave the fixture as the file describes it

  /* ============================================================ the four views === */

  const VIEWS = ['overview', 'cities', 'city', 'alerts', 'data'];
  const NAV = ['overview', 'cities', 'alerts', 'data'];

  group('Navigating between the sections');
  check('the page starts on the Overview and only the Overview is visible', () => {
    assert.equal(dom.node('view-overview').hidden, false, 'the overview is hidden at startup');
    NAV.filter((view) => view !== 'overview').forEach((view) => {
      assert.equal(dom.node(`view-${view}`).hidden, true, `the ${view} view is visible before it was chosen`);
    });
    assert.ok(dom.body.classList.contains('view-overview'), 'body does not declare the current view');
    assert.equal(dom.node('nav-overview').getAttribute('aria-current'), 'page',
      'the current section is not marked for assistive technology');
  });

  check('every section is reachable from the navigation and switches the whole view', () => {
    NAV.forEach((view) => {
      const wasHidden = dom.node(`view-${view}`).hidden;
      clickNav(dom, view);
      assert.equal(dom.node(`view-${view}`).hidden, false, `clicking ${view} did not show the ${view} view`);
      VIEWS.filter((other) => other !== view).forEach((other) => {
        assert.equal(dom.node(`view-${other}`).hidden, true,
          `the ${other} view stayed visible while ${view} was open`);
      });
      assert.ok(dom.body.classList.contains(`view-${view}`), `body does not declare view-${view}`);
      assert.equal(dom.node(`nav-${view}`).getAttribute('aria-current'), 'page',
        `${view} is not marked as the current section`);
      NAV.filter((other) => other !== view).forEach((other) => {
        assert.notEqual(dom.node(`nav-${other}`).getAttribute('aria-current'), 'page',
          `${other} is still marked as the current section`);
      });
      // a view change moves the keyboard with the content; re-choosing the view already on
      // screen must not yank focus away from the link the user just activated
      if (wasHidden) {
        assert.equal(dom.focusedId(), `view-${view}`,
          `focus stayed on ${dom.focusedId()} instead of moving into the ${view} view`);
      } else {
        assert.notEqual(dom.focusedId(), `view-${view}`,
          `focus jumped into the ${view} view although it was already open`);
      }
    });
    clickNav(dom, 'overview');
  });

  check('the URL and the page title follow the chosen section', () => {
    clickNav(dom, 'alerts');
    assert.equal(dom.window.location.hash, '#/alerts', `hash is "${dom.window.location.hash}"`);
    assert.match(dom.title(), /Alerts/);
    clickNav(dom, 'data');
    assert.equal(dom.window.location.hash, '#/data');
    assert.match(dom.title(), /Data & Methodology/);
    clickNav(dom, 'cities');
    assert.equal(dom.window.location.hash, '#/cities');
    assert.match(dom.title(), /Cities/);
    clickNav(dom, 'overview');
    assert.match(dom.title(), /FloodPulse/);
    assert.equal(dom.window.location.hash, '#/overview');
  });

  check('an unknown URL falls back to the Overview instead of blanking the page', () => {
    dom.window.location.hash = '#/definitely-not-a-section';
    dom.fire(dom.window, 'hashchange', {});
    assert.equal(dom.node('view-overview').hidden, false, 'an unknown route left every view hidden');
    dom.window.location.hash = '#/overview';
    dom.fire(dom.window, 'hashchange', {});
  });

  group('The Overview answers the public question');
  check('the overview names the fleet answer, the leader and the counts', () => {
    const headline = dom.text('ov-headline');
    assert.ok(headline && headline !== 'Connecting to the FloodPulse API…', 'the overview headline is still the placeholder');
    assert.ok(!/risk_score|heuristic|weighted|contribut/i.test(headline),
      `the headline must stay free of model jargon: "${headline}"`);

    const stats = dom.html('ov-stats');
    const cards = (stats.match(/class="stat(?: |")/g) || []).length;
    assert.ok(cards >= 4, `expected at least four headline figures, found ${cards}`);
    const leader = payload.highest_risk_zone
      ? payload.highest_risk_zone.name
      : (payload.zones[0] ? payload.zones[0].name : null);
    if (leader) assert.ok(stats.includes(leader), `the overview does not name the highest-risk city (${leader})`);
    assert.match(stats, /Active alerts/);
    assert.match(stats, /reporting/);
  });

  check('every city appears as a status card on the overview', () => {
    const html = dom.html('city-cards');
    assert.equal(cityCardsIn(html), payload.zones.length,
      `expected ${payload.zones.length} city cards, found ${cityCardsIn(html)}`);
    payload.zones.forEach((zone) => {
      assert.ok(html.includes(zone.name), `no overview card for ${zone.name}`);
      assert.ok(html.includes(`href="#/cities/${zone.zone_id}"`), `${zone.zone_id} has no link to its city view`);
    });
    assert.ok(!/weighted contribution|rise_fraction|hysteresis/i.test(html),
      'the overview cards leak model jargon');
  });

  check('the overview keeps the fleet distribution and the freshness line', () => {
    // the strip renders from the payload: real tiles, counts and a label that uses the fleet's
    // own noun (cities for the live fleet, zones for the simulated one)
    assert.match(dom.html('band-strip'), /band-tile/, 'the fleet strip did not render');
    assert.doesNotMatch(dom.html('band-strip'), /data-f="count-\w+">–</,
      'the fleet strip is still showing its static placeholders');
    const noun = payload.zone_mode === 'live_weather' ? 'cit' : 'zone';
    assert.ok(new RegExp(`of \\d+ ${noun}`).test(dom.text('band-strip-label')),
      `the fleet label reads "${dom.text('band-strip-label')}"`);
    assert.ok(dom.text('summary-freshness') && dom.text('summary-freshness') !== '–',
      'the freshness line was not rendered');
  });

  group('The Cities view lists the whole fleet');
  check('all five cities are present in the grid, with their live values', () => {
    assert.equal((dom.html('zones').match(/class="card zone-card/g) || []).length, payload.zones.length);
    payload.zones.forEach((zone) => {
      const card = dom.stub('zones', `[data-zone="${zone.zone_id}"]`);
      assert.ok(card, `no zone card for ${zone.zone_id}`);
    });
    const html = dom.html('zones');
    assert.ok(tilesIn(html) === 0, 'the city cards duplicate the metric tiles');
    assert.match(html, /data-metric=/, 'the city cards lost their metric rows');
  });

  group('Opening a city');
  payload.zones.forEach((zone) => {
    check(`the ${zone.name} view shows its own numbers`, () => {
      openCity(dom, zone.zone_id);
      assert.equal(dom.node('view-city').hidden, false, 'choosing a city did not open the city view');
      assert.equal(dom.node('view-cities').hidden, true, 'the city list stayed open behind the detail');
      assert.equal(dom.text('city-crumb'), zone.name, `the breadcrumb reads "${dom.text('city-crumb')}"`);
      assert.match(dom.title(), new RegExp(zone.name));

      const detail = dom.html('city-detail');
      assert.ok(detail.includes(zone.name), 'the detail panel does not name the city');
      if (!zone.latest) return;

      // the score and the class the API reported, formatted by the page's own helper: the
      // detail view must never recompute a risk value of its own
      const FP = app.sandbox.window.FP;
      assert.ok(detail.includes(FP.formatScore(zone.latest.risk_score)),
        `${zone.zone_id}: the detail does not show the API risk score ${FP.formatScore(zone.latest.risk_score)}`);
      assert.ok(new RegExp(zone.latest.risk_class, 'i').test(detail),
        `${zone.zone_id}: the detail does not show the effective class ${zone.latest.risk_class}`);

      // one tile per sensor channel, each with a value, a unit and a provenance chip
      assert.equal(tilesIn(detail), 5, `${zone.zone_id}: expected five metric tiles, found ${tilesIn(detail)}`);
      const METRICS = ['rainfall_mm_per_hr', 'drain_level_m', 'soil_saturation_pct', 'water_level_m', 'flow_rate_m3_s'];
      METRICS.forEach((metric) => {
        assert.ok(detail.includes(`data-metric="${metric}"`), `${zone.zone_id}: no tile for ${metric}`);
        const tileHtml = new RegExp(`data-metric="${metric}"[\\s\\S]*?</article>`).exec(detail)[0];
        const valuePart = /<p class="tile-value"[^>]*>([\s\S]*?)<\/p>/.exec(tileHtml)[1];
        const number = /<b>([^<]*)<\/b>/.exec(valuePart)[1];
        const unit = /class="tile-unit">([^<]*)</.exec(valuePart)[1];
        // compared with all whitespace removed, so this needs no opinion about whether the
        // unit is attached with a space: "0.74 m" and "73%" both reduce to the same string
        const tight = (text) => text.replace(/\s+/g, '');
        assert.equal(tight(number + unit), tight(FP.formatMetric(metric, zone.latest.readings[metric])),
          `${zone.zone_id}: the ${metric} tile reads "${number} ${unit}"`);
        assert.ok(number.length > 0 && unit.length > 0, `${zone.zone_id}: the ${metric} tile is missing value or unit`);
        // regression guard: the unit belongs in its own element, not in the number as well
        assert.ok(!number.includes(unit), `${zone.zone_id}: the ${metric} number repeats its unit`);
        assert.ok(tileHtml.includes(`src-${FP.sourceMeta(FP.metricSource(zone, metric)).key}`),
          `${zone.zone_id}: the ${metric} tile carries the wrong provenance chip`);
      });

      // the plain-language explanation: prose that names the driver, never the formula
      const why = /data-f="why">([^<]*)</.exec(detail);
      assert.ok(why && why[1].length > 20, `${zone.zone_id}: the detail has no plain-language explanation`);
      assert.match(why[1], /main reason|largest contribution/);
      const driver = (zone.latest.top_drivers || [])[0];
      if (driver) {
        assert.ok(why[1].includes(FP.metricPlain(driver).noun),
          `${zone.zone_id}: the explanation does not name its dominant driver`);
      }
      assert.ok(!/[=+]|weighted|risk_score 0\./.test(why[1]),
        `${zone.zone_id}: the plain-language explanation leaks model syntax: "${why[1]}"`);
    });
  });

  check('a city page keeps the technical detail folded away', () => {
    openCity(dom, payload.zones[0].zone_id);
    const detail = dom.html('city-detail');
    assert.match(detail, /<details class="tech"/, 'the technical section is not a native details element');
    assert.ok(!/<details[^>]*\sopen/.test(detail), 'the technical section is expanded by default');
    assert.match(detail, /<summary>Technical details/, 'the technical section has no summary to click');
    assert.match(detail, /weighted contribution/i, 'the folded table does not explain contributions');
  });

  check('clicking a city card on the overview opens that city', () => {
    clickNav(dom, 'overview');
    const target = payload.zones[payload.zones.length - 1];
    clickCityCard(dom, target.zone_id);
    assert.equal(dom.node('view-city').hidden, false, 'the city card did not open the city view');
    assert.match(dom.title(), new RegExp(target.name));
    assert.equal(dom.window.location.hash, `#/cities/${target.zone_id}`,
      `the URL reads ${dom.window.location.hash}`);
  });

  check('an unknown city id is reported instead of showing an empty page', () => {
    openCity(dom, 'city-does-not-exist');
    const detail = dom.html('city-detail');
    assert.match(detail, /not in the current fleet/, 'the unknown city is not reported');
    assert.ok(detail.includes('city-does-not-exist'), 'the unknown id is not shown');
    assert.match(detail, /Back to all cities/, 'there is no way back to the list');
  });

  group('The Alerts view separates live alerts from history');
  check('the alerts view shows the alerting status and every incident the API returned', () => {
    clickNav(dom, 'alerts');
    assert.ok(dom.text('incidents-alerting').length > 0, 'the SNS status line is empty');
    assert.match(dom.text('incidents-alerting'), /SNS alerts/);
    const all = incidents ? incidents.incidents || [] : [];
    const openCount = all.filter((incident) => incident.current_status === 'OPEN').length;
    const closedCount = all.length - openCount;
    assert.equal(alertRowsIn(dom.html('alerts-active')), openCount,
      'the active list does not match the open incidents in the payload');
    assert.equal(alertRowsIn(dom.html('alerts-closed')), closedCount,
      'the resolved list does not match the closed incidents in the payload');
    if (!openCount) {
      assert.match(dom.html('alerts-active'), /data-empty="active"/,
        'no active alerts and no empty state to say so');
    }
    if (!closedCount) {
      assert.match(dom.html('alerts-closed'), /data-empty="closed"/,
        'nothing resolved and no empty state to say so');
    }
  });

  check('a resolved incident shows its city, severity and duration', () => {
    const closed = (incidents ? incidents.incidents || [] : []).filter((incident) => incident.current_status !== 'OPEN');
    if (!closed.length) return;  // nothing resolved in this fixture: covered by the empty-state check
    const html = dom.html('alerts-closed');
    const sample = closed[0];
    assert.ok(html.includes(sample.zone_name || sample.zone_id), 'the resolved alert omits its city');
    assert.match(html, /Opened \d\d:\d\d:\d\dZ/, 'the resolved alert has no opened time');
    assert.match(html, /closed \d\d:\d\d:\d\dZ/, 'the resolved alert has no closed time');
    assert.ok(/class="alert-severity band-\w+"/.test(html), 'the resolved alert carries no severity');
  });

  await checkAsync('an incident that opens while the page is open appears as an active alert', async () => {
    const fixture = incidents ? incidents.incidents || [] : [];
    const candidate = fixture.find((incident) => incident.current_status !== 'OPEN') || null;
    if (!candidate) return;
    const alreadyOpen = fixture.filter((incident) => incident.current_status === 'OPEN').length;
    candidate.current_status = 'OPEN';
    candidate.closed_at = null;
    candidate.duration_seconds = null;
    await runtime.advance(10000);
    await runtime.driveUntilRequest(1);
    await runtime.settle();
    assert.equal(alertRowsIn(dom.html('alerts-active')), alreadyOpen + 1,
      `the reopened incident did not move into the active list (${alreadyOpen} were already open)`);
    assert.ok(dom.html('alerts-active').includes(candidate.zone_name || candidate.zone_id),
      'the active alert does not name its city');
    assert.match(dom.text('alerts-lede'), /active alert/,
      `the alerts summary still reads "${dom.text('alerts-lede')}"`);
    assert.match(dom.html('alerts-active'), /so far/, 'the open alert does not count its duration');
    candidate.current_status = 'CLOSED';  // restore the fixture
  });

  group('The Data & Methodology view states what the numbers are');
  check('the provenance legend explains every tag the API can report', () => {
    clickNav(dom, 'data');
    const legend = dom.html('provenance-legend');
    ['REAL', 'MODEL', 'SIM'].forEach((tag) => {
      assert.ok(legend.includes(`>${tag}<`), `the legend does not explain ${tag}`);
    });
    assert.match(legend, /Open-Meteo/, 'the REAL card does not name the provider');
    assert.match(legend, /simulator/i, 'the SIM card does not say where the values come from');
    // every chip is accounted for: either "Used for: …" or an explicit "not used here"
    assert.equal((legend.match(/legend-used/g) || []).length, 3,
      'each legend card must say what it is used for on this board');
    assert.match(dom.node('api-base').getAttribute('href'), /^https?:\/\//);

    // CC BY only applies while provider data is displayed. A simulated board must not
    // credit a provider it never fetched, and a live board must.
    const live = payload.zone_mode === 'live_weather';
    if (live) {
      assert.match(dom.text('attribution-detail'), /Open-Meteo/);
      assert.match(dom.text('attribution-detail'), /CC BY/);
      assert.equal(dom.node('attribution-block').hidden, false, 'the attribution card is hidden on a live board');
      assert.match(dom.text('summary-weather-provider'), /Open-Meteo/);
    } else {
      assert.equal(dom.text('attribution-detail'), '',
        `a simulated board credits "${dom.text('attribution-detail')}"`);
      assert.equal(dom.node('attribution-block').hidden, true,
        'a simulated board shows an attribution card for data it never fetched');
      assert.equal(dom.text('summary-weather-provider'), '',
        'a simulated board names a weather provider');
    }
  });

  check('the methodology page explains the model, the bands and the limitations', () => {
    const method = dom.html('method-block');
    assert.match(method, /heuristic-v1/);
    assert.match(method, /30%/, 'the rainfall weight is not stated');
    assert.match(method, /25%/, 'the water-level weight is not stated');
    assert.match(method, /0\.35/);
    assert.match(method, /0\.80/);
    assert.match(method, /CRITICAL/);
    assert.match(method, /hysteresis/i);

    const limitations = dom.html('limitations');
    assert.ok((limitations.match(/<li>/g) || []).length >= 4, 'fewer than four limitations are listed');
    assert.match(limitations, /not a calibrated hydrological model/i);
    assert.match(limitations, /not an official flood warning/i);
    assert.match(dom.text('why-simulated'), /simulator/i);
    assert.match(dom.text('why-simulated'), /rainfall stays real|physical sensors/i);
  });

  group('A phone-sized screen gets a menu instead of a squeezed bar');
  await checkAsync('the menu button opens and closes the section list', async () => {
    const phone = boot({ payload, incidents, history, viewport: 390 });
    await phone.runtime.settle();
    assert.ok(phone.dom.matches('(max-width: 760px)'), 'the fake viewport does not report a phone width');
    const toggle = phone.dom.node('nav-toggle');
    const list = phone.dom.node('nav-list');
    assert.equal(toggle.getAttribute('aria-expanded'), 'false', 'the menu claims to be open before it is');
    assert.ok(!list.classList.contains('is-open'), 'the menu list is open before it was opened');

    phone.dom.fire(toggle, 'click', { preventDefault() {} });
    assert.equal(toggle.getAttribute('aria-expanded'), 'true', 'the menu button did not report the open menu');
    assert.ok(list.classList.contains('is-open'), 'the menu list did not open');
    assert.ok(phone.dom.body.classList.contains('is-menu-open'), 'the page does not know the menu is open');

    phone.dom.fire(phone.dom.document, 'keydown', { key: 'Escape' });
    assert.equal(toggle.getAttribute('aria-expanded'), 'false', 'Escape did not close the menu');
    assert.ok(!list.classList.contains('is-open'), 'the menu stayed open after Escape');

    phone.dom.fire(toggle, 'click', { preventDefault() {} });
    clickNav(phone.dom, 'cities');
    assert.equal(phone.dom.node('view-cities').hidden, false, 'the menu link did not switch the view');
    assert.equal(toggle.getAttribute('aria-expanded'), 'false', 'choosing a section left the menu open');
    assert.ok(!list.classList.contains('is-open'), 'the menu panel is still covering the page');

    // A desktop viewport must not need the button at all: the list is laid out inline.
    const desktop = boot({ payload, incidents, history, viewport: 1440 });
    await desktop.runtime.settle();
    assert.ok(!desktop.dom.matches('(max-width: 760px)'), 'the fake viewport reports a phone width at 1440px');
    assert.ok(!desktop.dom.node('nav-list').classList.contains('is-open'),
      'a desktop page starts with the mobile menu open');
  });

  group('Losing the API must not lose the data already on screen');
  await checkAsync('a page that never reached the API says so, and invents no readings', async () => {
    const failing = boot({
      payload,
      incidents,
      history,
      overrides: { zones: () => ({ throw: new Error('Failed to fetch') }) }
    });
    await failing.runtime.settle();
    // Documented behaviour: with no successful poll yet the badge stays CONNECTING, but the
    // status line names the failure, the body is never marked live, and no view fabricates
    // a city, a score or a count.
    assert.equal(failing.dom.text('conn-badge'), 'CONNECTING',
      `the badge reads "${failing.dom.text('conn-badge')}" before any successful poll`);
    assert.match(failing.dom.text('conn-detail'), /Failed to fetch/,
      'the status line does not name the failure');
    assert.ok(!failing.dom.body.classList.contains('is-live'), 'a failed load is marked live');
    assert.ok(!/LIVE WEATHER|REAL\b/.test(failing.dom.text('banner-detail')),
      `a payload-less page claims data provenance: "${failing.dom.text('banner-detail')}"`);
    assert.equal(cityCardsIn(failing.dom.html('city-cards')), 0, 'an empty page invented city cards');
    assert.equal(failing.dom.html('zones'), '', 'the city grid should be empty');
    // the section that carries the static "Awaiting live data" placeholder must either be
    // untouched or say the same thing; it must never show a city or a score
    assert.ok(failing.dom.html('hero') === '' || /Awaiting live data/.test(failing.dom.html('hero')),
      `the hero panel reads "${failing.dom.html('hero')}"`);
    assert.ok(!/band-tile band-(watch|warning|critical)"[^>]*><span class="band-count">[1-9]/.test(failing.dom.html('band-strip')),
      'the fleet strip counted cities that never reported');
  });

  await checkAsync('a lost connection is reported and keeps the last good view', async () => {
    const flaky = boot({
      payload,
      incidents,
      history,
      overrides: {
        zones: (() => {
          let calls = 0;
          return (body) => {
            calls += 1;
            if (calls === 2) return { throw: new Error('Failed to fetch') };
            return { body };
          };
        })()
      }
    });
    await flaky.runtime.settle();
    assert.equal(flaky.dom.text('conn-badge'), 'LIVE', 'the first poll did not succeed');
    const rendered = flaky.dom.html('city-cards');
    assert.ok(cityCardsIn(rendered) === payload.zones.length, 'the first poll did not render the cities');

    await flaky.runtime.advance(6000);
    await flaky.runtime.driveUntilRequest(1);
    await flaky.runtime.settle();
    assert.ok(['STALE', 'DISCONNECTED'].includes(flaky.dom.text('conn-badge')),
      `a failed poll left the badge at "${flaky.dom.text('conn-badge')}"`);
    assert.match(flaky.dom.text('conn-detail'), /Failed to fetch/);
    assert.equal(flaky.dom.html('city-cards'), rendered,
      'the last good city view was replaced after a failed poll');

    await flaky.runtime.advance(6000);
    await flaky.runtime.driveUntilRequest(1);
    await flaky.runtime.settle();
    assert.equal(flaky.dom.text('conn-badge'), 'LIVE',
      `the page did not recover (badge reads "${flaky.dom.text('conn-badge')}")`);
  });

  await checkAsync('an empty incident list is an explicit empty state, never a blank panel', async () => {
    const quiet = boot({
      payload,
      incidents,
      history,
      overrides: { incidents: () => ({ body: { count: 0, open_count: 0, closed_count: 0, incidents: [], alerting: { enabled: true, cooldown_seconds: 900 } } }) }
    });
    await quiet.runtime.settle();
    clickNav(quiet.dom, 'alerts');
    assert.match(quiet.dom.html('alerts-active'), /data-empty="active"/, 'no empty state for active alerts');
    assert.match(quiet.dom.html('alerts-active'), /No active alerts/);
    assert.match(quiet.dom.html('alerts-closed'), /data-empty="closed"/, 'no empty state for resolved incidents');
    assert.match(quiet.dom.html('alerts-closed'), /Nothing resolved yet/);
    assert.match(quiet.dom.text('alerts-lede'), /WARNING or CRITICAL/,
      'the alerts page does not say when an alert would appear');
  });

  await checkAsync('a fleet with no readings says so on every view', async () => {
    const emptyFleet = {
      zone_mode: payload.zone_mode,
      zone_count: 0,
      generated_at: payload.generated_at,
      data_classification: payload.data_classification,
      zones: [],
      highest_risk_zone: null
    };
    const nothing = boot({ payload: emptyFleet, incidents: null, history: null, overrides: {} });
    await nothing.runtime.settle();
    assert.match(nothing.dom.html('city-cards'), /No city has reported/i,
      `the overview reads "${nothing.dom.html('city-cards')}"`);
    assert.match(nothing.dom.html('hero'), /Awaiting live data/);
    assert.equal(nothing.dom.html('zones'), '', 'the city grid should stay empty');
    assert.equal(nothing.dom.text('conn-badge'), 'LIVE', 'the API answered, so the API is live');
    assert.match(nothing.dom.text('band-strip-label'), /\d+ of \d+/,
      'the fleet label did not survive an empty fleet');
  });

  console.log(`\n${passed} runtime checks passed, ${failures.length} failed`);
  if (failures.length) {
    for (const failure of failures) console.log(`  - ${failure.name}: ${failure.message}`);
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(`runtime harness failed to run: ${error.stack || error.message}`);
  process.exitCode = 1;
});
