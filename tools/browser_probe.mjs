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
    const offenders = [];
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
      if ((box.right > window.innerWidth + 1 || box.left < -1) && offenders.length < 5) {
        offenders.push({ tag: node.tagName.toLowerCase(), cls: cls.slice(0, 44),
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
      metrics, offenders, clipped,
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
const note = (ok, text) => {
  if (!ok) failures.push(text);
  return `${ok ? 'ok  ' : 'FAIL'} ${text}`;
};
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
  const padX = parseInt(state.padX, 10);
  const contentBox = state.innerWidth - 2 * padX;
  const expectedRows = state.innerWidth >= 1200 ? 1 : state.innerWidth >= 768 ? 2 : 4;
  console.log(`\n${label}  (content box ${contentBox}px, --pad-x ${padX}px)`);
  console.log(`  ${note(state.scrollWidth <= state.innerWidth + 1 && state.bodyScrollWidth <= state.innerWidth + 1,
    `no horizontal scroll: scrollWidth ${state.scrollWidth}, innerWidth ${state.innerWidth}`)}`);
  console.log(`  ${note(state.offenders.length === 0, `nothing leaves the viewport (${state.offenders.length} offender(s))`)}`);
  state.offenders.forEach((one) => console.log(`      ${one.tag}.${one.cls} ${one.left}..${one.right} "${one.text}"`));
  console.log(`  ${note(state.clipped.length === 0, `nothing is clipped in the bar or a metric row (${state.clipped.length})`)}`);
  state.clipped.forEach((one) => console.log(`      ${one.cls}: scrollWidth ${one.scrollWidth} vs clientWidth ${one.clientWidth}`));
  const declaredRows = (String(state.toolbarAreas).match(/"[^"]+"|'[^']+'/g) || []).length;
  console.log(`  ${note(declaredRows === expectedRows,
    `the bar declares ${declaredRows} row(s), expected ${expectedRows}: ${state.toolbarAreas}`)}`);
  console.log(`  ${note(state.rows.length <= expectedRows,
    `the fields physically share ${state.rows.length} line(s), at most ${expectedRows}: ${state.rows.map((r) => r.join('+')).join(' | ')}`)}`);
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
  const state = await measure(`${site}/#/cities/bengaluru`, '#view-city, #zones .zone-card',
    (one) => one.metrics.length > 0 && one.metrics[0].name && one.metrics[0].name.width > 0);
  if (!state || !state.metrics.length || !state.metrics[0].name || state.metrics[0].name.width === 0) {
    console.log(`\ncity detail at ${w}px\n  ${note(false, 'no metric row had a measurable box')}`);
    continue;
  }
  const padX = parseInt(state.padX, 10);
  console.log(`\ncity detail at ${w}px  (content box ${state.innerWidth - 2 * padX}px, --pad-x ${padX}px)`);
  console.log(`  ${note(state.scrollWidth <= state.innerWidth + 1, `no horizontal scroll: scrollWidth ${state.scrollWidth}`)}`);
  console.log(`  ${note(state.offenders.length === 0, `nothing leaves the viewport (${state.offenders.length} offender(s))`)}`);
  state.offenders.forEach((one) => console.log(`      ${one.tag}.${one.cls} ${one.left}..${one.right} "${one.text}"`));
  const phone = state.innerWidth <= 767;
  state.metrics.forEach((row) => {
    if (!row.chip || !row.value || !row.name) return;
    const chipIsLabel = ['REAL', 'MODEL', 'SIM', '?'].includes(row.chipText);
    const stacked = row.value.top >= row.name.bottom - 2;
    console.log(`  metric ${row.metric}: name ${row.name.left}..${row.name.right}, ` +
      `chip ${row.chip.left}..${row.chip.right} "${row.chipText}", value ${row.value.left}..${row.value.right} "${row.valueText}"`);
    console.log(`    ${note(row.name.right <= row.chip.left + 1, 'the name does not run under the chip')}   ` +
      `${note(chipIsLabel, `the chip reads ${row.chipText}`)}   ` +
      `${note(phone ? stacked : !stacked, phone ? 'the value is on the second line' : 'name, chip and value share one line')}`);
  });
}

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
