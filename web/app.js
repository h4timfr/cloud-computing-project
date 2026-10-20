/* FloodPulse live dashboard controller.
 */
(function () {
  'use strict';

  var FP = window.FP;

  var params = new URLSearchParams(window.location.search);

  /* The page states its own API base in a data attribute, not in a script tag: the deployed
   * page carries it in a data attribute, so the same file runs locally and deployed. */
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
   * be wrong, so it is only accepted when it is a plain http(s) origin. */
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
  var BAND_EDGES = FP.SCORE_BAND_EDGES; // heuristic-v1 band boundaries, defined once in lib.js
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
    historyWindow: 60,
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
    // the slice of the loaded series the panel is showing, and its numbers: the arrow keys and
    // the tooltip read these, so they always describe what is on screen
    windowPoints: [],
    windowSummary: null,
    historyInFlight: false,
    // which city the request in flight belongs to: a switch to another city starts its own request
    historyLoadingZone: null,
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
    zonesSeq: 0,
    zonesAppliedSeq: 0,
    incidentsSeq: 0,
    incidentsAppliedSeq: 0,
    historySeq: 0,
    // which view the URL asks for, and what each view currently shows (so a poll that
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
    // M10: the alert preview on the Overview, and the state of the subscription form
    // ('idle' before anything is attempted, then one of the four outcomes)
    alertPreviewSignature: null,
    subscribe: 'idle',
    subscribeInFlight: false,
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

  /* The coverage map. Positions come from the payload's latitude and longitude and from nothing else, so
     neither the grid's sort order nor the DOM order can move a marker. Emphasis and the panel reuse the
     grid's own visible[] list, the payload's highest_risk_zone and stale_zone_ids. */
  function renderMap(visible) {
    var host = el['map-markers'];
    if (!host) return;
    var zones = state.zones || [];
    var payload = state.payload;
    var ready = Boolean(payload && zones.length);
    bindMapEvents(host);
    if (el['map-fallback']) el['map-fallback'].hidden = ready;
    state.mapVisible = visible;
    if (!ready) {
      host.innerHTML = '';
      host.removeAttribute('data-sig');
      state.mapPick = null;
      state.mapById = {};
      setMapDetail('');
      return;
    }
    var stale = {};
    var keep = {};
    var byId = {};
    (payload.stale_zone_ids || []).forEach(function (id) { stale[id] = true; });
    visible.forEach(function (zone) { if (zone) keep[zone.zone_id] = true; });
    zones.forEach(function (zone) { byId[zone.zone_id] = zone; });
    var top = payload.highest_risk_zone ? payload.highest_risk_zone.zone_id : '';
    var sig = zones.map(function (zone) {
      return zone.zone_id + (zone.latest ? FP.bandKey(zone.latest.risk_class) : '-') +
        (stale[zone.zone_id] || !zone.latest ? '~' : '') + (keep[zone.zone_id] ? '+' : '-');
    }).join('|') + top;
    if (host.getAttribute('data-sig') !== sig) {
      host.innerHTML = zones.map(function (zone) {
        return FP.renderMapMarker(zone, { stale: Boolean(stale[zone.zone_id] || !zone.latest),
          top: zone.zone_id === top, matches: Boolean(keep[zone.zone_id]) });
      }).join('');
      host.setAttribute('data-sig', sig);
    }
    state.mapStale = stale;
    state.mapKeep = keep;
    state.mapById = byId;
    state.mapTop = top;
    showMapPick(byId[state.mapPick] ? state.mapPick : null);
  }

  function bindMapEvents(host) {
    if (state.mapBound) return;
    state.mapBound = true;
    host.addEventListener('focusin', pickMapZone);
    host.addEventListener('pointerover', pickMapZone);
    host.addEventListener('focusout', dropMapZone);
    host.addEventListener('pointerout', dropMapZone);
  }

  function pickMapZone(event) {
    var link = event.target && event.target.closest ? event.target.closest('.map-marker') : null;
    if (link) showMapPick(link.getAttribute('data-zone'));
  }

  function dropMapZone(event) {
    var to = event.relatedTarget;
    if (!to || !to.closest || !to.closest('.map-markers')) showMapPick(null);
  }

  /* Repaints the panel and the selection ring only: a pointer crossing the map must not rebuild the
     links under it, and must not move focus. */
  function showMapPick(zoneId) {
    var zone = (state.mapById || {})[zoneId] || mapDefault();
    state.mapPick = zone ? zone.zone_id : null;
    var host = el['map-markers'];
    var was = host ? host.querySelector('.map-marker.is-selected') : null;
    if (was) was.classList.remove('is-selected');
    var now = zone && host ? host.querySelector('[data-zone="' + zone.zone_id + '"]') : null;
    if (now) now.classList.add('is-selected');
    setMapDetail(zone ? FP.renderMapDetail(zone, {
      stale: Boolean(state.mapStale[zone.zone_id] || !zone.latest),
      top: zone.zone_id === state.mapTop
    }) : '');
  }

  /* The fleet's own highest-risk zone when the filters kept it, else the best score still visible.
     Deliberately not "the first card": the grid's sort must not decide what the map shows. */
  function mapDefault() {
    var pool = (state.mapVisible || []).filter(function (zone) { return zone && zone.latest; });
    var flagged = (state.mapById || {})[state.mapTop];
    if (flagged && (!pool.length || state.mapKeep[flagged.zone_id])) return flagged;
    var best = pool.length ? pool[0] : null;
    pool.forEach(function (zone) {
      if (Number(zone.latest.risk_score) > Number(best.latest.risk_score)) best = zone;
    });
    return best;
  }

  function setMapDetail(html) {
    if (el['map-detail'] && el['map-detail'].innerHTML !== html) el['map-detail'].innerHTML = html;
    var desc = el['map-svg-desc'];
    if (!desc) return;
    var line = 'Schematic coastline. City risk data is temporarily unavailable.';
    if (state.payload) {
      var top = state.payload.highest_risk_zone;
      line = 'Schematic coastline and ' + (state.zones || []).length + ' monitored city markers' +
        (top && top.city ? '; highest monitored risk: ' + top.city : '') + '.';
    }
    if (desc.textContent !== line) desc.textContent = line;
  }

  function cacheDom() {
    var ids = [
      'conn-badge', 'conn-detail', 'conn-announce', 'last-update', 'api-base', 'hero', 'ranking', 'zones',
      'history-zone', 'history-range', 'history-canvas', 'history-meta', 'history-tooltip',
      'history-facts', 'history-window-note', 'rain-chart', 'rain-summary', 'history-panel',
      'city-context',
      'notice-text', 'summary-live', 'summary-model', 'summary-generated', 'poll-count',
      'incidents', 'incidents-meta', 'incidents-alerting',
      'data-banner', 'banner-title', 'banner-detail', 'summary-mode', 'summary-weather', 'attribution',
      'brand-subtitle', 'summary-freshness', 'footer-disclaimer', 'ranking-title', 'zones-title',
      'change-note',
      // fleet distribution strip: renderBandStrip() and bandStripLabel() are guards on
      'band-strip', 'band-strip-label',
      // The coverage map: marker pool, selected-city panel, fallback line, SVG description.
      'coast-map', 'map-markers', 'map-detail', 'map-fallback', 'map-svg-desc',
      // navigation and the views (M6 information architecture, extended with About in M9)
      'nav-toggle', 'nav-list', 'nav-overview', 'nav-cities', 'nav-alerts', 'nav-data', 'nav-about',
      'view-overview', 'view-cities', 'view-city', 'view-alerts', 'view-data', 'view-about',
      'ov-headline', 'ov-subline', 'ov-intro', 'ov-stats', 'band-strip-title',
    // M13: the overview's scope note, filled from the payload's coverage block
    'why-cities-note',
      'city-cards-title', 'cities-lede', 'incidents-zone-header', 'city-cards', 'city-detail', 'city-crumb',
      // M11: the Cities page's find-and-sort controls and the Overview's coverage note
      'cities-search', 'cities-coast', 'cities-coast-label', 'cities-coast-field', 'cities-risk',
      'cities-sort', 'cities-count', 'cities-empty', 'city-cards-note', 'city-cards-more',
      'alerts-lede', 'alerts-summary', 'alerts-active', 'alerts-closed',
      'provenance-legend', 'attribution-block', 'attribution-detail', 'summary-weather-provider',
      'method-block', 'why-simulated', 'limitations', 'summary-model-data', 'score-breakdown',
      // M9: system health, recent activity, alert delivery, per-city alert history and About
      'health', 'health-pill', 'health-dot', 'health-word', 'health-detail', 'health-list',
      'recent-activity', 'alerts-status', 'alerts-delivery', 'city-incidents',
      'about-cities', 'about-limitations', 'history-summary',
      // M10: the hero's own fields, the alert preview on the Overview, and the subscription form
      'hero-monitor', 'hero-why', 'hero-facts', 'hero-state',
      'alert-preview', 'alert-preview-title',
      'subscribe-form', 'subscribe-email', 'subscribe-button', 'subscribe-result', 'subscribe-note'
    ];
    ids.forEach(function (id) { el[id] = $(id); });
    // A page and a controller from different revisions (a half-finished deploy, a hand-edited
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
    setText('city-cards-title', Singular + ' flood risk');
    setText('cities-lede', 'Every monitored ' + singular + ' with its current risk level. ' +
      'Open one for the full picture.');
    setText('incidents-zone-header', Singular);
    // the section names ("Cities", the nav label, the window title) are part of the
    // information architecture and stay fixed; only sentences about the data follow the data
  }

  /* A payload element that is not an object (null, a string, a number) is not a zone and not an
   * incident, so it is dropped before anything tries to read a field from it. */
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
    renderCitiesPage();
    renderRanking(state.zones);
    renderZonePicker(state.zones);
    // every view is rendered on every successful poll, not only the visible one: the
    // sections are small, and switching a view must never show a stale answer
    renderOverview(payload);
    renderDataPage(payload);
    renderAlerts();
    renderAbout(payload);
    renderSystemHealth();
    if (state.route.view === 'city') renderCityDetail();
    refreshConnection();

    // One request per city view: the API may re-point the default zone at a new highest-risk zone, and
    // that is the only case a poll has to fetch again. The four window controls slice what is in memory.
    if (state.historyZone !== state.selectedZone) loadHistory();
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
   * claiming attention on every subsequent poll. */
  function renderChangeNote(payload) {
    var note = el['change-note'];
    var change = FP.fleetChange(state.previousClasses, payload);
    state.previousClasses = FP.fleetClasses(payload);
    // Keep the last few observed changes, newest first, for the Overview activity panel. These
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
  }

  /* Data freshness is its own panel row, not a footnote: it is the difference between
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
    var zones = usableEntries(payload && payload.zones);
    var names = zones.slice(0, 5).map(function (zone) { return zone.name; });
    var suffix = zones.length > names.length ? ' +' + (zones.length - names.length) : '';
    if (feed.live) {
      var coasts = FP.coverageInfo(payload);
      var where = coasts && coasts.coastCount !== null
        ? ' in ' + coasts.coastCount + (coasts.coastCount === 1 ? ' coast group' : ' coast groups') : '';
      // The status strip names the product and the fleet size. It deliberately does not say
      // "India": the fleet is a selected set of coastal cities, not national coverage.
      return 'FloodPulse Coastal India · ' + (feed.zoneCount || zones.length) +
        ' coastal cities' + where + ' · ' + names.join(', ') + suffix;
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
    if (state.connAnnounceKey !== info.announceKey) {
      state.connAnnounceKey = info.announceKey;
      setText('conn-announce', info.announcement);
    }
    if (state.lastSuccessMs) {
      setText('last-update', FP.clockLabel(new Date(state.lastSuccessMs).toISOString()));
    }
    // the feed countdown ticks here, once per second, on the panel row only
    if (state.payload) renderFreshness(state.payload);
    // Before the first successful poll the hero holds the copy index.html ships with. Once the
    // first poll fails, the hero says so in the page's own words.
    if (!state.payload && state.lastError) {
      setText('ov-headline', 'Awaiting live data');
      setText('hero-monitor', 'The FloodPulse API has not answered this page yet, so no reading ' +
        'is shown. The board fills in by itself as soon as a request succeeds — nothing to reload.');
      setText('hero-why', '');
      setText('hero-facts', '');
      setText('hero-state', '');
    }
    if (state.route.view === 'city') tickDetailFreshness();
    // The health pill and the chrome around the connection badge change with the connection
    // state, so they are refreshed on the same one-second tick (and are signature-guarded).
    renderSystemHealth();
  }

  /* ------------------------------------------------------------------- hero --- */

  /* The hero: the fleet's answer, then the reason behind the highest-risk city.
   * It is one section in index.html, patched in place rather than rebuilt. */
  function renderHero(payload) {
    var highest = payload.highest_risk_zone;
    var feed = FP.feedStatus(payload, Date.now());
    var noun = FP.zoneNoun(payload);
    var card = el.hero;
    if (!card) return;

    var emptyHint = feed.live
      ? 'No city reading has been computed yet. The live fleet fetches rainfall and soil ' +
        'moisture from ' + (feed.provider || 'the weather provider') +
        (feed.refreshMinutes ? ' every ' + feed.refreshMinutes + ' minutes' : '') +
        ' and this panel fills in as soon as the first refresh is ingested.'
      : 'No simulated reading has been ingested yet. Start the simulator and this panel ' +
        'populates automatically.';
    if (!highest) {
      card.setAttribute('data-band', 'unknown');
      setText('ov-headline', 'Awaiting live data');
      setText('hero-monitor', emptyHint);
      setText('hero-why', '');
      setText('hero-facts', '');
      setText('hero-state', '');
      return;
    }

    var zone = findZone(highest.zone_id);
    var latest = zone && zone.latest ? zone.latest : null;
    var band = FP.bandKey(highest.risk_class);
    var summary = zone ? FP.plainRiskSummary(zone) : null;
    var headed = FP.fleetHeadline(FP.classLabel(highest.risk_class),
      FP.bandCounts(payload), (payload.zones || []).length, FP.zoneNounPlural(payload));
    card.setAttribute('data-band', band);
    FP.BANDS.forEach(function (name) {
      card.classList.remove('band-' + name.toLowerCase());
    });
    card.classList.add('band-' + band);
    setText('ov-headline', headed);
    setText('hero-monitor', FP.monitorLine(payload, FP.fleetOverview(payload, state.incidents, Date.now())));
    // The fleet's scope, from the API's own coverage block, and unconditional: a page must not
    // state a wider or narrower fleet than the payload it is showing, whatever the risk is today.
    setText('why-cities-note', FP.fleetScopeNote(payload));
    setText('hero-why', summary && summary.why ? summary.why : '');
    // The facts line answers "how bad, why, how fresh, which way" in one narrow column: it is
    // not a second hero, so it never repeats the headline sentence.
    var direction = latest ? FP.trendDirection(latest.trend) : 'flat';
    var trendWords = { up: 'getting worse', down: 'easing', flat: 'holding steady' };
    var score = highest.risk_score !== undefined && highest.risk_score !== null
      ? highest.risk_score : (latest ? latest.risk_score : null);
    var age = highest.age_seconds !== undefined && highest.age_seconds !== null
      ? highest.age_seconds : (latest ? latest.age_seconds : null);
    var facts = [];
    if (summary && summary.driverLabel) facts.push('Main risk factor: ' + summary.driverLabel);
    if (score !== null) facts.push('Risk score ' + FP.formatScore(score));
    if (age !== null) facts.push('Updated ' + FP.ageLabel(age));
    if (latest) facts.push('Trend: ' + (trendWords[direction] || 'holding steady'));
    setText('hero-facts', facts.join(' · '));
    setText('hero-state', highest.name + ' · ' + FP.classLabel(highest.risk_class) + ' ' + noun + ' risk');
  }

  /* ------------------------------------------------------------------ cards --- */

  function zoneSignature(zones) {
    return zones.map(function (zone) { return zone.zone_id; }).join('|');
  }

  /* Zone ids go into an attribute selector, so they are escaped as CSS: a quote or a bracket
   * inside the render pass and stop the board updating. */
  function zoneSelector(zoneId) {
    var value = String(zoneId === null || zoneId === undefined ? '' : zoneId);
    var escaped = (typeof window !== 'undefined' && window.CSS && window.CSS.escape)
      ? window.CSS.escape(value)
      : value.replace(/["\\]/g, '\\$&');
    return '[data-zone="' + escaped + '"]';
  }

  /* The card grid holds cards and nothing else: an empty fleet leaves it empty, and the
   * sentence that explains the emptiness lives beside it (see renderCitiesPage). */
  function renderCards(zones) {
    var grid = el.zones;
    if (!grid) return;
    if (grid.getAttribute('data-signature') !== zoneSignature(zones)) {
      // The index decides image loading only: the first row of cards is on screen and loads the
      // way the browser does by default, the rest carry loading="lazy". (.map passes the index.)
      grid.innerHTML = zones.map(function (zone, index) {
        return FP.renderZoneCard(zone, { eager: index < 4, noun: FP.zoneNoun(state.payload) });
      }).join('');
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
    // rebuilt when the zone changes, so every field is patched, never replaced.
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
    // fallback below covers a canvas that has not been laid out yet.
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

  /* One request per city view, for the widest window the API keeps; the four buttons slice memory. */
  var HISTORY_MINUTES = 1440;

  function loadHistory() {
    if (!state.selectedZone || state.route.view !== 'city') return;
    if (state.historyInFlight && state.historyLoadingZone === state.selectedZone) return;
    // a broken history route must not be hammered by the poll, the ticker and a tab return at once
    if (state.historyFailedMs !== null && Date.now() - state.historyFailedMs < HISTORY_RETRY_MS) return;
    var zone = state.selectedZone, seq = ++state.historySeq;
    state.historyInFlight = true;
    state.historyLoadingZone = zone;
    state.historyError = null;
    setText('history-meta', 'loading the stored history…'); // the panel keeps what is on screen meanwhile
    var giveUp = setTimeout(function () {
      if (seq !== state.historySeq) return;
      state.historyInFlight = false;
      state.historyLoadingZone = null;
      state.historyFailedMs = Date.now();
      state.historyError = 'no answer';
      state.history = null;
      state.historyZone = null;
      drawHistory();
    }, FETCH_TIMEOUT_MS);
    getJson('/zones/' + encodeURIComponent(zone) + '/history?minutes=' + HISTORY_MINUTES).then(function (data) {
      clearTimeout(giveUp);
      if (seq !== state.historySeq) return; // a newer request owns the panel now
      state.historyInFlight = false;
      state.historyLoadingZone = null;
      state.historyFailedMs = null;
      state.historyError = null;
      if (zone !== state.selectedZone || state.route.view !== 'city') return; // nobody is on that city
      if (data && data.zone_id && data.zone_id !== zone) return; // an answer for another city is not this city's history
      state.history = data;
      state.historySeries = FP.temporal.clean(data && data.points);
      state.historyZone = zone;
      drawHistory();
    }).catch(function (error) {
      clearTimeout(giveUp);
      if (seq !== state.historySeq) return;
      state.historyInFlight = false;
      state.historyLoadingZone = null;
      state.historyFailedMs = Date.now();
      state.historyError = error.message;
      state.history = null;
      state.historySeries = null;
      state.historyZone = null;
      drawHistory(); // an empty chart that says why, never the previous city's line still on screen
    });
  }

  /* The panel's own city picker: the control that was here before, kept because a reader on one city's
   * chart should not have to go back to the Cities list to compare another. */
  function renderZonePicker(zones) {
    if (!el['history-zone']) return;
    var list = (zones || []).slice().sort(function (a, b) { return a.name < b.name ? -1 : 1; });
    el['history-zone'].innerHTML = list.map(function (zone) {
      var on = zone.zone_id === state.selectedZone;
      return '<button type="button" class="ghost' + (on ? ' is-active' : '') + '" data-zone-pick="' +
        FP.escapeHtml(zone.zone_id) + '" aria-pressed="' + (on ? 'true' : 'false') + '">' + FP.escapeHtml(zone.name) + '</button>';
    }).join('');
  }

  function renderWindows(choices, selected, summary) {
    if (el['history-range']) {
      el['history-range'].innerHTML = choices.map(function (option) {
        var on = selected === option.minutes;
        return '<button type="button" class="ghost' + (on ? ' is-active' : '') + '" data-range="' + (option.minutes === null ? 'all' : option.minutes) +
          '" aria-pressed="' + (on ? 'true' : 'false') + '"' + (option.enabled ? '' : ' disabled aria-describedby="history-window-note"') + '>' + option.label + '</button>';
      }).join('');
    }
    if (el['history-window-note']) {
      var off = choices.filter(function (option) { return !option.enabled; });
      var note = FP.temporal.quality(summary) + (off.length ? '. The ' + off.map(function (option) { return option.label; }).join(' and ') +
        ' window' + (off.length > 1 ? 's are' : ' is') + ' not offered yet: ' + off[0].reason + '.' : '');
      if (el['history-window-note'].textContent !== note) el['history-window-note'].textContent = note;
    }
  }

  /* One SVG chart sized from the element's own layout width, plus the sentence that goes with it. */
  function drawChart(id, width, summary, sentenceId, options) {
    var node = el[id];
    if (node && node.setAttribute) {
      var drawn = FP.temporal.chart(summary, { kind: options.kind, width: width, note: options.note });
      node.setAttribute('viewBox', drawn.viewBox);
      node.setAttribute('aria-label', options.label + ' ' + options.sentence);
      node.innerHTML = drawn.body;
    }
    if (el[sentenceId]) el[sentenceId].textContent = options.sentence;
  }

  function drawHistory() {
    var zoneId = state.route.view === 'city' ? state.route.zoneId : null;
    // a stored series belongs to one city: a switch shows the loading state, never the previous city's
    // line, and loadHistory's sequence guard drops an answer that arrives too late
    var series = zoneId && state.historyZone === zoneId ? state.historySeries : null;
    var choices = FP.temporal.windows(series), selected = state.historyWindow, active = null;
    choices.forEach(function (option) { if (option.minutes === selected) active = option; });
    if (!active || !active.enabled) { selected = null; active = choices[choices.length - 1]; }
    var summary = FP.temporal.summary(series, { minutes: selected, nowSeconds: Math.round(Date.now() / 1000) });
    var riskWidth = Math.max(200, Math.round((el['history-canvas'] && el['history-canvas'].clientWidth) || 640));
    var rainWidth = Math.max(200, Math.round((el['rain-chart'] && el['rain-chart'].clientWidth) || riskWidth));
    var data = state.history, loading = 'Loading the stored history…', failed = Boolean(state.historyError);
    state.windowPoints = summary.points;
    state.windowSummary = summary;
    renderWindows(choices, selected, summary);
    if (el['history-facts']) el['history-facts'].innerHTML = FP.temporal.facts(summary, { windowLabel: active.label });
    if (el['history-meta']) {
      el['history-meta'].textContent = data
        ? data.point_count + ' points · asked for ' + data.minutes + ' min · ' + (data.downsampled ? 'downsampled (stride ' + data.stride + ')' : 'full resolution') + ' · model ' + data.model
        : (failed ? 'history unavailable (' + state.historyError + ') — retrying in ' + Math.round(HISTORY_RETRY_MS / 1000) + ' s' : loading);
    }
    drawChart('history-canvas', riskWidth, summary, 'history-summary', {
      sentence: series ? FP.temporal.describe(summary) : (failed ? 'Risk score history could not be loaded.' : loading),
      note: failed ? 'History could not be loaded.' : 'No stored readings in this window yet.', label: 'Risk score over time, 0 to 1.'
    });
    drawChart('rain-chart', rainWidth, summary, 'rain-summary', {
      kind: 'rain', sentence: series ? FP.temporal.rainfallSentence(summary) : (failed ? 'The stored rainfall rate could not be loaded.' : loading),
      note: failed ? 'History could not be loaded.' : 'No stored rainfall readings in this window yet.', label: 'Rainfall rate over time, mm/h.'
    });
    // the timeline marks exactly the incidents this window cannot show: those that opened before its
    // first stored sample, and everything when the window holds no sample at all
    renderCityIncidents(findZone(zoneId), { windowStartSeconds: summary.count ? summary.firstTs : null, windowLabel: active.label, nowMs: Date.now() });
  }

  /* The chart's own label: the series it is actually showing, never a fixed sentence. */
  function historyAriaLabel(summary) {
    if (state.historyError) return 'Risk score history could not be loaded.';
    if (!summary || !summary.count) return 'Risk score history: no readings in this window.';
    return 'Risk score history: ' + summary.count + ' readings from ' + FP.temporal.clock(summary.firstTs) + ' to ' + FP.temporal.clock(summary.lastTs) +
      '. Lowest ' + summary.minScore.toFixed(2) + ', highest ' + summary.maxScore.toFixed(2) + '.';
  }

  /* Keyboard and touch reach the same per-point values the mouse tooltip shows: they used to be behind a
   * mousemove on a canvas that could not be focused. */
  function hoverIndexAt(event) {
    var points = state.windowPoints || [];
    if (!points.length) return null;
    var clientX = event.clientX; // a touch event carries its coordinates in touches[0]
    if (clientX === undefined || clientX === null) {
      var touch = event.touches && event.touches[0] ? event.touches[0] : null;
      if (!touch) return null;
      clientX = touch.clientX;
    }
    // the same paddings the chart draws with, so a hover lands on the point under the pointer
    var rect = el['history-canvas'].getBoundingClientRect(), plotWidth = rect.width - 42 - 14;
    if (!(plotWidth > 0)) return null;
    return points.length === 1 ? 0 : Math.round(Math.min(1, Math.max(0, (clientX - rect.left - 42) / plotWidth)) * (points.length - 1));
  }

  function showPoint(index) {
    var tooltip = el['history-tooltip'], points = state.windowPoints || [], point = null;
    if (index !== null && index !== undefined && points[index]) point = points[index];
    state.hoverIndex = point ? index : null;
    if (!tooltip) return;
    if (!point) { tooltip.hidden = true; return; }
    // the reading M16 is about, reported as the API stores it: absent readings are said to be absent
    // rather than looked up elsewhere
    var rain = (point.row && point.row.readings) ? point.row.readings.rainfall_mm_per_hr : null;
    tooltip.hidden = false;
    tooltip.innerHTML = '<b>#' + (index + 1) + ' of ' + points.length + '</b> ' + FP.escapeHtml(FP.temporal.clock(point.ts)) +
      ' · score ' + FP.escapeHtml(FP.formatScore(point.score)) + ' · ' + FP.escapeHtml(FP.classLabel(point.klass)) +
      (rain === null || rain === undefined ? '<br><span class="muted">no sensor readings stored for this point</span>'
        : '<br><span class="muted">rain ' + FP.escapeHtml(FP.formatValueWithUnit('rainfall_mm_per_hr', rain)) + ' stored with this point</span>');
  }

  /* The stored samples are readable without a mouse: the arrow keys step through them and write the same
   * point onto the page that the tooltip shows. */
  function handleHistoryKey(event) {
    var points = state.windowPoints || [], last = points.length - 1, index = state.hoverIndex;
    if (last < 0) return;
    if (event.key === 'ArrowRight') index = index === null ? 0 : Math.min(last, index + 1);
    else if (event.key === 'ArrowLeft') index = index === null ? last : Math.max(0, index - 1);
    else if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = last;
    else if (event.key === 'Escape') { showPoint(null); return; } else return;
    event.preventDefault();
    showPoint(index);
    var shown = ' Showing point ' + (index + 1) + ' of ' + points.length + ': score ' + FP.formatScore(points[index].score) + '.';
    // the label is the only text alternative a keyboard user has, so keep it current
    if (el['history-canvas'] && el['history-canvas'].setAttribute) {
      el['history-canvas'].setAttribute('aria-label', historyAriaLabel(state.windowSummary) + shown + ' Use the left and right arrow keys to move through the points.');
    }
    setText('history-summary', (state.windowSummary ? FP.temporal.describe(state.windowSummary) : '') + shown);
  }

  function handleHistoryHover(event) {
    var index = hoverIndexAt(event);
    if (index === null) {
      if (el['history-tooltip']) el['history-tooltip'].hidden = true;
      return;
    }
    showPoint(index);
  }


  /* The site is one document with six views. The URL hash is the only source of
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
      renderZonePicker(state.zones);
      // One request per city view, and none at all for an id the fleet does not have.
      if (findZone(route.zoneId) && (zoneChanged || state.historyZone !== route.zoneId)) loadHistory();
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


  /* ``metaHtml`` is deliberately raw HTML, not text: callers pass markup (a unit, an inline
   * icon), so the caller escapes anything that came from the API. */
  function statCard(label, value, metaHtml, tone, iconName) {
    return '<article class="stat' + (tone ? ' band-' + tone : '') + '">' +
      (iconName ? '<span class="stat-icon" aria-hidden="true">' + FP.icon(iconName, 20) + '</span>' : '') +
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
    // The headline sentence belongs to the hero, which writes it from the leader's band; this
    // renderer keeps to the line under it so the two never race for the same node.
    setText('ov-subline', ov.reporting + ' of ' + ov.total + ' ' + plural + ' are reporting a scored reading · updated ' +
      ov.updated + ' · ' + ov.subline);

    if (el['ov-stats']) {
      var leaderValue = ov.leaderName || 'No reading yet';
      var leaderMeta = ov.leaderName
        ? '<span class="pill band-' + FP.bandKey(ov.worst) + '">' + FP.escapeHtml(ov.worst) + '</span>' +
          ' score ' + FP.escapeHtml(ov.leaderScore) +
          // the score is only meaningful with the age of the reading behind it
          (ov.leaderAge === null ? '' : ' · updated ' + FP.escapeHtml(FP.ageLabel(ov.leaderAge))) +
          (ov.leaderStale ? ' <span class="muted">· not reporting recently</span>' : '')
        : 'The first reading will appear here';
      var alertMeta = ov.activeAlerts
        ? 'highest severity ' + FP.escapeHtml(state.incidents && state.incidents.incidents ? (FP.alertsSplit(state.incidents).active[0] || {}).severity || '–' : '–')
        : 'every ' + noun + ' is below the alert level';
      var reportingTone = ov.reporting === 0 ? '' : 'normal';
      var html = statCard('Highest risk ' + noun, leaderValue, leaderMeta, FP.bandKey(ov.worst), 'chart') +
        statCard('Active alerts', String(ov.activeAlerts), FP.escapeHtml(alertMeta),
          ov.activeAlerts ? 'warning' : '', ov.activeAlerts ? 'warning' : 'check') +
        statCard(plural.charAt(0).toUpperCase() + plural.slice(1) + ' reporting', ov.reporting + ' / ' + ov.total,
          'newest stored reading per ' + noun, reportingTone, 'city') +
        statCard('Weather freshness', ov.feedValue,
          FP.escapeHtml(ov.freshness || 'waiting for the first provider check'), '', 'refresh');
      if (el['ov-stats'].getAttribute('data-rendered') !== html) {
        el['ov-stats'].innerHTML = html;
        el['ov-stats'].setAttribute('data-rendered', html);
      }
    }

    renderCityCards(state.zones);
    renderAlertPreview(state.incidents);
    renderRecentActivity();
  }

    /* How many cities the Overview shows before it links to the rest. */

  var OVERVIEW_CITY_LIMIT = 5;

  /* "Is anything actually alerting?" in one compact block: the two newest active alerts, or a
   * payload as the Alerts page, so the two can never disagree. */
  function renderAlertPreview(payload) {
    var host = el['alert-preview'];
    if (!host) return;
    var signature = (payload ? FP.incidentsSignature(payload) : 'loading') + '|' +
      (state.incidentsError || '') + '|' + FP.zoneNounPlural(state.payload);
    if (signature === state.alertPreviewSignature) return;
    state.alertPreviewSignature = signature;
    host.innerHTML = FP.alertPreview(state.incidentsError ? null : payload, {
      noun: FP.zoneNoun(state.payload),
      plural: FP.zoneNounPlural(state.payload),
      nowMs: Date.now(),
      silentZoneIds: FP.staleZoneIds(state.payload)
    });
    if (el['alert-preview-title']) {
      el['alert-preview-title'].textContent = payload && FP.alertsSplit(payload).active.length
        ? 'Active alerts'
        : 'Recent alerts';
    }
  }

  /* "What should I pay attention to?" - the changes the page has actually observed since it was
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

    /* The highest-risk few: name, state, band, score, freshness. */

  function renderCityCards(zones) {
    var host = el['city-cards'];
    if (!host) return;
    var all = zones || [];
    // The same ordering the risk ranking uses, so the cards and the list can never disagree
    var list = FP.ranking(all).slice(0, OVERVIEW_CITY_LIMIT);
    // The loading/loaded state is part of the signature: an empty list before the first response
    var signature = (state.payload ? 'loaded' : 'loading') + '|' + all.length + '|' +
      (list.map(function (zone) {
        var latest = zone.latest;
        return zone.zone_id + ':' + (latest ? latest.risk_class + ':' + latest.ts_epoch : 'none');
      }).join('|') || 'empty');
    if (signature === state.cityCardSignature) return;
    state.cityCardSignature = signature;
    var noun = FP.zoneNoun(state.payload);
    var stale = FP.staleZoneIds(state.payload);
    host.innerHTML = list.length
      ? list.map(function (zone, index) {
        // index < 4: the first four cards are the ones a wide layout shows without scrolling, so
        // they load eagerly (no attribute); the rest let the browser defer them.
        return FP.cityStatusCard(zone, { noun: noun, staleZoneIds: stale, eager: index < 4 });
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
        : FP.cityCardsSkeleton(OVERVIEW_CITY_LIMIT));
    renderOverviewFleetNote(all, list);
  }

    /* The line under the grid and the way to the other cities, both from the payload. */

  function renderOverviewFleetNote(all, shown) {
    var plural = FP.zoneNounPlural(state.payload);
    var total = FP.toNumber(state.payload && state.payload.zone_count);
    if (total === null) total = all.length;
    if (el['city-cards-note']) {
      var note = !all.length ? ''
        : (all.length > shown.length
          ? 'Showing the ' + shown.length + ' highest-risk ' + plural + ' of ' + total +
            '. The rest are one click away.'
          : 'Showing every ' + plural + ' the fleet is monitoring (' + total + ').');
      setText('city-cards-note', note);
    }
    if (el['city-cards-more']) {
      var label = 'View all ' + total + ' ' + plural;
      if (el['city-cards-more'].textContent !== label) el['city-cards-more'].textContent = label;
      el['city-cards-more'].hidden = total <= shown.length;
    }
  }


  function renderCityDetail() {
    var slot = el['city-detail'];
    if (!slot) return;
    var zoneId = state.route.zoneId;
    var zone = findZone(zoneId);
    setText('city-crumb', zone ? zone.name : (zoneId || 'city'));
    if (!zone) {
      var waiting = !state.payload;
      var noun = FP.zoneNoun(state.payload);
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
      // the alert history for a city that is not in the fleet is empty, and says so, and the
      // retrospective panel is hidden rather than showing another city's stored history
      renderCityIncidents(null);
      if (el['history-panel']) el['history-panel'].hidden = true;
      if (el['city-context']) el['city-context'].innerHTML = '';
      return;
    }
    var latest = zone.latest;
    var signature = zoneId + '|' + (latest ? latest.ts_epoch : 'none') + '|' + (state.payload ? state.payload.generated_at : '');
    if (signature !== state.detailSignature) {
      state.detailSignature = signature;
      var feed = state.payload ? FP.feedStatus(state.payload, Date.now(), payloadAgeOffsetSeconds()) : null;
      // The article is written in two halves either side of the retrospective panel, which is static
      // markup: what is happening now above it, why this city is monitored and the raw model below.
      var article = FP.cityDetail(zone, { feed: feed, noun: FP.zoneNoun(state.payload) });
      var at = article.indexOf(FP.CITY_SPLIT);
      slot.innerHTML = article.slice(0, at);
      if (el['city-context']) el['city-context'].innerHTML = article.slice(at + FP.CITY_SPLIT.length);
    }
    if (el['history-panel']) el['history-panel'].hidden = false;
    renderZonePicker(state.zones);
    // Outside the signature check: the alert history belongs to whichever city is selected, and
    // switching cities must never leave the previous city's alerts on screen.
    renderCityIncidents(zone);
  }

  function renderCityIncidents(zone, options) {
    var opts = options || {};
    var host = el['city-incidents'];
    if (!host) return;
    var noun = FP.zoneNoun(state.payload);
    var zoneId = zone && zone.zone_id ? zone.zone_id : null;
    var incidents = (state.incidents && state.incidents.incidents) || [];
    var signature = String(zoneId) + '|' + String(opts.windowLabel || '') + '|' + String(opts.windowStartSeconds || '') + '|' +
      (state.incidents ? 'loaded|' + FP.incidentsSignature(state.incidents) : 'loading');
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
    host.innerHTML = FP.cityIncidentHistory(incidents, zoneId, Date.now(), {
      noun: noun, limit: 5, windowStartSeconds: opts.windowStartSeconds, windowLabel: opts.windowLabel });
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


  function renderAlerts() {
    var payload = state.incidents;
    var split = FP.alertsSplit(payload);
    var summary = FP.alertSummary(split);
    // "nothing loaded yet" and "loaded, and there are no incidents" are different states
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
      var rowOptions = { silentZoneIds: silentZones, alerting: payload ? payload.alerting : null,
        noun: FP.zoneNoun(payload) };
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
          : '<details class="tech"><summary>Technical alert details</summary>' +
        FP.deliveryPanel(payload) + '</details>';
      }
      if (el['alerts-summary']) {
        var alertsNoun = FP.zoneNoun(state.payload);
        var health = FP.systemHealth(state.payload, payload ? payload.incidents : null, Date.now());
        var html = loading ? '' : statCard('Active alerts', String(split.active.length),
          split.active.length ? '<span class="pill band-' + FP.bandKey(summary.highest) + '">' +
            FP.escapeHtml(summary.highest) + '</span> highest severity'
            : 'no ' + alertsNoun + ' above WATCH', summary.tone === 'calm' ? '' : FP.bandKey(summary.highest)) +
          statCard('Resolved incidents', String(split.closed.length), 'kept as history', '') +
          statCard('Total recorded', String(split.total), 'kept as history', '') +
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

  /* ------------------------------------------------- email alert subscriptions --- */

  /* One public form: an address, a button, and a result that never claims more than AWS has
   * actually confirmed back to the page. */
  function setSubscriptionState(kind, address) {
    var copy = FP.subscriptionResult(kind);
    var node = el['subscribe-result'];
    var input = el['subscribe-email'];
    state.subscribe = kind;
    if (node) {
      node.className = 'subscribe-result is-' + copy.tone;
      node.innerHTML = '';
      var title = document.createElement('p');
      title.className = 'subscribe-result-title';
      title.textContent = copy.title;
      var body = document.createElement('p');
      body.className = 'subscribe-result-body';
      body.textContent = copy.body;
      node.appendChild(title);
      node.appendChild(body);
      // the empty state is not an outcome: before the first attempt nothing is announced
      node.hidden = kind === 'idle';
    }
    if (input) {
      var invalid = kind === 'invalid';
      input.setAttribute('aria-invalid', invalid ? 'true' : 'false');
      if (invalid) input.setAttribute('aria-describedby', 'subscribe-result subscribe-note');
      else input.removeAttribute('aria-describedby');
      if (kind === 'pending' && address) {
        // the address stays in the field so the visitor can see what was sent, and can still
        // copy it - the form never says an address is subscribed, so this is not a claim either
        input.value = address;
      }
    }
    var button = el['subscribe-button'];
    if (button) {
      button.disabled = false;
      button.textContent = 'Subscribe to alerts';
      button.removeAttribute('aria-busy');
    }
  }

  function submitSubscription() {
    var input = el['subscribe-email'];
    var button = el['subscribe-button'];
    if (!input || state.subscribeInFlight) return;
    var address = String(input.value === null || input.value === undefined ? '' : input.value).trim();
    // The same shape the server enforces: a typo is caught here rather than costing one of the
    // three attempts an address is allowed per hour. The server still decides.
    if (!FP.looksLikeEmail(address)) {
      setSubscriptionState('invalid', null);
      if (typeof input.focus === 'function') input.focus();
      return;
    }

    state.subscribeInFlight = true;
    if (button) {
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      button.textContent = 'Sending…';
    }
    var done = function (kind) {
      state.subscribeInFlight = false;
      setSubscriptionState(kind, kind === 'pending' ? address : null);
    };
    fetch(API_BASE + '/subscriptions', {
      method: 'POST',
      mode: 'cors',
      cache: 'no-store',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ email: address })
    }).then(function (response) {
      // Only the status code is used. The body is drained (so the connection is reusable) and
      // never displayed: an AWS error message is not something a visitor should ever read.
      return response.text().then(function () {
        return response.status;
      });
    }).then(function (status) {
      done(FP.subscriptionKindForStatus(status));
    }).catch(function () {
      done('offline');
    });
  }

  /* --------------------------------------------------------- data and method --- */

  /* The whole methodology page is static prose plus the live provenance per metric, so
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

    /* The incident log is read-only and comes straight from GET /incidents. */

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
    // and the Overview's compact preview reads it too, so the two agree
    renderAlertPreview(state.incidents);
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
      state.alertPreviewSignature = null;
      renderCityCards(state.zones);
      renderCitiesPage();
      renderAlertPreview(state.incidents);
      renderSystemHealth();
      setTimeout(function () { button.disabled = false; }, 1200);
    });

    // The public subscription form. Submitting is intercepted: the page posts to the API with
    if (el['subscribe-form']) {
      el['subscribe-form'].addEventListener('submit', function (event) {
        event.preventDefault();
        submitSubscription();
      });
    }
    bindTo('subscribe-email', 'input', function () {
      // typing clears a previous verdict: an old "enter a valid address" must not sit next to a
      // field the visitor has since corrected
      if (state.subscribe === 'invalid') setSubscriptionState('idle', null);
    });

    bindCityControls();

    bindTo('history-zone', 'click', function (event) {
      var button = event.target.closest('[data-zone-pick]');
      if (!button) return;
      var picked = button.getAttribute('data-zone-pick');
      state.selectedZone = picked;
      state.userSelected = true;
      state.history = null;
      state.historySeries = null;
      state.historyZone = null;
      state.historyFailedMs = null; // an explicit click always retries immediately
      renderZonePicker(state.zones);
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
      // a window is a slice of the series already in memory: choosing one never asks again, and the
      // panel keeps the same request it was opened with
      var range = button.getAttribute('data-range');
      state.historyWindow = range === 'all' ? null : Number(range);
      drawHistory();
    });

    // The chart answers a mouse, a keyboard and a fingertip: the per-point values exist
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


  function cityFilters() {
    if (!state.cities) state.cities = { query: '', coast: 'all', risk: 'all', sort: 'risk' };
    return state.cities;
  }

  /* The risk floors: "WATCH+" means "this class or worse". Values are the API's own class names. */
  var RISK_FILTERS = [
    { value: 'all', label: 'All' },
    { value: 'WATCH', label: 'WATCH+' },
    { value: 'WARNING', label: 'WARNING+' },
    { value: 'CRITICAL', label: 'CRITICAL' }
  ];

    /* A chip group, rebuilt only when its own options or pressed chip changed. */

  function renderChipGroup(host, kind, options, active) {
    if (!host) return;
    var rendered = options.map(function (option) {
      return option.value + (option.value === active ? '*' : '');
    }).join('|');
    if (host.getAttribute('data-rendered') === rendered) return;
    host.setAttribute('data-rendered', rendered);
    host.innerHTML = FP.filterChips(kind, options, active);
  }

    /* Both chip groups, from the fleet's own coast groups and the API's own class names. */

  function renderCityFilters(payload) {
    var rules = cityFilters();
    var coasts = FP.coastOptions(payload);
    var grouped = coasts.length > 1;
    ['cities-coast', 'cities-coast-label', 'cities-coast-field'].forEach(function (id) {
      if (el[id]) el[id].hidden = !grouped;
    });
    renderChipGroup(el['cities-coast'], 'coast', grouped ? coasts : [], rules.coast);
    renderChipGroup(el['cities-risk'], 'risk', RISK_FILTERS, rules.risk);
    if (el['cities-sort'] && el['cities-sort'].value !== rules.sort) el['cities-sort'].value = rules.sort;
    if (el['cities-search'] && el['cities-search'].value !== rules.query) {
      el['cities-search'].value = rules.query;
    }
  }

    /* The grid, the count and the empty states: on every poll, and it fetches nothing. */

  function renderCitiesPage() {
    var rules = cityFilters();
    var all = state.zones || [];
    var plural = FP.zoneNounPlural(state.payload);
    var visible = FP.visibleCities(all, rules);
    var noun = FP.zoneNoun(state.payload);
    // Beside the grid, never inside it. Which empty state applies is the only difference between
    // "the fleet has nothing yet" and "these filters excluded everything".
    var emptyHtml = '';
    if (state.payload) {
      emptyHtml = !all.length
        ? FP.emptyState({
          key: 'no-cities-list',
          icon: 'info',
          title: 'No ' + noun + ' is reporting a scored reading',
          detail: 'The API answered, but no ' + noun + ' has a complete reading yet. ' +
            'This page keeps checking every few seconds.',
          action: 'retry',
          actionLabel: 'Check again now'
        })
        : (!visible.length ? FP.citiesEmptyState(plural) : '');
    }
    if (el['cities-empty']) {
      if (el['cities-empty'].innerHTML !== emptyHtml) el['cities-empty'].innerHTML = emptyHtml;
      el['cities-empty'].hidden = !emptyHtml;
    }
    // Rewritten only when the sentence changes: a status region that hears the same text every
    // poll would announce itself every five seconds for nothing.
    if (el['cities-count']) {
      var line = state.payload ? FP.citiesResultLine(visible.length, all.length, plural,
        FP.cityFilterLabels(rules, { coasts: FP.coastOptions(state.payload), risks: RISK_FILTERS })) : '';
      if (el['cities-count'].textContent !== line) el['cities-count'].textContent = line;
    }
    renderCards(visible);
    renderMap(visible);
    renderCityFilters(state.payload);
  }

  function bindCityControls() {
    // typing filters the list in place: no request, and no debounce needed for twelve rows
    bindTo('cities-search', 'input', function (event) {
      cityFilters().query = String(event.target.value || '');
      renderCitiesPage();
    });
    // one handler per chip group: the group's own attribute names the filter it sets
    var groups = { 'cities-coast': ['coast', 'data-coast'], 'cities-risk': ['risk', 'data-risk'] };
    Object.keys(groups).forEach(function (id) {
      var entry = groups[id];
      bindTo(id, 'click', function (event) {
        var chip = event.target && event.target.closest ? event.target.closest('[' + entry[1] + ']') : null;
        if (!chip) return;
        cityFilters()[entry[0]] = chip.getAttribute(entry[1]) || 'all';
        renderCitiesPage();
      });
    });
    bindTo('cities-sort', 'change', function (event) {
      cityFilters().sort = String(event.target.value || 'risk');
      renderCitiesPage();
    });
    // "Clear filters": the whole fleet back, and the controls back with it
    document.addEventListener('click', function (event) {
      var button = event.target && event.target.closest
        ? event.target.closest('[data-action="clear-filters"]') : null;
      if (!button) return;
      event.preventDefault();
      state.cities = { query: '', coast: 'all', risk: 'all', sort: 'risk' };
      renderCitiesPage();
    });
  }

  function start() {
    cacheDom();
    bind();
    // The route decides which of the six views is visible, before any data arrives.
    applyRoute({ focus: false });
    renderDataPage(null);
    // The three slow panels start as skeletons rather than as sentences, so the first paint
    renderCityCards([]);
    renderAbout(null);
    renderSystemHealth();
    if (!API_BASE) {
      // Say why the page will never fill in, in the same status line a failing poll uses:
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
