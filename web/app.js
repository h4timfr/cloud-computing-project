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
    payload: null,
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
    incidentsError: null
  };

  function $(id) { return document.getElementById(id); }

  function cacheDom() {
    [
      'conn-badge', 'conn-detail', 'last-update', 'api-base', 'hero', 'ranking', 'zones',
      'history-zone', 'history-range', 'history-canvas', 'history-meta', 'history-tooltip',
      'notice-text', 'summary-live', 'summary-model', 'summary-generated', 'poll-count',
      'incidents', 'incidents-meta', 'incidents-alerting'
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
      state.lastSuccessMs = Date.now();
      state.payload = payload;
      applyPayload(payload);
      var nowMs = Date.now();
      if (state.incidentsMs === null || nowMs - state.incidentsMs >= INCIDENTS_REFRESH_MS) {
        loadIncidents();
      }
    }).catch(function (error) {
      state.inFlight = false;
      state.lastError = true;
      if (el['conn-detail']) {
        el['conn-detail'].textContent = 'last request failed (' + error.message + ') — keeping last good data';
      }
      refreshConnection();
    });
  }

  function applyPayload(payload) {
    state.zones = payload.zones || [];

    if (!state.userSelected || !findZone(state.selectedZone)) {
      state.selectedZone = payload.highest_risk_zone ? payload.highest_risk_zone.zone_id
        : (state.zones[0] ? state.zones[0].zone_id : null);
    }

    rememberSeries(state.zones);
    renderSummary(payload);
    renderHero(payload);
    renderCards(state.zones);
    renderRanking(state.zones);
    renderHistoryButtons(state.zones);
    refreshConnection();

    if (state.history === null) {
      loadHistory();
    }
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
    el['summary-live'].textContent = live + ' / ' + (payload.zone_count || state.zones.length) + ' zones live';
    el['summary-model'].textContent = payload.model || 'heuristic-v1';
    el['summary-generated'].textContent = FP.clockLabel(payload.generated_at);
    if (el['notice-text'] && payload.model_note) {
      el['notice-text'].textContent = payload.model_note;
    }
    if (el['api-base']) {
      el['api-base'].textContent = API_BASE;
      el['api-base'].setAttribute('href', API_BASE + '/zones');
    }
  }

  function refreshConnection() {
    var info = FP.connectionState(Date.now(), state.lastSuccessMs, state.lastError);
    el['conn-badge'].textContent = info.label;
    el['conn-badge'].className = 'conn-badge conn-' + info.state;
    document.body.classList.remove('is-live', 'is-stale', 'is-disconnected', 'is-connecting');
    document.body.classList.add('is-' + info.state);
    if (!state.lastError) {
      el['conn-detail'].textContent = info.detail;
    }
    if (state.lastSuccessMs) {
      el['last-update'].textContent = FP.clockLabel(new Date(state.lastSuccessMs).toISOString());
    }
  }

  /* ------------------------------------------------------------------- hero --- */

  function renderHero(payload) {
    var highest = payload.highest_risk_zone;
    var container = el.hero;
    if (!highest) {
      container.innerHTML =
        '<div class="card hero hero-empty">' +
        '<p class="hero-kicker">Highest risk zone</p>' +
        '<h2>Awaiting live data</h2>' +
        '<p class="muted">No simulated reading has been ingested yet. Start the simulator ' +
        '(<code>python tools/live_simulator.py</code>) and this panel will populate automatically.</p>' +
        '</div>';
      return;
    }
    var zone = findZone(highest.zone_id);
    var latest = zone && zone.latest ? zone.latest : null;
    var band = FP.bandKey(highest.risk_class);
    container.innerHTML =
      '<div class="card hero band-' + band + '">' +
      '<div class="hero-main">' +
      '<p class="hero-kicker">Highest risk zone</p>' +
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

  function drawSpark(card, zone) {
    var canvas = card.querySelector('[data-f="spark"]');
    if (!canvas || !canvas.getContext) return;
    var series = state.series[zone.zone_id] || [];
    var values = series.map(function (point) { return point.risk_score; });
    var ctx = canvas.getContext('2d');
    var dpr = window.devicePixelRatio || 1;
    var width = canvas.width;
    var height = canvas.height;
    canvas.style.height = height + 'px';
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

  /* ---------------------------------------------------------------- history --- */

  function loadHistory() {
    if (!state.selectedZone) return;
    getJson('/zones/' + encodeURIComponent(state.selectedZone) + '/history?minutes=' + state.historyMinutes)
      .then(function (data) {
        state.history = data;
        state.hoverIndex = null;
        drawHistory();
        el['history-meta'].textContent = data.point_count + ' points · ' + data.minutes +
          ' min · ' + (data.downsampled ? 'downsampled (stride ' + data.stride + ')' : 'full resolution') +
          ' · model ' + data.model;
      })
      .catch(function (error) {
        el['history-meta'].textContent = 'history unavailable (' + error.message + ')';
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

  function drawHistory() {
    var canvas = el['history-canvas'];
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext('2d');
    var dpr = window.devicePixelRatio || 1;
    var cssWidth = canvas.clientWidth || 900;
    var cssHeight = 220;
    canvas.width = Math.round(cssWidth * dpr);
    canvas.height = Math.round(cssHeight * dpr);
    canvas.style.height = cssHeight + 'px';
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
    if (points.length === 0) {
      ctx.fillStyle = 'rgba(226, 232, 240, 0.7)';
      ctx.font = '13px system-ui, sans-serif';
      ctx.fillText('No history in this window yet.', padLeft + 8, padTop + 24);
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
    tooltip.hidden = false;
    tooltip.innerHTML = FP.escapeHtml(FP.clockLabel(point.timestamp)) + ' · score ' +
      FP.escapeHtml(FP.formatScore(point.risk_score)) + ' · ' + FP.escapeHtml(FP.classLabel(point.risk_class)) +
      '<br><span class="muted">rain ' + FP.escapeHtml(FP.formatValueWithUnit('rainfall_mm_per_hr', point.readings.rainfall_mm_per_hr)) +
      ' · water ' + FP.escapeHtml(FP.formatValueWithUnit('water_level_m', point.readings.water_level_m)) + '</span>';
    tooltip.style.left = Math.round(event.clientX - rect.left) + 'px';
    drawHistory();
  }

  /* ----------------------------------------------------------------- wiring --- */

  /* ------------------------------------------------------------- incidents --- */

  /* The incident log is read-only and comes straight from GET /incidents: this page
   * never invents or edits an incident, and the SNS topic is never exposed to it. */
  function loadIncidents() {
    state.incidentsMs = Date.now();
    getJson('/incidents?limit=' + INCIDENTS_LIMIT).then(function (payload) {
      state.incidents = payload;
      state.incidentsError = null;
      renderIncidentsPanel();
    }).catch(function (error) {
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
      el.incidents.innerHTML = FP.renderIncidents(payload, Date.now());
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
    el['history-zone'].addEventListener('click', function (event) {
      var button = event.target.closest('[data-zone-pick]');
      if (!button) return;
      state.selectedZone = button.getAttribute('data-zone-pick');
      state.userSelected = true;
      state.history = null;
      renderHistoryButtons(state.zones);
      loadHistory();
    });

    el['history-range'].addEventListener('click', function (event) {
      var button = event.target.closest('[data-range]');
      if (!button) return;
      state.historyMinutes = Number(button.getAttribute('data-range'));
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
    if (!API_BASE) {
      el['conn-detail'].textContent = 'no API base configured';
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
