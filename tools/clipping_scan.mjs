/* Clipping scan: every visible element on every route, at every width, against a real browser.
 *
 * The dashboard probe checks that nothing overhangs the right edge of the page, and it deliberately
 * ignores a box an ancestor clips, because such a box cannot scroll the page. That is the right rule for
 * page overflow and the wrong rule for content: three of the four defects this scan found in M14 were
 * content hidden by an ancestor - the sparkline caption cut off by the city card at every width, the
 * metric row wider than its column, a 46px ranking track for a 49px word, and the CRITICAL pill squeezed
 * into a 92px track. None of them moved a pixel of page scroll, so nothing else noticed them.
 *
 * So this asks the other question: does an element hold more content than its own box? It reports what
 * it finds, names the element, and fails (exit 1) unless the finding is on the intentional list below.
 *
 *   node tools/clipping_scan.mjs http://127.0.0.1:8140
 *   node tools/clipping_scan.mjs https://floodpulse-dashboard-725934295966.s3.eu-north-1.amazonaws.com
 *
 * Optional: --widths 320,390,768,1280  --routes /,/cities  --port 9222  --verbose
 */

import { setTimeout as sleep } from 'node:timers/promises';

const args = process.argv.slice(2);
const site = (args[0] || 'http://127.0.0.1:8140').replace(/\/$/, '');
const value = (flag, fallback) => {
  const at = args.indexOf(flag);
  return at >= 0 && args[at + 1] ? args[at + 1] : fallback;
};
const widths = value('--widths', '320,390,768,1280').split(',').map(Number);
const routes = value('--routes', '/,/cities,/alerts,/data,/about').split(',');
const port = value('--port', '9222');
const verbose = args.includes('--verbose');
const zoneId = value('--zone', 'city-kochi');

/* Text that is meant to be cut short with an ellipsis, and boxes that scroll on purpose, are not defects:
 * the element says so itself in its own computed style. */
const INTENTIONAL = `(node) => {
  const style = getComputedStyle(node);
  if (style.textOverflow === 'ellipsis') return true;
  if (/(auto|scroll)/.test(style.overflowX)) return true;
  for (let parent = node.parentElement; parent; parent = parent.parentElement) {
    const up = getComputedStyle(parent);
    if (up.textOverflow === 'ellipsis' || /(auto|scroll)/.test(up.overflowX)) return true;
  }
  return false;
}`;

const ACCOUNT = `(() => {
  const intentional = ${INTENTIONAL};
  const found = [];
  for (const node of document.querySelectorAll('body *')) {
    const box = node.getBoundingClientRect();
    if (box.width <= 1 || box.height <= 1) continue;
    if (getComputedStyle(node).visibility === 'hidden') continue;
    if (node.classList.contains('sr-only') || node.closest('.sr-only')) continue;
    if (intentional(node)) continue;
    const overX = node.scrollWidth - node.clientWidth;
    const overY = node.scrollHeight - node.clientHeight;
    if (overX > 1 || overY > 1) {
      found.push({
        tag: node.tagName.toLowerCase(),
        cls: String(node.className).slice(0, 46),
        id: node.id,
        over: 'x:' + overX + ' y:' + overY,
        box: Math.round(box.width) + 'x' + Math.round(box.height),
        text: (node.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 40),
        clippedByAncestor: (() => {
          // A box 2px taller than its own line-height is line-box rounding: the glyphs are smaller than
          // the box they are drawn in. Only a glyph that leaves the box that clips it is a defect, so the
          // glyph rectangle is what gets measured here.
          let clipper = null;
          for (let parent = node.parentElement; parent; parent = parent.parentElement) {
            const style = getComputedStyle(parent);
            if (/hidden|clip/.test(style.overflowX)) { clipper = parent; break; }
          }
          if (!clipper) return false;
          const range = document.createRange();
          range.selectNodeContents(node);
          const glyphs = range.getBoundingClientRect();
          const edge = clipper.getBoundingClientRect();
          return glyphs.bottom > edge.bottom + 0.5 || glyphs.top < edge.top - 0.5 ||
                 glyphs.right > edge.right + 0.5 || glyphs.left < edge.left - 0.5;
        })()
      });
    }
  }
  return { found: found, height: document.documentElement.scrollHeight };
})()`;

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const page = list.find((one) => one.type === 'page');
if (!page) {
  console.error('no debuggable page on port ' + port);
  process.exit(2);
}
const socket = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true });
  socket.addEventListener('error', reject, { once: true });
});
let nextId = 0;
const pending = new Map();
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const settle = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) settle.reject(new Error(message.error.message));
    else settle.resolve(message.result);
  }
});
const send = (method, params) => new Promise((resolve, reject) => {
  const id = ++nextId;
  pending.set(id, { resolve, reject });
  socket.send(JSON.stringify({ id, method, params: params || {} }));
});
const evaluate = async (expression) => {
  const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'evaluation failed');
  return result.result.value;
};

await send('Page.enable');
await send('Runtime.enable');
console.log(`clipping scan: ${routes.length} routes x ${widths.length} widths against ${site}`);

const findings = [];
for (const width of widths) {
  await send('Emulation.setDeviceMetricsOverride', { width, height: 1200, deviceScaleFactor: 1, mobile: width < 768 });
  for (const route of routes) {
    const hash = route === '/city' ? `/cities/${zoneId}` : route;
    await send('Page.navigate', { url: 'about:blank' });
    await send('Page.navigate', { url: `${site}/#${hash}` });
    await sleep(2600);
    const report = await evaluate(ACCOUNT);
    for (const row of report.found) {
      findings.push({ width, route: hash, ...row });
    }
    if (verbose || report.found.length) {
      console.log(`  ${route.padEnd(16)} ${String(width).padStart(4)}px  page ${report.height}px  ` +
        `${report.found.length ? report.found.length + ' finding(s)' : 'nothing holds more than its box'}`);
    }
  }
}

const hidden = findings.filter((row) => row.clippedByAncestor);
console.log(`\n${findings.length} element(s) hold more content than their box ` +
  `(${hidden.length} of them hidden by an ancestor that clips)`);
for (const row of findings.slice(0, 24)) {
  console.log(`   ${String(row.width).padStart(4)}px ${row.route.padEnd(16)} ${row.tag}.${row.cls}` +
    `${row.id ? '#' + row.id : ''}  overflows ${row.over}  box ${row.box}` +
    `${row.clippedByAncestor ? '  HIDDEN BY ANCESTOR' : ''}  "${row.text}"`);
}
if (findings.length > 24) console.log(`   ... and ${findings.length - 24} more`);
console.log(findings.length
  ? '\nsome of this is expected (measured, non-clipping line-box rounding); anything hidden by an ancestor is not'
  : '\npass: every visible element fits its own box on every route and width measured');
socket.close();
process.exit(hidden.length ? 1 : 0);
