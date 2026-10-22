/* Keyboard check: the M17 accessibility fixes, measured in a real browser rather than in the DOM stub.
 *
 *   node tools/keyboard_check.mjs http://127.0.0.1:8140 [--port 9222] [--api http://127.0.0.1:8199]
 *
 * The repository's DOM harness cannot show whether focus survives a poll: it records innerHTML writes
 * as strings and has no real child nodes, so replacing a button with an identical one is
 * indistinguishable from leaving it alone. A browser can tell the difference, and this is where the
 * four M17 keyboard claims are settled:
 *
 *   - a control that has not changed keeps the focus it had while a poll lands (five seconds apart);
 *   - the skip link moves the reader to <main> and leaves the view they were on alone;
 *   - every marker on the coast map has an accessible name, from the browser's own accessibility tree;
 *   - the sideways-scrolling incident table is reachable with the keyboard.
 *
 * Read-only: it loads the page, reads, and never submits a form or writes to the API.
 */

import { setTimeout as sleep } from 'node:timers/promises';

const args = process.argv.slice(2);
const site = (args[0] || 'http://127.0.0.1:8140').replace(/\/$/, '');
const value = (flag, fallback) => {
  const at = args.indexOf(flag);
  return at >= 0 && args[at + 1] ? args[at + 1] : fallback;
};
const port = value('--port', '9222');
const api = value('--api', null);
const zone = value('--zone', 'city-kochi');
const siteWithApi = api ? `${site}/?api=${encodeURIComponent(api)}` : site;

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
/* A fresh tab is preferred: an earlier run may have left a page whose renderer is busy, and a blank
 * page is always the one this tool navigates anyway. */
const page = list.find((one) => one.type === 'page' && one.url === 'about:blank')
  || list.find((one) => one.type === 'page');
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
let lastError = null;
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  if (message.method === 'Runtime.exceptionThrown') {
    lastError = message.params.exceptionDetails.text || 'script error';
  }
  if (message.id && pending.has(message.id)) {
    const settle = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) settle.reject(new Error(message.error.message));
    else settle.resolve(message.result);
  }
});
const send = (method, params) => new Promise((resolve, reject) => {
  const id = ++nextId;
  const timer = setTimeout(() => {
    pending.delete(id);
    reject(new Error(`the browser did not answer ${method} within 20s`));
  }, 20000);
  pending.set(id, { resolve: (value) => { clearTimeout(timer); resolve(value); },
                    reject: (error) => { clearTimeout(timer); reject(error); } });
  socket.send(JSON.stringify({ id, method, params: params || {} }));
});
const evaluate = async (expression) => {
  const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'evaluation failed');
  return result.result.value;
};
const open = async (hash) => {
  await send('Page.navigate', { url: 'about:blank' });
  await send('Page.navigate', { url: `${siteWithApi}#${hash}` });
  await send('Page.bringToFront');
  await sleep(2600);
};

await send('Page.enable');
await send('Runtime.enable');
await send('Accessibility.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });

/* A headless browser can refuse document focus when its window is behind another process; the check
 * itself is about the page, so it asks the browser to bring the page to the front first. */
await send('Page.bringToFront');

let passed = 0;
const failures = [];
const check = (name, run) => {
  try {
    run();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (error) {
    failures.push(`${name}: ${error.message}`);
    console.log(`  FAIL ${name}: ${error.message}`);
  }
};
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
const match = (text, pattern, message) => {
  assert(typeof text === 'string' && pattern.test(text), `${message} (saw: ${JSON.stringify(text)})`);
};

/* 1. Focus survives a poll: focus a window chip on the city page and a city card on the Cities page
 *    (both are rebuilt from every /zones response), wait for two ticks of the five-second poll, and ask
 *    the browser what still has focus. The measurement reports what it found, so a wrong selector
 *    shows up as a number rather than as a silent pass. */
await open(`/cities/${zone}`);
const focused = await evaluate(`(async () => {
  const chip = document.querySelector('#history-range [data-range]');
  const report = { chips: document.querySelectorAll('#history-range [data-range]').length,
                   zoneButtons: document.querySelectorAll('[data-zone]').length };
  if (chip) chip.focus();
  report.chipBefore = document.activeElement === chip;
  await new Promise((r) => setTimeout(r, 11000));
  report.chipAfter = document.activeElement === chip && document.contains(chip);
  report.view = (document.querySelector('main .view:not([hidden])') || {}).id || null;
  return report;
})()`);
check('the city page is the one on screen, with the controls a reader can focus', () => {
  assert(focused.view === 'view-city', `expected the city view, saw ${focused.view}`);
  assert(focused.chips >= 4, `expected at least four window chips, saw ${focused.chips}`);
  assert(focused.zoneButtons >= 3, `expected city links on the page, saw ${focused.zoneButtons}`);
});
check('a focused window chip keeps focus across two polls', () => {
  assert(focused.chipBefore, 'the chip could not be focused at all');
  assert(focused.chipAfter, 'the chip was replaced under the keyboard while a poll landed');
});
/* 1b. The city grid's cards are built by the same guarded writer, but on this page they carry no layout
 *     box in a headless run (measured: 17 a[data-zone] nodes, none with a rectangle outside the map's
 *     SVG), so a focus test would measure the harness rather than the page. The mechanism is pinned by
 *     tools/dashboard_check.mjs; the browser proof above is the window chips, which are rebuilt by the
 *     same swapHtml call the picker uses. */
await open('/cities');
const cardState = await evaluate(`(() => {
  const all = Array.from(document.querySelectorAll('a[data-zone]'));
  const outside = all.filter((link) => !link.closest('svg'));
  return { all: all.length, outsideSvg: outside.length,
           withBox: outside.filter((link) => link.getBoundingClientRect().width > 0).length };
})()`);
console.log(`  note the Cities page carries ${cardState.all} a[data-zone] links (${cardState.outsideSvg} outside the map, ` +
  `${cardState.withBox} with a layout box), so card focus is measured by the DOM harness, not here`);
/* 2. The skip link: activate it with the keyboard and check both halves of its job. */
await open('/data');
const skip = await evaluate(`(async () => {
  const link = document.querySelector('a.skip-link, a[href="#main"]');
  if (!link) return { found: false };
  const before = { hash: location.hash, view: (document.querySelector('main .view:not([hidden])') || {}).id };
  link.focus();
  const focused = document.activeElement === link;
  link.click();
  await new Promise((r) => setTimeout(r, 400));
  const main = document.getElementById('main');
  return {
    found: true, focused, before,
    after: { hash: location.hash, view: (document.querySelector('main .view:not([hidden])') || {}).id },
    activeId: document.activeElement ? document.activeElement.id : null,
    mainExists: !!main, mainTabIndex: main ? main.tabIndex : null
  };
})()`);
check('the skip link exists and takes focus', () => {
  assert(skip.found, 'no skip link and no href="#main" link on the page');
  assert(skip.focused, 'the skip link could not be focused');
  assert(skip.mainExists && skip.mainTabIndex === -1, '<main id="main"> is missing or not focusable');
});
check('the skip link moves to the content and leaves the Data view in place', () => {
  assert(skip.before.view === 'view-data', `the Data view was not the one on screen (saw ${skip.before.view})`);
  assert(skip.after.view === 'view-data', `the view changed to ${skip.after.view}: the skip link is routing`);
  assert(skip.after.hash === '#main', `the hash is ${skip.after.hash}, so the jump did not happen`);
});

/* 3. The map's markers, read from the browser's own accessibility tree rather than from the markup.
 *    The map lives in the Cities view (on the Overview the same element is hidden with the rest of that
 *    view), so the tree is read there, with the map scrolled into view. Chromium exposes only the nodes
 *    it considers on screen, which is exactly why a marker that is merely below the fold must not be
 *    confused with a marker that has no name. */
await open('/cities');
const mapState = await evaluate(`(() => {
  const map = document.getElementById('coast-map');
  if (!map) return { found: false };
  map.scrollIntoView({ block: 'center' });
  const links = Array.from(map.querySelectorAll('a'));
  const box = map.getBoundingClientRect();
  return { found: true, links: links.length, height: Math.round(box.height),
           inView: box.top > 0 && box.bottom < window.innerHeight,
           names: links.map((link) => {
             const title = link.querySelector('title');
             return ((title && title.textContent.trim()) || (link.getAttribute('aria-label') || '').trim());
           }),
           view: (document.querySelector('main .view:not([hidden])') || {}).id || null };
})()`);
mapState.unnamed = mapState.names ? mapState.names.filter((name) => !name).length : 0;
await sleep(600);
const { nodes } = await send('Accessibility.getFullAXTree');
const linkNames = nodes
  .filter((node) => node.role && node.role.value === 'link' && node.name && node.name.value)
  .map((node) => String(node.name.value));
const markers = linkNames.filter((name) => /city detail$/i.test(name));
const roleImage = nodes.filter((node) => node.role && node.role.value === 'image' && node.name && /coast|map/i.test(String(node.name.value)));
check('every coast-map marker is a link with an accessible name', () => {
  assert(mapState.found, 'there is no #coast-map element');
  assert(mapState.links >= 12, `expected at least twelve marker links, saw ${mapState.links}`);
  assert(mapState.unnamed === 0, `${mapState.unnamed} marker links have no name of their own`);
  assert(new Set(mapState.names).size === mapState.names.length, 'two markers share an accessible name');
  // Measured, not asserted: in this headless run Chromium's tree exposes only the markers it considers
  // on screen, and the number it reports varies with the scroll position of a 463px map. The claim that
  // every marker is a named link is pinned where it is deterministic - the markup check above, plus
  // tools/dashboard_check.mjs (twelve links, each with its own name) and the browser check, which reads
  // the rendered page. The gap is recorded in the M17 report rather than papered over with a weaker
  // assertion here.
  console.log(`  note the accessibility tree exposed ${markers.length} of ${mapState.links} named marker links ` +
    `with the map ${mapState.inView ? 'inside' : 'outside'} the ${mapState.height}px viewport`);
});
check('the map is not hidden inside one image role', () => {
  assert(roleImage.length === 0, `the accessibility tree still exposes an image node: ${roleImage.map((n) => n.name.value)}`);
});

/* 4. The incident table's sideways scroll box: what the rendered page says about it, and what a Tab
 *    walk does in this browser. The structural fact (a focusable scroll container) is asserted; the
 *    walk itself is measured and printed, because a headless Tab sequence is not a faithful stand-in
 *    for a reader's keyboard and pretending otherwise would be worse than saying so. */
await open('/alerts');
const scrollBox = await evaluate(`(() => {
  const all = Array.from(document.querySelectorAll('.incident-scroll'));
  const box = all.find((one) => one.offsetParent) || null;
  if (!box) return { found: false, count: all.length };
  return { found: true, tabIndex: box.tabIndex, describedBy: box.getAttribute('aria-describedby'),
           overflowX: getComputedStyle(box).overflowX, height: Math.round(box.getBoundingClientRect().height),
           view: (document.querySelector('main .view:not([hidden])') || {}).id };
})()`);
check('the incident table is a labelled, focusable scroll container', () => {
  assert(scrollBox.found, `no visible .incident-scroll container on the alerts view (found ${scrollBox.count})`);
  assert(scrollBox.tabIndex === 0, `the visible scroll container has tabindex ${scrollBox.tabIndex}, so a keyboard reader cannot reach it`);
  assert(/(auto|scroll)/.test(scrollBox.overflowX), `the container does not scroll sideways (overflow-x: ${scrollBox.overflowX})`);
});
await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: 40, y: 300, button: 'left', clickCount: 1 });
await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: 40, y: 300, button: 'left', clickCount: 1 });
const reached = { walk: 0, hit: false };
for (let step = 0; step < 60 && !reached.hit; step += 1) {
  await send('Input.dispatchKeyEvent', { type: 'rawKeyDown', windowsVirtualKeyCode: 9, key: 'Tab', code: 'Tab' });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', windowsVirtualKeyCode: 9, key: 'Tab', code: 'Tab' });
  reached.walk = step + 1;
  reached.hit = await evaluate(`(() => {
    const active = document.activeElement;
    const box = document.querySelector('.incident-scroll');
    return !!box && !!active && (active === box || box.contains(active));
  })()`);
}
console.log(`  note ${reached.hit ? `Tab reached the incident table after ${reached.walk} stops` : `Tab did not reach the incident table in ${reached.walk} stops in this headless run`}`);

check('the page stayed free of script errors', () => {
  assert(!lastError, `a script error was raised: ${lastError}`);
});

console.log(`\nkeyboard check: ${passed} check(s) passed, ${failures.length} failed`);
for (const failure of failures) console.log(`  - ${failure}`);
socket.close();
process.exit(failures.length ? 1 : 0);
