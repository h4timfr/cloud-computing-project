/* M16 browser check: the retrospective panel in a real renderer, with the network counted.
 *
 * The milestone's rules that cannot be proven from a DOM harness:
 *   - one GET /zones/{id}/history request per City Detail view, and none on any other route;
 *   - the 1H/6H/24H/AVAILABLE buttons re-slice what is already in memory (no second request);
 *   - the window the stored history cannot cover is visible, disabled, and explains itself in the
 *     data's own words ("only 21 h 17 min of history so far"), computed - never hard-coded;
 *   - both charts and both text summaries exist and agree with each other and with the change block;
 *   - a stored incident that opened before the window is marked out of window, not hidden;
 *   - no console error, no failed request, no horizontal overflow and no clipped text,
 *     at 390, 768 and 1280 px.
 *
 * Usage: node tools/m16_browser_check.mjs --site http://127.0.0.1:8140 [--api http://127.0.0.1:8199] [--port 9222]
 *
 * This tool NEVER starts a browser: it attaches to the one already listening on the debugging port, the
 * same rule the rest of the M16 work follows. Start exactly one, e.g.
 *   msedge --headless=new --remote-debugging-port=9222 --user-data-dir=<temp> about:blank
 */

import fs from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
const option = (name, fallback) => {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};
const site = option('--site', '').replace(/\/$/, '');
const port = option('--port', '9222');
const api = option('--api', '').replace(/\/$/, '');
const city = option('--city', '');
if (!site) {
  console.error('usage: node tools/m16_browser_check.mjs --site http://127.0.0.1:8140 [--api http://127.0.0.1:8199] [--port 9222]');
  process.exit(2);
}
/* The API override is part of the URL, and it must sit before the hash: the page reads ?api= from
 * location.search, so a hash-only navigation would quietly fall back to the deployed API and every
 * request from a local origin would be blocked by CORS. */
const urlFor = (hash) => `${site}/${api ? `?api=${encodeURIComponent(api)}` : ''}${hash}`;

const WIDTHS = [{ w: 390, h: 900 }, { w: 768, h: 1000 }, { w: 1280, h: 1000 }];
let passed = 0;
const failures = [];
const check = (name, body) => {
  try {
    body();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (error) {
    failures.push(`${name}: ${error.message}`);
    console.log(`  FAIL ${name}\n         ${error.message}`);
  }
};
const assert = {
  ok: (value, message) => { if (!value) throw new Error(message); },
  equal: (actual, expected, message) => {
    if (actual !== expected) throw new Error(`${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  },
  match: (value, pattern, message) => {
    if (!pattern.test(String(value))) throw new Error(`${message}: ${JSON.stringify(String(value).slice(0, 200))}`);
  }
};

/* ---------------------------------------------------------------- attach --- */

let list;
try {
  list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
} catch (error) {
  console.error(`no DevTools endpoint on 127.0.0.1:${port} (${error.message}).`);
  console.error('Start exactly one browser with a debugging port, then run this again; this tool never starts one.');
  process.exit(2);
}
const targets = list.filter((one) => one.type === 'page');
if (targets.length === 0) {
  console.error(`no page target on 127.0.0.1:${port}`);
  process.exit(2);
}
const socket = new WebSocket(targets[0].webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true });
  socket.addEventListener('error', reject, { once: true });
});

let nextId = 0;
const pending = new Map();
const historyRequests = [];
const failedRequests = [];
const consoleErrors = [];
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(message.error.message));
    else resolve(message.result);
    return;
  }
  const params = message.params || {};
  if (message.method === 'Network.requestWillBeSent' && /\/history(\?|$)/.test(params.request ? params.request.url : '')) {
    historyRequests.push(params.request.url);
  }
  if (message.method === 'Network.loadingFailed' && !params.canceled) {
    failedRequests.push(`${params.errorText || 'failed'} ${params.type || ''}`.trim());
  }
  if (message.method === 'Runtime.exceptionThrown') {
    const detail = params.exceptionDetails || {};
    consoleErrors.push(detail.exception ? detail.exception.description : detail.text);
  }
  if (message.method === 'Log.entryAdded' && params.entry && params.entry.level === 'error') {
    const entry = params.entry;
    // a 404 or a 5xx that the page reports in its own words is a route it handled, not a broken page
    const url = entry.url || '';
    if (url.includes('execute-api') || url.includes('amazonaws.com')) return;
    consoleErrors.push(`${entry.text} ${url}`.trim());
  }
});

const send = (method, params) => new Promise((resolve, reject) => {
  const id = ++nextId;
  pending.set(id, { resolve, reject });
  socket.send(JSON.stringify({ id, method, params: params || {} }));
});
const evaluate = async (expression) => {
  const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) {
    const detail = result.exceptionDetails;
    throw new Error(`${detail.text} ${detail.exception ? detail.exception.description : ''}`);
  }
  return result.result.value;
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

await send('Page.enable');
await send('Runtime.enable');
await send('Log.enable');
// Log.enable replays whatever the page target already logged, including a previous run's CORS failures
// from a session where the API override was missing. Clearing it keeps this run's verdict its own.
await send('Log.clear');
await send('Network.enable');
// The profile outlives a run, and a cached index.html would let this check pass against copy that is no
// longer in the repository: the page under test has to be the bytes on disk.
await send('Network.setCacheDisabled', { cacheDisabled: true });

const goto = async (hash, width) => {
  await send('Emulation.setDeviceMetricsOverride', { width: width.w, height: width.h, deviceScaleFactor: 1, mobile: width.w < 768 });
  const before = historyRequests.length;
  await send('Page.navigate', { url: urlFor(hash) });
  // the page boots from a fetch of the fleet payload; wait for the panel to have drawn or said why
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await sleep(250);
    const ready = await evaluate("(() => { const m = document.getElementById('history-meta'); return Boolean(m && m.textContent && m.textContent.length > 4); })()");
    if (ready) break;
  }
  await sleep(400); // let a second request appear if the panel were to make one
  return historyRequests.length - before;
};

const panel = () => evaluate(`(() => {
  const text = (id) => { const node = document.getElementById(id); return node ? node.textContent.replace(/\\s+/g, ' ').trim() : null; };
  const canvas = document.getElementById('history-canvas');
  const rain = document.getElementById('rain-chart');
  const buttons = Array.prototype.slice.call(document.querySelectorAll('#history-range button'));
  const box = (node) => {
    if (!node) return null;
    const style = getComputedStyle(node);
    const scrolls = node.scrollWidth > node.clientWidth + 1 && !['auto', 'scroll'].includes(style.overflowX);
    return { clipped: scrolls, hidden: style.visibility === 'hidden' || style.display === 'none' };
  };
  const incidentRows = Array.prototype.slice.call(document.querySelectorAll('#city-incidents [data-incident]'));
  return {
    meta: text('history-meta'),
    note: text('history-window-note'),
    summary: text('history-summary'),
    rainSummary: text('rain-summary'),
    facts: text('history-facts'),
    factsHtml: document.getElementById('history-facts') ? document.getElementById('history-facts').innerHTML : '',
    riskShape: canvas ? { lines: canvas.querySelectorAll('polyline').length, fills: canvas.querySelectorAll('rect.chart-fill').length, guides: canvas.querySelectorAll('line.chart-guide').length, label: canvas.getAttribute('aria-label') } : null,
    rainShape: rain ? { lines: rain.querySelectorAll('polyline').length, fills: rain.querySelectorAll('rect.chart-fill').length, label: rain.getAttribute('aria-label') } : null,
    buttons: buttons.map((button) => ({ range: button.getAttribute('data-range'), pressed: button.getAttribute('aria-pressed'), disabled: button.disabled, explains: button.getAttribute('aria-describedby') })),
    incidents: incidentRows.map((row) => ({ id: row.getAttribute('data-incident'), status: row.getAttribute('data-status'), inWindow: row.getAttribute('data-in-window'), marked: Boolean(row.querySelector('.incident-outside')) })),
    incidentsEmpty: (() => { const empty = document.querySelector('#city-incidents [data-empty]'); return empty ? empty.textContent.replace(/\\s+/g, ' ').trim() : null; })(),
    scope: (() => { const node = document.querySelector('#city-incidents [data-f="incident-scope"]'); return node ? node.textContent.replace(/\\s+/g, ' ').trim() : null; })(),
    clipped: ['history-summary', 'rain-summary', 'history-window-note', 'history-facts']
      .map((id) => ({ id, box: box(document.getElementById(id)) }))
      .filter((entry) => entry.box && (entry.box.clipped || entry.box.hidden)),
    overflow: document.documentElement.scrollWidth - window.innerWidth,
    title: document.title
  };
})()`);

/* The profile's HTTP cache outlives the repository: load once, reload past the cache, and only then start
 * counting, so the page under test is the build that is actually on disk. */
await send('Page.navigate', { url: urlFor('#/') });
await sleep(1200);
await send('Page.reload', { ignoreCache: true });
await sleep(1800);
historyRequests.length = 0;

const cityId = city || await (async () => {
  // the leader of the fleet the page is showing, read from the page itself rather than guessed.
  // Against the deployed site the first /zones call can be a cold Lambda plus a provider refresh, so
  // the grid is polled instead of being read once: the discovery is convenience, not a contract.
  await goto('#/cities', WIDTHS[2]);
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const leader = await evaluate(`(() => {
      const first = document.querySelector('a.zone-link[href*="#/cities/"]');
      return first ? first.getAttribute('href').split('#/cities/')[1] : null;
    })()`);
    if (leader) return leader;
    await sleep(500);
  }
  return null;
})();
if (!cityId) {
  console.error('no city could be read from the Cities page');
  process.exit(2);
}
console.log(`city under test: ${cityId}`);

/* --------------------------------------------- what the public Data page says --- */

/* Rendered, not echoed: the strings below are read out of the live DOM, with every collapsed technical
 * block removed first, so "visible on the normal public page" means exactly that. */
await goto('#/data', WIDTHS[2]);
const publicData = await evaluate(`(() => {
  const view = document.getElementById('view-data');
  if (!view) return null;
  const clone = view.cloneNode(true);
  [...clone.querySelectorAll('details:not([open])')].forEach((node) => node.remove());
  const text = clone.textContent.replace(/\\s+/g, ' ').trim();
  const links = [...view.querySelectorAll('a[href]')].map((a) => a.getAttribute('href'));
  const opened = [...view.querySelectorAll('details')].map((d) => d.hasAttribute('open'));
  return JSON.stringify({ text: text, links: links, opened: opened });
})()`);
const dataPage = JSON.parse(publicData || '{}');
check('the default Data page does not name the API host, the secret or the write route', () => {
  for (const banned of ['execute-api', 'amazonaws.com', 'shared secret', 'POST /readings', 'readings?', 'x-fp-key']) {
    assert.ok(!String(dataPage.text || '').includes(banned), `the visible Data page shows "${banned}"`);
  }
});
check('the default Data page says where its data comes from, in plain words', () => {
  assert.match(String(dataPage.text || ''), /served through its backend API/, 'the backend sentence is missing');
  assert.match(String(dataPage.text || ''), /current city state, recent historical readings and incident information/,
    'the sentence does not say what the dashboard reads');
});
check('the public Data page links nowhere near the API origin', () => {
  const bad = (dataPage.links || []).filter((href) => /execute-api|amazonaws\.com/.test(href));
  assert.equal(bad.length, 0, `the Data page links to ${bad.join(', ')}`);
});
const technical = await (async () => {
  // the Data view can hold more than one disclosure, so the technical one is found by its summary
  await evaluate(`(() => {
    const d = [...document.querySelectorAll('#view-data details')].find((n) => /technical/i.test(n.textContent));
    if (d) d.open = true;
    return true;
  })()`);
  await sleep(200);
  return evaluate(`(() => {
    const d = [...document.querySelectorAll('#view-data details')].find((n) => /technical/i.test(n.textContent));
    return d ? d.textContent.replace(/\\s+/g, ' ').trim() : null;
  })()`);
})();
check('expanding the technical block still explains the architecture, and stops at the reads', () => {
  assert.ok(technical, 'the technical block is missing');
  for (const route of ['GET /zones', 'GET /zones/{zone_id}/history', 'GET /incidents']) {
    assert.ok(technical.includes(route), `the technical block no longer lists ${route}`);
  }
  assert.ok(!/POST|shared secret|execute-api|amazonaws/.test(technical),
    'the technical block still describes the write path or the host');
  assert.match(technical, /read-only and public/, 'the technical block does not describe the routes it lists');
});

/* ------------------------------------------------- the single-request rule --- */

const otherRoutes = ['#/cities', '#/alerts', '#/data', '#/about', '#/'];
for (const hash of otherRoutes) {
  const made = await goto(hash, WIDTHS[2]);
  check(`${hash} asks for no history at all`, () => assert.equal(made, 0, `history requests on ${hash}`));
}

let firstOpen = 0;
for (const width of WIDTHS) {
  const made = await goto(`#/cities/${cityId}`, width);
  // The first open of the view is the one that must fetch. Re-entering the same city inside the run
  // re-uses the series the page already holds: fetching it again would be the N+1 the milestone forbids.
  if (width.w === 390) firstOpen = made;
  if (width.w === 390) {
    check(`${width.w}px: opening a city view asks for its history exactly once`, () => assert.equal(made, 1, `history requests at ${width.w}px`));
  } else {
    check(`${width.w}px: re-entering the same city re-uses the loaded series`, () => assert.equal(made, 0, `history requests at ${width.w}px`));
  }
}
check('the first city view made exactly one history request', () => assert.equal(firstOpen, 1, `history requests on the first open`));

/* -------------------------------------------------------------- the panel --- */

await goto(`#/cities/${cityId}`, WIDTHS[2]);
const seen = await panel();

check('the single request asked for the widest window the API keeps', () => {
  assert.ok(historyRequests.length > 0, 'no history request was recorded');
  assert.match(historyRequests[historyRequests.length - 1], /minutes=1440/, 'the request did not ask for the widest window');
});
check('the panel states how much history it holds and what the request was', () => {
  assert.match(seen.meta, /\d+ points · asked for \d+ min · (full resolution|downsampled)/, 'the meta line does not describe the payload');
});
check('the score chart is drawn with its bands, its edges and a line', () => {
  assert.ok(seen.riskShape, 'the risk chart element is missing');
  assert.ok(seen.riskShape.lines >= 1, 'the risk chart has no line');
  assert.equal(seen.riskShape.fills, 4, 'the score chart should shade four bands');
  assert.ok(seen.riskShape.guides >= 5, 'the score chart should draw the band edges as guides');
  assert.match(seen.riskShape.label, /Risk score over time/, 'the chart has no accessible label');
});
check('the rainfall chart is drawn and is labelled as a rate', () => {
  assert.ok(seen.rainShape, 'the rainfall chart element is missing');
  assert.ok(seen.rainShape.lines >= 1, 'the rainfall chart has no line');
  assert.equal(seen.rainShape.fills, 1, 'the rainfall chart should draw one plot area, not score bands');
  assert.match(seen.rainShape.label, /Rainfall rate over time, mm\/h/, 'the rainfall chart is not labelled as a rate');
});
check('both text summaries exist and describe their own chart', () => {
  assert.match(seen.summary, /Risk score \d\.\d\d to \d\.\d\d over/, 'the risk summary does not state the range and span');
  assert.match(seen.rainSummary, /Rainfall rate|No stored rainfall reading/, 'the rainfall summary is missing');
  assert.match(seen.rainSummary, /Instantaneous rate, not an accumulation|No stored rainfall reading/, 'the rainfall summary could be read as an accumulation');
});
check('the change block agrees with the sentence under the chart', () => {
  const riskNow = /Risk now\s*([0-9.]+)/.exec(seen.facts);
  assert.ok(riskNow, 'the change block has no "Risk now" figure');
  assert.match(seen.summary, new RegExp(`ending ${riskNow[1]}`), 'the summary and the change block disagree about the newest score');
  // dt and dd are separate nodes, so textContent joins them without a space: "Risk now0.23 · NORMAL".
  assert.match(seen.facts, /Net change\s*[+-]?\d/, 'the change block does not state how much changed');
  assert.match(seen.facts, /Peak\s*\d\.\d\d at \d\d:\d\d:\d\dZ/, 'the change block does not say when it peaked');
});
check('the window the stored history cannot cover is disabled and explains itself with the span', () => {
  const day = seen.buttons.find((button) => button.range === '1440');
  assert.ok(day, 'there is no 24H button');
  assert.equal(day.disabled, true, 'the 24H window is offered although the history cannot cover it');
  assert.equal(day.explains, 'history-window-note', 'the disabled button is not tied to the note that explains it');
  assert.match(seen.note, /24H window is not offered yet: only .+ of history so far/, 'the note does not explain the disabled window in the data\'s own words');
});
/* All measurements are taken here, at the top level, so that every check below is synchronous and a
 * failure inside it is counted rather than turned into an unhandled rejection. */
const afterSix = await (async () => {
  // The page remembers the window a reader chose, and the browser profile outlives one run of this
  // check, so the starting window is pinned to AVAILABLE first: otherwise clicking 6H can be the
  // no-op of clicking the window that was already selected.
  await evaluate(`(() => { const all = document.querySelector('#history-range [data-range="all"]'); if (all) all.click(); return true; })()`);
  await sleep(500);
  const beforePanel = await panel();
  const before = historyRequests.length;
  await evaluate(`(() => { const button = document.querySelector('#history-range [data-range="360"]'); if (button) button.click(); return true; })()`);
  await sleep(500);
  return { before: before, requests: historyRequests.length, panel: await panel(), previous: beforePanel };
})();
const afterAll = await (async () => {
  const before = historyRequests.length;
  await evaluate(`(() => { const button = document.querySelector('#history-range [data-range="all"]'); if (button) button.click(); return true; })()`);
  await sleep(400);
  return { before: before, requests: historyRequests.length, panel: await panel() };
})();

check('choosing another window re-slices the series without asking the API again', () => {
  assert.equal(afterSix.requests, afterSix.before, 'choosing a window made another history request');
  assert.ok(afterSix.panel.summary !== afterSix.previous.summary, 'the summary sentence did not follow the window');
  assert.match(String((afterSix.panel.buttons.find((button) => button.range === '360') || {}).pressed), /true/, 'the chosen window is not marked as pressed');
});
check('AVAILABLE shows the whole stored series and still asks for nothing', () => {
  assert.equal(afterAll.requests, afterAll.before, 'the AVAILABLE window made another history request');
  assert.ok(afterAll.panel.riskShape && afterAll.panel.riskShape.lines >= 1, 'the AVAILABLE window drew no line');
});

/* ------------------------------------------------------------- incidents --- */

check('the incident timeline marks what the window cannot show', () => {
  const page = afterAll.panel;
  if (page.incidents.length === 0) {
    assert.match(page.incidentsEmpty || '', /No alerts for this|No WARNING or CRITICAL/, 'the empty state does not say what was not recorded');
    return;
  }
  page.incidents.forEach((row) => {
    assert.ok(row.id, 'an incident row has no id');
    assert.ok(row.inWindow === 'true' || row.inWindow === 'false', 'an incident row does not say whether it is in the window');
    if (row.inWindow === 'false') assert.ok(row.marked, `an out-of-window incident is not marked: ${row.id}`);
  });
});
check('the timeline says what an incident is, and does not present it as a flood report', () => {
  const scope = afterAll.panel.scope;
  assert.ok(scope, 'the incident timeline has no description');
  assert.match(scope, /Band crossings|threshold|WARNING or CRITICAL/, 'the timeline does not say what it is');
  assert.match(scope, /not a flood report|no flood report|Nothing here is a flood report/, 'the timeline could be read as a record of floods');
});

/* ------------------------------------------------------------ page health --- */

for (const width of WIDTHS) {
  await goto(`#/cities/${cityId}`, width);
  const page = await panel();
  check(`${width.w}px: the page never scrolls sideways`, () => {
    assert.ok(page.overflow <= 1, `the document is ${page.overflow}px wider than the viewport`);
  });
  check(`${width.w}px: the panel's own text is not clipped`, () => {
    assert.equal(page.clipped.length, 0, `clipped: ${JSON.stringify(page.clipped)}`);
  });
}

check('no script error and no failed request reached the page', () => {
  assert.equal(consoleErrors.length, 0, `console errors: ${consoleErrors.slice(0, 3).join(' | ')}`);
  assert.equal(failedRequests.length, 0, `failed requests: ${failedRequests.slice(0, 3).join(' | ')}`);
});

check('a city view never asks for another city, and asks at all', () => {
  assert.ok(historyRequests.length >= 1, `no history request was made for ${cityId}`);
  assert.ok(historyRequests.length <= WIDTHS.length + 1, `${historyRequests.length} history requests for one city under test is more than a view needs`);
  historyRequests.forEach((url) => assert.match(url, new RegExp(`/zones/${cityId}/history`), 'a history request was made for another city'));
});

socket.close();
console.log(`\nm16 browser check: ${passed} check(s) passed, ${failures.length} failed`);
failures.forEach((failure) => console.log(`  - ${failure}`));
process.exit(failures.length ? 1 : 0);
