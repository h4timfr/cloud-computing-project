#!/usr/bin/env node
/*
 * Dashboard runtime harness (development tool, not part of the site).
 *
 * dashboard_check.mjs only inspects the sources; this file actually EXECUTES the real
 * web/lib.js + web/app.js in a VM against a minimal fake DOM and drives the polling
 * controller, so a runtime error inside a panel (e.g. a missing local function) fails
 * here instead of silently marking the deployed page STALE.
 *
 * Usage:
 *   node tools/dashboard_run.mjs --payload build/zones-live.json [--history f] [--incidents f]
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

const args = process.argv.slice(2);
const option = (name) => {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : null;
};

const siteUrl = option('--site');
const apiBase = option('--api');
// app.js returns early when no API base is configured, so always give it one; the fetch
// stub below serves the payloads by route, which is what the page actually consumes.
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

function group(title) {
  console.log(`\n${title}`);
}

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

/* -------------------------------------------------------------------- fake DOM --- */

const record = { html: new Map(), text: new Map(), classes: new Map() };

function makeContext() {
  const noop = () => {};
  return new Proxy({}, {
    get: (target, prop) => {
      if (prop === 'measureText') return () => ({ width: 12 });
      if (prop === 'createLinearGradient') return () => ({ addColorStop: noop });
      if (prop in target) return target[prop];
      return noop;
    },
    set: (target, prop, value) => { target[prop] = value; return true; }
  });
}

function makeNode(id) {
  const node = {
    id,
    className: '',
    textContent: '',
    value: '',
    width: 800,
    height: 300,
    clientWidth: 800,
    clientHeight: 300,
    children: [],
    _html: '',
    _attrs: new Map(),
    dataset: {},
    style: { setProperty: () => {}, removeProperty: () => {}, getPropertyValue: () => '' },
    classList: {
      _set: new Set(),
      add(...names) { names.forEach((n) => this._set.add(n)); },
      remove(...names) { names.forEach((n) => this._set.delete(n)); },
      contains(name) { return this._set.has(name); },
      toggle(name) { this._set.has(name) ? this._set.delete(name) : this._set.add(name); }
    },
    get innerHTML() { return this._html; },
    set innerHTML(value) { this._html = String(value); record.html.set(this.id, this._html); },
    setAttribute(name, value) { this._attrs.set(name, String(value)); },
    getAttribute(name) { return this._attrs.has(name) ? this._attrs.get(name) : null; },
    removeAttribute(name) { this._attrs.delete(name); },
    appendChild(child) { this.children.push(child); return child; },
    removeChild(child) { return child; },
    addEventListener: () => {},
    removeEventListener: () => {},
    querySelector: () => makeNode('stub'),
    querySelectorAll: () => [],
    closest: () => null,
    getContext: () => makeContext(),
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 300, right: 800, bottom: 300 }),
    focus: () => {},
    blur: () => {},
    remove: () => {}
  };
  let text = String(node.textContent);
  Object.defineProperty(node, 'textContent', {
    get() { return text; },
    set(value) { text = String(value); record.text.set(this.id, text); },
    configurable: true
  });
  return node;
}

const nodes = new Map();
const body = makeNode('body');

const documentStub = {
  body,
  documentElement: makeNode('html'),
  visibilityState: 'visible',
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  },
  querySelector: () => makeNode('stub'),
  querySelectorAll: () => [],
  createElement: (tag) => makeNode(tag),
  addEventListener: () => {},
  removeEventListener: () => {}
};

/* ---------------------------------------------------------------------- run --- */

let fakeNow = Date.parse('2026-10-15T17:00:00Z');
const timers = [];
const requests = [];

function makeSandbox(payload, history, incidents) {
  const fetchStub = (url) => {
    requests.push(url);
    const route = String(url).split('?')[0];
    let data = null;
    if (route.endsWith('/zones')) data = payload;
    else if (route.endsWith('/incidents')) data = incidents;
    else if (route.includes('/history')) data = history;
    if (data === null) {
      return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({ error: 'not_found' }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) });
  };

  class FakeDate extends Date {
    constructor(...values) { super(...(values.length ? values : [fakeNow])); }
    static now() { return fakeNow; }
  }

  const sandbox = {
    console: { log: () => {}, warn: () => {}, error: () => {} },
    window: {
      location: { search: `?api=${encodeURIComponent(apiBaseForRun)}`, href: 'http://localhost/' },
      FLOODPULSE_API_BASE: apiBaseForRun,
      addEventListener: () => {},
      removeEventListener: () => {},
      devicePixelRatio: 1,
      matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} })
    },
    document: documentStub,
    fetch: fetchStub,
    Date: FakeDate,
    Math,
    JSON,
    Number,
    String,
    Boolean,
    Array,
    Object,
    Promise,
    Error,
    isFinite,
    parseInt,
    parseFloat,
    encodeURIComponent,
    decodeURIComponent,
    URLSearchParams,
    AbortController,
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
    setInterval: (fn, delay) => { timers.push({ fn, delay }); return timers.length; },
    clearInterval: () => {},
    setTimeout: () => 0, // inert: a fired abort-timer would add noise, polling is setInterval-based
    clearTimeout: () => {},
    requestAnimationFrame: () => 0
  };
  sandbox.window.document = documentStub;
  sandbox.globalThis = sandbox;
  return sandbox;
}

const settle = async () => {
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
};

// Invoke the registered intervals until one of them actually issues requests — that is the
// polling controller, whatever constant POLL_MS currently is. Tickers that only repaint
// (e.g. the incident duration ticker) are harmless to fire.
async function drivePoll() {
  const before = requests.length;
  for (const timer of timers) {
    timer.fn();
    await settle();
    if (requests.length > before) return true;
  }
  return false;
}

async function main() {
  const libSource = await readSource(libPath, 'lib.js');
  const appSource = await readSource(appPath, 'app.js');
  const { payload, incidents, history, source } = await loadPayloads();
  console.log(`  payload source: ${source}`);

  const sandbox = makeSandbox(payload, history, incidents);
  vm.createContext(sandbox);

  group('Executing the dashboard assets against the payload');
  let loadError = null;
  try {
    vm.runInContext(libSource, sandbox, { filename: libPath });
    vm.runInContext(appSource, sandbox, { filename: appPath });
  } catch (error) {
    loadError = error;
  }
  check('lib.js and app.js load and start without throwing', () => {
    assert.equal(loadError, null, loadError ? String(loadError.message) : '');
    assert.ok(requests.length > 0, 'the dashboard never issued a request');
  });

  await settle();

  const badge = nodes.get('conn-badge');
  const detail = nodes.get('conn-detail');
  const ranking = nodes.get('ranking');

  check('the first poll renders the risk ranking for every zone', () => {
    const html = ranking ? ranking.innerHTML : '';
    assert.match(html, /rank-row/, 'ranking panel is still the static placeholder');
    for (const zone of payload.zones) {
      assert.ok(html.includes(zone.name), `ranking omitted ${zone.name}`);
    }
    const rows = (html.match(/class="rank-row/g) || []).length;
    assert.equal(rows, payload.zones.length, `expected ${payload.zones.length} ranking rows, found ${rows}`);
  });

  check('the ranking is ordered by risk score, highest first', () => {
    const html = ranking ? ranking.innerHTML : '';
    const order = [...html.matchAll(/class="rank-row[^"]*" data-zone="([^"]+)"/g)].map((m) => m[1]);
    assert.equal(order.length, payload.zones.length);
    const scores = new Map(payload.zones.map((z) => [z.zone_id, z.latest ? z.latest.risk_score : -1]));
    for (let i = 1; i < order.length; i += 1) {
      assert.ok(scores.get(order[i - 1]) >= scores.get(order[i]),
        `ranking order ${order[i - 1]} before ${order[i]} is not descending`);
    }
  });

  check('the page reports LIVE after a successful poll', () => {
    assert.ok(badge, 'no connection badge');
    assert.equal(badge.textContent, 'LIVE', `badge reads "${badge.textContent}"`);
    assert.ok(body.classList.contains('is-live'), 'body is not marked is-live');
    assert.ok(!body.classList.contains('is-stale'), 'body is marked is-stale');
  });

  check('no panel raised a runtime error', () => {
    const message = detail ? String(detail.textContent) : '';
    assert.ok(!/is not defined|not a function|Cannot read/i.test(message),
      `the controller reported: ${message}`);
  });

  check('the summary and zone cards were filled from the payload', () => {
    const summary = nodes.get('summary-live');
    assert.ok(summary && summary.textContent && summary.textContent !== '–',
      'fleet summary was not updated');
    const hero = nodes.get('hero');
    assert.ok(hero && /hero-name|Awaiting live data/.test(hero.innerHTML), 'hero panel was not rendered');
  });

  check('the provenance banner and chips match the payload mode', () => {
    // DOM-level proof that the page states what is real and what is not, for whichever
    // mode the API is serving. A legacy payload that reports no zone_mode must degrade
    // to "unknown" -- never to a REAL claim.
    const liveMode = payload.zone_mode === 'live_weather';
    const bannerTitle = nodes.get('banner-title');
    const bannerDetail = nodes.get('banner-detail');
    assert.ok(bannerTitle, 'no banner title element');
    assert.ok(bannerDetail, 'no banner detail element');
    assert.ok(bannerTitle.textContent && bannerTitle.textContent !== '', 'banner title is empty');
    assert.ok(bannerDetail.textContent && bannerDetail.textContent !== '', 'banner detail is empty');

    if (liveMode) {
      assert.match(bannerTitle.textContent, /LIVE WEATHER/);
      assert.match(bannerDetail.textContent, /Open-Meteo/);
    } else {
      assert.match(bannerTitle.textContent, /SIMULATED/);
      assert.match(bannerDetail.textContent, /SIMULATED/);
    }

    const zonesHtml = nodes.get('zones') ? nodes.get('zones').innerHTML : '';
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

    const summaryMode = nodes.get('summary-mode');
    assert.ok(summaryMode && summaryMode.textContent && summaryMode.textContent !== '–',
      'the fleet summary does not report the mode');
  });

  group('Staying LIVE while fresh data keeps arriving');
  let polls = 0;
  for (let tick = 1; tick <= 3 && polls < 2; tick += 1) {
    fakeNow += 5000;
    if (await drivePoll()) polls += 1;
  }
  check('two further polls (5s apart) keep the page LIVE', () => {
    assert.equal(loadError, null, loadError ? String(loadError.message) : '');
    assert.equal(polls, 2, `only ${polls} additional poll(s) were driven`);
    assert.equal(badge.textContent, 'LIVE', `badge reads "${badge.textContent}"`);
    assert.ok(body.classList.contains('is-live'), 'body lost is-live');
    assert.ok(requests.length >= 3, `expected repeated polling, saw ${requests.length} requests`);
    const html = ranking.innerHTML;
    for (const zone of payload.zones) assert.ok(html.includes(zone.name), `ranking lost ${zone.name}`);
  });

  /* The board has to say what moved between polls, so drive a real change through the real app:
   * the stub serves the same objects, so mutating the payload between polls is exactly what a
   * changed API response looks like to this code. A stage fixture may already sit at the top
   * band, in which case the movement to check is the other direction. */
  const note = nodes.get('change-note');
  const ladder = ['NORMAL', 'WATCH', 'WARNING', 'CRITICAL'];
  const rankOf = (value) => ladder.indexOf(String(value === null || value === undefined ? '' : value).toUpperCase());
  const movable = payload.zones.find((zone) => zone.latest && rankOf(zone.latest.risk_class) >= 0
    && rankOf(zone.latest.risk_class) < ladder.length - 1);
  const mover = movable || payload.zones.find((zone) => zone.latest && rankOf(zone.latest.risk_class) > 0);
  const originalClass = mover ? mover.latest.risk_class : null;
  const movingUp = Boolean(movable);
  const nextClass = movingUp
    ? ladder[rankOf(originalClass) + 1]
    : ladder[rankOf(originalClass) - 1];

  group('Saying what changed between polls');
  check('a quiet poll shows no change note', () => {
    assert.ok(note, 'index.html has no change-note element');
    assert.equal(note.textContent, '', `the note claims "${note.textContent}" with nothing changed`);
    assert.equal(note.hidden, true, 'the empty note is still visible');
  });

  mover.latest.risk_class = nextClass;
  mover.latest.risk_score = Math.max(Number(mover.latest.risk_score) || 0, 0.9);
  fakeNow += 5000;
  const moved = await drivePoll();

  check('a change between two polls is named on the page', () => {
    assert.ok(moved, 'the poll after the change never ran');
    const verb = movingUp ? 'escalated' : 'eased';
    assert.match(note.textContent, new RegExp(`^${mover.name} ${verb} .+ → ${nextClass}$`),
      `the note reads "${note.textContent}" instead of naming the move`);
    assert.equal(note.hidden, false, 'the note stayed hidden while the fleet moved');
    const expectedTone = movingUp ? nextClass : 'NORMAL';
    assert.equal(note.className, 'change-note band-' + expectedTone.toLowerCase(),
      `the note reads "${note.textContent}" but is styled "${note.className}"`);
  });

  fakeNow += 5000;
  const settled = await drivePoll();
  check('the change is stated once, not repeated forever', () => {
    assert.ok(settled, 'the follow-up poll never ran');
    assert.equal(note.textContent, '', `the note still claims "${note.textContent}"`);
    assert.equal(note.hidden, true, 'the stale note is still on screen');
  });

  mover.latest.risk_class = originalClass;  // leave the fixture as the file describes it

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
