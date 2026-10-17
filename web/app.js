/* FloodPulse live dashboard controller.
 *
 * All displayed state comes from the deployed API (DynamoDB -> Lambda -> API Gateway).
 * This file never invents a risk value: on a failed request it keeps the last good
 * payload and marks the page stale/disconnected instead of faking freshness.
 */
(function () {
  'use strict';

  var FP = window.FP;

  var params = new URLSearchParams(window.location.search);

  /* The page states its own API base in a data attribute, not in a script tag: the deployed
   * page carries a Content-Security-Policy with no 'unsafe-inline', so there is no inline
   * script allowed anywhere on it. A window global is still honoured for local harnesses, and
   * ?api= still overrides both for testing. */
  function configuredApiBase() {
    var fromPage = '';
    try {
      fromPage = (document.documentElement && document.documentElement.dataset.api) || '';
    } catch (error) {
      fromPage = '';
    }
    return params.get('api') || window.FLOODPULSE_API_BASE || fromPage || '';
  }

  var API_BASE = configuredApiBase().replace(/\/+$/, '');

  /* An API base may be overridden with ?api=<origin> for testing, which means it can also be
   * arbitrary text from a link. It is only ever used as a fetch origin and as a display link,
   * so anything that is not plainly an http(s) origin is ignored rather than followed: a
   * javascript:/data: value would otherwise end up in an href, and a relative value would
   * silently fetch from the dashboard host. */
  function isHttpOrigin(value) {
    return typeof value === 'string' && /^https?:\/\/[^\s"'<>]+$/i.test(value);
  }

  API_BASE = isHttpOrigin(API_BASE) ? API_BASE : '';

  var POLL_MS = 5000;
  var HISTORY_REFRESH_MS = 30000;
  var HISTORY_RETRY_MS = 45000;
  var INCIDENTS_REFRESH_MS = 10000;
  var INCIDENTS_LIMIT = 20;
  var FETCH_TIMEOUT_MS = 9000;
  var SESSION_POINTS = 60;
  var BAND_EDGES = [0.35, 0.60, 0.80]; // heuristic-v1 band boundaries (see README)
  var BAND_FILL = {
    NORMAL: 'rgba(34, 211, 238, 0.16)',
    WATCH: 'rgba(251, 191, 36, 0.16)',
    WARNING: 'rgba(249, 115, 22, 0.18)',
    CRITICAL: 'rgba(239, 68, 68, 0.20)'
  };
  var BAND_STROKE = {
    normal: '#22d3ee',
    watch: '#fbbf24',
    warning: '#f97316',
    critical: '#ef4444',
    unknown: '#64748b'
  };

  var el = {};
  var state = {
    zones: [],
    lastSuccessMs: null,
    lastError: false,
    lastErrorMessage: null,
    payload: null,
    // class per zone at the previous successful poll, so the board can say what moved
    previousClasses: null,
    series: {},
    history: null,
    selectedZone: null,
    userSelected: false,
    historyMinutes: 60,
    inFlight: false,
    hoverIndex: null,
    incidents: null,
    incidentsSignature: null,
    incidentsMs: null,
    incidentsError: null,
    // one /incidents request at a time: the poll and the 10 s interval used to overlap
    incidentsInFlight: false,
    // history bookkeeping: which zone the loaded series belongs to, and when a failed
    // attempt happened, so a broken history route is not retried on every 5 s poll.
    historyZone: null,
    historyMinutesLoaded: null,
    historyInFlight: false,
    historyFailedMs: null,
    // why the last history load failed, so the empty chart can say "could not be loaded"
    // rather than the misleading "no history in this window"
    historyError: null,
    connectionKey: null,
    // what the off-screen polite region last announced, so it only speaks on real changes
    connAnnounceKey: null,
    // when the current payload arrived, and how much local time has passed since: the feed
    // ages reported by the API are interpolated forward so the freshness line keeps moving
    payloadReceivedMs: null,
    // Monotonic request stamps: a response that is no longer the newest one for its route is
    // dropped instead of being written over newer state (a slow reply must not win a race
    // against the poll it belongs to).
    zonesSeq: 0,
    zonesAppliedSeq: 0,
    incidentsSeq: 0,
    incidentsAppliedSeq: 0,
    historySeq: 0,
    // which view the URL asks for, and what each view currently shows (so a poll that
    // changed nothing does not rewrite the DOM). The route starts empty so the first
    // applyRoute() always installs the view the URL asked for.
    route: { view: null, zoneId: null },
    overviewSignature: null,
    cityCardSignature: null,
    detailSignature: null,
    alertsSignature: null,
    dataSignature: null,
    // the worked example on the methodology page is tied to the reading it shows, not to the
    // payload's mode, so it re-renders when that reading changes
    breakdownSignature: null,
    // M9: the activity panel, the About page and the system-health pill each render from a
    // signature so a poll that changed nothing does not rewrite the DOM
    activitySignature: null,
    aboutSignature: null,
    healthSignature: null,
    cityIncidentsSignature: null,
    // risk-class transitions this page watched happen while it was open, newest first
    changes: [],
    menuOpen: false
  };

  /* Seconds elapsed since the payload was received, added to the ages inside it. */
  function payloadAgeOffsetSeconds() {
    if (state.payloadReceivedMs === null) return 0;
    return Math.max(0, (Date.now() - state.payloadReceivedMs) / 1000);
  }

  function $(id) { return document.getElementById(id); }

  /* Writing the same string into a node still dirties it, so every text update goes
   * through here: the one-second ticker must not churn the DOM for nothing. */
  function setText(id, text) {
    var node = el[id];
    if (!node) return;
    var value = (text === null || text === undefined) ? '' : String(text);
    if (node.textContent !== value) node.textContent = value;
  }

  function cacheDom() {
    var ids = [
      'conn-badge', 'conn-detail', 'conn-announce', 'last-update', 'api-base', 'hero', 'ranking', 'zones',
      'history-zone', 'history-range', 'history-canvas', 'history-meta', 'history-tooltip',
      'notice-text', 'summary-live', 'summary-model', 'summary-generated', 'poll-count',
      'incidents', 'incidents-meta', 'incidents-alerting',
      'data-banner', 'banner-title', 'banner-detail', 'summary-mode', 'summary-weather', 'attribution',
      'brand-subtitle', 'summary-freshness', 'footer-disclaimer', 'ranking-title', 'zones-title',
      'change-note',
      // fleet distribution strip: renderBandStrip() and bandStripLabel() are guards on
      // these two entries, so leaving them out of this list silently froze the strip at
      // its static placeholders (found by the runtime harness, not by the static checks)
      'band-strip', 'band-strip-label',
      // navigation and the views (M6 information architecture, extended with About in M9)
      'nav-toggle', 'nav-list', 'nav-overview', 'nav-cities', 'nav-alerts', 'nav-data', 'nav-about',
      'view-overview', 'view-cities', 'view-city', 'view-alerts', 'view-data', 'view-about',
      'ov-headline', 'ov-subline', 'ov-intro', 'ov-stats', 'band-strip-title',
      'city-cards-title', 'cities-lede', 'incidents-zone-header', 'city-cards', 'city-detail', 'city-crumb',
      'alerts-lede', 'alerts-summary', 'alerts-active', 'alerts-closed',
      'provenance-legend', 'attribution-block', 'attribution-detail', 'summary-weather-provider',
      'method-block', 'why-simulated', 'limitations', 'summary-model-data', 'score-breakdown',
      // M9: system health, recent activity, alert delivery, per-city alert history and About
      'health', 'health-pill', 'health-dot', 'health-word', 'health-detail', 'health-list',
      'recent-activity', 'alerts-status', 'alerts-delivery', 'city-incidents',
      'about-cities', 'about-limitations', 'history-summary'
    ];
    ids.forEach(function (id) { el[id] = $(id); });
    // A page and a controller from different revisions (a half-finished deploy, a hand-edited
    // index.html) must not fail as a TypeError in the middle of start(): the missing ids are
    // reported once, together, and the parts that can still work keep working.
    var missing = ids.filter(function (id) { return !el[id]; });
    if (missing.length) {
      state.domMismatch = missing;
      if (typeof console !== 'undefined' && console.warn) {
        console.warn('FloodPulse: ' + missing.length + ' element(s) referenced by app.js are not in this page: ' + missing.join(', '));
      }
    }
  }

  /* Wire an event only when its element exists; a missing node is a page/controller
   * revision mismatch, not a reason to abort every other listener. */
  function bindTo(id, event, handler, options) {
    var node = el[id];
    if (!node || typeof node.addEventListener !== 'function') return;
    node.addEventListener(event, handler, options);
  }

  /* ------------------------------------------------------------------ fetch --- */

  function getJson(path) {
    // AbortController is only used to *cancel* a request that the caller has already given up
    // on; it is not what makes the dashboard recover. Every caller runs its own watchdog and
    // a request-sequence guard, so recovery works in a browser without AbortController too.
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, FETCH_TIMEOUT_MS) : null;
    var url = API_BASE + path + (path.indexOf('?') >= 0 ? '&' : '?') + 'cachebust=' + Date.now();
    return fetch(url, {
      method: 'GET',
      mode: 'cors',
      cache: 'no-store',
      signal: controller ? controller.signal : undefined
    }).then(function (response) {
      if (!response.ok) {
        throw new Error('HTTP ' + response.status);
      }
      return response.json();
    }).then(function (data) {
      if (timer) clearTimeout(timer);
      return data;
    }).catch(function (error) {
      if (timer) clearTimeout(timer);
      throw error;
    });
  }

  /* A request the page has stopped waiting for. Returns the sequence number of the request
   * now considered current, so a late answer can be recognised and dropped. */
  function markedStale(seq, appliedSeq) {
    return seq !== null && appliedSeq >= seq;
  }

  /* ------------------------------------------------------------------- poll --- */

  function poll() {
    if (state.inFlight) return;
    state.inFlight = true;
    var seq = ++state.zonesSeq;
    // Watchdog: if this request never answers, stop counting it as in flight so the next poll
    // can run. The answer, if it ever arrives, is discarded by the sequence checks below.
    var giveUp = setTimeout(function () {
      if (state.zonesAppliedSeq >= seq) return;
      state.inFlight = false;
      state.lastError = true;
      state.lastErrorMessage = 'no answer within ' + Math.round(FETCH_TIMEOUT_MS / 1000) + ' s';
      refreshConnection();
    }, FETCH_TIMEOUT_MS);
    getJson('/zones').then(function (payload) {
      clearTimeout(giveUp);
      if (markedStale(seq, state.zonesAppliedSeq)) return; // an older reply: drop it
      state.zonesAppliedSeq = seq;
      state.inFlight = false;
      state.lastError = false;
      state.lastErrorMessage = null;
      state.lastSuccessMs = Date.now();
      state.payloadReceivedMs = Date.now();
      state.payload = payload;
      // The request succeeded, so the status line must say so whatever the payload contains.
      // A malformed entry that defeats one renderer is logged and the rest of the board keeps
      // its values: a payload must not be able to freeze the page on "CONNECTING" forever.
      try {
        applyPayload(payload);
      } catch (error) {
        if (typeof console !== 'undefined' && console.error) {
          console.error('FloodPulse: the payload could not be fully rendered: ' + (error && error.message));
        }
        refreshConnection();
      }
      // Incidents have their own 10 s interval. Polling them from here as well meant two
      // /incidents requests fired together every time the 5 s poll crossed a 10 s boundary.
    }).catch(function (error) {
      clearTimeout(giveUp);
      if (state.zonesAppliedSeq >= seq) return;
      state.inFlight = false;
      state.lastError = true;
      state.lastErrorMessage = error.message;
      // keep the last good payload and let refreshConnection describe the failure in the
      // status line, so a long outage cannot leave one stale error message on screen
      refreshConnection();
    });
  }

  /* Every label that names the fleet itself is written from the fleet's own mode, in one
   * place, so no view can drift into calling a simulated zone a city (or the reverse). */
  function renderFleetLabels(payload) {
    var singular = FP.zoneNoun(payload);
    var plural = FP.zoneNounPlural(payload);
    var Plural = plural.charAt(0).toUpperCase() + plural.slice(1);
    var Singular = singular.charAt(0).toUpperCase() + singular.slice(1);
    setText('ov-intro', FP.productIntro(payload));
    setText('band-strip-title', Plural + ' by risk level');
    setText('city-cards-title', Singular + ' status');
    setText('cities-lede', 'Every monitored ' + singular + ' with its current risk level. ' +
      'Open one for the full picture.');
    setText('incidents-zone-header', Singular);
    // the section names ("Cities", the nav label, the window title) are part of the
    // information architecture and stay fixed; only sentences about the data follow the data
  }

  /* A payload element that is not an object (null, a string, a number) is not a zone and not an
   * incident. It is dropped where the list enters state, so every consumer below - the card
   * grid, the ranking, the change note, the session series, the alert view - can rely on
   * objects and one malformed element cannot stop the board updating. The API's own counts are
   * left untouched: they say what the fleet holds, not what this page could render. */
  function usableEntries(list) {
    return (Array.isArray(list) ? list : []).filter(function (entry) {
      return entry && typeof entry === 'object';
    });
  }

  function applyPayload(payload) {
    state.zones = usableEntries(payload.zones);

    if (!state.userSelected || !findZone(state.selectedZone)) {
      state.selectedZone = payload.highest_risk_zone ? payload.highest_risk_zone.zone_id
        : (state.zones[0] ? state.zones[0].zone_id : null);
    }

    rememberSeries(state.zones);
    renderFleetLabels(payload);
    renderSummary(payload);
    renderTitles(payload);
    renderBandStrip(payload);
    renderChangeNote(payload);
    renderHero(payload);
    renderCards(state.zones);
    renderRanking(state.zones);
    renderHistoryButtons(state.zones);
    // every view is rendered on every successful poll, not only the visible one: the
    // sections are small, and switching a view must never show a stale answer
    renderOverview(payload);
    renderDataPage(payload);
    renderAlerts();
    renderAbout(payload);
    renderSystemHealth();
    if (state.route.view === 'city') renderCityDetail();
    refreshConnection();

    if (state.historyZone !== state.selectedZone || state.historyMinutesLoaded !== state.historyMinutes) {
      // The API may re-point the default zone at a new highest-risk zone (the live fleet
      // changes leader as rain moves). Reload so the chart always matches the highlighted
      // zone button instead of silently showing a different city's series.
      loadHistory();
    }
  }

  /* The board is one page in both modes, so its headings follow the fleet the payload
   * describes: "city" in the India live fleet, "zone" in the simulated demo fleet. */
  function renderTitles(payload) {
    var noun = FP.zoneNoun(payload);
    var plural = FP.zoneNounPlural(payload);
    setText('ranking-title', 'Risk ranking by ' + noun);
    if (el['zones-title']) {
      el['zones-title'].innerHTML = noun.charAt(0).toUpperCase() + noun.slice(1) +
        ' detail <span class="muted">live values from DynamoDB via the API</span>';
    }
    if (el['summary-live'] && el['summary-live'].title !== plural) {
      el['summary-live'].title = 'How many ' + plural + ' have a scored reading in this snapshot';
    }
  }

  /* Fleet risk distribution: rendered from the payload only, so an empty or partial
   * snapshot shows the counts it can prove instead of a reassuring zero. */
  function renderBandStrip(payload) {
    if (!el['band-strip']) return;
    var html = FP.renderBandStrip(payload);
    if (el['band-strip'].getAttribute('data-rendered') !== html) {
      el['band-strip'].innerHTML = html;
      el['band-strip'].setAttribute('data-rendered', html);
    }
    setText('band-strip-label', FP.bandStripLabel(payload));
  }

  /* What changed since the last poll. The comparison is against the previous *successful*
   * snapshot and is then forgotten, so a single escalation is stated once and does not keep
   * claiming attention on every subsequent poll. */
  function renderChangeNote(payload) {
    var note = el['change-note'];
    var change = FP.fleetChange(state.previousClasses, payload);
    state.previousClasses = FP.fleetClasses(payload);
    // Keep the last few observed changes, newest first, for the Overview activity panel. These
    // are transitions this page watched happen - not a history, and not a claim about anything
    // that happened while nobody was looking at the page.
    if (change.text) {
      state.changes = [change.text].concat(state.changes || []).slice(0, 4);
    }
    if (!note) return;
    var classes = 'change-note' + (change.tone ? ' ' + change.tone : '');
    note.className = classes;
    note.textContent = change.text;
    note.hidden = !change.text;
  }

  function findZone(zoneId) {
    if (!zoneId) return null;
    for (var i = 0; i < state.zones.length; i += 1) {
      if (state.zones[i].zone_id === zoneId) return state.zones[i];
    }
    return null;
  }

  function rememberSeries(zones) {
    zones.forEach(function (zone) {
      var latest = zone.latest;
      if (!latest) return;
      var list = state.series[zone.zone_id] || (state.series[zone.zone_id] = []);
      var last = list[list.length - 1];
      if (last && last.ts_epoch === latest.ts_epoch) {
        last.risk_score = latest.risk_score; // same reading, refreshed view
        return;
      }
      list.push({ ts_epoch: latest.ts_epoch, risk_score: latest.risk_score });
      while (list.length > SESSION_POINTS) list.shift();
    });
  }

  /* ---------------------------------------------------------------- summary --- */

  function renderSummary(payload) {
    var live = FP.liveCount(state.zones);
    var mode = FP.modeMeta(payload.zone_mode);
    setText('summary-live', live + ' / ' + (payload.zone_count || state.zones.length) + ' ' +
      FP.zoneNounPlural(payload) + ' reporting');
    setText('summary-model', payload.model || 'heuristic-v1');
    setText('summary-generated', FP.clockLabel(payload.generated_at));
    setText('summary-mode', mode.label + (payload.data_classification ? ' · ' + payload.data_classification : ''));
    if (el['summary-mode']) el['summary-mode'].title = mode.title;
    setText('summary-weather', FP.weatherSummary(payload));
    renderFreshness(payload);
    renderDataNotice(payload);
    if (el['api-base']) {
      setText('api-base', API_BASE || 'not configured');
      if (API_BASE) {
        el['api-base'].setAttribute('href', API_BASE + '/zones');
      } else {
        el['api-base'].removeAttribute('href');
        el['api-base'].title = 'the dashboard has no valid API origin to point at';
      }
    }
  }

  /* Data freshness is its own panel row, not a footnote: it is the difference between
   * "the API answered" and "the weather is new". Recomputed every second so the
   * countdown to the next provider check actually moves. */
  function renderFreshness(payload) {
    if (!el['summary-freshness']) return;
    var feed = FP.feedStatus(payload, Date.now(), payloadAgeOffsetSeconds());
    if (!feed.live) {
      setText('summary-freshness', 'simulator feed');
      if (el['summary-freshness']) el['summary-freshness'].title = 'Demo mode: readings come from the FloodPulse simulator';
      return;
    }
    var line = FP.freshnessLine(feed) || 'no weather fetched yet';
    setText('summary-freshness', line);
    if (el['summary-freshness']) {
      el['summary-freshness'].title = line;
      el['summary-freshness'].setAttribute('data-feed-state', feed.state);
    }
  }

  // The banner is driven by the payload so the page stays honest in both modes: demo
  // payloads say everything is simulated, live payloads name what is real. A stale or
  // failing feed colours it amber/red instead of pretending nothing is wrong.
  function renderDataNotice(payload) {
    if (!el['data-banner']) return;
    var feed = FP.feedStatus(payload, Date.now(), payloadAgeOffsetSeconds());
    el['data-banner'].classList.toggle('is-live-data', feed.live);
    el['data-banner'].classList.toggle('is-feed-degraded', feed.live && (feed.state === 'stale' || feed.state === 'failed'));
    el['data-banner'].classList.toggle('is-feed-off', feed.live && feed.state === 'off');
    setText('banner-title', FP.modeTitle(payload));
    setText('banner-detail', FP.dataNotice(payload));
    setText('attribution', FP.dataAttribution(payload));
    if (el['footer-disclaimer'] && payload.model_note) {
      setText('footer-disclaimer', payload.model_note);
    }
    // The page title line states the fleet the payload actually describes, so a live
    // board is never captioned with the demo fleet's cities.
    if (el['brand-subtitle']) {
      setText('brand-subtitle', fleetSubtitle(payload, feed));
    }
  }

  function fleetSubtitle(payload, feed) {
    // An element that is not an object is not a zone, so it has no name to list. The subtitle
    // used to throw here on a malformed payload, which - because this runs before the rest of
    // the render - left the whole board frozen on its loading placeholders.
    var zones = usableEntries(payload && payload.zones);
    var names = zones.slice(0, 5).map(function (zone) { return zone.name; });
    var suffix = zones.length > names.length ? ' +' + (zones.length - names.length) : '';
    if (feed.live) {
      return 'India live-weather monitoring · ' + (feed.zoneCount || zones.length) +
        ' cities · ' + names.join(', ') + suffix;
    }
    return 'Simulated sensor fleet · ' + zones.length + ' zones · ' +
      (names.length ? names.join(', ') + suffix : 'demo mode');
  }

  function refreshConnection() {
    var info = FP.connectionState(Date.now(), state.lastSuccessMs, state.lastError, state.payload, {
      ageOffsetSeconds: payloadAgeOffsetSeconds(),
      lastErrorMessage: state.lastErrorMessage
    });
    // Toggling body classes re-styles the whole document, so only do it on a real
    // transition; the one-second ticker otherwise rewrote the same classes forever.
    if (state.connectionKey !== info.state) {
      state.connectionKey = info.state;
      if (el['conn-badge']) el['conn-badge'].className = 'conn-badge conn-' + info.state;
      document.body.classList.remove('is-live', 'is-stale', 'is-disconnected', 'is-connecting');
      document.body.classList.add('is-' + info.state);
    }
    // A first poll that already failed is still CONNECTING, but it must not pulse as if it
    // were loading: the CSS pulse is scoped to is-connecting without is-waiting-failed.
    document.body.classList.toggle('is-waiting-failed', info.state === 'connecting' && !!state.lastError);
    setText('conn-badge', info.label);
    setText('conn-detail', info.detail);
    // The visible status line ticks with the clock, so it is not a live region. The off-screen
    // polite region gets a sentence with no ages in it, and only when the state really
    // changes: announcing the ticking line re-read the top bar once per second, forever.
    if (state.connAnnounceKey !== info.announceKey) {
      state.connAnnounceKey = info.announceKey;
      setText('conn-announce', info.announcement);
    }
    if (state.lastSuccessMs) {
      setText('last-update', FP.clockLabel(new Date(state.lastSuccessMs).toISOString()));
    }
    // the feed countdown ticks here, once per second, on the panel row only
    if (state.payload) renderFreshness(state.payload);
    if (state.route.view === 'city') tickDetailFreshness();
    // The health pill and the chrome around the connection badge change with the connection
    // state, so they are refreshed on the same one-second tick (and are signature-guarded).
    renderSystemHealth();
  }

  /* ------------------------------------------------------------------- hero --- */

  function renderHero(payload) {
    var highest = payload.highest_risk_zone;
    var container = el.hero;
    var feed = FP.feedStatus(payload, Date.now());
    var noun = FP.zoneNoun(payload);
    if (!highest) {
      // An empty board has to say why it is empty, and the reason differs per mode: in
      // live mode nothing is started by hand, the fleet waits for the provider.
      var hint = feed.live
        ? 'No city reading has been computed yet. The live fleet fetches rainfall and soil ' +
          'moisture from ' + FP.escapeHtml(feed.provider || 'the weather provider') +
          (feed.refreshMinutes ? ' every ' + FP.escapeHtml(feed.refreshMinutes) + ' minutes' : '') +
          ' and this panel fills in as soon as the first refresh is ingested.'
        : 'No simulated reading has been ingested yet. Start the simulator ' +
          '(<code>python tools\\live_simulator.py</code>) and this panel populates automatically.';
      container.innerHTML =
        '<div class="card hero hero-empty">' +
        '<p class="hero-kicker">Highest risk ' + noun + '</p>' +
        '<h2>Awaiting live data</h2>' +
        '<p class="muted">' + hint + '</p>' +
        '</div>';
      return;
    }
    var zone = findZone(highest.zone_id);
    var latest = zone && zone.latest ? zone.latest : null;
    var band = FP.bandKey(highest.risk_class);
    var summary = zone ? FP.plainRiskSummary(zone) : null;
    // The spotlight answers "why this city?" in a sentence, and keeps everything else about the
    // reading short: the band, the score, the main factor, how old it is and which way it is
    // going. The model's own output line, the weights and the variable names are deliberately not
    // here - they belong to the city page's technical section and to Data & Methodology.
    var trendWords = { up: 'getting worse', down: 'easing', flat: 'holding steady' };
    var direction = latest ? FP.trendDirection(latest.trend) : 'flat';
    var facts = [];
    if (summary && summary.driverLabel) facts.push('Main risk factor: ' + summary.driverLabel);
    if (latest) facts.push('Updated ' + FP.ageLabel(latest.age_seconds));
    if (latest) facts.push('Trend: ' + (trendWords[direction] || 'holding steady'));
    container.innerHTML =
      '<div class="card hero band-' + band + '">' +
      '<div class="hero-main">' +
      '<p class="hero-kicker">Highest risk ' + noun + '</p>' +
      '<h2 class="hero-name">' + FP.escapeHtml(highest.name) + '</h2>' +
      '<p class="hero-reason" data-f="hero-why">' +
      FP.escapeHtml(summary && summary.why ? summary.why : 'No scored reading has been computed for this ' + noun + ' yet.') +
      '</p>' +
      (facts.length ? '<p class="hero-facts">' + FP.escapeHtml(facts.join(' · ')) + '</p>' : '') +
      '</div>' +
      '<div class="hero-gauge">' +
      '<div class="ring ring-lg band-' + band + '" style="--pct:' + FP.riskPercent(highest.risk_score).toFixed(1) + '">' +
      '<span class="ring-value">' + FP.escapeHtml(FP.formatScore(highest.risk_score)) + '</span>' +
      '<span class="ring-caption">risk score</span>' +
      '</div>' +
      '<span class="pill band-' + band + '">' + FP.escapeHtml(FP.classLabel(highest.risk_class)) + '</span>' +
      '</div>' +
      '</div>';
  }

  /* ------------------------------------------------------------------ cards --- */

  function zoneSignature(zones) {
    return zones.map(function (zone) { return zone.zone_id; }).join('|');
  }

  /* Zone ids go into an attribute selector, so they are escaped as CSS: a quote or a bracket
   * in one (a malformed payload, a hand-edited fixture) would otherwise throw a SyntaxError
   * inside the render pass and stop the board updating. */
  function zoneSelector(zoneId) {
    var value = String(zoneId === null || zoneId === undefined ? '' : zoneId);
    var escaped = (typeof window !== 'undefined' && window.CSS && window.CSS.escape)
      ? window.CSS.escape(value)
      : value.replace(/["\\]/g, '\\$&');
    return '[data-zone="' + escaped + '"]';
  }

  function renderCards(zones) {
    var grid = el.zones;
    if (!grid) return;
    if (grid.getAttribute('data-signature') !== zoneSignature(zones)) {
      grid.innerHTML = zones.map(FP.renderZoneCard).join('');
      grid.setAttribute('data-signature', zoneSignature(zones));
    }
    zones.forEach(function (zone) {
      if (!zone || typeof zone !== 'object') return;
      var card = grid.querySelector(zoneSelector(zone.zone_id));
      if (card) patchCard(card, zone);
    });
  }

  function setBandClass(node, band) {
    FP.BANDS.forEach(function (name) {
      node.classList.remove('band-' + name.toLowerCase());
    });
    node.classList.add('band-' + band);
  }

  function patchCard(card, zone) {
    var latest = zone.latest;
    var band = FP.bandKey(latest ? latest.risk_class : null);
    var setText = function (name, text) {
      var node = card.querySelector('[data-f="' + name + '"]');
      if (node) node.textContent = text;
    };

    setBorderBand(card, latest ? latest.risk_class : null);

    // Provenance follows the payload: switch to the live-weather labels the moment the
    // API reports them, without rebuilding the card.
    var provenance = card.querySelector('[data-f="provenance"]');
    if (provenance) {
      var badge = FP.zoneProvenance(zone);
      if (provenance.innerHTML !== badge) provenance.innerHTML = badge;
    }
    var sourceSet = card.querySelector('[data-f="source-set"]');
    if (sourceSet) {
      var chips = FP.sourceSummary(zone);
      if (sourceSet.innerHTML !== chips) sourceSet.innerHTML = chips;
    }
    FP.METRIC_ORDER.forEach(function (metric) {
      var row = card.querySelector('[data-metric="' + metric + '"]');
      if (!row) return;
      var nameNode = row.querySelector('[data-f="metric-source"]');
      var chip = FP.sourceChip(FP.metricSource(zone, metric));
      if (nameNode && nameNode.innerHTML !== chip) nameNode.innerHTML = chip;
    });

    if (!latest) {
      setText('risk_class', FP.classLabel(null));
      setText('sensor_node_id', 'no node');
      setText('age', 'no data');
      var pill = card.querySelector('[data-f="risk_class"]');
      if (pill) setBandClass(pill, band);
      return;
    }

    setText('risk_score', FP.formatScore(latest.risk_score));
    setText('risk_class', FP.classLabel(latest.risk_class));
    // the human label, never the payload's variable name: this list is a public view
    setText('top_driver', latest.top_drivers && latest.top_drivers.length
      ? FP.driverLabel(latest.top_drivers[0]) : '–');
    setText('sensor_node_id', latest.sensor_node_id);
    setText('age', FP.ageLabel(latest.age_seconds));
    // The plain-language reason is patched like every other value on this card: the card is only
    // rebuilt when the *fleet* changes, so a sentence written once at build time would freeze
    // while the reading behind it moved. The raw model output string is not patched into any
    // visible node: it lives in the city page's technical section, which is redrawn on navigation.
    setText('why', FP.plainRiskSummary(zone).why);

    var trend = card.querySelector('[data-f="trend"]');
    if (trend) {
      trend.textContent = FP.trendLabel(latest.trend);
      trend.setAttribute('data-dir', FP.trendDirection(latest.trend));
    }

    var pill = card.querySelector('[data-f="risk_class"]');
    if (pill) setBandClass(pill, band);

    var ring = card.querySelector('[data-f="ring"]');
    if (ring) {
      ring.style.setProperty('--pct', FP.riskPercent(latest.risk_score).toFixed(1));
      setBandClass(ring, band);
    }

    // A stored reading without a readings map is already reported as "no data" by the API,
    // but a malformed payload must not throw here: the card is patched with dashes instead.
    var readings = (latest && latest.readings) || {};
    FP.METRIC_ORDER.forEach(function (metric) {
      var row = card.querySelector('[data-metric="' + metric + '"]');
      if (!row) return;
      var valueNode = row.querySelector('[data-f="metric-value"]');
      var fillNode = row.querySelector('[data-f="metric-fill"]');
      var scale = FP.metricScale(metric, readings[metric], zone.thresholds || {});
      if (valueNode) valueNode.textContent = FP.formatValueWithUnit(metric, readings[metric]);
      if (fillNode) {
        fillNode.style.setProperty('--pct', (scale.valuePct === null ? 0 : scale.valuePct).toFixed(1));
        fillNode.setAttribute('data-state', scale.state);
      }
    });

    drawSpark(card, zone);
  }

  function setBorderBand(card, riskClass) {
    var band = FP.bandKey(riskClass);
    setBandClass(card, band);
    card.classList.toggle('is-empty', !riskClass);
  }

  /* --------------------------------------------------------------- canvases --- */

  var SPARK_CSS_HEIGHT = 56;

  function drawSpark(card, zone) {
    var canvas = card.querySelector('[data-f="spark"]');
    if (!canvas || !canvas.getContext) return;
    var series = state.series[zone.zone_id] || [];
    var values = series.map(function (point) { return point.risk_score; });
    var ctx = canvas.getContext('2d');
    var dpr = window.devicePixelRatio || 1;
    // Size the backing store from the laid-out width, like the history chart does. The old
    // code drew a dpr-scaled path into the 280x56 attribute-sized bitmap, so on a HiDPI
    // screen the newest samples were drawn past the right edge and silently clipped, and on
    // a 1x screen a 280 px bitmap was stretched across the card. clientWidth is 0 for a
    // canvas that is not laid out (a hidden view), so fall back to the attribute size.
    var width = Math.round(canvas.clientWidth || 0) || Math.round(canvas.width / dpr) || 280;
    var height = SPARK_CSS_HEIGHT;
    var pixelWidth = Math.round(width * dpr);
    var pixelHeight = Math.round(height * dpr);
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
      canvas.style.height = height + 'px';
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (values.length === 0) return;

    ctx.strokeStyle = 'rgba(148, 163, 184, 0.18)';
    ctx.lineWidth = 1;
    BAND_EDGES.forEach(function (edge) {
      var y = height - 6 - edge * (height - 12);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.stroke();
    });

    var shape = FP.sparklinePath(values, width, height, 6);
    if (!shape.path) return;
    var stroke = BAND_STROKE[FP.bandKey(zone.latest ? zone.latest.risk_class : null)] || BAND_STROKE.unknown;
    var gradient = ctx.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, stroke + '66');
    gradient.addColorStop(1, stroke + '00');

    ctx.beginPath();
    ctx.moveTo(shape.points[0].x, height);
    shape.points.forEach(function (point) { ctx.lineTo(point.x, point.y); });
    ctx.lineTo(shape.points[shape.points.length - 1].x, height);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    ctx.moveTo(shape.points[0].x, shape.points[0].y);
    shape.points.slice(1).forEach(function (point) { ctx.lineTo(point.x, point.y); });
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  /* ---------------------------------------------------------------- ranking --- */

  function renderRanking(zones) {
    if (!el.ranking) return;
    var html = (zones && zones.length)
      ? FP.renderRanking(zones)
      : '<li class="muted">waiting for data…</li>';
    if (el.ranking.innerHTML !== html) el.ranking.innerHTML = html;
  }

  /* ---------------------------------------------------------------- history --- */

  function loadHistory() {
    if (!state.selectedZone) return;
    if (state.historyInFlight) return;
    // A failing history route must not be hammered: the 5 s poll and the 30 s ticker would
    // otherwise both retry a broken endpoint. One retry per backoff window instead.
    if (state.historyFailedMs !== null && Date.now() - state.historyFailedMs < HISTORY_RETRY_MS) return;
    var zone = state.selectedZone;
    var minutes = state.historyMinutes;
    var seq = ++state.historySeq;
    state.historyInFlight = true;
    // Say we are fetching before the request goes out: the panel used to keep the previous
    // window's line ("18 points · 1440 min") while showing a chart of a different range.
    state.history = null;
    state.historyError = null;
    state.hoverIndex = null;
    if (el['history-meta']) el['history-meta'].textContent = 'loading the last ' + minutes + ' min…';
    drawHistory();
    var giveUp = setTimeout(function () {
      if (seq !== state.historySeq) return;
      // The request never answered: release the single-flight flag so the backoff can retry,
      // instead of leaving the panel loading forever.
      state.historyInFlight = false;
      state.historyFailedMs = Date.now();
      state.historyError = 'no answer';
      state.history = null;
      drawHistory();
      if (el['history-meta']) {
        el['history-meta'].textContent = 'history timed out — retrying in ' +
          Math.round(HISTORY_RETRY_MS / 1000) + ' s';
      }
    }, FETCH_TIMEOUT_MS);
    getJson('/zones/' + encodeURIComponent(zone) + '/history?minutes=' + minutes)
      .then(function (data) {
        clearTimeout(giveUp);
        if (seq !== state.historySeq) return; // a newer request owns the panel now
        state.historyInFlight = false;
        state.historyFailedMs = null;
        state.historyError = null;
        if (zone !== state.selectedZone || minutes !== state.historyMinutes) return; // stale response
        state.history = data;
        state.historyZone = zone;
        state.historyMinutesLoaded = minutes;
        state.hoverIndex = null;
        drawHistory();
        el['history-meta'].textContent = data.point_count + ' points · ' + data.minutes +
          ' min · ' + (data.downsampled ? 'downsampled (stride ' + data.stride + ')' : 'full resolution') +
          ' · model ' + data.model;
      })
      .catch(function (error) {
        clearTimeout(giveUp);
        if (seq !== state.historySeq) return;
        state.historyInFlight = false;
        state.historyFailedMs = Date.now();
        state.historyError = error.message;
        state.history = null;
        state.historyZone = null;
        state.historyMinutesLoaded = null;
        // Redraw the empty state instead of leaving the previous chart on screen: the canvas
        // used to stay a blank box carrying a "Risk score history chart" label for AT.
        drawHistory();
        el['history-meta'].textContent = 'history unavailable (' + error.message + ') — retrying in ' +
          Math.round(HISTORY_RETRY_MS / 1000) + ' s';
      });
  }

  function renderHistoryButtons(zones) {
    var zoneButtons = zones.map(function (zone) {
      var active = zone.zone_id === state.selectedZone ? ' is-active' : '';
      return '<button type="button" class="ghost' + active + '" data-zone-pick="' + FP.escapeHtml(zone.zone_id) + '">' +
        FP.escapeHtml(zone.name) + '</button>';
    }).join('');
    if (el['history-zone'].getAttribute('data-rendered') !== zoneSignature(zones)) {
      el['history-zone'].innerHTML = zoneButtons;
      el['history-zone'].setAttribute('data-rendered', zoneSignature(zones));
    }
    Array.prototype.forEach.call(el['history-zone'].querySelectorAll('[data-zone-pick]'), function (button) {
      button.classList.toggle('is-active', button.getAttribute('data-zone-pick') === state.selectedZone);
    });

    var ranges = [15, 60, 360];
    if (el['history-range'].getAttribute('data-rendered') !== 'yes') {
      el['history-range'].innerHTML = ranges.map(function (minutes) {
        return '<button type="button" class="ghost" data-range="' + minutes + '">' + minutes + ' min</button>';
      }).join('');
      el['history-range'].setAttribute('data-rendered', 'yes');
    }
    Array.prototype.forEach.call(el['history-range'].querySelectorAll('[data-range]'), function (button) {
      button.classList.toggle('is-active', Number(button.getAttribute('data-range')) === state.historyMinutes);
    });
  }

  function historyAriaLabel(points) {
    var minutes = state.historyMinutes;
    if (state.historyError) return 'Risk score history could not be loaded.';
    if (!points.length) return 'Risk score history for the last ' + minutes + ' min: no readings in this window.';
    var scores = points.map(function (point) { return FP.toNumber(point.risk_score); })
      .filter(function (score) { return score !== null; });
    if (!scores.length) return 'Risk score history for the last ' + minutes + ' min: ' + points.length + ' readings.';
    var lowest = Math.min.apply(null, scores);
    var highest = Math.max.apply(null, scores);
    return 'Risk score history for the last ' + minutes + ' min: ' + points.length + ' readings, from ' +
      FP.clockLabel(points[0].timestamp) + ' to ' + FP.clockLabel(points[points.length - 1].timestamp) +
      '. Lowest ' + lowest.toFixed(2) + ', highest ' + highest.toFixed(2) + '.';
  }

  function drawHistory() {
    var canvas = el['history-canvas'];
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext('2d');
    var dpr = window.devicePixelRatio || 1;
    var cssWidth = Math.round(canvas.clientWidth || 900);
    var cssHeight = 220;
    // Only resize the backing store when it actually changes: assigning canvas.width on
    // every hover repaint forced a synchronous reflow on each mousemove.
    var pixelWidth = Math.round(cssWidth * dpr);
    var pixelHeight = Math.round(cssHeight * dpr);
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
      canvas.style.height = cssHeight + 'px';
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    var padLeft = 42;
    var padRight = 12;
    var padTop = 10;
    var padBottom = 24;
    var plotWidth = cssWidth - padLeft - padRight;
    var plotHeight = cssHeight - padTop - padBottom;

    var yFor = function (score) {
      return padTop + (1 - FP.toNumber(score)) * plotHeight;
    };

    // band background, drawn from the documented heuristic-v1 boundaries
    var edges = [0].concat(BAND_EDGES).concat([1]);
    var names = ['CRITICAL', 'WARNING', 'WATCH', 'NORMAL'];
    for (var i = 0; i < 4; i += 1) {
      ctx.fillStyle = BAND_FILL[names[names.length - 1 - i]];
      ctx.fillRect(padLeft, yFor(edges[i + 1]), plotWidth, yFor(edges[i]) - yFor(edges[i + 1]));
    }

    ctx.strokeStyle = 'rgba(148, 163, 184, 0.35)';
    ctx.fillStyle = 'rgba(148, 163, 184, 0.9)';
    ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
    ctx.lineWidth = 1;
    [0, 0.35, 0.6, 0.8, 1].forEach(function (tick) {
      var y = Math.round(yFor(tick)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(padLeft, y);
      ctx.lineTo(cssWidth - padRight, y);
      ctx.stroke();
      ctx.fillText(tick.toFixed(2), 4, y + 3);
    });

    var data = state.history;
    var points = data && data.points ? data.points : [];
    // The chart is the only place the per-point values live, so its label must describe the
    // series it is actually showing instead of a fixed sentence.
    var seriesLabel = historyAriaLabel(points);
    canvas.setAttribute('aria-label', seriesLabel);
    // ...and the same sentence is on the page itself, because a sighted reader on a small screen
    // cannot hover a phone-sized canvas, and a canvas is invisible to a screen reader.
    setText('history-summary', points.length || state.historyError ? seriesLabel : '');
    if (points.length === 0) {
      ctx.fillStyle = 'rgba(226, 232, 240, 0.7)';
      ctx.font = '13px system-ui, sans-serif';
      ctx.fillText(state.historyError ? 'History could not be loaded.' : 'No history in this window yet.',
        padLeft + 8, padTop + 24);
      return;
    }

    var xFor = function (index) {
      if (points.length === 1) return padLeft + plotWidth / 2;
      return padLeft + (index / (points.length - 1)) * plotWidth;
    };

    ctx.beginPath();
    points.forEach(function (point, index) {
      var x = xFor(index);
      var y = yFor(point.risk_score);
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = '#22d3ee';
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.lineTo(xFor(points.length - 1), padTop + plotHeight);
    ctx.lineTo(xFor(0), padTop + plotHeight);
    ctx.closePath();
    ctx.fillStyle = 'rgba(34, 211, 238, 0.12)';
    ctx.fill();

    points.forEach(function (point, index) {
      var band = FP.bandKey(point.risk_class);
      ctx.beginPath();
      ctx.arc(xFor(index), yFor(point.risk_score), points.length > 120 ? 1.2 : 2.4, 0, Math.PI * 2);
      ctx.fillStyle = BAND_STROKE[band];
      ctx.fill();
    });

    ctx.fillStyle = 'rgba(148, 163, 184, 0.9)';
    ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
    ctx.fillText(FP.clockLabel(points[0].timestamp), padLeft, cssHeight - 6);
    var lastLabel = FP.clockLabel(points[points.length - 1].timestamp);
    ctx.fillText(lastLabel, cssWidth - padRight - ctx.measureText(lastLabel).width, cssHeight - 6);

    if (state.hoverIndex !== null && points[state.hoverIndex]) {
      var hover = points[state.hoverIndex];
      var hx = Math.round(xFor(state.hoverIndex)) + 0.5;
      var hy = Math.round(yFor(hover.risk_score)) + 0.5;
      ctx.strokeStyle = 'rgba(226, 232, 240, 0.5)';
      ctx.beginPath();
      ctx.moveTo(hx, padTop);
      ctx.lineTo(hx, padTop + plotHeight);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(hx, hy, 4, 0, Math.PI * 2);
      ctx.fillStyle = '#e2e8f0';
      ctx.fill();
    }
  }

  /* Keyboard and touch path to the same per-point values the mouse tooltip shows. The chart
   * used to answer `mousemove` only, and the canvas was not focusable, so on a phone or with a
   * keyboard those numbers were unreachable: nothing else on the page exposes them. */
  function hoverIndexAt(event) {
    if (!state.history || !state.history.points || !state.history.points.length) return null;
    // a touch event carries its coordinates in touches[0], a mouse event on clientX
    var clientX = event.clientX;
    if (clientX === undefined || clientX === null) {
      var touch = event.touches && event.touches[0] ? event.touches[0] : null;
      if (!touch) return null;
      clientX = touch.clientX;
    }
    var rect = el['history-canvas'].getBoundingClientRect();
    var plotWidth = rect.width - 42 - 12;
    if (!(plotWidth > 0)) return null;
    var ratio = Math.min(1, Math.max(0, (clientX - rect.left - 42) / plotWidth));
    var count = state.history.points.length;
    return count === 1 ? 0 : Math.round(ratio * (count - 1));
  }

  function showPoint(index) {
    var tooltip = el['history-tooltip'];
    var data = state.history;
    var point = data && data.points ? data.points[index] : null;
    if (!point) {
      tooltip.hidden = true;
      state.hoverIndex = null;
      drawHistory();
      return;
    }
    state.hoverIndex = index;
    var readings = point.readings || {};
    var channels = [
      ['rainfall_mm_per_hr', 'rain'], ['drain_level_m', 'drain'], ['soil_saturation_pct', 'soil'],
      ['water_level_m', 'water'], ['flow_rate_m3_s', 'flow']
    ].map(function (entry) {
      var value = readings[entry[0]];
      if (value === null || value === undefined) return null;
      return entry[1] + ' ' + FP.formatValueWithUnit(entry[0], value);
    }).filter(Boolean);
    tooltip.hidden = false;
    tooltip.innerHTML = '<b>#' + (index + 1) + ' of ' + data.points.length + '</b> ' +
      FP.escapeHtml(FP.clockLabel(point.timestamp)) + ' · score ' +
      FP.escapeHtml(FP.formatScore(point.risk_score)) + ' · ' + FP.escapeHtml(FP.classLabel(point.risk_class)) +
      (channels.length
        ? '<br><span class="muted">' + FP.escapeHtml(channels.join(' · ')) + '</span>'
        : '<br><span class="muted">no sensor readings stored for this point</span>');
    drawHistory();
  }

  function handleHistoryKey(event) {
    var data = state.history;
    if (!data || !data.points || !data.points.length) return;
    var last = data.points.length - 1;
    var index = state.hoverIndex === null ? last : state.hoverIndex;
    if (event.key === 'ArrowRight') index = Math.min(last, index + 1);
    else if (event.key === 'ArrowLeft') index = Math.max(0, index - 1);
    else if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = last;
    else if (event.key === 'Escape') { showPoint(null); return; }
    else return;
    event.preventDefault();
    showPoint(index);
    var canvas = el['history-canvas'];
    var pointLabel = ' Showing point ' + (index + 1) + ' of ' + data.points.length + ': score ' +
      FP.formatScore(data.points[index].risk_score) +
      '. Use the left and right arrow keys to move through the points.';
    if (canvas && canvas.setAttribute) {
      // the label is the only text alternative for a keyboard user, so keep it current
      canvas.setAttribute('aria-label', historyAriaLabel(data.points) + pointLabel);
    }
    // ...and the same sentence on the page, for a reader who cannot see the hover tooltip
    setText('history-summary', historyAriaLabel(data.points) + pointLabel);
  }

  function handleHistoryHover(event) {
    var index = hoverIndexAt(event);
    if (index === null) {
      el['history-tooltip'].hidden = true;
      return;
    }
    showPoint(index);
  }

  /* ===================================================== views and navigation === */

  /* The site is one document with six views. The URL hash is the only source of
   * truth for which one is visible, so a link, a reload or the back button all land in
   * the same place; nothing here fetches, it only decides what is on screen. */
  var VIEWS = ['overview', 'cities', 'city', 'alerts', 'data', 'about'];

  function navigate(hash) {
    if (window.location.hash !== hash) window.location.hash = hash;
    applyRoute({ focus: true });
  }

  /* The mobile menu: a real button with aria-expanded, closed by a second press, by
   * Escape, by choosing a section, or by the viewport growing past the breakpoint. */
  function setMenu(open) {
    var button = el['nav-toggle'];
    var list = el['nav-list'];
    if (!button || !list) return;
    var expanded = !!open;
    state.menuOpen = expanded;
    button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    list.classList.toggle('is-open', expanded);
    document.body.classList.toggle('is-menu-open', expanded);
  }

  function applyRoute(options) {
    var opts = options || {};
    var route = FP.routeFromHash(window.location.hash);
    var viewChanged = route.view !== state.route.view;
    var zoneChanged = route.zoneId !== state.route.zoneId;
    state.route = route;

    VIEWS.forEach(function (view) {
      var section = el['view-' + view];
      if (section) section.hidden = view !== route.view;
      document.body.classList.toggle('view-' + view, view === route.view);
    });

    FP.navState(route).forEach(function (item) {
      var link = el['nav-' + item.view];
      if (!link) return;
      if (item.current) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });

    setMenu(false);
    document.title = FP.viewTitle(route, state.payload);

    if (route.view === 'city' && route.zoneId) {
      state.selectedZone = route.zoneId;
      state.userSelected = true;
      renderHistoryButtons(state.zones);
      if (zoneChanged || !state.history) loadHistory();
    }

    if (viewChanged || zoneChanged) {
      if (route.view === 'city') renderCityDetail();
      if (route.view === 'alerts') renderAlerts();
      if (route.view === 'about') renderAbout(state.payload);
      // the About page carries the same live city list as the rest of the page, so it is
      // rendered from whatever the last poll returned rather than hard-coding five names
      if (route.view === 'about') renderSystemHealth();
      if (opts.focus !== false) focusView();
    }
  }

  /* Moving between views moves the keyboard focus with the content, the way a page
   * navigation would: the next Tab lands inside the view just opened, not in the nav. */
  function focusView() {
    var section = el['view-' + state.route.view];
    if (!section || typeof section.focus !== 'function') return;
    section.focus();
    if (typeof window.scrollTo === 'function') window.scrollTo(0, 0);
  }

  /* ------------------------------------------------------------------ overview --- */

  /* ``metaHtml`` is deliberately raw HTML, not text: callers pass markup (a unit, an inline
   * <code>). Every caller must therefore pass markup it built itself or escaped - never a
   * payload string. Anything from the API goes through ``label`` or ``value``, which are
   * escaped here. */
  function statCard(label, value, metaHtml, tone) {
    return '<article class="stat' + (tone ? ' band-' + tone : '') + '">' +
      '<p class="stat-label">' + FP.escapeHtml(label) + '</p>' +
      '<p class="stat-value">' + FP.escapeHtml(value) + '</p>' +
      '<p class="stat-meta">' + metaHtml + '</p>' +
      '</article>';
  }

  /* The four answers a visitor wants before any table: who is worst, how many alerts,
   * how much of the fleet is reporting, and how fresh the data is. */
  function renderOverview(payload) {
    var ov = FP.fleetOverview(payload, state.incidents, Date.now());
    // the fleet's own noun: "cities" for the live India fleet, "zones" for the simulated one,
    // so the copy never calls a demo zone a city (renderFleetLabels writes the page labels)
    var noun = FP.zoneNoun(payload);
    var plural = FP.zoneNounPlural(payload);
    setText('ov-headline', ov.headline);
    setText('ov-subline', ov.reporting + ' of ' + ov.total + ' ' + plural + ' are reporting a scored reading · updated ' +
      ov.updated + ' · ' + ov.subline);

    if (el['ov-stats']) {
      var leaderValue = ov.leaderName || 'No reading yet';
      var leaderMeta = ov.leaderName
        ? '<span class="pill band-' + FP.bandKey(ov.worst) + '">' + FP.escapeHtml(ov.worst) + '</span>' +
          ' score ' + FP.escapeHtml(ov.leaderScore) +
          // the score is only meaningful with the age of the reading behind it
          (ov.leaderAge === null ? '' : ' · reading ' + FP.escapeHtml(FP.ageLabel(ov.leaderAge))) +
          (ov.leaderStale ? ' <span class="muted">· not reporting recently</span>' : '')
        : 'The first reading will appear here';
      var alertMeta = ov.activeAlerts
        ? 'highest severity ' + FP.escapeHtml(state.incidents && state.incidents.incidents ? (FP.alertsSplit(state.incidents).active[0] || {}).severity || '–' : '–')
        : 'nothing needs attention right now';
      var reportingTone = ov.reporting === 0 ? '' : 'normal';
      var html = statCard('Highest risk ' + noun, leaderValue, leaderMeta, FP.bandKey(ov.worst)) +
        statCard('Active alerts', String(ov.activeAlerts), FP.escapeHtml(alertMeta), ov.activeAlerts ? 'warning' : '') +
        statCard(plural.charAt(0).toUpperCase() + plural.slice(1) + ' reporting', ov.reporting + ' / ' + ov.total,
          'newest stored reading per ' + noun, reportingTone) +
        statCard('Weather freshness', ov.feedValue,
        FP.escapeHtml(ov.freshness || 'waiting for the first provider check'));
      if (el['ov-stats'].getAttribute('data-rendered') !== html) {
        el['ov-stats'].innerHTML = html;
        el['ov-stats'].setAttribute('data-rendered', html);
      }
    }

    renderCityCards(state.zones);
    renderRecentActivity();
  }

  /* "What should I pay attention to?" - the changes the page has actually observed since it was
   * opened, plus the newest incidents. Both come from state the page already holds: nothing here
   * is inferred, and an empty list says so rather than showing a blank panel. */
  function renderRecentActivity() {
    var host = el['recent-activity'];
    if (!host) return;
    var changes = state.changes || [];
    var signature = (state.payload ? 'live' : 'loading') + '|' + changes.join('~') + '|' +
      (state.incidents ? FP.incidentsSignature(state.incidents) : 'none');
    if (signature === state.activitySignature) return;
    state.activitySignature = signature;
    host.innerHTML = FP.recentActivity(state.payload, state.incidents ? state.incidents.incidents : null,
      Date.now(), { changes: changes });
  }

  /* Five compact cards: name, band, one plain sentence, score and freshness. The full
   * detail lives on the city view, one click away. */
  function renderCityCards(zones) {
    var host = el['city-cards'];
    if (!host) return;
    var list = zones || [];
    // The loading/loaded state is part of the signature: an empty list before the first response
    // is a skeleton, and an empty list *after* a response is an empty state, and those two must
    // not hash to the same key (that mistake left the skeleton on screen for ever).
    var signature = (state.payload ? 'loaded' : 'loading') + '|' + (list.map(function (zone) {
      var latest = zone.latest;
      return zone.zone_id + ':' + (latest ? latest.risk_class + ':' + latest.ts_epoch : 'none');
    }).join('|') || 'empty');
    if (signature === state.cityCardSignature) return;
    state.cityCardSignature = signature;
    var noun = FP.zoneNoun(state.payload);
    var stale = FP.staleZoneIds(state.payload);
    host.innerHTML = list.length
      ? list.map(function (zone) {
        return FP.cityStatusCard(zone, { noun: noun, staleZoneIds: stale });
      }).join('')
      : (state.payload
        // the API answered and there is genuinely nothing to show: an empty state with a
        // way forward, not a blank grid
        ? FP.emptyState({
          key: 'no-cities',
          icon: 'info',
          title: 'No ' + noun + ' is reporting a scored reading',
          detail: 'The API answered, but no ' + noun + ' has a complete reading yet. This page keeps ' +
            'checking every few seconds.',
          action: 'retry',
          actionLabel: 'Check again now'
        })
        // nothing has arrived yet: placeholders that show the shape of what is coming
        : FP.cityCardsSkeleton(3));
  }

  /* -------------------------------------------------------------- city detail --- */

  function renderCityDetail() {
    var slot = el['city-detail'];
    if (!slot) return;
    var zoneId = state.route.zoneId;
    var zone = findZone(zoneId);
    setText('city-crumb', zone ? zone.name : (zoneId || 'city'));
    if (!zone) {
      var waiting = !state.payload;
      var noun = FP.zoneNoun(state.payload);
      var nounTitle = noun.charAt(0).toUpperCase() + noun.slice(1);
      var pluralTitle = FP.zoneNounPlural(state.payload);
      pluralTitle = pluralTitle.charAt(0).toUpperCase() + pluralTitle.slice(1);
      var signature = 'missing|' + String(zoneId) + '|' + (waiting ? 'loading' : 'known') + '|' + noun;
      if (signature !== state.detailSignature) {
        state.detailSignature = signature;
        if (waiting) {
          // the shape of the page that is coming, rather than the word "loading"
          slot.innerHTML = '<div class="card detail-missing" aria-hidden="true">' +
            '<span class="skel skel-title"></span>' + FP.skeletonLines(3) + '</div>';
        } else {
          // h2: this card stands in for the city detail view, whose heading is an h2 too
          slot.innerHTML = FP.emptyState({
            key: 'unknown-city',
            icon: 'info',
            title: 'No ' + noun + ' with that name',
            detail: 'The API does not report a ' + noun + ' with the id "' + String(zoneId || '') +
              '", so it is not in the current fleet. It may belong to the simulated demo ' +
              'fleet, or the link may be out of date.',
            actionHref: '#/cities',
            actionLabel: 'Back to all ' + pluralTitle.toLowerCase()
          });
        }
      }
      // the alert history for a city that is not in the fleet is empty, and says so
      renderCityIncidents(null);
      return;
    }
    var latest = zone.latest;
    var signature = zoneId + '|' + (latest ? latest.ts_epoch : 'none') + '|' + (state.payload ? state.payload.generated_at : '');
    if (signature !== state.detailSignature) {
      state.detailSignature = signature;
      var feed = state.payload ? FP.feedStatus(state.payload, Date.now(), payloadAgeOffsetSeconds()) : null;
      slot.innerHTML = FP.cityDetail(zone, { feed: feed, noun: FP.zoneNoun(state.payload) });
    }
    // Outside the signature check: the alert history belongs to whichever city is selected, and
    // switching cities must never leave the previous city's alerts on screen.
    renderCityIncidents(zone);
  }

  /* This city's own alert history, from the incident list the page already holds. */
  function renderCityIncidents(zone) {
    var host = el['city-incidents'];
    if (!host) return;
    var noun = FP.zoneNoun(state.payload);
    var zoneId = zone && zone.zone_id ? zone.zone_id : null;
    var incidents = (state.incidents && state.incidents.incidents) || [];
    var signature = String(zoneId) + '|' + (state.incidents ? 'loaded' : 'loading') + '|' +
      (state.incidents ? FP.incidentsSignature(state.incidents) : 'none');
    if (signature === state.cityIncidentsSignature) return;
    state.cityIncidentsSignature = signature;
    if (!zoneId) {
      host.innerHTML = FP.emptyState({
        key: 'city-history',
        icon: 'info',
        title: 'No alert history to show',
        detail: 'Pick a ' + noun + ' to see the alerts it has had.',
        actionHref: '#/cities',
        actionLabel: 'Back to all ' + FP.zoneNounPlural(state.payload)
      });
      return;
    }
    if (!state.incidents) {
      host.innerHTML = '<div class="card incident-history" aria-hidden="true">' +
        '<span class="skel skel-title"></span>' + FP.skeletonLines(2) + '</div>';
      return;
    }
    host.innerHTML = FP.cityIncidentHistory(incidents, zoneId, Date.now(), { noun: noun, limit: 5 });
  }

  /* The freshness sentence inside the city view moves with the clock, like the one in
   * the snapshot panel; the rest of the panel is only rewritten when data changes. */
  function tickDetailFreshness() {
    var slot = el['city-detail'];
    if (!slot || !state.payload || !slot.querySelector) return;
    var node = slot.querySelector('[data-f="freshness"]');
    if (!node) return;
    var feed = FP.feedStatus(state.payload, Date.now(), payloadAgeOffsetSeconds());
    var line = feed.live ? (FP.freshnessLine(feed) || 'no weather fetched yet') : 'simulator feed';
    if (node.textContent !== line) node.textContent = line;
  }

  /* -------------------------------------------------------------------- alerts --- */

  function renderAlerts() {
    var payload = state.incidents;
    var split = FP.alertsSplit(payload);
    var summary = FP.alertSummary(split);
    // "nothing loaded yet" and "loaded, and there are no incidents" are different states
    // that happen to have the same empty incident list: the loading flag keeps them apart,
    // otherwise the view would stay on the loading placeholder forever.
    var signature = (payload ? 'loaded' : 'loading') + '|' + (FP.incidentsSignature(payload) || 'none') + '|' +
      (state.incidentsError || '') + '|' + (state.payload ? FP.zoneNounPlural(state.payload) : '');
    if (signature !== state.alertsSignature) {
      state.alertsSignature = signature;
      // before the first response the view says so: an empty list must never be shown as
      // "no alerts" when the truth is "not answered yet"
      var loading = !payload;
      // A zone that stopped reporting can leave an incident OPEN for ever (an incident
      // closes on a return to NORMAL, not on silence), so say when that is the case.
      var silentZones = FP.staleZoneIds(state.payload);
      // Every incident row states what happened to its alert, using the API's own alert log
      // count: "published to the notification topic" is only claimed where a publish is on
      // the record, and delivery to a person is never claimed at all.
      var rowOptions = { silentZoneIds: silentZones, alerting: payload ? payload.alerting : null };
      if (el['alerts-active']) {
        el['alerts-active'].innerHTML = loading
          ? FP.alertRowsSkeleton(2)
          : (split.active.length
            ? split.active.map(function (incident) {
              return FP.alertRow(incident, Date.now(), rowOptions);
            }).join('')
            : FP.alertsEmptyState('active', FP.zoneNoun(state.payload)));
      }
      if (el['alerts-closed']) {
        el['alerts-closed'].innerHTML = loading
          ? FP.alertRowsSkeleton(2)
          : (split.closed.length
            ? split.closed.map(function (incident) {
              return FP.alertRow(incident, Date.now(), rowOptions);
            }).join('')
            : FP.alertsEmptyState('closed', FP.zoneNoun(state.payload)));
      }
      if (el['alerts-delivery']) {
        el['alerts-delivery'].innerHTML = loading
          ? '<div class="card"><p class="muted" data-f="loading">alert delivery details will appear here…</p></div>'
          : FP.deliveryPanel(payload);
      }
      if (el['alerts-summary']) {
        var alertsNoun = FP.zoneNoun(state.payload);
        var health = FP.systemHealth(state.payload, payload ? payload.incidents : null, Date.now());
        var html = loading ? '' : statCard('Active alerts', String(split.active.length),
          split.active.length ? '<span class="pill band-' + FP.bandKey(summary.highest) + '">' +
            FP.escapeHtml(summary.highest) + '</span> highest severity'
            : 'no ' + alertsNoun + ' above WATCH', summary.tone === 'calm' ? '' : FP.bandKey(summary.highest)) +
          statCard('Resolved incidents', String(split.closed.length), 'kept as history', '') +
          statCard('Total recorded', String(split.total), 'source <code>GET /incidents</code>', '') +
          // how alert publishing is configured belongs to the delivery panel below, where it can
          // be stated precisely; this card answers the "is anything reporting?" question instead
          statCard(FP.zoneNounPlural(state.payload).charAt(0).toUpperCase() +
            FP.zoneNounPlural(state.payload).slice(1) + ' reporting',
            health.reporting + ' / ' + health.total, 'newest stored reading per ' + alertsNoun,
            health.reporting === health.total && health.total > 0 ? 'normal' : '');
        if (el['alerts-summary'].getAttribute('data-rendered') !== html) {
          el['alerts-summary'].innerHTML = html;
          el['alerts-summary'].setAttribute('data-rendered', html);
        }
      }
    } else {
      tickAlertDurations();
    }

    var lede;
    if (state.incidentsError) {
      lede = 'Alerts unavailable (' + state.incidentsError + ') — showing the last list that loaded.';
    } else if (!payload) {
      lede = 'Loading alerts…';
    } else if (!split.active.length && !split.closed.length) {
      lede = 'Nothing has needed an alert yet. FloodPulse opens one automatically when a ' +
        FP.zoneNoun(state.payload) + ' reaches WARNING or CRITICAL.';
    } else if (!split.active.length) {
      lede = 'No active alert right now. ' + split.closed.length + ' resolved incident' +
        (split.closed.length === 1 ? ' is' : 's are') + ' kept below for the record.';
    } else {
      lede = summary.text + ' — ' + split.active.map(function (incident) {
        return incident.zone_name || incident.zone_id;
      }).join(', ') + '.';
    }
    setText('alerts-lede', lede);
  }

  /* Active alerts keep counting between polls without re-rendering the list. */
  function tickAlertDurations() {
    if (!el['alerts-active'] || !state.incidents) return;
    var nowMs = Date.now();
    Array.prototype.forEach.call(el['alerts-active'].querySelectorAll('[data-incident][data-status="OPEN"]'), function (row) {
      var incidentId = row.getAttribute('data-incident');
      var incident = (state.incidents.incidents || []).filter(function (candidate) {
        return candidate.incident_id === incidentId;
      })[0];
      if (!incident) return;
      var node = row.querySelector('[data-f="duration"]');
      if (node) node.textContent = FP.formatDuration(FP.incidentDurationSeconds(incident, nowMs)) + ' so far';
    });
  }

  /* --------------------------------------------------------- data and method --- */

  /* The whole methodology page is static prose plus the live provenance per metric, so
   * it renders from the helper data even before the first successful poll: this page has
   * to be readable when the API is down, which is exactly when somebody looks at it. */
  function renderDataPage(payload) {
    var metricSources = (payload && payload.metric_sources) || null;
    var signature = (payload ? [payload.model, payload.zone_mode, payload.data_classification].join('|') : 'none');
    if (signature !== state.dataSignature) {
      state.dataSignature = signature;
      var usedBy = function (tag) {
        if (!metricSources) return '';
        var names = FP.METRIC_ORDER.filter(function (metric) { return metricSources[metric] === tag; })
          .map(function (metric) { return (FP.METRIC_META[metric] || { label: metric }).label; });
        if (!names.length) return '<p class="legend-used muted small">Not used in this mode</p>';
        return '<p class="legend-used muted small">Used for: ' + FP.escapeHtml(names.join(', ')) + '</p>';
      };
      if (el['provenance-legend']) {
        el['provenance-legend'].innerHTML = FP.provenanceLegend().map(function (entry) {
          return '<article class="card legend-card src-' + entry.key + '">' +
            '<header class="legend-head"><span class="src-chip src-' + entry.key + '">' + FP.escapeHtml(FP.sourceMeta(entry.tag).label) + '</span>' +
            '<span class="legend-tag"><code>' + FP.escapeHtml(entry.tag) + '</code></span></header>' +
            '<h4>' + FP.escapeHtml(entry.title) + '</h4>' +
            '<p>' + FP.escapeHtml(entry.body) + '</p>' + usedBy(entry.tag) + '</article>';
        }).join('');
      }
      var facts = FP.methodologyFacts();
      if (el['method-block']) {
        el['method-block'].innerHTML =
          '<p>FloodPulse scores every ' + FP.zoneNoun(state.payload) + ' with <code>' + FP.escapeHtml(facts.model) + '</code>, a deterministic hand-tuned heuristic. ' +
          'Nothing is trained, sampled or guessed at request time: the same readings always produce the same score.</p>' +
          '<h4>Step by step</h4><ol class="method-steps">' + facts.steps.map(function (step) {
            return '<li>' + FP.escapeHtml(step) + '</li>';
          }).join('') + '</ol>' +
          '<h4>Fixed weights</h4><ul class="weight-list">' + facts.weights.map(function (weight) {
            return '<li><span class="weight-name">' + FP.escapeHtml((FP.METRIC_META[weight.metric] || { label: weight.metric }).label) + '</span>' +
              '<span class="weight-bar" aria-hidden="true"><i style="--pct:' + parseFloat(weight.weight) + '"></i></span>' +
              '<span class="weight-pct">' + FP.escapeHtml(weight.weight) + '</span></li>';
          }).join('') + '</ul>' +
          '<h4>Risk bands</h4><table class="tech-table"><thead><tr><th scope="col">Class</th>' +
          '<th scope="col">Score</th><th scope="col">What it means</th></tr></thead><tbody>' +
          facts.bands.map(function (band) {
            return '<tr><th scope="row" class="band-' + band.band.toLowerCase() + '">' + FP.escapeHtml(band.band) + '</th>' +
              '<td>' + FP.escapeHtml(band.range) + '</td><td>' + FP.escapeHtml(band.meaning) + '</td></tr>';
          }).join('') + '</tbody></table>';
      }
      if (el['why-simulated']) setText('why-simulated', facts.whySimulated);
      if (el['limitations']) {
        el['limitations'].innerHTML = facts.limitations.map(function (limitation) {
          return '<li>' + FP.escapeHtml(limitation) + '</li>';
        }).join('');
      }
    }
    // The exact arithmetic of one real reading, folded away: which variable carried which weight,
      // what each contributed, and the model's own output line - the transparency that used to sit
      // on the Overview, in the place where a reader goes looking for it.
    if (el['score-breakdown']) {
      var leader = null;
      (state.zones || []).forEach(function (zone) {
        if (!zone || !zone.latest) return;
        if (!leader || FP.riskPercent(zone.latest.risk_score) > FP.riskPercent(leader.latest.risk_score)) leader = zone;
      });
      var breakdownSignature = leader
        ? leader.zone_id + '|' + leader.latest.risk_score + '|' + (leader.latest.sequence === undefined ? '' : leader.latest.sequence)
        : 'none';
      if (breakdownSignature !== state.breakdownSignature) {
        state.breakdownSignature = breakdownSignature;
        el['score-breakdown'].innerHTML = leader
          ? '<details class="tech" data-f="data-breakdown">' +
            '<summary>Worked example: the highest-risk reading, number by number</summary>' +
            '<p class="muted small">Every figure below is a value this page received from the API: the ' +
            'measurement, its own reading, the fixed weight of that channel, and the weighted ' +
            'contribution it made to this particular score.</p>' +
            FP.scoreBreakdown(leader) +
            '<p class="raw-label">Raw model output, as returned by the API</p>' +
            '<p class="raw-model"><code>' + FP.escapeHtml(leader.latest.explanation || '') + '</code></p>' +
            '</details>'
          : FP.emptyState({
            key: 'no-breakdown',
            icon: 'info',
            title: 'No scored reading to show yet',
            detail: 'The worked example fills in as soon as the first reading is computed.'
          });
      }
    }
    if (el['attribution-detail'] && payload) {
      var attribution = FP.attributionLine(payload);
      setText('attribution-detail', attribution.text);
      setText('summary-weather-provider', attribution.provider);
      // no provider data on this board means no attribution card to show
      if (el['attribution-block']) el['attribution-block'].hidden = !attribution.text;
      if (attribution.docs) {
        var link = el['attribution-block'].querySelector('[data-f="attribution-docs"]');
        if (link) {
          link.setAttribute('href', attribution.docs);
          link.textContent = attribution.docs;
        }
      }
    }
  }

  /* --------------------------------------------------------- system health --- */

  /* One honest sentence about right now: how much of the fleet is answering, how old the newest
   * weather observation is, whether anything is stale, and whether any alert is open. Counts and
   * ages only - no infrastructure detail, no topic, no region, nothing an attacker can use. */
  function renderSystemHealth() {
    var health = FP.systemHealth(state.payload, state.incidents ? state.incidents.incidents : null, Date.now());
    var signature = health.tone + '|' + health.reporting + '|' + health.total + '|' +
      health.lines.map(function (line) { return line.label; }).join('~') + '|' +
      (state.lastError ? 'error' : 'ok') + '|' + (state.payload ? 'loaded' : 'loading');
    if (signature === state.healthSignature) return;
    state.healthSignature = signature;

    var tone = state.lastError ? 'bad' : (state.payload ? health.tone : 'quiet');
    var word = state.lastError ? 'Degraded' : (state.payload ? health.label : 'Connecting');
    var detail = state.lastError
      ? 'the API did not answer the last request'
      : (state.payload ? health.reporting + '/' + health.total + ' reporting' : 'waiting for the first response');

    var pill = el['health-pill'];
    if (pill) {
      pill.className = 'health-summary is-' + tone;
    }
    var dot = el['health-dot'];
    if (dot) dot.className = 'health-dot is-' + tone;
    setText('health-word', word);
    setText('health-detail', detail);

    var list = el['health-list'];
    if (list) {
      var lines = health.lines.slice();
      if (state.lastError) {
        lines.push({ icon: 'warning', tone: 'bad', label: 'The last API request failed' });
      }
      lines.push({
        icon: 'refresh',
        tone: 'quiet',
        label: state.payload
          ? 'Last successful update ' + FP.ageLabel(Math.round((Date.now() - state.payloadReceivedMs) / 1000))
          : 'No successful update yet'
      });
      list.innerHTML = lines.map(function (line) {
        return '<li class="health-item is-' + FP.escapeHtml(line.tone) + '">' +
          FP.icon(line.icon, 16) + '<span>' + FP.escapeHtml(line.label) + '</span></li>';
      }).join('');
    }

    var grid = el['alerts-status'];
    if (grid) {
      grid.innerHTML = health.lines.map(function (line) {
        return '<article class="status-card is-' + FP.escapeHtml(line.tone) + '">' +
          '<span class="status-icon">' + FP.icon(line.icon, 20) + '</span>' +
          '<p class="status-label">' + FP.escapeHtml(line.label) + '</p></article>';
      }).join('');
    }
  }

  /* ---------------------------------------------------------------- about --- */

  /* The About page repeats two live facts - which cities are monitored, and what the model does
   * not do - so it is rendered from the payload rather than hard-coded, and stays true if the
   * fleet changes. Everything else on that page is static prose in index.html. */
  function renderAbout(payload) {
    var names = state.zones.map(function (zone) { return zone.name; }).filter(Boolean);
    var signature = (payload ? 'loaded' : 'loading') + '|' + names.join(',') + '|' +
      (payload ? payload.zone_mode : '');
    if (signature === state.aboutSignature) return;
    state.aboutSignature = signature;

    if (el['about-cities']) {
      el['about-cities'].innerHTML = names.length
        ? 'FloodPulse currently monitors <b>' + names.length + '</b> ' + FP.zoneNounPlural(payload) + ': ' +
          FP.escapeHtml(names.join(', ')) + '. ' +
          (payload && payload.zone_mode === 'live_weather'
            ? 'Rainfall for each of them comes from the live weather provider.'
            : 'This board is running in simulated mode: every value is generated by the FloodPulse simulator.')
        : 'Waiting for the first response from <code>GET /zones</code> to list the monitored ' +
          FP.zoneNounPlural(payload) + '.';
    }
    if (el['about-limitations']) {
      el['about-limitations'].innerHTML = FP.methodologyFacts().limitations.map(function (limitation) {
        return '<li>' + FP.escapeHtml(limitation) + '</li>';
      }).join('');
    }
  }

  /* ----------------------------------------------------------------- wiring --- */

  /* ------------------------------------------------------------- incidents --- */

  /* The incident log is read-only and comes straight from GET /incidents: this page
   * never invents or edits an incident, and the SNS topic is never exposed to it. */
  function loadIncidents() {
    if (state.incidentsInFlight) return; // one request at a time, whoever asked for it
    var seq = ++state.incidentsSeq;
    state.incidentsInFlight = true;
    state.incidentsMs = Date.now();
    var giveUp = setTimeout(function () {
      if (state.incidentsAppliedSeq >= seq) return;
      // Release the single-flight flag so the 10 s ticker can try again, and say why the
      // panel is not updating rather than leaving it silently frozen.
      state.incidentsInFlight = false;
      state.incidentsError = 'no answer within ' + Math.round(FETCH_TIMEOUT_MS / 1000) + ' s';
      renderIncidentsPanel();
    }, FETCH_TIMEOUT_MS);
    getJson('/incidents?limit=' + INCIDENTS_LIMIT).then(function (payload) {
      clearTimeout(giveUp);
      if (markedStale(seq, state.incidentsAppliedSeq)) return; // an older reply: drop it
      state.incidentsAppliedSeq = seq;
      state.incidentsInFlight = false;
      // Same rule as the zone list: an element that is not an object is not an incident.
      state.incidents = payload
        ? Object.assign({}, payload, { incidents: usableEntries(payload.incidents) })
        : payload;
      state.incidentsError = null;
      renderIncidentsPanel();
    }).catch(function (error) {
      clearTimeout(giveUp);
      if (state.incidentsAppliedSeq >= seq) return;
      state.incidentsInFlight = false;
      state.incidentsError = error.message;
      // keep the last good list rather than blanking the panel or inventing history
      renderIncidentsPanel();
    });
  }

  function renderIncidentsPanel() {
    if (!el.incidents) return;
    var payload = state.incidents;
    var signature = FP.incidentsSignature(payload);
    if (signature !== state.incidentsSignature) {
      state.incidentsSignature = signature;
      el.incidents.innerHTML = FP.renderIncidents(payload, Date.now(), {
        noun: FP.zoneNoun(state.payload)
      });
    } else {
      tickIncidentDurations();
    }
    if (el['incidents-meta']) {
      el['incidents-meta'].textContent = state.incidentsError
        ? 'incidents unavailable (' + state.incidentsError + ') — showing the last loaded list'
        : FP.incidentsMeta(payload);
    }
    if (el['incidents-alerting']) {
      el['incidents-alerting'].textContent = FP.alertingNote(payload ? payload.alerting : null);
      el['incidents-alerting'].setAttribute(
        'data-alerting',
        payload && payload.alerting && payload.alerting.enabled ? 'armed' : 'off'
      );
    }
    // the alerts view reads the same payload, split into active and resolved
    renderAlerts();
    // the system status includes the active-alert count, so it moves with the incidents
    renderSystemHealth();
  }

  /* Open incidents keep counting between polls; only the duration text is patched so
     the rest of the list (and any text selection) is left alone. */
  function tickIncidentDurations() {
    if (!el.incidents || !state.incidents) return;
    var rows = el.incidents.querySelectorAll('[data-incident][data-status="OPEN"]');
    var nowMs = Date.now();
    Array.prototype.forEach.call(rows, function (row) {
      var incidentId = row.getAttribute('data-incident');
      var incident = (state.incidents.incidents || []).filter(function (candidate) {
        return candidate.incident_id === incidentId;
      })[0];
      if (!incident) return;
      var node = row.querySelector('[data-f="duration"]');
      if (node) {
        node.textContent = FP.formatDuration(FP.incidentDurationSeconds(incident, nowMs)) + ' and counting';
      }
    });
  }

  function bind() {
    // ---- navigation ------------------------------------------------------------------
    // The links are real anchors, so a click, a paste of the URL or the back button all
    // work; these handlers only make the switch immediate and announce it accessibly.
    function followNav(event) {
      var link = event.target && event.target.closest ? event.target.closest('[data-nav]') : null;
      if (!link) return false;
      event.preventDefault();
      navigate(FP.hashFor(link.getAttribute('data-nav')));
      return true;
    }

    // Choosing a city from the Overview cards or the city switcher opens its city view.
    function followCity(event) {
      var card = event.target && event.target.closest ? event.target.closest('[data-zone]') : null;
      if (!card) return false;
      var zoneId = card.getAttribute('data-zone');
      if (!zoneId) return false;
      event.preventDefault();
      navigate(FP.hashFor('city', zoneId));
      return true;
    }

    if (el['nav-list']) {
      bindTo('nav-list', 'click', function (event) {
        followNav(event);
      });
    }
    if (el['nav-toggle']) {
      bindTo('nav-toggle', 'click', function (event) {
        event.preventDefault();
        setMenu(!state.menuOpen);
      });
    }
    if (el['city-cards']) {
      bindTo('city-cards', 'click', function (event) {
        followCity(event);
      });
    }
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && state.menuOpen) setMenu(false);
    });
    window.addEventListener('hashchange', function () {
      applyRoute({ focus: true });
    });

    // A "check again" button in any empty or error state: a real retry of the two read routes,
    // not a page reload, so the reader keeps their place and their scroll position.
    document.addEventListener('click', function (event) {
      var button = event.target && event.target.closest ? event.target.closest('[data-action="retry"]') : null;
      if (!button) return;
      event.preventDefault();
      if (button.disabled) return;
      button.disabled = true;
      setText('conn-detail', 'checking the API again…');
      state.lastError = false;
      state.incidentsFailedMs = null;
      poll();
      loadIncidents();
      // every renderer is signature-guarded, so the retry has to defeat those guards explicitly
      state.cityCardSignature = null;
      state.activitySignature = null;
      state.healthSignature = null;
      state.breakdownSignature = null;
      state.detailSignature = null;
      renderCityCards(state.zones);
      renderSystemHealth();
      setTimeout(function () { button.disabled = false; }, 1200);
    });

    bindTo('history-zone', 'click', function (event) {
      var button = event.target.closest('[data-zone-pick]');
      if (!button) return;
      var picked = button.getAttribute('data-zone-pick');
      state.selectedZone = picked;
      state.userSelected = true;
      state.history = null;
      state.historyFailedMs = null; // an explicit click always retries immediately
      renderHistoryButtons(state.zones);
      // keep the URL on the city that is actually on screen; the browser then fires
      // hashchange, which re-renders the detail panel for the same city
      if (window.location.hash !== FP.hashFor('city', picked)) {
        window.location.hash = FP.hashFor('city', picked);
      }
      loadHistory();
    });

    bindTo('history-range', 'click', function (event) {
      var button = event.target.closest('[data-range]');
      if (!button) return;
      state.historyMinutes = Number(button.getAttribute('data-range'));
      state.history = null;
      state.historyFailedMs = null;
      renderHistoryButtons(state.zones);
      loadHistory();
    });

    // The chart answers a mouse, a keyboard and a fingertip: the per-point values exist
    // nowhere else on the page, so a pointer-only tooltip hides data from a whole class of
    // people (a phone user, someone who cannot use a mouse).
    bindTo('history-canvas', 'mousemove', handleHistoryHover);
    bindTo('history-canvas', 'keydown', handleHistoryKey);
    bindTo('history-canvas', 'touchstart', handleHistoryHover, { passive: true });
    bindTo('history-canvas', 'touchmove', handleHistoryHover, { passive: true });
    function clearHistoryHover() {
      state.hoverIndex = null;
      if (el['history-tooltip']) el['history-tooltip'].hidden = true;
      drawHistory();
    }
    bindTo('history-canvas', 'mouseleave', clearHistoryHover);
    bindTo('history-canvas', 'touchend', clearHistoryHover);
    bindTo('history-canvas', 'blur', function () {
      if (el['history-tooltip'] && !el['history-tooltip'].hidden) clearHistoryHover();
    });

    function redrawCharts() {
      drawHistory();
      if (!el.zones) return;
      state.zones.forEach(function (zone) {
        if (!zone || typeof zone !== 'object') return;
        var card = el.zones.querySelector(zoneSelector(zone.zone_id));
        if (card) drawSpark(card, zone);
      });
    }

    /* Dragging a window edge fires resize dozens of times a second and each redraw resizes two
     * canvases per card, which reallocates their backing stores. Coalescing to one redraw per
     * frame keeps the last size and drops the events the browser could not paint anyway. */
    var resizeQueued = false;
    function queueResizeRedraw() {
      if (resizeQueued) return;
      resizeQueued = true;
      var schedule = window.requestAnimationFrame
        ? function (fn) { return window.requestAnimationFrame(fn); }
        : function (fn) { return setTimeout(fn, 16); };
      schedule(function () {
        resizeQueued = false;
        redrawCharts();
      });
    }

    window.addEventListener('resize', queueResizeRedraw);

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) {
        poll();
        loadHistory();
        loadIncidents();
      }
    });
  }

  function start() {
    cacheDom();
    bind();
    // The route decides which of the six views is visible, before any data arrives.
    applyRoute({ focus: false });
    renderDataPage(null);
    // The three slow panels start as skeletons rather than as sentences, so the first paint
    // already shows the shape of what is coming (and the About/status panels get their static
    // contents immediately, since they must be readable even if the API never answers).
    renderCityCards([]);
    renderAbout(null);
    renderSystemHealth();
    if (!API_BASE) {
      // Say why the page will never fill in, in the same status line a failing poll uses:
      // this used to leave the badge on CONNECTING and five panels on "waiting for data…"
      // forever, because start() returned before anything could update them.
      if (el['conn-badge']) el['conn-badge'].className = 'conn-badge conn-disconnected';
      setText('conn-badge', 'DISCONNECTED');
      setText('conn-detail', 'no API base configured — add ?api=https://… to the address');
      setText('conn-announce',
        'no API base configured. Add the api parameter to the address to connect this page to a FloodPulse API.');
      state.connAnnounceKey = 'no-api-base';
      document.body.classList.add('is-disconnected');
      setText('summary-freshness', 'not connected');
      return;
    }
    refreshConnection();
    poll();
    loadIncidents();
    setInterval(function () {
      if (!document.hidden) poll();
    }, POLL_MS);
    setInterval(function () {
      if (!document.hidden) loadHistory();
    }, HISTORY_REFRESH_MS);
    setInterval(function () {
      if (!document.hidden) loadIncidents();
    }, INCIDENTS_REFRESH_MS);
    setInterval(refreshConnection, 1000);
    setInterval(tickIncidentDurations, 1000);
    setInterval(function () {
      var total = Object.keys(state.series).reduce(function (sum, key) { return sum + state.series[key].length; }, 0);
      el['poll-count'].textContent = total + ' live samples buffered';
    }, POLL_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
