/*
 * Shared fake DOM for the dashboard harnesses (development tool, not part of the site).
 *
 * dashboard_run.mjs and dashboard_scenarios.mjs both execute the real web/lib.js and
 * web/app.js inside a Node VM. This module provides the minimum DOM those files touch,
 * plus event dispatch and scripted fetch/time control, so a harness can drive the page
 * through real user actions and real failures instead of only loading it once.
 */

export function createFakeDom() {
  const noop = () => {};
  const nodes = new Map();
  const stubNodes = new Map();
  const listeners = new Map(); // node (or the document/window stubs) -> { type: [fn] }

  function addListener(target, type, fn) {
    if (!listeners.has(target)) listeners.set(target, {});
    const byType = listeners.get(target);
    (byType[type] = byType[type] || []).push(fn);
  }

  function makeCanvasContext() {    return new Proxy({}, {
      get: (target, prop) => {
        if (prop === 'measureText') return () => ({ width: 12 });
        if (prop === 'createLinearGradient') return () => ({ addColorStop: noop });
        if (prop in target) return target[prop];
        return noop;
      },
      set: (target, prop, value) => { target[prop] = value; return true; }
    });
  }

  /* Attributes are stored per node so delegated handlers (data-zone-pick, data-range,
   * data-incident) can be read back the way a browser would. */
  const attributes = new Map(); // id -> Map

  function attrsOf(node) {
    if (!attributes.has(node.id)) attributes.set(node.id, new Map());
    return attributes.get(node.id);
  }

  function makeNode(id) {
    const node = {
      id,
      className: '',
      value: '',
      width: 800,
      height: 300,
      clientWidth: 800,
      clientHeight: 300,
      children: [],
      dataset: {},
      hidden: false,
      style: {
        setProperty(name, value) { attrsOf(node).set('style:' + name, String(value)); },
        removeProperty(name) { attrsOf(node).delete('style:' + name); },
        getPropertyValue(name) { return attrsOf(node).get('style:' + name) || ''; }
      },
      classList: {
        _set: new Set(),
        add(...names) { names.forEach((n) => this._set.add(n)); },
        remove(...names) { names.forEach((n) => this._set.delete(n)); },
        contains(name) { return this._set.has(name); },
        toggle(name, force) {
          const on = force === undefined ? !this._set.has(name) : !!force;
          if (on) this._set.add(name); else this._set.delete(name);
          return on;
        }
      },
      get innerHTML() { return this._html || ''; },
      // Assigning innerHTML replaces the element's children in a browser, so the stub drops
      // them too: a harness that reads `children` after a re-render must not see the old ones.
      set innerHTML(value) { this._html = String(value); this.children.length = 0; },
      setAttribute(name, value) { attrsOf(node).set(name, String(value)); },
      getAttribute(name) { return attrsOf(node).has(name) ? attrsOf(node).get(name) : null; },
      removeAttribute(name) { attrsOf(node).delete(name); },
      appendChild(child) { this.children.push(child); return child; },
      removeChild(child) { return child; },
      addEventListener(type, fn) { addListener(node, type, fn); },
      removeEventListener: noop,
      // Memoised per (node, selector) so repeated lookups during polling patch the same
      // stub, and the harness can read back what patchCard wrote into a card field.
      querySelector(selector) {
        const key = node.id + '>>' + selector;
        if (!stubNodes.has(key)) stubNodes.set(key, makeNode(key));
        return stubNodes.get(key);
      },
      querySelectorAll: () => [],
      closest: () => null,
      getContext: () => makeCanvasContext(),
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 300, right: 800, bottom: 300 }),
      // focus() records the focused node the way a browser exposes document.activeElement,
      // so a harness can check where the keyboard landed after a view change.
      focus() { documentStub.activeElement = this; },
      blur: noop,
      remove: noop
    };
    let text = '';
    Object.defineProperty(node, 'textContent', {
      get() { return text; },
      set(value) { text = String(value); },
      configurable: true
    });
    return node;
  }

  const body = makeNode('body');
  /* The viewport is a mutable fact of this fake DOM: media queries resolve against it, so a
   * harness can prove what the page does on a phone-sized screen and on a desktop. */
  let viewportWidth = 1280;
  function mediaMatches(query) {
    const max = /max-width:\s*(\d+)px/.exec(String(query));
    const min = /min-width:\s*(\d+)px/.exec(String(query));
    if (max && viewportWidth > Number(max[1])) return false;
    if (min && viewportWidth < Number(min[1])) return false;
    return true;
  }
  function matchMedia(query) {
    return { media: String(query), matches: mediaMatches(query), addEventListener: noop, removeEventListener: noop };
  }
  const documentStub = {
    body,
    documentElement: makeNode('html'),
    activeElement: body,
    visibilityState: 'visible',
    hidden: false,
    readyState: 'complete',
    title: '',
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, makeNode(id));
      return nodes.get(id);
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag) => makeNode(tag),
    addEventListener(type, fn) { addListener(documentStub, type, fn); },
    removeEventListener: noop
  };
  const windowStub = {
    location: { search: '', hash: '', href: 'http://localhost/' },
    FLOODPULSE_API_BASE: '',
    innerWidth: viewportWidth,
    devicePixelRatio: 1,
    matchMedia,
    addEventListener(type, fn) { addListener(windowStub, type, fn); },
    removeEventListener: noop
  };

  function fire(target, type, event) {
    const byType = listeners.get(target);
    if (!byType || !byType[type]) return 0;
    byType[type].forEach((fn) => fn(event || {}));
    return byType[type].length;
  }

  /* A stand-in for event.target.closest(selector): the harness says which delegation
   * target the event should resolve to, exactly as a browser would resolve it. */
  function targetWith(selector, attribute, value) {
    return {
      selector,
      closest(wanted) {
        if (wanted !== selector) return null;
        return { getAttribute: (name) => (name === attribute ? value : null) };
      }
    };
  }

  return {
    nodes,
    body,
    document: documentStub,
    window: windowStub,
    node: (id) => nodes.get(id) || documentStub.getElementById(id),
    text: (id) => {
      const node = nodes.get(id);
      return node ? node.textContent : '';
    },
    html: (id) => {
      const node = nodes.get(id);
      return node ? node.innerHTML : '';
    },
    title: () => documentStub.title,
    focusedId: () => (documentStub.activeElement ? documentStub.activeElement.id : null),
    /* Resize the fake viewport: `max-width`/`min-width` queries then answer accordingly,
     * which is how a harness drives the responsive behaviour of the real stylesheet. */
    setViewport(width) {
      viewportWidth = Number(width) || 0;
      windowStub.innerWidth = viewportWidth;
      return viewportWidth;
    },
    matches: (query) => mediaMatches(query),
    fire,
    targetWith,
    listeners,
    /* Read back what the controller wrote into a card/section stub, e.g.
     * stub('zones', '[data-zone="city-chennai"]') */
    stub: (nodeId, selector) => stubNodes.get(nodeId + '>>' + selector) || null
  };
}

/*
 * A scriptable request/clock layer. `route` handlers are matched in order against the
 * request path (before the query string), so a scenario can make one route fail while
 * others keep working, then change its mind mid-scenario.
 */
export function createRuntime() {
  const requests = [];
  const handlers = [];
  const timers = [];
  const timeouts = [];
  const held = [];
  let nowMs = Date.parse('2026-10-15T17:00:00Z');

  /* Patterns are exact pathnames (query string ignored) or regular expressions, so that
   * '/zones' cannot swallow '/zones/city-x/history' like a prefix match would. */
  function on(pattern, handler) {
    handlers.push({ pattern, handler });
  }

  function matchPath(path) {
    return handlers.find((entry) => (entry.pattern instanceof RegExp
      ? entry.pattern.test(path)
      : entry.pattern === path || entry.pattern === '*'));
  }

  /* Freeze a route: the request stays pending until release() is called, which is how the
   * in-flight guard is tested without a real slow network. */
  function hold(pattern, answer) {
    held.push({ pattern, answer: answer || { body: null } });
  }

  function release() {
    held.length = 0;
  }

  function pathnameOf(url) {
    try {
      return new URL(String(url)).pathname;
    } catch (error) {
      return String(url).split('?')[0];
    }
  }

  async function fetchStub(url, options) {
    const text = String(url);
    const path = pathnameOf(text);
    requests.push(text);
    const signal = options && options.signal;
    const frozen = held.find((entry) => (entry.pattern instanceof RegExp
      ? entry.pattern.test(path)
      : entry.pattern === path));
    if (frozen) {
      // A held route behaves like a request that never answers: it stays pending until the
      // page's own AbortController timeout fires, exactly as a hung request would.
      return await new Promise((resolve, reject) => {
        if (signal) {
          signal.addEventListener('abort', () => reject(new Error('The operation was aborted.')));
        }
      });
    }
    const entry = matchPath(path);
    const answer = entry ? await entry.handler(path, text) : { status: 404, body: { error: 'not_found' } };
    if (answer && answer.throw) throw answer.throw;
    const status = answer && answer.status !== undefined ? answer.status : 200;
    const body = answer ? answer.body : null;
    // `text()` exists on every real Response, and the subscription form drains the body with it
    // so that a server error message is never rendered; a stub without it would make the page
    // look like it had failed while the network was fine.
    const asText = () => Promise.resolve(body === null ? '' : JSON.stringify(body));
    if (status < 200 || status >= 300) {
      return {
        ok: false,
        status,
        text: asText,
        json: () => Promise.resolve(body === null ? {} : body)
      };
    }
    if (answer && answer.raw !== undefined) {
      // a 200 with a body that is not JSON: response.json() rejects
      return {
        ok: true,
        status,
        text: () => Promise.resolve('<html>not json</html>'),
        json: () => Promise.reject(new SyntaxError('Unexpected token < in JSON'))
      };
    }
    return { ok: true, status, text: asText, json: () => Promise.resolve(body) };
  }

  const settle = async () => {
    for (let i = 0; i < 25; i += 1) await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    for (let i = 0; i < 25; i += 1) await Promise.resolve();
  };

  class FakeDate extends Date {
    constructor(...values) { super(...(values.length ? values : [nowMs])); }
    static now() { return nowMs; }
  }

  function sandbox(dom) {
    return {
      console: { log: () => {}, warn: () => {}, error: () => {} },
      window: dom.window,
      document: dom.document,
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
      SyntaxError,
      TypeError,
      isFinite,
      parseInt,
      parseFloat,
      encodeURIComponent,
      decodeURIComponent,
      URLSearchParams,
      AbortController,
      getComputedStyle: () => ({ getPropertyValue: () => '' }),
      matchMedia: dom.window.matchMedia,
      setInterval: (fn, delay) => { timers.push({ fn, delay }); return timers.length; },
      clearInterval: () => {},
      // Real timers, driven by the fake clock: the page's fetch timeout/abort path is part
      // of the behaviour under test, so it must actually be able to fire.
      setTimeout: (fn, delay) => {
        timeouts.push({ fn, delay: delay || 0, at: nowMs + (delay || 0) });
        return timeouts.length;
      },
      clearTimeout: (id) => {
        if (id) timeouts[id - 1] = null;
      },
      requestAnimationFrame: () => 0
    };
  }

  return {
    requests,
    timers,
    on,
    hold,
    release,
    sandbox,
    settle,
    now: () => nowMs,
    /* Move the fake clock forward; intervals are fired by the caller via driveTimers(),
     * while due setTimeout callbacks (e.g. the page's fetch abort) fire immediately. */
    async advance(ms) {
      nowMs += ms;
      await this.fireTimeouts();
    },
    async fireTimeouts() {
      const due = timeouts.filter((entry) => entry && entry.at <= nowMs);
      due.forEach((entry) => { entry.fn(); timeouts[timeouts.indexOf(entry)] = null; });
      await settle();
    },
    /* Fire every registered interval once, as a browser tick would. */
    async driveTimers() {
      for (const timer of timers) {
        timer.fn();
        await settle();
      }
    },
    /* Fire the timers until the request count stops growing (a poll actually happened). */
    async driveUntilRequest(stopAfter = 2) {
      for (let round = 0; round < 8; round += 1) {
        const before = requests.length;
        await this.driveTimers();
        if (requests.length > before) {
          // give the promise chain the chance to finish before deciding to stop
          if (round + 1 >= stopAfter) return true;
        }
      }
      return false;
    },
    count: (pattern) => requests.filter((url) => {
      const path = pathnameOf(url);
      return pattern instanceof RegExp ? pattern.test(path) : path === pattern;
    }).length,
    countMatching: (fragment) => requests.filter((url) => url.includes(fragment)).length,
    lastRequest: () => requests[requests.length - 1],
    /* Fire only the tickers with the given period (e.g. the 1 s freshness ticker)
     * without letting the polling interval fetch anything new. */
    async fireDelay(delay) {
      for (const timer of timers) {
        if (timer.delay !== delay) continue;
        timer.fn();
        await settle();
      }
    }
  };
}
