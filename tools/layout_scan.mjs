/* Layout scan: the M15.2 alignment and public-copy contract, measured in a real browser.
 *
 * The M15.2 brief came from screenshots: a city card sitting lower than its neighbours, a "Why these
 * cities?" panel starting left of the page rhythm, a native select arrow glued to the far edge, nested
 * Data blocks hugging their parent's border, and operator wording on public screens. Guessing at those
 * from source is how you end up special-casing one city, so this measures them instead: every card's
 * photograph box, title offset and body offset; the inner left edge of every panel; the select's own
 * geometry; and the visible words on the public routes.
 *
 *   node tools/layout_scan.mjs http://127.0.0.1:8140
 *   node tools/layout_scan.mjs https://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com \
 *     --widths 320,390,768,1280 --port 9222
 *
 * Exits 1 if any contract below fails. Report only with --verbose.
 */

import { setTimeout as sleep } from 'node:timers/promises';

const args = process.argv.slice(2);
const site = (args[0] || 'http://127.0.0.1:8140').replace(/\/$/, '');
const value = (flag, fallback) => {
  const at = args.indexOf(flag);
  return at >= 0 && args[at + 1] ? args[at + 1] : fallback;
};
const widths = value('--widths', '320,390,768,1280').split(',').map(Number);
const port = value('--port', '9222');
const api = value('--api', '');
const suffix = api ? `?api=${api}` : '';
const verbose = args.includes('--verbose');
const zoneId = value('--zone', 'city-kochi');

const JARGON = /(^|[^A-Za-z])(SNS|Amazon Web Services|notification topic|http_5\d\d|shared secret)([^A-Za-z]|$)/;

/* One expression per route: the boxes the brief is about, as plain numbers. */
const MEASURE = {
  cities: `(() => {
    const rect = (node) => node ? node.getBoundingClientRect() : null;
    const off = (node, top) => node ? Math.round(rect(node).top - top) : null;
    const cards = [...document.querySelectorAll('.zone-card')].map((card) => {
      const top = rect(card).top;
      const thumb = rect(card.querySelector('.city-thumb'));
      return {
        key: card.dataset.zone,
        height: Math.round(rect(card).height),
        thumb: thumb ? [Math.round(thumb.width), Math.round(thumb.height)] : null,
        photo: Boolean(card.querySelector('.city-photo')),
        natural: (() => { const img = card.querySelector('.city-photo'); return img ? [img.naturalWidth, img.naturalHeight] : null; })(),
        plate: Boolean(card.querySelector('.city-thumb-coast')),
        title: off(card.querySelector('h3'), top),
        place: off(card.querySelector('.zone-place'), top),
        pill: off(card.querySelector('.pill'), top),
        body: off(card.querySelector('.zone-body'), top)
      };
    });
    const select = document.querySelector('#cities-sort');
    const wrap = select ? select.closest('.select-wrap') : null;
    const wrapper = wrap ? getComputedStyle(wrap, '::after') : null;
    return {
      route: '/cities',
      cards,
      select: select ? {
        width: Math.round(rect(select).width),
        height: Math.round(rect(select).height),
        appearance: getComputedStyle(select).appearance,
        textAlign: getComputedStyle(select).textAlign,
        paddingRight: getComputedStyle(select).paddingRight,
        chevron: wrapper && wrapper.content !== 'none' ? wrapper.pointerEvents : null,
        focused: document.activeElement === select
      } : null,
      overflow: document.documentElement.scrollWidth - window.innerWidth
    };
  })()`,
  overview: `(() => {
    const rect = (node) => node ? node.getBoundingClientRect() : null;
    const off = (node, top) => node ? Math.round(rect(node).top - top) : null;
    const cards = [...document.querySelectorAll('.city-card')].map((card) => {
      const top = rect(card).top;
      const thumb = rect(card.querySelector('.city-thumb'));
      return {
        key: card.getAttribute('data-zone'),
        height: Math.round(rect(card).height),
        thumb: thumb ? [Math.round(thumb.width), Math.round(thumb.height)] : null,
        photo: Boolean(card.querySelector('.city-photo')),
        title: off(card.querySelector('h3'), top),
        score: off(card.querySelector('.city-score, .kv'), top)
      };
    });
    const why = document.querySelector('#why-these-cities');
    const inset = (node) => node && why ? Math.round(rect(node).left - rect(why).left) : null;
    return {
      route: '/',
      cards,
      why: why ? {
        paddingLeft: getComputedStyle(why).paddingLeft,
        title: inset(document.querySelector('#why-cities-title')),
        lede: inset(document.querySelector('#why-cities-lede')),
        note: inset(document.querySelector('#why-cities-note')),
        button: inset(document.querySelector('#why-cities-link')),
        siblingPadding: (() => {
          const other = [...document.querySelectorAll('.card')].find((node) => node !== why);
          return other ? getComputedStyle(other).paddingLeft : null;
        })()
      } : null,
      overflow: document.documentElement.scrollWidth - window.innerWidth
    };
  })()`,
  alerts: `(() => {
    const rect = (node) => node ? node.getBoundingClientRect() : null;
    const visible = (node) => {
      const box = rect(node);
      return box && box.width > 1 && box.height > 1 && getComputedStyle(node).visibility !== 'hidden';
    };
    const text = [...document.querySelectorAll('#view-alerts p, #view-alerts li, #view-alerts h3, #view-alerts h4, #view-alerts span, #view-alerts summary')]
      .filter(visible).map((node) => (node.textContent || '').trim().replace(/\\s+/g, ' ')).filter(Boolean);
    const flagged = text.filter((line) => ${JARGON}.test(line));
    const blocks = [...document.querySelectorAll('#view-alerts > *, #view-alerts section > *')]
      .filter(visible).map((node) => ({
        tag: node.tagName.toLowerCase(),
        cls: String(node.className).slice(0, 40),
        open: node.tagName === 'DETAILS' ? node.open : null,
        text: (node.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60)
      }));
    const subscribe = document.querySelector('#view-alerts form, #view-alerts .subscribe, #subscribe-form');
    return {
      route: '/alerts',
      lines: text.length,
      flagged: flagged.slice(0, 8),
      blocks: blocks.slice(0, 14),
      subscribeLeft: subscribe ? Math.round(rect(subscribe).left) : null,
      overflow: document.documentElement.scrollWidth - window.innerWidth
    };
  })()`,
  data: `(() => {
    const rect = (node) => node ? node.getBoundingClientRect() : null;
    const sections = [...document.querySelectorAll('#view-data .about-card, #view-data .card, #view-data > section')]
      .filter((node) => getComputedStyle(node).display !== 'contents')
      // A panel inside a collapsed disclosure is not on screen: its text has no box at all, so measuring
      // it against the panel reported a phantom inset of minus the panel's own left edge.
      .filter((node) => {
        const disclosure = node.closest('details');
        return !disclosure || disclosure.open;
      });
    const insets = sections.map((node) => ({
      cls: (node.id ? '#' + node.id + ' ' : '') + String(node.className).slice(0, 34),
      paddingLeft: getComputedStyle(node).paddingLeft,
      first: (() => {
        const child = node.querySelector('h3, h4, p');
        return child ? child.tagName.toLowerCase() + ':' + (child.textContent || '').trim().slice(0, 28) : null;
      })(),
      inner: (() => {
        const first = node.querySelector('h3, h4, p');
        if (!first) return null;
        // Against the panel's own border box: a line inside the padding is an inset, and only a line
        // that leaves the box is an overflow.
        return Math.round(rect(first).left - rect(node).left);
      })()
    }));
    const flagged = [...document.querySelectorAll('#view-data p, #view-data li')]
      .map((node) => (node.textContent || '').trim().replace(/\\s+/g, ' '))
      .filter((line) => /university cloud-computing/i.test(line));
    const insetOf = (selector) => {
      const node = document.querySelector(selector);
      return node ? getComputedStyle(node).paddingLeft : null;
    };
    return { route: '/data', insets, university: flagged.slice(0, 4),
      named: { selection: insetOf('#city-selection + .card'), architecture: insetOf('.tech .card') },
      overflow: document.documentElement.scrollWidth - window.innerWidth };
  })()`
};

const ROUTES = {
  cities: { hash: '/cities', needs: '.zone-card' },
  overview: { hash: '/', needs: '#why-these-cities' },
  alerts: { hash: '/alerts', needs: '#view-alerts' },
  data: { hash: '/data', needs: '#view-data' }
};

const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const page = pages.find((entry) => entry.type === 'page' && entry.webSocketDebuggerUrl);
if (!page) {
  console.error(`no debuggable page on port ${port}`);
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
// What is measured must be what the site published, not what this profile happens to remember: the
// public assets carry a day of max-age, and a stale script would look like a mapping failure.
await send('Network.enable');
await send('Network.setCacheDisabled', { cacheDisabled: true });

const failures = [];
const note = (route, width, message) => failures.push(`${route} @${width}px: ${message}`);
const spread = (values) => {
  const clean = values.filter((value) => value !== null && value !== undefined);
  return clean.length ? Math.max(...clean) - Math.min(...clean) : 0;
};

console.log(`layout scan: ${Object.keys(ROUTES).length} routes x ${widths.length} widths against ${site}`);
const report = [];
for (const width of widths) {
  await send('Emulation.setDeviceMetricsOverride', { width, height: 1200, deviceScaleFactor: 1, mobile: width < 768 });
  for (const [name, route] of Object.entries(ROUTES)) {
    await send('Page.navigate', { url: 'about:blank' });
    await send('Page.navigate', { url: `${site}/${suffix}#${route.hash}` });
    await sleep(2600);
    // A photograph below the fold is not loaded until something asks for it. Ask, then wait for the
    // decode, so "naturalWidth > 0" means "this file is a real image" rather than "this one was visible".
    await evaluate(`(async () => {
      document.querySelectorAll('img[loading="lazy"]').forEach((img) => { img.loading = 'eager'; });
      await Promise.all([...document.images].map((img) => img.decode().catch(() => {})));
      return true;
    })()`);
    const ready = await evaluate(`Boolean(document.querySelector(${JSON.stringify(route.needs)}))`);
    if (!ready) {
      note(name, width, `nothing rendered for ${route.needs}`);
      continue;
    }
    const data = await evaluate(MEASURE[name]);
    report.push({ width, data });
    if (data.overflow > 1) note(name, width, `page scrolls sideways by ${data.overflow}px`);
    if (name === 'cities') {
      const thumbs = data.cards.map((card) => card.thumb && card.thumb[1]);
      const ratios = data.cards.map((card) => card.thumb && (card.thumb[0] / card.thumb[1]).toFixed(3));
      if (data.cards.length !== 12) note(name, width, `${data.cards.length} city cards, expected 12`);
      if (data.cards.some((card) => !card.photo)) {
        note(name, width, `a monitored city has no photograph: ` +
          data.cards.filter((card) => !card.photo).map((card) => card.key).join(', '));
      }
      const undecoded = data.cards.filter((card) => !card.natural || !card.natural[0]);
      if (undecoded.length) {
        note(name, width, `photograph(s) never decoded: ${undecoded.map((card) => card.key).join(', ')}`);
      }
      const plated = data.cards.filter((card) => card.plate);
      if (plated.length) {
        note(name, width, `the drawn plate is standing in for: ${plated.map((card) => card.key).join(', ')}`);
      }
      if (spread(thumbs) > 1) note(name, width, `photograph boxes differ by ${spread(thumbs)}px`);
      if (new Set(ratios).size > 1) note(name, width, `photograph ratios differ: ${[...new Set(ratios)].join(' ')}`);
      if (spread(data.cards.map((card) => card.title)) > 2) {
        note(name, width, `title offsets differ by ${spread(data.cards.map((card) => card.title))}px ` +
          `(${data.cards.map((card) => card.title).join(',')})`);
      }
      if (spread(data.cards.map((card) => card.body)) > 2) {
        note(name, width, `body offsets differ by ${spread(data.cards.map((card) => card.body))}px`);
      }
      const select = data.select;
      if (!select) note(name, width, 'the sort control is missing');
      else {
        if (select.appearance !== 'none') note(name, width, `the sort control still uses native appearance (${select.appearance})`);
        if (!select.chevron) note(name, width, 'the sort control has no decorative chevron');
        else if (select.chevron !== 'none') note(name, width, `the chevron captures pointer events (${select.chevron})`);
        if (select.height < 44) note(name, width, `the sort control is ${select.height}px tall`);
        if (parseFloat(select.paddingRight) < 34) note(name, width, `the sort label has only ${select.paddingRight} of right padding`);
      }
    }
    if (name === 'overview') {
      const thumbs = data.cards.map((card) => card.thumb && card.thumb[1]);
      if (spread(thumbs) > 1) note(name, width, `overview photograph boxes differ by ${spread(thumbs)}px`);
      if (spread(data.cards.map((card) => card.title)) > 2) {
        note(name, width, `overview title offsets differ by ${spread(data.cards.map((card) => card.title))}px`);
      }
      if (data.why) {
        const inset = (value) => (value === null ? null : `${value}px`);
        const edges = [data.why.title, data.why.lede, data.why.note, data.why.button];
        const numeric = edges.filter((value) => value !== null);
        if (spread(numeric) > 1) {
          note(name, width, `"Why these cities?" children start at different insets: ${numeric.map(inset).join(' ')}`);
        }
        if (parseFloat(data.why.paddingLeft) < 16) {
          note(name, width, `the panel has no inset of its own (${data.why.paddingLeft})`);
        }
        if (data.why.siblingPadding === '0px') {
          note(name, width, 'a sibling panel on the same page is inset by nothing at all');
        }
      }
    }
    if (name === 'alerts' && data.flagged.length) {
      note(name, width, `operator wording is visible: "${data.flagged[0]}"`);
    }
    if (name === 'data') {
      const thin = data.insets.filter((row) => parseFloat(row.paddingLeft) < 16);
      if (thin.length) {
        note(name, width, `${thin.length} panel(s) are inset by less than 16px: ` +
          thin.map((row) => `${row.cls || 'section'} ${row.paddingLeft}`).join(', '));
      }
      const flush = data.insets.filter((row) => row.inner !== null && row.inner < -1);
      if (flush.length) {
        note(name, width, `${flush.length} panel(s) put their first line left of their own content edge: ` +
          flush.map((row) => `${row.cls || 'section'} ${row.inner}px [${row.first}]`).join(', '));
      }
      for (const [label, measure] of [['City selection', data.named && data.named.selection],
                                    ['Architecture', data.named && data.named.architecture]]) {
        if (!measure || parseFloat(measure) < 16) {
          note(name, width, `the ${label} panel has no shared inset (${measure})`);
        }
      }
      if (data.university.length) {
        note(name, width, `"university cloud-computing project" is still on the Data page: "${data.university[0].slice(0, 60)}"`);
      }
    }
  }
}

if (verbose) {
  for (const entry of report) console.log(`   ${JSON.stringify(entry).slice(0, 400)}`);
}
console.log(`\n${failures.length} contract failure(s)`);
for (const line of failures.slice(0, 30)) console.log(`   FAIL ${line}`);
if (failures.length > 30) console.log(`   ... and ${failures.length - 30} more`);
if (!failures.length) {
  console.log('pass: cards share one geometry, panels share one inset, the sort control is dressed, ' +
    'and the public routes carry no operator wording');
}
socket.close();
process.exit(failures.length ? 1 : 0);
