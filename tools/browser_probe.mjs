/* M11.2 browser probe: what the deployed page actually measures in a real renderer.
 *
 * The milestone asks for 320/390/768/desktop verification and forbids claiming pixel-perfect without
 * a browser. A screenshot proves a render happened; it does not prove the control bar fits at 320px.
 * So this drives headless Edge over the DevTools protocol (Node 24 has WebSocket built in) and reads
 * real geometry out of the rendered page:
 *
 *   - documentElement.scrollWidth against innerWidth: horizontal scrolling, or not;
 *   - the toolbar's four fields, grouped into rows by their own top edges: 1 row, 2 rows, 4 stacked;
 *   - each field inside the content box, and each field's height, for the 44px touch target;
 *   - one city card's metric rows: the x-ranges of name, chip, value and bar, in the order and on the
 *     lines the design claims - side by side on a wide screen, two lines on a phone;
 *   - any box that sticks out of the viewport, with its details (.sr-only aside: it is off-screen on
 *     purpose);
 *   - script errors, kept apart from failed requests: a 404 from the API is a route the page reports
 *     in its own words, not a broken page.
 *
 * Usage: node tools/browser_probe.mjs <site-url> [--port 9222]
 */

import { setTimeout as sleep } from 'node:timers/promises';

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const site = (process.argv[2] || '').replace(/\/$/, '');
if (!site) {
  console.error('usage: node tools/browser_probe.mjs <site-url> [--port 9222]');
  process.exit(2);
}
const portArg = process.argv.indexOf('--port');
const port = portArg > 0 ? process.argv[portArg + 1] : '9222';

const WIDTHS = [
  { w: 320, h: 1000, label: '320px phone' },
  { w: 360, h: 1000, label: '360px phone' },
  { w: 390, h: 1000, label: '390px phone' },
  { w: 430, h: 1000, label: '430px phone' },
  { w: 600, h: 1000, label: '600px large phone' },
  { w: 768, h: 1000, label: '768px tablet' },
  { w: 900, h: 1000, label: '900px tablet' },
  { w: 1024, h: 1000, label: '1024px small laptop' },
  { w: 1200, h: 1000, label: '1200px laptop' },
  { w: 1280, h: 1000, label: '1280px desktop' },
  { w: 1440, h: 1000, label: '1440px desktop' },
];

/* One expression, given a selector for the card whose metric rows to measure. It returns data, never
 * a thrown error: a measurement that cannot run should say so in the output. */
const PROBE = `((args) => {
  try {
    const rect = (node) => {
      if (!node) return null;
      const r = node.getBoundingClientRect();
      return { left: Math.round(r.left), right: Math.round(r.right), top: Math.round(r.top),
               bottom: Math.round(r.bottom), width: Math.round(r.width), height: Math.round(r.height) };
    };
    const text = (node) => (node ? (node.textContent || '').trim().replace(/\\s+/g, ' ') : '');
    const fields = ['search', 'region', 'risk', 'sort'].map((name) => {
      const node = document.querySelector('.toolbar-field-' + name);
      return { name, box: rect(node), scrollWidth: node ? node.scrollWidth : null,
               clientWidth: node ? node.clientWidth : null };
    });
    const rows = [];
    fields.forEach((one) => {
      if (!one.box) return;
      const row = rows.find((candidate) => Math.abs(candidate.top - one.box.top) <= 4);
      if (row) row.fields.push(one.name);
      else rows.push({ top: one.box.top, fields: [one.name] });
    });
    const card = document.querySelector(args.selector) || document.querySelector('#zones .zone-card');
    const metrics = [...(card ? card.querySelectorAll('.metric') : [])].map((row) => {
      const cell = (selector) => row.querySelector(selector);
      const style = getComputedStyle(row);
      return { metric: row.getAttribute('data-metric'),
               name: rect(cell('.metric-name')), chip: rect(cell('.metric-source')),
               value: rect(cell('.metric-value')), track: rect(cell('.metric-track')),
               columns: style.gridTemplateColumns, gap: style.columnGap,
               chipText: text(cell('.metric-source')), valueText: text(cell('.metric-value')) };
    });
    const tiles = [...(card ? card.querySelectorAll('.tile') : [])].map((tile) => {
      const head = tile.querySelector('.tile-head');
      const chip = tile.querySelector('.src-chip');
      const value = tile.querySelector('.tile-value');
      return { head: rect(head), chip: rect(chip), value: rect(value),
               chipText: text(chip), valueText: text(value),
               clipped: head ? head.scrollWidth > head.clientWidth + 1 : false,
               columns: getComputedStyle(tile).gridTemplateColumns };
    });
    const offenders = [];
    const leftOvers = [];
    const clippedBy = (node) => {
      for (let parent = node.parentElement; parent; parent = parent.parentElement) {
        const overflow = getComputedStyle(parent);
        if (/hidden|clip|scroll|auto/.test(overflow.overflowX + ' ' + overflow.overflowY)) return true;
      }
      return false;
    };
    [...document.querySelectorAll('body *')].forEach((node) => {
      const box = node.getBoundingClientRect();
      const cls = node.className.toString();
      // off-screen furniture: the visually-hidden helpers and the skip link, until it is focused
      if (box.width <= 1 || box.height <= 1 || cls.indexOf('sr-only') >= 0) return;
      if (box.left <= -1000 || cls.indexOf('skip-link') >= 0) return;
      // a box an ancestor clips is not overflow: it cannot scroll the page or sit over anything
      if (clippedBy(node)) return;
      if (box.right > window.innerWidth + 1 && offenders.length < 5) {
        offenders.push({ tag: node.tagName.toLowerCase(), cls: cls.slice(0, 44), side: 'right',
                         left: Math.round(box.left), right: Math.round(box.right),
                         text: text(node).slice(0, 28) });
      }
      // an overhang to the left cannot scroll the page: it is reported as a note, not a failure
      if (box.left < -1 && leftOvers.length < 5) {
        leftOvers.push({ tag: node.tagName.toLowerCase(), cls: cls.slice(0, 44), side: 'left',
                         left: Math.round(box.left), right: Math.round(box.right),
                         text: text(node).slice(0, 28) });
      }
    });
    const clipped = [...document.querySelectorAll('#cities-toolbar *, #zones .metric *')]
      .filter((node) => node.scrollWidth > node.clientWidth + 1 && node.clientWidth > 0)
      .slice(0, 5)
      .map((node) => ({ cls: node.className.toString().slice(0, 36), scrollWidth: node.scrollWidth,
                        clientWidth: node.clientWidth }));
    return {
      innerWidth: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      bodyScrollWidth: document.body.scrollWidth,
      padX: getComputedStyle(document.documentElement).getPropertyValue('--pad-x').trim(),
      toolbar: rect(document.querySelector('.cities-toolbar')),
      // the declared layout: a grid row per quoted run of names, plus the column count
      toolbarAreas: (() => {
        const node = document.querySelector('.cities-toolbar');
        if (!node) return '';
        return getComputedStyle(node).gridTemplateAreas;
      })(),
      toolbarColumns: (() => {
        const node = document.querySelector('.cities-toolbar');
        return node ? getComputedStyle(node).gridTemplateColumns : '';
      })(),
      rows: rows.map((row) => row.fields),
      fields,
      count: text(document.querySelector('#cities-count') || document.querySelector('#city-name') || document.querySelector('h1')),
      cards: document.querySelectorAll('#zones .zone-card').length,
      metrics, tiles, offenders, leftOvers, clipped,
      detail: (() => {
        const slot = document.querySelector('#city-detail');
        const view = document.querySelector('#view-city');
        return { slot: !!slot, view: !!view, viewHidden: view ? view.hidden : null,
                 children: slot ? slot.children.length : 0,
                 metrics: slot ? slot.querySelectorAll('.metric').length : 0,
                 length: slot ? slot.innerHTML.length : 0 };
      })(),
    };
  } catch (error) {
    return { probeFailure: String((error && error.stack) || error) };
  }
})`;

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const page = list.find((one) => one.type === 'page');
if (!page) {
  console.error(`no page target on 127.0.0.1:${port}`);
  process.exit(2);
}

const socket = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true });
  socket.addEventListener('error', reject, { once: true });
});

let nextId = 0;
const pending = new Map();
const scriptErrors = [];
const resourceErrors = [];
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(message.error.message));
    else resolve(message.result);
    return;
  }
  if (message.method === 'Runtime.exceptionThrown') {
    const detail = message.params.exceptionDetails;
    scriptErrors.push(`exception: ${detail.exception ? detail.exception.description : detail.text}`);
  }
  if (message.method === 'Log.entryAdded' && message.params.entry.level === 'error') {
    const entry = message.params.entry;
    const url = entry.url || '';
    if (url.includes('execute-api') || url.includes('amazonaws.com')) resourceErrors.push(`${entry.text} ${url}`);
    else scriptErrors.push(`${entry.text} ${url}`);
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
    const where = `${(detail.lineNumber || 0) + 1}:${detail.columnNumber || 0}`;
    throw new Error(`${detail.text} at ${where} ${detail.exception ? detail.exception.description : ''}`);
  }
  return result.result.value;
};

await send('Page.enable');
await send('Runtime.enable');
await send('Log.enable');

const failures = [];
let zoneId = null;
const note = (ok, text) => {
  if (!ok) failures.push(text);
  return `${ok ? 'ok  ' : 'FAIL'} ${text}`;
};
const scrollWidth_text = (state) => `no horizontal scroll: scrollWidth ${state.scrollWidth}, innerWidth ${state.innerWidth}`;

const measure = async (url, selector, ready) => {
  await send('Page.navigate', { url });
  const deadline = Date.now() + 30000;
  let state = null;
  while (Date.now() < deadline) {
    await sleep(400);
    try {
      state = await evaluate(`(${PROBE})(${JSON.stringify({ selector })})`);
    } catch (error) {
      console.log(`  the measurement could not run: ${error.message}`);
      return null;
    }
    if (state && state.probeFailure) {
      console.log(`  the measurement reported: ${state.probeFailure.split('\n')[0]}`);
      return null;
    }
    if (state && ready(state)) return state;
  }
  return state;
};

for (const { w, h, label } of WIDTHS) {
  await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile: w < 768 });
  const state = await measure(`${site}/#/cities`, '#zones .zone-card', (one) => one.cards > 0 && one.count);
  if (!state || state.cards === 0) {
    console.log(`\n${label}\n  ${note(false, 'the Cities page never rendered its fleet')}`);
    continue;
  }
  if (!zoneId) {
    const first = await evaluate(`(document.querySelector('#zones .zone-card[data-zone]') || {}).getAttribute`
      + ` ? document.querySelector('#zones .zone-card[data-zone]').getAttribute('data-zone') : null`);
    zoneId = first || null;
    if (zoneId) console.log(`  measuring the city page at ${zoneId}, as the fleet itself names it`);
  }
  const padX = parseInt(state.padX, 10);
  const contentBox = state.innerWidth - 2 * padX;
  const expectedRows = state.innerWidth >= 1200 ? 1 : state.innerWidth >= 768 ? 2 : 4;
  console.log(`\n${label}  (content box ${contentBox}px, --pad-x ${padX}px)`);
  console.log(`  ${note(state.scrollWidth <= state.innerWidth + 1 && state.bodyScrollWidth <= state.innerWidth + 1,
    `no horizontal scroll: scrollWidth ${state.scrollWidth}, innerWidth ${state.innerWidth}`)}`);
  console.log(`  ${note(state.offenders.length === 0, `nothing overhangs the right edge (${state.offenders.length})`)}`);
  state.offenders.forEach((one) => console.log(`      ${one.tag}.${one.cls} ${one.left}..${one.right} "${one.text}"`));
  if (state.leftOvers.length) {
    console.log(`  note: ${state.leftOvers.length} box(es) overhang the left edge (nothing scrollable; M10 furniture)`);
    state.leftOvers.slice(0, 3).forEach((one) => console.log(`      ${one.tag}.${one.cls} ${one.left}..${one.right} "${one.text}"`));
  }
  console.log(`  ${note(state.clipped.length === 0, `nothing is clipped in the bar or a metric row (${state.clipped.length})`)}`);
  state.clipped.forEach((one) => console.log(`      ${one.cls}: scrollWidth ${one.scrollWidth} vs clientWidth ${one.clientWidth}`));
  const declaredRows = (String(state.toolbarAreas).match(/"[^"]+"|'[^']+'/g) || []).length;
  console.log(`  ${note(declaredRows === expectedRows,
    `the bar declares ${declaredRows} row(s), expected ${expectedRows}: ${state.toolbarAreas}`)}`);
  state.fields.forEach((one) => {
    if (!one.box) { failures.push(`${label}: ${one.name} field is missing`); return; }
    const inside = one.box.left >= padX - 1 && one.box.right <= state.innerWidth - padX + 1;
    console.log(`  ${note(inside, `${one.name} sits in the content box (${one.box.left}..${one.box.right})`)}`);
    console.log(`  ${note(one.box.height >= 44, `${one.name} is ${one.box.height}px tall (44px target)`)}`);
  });
  console.log(`  count line: "${state.count}"`);

  const phone = state.innerWidth <= 767;
  state.metrics.slice(0, 3).forEach((row) => {
    if (!row.chip || !row.value || !row.track || !row.name) {
      failures.push(`${label}: ${row.metric} is missing a cell`);
      return;
    }
    const chipIsLabel = ['REAL', 'MODEL', 'SIM', '?'].includes(row.chipText);
    const stacked = row.value.top >= row.name.bottom - 2;
    console.log(`  metric ${row.metric}: name ${row.name.left}..${row.name.right}, ` +
      `chip ${row.chip.left}..${row.chip.right} "${row.chipText}", ` +
      `value ${row.value.left}..${row.value.right} "${row.valueText}", bar ${row.track.left}..${row.track.right}`);
    console.log(`    ${note(row.chip.left >= row.name.right - 1, 'the chip is its own cell after the name')}   ` +
      `${note(chipIsLabel, `the chip reads ${row.chipText}`)}   ` +
      `${note(!/[0-9]/.test(row.valueText) || /[0-9]\s?\S/.test(row.valueText), 'the value keeps its unit')}`);
    console.log(`    ${note(phone ? stacked : !stacked,
      phone ? 'the value is on the second line' : 'name, chip and value share one line')}   columns: ${row.columns}`);
  });
}

for (const w of [320, 390, 768, 1280]) {
  await send('Emulation.setDeviceMetricsOverride', { width: w, height: 1200, deviceScaleFactor: 1, mobile: w < 768 });
  if (!zoneId) { console.log('no zone id was available for the city page'); break; }
  const state = await measure(`${site}/#/cities/${zoneId}`, '#city-detail, #view-city',
    (one) => one.tiles.length > 0 && one.tiles[0].value && one.tiles[0].value.width > 0);
  if (!state || !state.tiles.length || !state.tiles[0].value || state.tiles[0].value.width === 0) {
    console.log(`\ncity detail at ${w}px\n  ${note(false, 'no measurement tile had a measurable box')}`);
    continue;
  }
  const padX = parseInt(state.padX, 10);
  console.log(`\ncity detail at ${w}px  (content box ${state.innerWidth - 2 * padX}px, --pad-x ${padX}px)`);
  console.log(`  ${note(state.scrollWidth <= state.innerWidth + 1, `no horizontal scroll: scrollWidth ${state.scrollWidth}`)}`);
  console.log(`  ${note(state.offenders.length === 0, `nothing overhangs the right edge (${state.offenders.length})`)}`);
  state.offenders.forEach((one) => console.log(`      ${one.tag}.${one.cls} ${one.left}..${one.right} "${one.text}"`));
  const phone = state.innerWidth <= 767;
  state.tiles.forEach((row) => {
    if (!row.head || !row.value) return;
    const chipIsLabel = ['REAL', 'MODEL', 'SIM', '?'].includes(row.chipText);
    const stacked = row.value.top >= row.head.bottom - 2;
    const unitAttached = !/[0-9]/.test(row.valueText) || /[0-9]\s?\S/.test(row.valueText);
    console.log(`  tile: head ${row.head.left}..${row.head.right} (${row.head.height}px), ` +
      `chip ${row.chip ? `${row.chip.left}..${row.chip.right} "${row.chipText}"` : 'none'}, ` +
      `value ${row.value.left}..${row.value.right} "${row.valueText}"`);
    const rightEdge = state.innerWidth - padX + 1;
    console.log(`    ${note(!row.clipped, 'the tile head does not clip its label')}   ` +
      `${note(row.chip ? chipIsLabel : true, `the chip reads ${row.chipText}`)}   ` +
      `${note(unitAttached, 'the value keeps its unit')}   ` +
      // the tile's internal arrangement is its own (M10); what it must not do is clip or overflow
      `${note(row.value.right <= rightEdge && (!row.chip || row.chip.right <= rightEdge),
        `the chip and value stay inside the card (${row.value.right} <= ${rightEdge})`)}`);
  });
}


const ROUTE_PROBE = `((args) => {
  try {
    const visible = (node) => !!node && !node.hasAttribute('hidden') && getComputedStyle(node).display !== 'none';
    const box = (node) => (node ? node.getBoundingClientRect() : null);
    const padX = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--pad-x')) || 0;
    const state = {
      hash: location.hash,
      innerWidth: window.innerWidth,
      scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
      padX,
      views: [...document.querySelectorAll('.view')].map((view) => ({ id: view.id, visible: visible(view) })),
      viewVisible: visible(document.querySelector(args.view)),
      needs: args.needs.map((sel) => {
        const node = document.querySelector(sel);
        const rect = box(node);
        return { sel, present: !!node, visible: visible(node), width: rect ? Math.round(rect.width) : 0 };
      }),
      styles: args.styles.map((sel) => {
        const node = document.querySelector(sel);
        if (!node) return { sel, present: false };
        const style = getComputedStyle(node);
        return { sel, present: true, display: style.display, gap: style.gap,
                 columns: style.gridTemplateColumns, radius: style.borderRadius,
                 border: style.borderLeftColor, colour: style.color, background: style.backgroundColor };
      }),
      sheetRules: [...document.styleSheets].reduce((sum, sheet) => {
        try { return sum + sheet.cssRules.length; } catch (error) { return sum; }
      }, 0),
      navLinks: [...document.querySelectorAll('.nav-list a')].map((link) => ({
        text: link.textContent.trim(), href: link.getAttribute('href'), current: link.getAttribute('aria-current') || '',
      })),
      offenders: [],
    };
    const clippedByAncestor = (node) => {
      for (let parent = node.parentElement; parent; parent = parent.parentElement) {
        const style = getComputedStyle(parent);
        if (['hidden', 'clip', 'auto', 'scroll'].includes(style.overflowX)) return true;
      }
      return false;
    };
    [...document.querySelectorAll('body *')].forEach((node) => {
      if (node.closest('.sr-only') || node.getClientRects().length === 0) return;
      const rect = node.getBoundingClientRect();
      if (rect.width === 0) return;
      // a full-bleed element ends exactly at the viewport edge; that is not an overhang
      if (rect.width >= window.innerWidth - 1) return;
      if (rect.right > window.innerWidth + 1 && !clippedByAncestor(node)) {
        state.offenders.push({ tag: node.tagName.toLowerCase(),
          cls: String(node.className || '').slice(0, 34), right: Math.round(rect.right) });
      }
    });
    state.offenders = state.offenders.slice(0, 4);
    return state;
  } catch (error) { return { probeFailure: String((error && error.message) || error) }; }
})`;

const ROUTES = [
  { hash: '#/cities', label: 'Cities', view: '#view-cities',
    needs: ['#cities-toolbar', '.cities-search', '.filter-chip'], styles: ['.cities-toolbar', '.city-cards'] },
  { hash: '#/', label: 'Overview', view: '#view-overview',
    needs: ['.hero', '.stat-row'], styles: ['.hero', '.stat-row'] },
  { hash: '#/alerts', label: 'Alerts', view: '#view-alerts',
    needs: ['.alert-section', '.alert-list'], styles: ['.alert-section', '.alert-list'] },
  { hash: '#/data', label: 'Data', view: '#view-data',
    needs: ['.legend-grid', '.legend-card'], styles: ['.legend-grid', '.legend-card'] },
  { hash: '#/about', label: 'About', view: '#view-about',
    needs: ['.about-grid', '.about-card'], styles: ['.about-grid', '.about-card'] },
];

const shipped = ['index.html', 'styles.css', 'lib.js', 'app.js'];
const localHash = (name) => crypto.createHash('sha256')
  .update(fs.readFileSync(path.join('web', name))).digest('hex').slice(0, 32);

console.log('\ndeployed files, hashed by the browser against web/');
for (const name of shipped) {
  const response = await fetch(`${site}/${name}`);
  const fetched = await response.text();
  const digest = crypto.createHash('sha256').update(Buffer.from(fetched, 'utf8')).digest('hex').slice(0, 32);
  const local = localHash(name);
  console.log(`  ${note(digest === local, `${name}: deployed ${digest} ${digest === local ? '==' : '!='} local ${local}`)}`);
}

const routeWidths = [1280, 390];
for (const w of routeWidths) {
  await send('Emulation.setDeviceMetricsOverride', { width: w, height: 1200, deviceScaleFactor: 1, mobile: w < 767 });
  console.log(`\nroutes at ${w}px`);
  const routes = ROUTES.slice();
  if (zoneId) routes.splice(1, 0, { hash: `#/cities/${zoneId}`, label: 'City detail', view: '#view-city',
    needs: ['.city-detail-slot', '.tile-grid'], styles: ['.city-detail', '.tile-grid'] });
  for (const route of routes) {
    await send('Page.navigate', { url: `${site}/${route.hash}` });
    let state = null;
    const deadline = Date.now() + 25000;
    while (Date.now() < deadline) {
      await sleep(350);
      try { state = await evaluate(`${ROUTE_PROBE}(${JSON.stringify({ view: route.view, needs: route.needs, styles: route.styles })})`); }
      catch (error) { state = null; break; }
      if (state && state.viewVisible) break;
    }
    if (!state || !state.viewVisible) {
      console.log(`  ${note(false, `${route.label}: the view never became visible (${route.hash})`)}`);
      continue;
    }
    const shown = state.views.filter((one) => one.visible).map((one) => one.id);
    const scrollOk = state.scrollWidth <= state.innerWidth + 1;
    const needs = state.needs.filter((one) => !one.present || one.width === 0);
    const missingStyles = state.styles.filter((one) => !one.present);
    console.log(`  ${route.label.padEnd(11)} ${note(shown.length === 1, `one view visible (${shown.join(', ') || 'none'})`)}   ` +
      `${note(scrollOk, scrollWidth_text(state))}   ` +
      `${note(state.offenders.length === 0, `nothing overhangs the right edge (${state.offenders.length})`)}   ` +
      `${note(needs.length === 0, `content is present and sized: ${route.needs.join(', ')}`)}   ` +
      `${note(missingStyles.length === 0, `the elements this route styles exist: ${route.styles.join(', ')}`)}   ` +
      `${note(state.sheetRules > 700, `${state.sheetRules} stylesheet rules loaded`)}`);
    state.offenders.forEach((one) => console.log(`      ${one.tag}.${one.cls} right ${one.right}`));
    needs.forEach((one) => console.log(`      missing or zero-width: ${one.sel}`));
    missingStyles.forEach((one) => console.log(`      not on this route: ${one.sel}`));
    if (route.hash === '#/cities') {
      const navOk = state.navLinks.length >= 4;
      console.log(`      ${note(navOk, `the nav offers ${state.navLinks.length} route(s): ${state.navLinks.map((one) => one.text).join(', ')}`)}`);
    }
    if (route.hash === '#/') {
      const merges = await evaluate(`(() => {
        const read = (sel, prop) => { const node = document.querySelector(sel);
          return node ? getComputedStyle(node)[prop] : null; };
        return {
          summaryGap: read('.health-summary', 'gap'),
          figuresGap: read('.city-figures', 'gap'),
          statColumns: read('.stat-row', 'gridTemplateColumns'),
          bandName: read('.band-tile.is-zero .band-name', 'color'),
          bandCount: read('.band-tile.is-zero .band-count', 'color'),
        };
      })()`);
      if (merges.statColumns) {
        const tracks = merges.statColumns.split(' ').length;
        // the merge lives in the phone tier; above it the top-level .stat-row rule is the one that applies
        if (w <= 767) {
          console.log(`      ${note(tracks === 1, `.stat-row (merged with .city-cards) has ${tracks} column(s) at ${w}px: ${merges.statColumns}`)}`);
        } else {
          console.log(`      note: above the phone tier .stat-row lays out ${tracks} track(s) as its own rule says: ${merges.statColumns}`);
        }
      }
      if (merges.bandName && merges.bandCount) {
        console.log(`      ${note(merges.bandName === merges.bandCount,
          `the merged zero-count band rule reaches both cells: name ${merges.bandName}, count ${merges.bandCount}`)}`);
      } else {
        console.log('      note: no zero-count band tile in this reading, so that merge has no element to check');
      }
      if (merges.summaryGap && merges.figuresGap) {
        if (w <= 767) {
          console.log(`      ${note(merges.summaryGap === '6px',
            `the phone-tier merge applies to .health-summary: gap ${merges.summaryGap} (from .health-summary, .city-figures)`)}`);
          console.log(`      note: .city-figures computes gap ${merges.figuresGap}: a later rule in the same tier sets 8px, ` +
            'before and after the merge');
        } else {
          console.log(`      note: above the phone tier .health-summary is ${merges.summaryGap} and .city-figures ${merges.figuresGap}, ` +
            'as the top-level rules say');
        }
      }
    }
    if (route.hash.startsWith('#/cities/')) {
      const merges = await evaluate(`(() => {
        const read = (sel, prop) => { const node = document.querySelector(sel);
          return node ? getComputedStyle(node)[prop] : null; };
        const slot = document.querySelector('.city-detail-slot'), detail = document.querySelector('.city-detail');
        return {
          slotDisplay: slot ? getComputedStyle(slot).display : null,
          detailDisplay: detail ? getComputedStyle(detail).display : null,
          detailGap: detail ? getComputedStyle(detail).gap : null,
          criticalIcon: read('.metric-tile[data-state="critical"] .metric-icon', 'color'),
          criticalValue: read('.metric-tile[data-state="critical"] .tile-value', 'color'),
          states: [...document.querySelectorAll('.metric-tile[data-state]')].map((one) => one.getAttribute('data-state')),
        };
      })()`);
      if (merges.detailDisplay && merges.slotDisplay) {
        console.log(`      ${note(merges.detailDisplay === merges.slotDisplay && merges.detailDisplay === 'grid',
          `the merged .city-detail-slot / .city-detail rule applies to both: display ${merges.slotDisplay} and ${merges.detailDisplay}, gap ${merges.detailGap}`)}`);
      }
      if (merges.criticalIcon && merges.criticalValue) {
        console.log(`      ${note(merges.criticalIcon === merges.criticalValue,
          `the merged critical-tile rule reaches both cells: ${merges.criticalIcon}`)}`);
      } else {
        console.log(`      note: no critical metric tile in this reading (states: ${merges.states.join(', ') || 'none'}), ` +
          'so that merge has no element to check');
      }
    }
  }
}

// one real navigation, clicked rather than typed
await send('Page.navigate', { url: `${site}/#/cities` });
await sleep(2500);
const clicked = await evaluate(`(() => {
  const link = [...document.querySelectorAll('.nav-list a')].find((one) => (one.getAttribute('href') || '').includes('/alerts'));
  if (!link) return { found: false };
  link.click();
  return { found: true, href: link.getAttribute('href') };
})()`);
await sleep(2500);
const afterClick = await evaluate(`({
  hash: location.hash,
  alertsVisible: (() => { const view = document.querySelector('#view-alerts');
    return !!view && !view.hasAttribute('hidden') && getComputedStyle(view).display !== 'none'; })(),
  errors: window.__probeErrors ? window.__probeErrors.length : 0,
})`);
console.log(`\nnavigation by click\n  ${note(clicked.found && afterClick.alertsVisible && afterClick.hash.includes('alerts'),
  `clicking the Alerts nav link reached ${afterClick.hash} and the view is visible`)}`);

await send('Page.navigate', { url: 'about:blank' });
console.log(`\nscript errors during the whole run: ${scriptErrors.length}`);
scriptErrors.slice(0, 6).forEach((one) => console.log(`  ${one.slice(0, 150)}`));
console.log(`requests that 404'd: ${resourceErrors.length} (not script errors: the page reports a missing route itself)`);
resourceErrors.slice(0, 3).forEach((one) => console.log(`  ${one.slice(0, 130)}`));
if (scriptErrors.length) failures.push(`${scriptErrors.length} script error(s)`);

console.log(`\n${failures.length === 0 ? 'browser probe: everything checked' : `browser probe: ${failures.length} failure(s)`}`);
failures.slice(0, 10).forEach((one) => console.log(`  FAILED ${one}`));
socket.close();
process.exit(failures.length ? 1 : 0);
