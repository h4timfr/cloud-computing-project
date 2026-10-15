/* FloodPulse dashboard helpers.
 *
 * Deliberately dependency-free and DOM-free: every function here is pure so the same
 * file can be loaded by the browser (as a classic script defining window.FP) and by the
 * node check in tools/dashboard_check.mjs. Nothing in this file invents a risk value:
 * scores, classes, drivers, explanations and thresholds all come from the API payload.
 */
(function (root) {
  'use strict';

  var BANDS = ['NORMAL', 'WATCH', 'WARNING', 'CRITICAL'];

  var METRIC_ORDER = [
    'rainfall_mm_per_hr',
    'drain_level_m',
    'soil_saturation_pct',
    'water_level_m',
    'flow_rate_m3_s'
  ];
  var METRIC_META = {
    rainfall_mm_per_hr: { label: 'Rainfall', unit: 'mm/h', decimals: 1 },
    drain_level_m: { label: 'Drain level', unit: 'm', decimals: 2 },
    soil_saturation_pct: { label: 'Soil saturation', unit: '%', decimals: 0 },
    water_level_m: { label: 'Water level', unit: 'm', decimals: 2 },
    flow_rate_m3_s: { label: 'Flow rate', unit: 'm³/s', decimals: 2 }
  };

  // Connection health, derived only from when the last successful poll happened.
  var LIVE_MS = 10000;
  var STALE_MS = 25000;

  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function isFiniteNumber(value) {
    return typeof value === 'number' && isFinite(value);
  }

  function toNumber(value) {
    var num = typeof value === 'number' ? value : parseFloat(value);
    return isFiniteNumber(num) ? num : null;
  }

  function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  /* ---------------------------------------------------------------- bands --- */

  function bandKey(riskClass) {
    var text = String(riskClass === null || riskClass === undefined ? '' : riskClass).toUpperCase();
    return BANDS.indexOf(text) >= 0 ? text.toLowerCase() : 'unknown';
  }

  function classLabel(riskClass) {
    var text = String(riskClass === null || riskClass === undefined ? '' : riskClass).toUpperCase();
    return BANDS.indexOf(text) >= 0 ? text : 'NO DATA';
  }

  function classRank(riskClass) {
    var index = BANDS.indexOf(String(riskClass || '').toUpperCase());
    return index < 0 ? -1 : index;
  }

  /* ----------------------------------------------------------- formatting --- */

  function formatScore(value) {
    var num = toNumber(value);
    return num === null ? '–' : num.toFixed(2);
  }

  function riskPercent(value) {
    var num = toNumber(value);
    return num === null ? 0 : clamp(num, 0, 1) * 100;
  }

  function formatMetric(metric, value) {
    var meta = METRIC_META[metric];
    var num = toNumber(value);
    if (num === null) return '–';
    var text = num.toFixed(meta.decimals);
    return text + (meta.unit === '%' ? '%' : ' ' + meta.unit);
  }

  function formatValueWithUnit(metric, value) {
    var meta = METRIC_META[metric];
    var num = toNumber(value);
    if (num === null) return '–';
    return num.toFixed(meta.decimals) + (meta.unit === '%' ? '%' : ' ' + meta.unit);
  }

  function ageLabel(seconds) {
    var num = toNumber(seconds);
    if (num === null) return 'no data';
    if (num < 5) return 'just now';
    if (num < 90) return Math.round(num) + ' s ago';
    if (num < 5400) return Math.round(num / 60) + ' min ago';
    return Math.round(num / 3600) + ' h ago';
  }

  function clockLabel(isoTimestamp) {
    if (!isoTimestamp) return 'unknown time';
    var date = new Date(isoTimestamp);
    if (isNaN(date.getTime())) return String(isoTimestamp);
    return date.toISOString().substr(11, 8) + 'Z';
  }

  function trendLabel(trend) {
    if (!trend || typeof trend !== 'object') return 'no lookback';
    var delta = toNumber(trend.water_level_delta_m);
    if (delta === null) return 'no lookback';
    var sign = delta > 0 ? '+' : '';
    var fraction = toNumber(trend.rise_fraction);
    var suffix = fraction === null ? '' : ' (rise ' + Math.round(fraction * 100) + '%)';
    return sign + delta.toFixed(2) + ' m / 5 min' + suffix;
  }

  function trendDirection(trend) {
    var delta = trend ? toNumber(trend.water_level_delta_m) : null;
    if (delta === null) return 'flat';
    if (delta > 0.05) return 'up';
    if (delta < -0.05) return 'down';
    return 'flat';
  }

  function metricsFrom(zone) {
    var latest = zone && zone.latest ? zone.latest : null;
    var readings = (latest && latest.readings) || {};
    return METRIC_ORDER.map(function (metric) {
      return { metric: metric, meta: METRIC_META[metric], value: readings[metric] };
    });
  }

  /* Position of a value on its own 0..critical scale, using the thresholds that the
   * API returns for that zone. Nothing is hardcoded here. */
  function metricScale(metric, value, thresholds) {
    var watch = thresholds && thresholds[metric] ? toNumber(thresholds[metric].watch) : null;
    var critical = thresholds && thresholds[metric] ? toNumber(thresholds[metric].critical) : null;
    var num = toNumber(value);
    if (watch === null || critical === null || critical <= 0) {
      return { watchPct: null, criticalPct: null, valuePct: null, state: 'unknown' };
    }
    var span = critical * 1.1;
    var valuePct = num === null ? null : clamp(num / span, 0, 1) * 100;
    var state = 'low';
    if (num !== null) {
      if (num >= critical) state = 'critical';
      else if (num >= watch) state = 'watch';
    }
    return {
      watchPct: clamp(watch / span, 0, 1) * 100,
      criticalPct: clamp(critical / span, 0, 1) * 100,
      valuePct: valuePct,
      state: state
    };
  }

  function contributionMap(zone) {
    var latest = zone && zone.latest ? zone.latest : null;
    var contributions = (latest && latest.contributions) || [];
    var map = {};
    contributions.forEach(function (entry) {
      if (entry && entry.metric) map[entry.metric] = entry;
    });
    return map;
  }

  function contributionPercent(points, maxPoints) {
    var num = toNumber(points);
    var max = toNumber(maxPoints);
    if (num === null || max === null || max <= 0) return 0;
    return clamp(num / max, 0, 1) * 100;
  }

  /* ------------------------------------------------------- connection state --- */

  function connectionState(nowMs, lastSuccessMs, hadError) {
    if (lastSuccessMs === null || lastSuccessMs === undefined) {
      return {
        state: 'connecting',
        label: 'CONNECTING',
        detail: 'waiting for the first successful poll'
      };
    }
    var age = Math.max(0, nowMs - lastSuccessMs);
    if (age <= LIVE_MS && !hadError) {
      return { state: 'live', label: 'LIVE', detail: 'updated ' + ageLabel(age / 1000) };
    }
    if (age <= STALE_MS) {
      return {
        state: 'stale',
        label: 'STALE',
        detail: 'showing last good data, updated ' + ageLabel(age / 1000)
      };
    }
    return {
      state: 'disconnected',
      label: 'DISCONNECTED',
      detail: 'no successful poll for ' + ageLabel(age / 1000) + ' — values below may be out of date'
    };
  }

  /* -------------------------------------------------------------- ranking --- */

  function ranking(zones) {
    var list = (zones || []).slice();
    list.sort(function (a, b) {
      var aScore = a && a.latest ? toNumber(a.latest.risk_score) : null;
      var bScore = b && b.latest ? toNumber(b.latest.risk_score) : null;
      if (aScore === null && bScore === null) return String(a.zone_id).localeCompare(String(b.zone_id));
      if (aScore === null) return 1;
      if (bScore === null) return -1;
      if (bScore !== aScore) return bScore - aScore;
      return String(a.zone_id).localeCompare(String(b.zone_id));
    });
    return list;
  }

  function liveCount(zones) {
    return (zones || []).filter(function (zone) {
      return zone && zone.status === 'live' && zone.latest;
    }).length;
  }

  /* --------------------------------------------------------------- markup --- */

  function renderMetricRow(zone, metric) {
    var value = (zone.latest && zone.latest.readings) ? zone.latest.readings[metric] : null;
    var scale = metricScale(metric, value, zone.thresholds || {});
    var contribution = contributionMap(zone)[metric];
    var contributionText = contribution ? formatScore(contribution.contribution) : '–';
    // a zone with no reading yet (or without thresholds) has no measurable position:
    // show an empty bar rather than guessing a value
    var valuePct = scale.valuePct === null ? 0 : scale.valuePct;
    var watchPct = scale.watchPct === null ? 0 : scale.watchPct;
    var criticalPct = scale.criticalPct === null ? 0 : scale.criticalPct;
    return (
      '<li class="metric" data-metric="' + escapeHtml(metric) + '">' +
      '<span class="metric-name">' + escapeHtml(METRIC_META[metric].label) + '</span>' +
      '<span class="metric-value" data-f="metric-value">' + escapeHtml(formatValueWithUnit(metric, value)) + '</span>' +
      '<span class="metric-track" aria-hidden="true">' +
      '<i class="metric-fill" data-f="metric-fill" style="--pct:' + valuePct.toFixed(1) + '"></i>' +
      '<i class="metric-tick watch" style="--pct:' + watchPct.toFixed(1) + '"></i>' +
      '<i class="metric-tick critical" style="--pct:' + criticalPct.toFixed(1) + '"></i>' +
      '</span>' +
      '<span class="metric-weight" title="weighted contribution to the heuristic score">' +
      escapeHtml(contributionText) +
      '</span>' +
      '</li>'
    );
  }

  function renderZoneCard(zone) {
    var latest = zone.latest;
    var band = bandKey(latest ? latest.risk_class : null);
    var rows = METRIC_ORDER.map(function (metric) {
      return renderMetricRow(zone, metric);
    }).join('');

    var statusBlock = latest
      ? '<div class="zone-score">' +
        '<div class="ring band-' + band + '" data-f="ring" style="--pct:' + riskPercent(latest.risk_score).toFixed(1) + '">' +
        '<span class="ring-value" data-f="risk_score">' + escapeHtml(formatScore(latest.risk_score)) + '</span>' +
        '<span class="ring-caption">heuristic score</span>' +
        '</div>' +
        '<dl class="kv">' +
        '<div><dt>Candidate</dt><dd data-f="risk_class_candidate">' + escapeHtml(classLabel(latest.risk_class_candidate)) + '</dd></div>' +
        '<div><dt>Trend</dt><dd data-f="trend" data-dir="' + escapeHtml(trendDirection(latest.trend)) + '">' + escapeHtml(trendLabel(latest.trend)) + '</dd></div>' +
        '<div><dt>Top driver</dt><dd data-f="top_driver">' + escapeHtml((latest.top_drivers || [])[0] || '–') + '</dd></div>' +
        '<div><dt>Reading #</dt><dd data-f="sequence">' + escapeHtml(latest.sequence) + '</dd></div>' +
        '</dl>' +
        '</div>'
      : '<div class="zone-empty"><p>Awaiting the first simulated reading for this zone.</p>' +
        '<p class="muted">Thresholds below are the configured zone limits.</p></div>';

    return (
      '<article class="card zone-card band-' + band + (latest ? '' : ' is-empty') + '" data-zone="' + escapeHtml(zone.zone_id) + '">' +
      '<header class="zone-head">' +
      '<div class="zone-title">' +
      '<h3>' + escapeHtml(zone.name) + '</h3>' +
      '<p class="zone-sub"><span data-f="sensor_node_id">' + escapeHtml(latest ? latest.sensor_node_id : 'no node') + '</span>' +
      '<span class="dot">·</span><span data-f="age">' + escapeHtml(latest ? ageLabel(latest.age_seconds) : 'no data') + '</span></p>' +
      '</div>' +
      '<span class="pill band-' + band + '" data-f="risk_class">' + escapeHtml(classLabel(latest ? latest.risk_class : null)) + '</span>' +
      '</header>' +
      statusBlock +
      '<ul class="metrics" data-f="metrics">' + rows + '</ul>' +
      (latest
        ? '<div class="spark-row">' +
          '<canvas class="spark" data-f="spark" width="280" height="56" aria-label="session trend"></canvas>' +
          '<span class="spark-label">session trend from this page\'s polls</span>' +
          '</div>' +
          '<p class="explain" data-f="explanation">' + escapeHtml(latest.explanation) + '</p>'
        : '') +
      '</article>'
    );
  }

  function renderRanking(zones) {
    var ordered = ranking(zones);
    return ordered.map(function (zone, index) {
      var latest = zone.latest;
      var band = bandKey(latest ? latest.risk_class : null);
      return (
        '<li class="rank-row band-' + band + '" data-zone="' + escapeHtml(zone.zone_id) + '">' +
        '<span class="rank-index">' + (index + 1) + '</span>' +
        '<span class="rank-name">' + escapeHtml(zone.name) + '</span>' +
        '<span class="rank-track"><i style="--pct:' + (latest ? riskPercent(latest.risk_score).toFixed(1) : '0') + '"></i></span>' +
        '<span class="rank-score">' + escapeHtml(latest ? formatScore(latest.risk_score) : '–') + '</span>' +
        '<span class="rank-class band-' + band + '">' + escapeHtml(classLabel(latest ? latest.risk_class : null)) + '</span>' +
        '</li>'
      );
    }).join('');
  }

  /* ------------------------------------------------------------- sparkline --- */

  function sparklinePath(values, width, height, padding) {
    var pad = isFiniteNumber(padding) ? padding : 6;
    var points = [];
    var usable = [];
    (values || []).forEach(function (value, index) {
      var num = toNumber(value);
      if (num !== null) usable.push({ index: index, value: clamp(num, 0, 1) });
    });
    if (usable.length === 0) {
      return { points: [], path: '', min: 0, max: 0 };
    }
    var min = Math.min.apply(null, usable.map(function (p) { return p.value; }));
    var max = Math.max.apply(null, usable.map(function (p) { return p.value; }));
    var lo = Math.min(min, 0.1);
    var hi = Math.max(max, lo + 0.1);
    var count = (values || []).length;
    usable.forEach(function (point) {
      var x = count <= 1 ? width / 2 : pad + (point.index / (count - 1)) * (width - pad * 2);
      var y = height - pad - ((point.value - lo) / (hi - lo)) * (height - pad * 2);
      points.push({ x: Math.round(x * 100) / 100, y: Math.round(y * 100) / 100, value: point.value });
    });
    return {
      points: points,
      path: points.map(function (p, index) {
        return (index === 0 ? 'M' : 'L') + p.x + ' ' + p.y;
      }).join(' '),
      min: lo,
      max: hi
    };
  }

  function historySeries(points) {
    var list = (points || []).slice();
    return {
      values: list.map(function (point) { return toNumber(point.risk_score); }),
      classes: list.map(function (point) { return point.risk_class; }),
      labels: list.map(function (point) { return point.timestamp; }),
      epochs: list.map(function (point) { return toNumber(point.ts_epoch); })
    };
  }

  var api = {
    BANDS: BANDS,
    METRIC_ORDER: METRIC_ORDER,
    METRIC_META: METRIC_META,
    LIVE_MS: LIVE_MS,
    STALE_MS: STALE_MS,
    escapeHtml: escapeHtml,
    bandKey: bandKey,
    classLabel: classLabel,
    classRank: classRank,
    formatScore: formatScore,
    riskPercent: riskPercent,
    formatMetric: formatMetric,
    formatValueWithUnit: formatValueWithUnit,
    ageLabel: ageLabel,
    clockLabel: clockLabel,
    trendLabel: trendLabel,
    trendDirection: trendDirection,
    metricsFrom: metricsFrom,
    metricScale: metricScale,
    contributionMap: contributionMap,
    contributionPercent: contributionPercent,
    connectionState: connectionState,
    ranking: ranking,
    liveCount: liveCount,
    renderMetricRow: renderMetricRow,
    renderZoneCard: renderZoneCard,
    renderRanking: renderRanking,
    sparklinePath: sparklinePath,
    historySeries: historySeries,
    toNumber: toNumber
  };

  root.FP = api;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;  // convenience for node-based checks
  }
})(typeof window !== 'undefined' ? window : this);
