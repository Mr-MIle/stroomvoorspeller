/**
 * stroomvoorspeller.nl — embed widget v1.0
 *
 * Gebruik:
 *   <div id="sv-widget"></div>
 *   <script src="https://stroomvoorspeller.nl/widget.js"></script>
 *
 * Of met data-attributen voor aanpassing:
 *   <div id="sv-widget"
 *        data-supplier="frank"
 *        data-theme="dark"
 *        data-lang="nl"></div>
 *
 * Ondersteunde data-attributen:
 *   data-supplier  — aanbieder-id uit config.json (default: "average")
 *                    opties: average, tibber, frank, energyzero, anwb,
 *                            vandebron, zonneplan, easy
 *   data-theme     — "light" (default) of "dark"
 *   data-lang      — "nl" (default) of "en"
 *   data-compact   — "true" voor compacte weergave zonder slimste uren
 */
(function () {
  'use strict';

  var DATA_URL = 'https://stroomvoorspeller.nl/data/prices.json';
  var CONFIG_URL = 'https://stroomvoorspeller.nl/data/config.json';
  var SITE_URL = 'https://stroomvoorspeller.nl';
  var VERSION = '1.0';

  // ── Vertalingen ──────────────────────────────────────────────────────────
  var i18n = {
    nl: {
      now: 'Nu',
      cheapest: 'Goedkoopst vandaag',
      cheapest_upcoming: 'Goedkoopste komende uren',
      at: 'om',
      source: 'Bron',
      negative: 'Gratis (negatief)',
      very_cheap: 'Heel goedkoop',
      cheap: 'Goedkoop',
      normal: 'Normaal',
      pricey: 'Duur',
      very_pricey: 'Heel duur',
      no_data: 'Geen data beschikbaar',
      loading: 'Laden…',
      per_kwh: '/kWh',
      tomorrow_available: 'Morgen ook beschikbaar',
    },
    en: {
      now: 'Now',
      cheapest: 'Cheapest today',
      cheapest_upcoming: 'Cheapest upcoming hours',
      at: 'at',
      source: 'Source',
      negative: 'Free (negative)',
      very_cheap: 'Very cheap',
      cheap: 'Cheap',
      normal: 'Normal',
      pricey: 'Expensive',
      very_pricey: 'Very expensive',
      no_data: 'No data available',
      loading: 'Loading…',
      per_kwh: '/kWh',
      tomorrow_available: 'Tomorrow also available',
    }
  };

  // ── Kleuren per classificatie ─────────────────────────────────────────────
  var COLORS = {
    negative:   { bg: '#f0ebff', border: '#7048e8', text: '#4c2db5', dot: '#7048e8' },
    very_cheap: { bg: '#e8f8ec', border: '#22c55e', text: '#15803d', dot: '#22c55e' },
    cheap:      { bg: '#f0fdf4', border: '#86efac', text: '#15803d', dot: '#4ade80' },
    normal:     { bg: '#f8f9fa', border: '#dee2e6', text: '#374151', dot: '#9ca3af' },
    pricey:     { bg: '#fff8ed', border: '#fb923c', text: '#c2410c', dot: '#fb923c' },
    very_pricey:{ bg: '#fff0f0', border: '#f87171', text: '#b91c1c', dot: '#f87171' },
  };

  var COLORS_DARK = {
    negative:   { bg: '#2d1f55', border: '#7048e8', text: '#c4b5fd', dot: '#7048e8' },
    very_cheap: { bg: '#14391f', border: '#22c55e', text: '#86efac', dot: '#22c55e' },
    cheap:      { bg: '#052e16', border: '#4ade80', text: '#86efac', dot: '#4ade80' },
    normal:     { bg: '#1f2937', border: '#4b5563', text: '#d1d5db', dot: '#6b7280' },
    pricey:     { bg: '#431407', border: '#fb923c', text: '#fdba74', dot: '#fb923c' },
    very_pricey:{ bg: '#450a0a', border: '#f87171', text: '#fca5a5', dot: '#f87171' },
  };

  // ── Helpers ───────────────────────────────────────────────────────────────

  function classify(inclCt) {
    if (inclCt < 0)  return 'negative';
    if (inclCt < 14) return 'very_cheap';
    if (inclCt < 22) return 'cheap';
    if (inclCt < 28) return 'normal';
    if (inclCt < 38) return 'pricey';
    return 'very_pricey';
  }

  function toInclCt(epexEurMwh, markup, taxes) {
    return (epexEurMwh / 1000 + markup + taxes.energiebelasting_per_kwh) * taxes.btw_factor * 100;
  }

  function fmt(ct) {
    return ct.toFixed(1) + ' ct';
  }

  function nowUtcHour() {
    var d = new Date();
    return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), d.getUTCHours()));
  }

  function parseHour(str) {
    return new Date(str.replace('Z', '+00:00'));
  }

  function sameDay(a, b) {
    return a.getUTCFullYear() === b.getUTCFullYear() &&
           a.getUTCMonth()    === b.getUTCMonth() &&
           a.getUTCDate()     === b.getUTCDate();
  }

  function formatHour(dt) {
    return dt.getUTCHours() + ':00';
  }

  // ── Injecteer scoped CSS ──────────────────────────────────────────────────

  function injectStyles() {
    if (document.getElementById('sv-widget-styles')) return;
    var css = [
      '.sv-widget{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;line-height:1.4;border-radius:10px;border:1.5px solid #dee2e6;overflow:hidden;max-width:320px;box-shadow:0 1px 4px rgba(0,0,0,.07);}',
      '.sv-widget *{box-sizing:border-box;margin:0;padding:0;}',
      '.sv-widget a{color:inherit;text-decoration:none;}',
      '.sv-widget a:hover{text-decoration:underline;}',
      '.sv-header{display:flex;align-items:center;justify-content:space-between;padding:10px 12px 8px;border-bottom:1px solid rgba(0,0,0,.06);}',
      '.sv-header-left{display:flex;align-items:center;gap:6px;}',
      '.sv-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;}',
      '.sv-label{font-size:11px;font-weight:600;letter-spacing:.03em;text-transform:uppercase;opacity:.7;}',
      '.sv-body{padding:12px;}',
      '.sv-price-row{display:flex;align-items:baseline;gap:4px;margin-bottom:4px;}',
      '.sv-price{font-size:28px;font-weight:700;line-height:1;}',
      '.sv-unit{font-size:12px;opacity:.6;}',
      '.sv-status{font-size:12px;font-weight:600;margin-bottom:10px;}',
      '.sv-divider{height:1px;background:rgba(0,0,0,.06);margin:8px 0;}',
      '.sv-row{display:flex;justify-content:space-between;align-items:center;font-size:12px;padding:2px 0;}',
      '.sv-row-label{opacity:.65;}',
      '.sv-row-value{font-weight:600;}',
      '.sv-footer{padding:7px 12px;border-top:1px solid rgba(0,0,0,.06);display:flex;justify-content:space-between;align-items:center;}',
      '.sv-source{font-size:10px;opacity:.5;}',
      '.sv-source a{opacity:1;}',
      '.sv-loading{padding:16px 12px;text-align:center;font-size:12px;opacity:.5;}',
      /* dark */
      '.sv-widget.sv-dark{border-color:#374151;}',
      '.sv-widget.sv-dark .sv-header{border-bottom-color:rgba(255,255,255,.06);}',
      '.sv-widget.sv-dark .sv-divider{background:rgba(255,255,255,.06);}',
      '.sv-widget.sv-dark .sv-footer{border-top-color:rgba(255,255,255,.06);}',
    ].join('');
    var style = document.createElement('style');
    style.id = 'sv-widget-styles';
    style.textContent = css;
    document.head.appendChild(style);
  }

  // ── Render ────────────────────────────────────────────────────────────────

  function render(container, prices, config, opts) {
    var lang   = opts.lang || 'nl';
    var theme  = opts.theme || 'light';
    var compact = opts.compact === 'true' || opts.compact === true;
    var supplierId = opts.supplier || 'average';
    var t = i18n[lang] || i18n.nl;

    // Lookup supplier markup
    var markup = 0.021; // fallback: gemiddelde
    var suppliers = config.suppliers || {};
    if (suppliers[supplierId] && suppliers[supplierId].markup_per_kwh !== undefined) {
      markup = suppliers[supplierId].markup_per_kwh;
    }

    var taxes = config.taxes || {
      energiebelasting_per_kwh: 0.0916,
      btw_factor: 1.21
    };

    // Verwerk uurdata
    var now = nowUtcHour();
    var today = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));

    var allPrices = prices.prices || [];
    var currentEntry = null;
    var todayEntries = [];
    var upcomingEntries = [];

    for (var i = 0; i < allPrices.length; i++) {
      var entry = allPrices[i];
      var dt = parseHour(entry.hour || entry.time || entry.timestamp || '');
      if (!dt || isNaN(dt.getTime())) continue;
      var inclCt = toInclCt(entry.epex || entry.epex_eur_mwh || 0, markup, taxes);
      entry._ct = inclCt;
      entry._dt = dt;

      if (dt.getTime() === now.getTime()) currentEntry = entry;
      if (sameDay(dt, today)) todayEntries.push(entry);
      if (dt >= now) upcomingEntries.push(entry);
    }

    // Goedkoopste uur vandaag
    var cheapestToday = todayEntries.slice().sort(function (a, b) { return a._ct - b._ct; })[0];
    // Goedkoopste komende 6 uur (exclusief het huidige uur)
    var next6 = upcomingEntries.slice(1, 7).sort(function (a, b) { return a._ct - b._ct; })[0];

    var currentCt = currentEntry ? currentEntry._ct : null;
    var bucket = currentCt !== null ? classify(currentCt) : 'normal';
    var palette = (theme === 'dark' ? COLORS_DARK : COLORS)[bucket];
    var statusLabel = t[bucket] || t.normal;

    // Bouw HTML
    var html = '';

    if (currentCt === null) {
      html = '<div class="sv-loading">' + t.no_data + '</div>';
    } else {
      html += '<div class="sv-header" style="background:' + palette.bg + ';color:' + palette.text + '">';
      html +=   '<div class="sv-header-left">';
      html +=     '<span class="sv-dot" style="background:' + palette.dot + '"></span>';
      html +=     '<span class="sv-label">' + t.now + '</span>';
      html +=   '</div>';
      html +=   '<span class="sv-label" style="font-size:10px;opacity:.55">' + formatHour(now) + 'u</span>';
      html += '</div>';

      html += '<div class="sv-body" style="background:' + palette.bg + ';color:' + palette.text + '">';
      html +=   '<div class="sv-price-row">';
      html +=     '<span class="sv-price">' + fmt(currentCt) + '</span>';
      html +=     '<span class="sv-unit">' + t.per_kwh + '</span>';
      html +=   '</div>';
      html +=   '<div class="sv-status">' + statusLabel + '</div>';

      if (!compact) {
        html += '<div class="sv-divider"></div>';

        if (cheapestToday) {
          html += '<div class="sv-row">';
          html +=   '<span class="sv-row-label">' + t.cheapest + '</span>';
          html +=   '<span class="sv-row-value">' + fmt(cheapestToday._ct) + ' ' + t.at + ' ' + formatHour(cheapestToday._dt) + 'u</span>';
          html += '</div>';
        }

        if (next6 && next6._dt.getTime() !== now.getTime()) {
          html += '<div class="sv-row">';
          html +=   '<span class="sv-row-label">' + t.cheapest_upcoming + '</span>';
          html +=   '<span class="sv-row-value">' + fmt(next6._ct) + ' ' + t.at + ' ' + formatHour(next6._dt) + 'u</span>';
          html += '</div>';
        }
      }

      html += '</div>';
    }

    html += '<div class="sv-footer">';
    html +=   '<span class="sv-source">' + t.source + ': <a href="' + SITE_URL + '?utm_source=widget&utm_medium=embed&utm_campaign=sv-widget" target="_blank" rel="noopener">stroomvoorspeller.nl</a></span>';
    html +=   '<span class="sv-source">v' + VERSION + '</span>';
    html += '</div>';

    container.innerHTML = html;
    container.className = 'sv-widget' + (theme === 'dark' ? ' sv-dark' : '');
    container.style.borderColor = palette.border;
  }

  // ── Initialiseer ──────────────────────────────────────────────────────────

  function init() {
    var container = document.getElementById('sv-widget');
    if (!container) return;

    var opts = {
      supplier: container.getAttribute('data-supplier') || 'average',
      theme:    container.getAttribute('data-theme')    || 'light',
      lang:     container.getAttribute('data-lang')     || 'nl',
      compact:  container.getAttribute('data-compact')  || 'false',
    };

    injectStyles();
    container.innerHTML = '<div class="sv-loading">Laden…</div>';
    container.className = 'sv-widget';

    // Laad prices.json en config.json parallel
    Promise.all([
      fetch(DATA_URL).then(function (r) { return r.json(); }),
      fetch(CONFIG_URL).then(function (r) { return r.json(); }),
    ])
    .then(function (results) {
      render(container, results[0], results[1], opts);
    })
    .catch(function () {
      var t = i18n[opts.lang] || i18n.nl;
      container.innerHTML = '<div class="sv-loading">' + t.no_data + '</div>';
    });
  }

  // Start na DOM-ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
