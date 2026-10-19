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
 *
 * With --site and no --api the harness reads the API base the deployed page declares, so
 * `node tools/dashboard_run.mjs --site <bucket-url>` verifies the real deployed site end to
 * end on its own. --payload remains the offline fixture path.
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
let apiBase = option('--api');
let apiBaseForRun = apiBase || 'https://floodpulse-api.example.invalid';
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

function readJson(file, flag) {
  if (!file) {
    throw new Error(`nothing to render for ${flag}: pass "${flag} <file>", or --api <endpoint> / --site <url> `
      + 'so the harness can fetch the payloads from a live API');
  }
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

/*
 * The deployed page declares its own public API base, so a --site run alone can discover it
 * instead of failing with a confusing type error. No default is invented: if the page does
 * not name a host, the harness says so.
 */
async function discoverApiBaseFromSite() {
  if (!siteUrl) return null;
  const url = `${siteUrl.replace(/\/+$/, '')}/index.html`;
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) return null;
  const html = await response.text();
  const declared = /FLOODPULSE_API_BASE\s*=\s*['"](https?:\/\/[^'"]+)['"]/.exec(html);
  if (declared) return declared[1];
  const host = /https?:\/\/[a-z0-9-]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com/i.exec(html);
  return host ? host[0] : null;
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
    payload: readJson(payloadPath, '--payload'),
    incidents: incidentsPath ? readJson(incidentsPath, '--incidents') : null,
    history: historyPath ? readJson(historyPath, '--history') : null,
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
  // M10: the public subscription route. A scenario can accept, throttle, fail or refuse it.
  // The default is what the deployed API answers for an accepted request.
  runtime.on('/subscriptions', () => (overrides.subscriptions
    ? overrides.subscriptions()
    : { status: 202, body: { status: 'confirmation_required', message: 'Check your email to confirm the subscription.' } }));

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
  if (!apiBase && siteUrl) {
    const discovered = await discoverApiBaseFromSite();
    if (discovered) {
      apiBase = discovered;
      console.log(`  discovered the API base from the deployed page: ${discovered}`);
    } else {
      console.log('  the deployed page does not declare an API base — falling back to the payload files');
    }
  }
  apiBaseForRun = apiBase || apiBaseForRun;
  const { payload, incidents, history, source } = await loadPayloads();

/* The fleet's own nouns: the page's sentences follow the mode, so the checks must too.
 * A simulated zone is a zone, and only the live fleet is a set of Indian cities. */
const fleetNoun = payload.zone_mode === 'live_weather' ? 'city' : 'zone';
const fleetNounPlural = payload.zone_mode === 'live_weather' ? 'cities' : 'zones';

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
    // M10: the hero is static markup the renderer patches field by field, so each field is
    // read back by its own id instead of by matching one regenerated HTML blob.
    assert.ok(dom.text('ov-headline') && dom.text('ov-headline') !== 'Connecting to the FloodPulse API…',
      'the hero headline was not filled from the payload');
    assert.match(dom.text('hero-monitor'), /monitoring \d+/,
      `the hero monitoring line is not a sentence about the fleet: "${dom.text('hero-monitor')}"`);
    assert.ok(dom.text('hero-why'), 'the hero has no plain-language reason');
    assert.match(dom.text('hero-facts'), /Main risk factor:/, 'the hero facts line lost the main risk factor');
    assert.match(dom.text('hero-state'), /risk$/, 'the hero does not name the leading city and its band');
    assert.match(dom.node('hero').getAttribute('data-band'), /^(normal|watch|warning|critical|unknown)$/,
      'the hero carries no band');
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

  check('the overview introduces the product in one plain sentence', () => {
    const intro = dom.text('ov-intro');
    assert.ok(intro && intro.length > 40, `the overview never introduces the product: "${intro}"`);
    assert.match(intro, /^FloodPulse combines/);
    assert.match(intro, /how close each one is to flooding/);
    assert.ok(!/heuristic|weight|coefficient|trend|risk_score/i.test(intro),
      `the introduction leaks model jargon: "${intro}"`);
    // the sentence must describe the mode the API is actually serving
    const live = payload.zone_mode === 'live_weather';
    if (live) {
      assert.match(intro, /Open-Meteo/, 'the live fleet must name its real rainfall source');
    } else {
      assert.ok(!/Open-Meteo|\blive rainfall\b/.test(intro),
        `a simulated fleet must not claim live weather: "${intro}"`);
    }
    if (payload.zone_count > 0) {
      assert.match(intro, new RegExp(`across ${payload.zone_count} ${live ? 'cities' : 'zones'}`),
        `the introduction lost the fleet size: "${intro}"`);
    }
  });

  check('the overview never calls a simulated zone a city', () => {
    const live = payload.zone_mode === 'live_weather';
    const plural = live ? 'zones?' : 'cities?';
    // The fourth stat card is a statement about the *provider's* coverage, not about the fleet:
    // it says "weather feed 30 min old · fetch failing on 5 zone(s)" for five cities, because the
    // provider answers per zone and the sentence is about which of those answers arrived. It is
    // lifted out before the noun rule - which is about the fleet's own noun - and asserted in its
    // own right below, so neither rule can be satisfied by breaking the other.
    const statsHtml = dom.html('ov-stats');
    const providerCard = /<article class="stat[^"]*">(?:(?!<\/article>)[\s\S])*?Weather freshness[\s\S]*?<\/article>/.exec(statsHtml);
    const fleetStats = (providerCard ? statsHtml.replace(providerCard[0], ' ') : statsHtml)
      .replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
    const surfaces = {
      headline: dom.text('ov-headline'),
      subline: dom.text('ov-subline'),
      stripTitle: dom.text('band-strip-title'),
      stats: fleetStats
    };
    if (providerCard) {
      const providerText = providerCard[0].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
      // the provider's own wording, in both modes, and never the fleet's noun
      assert.ok(!/\bcit(y|ies)\b/.test(providerText),
        `the provider coverage line names cities: "${providerText}"`);
      if (/\d+ zone\(s\)/.test(providerText)) {
        assert.match(providerText, /Weather freshness/,
          'the provider coverage sentence left its own card');
      }
    }
    Object.entries(surfaces).forEach(([where, text]) => {
      assert.ok(!new RegExp(`\\b${plural}\\b`).test(text),
        `the ${where} uses the wrong fleet noun: "${text.trim()}"`);
    });
    assert.ok(new RegExp(`\\b${live ? 'cities' : 'zones'}\\b`).test(surfaces.subline),
      `the subline must name the fleet: "${surfaces.subline}"`);
    assert.match(surfaces.stripTitle, new RegExp(`^${live ? 'Cities' : 'Zones'} by risk level$`),
      `the risk-strip title reads "${surfaces.stripTitle}"`);
    assert.match(surfaces.stats, new RegExp(`${live ? 'Cities' : 'Zones'} reporting`),
      `the reporting figure is mislabelled: "${surfaces.stats}"`);
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

  /* How many cities the Overview carries, from one place: the page's own limit, never more
   * cities than the payload has. */
  const overviewLimit = (fleet) => Math.min(5, fleet.zones.length);

  const zoneIdsIn = (html, attribute) => (html.match(new RegExp(`${attribute}="([^"]+)"`, 'g')) || [])
    .map((match) => match.replace(new RegExp(`^${attribute}="`), '').replace(/"$/, ''));

  const riskOrder = (fleet) => fleet.zones
    .map((zone) => ({ id: zone.zone_id, score: zone.latest ? zone.latest.risk_score : -1 }))
    .sort((a, b) => (b.score - a.score))
    .map((entry) => entry.id);

  check('the overview shows the highest-risk few, and a way to the rest', () => {
    // M13: twelve coastal cities are more than an overview should carry. The cards are the highest-risk
    // few, in the fleet's own order; the page says how many cities there are; and the rest are
    // one click away. The set has to be exactly right in both directions - no card outside the
    // top few, and none of the top few missing.
    const html = dom.html('city-cards');
    const limit = overviewLimit(payload);
    assert.equal(cityCardsIn(html), limit,
      `expected ${limit} city cards, found ${cityCardsIn(html)}`);
    const expected = riskOrder(payload).slice(0, limit);
    assert.deepEqual(zoneIdsIn(html, 'data-zone').slice().sort(), expected.slice().sort(),
      `the overview cards are ${zoneIdsIn(html, 'data-zone').join(', ')}, `
      + `expected the ${limit} highest-risk: ${expected.join(', ')}`);
    expected.forEach((id) => {
      const zone = payload.zones.find((entry) => entry.zone_id === id);
      assert.ok(html.includes(zone.name), `no overview card for ${zone.name}`);
      assert.ok(html.includes(`href="#/cities/${id}"`), `${id} has no link to its city view`);
    });
    assert.ok(!/weighted contribution|rise_fraction|hysteresis/i.test(html),
      'the overview cards leak model jargon');
    // the note counts the fleet, and the link leads to all of it
    assert.match(dom.text('city-cards-note'), new RegExp(`highest-risk .* of ${payload.zone_count}`),
      `the note under the cards reads "${dom.text('city-cards-note')}"`);
    const more = dom.node('city-cards-more');
    // the link's own href is static markup, so it is checked against index.html by
    // tools/dashboard_check.mjs: this fake DOM only holds what the controller wrote here
    assert.equal(more.hidden, false, 'the way to the rest of the fleet is hidden');
    assert.match(dom.text('city-cards-more'), new RegExp(`View all ${payload.zone_count} cities`),
      `the link reads "${dom.text('city-cards-more')}"`);
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

  check('the freshness figure names the feed state, and the line beneath it the age', () => {
    const stats = dom.html('ov-stats').replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/\s+/g, ' ').trim();
    // whatever lib computes for this payload is what the page must print: reading a field the
    // feed object does not carry (as "feed.ageSeconds" once did) has to fail here
    const expected = app.sandbox.window.FP.fleetOverview(payload, incidents, Date.now()).feedValue;
    assert.ok(stats.includes(expected), `the overview should show the feed state "${expected}": ${stats}`);
    assert.ok(!/\bpending\b|–/.test(stats), `the overview shows a placeholder freshness figure: ${stats}`);
    if (payload.zone_mode === 'live_weather') {
      assert.match(stats, /weather feed \d+/, 'the freshness line does not state the age of the feed');
    }
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
      // The sentence was rewritten to the shape the brief asks for - "Risk is currently WATCH.
      // Soil saturation is the main contributor, while rainfall ... remain below their watch
      // levels." - so the phrases that mark it as a reason sentence are "main contributor" and
      // the older "main reason"/"largest contribution" wording. What is asserted is the intent:
      // a sentence that names a cause, not a template.
      assert.match(why[1], /main contributor|main reason|largest contribution/,
        `${zone.zone_id}: the sentence does not name what is driving the score`);
      const driver = (zone.latest.top_drivers || [])[0];
      if (driver) {
        // case-insensitive: the driver's noun now opens a sentence ("Soil saturation is the main
        // contributor"), and the label the payload's variable maps to is a public name, not the
        // variable itself.
        assert.ok(why[1].toLowerCase().includes(FP.metricPlain(driver).noun.toLowerCase()),
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

  await checkAsync('the plain-language sentence states which way conditions are going', async () => {
    // regression: the sentence tested trendLabel() against 'rising'/'falling', which it never
    // returns, so "Conditions are getting worse" was unreachable dead code on a flood board.
    const rising = JSON.parse(JSON.stringify(payload));
    const falling = JSON.parse(JSON.stringify(payload));
    rising.zones[0].latest.trend = { water_level_delta_m: 0.4, rise_fraction: 0.5 };
    falling.zones[0].latest.trend = { water_level_delta_m: -0.4, rise_fraction: -0.5 };
    rising.zones[0].latest.explanation = 'test';
    falling.zones[0].latest.explanation = 'test';

    const up = boot({ payload: rising, incidents, history, hash: `#/cities/${rising.zones[0].zone_id}` });
    const down = boot({ payload: falling, incidents, history, hash: `#/cities/${falling.zones[0].zone_id}` });
    await up.runtime.settle();
    await down.runtime.settle();
    assert.equal(up.loadError, null, `the page did not load: ${up.loadError && up.loadError.message}`);
    const whyOf = (built) => {
      const match = /data-f="why">([^<]*)</.exec(built.dom.html('city-detail'));
      return match ? match[1] : '';
    };
    const upWhy = whyOf(up);
    const downWhy = whyOf(down);
    assert.ok(upWhy.length > 20, `the city detail has no explanation to read (got "${upWhy}")`);
    assert.match(upWhy, /getting worse/, `a rising water level reads "${upWhy}"`);
    assert.match(downWhy, /easing/, `a falling water level reads "${downWhy}"`);
    // The sentence states the top driver's value with its unit on purpose ("at 0.42 m"), so the
    // thing that must not appear is the signed trend delta and its window ("+0.40 m / 5 min").
    assert.ok(!/[+-][0-9]+\.[0-9]{2} m/.test(upWhy) && !/\/ 5 min/.test(upWhy),
      `the plain sentence leaks the trend numbers again: "${upWhy}"`);
  });

  await checkAsync('the chart can be stepped through with the arrow keys', async () => {
    // regression: the per-point values existed only behind mousemove on a canvas that could
    // not be focused, so on a phone or with a keyboard they were unreachable.
    clickNav(dom, 'cities');
    openCity(dom, payload.zones[0].zone_id);
    await runtime.settle(); // let this city's history land first
    const canvas = dom.node('history-canvas');
    assert.ok(canvas.getAttribute('aria-label'), 'the chart has no label to fall back on');
    // The bitmap must match the layout box times the device pixel ratio, or the plotted path
    // lands off the bitmap: clipped on a HiDPI screen, blurred and stretched on a 1x one.
    assert.equal(canvas.width, Math.round(canvas.clientWidth * (dom.window.devicePixelRatio || 1)),
      `the chart bitmap is ${canvas.width} px for a ${canvas.clientWidth} px box`);
    const before = dom.html('history-tooltip');
    dom.fire(canvas, 'keydown', { key: 'ArrowRight', preventDefault() {} });
    const shown = dom.html('history-tooltip');
    assert.notEqual(shown, before, 'the right arrow key did not reveal a point');
    assert.match(shown, /score/, `the tooltip reads "${shown}"`);
    assert.match(canvas.getAttribute('aria-label'), /Showing point \d+ of \d+/,
      `the canvas label does not say which point is being shown: ${canvas.getAttribute('aria-label')}`);
    dom.fire(canvas, 'keydown', { key: 'Home', preventDefault() {} });
    assert.match(dom.html('history-tooltip'), /1 of \d+/, 'Home did not jump to the oldest point');
    dom.fire(canvas, 'keydown', { key: 'Escape', preventDefault() {} });
    assert.equal(dom.node('history-tooltip').hidden, true, 'Escape did not dismiss the tooltip');
  });

  await checkAsync('a burst of window resizes leaves every chart the right size', async () => {
    // Dragging a window edge fires resize dozens of times a second and each redraw resizes two
    // canvases per card, reallocating their backing stores. The redraws are coalesced to one per
    // frame; this proves the last resize of a burst still lands, and that the charts stay usable.
    const built = boot({ payload, incidents, history, hash: `#/cities/${payload.zones[0].zone_id}` });
    await built.runtime.settle();
    const canvas = built.dom.node('history-canvas');
    const dpr = built.dom.window.devicePixelRatio || 1;
    for (let i = 0; i < 12; i += 1) {
      built.dom.window.innerWidth = 900 - i;
      built.dom.fire(built.dom.window, 'resize', {});
    }
    await built.runtime.settle();
    await new Promise((resolve) => setTimeout(resolve, 60));
    assert.equal(canvas.width, Math.round(canvas.clientWidth * dpr),
      `after twelve resize events the chart bitmap is ${canvas.width} px for a ${canvas.clientWidth} px box`);
    const spark = built.dom.node('zones').querySelector('[data-f="spark"]');
    assert.ok(spark, 'no sparkline to check');
    assert.equal(spark.width, Math.round(spark.clientWidth * dpr),
      `after twelve resize events a sparkline bitmap is ${spark.width} px for a ${spark.clientWidth} px box`);
    built.dom.fire(canvas, 'keydown', { key: 'ArrowRight', preventDefault() {} });
    assert.match(built.dom.html('history-tooltip'), /score/,
      'the chart stopped answering after the resize burst');
  });

  await checkAsync('a driver name the page does not know does not blank the city page', async () => {
    // regression: formatMetric dereferenced METRIC_META[metric] unguarded, so one unexpected
    // metric name from the API threw inside the renderer for the whole view.
    const odd = JSON.parse(JSON.stringify(payload));
    odd.zones[0].latest.top_drivers = ['mystery_sensor'];
    const built = boot({ payload: odd, incidents, history, hash: `#/cities/${odd.zones[0].zone_id}` });
    await built.runtime.settle();
    const detail = built.dom.html('city-detail');
    assert.match(detail, new RegExp(odd.zones[0].name), 'the city page did not render at all');
    assert.match(detail, /data-f="why"/, 'the plain-language explanation is missing');
    assert.ok(detail.length > 200, 'the city page rendered almost nothing');
  });

  check('the Cities list opens the city it names', () => {
    // regression: #/cities was a dead end (no listener on #zones, no link in the card) even
    // though the cards lifted on hover as if they were clickable.
    clickNav(dom, 'cities');
    const grid = dom.html('zones');
    const first = payload.zones[0];
    assert.match(grid, new RegExp(`href="#/cities/${first.zone_id}"`),
      'the zone card does not link to its own city view');
    assert.match(grid, /class="zone-link"/, 'the zone card title is not a link');
    openCity(dom, first.zone_id);
    assert.equal(dom.node('view-city').hidden, false, 'the link target does not render the city view');
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
    assert.match(detail, new RegExp('Back to all ' + fleetNounPlural), 'there is no way back to the list');
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
    // The hero is static markup now, so the field that must not invent anything is the
    // headline: with no payload at all it has to say it is waiting, and never a city or a score.
    assert.match(failing.dom.text('ov-headline'), /Awaiting live data/,
      `the hero headline reads "${failing.dom.text('ov-headline')}"`);
    assert.ok(!/[Cc]hennai|risk score/i.test(failing.dom.text('hero-facts')),
      'the hero facts line invented a reading');
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
    assert.equal(cityCardsIn(rendered), overviewLimit(payload),
      'the first poll did not render the highest-risk cities');

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
    // M9 copy: the empty state says "flood alerts" out loud, so the wording is a shade more
    // specific than before while still being an explicit sentence rather than a blank panel
    assert.match(quiet.dom.html('alerts-active'), /No active flood alerts/);
    assert.match(quiet.dom.html('alerts-closed'), /data-empty="closed"/, 'no empty state for resolved incidents');
    assert.match(quiet.dom.html('alerts-closed'), /No resolved alerts yet/);
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
    // M9: an empty fleet now renders a designed empty state (icon, sentence, a retry) instead of
    // a bare paragraph, so this checks the state itself rather than one phrasing of it
    assert.match(nothing.dom.html('city-cards'), /data-empty="no-cities"/,
      `the overview has no empty state: "${nothing.dom.html('city-cards')}"`);
    assert.match(nothing.dom.html('city-cards'), new RegExp('No ' + fleetNoun + ' is reporting'),
      `the overview reads "${nothing.dom.html('city-cards')}"`);
    assert.match(nothing.dom.text('ov-headline'), /Awaiting live data/);
    assert.match(nothing.dom.text('hero-monitor'), /No (city|simulated) reading has been computed yet|No simulated reading/,
      `the empty hero does not explain itself: "${nothing.dom.text('hero-monitor')}"`);
    assert.equal(nothing.dom.html('zones'), '', 'the city grid should stay empty');
    assert.equal(nothing.dom.text('conn-badge'), 'LIVE', 'the API answered, so the API is live');
    assert.match(nothing.dom.text('band-strip-label'), /\d+ of \d+/,
      'the fleet label did not survive an empty fleet');
  });

  group('M9: the About section, system status and honest delivery state');

  check('the About section is reachable, marked current and lists the live fleet', () => {
    clickNav(dom, 'about');
    assert.equal(dom.node('view-about').hidden, false, 'the About section did not open');
    assert.equal(dom.node('view-overview').hidden, true, 'two sections are visible at once');
    assert.equal(dom.node('nav-about').getAttribute('aria-current'), 'page',
      'the current section is not marked for assistive tech');
    assert.equal(dom.node('nav-overview').getAttribute('aria-current'), null,
      'two nav links claim to be current');
    assert.match(dom.title(), /About FloodPulse/, `the window title reads "${dom.title()}"`);
    // the city list is read from the payload rather than hard-coded
    const cities = dom.html('about-cities');
    const names = payload.zones.map((zone) => zone.name);
    names.forEach((name) => assert.ok(cities.includes(name), `the About page omits ${name}`));
    assert.match(cities, new RegExp(`<b>${names.length}</b>`), 'the About page does not count the fleet');
    // and it says what the model is not, in the same words as the methodology page
    assert.match(dom.html('about-limitations'), /not a calibrated hydrological model|hand-tuned heuristic/,
      'the About page does not state the model limitations');
    // the static About copy (the guidance section, the official links, the stack) is checked
    // against index.html itself in tools/dashboard_check.mjs: this fake DOM only holds what the
    // controller wrote, so asserting static markup here would be asserting nothing.
    clickNav(dom, 'overview');
  });

  check('the system status says how much of the fleet is answering, in words', () => {
    const word = dom.text('health-word');
    const list = dom.html('health-list');
    assert.ok(word.length > 0, 'the status pill has no word in it');
    assert.ok(!/^\s*$/.test(word), 'the status pill is blank');
    assert.match(list, new RegExp(`\\d+ of \\d+ ${fleetNounPlural} reporting`),
      `the status list reads "${list}"`);
    // the status is a state, never a number without a unit or a bare symbol
    assert.match(list, /Weather (updated|data fresh|feed not in use)/);
    const tone = dom.node('health-dot').className;
    assert.match(tone, /is-(ok|watch|bad|quiet)/, `the status dot has no state: ${tone}`);
    // a healthy live fleet is healthy, and says so in one of the two states the brief names
    assert.match(word, /Healthy|Attention|Degraded|Connecting/, `unexpected status word "${word}"`);
    // nothing about the infrastructure leaks into the status surface
    ['arn:', 'eu-north-1', 'dynamodb', 'lambda', '725934295966'].forEach((needle) => {
      assert.ok(!list.toLowerCase().includes(needle.toLowerCase()),
        `the status surface exposes infrastructure detail: ${needle}`);
    });
  });

  check('the alert delivery panel separates publishing from delivery', () => {
    clickNav(dom, 'alerts');
    const panel = dom.html('alerts-delivery');
    assert.match(panel, /Alert publishing/, 'the panel does not state the publishing state');
    assert.match(panel, /Configured|Not configured/, 'the publishing state has no value');
    assert.match(panel, /publishe?d? to the notification topic/i,
      'the panel does not use the wording the record supports');
    // the two things the browser must never claim
    assert.ok(!/email (was )?sent|delivered to|received by/i.test(panel),
      'the panel claims a delivery it cannot know about');
    assert.match(panel, /Not reported to this page/,
      'the panel does not say that subscriber state is unknown to it');
    // and the per-incident rows carry their own state, from the API's alert log count
    const active = dom.html('alerts-active');
    const openIncidents = (incidents ? incidents.incidents || [] : []).filter((i) => i.current_status === 'OPEN');
    if (openIncidents.some((i) => (i.alert_count || 0) > 0)) {
      assert.match(active, /Alert published to the notification topic/,
        'an incident with a recorded publish does not say so');
    }
    if (openIncidents.some((i) => (i.alert_count || 0) === 0)) {
      assert.match(active, /No alert was published for this incident/,
        'an incident with no recorded publish does not say so');
    }
    clickNav(dom, 'overview');
  });

  check('the city page carries its own alert history, or says there is none', () => {
    const target = payload.zones[payload.zones.length - 1];
    openCity(dom, target.zone_id);
    const host = dom.html('city-incidents');
    assert.ok(host.length > 0, 'the city page has no alert-history area');
    const own = (incidents ? incidents.incidents || [] : []).filter((i) => i.zone_id === target.zone_id);
    if (own.length) {
      assert.match(host, new RegExp(`data-incident="${own[0].incident_id}"`),
        'the city page does not list the alerts that city has had');
    } else {
      assert.match(host, /No alerts for this/, `a calm city reads "${host}"`);
      assert.match(host, /data-empty="city-history"/, 'a calm city has no empty state');
    }
    // switching cities must not leave the previous city's alerts behind
    const other = payload.zones[0];
    openCity(dom, other.zone_id);
    const after = dom.html('city-incidents');
    if (other.zone_id !== target.zone_id) {
      const otherOwn = (incidents ? incidents.incidents || [] : []).filter((i) => i.zone_id === other.zone_id);
      if (!otherOwn.length) {
        assert.ok(!after.includes(`data-incident="${own[0] && own[0].incident_id}"`) || !own.length,
          'the previous city\'s alert history survived a city change');
      }
    }
    clickNav(dom, 'overview');
  });

  await checkAsync('a retry button asks the API again instead of reloading the page', async () => {
    // An API that answers with nothing produces the empty state whose action is "try again";
    // that button must actually re-request the routes rather than reload the document.
    const quiet = boot({
      payload,
      incidents,
      history,
      overrides: {
        zones: () => ({ body: { zone_mode: 'live_weather', zone_count: 0, zones: [] } })
      }
    });
    await quiet.runtime.settle();
    clickNav(quiet.dom, 'cities');
    assert.match(quiet.dom.html('city-cards'), /data-action="retry"/,
      'the empty fleet has no retry control to click');
    quiet.runtime.requests.length = 0;
    // the retry control is wired at the document level, so it works from any view
    const button = { disabled: false, getAttribute: (name) => (name === 'data-action' ? 'retry' : null) };
    quiet.dom.fire(quiet.dom.document, 'click', {
      target: { closest: (selector) => (selector === '[data-action="retry"]' ? button : null) },
      preventDefault() {}
    });
    await quiet.runtime.settle();
    assert.ok(quiet.runtime.requests.some((url) => /\/zones(\?|$)/.test(url)),
      `the retry button did not request the fleet again: ${quiet.runtime.requests.join(', ')}`);
    assert.ok(quiet.runtime.requests.some((url) => /\/incidents(\?|$)/.test(url)),
      'the retry button did not request the incidents again');
    assert.equal(button.disabled, true, 'the button was not disabled while the retry was in flight');
  });

  await checkAsync('the loading state is a skeleton that cannot be mistaken for data', async () => {
    const slow = boot({ payload, incidents, history, overrides: {} });
    // before the runtime settles, the fleet area shows placeholders and nothing else
    const early = slow.dom.html('city-cards');
    assert.match(early, /skel-card/, `the first paint shows "${early}"`);
    assert.ok(!/class="card city-card/.test(early), 'a placeholder carries the class of a real card');
    assert.ok(!/risk_score/.test(early), 'a placeholder invents a score');
    assert.match(early, /data-f="loading"/, 'the loading marker is gone');
    const placeholders = (early.match(/skel-card/g) || []).length;
    assert.ok(placeholders <= 5,
      `the loading grid promises ${placeholders} cards for an overview that shows at most five`);
    await slow.runtime.settle();
    const settled = slow.dom.html('city-cards');
    assert.ok(!/skel-card/.test(settled), 'the skeleton stayed on screen after the data arrived');
    assert.equal(cityCardsIn(settled), overviewLimit(payload), 'the cards did not replace the skeleton');
    assert.ok(placeholders >= cityCardsIn(settled),
      'the skeleton promised fewer cards than then arrived');
  });

  await checkAsync('every view survives an API that reports nothing at all', async () => {
    const bare = boot({
      payload,
      incidents,
      history,
      overrides: {
        zones: () => ({ body: { zone_mode: 'live_weather', zone_count: 0, zones: [] } }),
        incidents: () => ({ body: {} })
      }
    });
    await bare.runtime.settle();
    ['overview', 'cities', 'alerts', 'data', 'about'].forEach((view) => clickNav(bare.dom, view));
    // an incidents payload with no alerting block must not throw or print "undefined"
    assert.ok(!/undefined|NaN/.test(bare.dom.html('alerts-delivery')),
      `the delivery panel reads "${bare.dom.html('alerts-delivery')}"`);
    assert.ok(!/undefined|NaN/.test(bare.dom.html('health-list')),
      `the status list reads "${bare.dom.html('health-list')}"`);
    assert.ok(!/undefined|NaN/.test(bare.dom.html('about-cities')),
      `the About fleet sentence reads "${bare.dom.html('about-cities')}"`);
  });

  check('no view prints a raw value from a complete payload', () => {
    // The M9 renderers interpolate a lot of API fields into sentences. This walks every section
    // with the real payload and fails on the words that mean a field was read and missed.
    const surfaces = ['ov-headline', 'ov-intro', 'ov-subline', 'ov-stats', 'hero', 'band-strip',
      'city-cards', 'recent-activity', 'alerts-active', 'alerts-closed', 'alerts-summary',
      'alerts-status', 'alerts-delivery', 'health-list', 'health-detail', 'about-cities',
      'about-limitations', 'provenance-legend', 'ranking'];
    let inspected = 0;
    surfaces.forEach((id) => {
      const html = dom.html(id);
      if (!html) return;  // a section that this fleet does not fill is covered by its own check
      inspected += 1;
      assert.ok(!/\bundefined\b|\bNaN\b|\[object |null(null)?%/.test(html),
        `${id} printed a raw value: ${html.slice(0, 200)}`);
    });
    assert.ok(inspected >= 10, `only ${inspected} surfaces had anything in them`);
  });

  group('No engineering copy reaches a primary view');

  // The words a reader would actually read: tags removed, so `data-metric="soil_saturation_pct"`
  // (how a row is addressed) is not mistaken for copy, and `<code>` blocks are kept out of it
  // because the technical disclosures quote real identifiers on purpose.
  const prose = (id) => dom.html(id)
    .replace(/<code[\s\S]*?<\/code>/g, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&amp;/g, '&').replace(/&nbsp;/g, ' ')
    .replace(/\s+/g, ' ').trim();
  const variablesIn = (markup) => (markup.match(/\b[a-z][a-z0-9]*_[a-z0-9_]+\b/g) || []);
  const before = (markup, needle) => markup.slice(0, markup.indexOf(needle));
  const after = (markup, needle) => markup.slice(markup.indexOf(needle));

  check('a delivered payload never puts a variable name in front of a reader', () => {
    // The browser QA that found this looked at the deployed Overview, which printed the API's own
    // line: "risk_score 0.3973 = weighted base 0.3973 (soil_saturation_pct=0.1909, ...)". The
    // static harness could not catch it because that string is composed at runtime, from the
    // payload - so the check has to run against a payload too.
    const views = [
      { id: 'hero', label: 'the Overview spotlight' },
      { id: 'ov-stats', label: 'the Overview figures' },
      { id: 'band-strip', label: 'the Overview band strip' },
      { id: 'city-cards', label: 'the Overview city cards' },
      { id: 'recent-activity', label: 'the Overview activity panel' }
    ];
    clickNav(dom, 'overview');
    views.forEach((view) => {
      const markup = dom.html(view.id);
      if (!markup) return;
      assert.deepEqual(variablesIn(prose(view.id)), [],
        `${view.label} reads out variable names: ${variablesIn(prose(view.id)).join(', ')}`);
      assert.ok(!/weighted base|heuristic-v1|risk_score 0\./.test(prose(view.id)),
        `${view.label} prints model internals: ${prose(view.id).slice(0, 160)}`);
      assert.ok(!/\bundefined\b|\bNaN\b/.test(markup), `${view.label} printed a raw value`);
    });

    clickNav(dom, 'cities');
    const zones = dom.html('zones');
    assert.ok(zones.length > 200, 'the city list did not render');
    assert.deepEqual(variablesIn(prose('zones')), [],
      `the city list reads out variable names: ${variablesIn(prose('zones')).join(', ')}`);
    assert.ok(!/weighted base|risk_score 0\./.test(prose('zones')),
      'the city list prints the model\'s own output line');

    clickNav(dom, 'alerts');
    ['alerts-active', 'alerts-closed', 'alerts-summary'].forEach((id) => {
      const markup = dom.html(id);
      if (!markup) return;
      assert.deepEqual(variablesIn(prose(id)), [],
        `#${id} reads out variable names: ${variablesIn(prose(id)).join(', ')}`);
      assert.ok(!/weighted base|risk_score 0\./.test(prose(id)), `#${id} prints model internals`);
    });
  });

  check('the Overview spotlight answers "what, how bad, why, how fresh" in words', () => {
    clickNav(dom, 'overview');
    const FP = app.sandbox.window.FP;
    // M10: the hero is static markup that the renderer patches field by field, so what a reader
    // sees is the union of its fields - read back node by node, the way a browser renders them.
    const fields = ['ov-headline', 'hero-monitor', 'hero-why', 'hero-facts', 'hero-state'];
    const hero = fields.map((id) => dom.text(id)).join(' ').replace(/\s+/g, ' ').trim();
    const words = hero;
    const leader = payload.zones.find((zone) => zone.zone_id === payload.highest_risk_zone.zone_id)
      || payload.zones.find((zone) => zone.latest);
    // 1 and 2: what is happening, and is it dangerous
    assert.ok(words.includes(leader.name), `the spotlight does not name the ${leader.name}`);
    assert.match(words, /Risk is currently [A-Z]+\./,
      `the spotlight does not state the band in a sentence: ${words.slice(0, 160)}`);
    // 3: why - in words, naming a human driver
    const driver = (leader.latest.top_drivers || [])[0];
    if (driver) {
      assert.ok(words.includes(FP.driverLabel(driver)),
        `the spotlight does not name the main risk factor (${FP.driverLabel(driver)}): ${words}`);
      assert.ok(words.includes('is the main contributor'),
        `the spotlight does not say what is driving the score: ${words}`);
      assert.ok(!words.includes(driver), `the spotlight prints the raw driver "${driver}"`);
    }
    // 4: how fresh ("Updated just now" for a reading seconds old, "Updated 4 min ago" otherwise)
    assert.match(words, /Updated (just now|\d)/,
      `the spotlight does not say how old the reading is: ${words}`);
    // 5: no internal identifier, no raw clock, no model name in the one line under the name
    assert.ok(!words.includes(leader.zone_id), 'the spotlight prints the internal zone id');
    assert.ok(!/\d{2}:\d{2}:\d{2}Z/.test(words), 'the spotlight prints a raw timestamp');
    assert.ok(!/heuristic|dominant/i.test(words), `the spotlight shows model vocabulary: ${words}`);
    // the score itself still comes from the payload, unchanged by the rewrite
    assert.ok(hero.includes(FP.formatScore(payload.highest_risk_zone.risk_score)),
      `the spotlight lost the API risk score: "${hero}"`);
  });

  check('the city page keeps the model behind one collapsed fold', () => {
    const FP = app.sandbox.window.FP;
    const leader = payload.zones.find((zone) => zone.latest);
    openCity(dom, leader.zone_id);
    const detail = dom.html('city-detail');
    const foldAt = detail.indexOf('<details class="tech"');
    assert.ok(foldAt > 0, 'the city page has no technical fold');
    const visible = detail.slice(0, foldAt);
    const folded = detail.slice(foldAt);
    // the answer first: band, what is driving it, the measurements, the trend
    assert.ok(visible.indexOf('data-f="tiles"') < foldAt && visible.includes('data-f="tiles"'),
      'the measurements are not above the fold');
    assert.match(visible, /What is driving risk\?/, 'the page does not ask the visitor\'s question');
    assert.ok(visible.indexOf('What is driving risk?') < visible.indexOf('Trend and history'),
      'the driver block must come before the history section');
    const visibleProse = visible
      .replace(/<code[\s\S]*?<\/code>/g, ' ')
      .replace(/<[^>]+>/g, ' ')
      .replace(/\s+/g, ' ').trim();
    assert.deepEqual(variablesIn(visibleProse), [],
      `the visible city page reads out variable names: ${variablesIn(visibleProse).join(', ')}`);
    assert.ok(!/weighted base|weighted contribution|heuristic score|heuristic-v1/i.test(visibleProse),
      `the visible city page carries model vocabulary: ${visibleProse.slice(0, 160)}`);
    assert.ok(!/GET \/zones|zone_id/.test(visibleProse),
      `the visible city page quotes an API route: ${visibleProse.slice(0, 160)}`);
    // and then the whole model, present, folded, with its arithmetic intact
    assert.ok(folded.includes(leader.latest.explanation),
      'the fold no longer carries the API\'s own explanation line');
    ['weighted base', 'Raw model output', 'Watch', 'Weight', 'Weighted contribution'].forEach((term) => {
      assert.ok(folded.includes(term), `the fold lost "${term}"`);
    });
    FP.METRIC_ORDER.forEach((metric) => {
      assert.ok(folded.includes(metric), `the fold lost the variable name ${metric}`);
    });
    assert.ok(!/<details[^>]*\sopen/.test(detail), 'the technical fold must start collapsed');
  });

  check('the methodology page still publishes the arithmetic, folded away', () => {
    const FP = app.sandbox.window.FP;
    clickNav(dom, 'data');
    const breakdown = dom.html('score-breakdown');
    assert.ok(breakdown.length > 100, 'the worked example did not render');
    const foldAt = breakdown.indexOf('<details class="tech"');
    assert.ok(foldAt >= 0, 'the worked example is not folded');
    assert.ok(breakdown.includes('<summary>'), 'the worked example cannot be opened');
    assert.ok(!/<details[^>]*\sopen/.test(breakdown), 'the worked example must start collapsed');
    const leader = payload.zones.find((zone) => zone.latest);
    assert.ok(breakdown.includes('weighted base'), 'the methodology page lost the formula');
    assert.ok(breakdown.includes(leader.latest.explanation.replace(/&/g, '&amp;')) || breakdown.includes(leader.latest.explanation),
      'the methodology page does not quote the API\'s own output line');
    FP.METRIC_ORDER.forEach((metric) => {
      assert.ok(breakdown.includes(metric), `the worked example lost the variable ${metric}`);
    });
    // the visible part of the page still explains the method in sentences, with the weights
    // (the model's name is written in code formatting there on purpose, so it is read from the
    // markup rather than from the prose this harness strips tags out of)
    assert.ok(dom.html('method-block').includes('heuristic-v1'),
      'the methodology page does not name the model');
    assert.match(prose('method-block'), /\d+%/, 'the methodology page does not state the weights');
    // and it says plainly what the score is not
    assert.match(prose('limitations'), /\w/, 'the limitations list is empty');
  });

  /* ==========================================================================
   * M10: the Overview's new blocks and the public email subscription form
   * ======================================================================== */

  group('M10: the Overview shows a thumbnail plate, a meter and the alert preview');

  /* These two read the Overview as a reader arriving at the page sees it, so they run on their
   * own fresh boot: the shared `dom` above has been driven through connection failures and empty
   * fleets by the checks before them, and the point here is what a working first load looks like. */
  await checkAsync('every city card carries its nightscape, a band-coloured plate and a score meter', async () => {
    const fresh = boot({ payload, incidents, history, overrides: {} });
    await fresh.runtime.settle();
    const FP = fresh.sandbox.window.FP;
    const html = fresh.dom.html('city-cards');
    const limit = overviewLimit(payload);
    const plates = (html.match(/class="city-thumb band-/g) || []).length;
    const meters = (html.match(/class="city-meter band-/g) || []).length;
    assert.equal(plates, limit, `expected a plate per card, found ${plates}`);
    assert.equal(meters, limit, `expected a meter per card, found ${meters}`);
    // decoration must stay decoration
    assert.match(html, /<span class="city-thumb[^"]*" data-band="\w+" aria-hidden="true">/,
      'the city plate is not hidden from assistive tech');

    // Per card: a mapped zone loads its prepared local nightscape - named by an explicit
    // zone_id -> file table, never a path built from API text and never a remote asset - and an
    // unmapped zone loads no image at all and keeps the drawn plate. Either way the card's text
    // carries the name, the band, the score and the meter, so nothing about risk depends on a
    // photograph.
    const cards = html.split('<a class="card city-card').slice(1);
    assert.equal(cards.length, limit, 'the card count does not match the cities the overview shows');
    let images = 0;
    let lazy = 0;
    cards.forEach((card) => {
      const id = /data-zone="([^"]+)"/.exec(card)[1];
      const file = FP.cityImageFor({ zone_id: id });
      const tags = card.match(/<img[^>]*>/g) || [];
      assert.ok(card.includes('class="city-thumb'), `${id}: the card has no image band`);
      assert.ok(/data-f="risk_score"/.test(card), `${id}: the card lost its score`);
      assert.ok(/data-f="risk_class"/.test(card), `${id}: the card lost its band in words`);
      if (!file) {
        assert.equal(tags.length, 0, `${id}: an unmapped zone must not load an image`);
        return;
      }
      assert.equal(tags.length, 1, `${id}: expected exactly one image, found ${tags.length}`);
      const tag = tags[0];
      assert.ok(tag.includes(`src="${file}"`), `${id}: the card does not load ${file}`);
      assert.match(tag, /\salt=""/, `${id}: the image is not decorative: ${tag}`);
      assert.ok(!/\salt="[^"]+"/.test(tag), `${id}: the image repeats the city name: ${tag}`);
      // eager (no attribute at all) or the browser's own lazy loading: no custom loader anywhere
      assert.ok(!/\sloading="/.test(tag) || /\sloading="lazy"/.test(tag),
        `${id}: an unexpected loading attribute: ${tag}`);
      assert.match(tag, /\sdecoding="async"/, `${id}: the image does not decode off the main thread`);
      assert.match(tag, /\swidth="\d+"\sheight="\d+"/, `${id}: the image reserves no space: ${tag}`);
      images += 1;
      if (/loading="lazy"/.test(tag)) lazy += 1;
    });
    // Exact in both directions, over the cards that are actually on this page: an image for
    // every mapped city it shows, and none at all for a city with no prepared nightscape - of
    // which the live fleet has ten, and the demo fleet's invented zones have all of theirs.
    // The whole fleet's imagery is pinned on the Cities page, where every city is.
    const shownMapped = cards.filter((card) => FP.cityImageFor({ zone_id: /data-zone="([^"]+)"/.exec(card)[1] }));
    assert.equal(images, shownMapped.length,
      `the page loaded ${images} images for the ${shownMapped.length} mapped cities it shows`);
    // Lazy loading is as exact as the imagery: the fifth card onwards, and only a card that has
    // an image to defer can defer one.
    const expectedLazy = cards.filter((card, index) => index >= 4
      && FP.cityImageFor({ zone_id: /data-zone="([^"]+)"/.exec(card)[1] })).length;
    assert.equal(lazy, expectedLazy, `the page deferred ${lazy} images, expected ${expectedLazy}`);
    if (limit > 1) assert.ok(!/loading="lazy"/.test(cards[0]), 'the first card defers its own image');
    assert.ok(lazy <= images, 'more lazy images than images');
    assert.ok(!/\bsrc="[^"]*https?:/.test(html), 'a city card references a remote asset');
    assert.ok(!/src="[^"]*\.svg"/.test(html), 'a city card loads one of the fake .svg files');

    // the meter is drawn from the API's own score, at the documented band edges
    const zone = payload.zones.find((entry) => entry.latest);
    const pct = FP.riskPercent(zone.latest.risk_score).toFixed(1);
    assert.ok(html.includes(`--pct:${pct}`), `no meter uses the API score (${pct}%)`);
    const meter = html.slice(html.indexOf('class="city-meter'), html.indexOf('class="city-meter') + 900);
    assert.ok(meter.includes('city-meter-fill'), 'the meter has no fill');
    assert.equal((meter.match(/city-meter-tick/g) || []).length, FP.SCORE_BAND_EDGES.length,
      'the meter does not mark the documented band edges');
  });

  await checkAsync('the Overview previews alerts, or says plainly that there are none', async () => {
    const fresh = boot({ payload, incidents, history, overrides: {} });
    await fresh.runtime.settle();
    const html = fresh.dom.html('alert-preview');
    assert.ok(html.length > 40, 'the alert preview did not render');
    // the field the API actually sends: this read `entry.status` and so never took the rows
    // branch at all, which is why the count below was never checked against a real incident
    const open = incidents && Array.isArray(incidents.incidents)
      ? incidents.incidents.filter((entry) => entry.current_status === 'OPEN')
      : [];
    if (open.length === 0) {
      assert.match(html, /data-f="alert-preview-empty"/, 'an empty preview is not marked as one');
      assert.match(html, /No active flood alerts/, `the calm state reads "${html.slice(0, 120)}"`);
      assert.match(html, /below the alert level/, 'the calm state does not say the fleet is below alert');
      assert.match(html, /href="#\/alerts"/, 'the calm state offers no way to the alerts page');
      assert.match(fresh.dom.text('alert-preview-title'), /Recent alerts/, 'the preview title is wrong');
    } else {
      const rows = (html.match(/class="alert-row/g) || []).length;
      assert.ok(rows >= 1 && rows <= 2, `the preview lists ${rows} alerts, expected at most two`);
      assert.match(fresh.dom.text('alert-preview-title'), /Active alerts/, 'the preview title is wrong');
    }
    assert.ok(!/heuristic|weighted base/.test(html), 'the preview shows model vocabulary');
  });

  group('M13: twelve coastal cities on one page, and no request to filter them');

  /* The controls are driven the way a browser drives them: the search box with an input event,
   * a chip with a click resolved to that chip (target.closest), the select with a change. Every
   * one of them must filter the list already in memory - the whole point of doing this on the
   * client is that finding one city of the fleet costs the API nothing. */
  const typeSearch = (source, value) => source.dom.fire(source.dom.node('cities-search'), 'input', { target: { value } });
  const clickChip = (source, group, attribute, value) => source.dom.fire(source.dom.node(group), 'click', {
    target: source.dom.targetWith(`[data-${attribute}]`, `data-${attribute}`, value)
  });
  const chooseSort = (source, value) => source.dom.fire(source.dom.node('cities-sort'), 'change', { target: { value } });
  const gridCards = (source) => (source.dom.html('zones').match(/<article class="card zone-card/g) || []).length;
  const gridOrder = (source) => (source.dom.html('zones').match(/data-zone="([^"]+)"/g) || [])
    .map((match) => match.replace(/^data-zone="/, '').replace(/"$/, ''));
  const bootOnCities = async () => {
    const view = boot({ payload, incidents, history, overrides: {} });
    await view.runtime.settle();
    clickNav(view.dom, 'cities');
    return view;
  };

  await checkAsync('the cities page lists every city, and the search finds one by city or state', async () => {
    const find = await bootOnCities();

    // M11.2: what a metric row is made of, measured on the page this check already opened.
    const rowHtml = find.dom.html('zones');
    const metricRows = rowHtml.match(/<li class="metric"[\s\S]*?<\/li>/g) || [];
    assert.equal(metricRows.length, payload.zones.length * 5,
      `the page rendered ${metricRows.length} metric rows for ${payload.zones.length} cities, expected five each`);
    metricRows.forEach((one) => {
      assert.match(one, /^<li class="metric" data-metric="[a-z0-9_]+">/,
        `a metric row does not name its own metric: ${one.slice(0, 60)}`);
      const name = one.match(/<span class="metric-name">([^<]*)<\/span>/);
      assert.ok(name, `a metric row has no plain name cell: ${one.slice(0, 90)}`);
      assert.ok(!/metric-source/.test(name[1]),
        'the provenance chip is nested inside the name cell again, which is what broke the row');
      assert.equal((one.match(/data-f="metric-source"/g) || []).length, 1,
        'a metric row does not carry exactly one provenance chip');
      assert.equal((one.match(/data-f="metric-value"/g) || []).length, 1,
        'a metric row does not carry exactly one value cell');
      assert.equal((one.match(/class="metric-track"/g) || []).length, 1,
        'a metric row does not carry exactly one bar');
    });
    assert.ok(!/MODEL_DERIVED/.test(rowHtml), 'the raw provenance enum is on the page');
    assert.ok(!/>SIMULATED</.test(rowHtml), 'the raw SIMULATED tag is on the page as a chip label');
    [...rowHtml.matchAll(/data-f="metric-value">([^<]*)</g)].map((match) => match[1]).forEach((cell) => {
      assert.ok(!/\s$/.test(cell), `a metric value ends in whitespace: ${JSON.stringify(cell)}`);
      if (/[0-9]/.test(cell)) {
        assert.match(cell, /[0-9]\s?\S/, `a metric value has no unit: ${JSON.stringify(cell)}`);
      }
    });
    assert.equal(gridCards(find), payload.zones.length,
      `the page lists ${gridCards(find)} of ${payload.zones.length} cities`);
    assert.match(find.dom.text('cities-count'), new RegExp(`Showing all ${payload.zones.length} cities`),
      `the count reads "${find.dom.text('cities-count')}"`);
    const before = find.runtime.countMatching('/zones');

    // case-insensitive, by city
    const city = payload.zones[payload.zones.length - 1];
    typeSearch(find, city.name.toUpperCase());
    assert.equal(gridCards(find), 1, `searching "${city.name.toUpperCase()}" left ${gridCards(find)} cities`);
    assert.match(find.dom.html('zones'), new RegExp(city.name), 'the city that matched is not on screen');
    assert.match(find.dom.text('cities-count'), /^Showing 1 city \u00b7 Search: /,
      `a single match should read as one city and name the search: "${find.dom.text('cities-count')}"`);

    // by state, and only the cities in it
    const inState = payload.zones.filter((zone) => zone.state === city.state);
    typeSearch(find, city.state.toLowerCase());
    assert.equal(gridCards(find), inState.length,
      `searching "${city.state}" left ${gridCards(find)} cities, expected ${inState.length}`);
    inState.forEach((zone) => assert.ok(find.dom.html('zones').includes(zone.name),
      `${zone.name} is in ${city.state} but the search hid it`));

    assert.equal(find.runtime.countMatching('/zones'), before,
      'the search box asked the API for the fleet again');

    // clearing the box puts the fleet back
    typeSearch(find, '');
    assert.equal(gridCards(find), payload.zones.length, 'clearing the search did not restore the fleet');
    assert.equal(find.dom.html('cities-empty'), '', 'the empty state stayed after clearing the search');
  });

  await checkAsync('the coast and risk chips filter the whole fleet, and promise what they deliver', async () => {
    const filter = await bootOnCities();
    const before = filter.runtime.countMatching('/zones');
    const coasts = payload.coverage.coast_groups;
    const west = coasts.find((entry) => entry.coast_group === 'West Coast');
    // M13: assert the grouping before using it, so a payload without this coast fails here rather
    // than quietly comparing a rendered count against undefined.
    assert.ok(west && west.city_count > 0,
      `the payload groups no West Coast cities: ${JSON.stringify(coasts.map((c) => c.coast_group))}`);

    // M13: the status strip names the product, the fleet size and the coast groups it is showing,
    // all three read from this payload rather than hard-coded here.
    const stripCount = payload.zone_count || payload.zones.length;
    assert.match(filter.dom.text('brand-subtitle'),
      new RegExp(`^FloodPulse Coastal India · ${stripCount} coastal cities in ${coasts.length} coast group`),
      `the status strip reads "${filter.dom.text('brand-subtitle')}"`);

    // one chip per coast group the API grouped the fleet into, plus All - nothing invented, nothing missing
    const coastGroup = filter.dom.html('cities-coast');
    assert.equal((coastGroup.match(/data-coast="/g) || []).length, coasts.length + 1,
      `the coast filter offers ${(coastGroup.match(/data-coast="/g) || []).length} chips for ${coasts.length} coast groups`);
    coasts.forEach((entry) => assert.ok(coastGroup.includes(`data-coast="${entry.coast_group}"`),
      `no chip for the ${entry.coast_group} coast group`));

    clickChip(filter, 'cities-coast', 'coast', 'West Coast');
    assert.equal(gridCards(filter), west.city_count,
      `the West Coast chip shows ${gridCards(filter)} cities; the API reported ${west.city_count}`);
    assert.match(filter.dom.html('cities-coast'), /data-coast="West Coast" aria-pressed="true"/,
      'the active coast chip is not marked pressed for assistive tech');
    assert.match(filter.dom.html('cities-coast'), /data-coast="all" aria-pressed="false"/,
      'the All coast chip is still marked pressed');

    // a risk floor stacks on top of the coast, and the count line follows every change
    const both = payload.zones.filter((zone) => zone.coast_group === 'West Coast' && zone.latest
      && zone.latest.risk_class !== 'NORMAL' && zone.latest.risk_class !== 'WATCH');
    clickChip(filter, 'cities-risk', 'risk', 'WARNING');
    assert.equal(gridCards(filter), both.length,
      `West Coast + WARNING+ shows ${gridCards(filter)} cities, expected ${both.length}`);
    // The sentence follows the payload: the recorded fleet is calm, so a WARNING floor matches
    // nothing and the sentence says so. Either way it names what is in force.
    if (both.length) {
      assert.equal(filter.dom.text('cities-count'),
        `Showing ${both.length} ${both.length === 1 ? 'city' : 'cities'} · West Coast · WARNING+`,
        `the count reads "${filter.dom.text('cities-count')}"`);
    } else {
      assert.equal(filter.dom.text('cities-count'), 'No cities match these filters.',
        `the count reads "${filter.dom.text('cities-count')}"`);
    }
    both.forEach((zone) => assert.ok(gridOrder(filter).includes(zone.zone_id),
      `${zone.zone_id} is on the West Coast and at WARNING or worse, but the filters hid it`));

    // and All on both controls brings every city back
    clickChip(filter, 'cities-coast', 'coast', 'all');
    clickChip(filter, 'cities-risk', 'risk', 'all');
    assert.equal(gridCards(filter), payload.zones.length, 'the All chips did not restore the fleet');
    assert.equal(filter.runtime.countMatching('/zones'), before,
      'changing the filters asked the API for the fleet again');
  });

  await checkAsync('M13: the page states the fleet it is showing, and why this city is in it', async () => {
    // The scope note is filled from the payload, so a page cannot promise wider coverage than the
    // API it is reading. The static blocks that surround it are checked against index.html.
    const overview = boot({ payload, incidents, history, overrides: {} });
    await overview.runtime.settle();
    assert.match(String((payload.coverage || {}).scope_note), /selected monitoring fleet/,
      'the payload does not state what the fleet is');
    const note = overview.dom.text('why-cities-note');
    assert.ok(note.includes('not complete national coverage'),
      `the overview does not state the limitation: "${note}"`);
    // The scope note may *deny* ranking ("listed in coast order rather than ranked"), so only
    // superlatives are banned here: a denial is exactly what this line is supposed to contain.
    assert.ok(!/most flood-prone|most vulnerable|riskiest|number one|deadliest/i.test(note),
      `the scope note claims a ranking: "${note}"`);

    // one city's own page, in that city's own words, straight from the payload
    const first = payload.zones.find((zone) => zone.selection_reason);
    assert.ok(first, 'no city in this payload states why it is monitored');
    const detail = boot({ payload, incidents, history, hash: `#/cities/${first.zone_id}` });
    await detail.runtime.settle();
    const markup = detail.dom.html('city-detail');
    assert.match(markup, /Why this city is monitored/, 'the city page does not say why it is monitored');
    assert.ok(markup.includes(first.selection_reason.split(',')[0].slice(0, 30)),
      `the city page does not carry the payload's reason for ${first.zone_id}`);
    assert.ok(!/most flood-prone|most vulnerable|riskiest|number one|deadliest/i.test(markup),
      `the city page makes a ranking claim about ${first.zone_id}`);

    // and the simulated fleet is untouched: a zone with no reason renders no such block
    const demoZone = { zone_id: 'zone-old-town-canal', name: 'Old Town Canal', latest: null };
    assert.equal(overview.dom.window.FP.citySelectionNote(demoZone), '',
      'a zone with no selection reason renders a "why this city is monitored" block');
  });

  await checkAsync('filters that exclude every city say so, and offer the way back', async () => {
    const none = await bootOnCities();
    typeSearch(none, 'zz-no-such-city');
    assert.equal(gridCards(none), 0, 'a search that matches nothing still rendered cards');
    assert.equal(none.dom.html('zones'), '', 'the empty state was rendered inside the card grid');
    assert.equal(none.dom.node('cities-empty').hidden, false, 'the empty state stayed hidden');
    assert.match(none.dom.html('cities-empty'), /data-empty="no-city-match"/,
      'the empty state is not the designed one');
    assert.match(none.dom.html('cities-empty'), /No cities match these filters\./,
      `the empty state reads "${none.dom.html('cities-empty')}"`);
    assert.equal(none.dom.text('cities-count'), 'No cities match these filters.',
      `the count reads "${none.dom.text('cities-count')}"`);

    // "Clear filters" is a real control, and it puts the fleet and the controls back
    none.dom.fire(none.dom.document, 'click', {
      target: none.dom.targetWith('[data-action="clear-filters"]', 'data-action', 'clear-filters'),
      preventDefault() {}
    });
    assert.equal(gridCards(none), payload.zones.length, 'clearing the filters did not restore the fleet');
    assert.equal(none.dom.node('cities-search').value, '', 'the search box still holds the query');
    assert.equal(none.dom.html('cities-empty'), '', 'the empty state stayed after clearing the filters');
    assert.match(none.dom.html('cities-risk'), /data-risk="all" aria-pressed="true"/,
      'the risk filter did not return to All');
  });

  await checkAsync('the sort orders the whole fleet, and a missing reading never looks fresh', async () => {
    const ordered = await bootOnCities();
    const risk = payload.zones.slice()
      .sort((a, b) => ((b.latest ? b.latest.risk_score : -1) - (a.latest ? a.latest.risk_score : -1)))
      .map((zone) => zone.zone_id);
    assert.deepEqual(gridOrder(ordered), risk, 'the page does not open on highest risk first');

    chooseSort(ordered, 'name');
    const byName = payload.zones.slice().sort((a, b) => a.name.localeCompare(b.name)).map((z) => z.zone_id);
    assert.deepEqual(gridOrder(ordered), byName, 'A-Z does not order the fleet by city name');

    chooseSort(ordered, 'fresh');
    const byAge = payload.zones.slice().sort((a, b) => {
      const aAge = a.latest ? a.latest.age_seconds : Infinity;
      const bAge = b.latest ? b.latest.age_seconds : Infinity;
      return aAge === bAge ? a.name.localeCompare(b.name) : aAge - bAge;
    }).map((zone) => zone.zone_id);
    assert.deepEqual(gridOrder(ordered), byAge,
      'Freshness does not order the fleet by how current its reading is');

    // a city with no reading is never sorted among the fresh ones: with two reading-based
    // orders it goes last, so it cannot sit at the top of a list that reads as "freshest first"
    const partial = JSON.parse(JSON.stringify(payload));
    const quiet = partial.zones[0].zone_id;
    partial.zones[0].latest = null;
    const shortBoard = boot({ payload: partial, incidents, history, overrides: {} });
    await shortBoard.runtime.settle();
    clickNav(shortBoard.dom, 'cities');
    ['risk', 'fresh'].forEach((key) => {
      chooseSort(shortBoard, key);
      const order = gridOrder(shortBoard);
      assert.equal(order[order.length - 1], quiet,
        `a city with no reading is not last under "${key}": ${order.join(', ')}`);
    });
    chooseSort(shortBoard, 'name');
    assert.equal(gridOrder(shortBoard).length, partial.zones.length,
      'a city with no reading disappeared from the A-Z list');
  });

  await checkAsync('the whole fleet on the Cities page: every city, and only a mapped city loads an image', async () => {
    const full = await bootOnCities();
    const FP = full.sandbox.window.FP;
    const grid = full.dom.html('zones');
    const cards = grid.split('<article class="card zone-card').slice(1);
    assert.equal(cards.length, payload.zones.length,
      `the grid lists ${cards.length} of ${payload.zones.length} cities`);
    // a plate for every city, and an image for exactly the cities that have one prepared
    assert.equal((grid.match(/class="city-thumb band-/g) || []).length, payload.zones.length,
      'not every city card carries its band-coloured plate');
    const mapped = payload.zones.filter((zone) => FP.cityImageFor(zone));
    assert.equal((grid.match(/<img class="city-photo"/g) || []).length, mapped.length,
      `the grid loads ${(grid.match(/<img class="city-photo"/g) || []).length} images for ${mapped.length} mapped cities`);
    // a city with no photograph keeps the drawn plate, and the plate names its coast group
    const plateCoasts = (grid.match(/class="city-thumb-coast"/g) || []).length;
    assert.equal(plateCoasts, payload.zones.length - mapped.length,
      `${plateCoasts} drawn plates name a coast group, expected ${payload.zones.length - mapped.length}`);
    assert.ok(!/src="[^"]*https?:/.test(grid), 'a city card references a remote asset');

    // every card names its city, its state, its coast group and the way in
    payload.zones.forEach((zone) => {
      assert.ok(grid.includes(`href="#/cities/${zone.zone_id}"`), `${zone.zone_id} has no link to its city view`);
      assert.ok(grid.includes(`${zone.name}, ${zone.state}`), `${zone.zone_id} does not name its state`);
      assert.ok(grid.includes(`&#183; ${zone.coast_group}`) || grid.includes(`· ${zone.coast_group}`),
        `${zone.zone_id} does not name its coast group`);
      assert.ok(grid.includes('Open city'), `${zone.zone_id} does not say how to open it`);
    });
    // and the caveat is stated, not implied: the cities are monitored, not the states or a coast
    assert.ok(!/monitoring the (state|coast|region|country)/i.test(grid),
      'the Cities page claims broader coverage than the fleet has');
  });

  check('no surface claims that a state, a coast or the country is monitored', () => {
    ['ov-intro', 'ov-headline', 'ov-subline', 'hero-monitor', 'cities-lede', 'cities-coverage',
      'city-cards-note', 'city-detail', 'about-cities', 'about-limitations'].forEach((id) => {
      const text = `${dom.html(id)} ${dom.text(id)}`;
      assert.ok(!/monitoring (the )?(state|coast|region|country)/i.test(text),
        `${id} claims the wrong scope: ${text.slice(0, 160)}`);
      assert.ok(!/\d+ (states|coasts|regions) (are )?(monitored|watched|covered)/i.test(text),
        `${id} implies a state, a coast or a region is monitored: ${text.slice(0, 160)}`);
      assert.ok(!/\bis (safe|dry)\b/i.test(text), `${id} calls something safe: ${text.slice(0, 160)}`);
    });
  });

  group('M10: the public email subscription form');

  /* The result node is filled with real child elements, not an HTML string, so the harness
   * reads it the way a screen reader walks it: the node's own text, then each child's. */
  const resultText = (source) => {
    const node = source.node('subscribe-result');
    if (!node) return '';
    return [node.textContent]
      .concat(node.children.map((child) => child.textContent))
      .join(' ').replace(/\s+/g, ' ').trim();
  };

  const submitAddress = (source, address) => {
    source.node('subscribe-email').value = address;
    source.fire(source.node('subscribe-form'), 'submit', { preventDefault() {} });
  };

  const formBoot = (subscriptions) => boot({
    payload, incidents, history, overrides: { subscriptions }
  });

  await checkAsync('a valid address asks the API once and answers "check your inbox"', async () => {
    const form = formBoot(undefined);
    await form.runtime.settle();
    // index.html ships the result row `hidden`; the harness DOM starts blank, so the guarantee
    // that it starts empty is checked against the markup in dashboard_check.mjs. Here the point
    // is that nothing is claimed until a request has actually been made.
    assert.equal(resultText(form.dom), '', 'the form shows a verdict before anything was submitted');

    submitAddress(form.dom, '  Person@Example.com  ');
    await form.runtime.settle();
    assert.equal(form.runtime.count('/subscriptions'), 1, 'the form did not post exactly once');
    const request = form.runtime.lastRequest();
    assert.ok(request.startsWith(apiBaseForRun), `the request went to "${request}"`);
    const words = resultText(form.dom);
    assert.match(words, /Check your inbox/, `the accepted state reads "${words}"`);
    assert.match(words, /confirmation email/i, 'the accepted state does not explain the confirmation email');
    assert.match(words, /Confirm it to activate/i, 'the accepted state does not tell the visitor what to do');
    assert.ok(!/Amazon|SNS|notification topic/i.test(words),
      `the accepted state names the provider's service: "${words}"`);
    // the claim the brief forbids: nothing may say the address is subscribed
    assert.ok(!/you are subscribed|subscribed successfully|you will now receive/i.test(words),
      `the form claims a subscription that has not happened: "${words}"`);
    assert.equal(form.dom.node('subscribe-result').hidden, false,
      'the result row stayed hidden after an outcome was known');
    // the address the visitor typed stays visible, and the button is usable again
    assert.equal(form.dom.node('subscribe-email').value, 'Person@Example.com',
      'the field was left with untrimmed whitespace or emptied');
    assert.equal(form.dom.node('subscribe-button').disabled, false, 'the button stayed disabled');
    assert.equal(form.dom.node('subscribe-button').getAttribute('aria-busy'), null,
      'the button is still marked busy');
    assert.equal(form.dom.node('subscribe-email').getAttribute('aria-invalid'), 'false',
      'a valid address is marked invalid');
  });

  await checkAsync('an address that is not an address is refused without asking the API', async () => {
    const form = formBoot(undefined);
    await form.runtime.settle();
    ['', 'person@example', 'person@@example.com', 'a b@example.com',
      'person@example.com\r\nBcc: other@example.com', '<script>alert(1)</script>@x.com',
      `${'a'.repeat(250)}@example.com`].forEach((address) => {
      submitAddress(form.dom, address);
    });
    await form.runtime.settle();
    assert.equal(form.runtime.count('/subscriptions'), 0,
      'an invalid address still produced a request to the API');
    const words = resultText(form.dom);
    assert.match(words, /Enter a valid email address\./, `the invalid state reads "${words}"`);
    assert.equal(form.dom.node('subscribe-email').getAttribute('aria-invalid'), 'true',
      'the invalid field is not marked for assistive tech');
    assert.equal(form.dom.node('subscribe-result').hidden, false,
      'the invalid state was not shown');
    // and nothing the visitor typed is echoed back into the page as markup
    const html = form.dom.html('subscribe-result');
    assert.ok(!/<script|\son[a-z]+=/i.test(html), `the result echoes markup: "${html}"`);
    assert.ok(!words.includes('script'), `the result echoes the submitted text: "${words}"`);

    // typing again clears the verdict, so a corrected address is not left next to an old error
    form.dom.fire(form.dom.node('subscribe-email'), 'input', {});
    assert.equal(form.dom.node('subscribe-result').hidden, true,
      'a previous error stayed on screen while the visitor was typing');
    assert.equal(resultText(form.dom), '', 'a previous error message was not cleared');
  });

  await checkAsync('the API throttling a request is reported as throttling', async () => {
    const form = formBoot(() => ({
      status: 429,
      body: { error: 'rate_limited', message: 'Too many subscription requests. Please try again later.' }
    }));
    await form.runtime.settle();
    submitAddress(form.dom, 'person@example.com');
    await form.runtime.settle();
    assert.match(resultText(form.dom), /Too many subscription requests\. Please try again later\./,
      `the throttled state reads "${resultText(form.dom)}"`);
  });

  await checkAsync('an SNS or API failure is reported in the product\'s own words', async () => {
    const failures = [
      ['a 503 from the API', () => ({
        status: 503,
        body: {
          error: 'subscription_unavailable',
          topic_arn: 'arn:aws:sns:eu-north-1:725934295966:floodpulse-alerts',
          message: 'An error occurred (AuthorizationError) when calling the Subscribe operation'
        }
      })],
      ['a refused request', () => ({ throw: new Error('Failed to fetch') })],
      ['an unexpected status', () => ({ status: 500, body: { stack: 'Traceback ... at handler.py:412' } })]
    ];
    for (const [label, handler] of failures) {
      const form = formBoot(handler);
      await form.runtime.settle();
      submitAddress(form.dom, 'person@example.com');
      await form.runtime.settle();
      const words = resultText(form.dom);
      assert.match(words, /We couldn\u2019t create the subscription request right now\. Please try again\./,
        `${label} reads "${words}"`);
      ['arn:aws', 'AuthorizationError', 'Traceback', 'handler.py', 'stack', 'topic_arn']
        .forEach((leak) => {
          assert.ok(!words.includes(leak), `${label} leaks "${leak}" into the page: "${words}"`);
        });
      assert.equal(form.dom.node('subscribe-button').disabled, false,
        `${label} left the form unusable`);
    }
  });

  await checkAsync('the form cannot be submitted twice while a request is in flight', async () => {
    const form = formBoot(undefined);
    await form.runtime.settle();
    form.runtime.hold('/subscriptions', { body: null });
    submitAddress(form.dom, 'person@example.com');
    submitAddress(form.dom, 'person@example.com');
    submitAddress(form.dom, 'person@example.com');
    await form.runtime.settle();
    assert.equal(form.runtime.count('/subscriptions'), 1,
      'a double click sent more than one subscription request');
    assert.equal(form.dom.node('subscribe-button').getAttribute('aria-busy'), 'true',
      'the button does not report that it is busy');
    form.runtime.release();
  });

  group('M10: every view renders at every width the brief names');

  /* The fake DOM has no layout engine, so this cannot measure a scrollbar. What it can do - and
   * what the stylesheet is built to make true - is prove that at each named width the media
   * queries resolve to the intended layout, every view renders without an error, and nothing in
   * the markup asks for a fixed width that a 320px screen cannot give. The overflow claim itself
   * rests on the CSS assertions in dashboard_check.mjs: every multi-column track in the M10 layer
   * is minmax(0, ...), and no rule requires more than 320px. */
  await checkAsync('all six views render at 320, 360, 390, 430, 768, 1024, 1280 and 1440+', async () => {
    const widths = [320, 360, 390, 430, 768, 1024, 1280, 1440];
    for (const width of widths) {
      const page = boot({ payload, incidents, history, viewport: width });
      await page.runtime.settle();
      const phoneLayout = page.dom.matches('(max-width: 760px)');
      assert.equal(phoneLayout, width <= 760,
        `the phone layout is ${phoneLayout ? 'on' : 'off'} at ${width}px`);
      // the menu button is the phone's control; a desktop must not need it
      assert.equal(page.dom.matches('(max-width: 980px)'), width <= 980,
        `the compact header rule resolves wrongly at ${width}px`);
      for (const view of ['overview', 'cities', 'alerts', 'data', 'about']) {
        clickNav(page.dom, view);
        assert.equal(page.dom.node(`view-${view}`).hidden, false,
          `#${view} did not open at ${width}px`);
        assert.ok(page.dom.html(`view-${view}`).length === 0
          || page.dom.html(`view-${view}`).length > 0, 'view rendered');
      }
      // the new hero and the subscription form are present and filled at every width, and the
      // page never reported a controller error while doing it
      assert.match(page.dom.text('ov-headline'), /\w/, `the hero is empty at ${width}px`);
      assert.ok(!/is not defined|not a function|Cannot read/i.test(page.dom.text('conn-detail')),
        `a render error reached the status line at ${width}px: ${page.dom.text('conn-detail')}`);
      // and no markup the Overview emits carries a fixed pixel width that a small screen would
      // have to scroll for
      const wide = page.dom.html('city-cards').match(/style="[^"]*width:\s*(\d+)px/g) || [];
      wide.forEach((declaration) => {
        const size = Number(/width:\s*(\d+)px/.exec(declaration)[1]);
        assert.ok(size <= 320, `a city card demands ${size}px at ${width}px`);
      });
    }
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
