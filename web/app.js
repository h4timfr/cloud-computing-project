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
  var API_BASE = (params.get('api') || window.FLOODPULSE_API_BASE || '').replace(/\/+$/, '');

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
    lastErrorMessage: null,
    // which view the URL asks for, and what each view currently shows (so a poll that
    // changed nothing does not rewrite the DOM). The route starts empty so the first
    // applyRoute() always installs the view the URL asked for.
    route: { view: null, zoneId: null },
    overviewSignature: null,
    cityCardSignature: null,
    detailSignature: null,
    alertsSignature: null,
    dataSignature: null,
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
    [
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
      // navigation and the four views (M6 information architecture)
      'nav-toggle', 'nav-list', 'nav-overview', 'nav-cities', 'nav-alerts', 'nav-data',
      'view-overview', 'view-cities', 'view-city', 'view-alerts', 'view-data',
      'ov-headline', 'ov-subline', 'ov-intro', 'ov-stats', 'band-strip-title',
    'city-cards-title', 'cities-lede', 'incidents-zone-header', 'city-cards', 'city-detail', 'city-crumb',
      'alerts-lede', 'alerts-summary', 'alerts-active', 'alerts-closed',
      'provenance-legend', 'attribution-block', 'attribution-detail', 'summary-weather-provider',
      'method-block', 'why-simulated', 'limitations', 'summary-model-data'
    ].forEach(function (id) { el[id] = $(id); });
  }

  /* ------------------------------------------------------------------ fetch --- */

  function getJson(path) {
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

  /* ------------------------------------------------------------------- poll --- */

  function poll() {
    if (state.inFlight) return;
    state.inFlight = true;
    getJson('/zones').then(function (payload) {
      state.inFlight = false;
      state.lastError = false;
      state.lastErrorMessage = null;
      state.lastSuccessMs = Date.now();
      state.payloadReceivedMs = Date.now();
      state.payload = payload;
      applyPayload(payload);
      // Incidents have their own 10 s interval. Polling them from here as well meant two
      // /incidents requests fired together every time the 5 s poll crossed a 10 s boundary.
    }).catch(function (error) {
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

  function applyPayload(payload) {
    state.zones = payload.zones || [];

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
      setText('api-base', API_BASE);
      el['api-base'].setAttribute('href', API_BASE + '/zones');
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
    var zones = (payload && payload.zones) || [];
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
      el['conn-badge'].className = 'conn-badge conn-' + info.state;
      document.body.classList.remove('is-live', 'is-stale', 'is-disconnected', 'is-connecting');
      document.body.classList.add('is-' + info.state);
    }
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
          'moisture from ' + (feed.provider || 'the weather provider') + (feed.refreshMinutes ? ' every ' + feed.refreshMinutes + ' minutes' : '') +
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
    container.innerHTML =
      '<div class="card hero band-' + band + '">' +
      '<div class="hero-main">' +
      '<p class="hero-kicker">Highest risk ' + noun + '</p>' +
      '<h2 class="hero-name">' + FP.escapeHtml(highest.name) + '</h2>' +
      '<p class="hero-meta">' + FP.escapeHtml(highest.zone_id) + ' · reading ' +
      FP.escapeHtml(FP.clockLabel(highest.timestamp)) +
      (latest ? ' · ' + FP.escapeHtml(FP.ageLabel(latest.age_seconds)) : '') + '</p>' +
      '<p class="hero-explain">' + FP.escapeHtml(latest ? latest.explanation : 'no explanation available') + '</p>' +
      (latest && latest.top_drivers && latest.top_drivers.length
        ? '<p class="hero-drivers">Dominant drivers: ' + latest.top_drivers.map(function (driver) {
          return '<span class="chip">' + FP.escapeHtml(driver) + '</span>';
        }).join('') + '</p>'
        : '') +
      '</div>' +
      '<div class="hero-gauge">' +
      '<div class="ring ring-lg band-' + band + '" style="--pct:' + FP.riskPercent(highest.risk_score).toFixed(1) + '">' +
      '<span class="ring-value">' + FP.escapeHtml(FP.formatScore(highest.risk_score)) + '</span>' +
      '<span class="ring-caption">heuristic-v1 score</span>' +
      '</div>' +
      '<span class="pill band-' + band + '">' + FP.escapeHtml(FP.classLabel(highest.risk_class)) + '</span>' +
      '</div>' +
      '</div>';
  }

  /* ------------------------------------------------------------------ cards --- */

  function zoneSignature(zones) {
    return zones.map(function (zone) { return zone.zone_id; }).join('|');
  }

  function renderCards(zones) {
    var grid = el.zones;
    if (grid.getAttribute('data-signature') !== zoneSignature(zones)) {
      grid.innerHTML = zones.map(FP.renderZoneCard).join('');
      grid.setAttribute('data-signature', zoneSignature(zones));
    }
    zones.forEach(function (zone) {
      var card = grid.querySelector('[data-zone="' + zone.zone_id + '"]');
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
    setText('risk_class_candidate', FP.classLabel(latest.risk_class_candidate));
    setText('top_driver', (latest.top_drivers || [])[0] || '–');
    setText('sequence', latest.sequence);
    setText('sensor_node_id', latest.sensor_node_id);
    setText('age', FP.ageLabel(latest.age_seconds));
    setText('explanation', latest.explanation);

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

    FP.METRIC_ORDER.forEach(function (metric) {
      var row = card.querySelector('[data-metric="' + metric + '"]');
      if (!row) return;
      var valueNode = row.querySelector('[data-f="metric-value"]');
      var fillNode = row.querySelector('[data-f="metric-fill"]');
      var scale = FP.metricScale(metric, latest.readings[metric], zone.thresholds || {});
      if (valueNode) valueNode.textContent = FP.formatValueWithUnit(metric, latest.readings[metric]);
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
    state.historyInFlight = true;
    // Say we are fetching before the request goes out: the panel used to keep the previous
    // window's line ("18 points · 1440 min") while showing a chart of a different range.
    state.history = null;
    state.historyError = null;
    state.hoverIndex = null;
    el['history-meta'].textContent = 'loading the last ' + minutes + ' min…';
    drawHistory();
    getJson('/zones/' + encodeURIComponent(zone) + '/history?minutes=' + minutes)
      .then(function (data) {
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
    canvas.setAttribute('aria-label', historyAriaLabel(points));
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

  function handleHistoryHover(event) {
    var data = state.history;
    if (!data || !data.points || data.points.length === 0) {
      el['history-tooltip'].hidden = true;
      return;
    }
    var canvas = el['history-canvas'];
    var rect = canvas.getBoundingClientRect();
    var padLeft = 42;
    var padRight = 12;
    var plotWidth = rect.width - padLeft - padRight;
    var ratio = Math.min(1, Math.max(0, (event.clientX - rect.left - padLeft) / plotWidth));
    var index = data.points.length === 1 ? 0 : Math.round(ratio * (data.points.length - 1));
    state.hoverIndex = index;
    var point = data.points[index];
    var tooltip = el['history-tooltip'];
    var readings = point.readings || {}; // a point without readings must not throw
    tooltip.hidden = false;
    // Every sensor channel the point carries, named as the cards name it, so the chart can
    // be read without hunting for the same values in a card.
    var channels = [
      ['rainfall_mm_per_hr', 'rain'],
      ['drain_level_m', 'drain'],
      ['soil_saturation_pct', 'soil'],
      ['water_level_m', 'water'],
      ['flow_rate_m3_s', 'flow']
    ].map(function (entry) {
      var value = readings[entry[0]];
      if (value === null || value === undefined) return null;
      return entry[1] + ' ' + FP.formatValueWithUnit(entry[0], value);
    }).filter(Boolean);
    tooltip.innerHTML = FP.escapeHtml(FP.clockLabel(point.timestamp)) + ' · score ' +
      FP.escapeHtml(FP.formatScore(point.risk_score)) + ' · ' + FP.escapeHtml(FP.classLabel(point.risk_class)) +
      (channels.length
        ? '<br><span class="muted">' + FP.escapeHtml(channels.join(' · ')) + '</span>'
        : '<br><span class="muted">no sensor readings stored for this point</span>');
    // Keep the tooltip inside the panel: it is centred on the cursor, so near an edge it
    // would otherwise hang outside the card and clip.
    var half = tooltip.offsetWidth ? tooltip.offsetWidth / 2 : 0;
    var offsetX = Math.round(event.clientX - rect.left);
    if (half) offsetX = Math.min(Math.max(offsetX, half + 4), Math.round(rect.width - half - 4));
    tooltip.style.left = offsetX + 'px';
    drawHistory();
  }

  /* ===================================================== views and navigation === */

  /* The site is one document with five views. The URL hash is the only source of
   * truth for which one is visible, so a link, a reload or the back button all land in
   * the same place; nothing here fetches, it only decides what is on screen. */
  var VIEWS = ['overview', 'cities', 'city', 'alerts', 'data'];

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

  function statCard(label, value, meta, tone) {
    return '<article class="stat' + (tone ? ' band-' + tone : '') + '">' +
      '<p class="stat-label">' + FP.escapeHtml(label) + '</p>' +
      '<p class="stat-value">' + FP.escapeHtml(value) + '</p>' +
      '<p class="stat-meta">' + meta + '</p>' +
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
          ' score ' + FP.escapeHtml(ov.leaderScore)
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
  }

  /* Five compact cards: name, band, one plain sentence, score and freshness. The full
   * detail lives on the city view, one click away. */
  function renderCityCards(zones) {
    var host = el['city-cards'];
    if (!host) return;
    var list = zones || [];
    var signature = list.map(function (zone) {
      var latest = zone.latest;
      return zone.zone_id + ':' + (latest ? latest.risk_class + ':' + latest.ts_epoch : 'none');
    }).join('|') || 'empty';
    if (signature === state.cityCardSignature) return;
    state.cityCardSignature = signature;
    host.innerHTML = list.length
      ? list.map(function (zone) {
        return FP.cityStatusCard(zone, { noun: FP.zoneNoun(state.payload) });
      }).join('')
      : '<p class="muted" data-f="no-cities">No ' + FP.zoneNoun(state.payload) +
        ' has reported a scored reading yet. ' +
        'This page keeps polling the API and fills in as soon as the first reading arrives.</p>';
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
        slot.innerHTML = '<div class="card detail-missing">' +
          '<h3>' + (waiting ? 'Loading this ' + noun + '…' : 'This ' + noun + ' is not in the current fleet') + '</h3>' +
          '<p class="muted">' + (waiting
            ? 'Waiting for the first response from <code>GET /zones</code>.'
            : 'The API does not report a ' + noun + ' with the id <code>' + FP.escapeHtml(zoneId || '') + '</code>. ' +
              'It may belong to a different fleet (the simulated demo zones are not part of the live one).') + '</p>' +
          '<p><a class="ghost-link" href="#/cities">Back to all ' + pluralTitle.toLowerCase() + '</a></p>' +
          '</div>';
      }
      return;
    }
    var latest = zone.latest;
    var signature = zoneId + '|' + (latest ? latest.ts_epoch : 'none') + '|' + (state.payload ? state.payload.generated_at : '');
    if (signature === state.detailSignature) return;
    state.detailSignature = signature;
    var feed = state.payload ? FP.feedStatus(state.payload, Date.now(), payloadAgeOffsetSeconds()) : null;
    slot.innerHTML = FP.cityDetail(zone, { feed: feed, noun: FP.zoneNoun(state.payload) });
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
      if (el['alerts-active']) {
        el['alerts-active'].innerHTML = loading
          ? '<li class="muted">loading alerts…</li>'
          : (split.active.length
            ? split.active.map(function (incident) { return FP.alertRow(incident, Date.now()); }).join('')
            : FP.alertsEmptyState('active', FP.zoneNoun(state.payload)));
      }
      if (el['alerts-closed']) {
        el['alerts-closed'].innerHTML = loading
          ? '<li class="muted">loading incidents…</li>'
          : (split.closed.length
            ? split.closed.map(function (incident) { return FP.alertRow(incident, Date.now()); }).join('')
            : FP.alertsEmptyState('closed', FP.zoneNoun(state.payload)));
      }
      if (el['alerts-summary']) {
        var alertsNoun = FP.zoneNoun(state.payload);
        var html = loading ? '' : statCard('Active alerts', String(split.active.length),
          split.active.length ? '<span class="pill band-' + FP.bandKey(summary.highest) + '">' +
            FP.escapeHtml(summary.highest) + '</span> highest severity'
            : 'no ' + alertsNoun + ' above WATCH', summary.tone === 'calm' ? '' : FP.bandKey(summary.highest)) +
          statCard('Resolved incidents', String(split.closed.length), 'kept as history', '') +
          statCard('Total recorded', String(split.total), 'source <code>GET /incidents</code>', '') +
          // "armed" only claims the host-side publish path is configured: the browser is never
          // told about subscribers, so delivery to a person is never asserted here
          statCard('Alert publishing', payload && payload.alerting && payload.alerting.enabled ? 'Configured' : 'Not configured',
            FP.escapeHtml(FP.alertingNote(payload ? payload.alerting : null)), payload && payload.alerting && payload.alerting.enabled ? 'normal' : '');
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

  /* ----------------------------------------------------------------- wiring --- */

  /* ------------------------------------------------------------- incidents --- */

  /* The incident log is read-only and comes straight from GET /incidents: this page
   * never invents or edits an incident, and the SNS topic is never exposed to it. */
  function loadIncidents() {
    if (state.incidentsInFlight) return; // one request at a time, whoever asked for it
    state.incidentsInFlight = true;
    state.incidentsMs = Date.now();
    getJson('/incidents?limit=' + INCIDENTS_LIMIT).then(function (payload) {
      state.incidentsInFlight = false;
      state.incidents = payload;
      state.incidentsError = null;
      renderIncidentsPanel();
    }).catch(function (error) {
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
      el['nav-list'].addEventListener('click', function (event) {
        followNav(event);
      });
    }
    if (el['nav-toggle']) {
      el['nav-toggle'].addEventListener('click', function (event) {
        event.preventDefault();
        setMenu(!state.menuOpen);
      });
    }
    if (el['city-cards']) {
      el['city-cards'].addEventListener('click', function (event) {
        followCity(event);
      });
    }
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && state.menuOpen) setMenu(false);
    });
    window.addEventListener('hashchange', function () {
      applyRoute({ focus: true });
    });

    el['history-zone'].addEventListener('click', function (event) {
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

    el['history-range'].addEventListener('click', function (event) {
      var button = event.target.closest('[data-range]');
      if (!button) return;
      state.historyMinutes = Number(button.getAttribute('data-range'));
      state.history = null;
      state.historyFailedMs = null;
      renderHistoryButtons(state.zones);
      loadHistory();
    });

    el['history-canvas'].addEventListener('mousemove', handleHistoryHover);
    el['history-canvas'].addEventListener('mouseleave', function () {
      state.hoverIndex = null;
      el['history-tooltip'].hidden = true;
      drawHistory();
    });

    window.addEventListener('resize', function () {
      drawHistory();
      state.zones.forEach(function (zone) {
        var card = el.zones.querySelector('[data-zone="' + zone.zone_id + '"]');
        if (card) drawSpark(card, zone);
      });
    });

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
    // the route decides which of the five views is visible, before any data arrives
    applyRoute({ focus: false });
    renderDataPage(null);
    if (!API_BASE) {
      // Say why the page will never fill in, in the same status line a failing poll uses:
      // this used to leave the badge on CONNECTING and five panels on "waiting for data…"
      // forever, because start() returned before anything could update them.
      el['conn-badge'].className = 'conn-badge conn-disconnected';
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
