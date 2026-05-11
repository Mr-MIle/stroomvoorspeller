// morgen.js — Stroomvoorspeller.nl — dedicated pagina: stroomprijs morgen
// Laadt config.json + prices.json (+ optioneel forecast.json) en rendert alle
// morgen-specifieke secties: highlights, bar-chart, slimste momenten,
// per-aanbieder-tabel, waarom-sectie en dynamische FAQ.

(function () {
  "use strict";

  const STORAGE_KEYS = {
    mode:         "sv.viewMode",
    supplier:     "sv.supplierId",
    customMarkup: "sv.customMarkup",
  };

  const state = {
    config:            null,
    tomorrowPrices:    [],   // {time, price} uurdata voor morgen
    tomorrowPrices15m: [],   // {time, price} kwartierdata voor morgen (PT15M)
    todayPrices:       [],   // {time, price} voor vandaag (vergelijking)
    tomorrowForecasts: [],   // forecast-entries voor morgen (optioneel)
    hasPt15m:          false,
    chartRes:          "hourly",   // 'hourly' | 'quarter'
    mode:              "inclusive",
    supplierId:        "average",
    customMarkup:      0.025,
    chart:             null,
    tomorrowDate:      null,
  };

  // ---- Storage ----
  function loadStored(key, fallback) {
    try { const v = localStorage.getItem(key); return v == null ? fallback : v; }
    catch (e) { return fallback; }
  }

  // ---- Leverancier ----
  function getSupplier() {
    const list = (state.config && state.config.suppliers) || [];
    return list.find(s => s.id === state.supplierId) || list[0] || { markup_per_kwh: 0.025 };
  }
  function effectiveMarkup() {
    const s = getSupplier();
    if (s.id === "custom") {
      const n = Number(state.customMarkup);
      return Number.isFinite(n) && n >= 0 ? n : 0;
    }
    return Number(s.markup_per_kwh) || 0;
  }

  // ---- Prijsberekening ----
  function priceCents(eurMwh, mode) {
    mode = mode || state.mode;
    const epex = eurMwh / 1000;
    if (mode === "exclusive") return (epex + effectiveMarkup()) * 100;
    const t = state.config.taxes;
    return (epex + effectiveMarkup() + t.energiebelasting_per_kwh) * t.btw_factor * 100;
  }
  function priceCentsForSupplier(eurMwh, supplier) {
    const epex = eurMwh / 1000;
    const t = state.config.taxes;
    return (epex + (Number(supplier.markup_per_kwh) || 0) + t.energiebelasting_per_kwh) * t.btw_factor * 100;
  }
  function priceCentsRaw(eurMwh) { return (eurMwh / 1000) * 100; }

  function classify(eurMwh) {
    const ct = priceCents(eurMwh, "inclusive");
    const t = state.config.thresholds_ct_kwh_inclusive || {};
    if (ct < 0)                          return "negative";
    if (ct < (t.very_cheap ?? 14))       return "very_cheap";
    if (ct < (t.cheap      ?? 22))       return "cheap";
    if (ct > (t.very_pricey ?? 38))      return "very_pricey";
    if (ct > (t.pricey      ?? 28))      return "pricey";
    return "normal";
  }

  const CLASS_COLOR = {
    negative:   "#7048e8",
    very_cheap: "#1a7a31",
    cheap:      "#2f9e44",
    normal:     "#d4a017",
    pricey:     "#c92a2a",
    very_pricey:"#9c1a1a",
  };
  const CLASS_LABEL = {
    negative:   "Negatief / gratis",
    very_cheap: "Uitstekend goedkoop",
    cheap:      "Goedkoop",
    normal:     "Normaal",
    pricey:     "Duur",
    very_pricey:"Extreem duur",
  };

  // ---- Format ----
  function fmtNum(v, d) {
    return Number(v).toLocaleString("nl-NL", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function fmtCt(eurMwh, d)  { return fmtNum(priceCents(eurMwh), d == null ? 1 : d); }
  function fmtTime(iso) {
    return new Date(iso).toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
  }
  function endTime(iso) {
    return new Date(new Date(iso).getTime() + 3_600_000)
      .toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
  }
  function modeLabel() { return state.mode === "exclusive" ? "excl. belasting" : "incl. belasting"; }

  // ---- Datum ----
  function isSameLocalDay(iso, date) {
    const d = new Date(iso);
    return d.getFullYear() === date.getFullYear() &&
           d.getMonth()    === date.getMonth()    &&
           d.getDate()     === date.getDate();
  }
  function getTomorrow() {
    const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(0, 0, 0, 0); return d;
  }
  function getToday() {
    const d = new Date(); d.setHours(0, 0, 0, 0); return d;
  }

  // ---- Beste vensters ----
  function findBestMoments(prices, count, windowHours) {
    count = count || 3; windowHours = windowHours || 2;
    const candidates = [];
    for (let i = 0; i <= prices.length - windowHours; i++) {
      let sum = 0;
      for (let k = 0; k < windowHours; k++) sum += prices[i + k].price;
      candidates.push({ start: i, avg: sum / windowHours });
    }
    candidates.sort((a, b) => a.avg - b.avg);
    const chosen = [], used = new Set();
    for (const c of candidates) {
      let overlap = false;
      for (let k = 0; k < windowHours; k++) { if (used.has(c.start + k)) { overlap = true; break; } }
      if (overlap) continue;
      for (let k = 0; k < windowHours; k++) used.add(c.start + k);
      chosen.push(c);
      if (chosen.length >= count) break;
    }
    return chosen.map(c => ({
      startIso: prices[c.start].time,
      endIso:   prices[c.start + windowHours - 1].time,
      avg:      c.avg,
    }));
  }

  // ---- Fetch ----
  async function fetchJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
    return res.json();
  }

  // ---- DOM helpers ----
  function setText(id, txt) { const el = document.getElementById(id); if (el) el.textContent = txt; }
  function setHTML(id, html) { const el = document.getElementById(id); if (el) el.innerHTML = html; }

  // ---- No-data state ----
  function showNoDataState() {
    setHTML("morgen-content", `
      <div class="morgen-no-data container">
        <p class="morgen-no-data-icon" aria-hidden="true">⏳</p>
        <h2>Morgen-prijzen nog niet beschikbaar</h2>
        <p>De day-ahead prijzen voor morgen worden elke dag rond <strong>13:00 uur</strong>
           gepubliceerd door ENTSO-E. Kom dan terug voor het volledige overzicht.</p>
        <p><a href="/">← Bekijk de huidige stroomprijzen voor vandaag</a></p>
      </div>`);
  }

  // ---- Render: datumlabels ----
  function renderDateLabels() {
    const d = state.tomorrowDate;
    const lang = d.toLocaleDateString("nl-NL", { weekday: "long", day: "numeric", month: "long" });
    const kort = d.toLocaleDateString("nl-NL", { day: "numeric", month: "long", year: "numeric" });
    setText("morgen-h1", `Stroomprijs morgen — ${lang}`);
    document.title = `Stroomprijs morgen ${kort} — alle 24 uren | Stroomvoorspeller.nl`;

    const descEl = document.querySelector('meta[name="description"]');
    if (descEl && state.tomorrowPrices.length) {
      const cheapest = state.tomorrowPrices.reduce((a, b) => a.price <= b.price ? a : b);
      descEl.content =
        `Stroomprijs per uur voor morgen, ${kort}. Goedkoopste uur: ` +
        `${fmtNum(priceCents(cheapest.price, "inclusive"), 1)} ct/kWh om ${fmtTime(cheapest.time)}. ` +
        `Bekijk wanneer stroom morgen goedkoop is voor je wasmachine, EV of warmtepomp.`;
    }
  }

  // ---- Render: mode toggle ----
  function renderModeToggle() {
    document.querySelectorAll("[data-mode-btn]").forEach(btn => {
      const active = btn.dataset.modeBtn === state.mode;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-pressed", String(active));
    });
    document.querySelectorAll("[data-mode-label]").forEach(el => el.textContent = modeLabel());
  }

  // ---- Render: highlight cards ----
  function renderHighlights() {
    const prices = state.tomorrowPrices;
    const cheapest = prices.reduce((a, b) => a.price <= b.price ? a : b);
    const priciest = prices.reduce((a, b) => a.price >= b.price ? a : b);
    const avg      = prices.reduce((s, p) => s + p.price, 0) / prices.length;
    const negHours = prices.filter(p => priceCents(p.price, "inclusive") < 0);

    setText("hl-cheapest-val",  `${fmtCt(cheapest.price)} ct`);
    setText("hl-cheapest-time", `om ${fmtTime(cheapest.time)}`);
    setText("hl-priciest-val",  `${fmtCt(priciest.price)} ct`);
    setText("hl-priciest-time", `om ${fmtTime(priciest.time)}`);
    setText("hl-avg-val",       `${fmtCt(avg)} ct/kWh`);

    const negEl = document.getElementById("hl-neg-val");
    const negCard = document.getElementById("hl-neg-card");
    if (negEl) {
      if (negHours.length > 0) {
        negEl.textContent = `${negHours.length} uur${negHours.length > 1 ? "" : ""}`;
        if (negCard) negCard.classList.add("is-free");
      } else {
        negEl.textContent = "Geen";
      }
    }

    // Vergelijking met vandaag
    const cmpEl = document.getElementById("morgen-compare");
    if (cmpEl && state.todayPrices.length) {
      const todayAvg = state.todayPrices.reduce((s, p) => s + p.price, 0) / state.todayPrices.length;
      const diffCt = priceCents(avg, "inclusive") - priceCents(todayAvg, "inclusive");
      const diffPct = Math.abs(diffCt / priceCents(todayAvg, "inclusive") * 100);
      if (Math.abs(diffCt) > 0.5) {
        const sign = diffCt < 0 ? "−" : "+";
        const word = diffCt < 0 ? "lager" : "hoger";
        cmpEl.innerHTML = `Morgen gemiddeld <strong>${sign}${fmtNum(diffPct, 0)}%</strong> ${word} dan vandaag`;
        cmpEl.className = `morgen-compare ${diffCt < 0 ? "is-cheaper" : "is-pricier"}`;
      } else {
        cmpEl.textContent = "Vergelijkbaar met vandaag";
        cmpEl.className = "morgen-compare";
      }
    }
  }

  // ---- Render: bar chart ----
  function renderChart() {
    const canvas = document.getElementById("morgenChart");
    if (!canvas || !window.Chart) return;

    const isQuarter = state.chartRes === "quarter" && state.tomorrowPrices15m.length > 0;
    const prices  = isQuarter ? state.tomorrowPrices15m : state.tomorrowPrices;
    const labels  = prices.map(p => fmtTime(p.time));
    const values  = prices.map(p => priceCents(p.price));
    const classes = prices.map(p => classify(p.price));
    const colors  = classes.map(c => CLASS_COLOR[c] || "#d4a017");
    const intervalMs = isQuarter ? 900_000 : 3_600_000;

    if (state.chart) { state.chart.destroy(); state.chart = null; }

    state.chart = new Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels,
        datasets: [{
          label: `ct/kWh (${modeLabel()})`,
          data: values,
          backgroundColor: colors.map(c => c + "bb"),
          borderColor: colors,
          borderWidth: isQuarter ? 0.5 : 1.5,
          borderRadius: isQuarter ? 1 : 4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: items => {
                const i = items[0].dataIndex;
                const end = new Date(new Date(prices[i].time).getTime() + intervalMs);
                return `${labels[i]}–${end.toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" })}`;
              },
              label: item => {
                const i   = item.dataIndex;
                const p   = prices[i];
                const cls = classes[i];
                return [
                  CLASS_LABEL[cls] || cls,
                  `Incl. belasting: ${fmtNum(priceCents(p.price, "inclusive"), 2)} ct/kWh`,
                  `Excl. belasting: ${fmtNum(priceCents(p.price, "exclusive"), 2)} ct/kWh`,
                  `Kale EPEX: ${fmtNum(priceCentsRaw(p.price), 2)} ct/kWh`,
                ];
              },
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: {
              font: { size: 11 },
              maxRotation: 0,
              // Kwartier-modus: toon alleen uurlabels (elke 4e tick = elk heel uur)
              callback: isQuarter
                ? function(val, idx) { return idx % 4 === 0 ? this.getLabelForValue(val) : ""; }
                : undefined,
            },
          },
          y: {
            title: { display: true, text: `ct/kWh (${modeLabel()})` },
            ticks: { callback: v => fmtNum(v, 0) + " ct" },
          },
        },
      },
    });
  }

  // ---- Render: slimste momenten ----
  function renderMoments() {
    const moments = findBestMoments(state.tomorrowPrices, 3, 2);
    const list = document.getElementById("morgen-moments-list");
    if (!list) return;
    list.innerHTML = "";
    moments.forEach((m, i) => {
      const ct   = priceCents(m.avg, "inclusive");
      const isNeg = ct <= 0;
      const li   = document.createElement("li");
      li.className = `moment${isNeg ? " is-negative" : ""}`;
      const badge = isNeg ? `<span class="moment-badge-free">⚡ gratis</span>` : "";
      li.innerHTML = `
        <span class="moment-rank">${i + 1}</span>
        <span class="moment-when">${fmtTime(m.startIso)}–${endTime(m.endIso)}<small> (2 uur)</small></span>
        <span class="moment-price">${fmtNum(ct, 1)} ct/kWh${badge}</span>`;
      list.appendChild(li);
    });

    // Tip-box met het beste venster
    const tipEl = document.getElementById("morgen-best-tip");
    if (tipEl && moments.length) {
      const best = moments[0];
      const bestCt = fmtNum(priceCents(best.avg, "inclusive"), 1);
      tipEl.innerHTML = `<strong>Beste moment morgen: ${fmtTime(best.startIso)}–${endTime(best.endIso)}</strong>
        &nbsp;(${bestCt} ct/kWh)&nbsp;— ideaal voor wasmachine, droger, EV-laden of warmtepomp.`;
    }
  }

  // ---- Render: aanbieder-tabel ----
  function renderSupplierTable() {
    const tbody = document.getElementById("morgen-suppliers-tbody");
    if (!tbody) return;
    const prices = state.tomorrowPrices;
    if (!prices.length) return;

    // Daggemiddelde EPEX als grondslag voor per-aanbieder-vergelijking
    const avgEpex = prices.reduce((s, p) => s + p.price, 0) / prices.length;

    const rows = (state.config.suppliers || [])
      .filter(s => s.id !== "custom")
      .map(s => ({ s, avgCt: priceCentsForSupplier(avgEpex, s) }))
      .sort((a, b) => a.avgCt - b.avgCt);

    tbody.innerHTML = "";
    rows.forEach(({ s, avgCt }) => {
      const tr = document.createElement("tr");
      if (s.id === state.supplierId) tr.className = "is-mine";

      const tdName = document.createElement("td");
      tdName.className = "td-supplier";
      if (s.website) {
        const a = document.createElement("a");
        a.href = s.website; a.target = "_blank"; a.rel = "noopener noreferrer";
        a.textContent = s.name; tdName.appendChild(a);
      } else { tdName.textContent = s.name; }
      if (s.id === state.supplierId) {
        const badge = document.createElement("span");
        badge.className = "supplier-mine-badge"; badge.textContent = "jouw keuze";
        tdName.appendChild(badge);
      }

      const tdPrice = document.createElement("td");
      tdPrice.className = "td-price morgen-td-avg";
      tdPrice.textContent = fmtNum(avgCt, 1);

      const tdMarkup = document.createElement("td");
      tdMarkup.className = "td-markup";
      tdMarkup.textContent = `€${fmtNum(s.markup_per_kwh, 4)}`;

      const tdFixed = document.createElement("td");
      tdFixed.className = "td-fixed";
      tdFixed.textContent = s.fixed_per_month > 0 ? `€${fmtNum(s.fixed_per_month, 2)}` : "—";

      tr.append(tdName, tdPrice, tdMarkup, tdFixed);
      tbody.appendChild(tr);
    });
  }

  // ---- Render: "Morgen samengevat" prose-box (hero) ----
  function renderSummary() {
    const el = document.getElementById("morgen-summary-box");
    if (!el || !state.tomorrowPrices.length) return;

    const prices   = state.tomorrowPrices;
    const avgEpex  = prices.reduce((s, p) => s + p.price, 0) / prices.length;
    const avgCt    = priceCents(avgEpex, "inclusive");
    const negHours = prices.filter(p => priceCents(p.price, "inclusive") < 0);
    const cheapHrs = prices.filter(p => { const c = classify(p.price); return c === "cheap" || c === "very_cheap"; });
    const valley   = prices.reduce((a, b) => a.price <= b.price ? a : b);
    const peak     = prices.reduce((a, b) => a.price >= b.price ? a : b);
    const cls      = classify(avgEpex);
    const bestM    = findBestMoments(prices, 1, 2)[0];

    const levelZin = {
      negative:   `Morgen worden de stroomprijzen uitzonderlijk laag — met negatieve uren.`,
      very_cheap: `Morgen zijn de stroomprijzen uitstekend laag.`,
      cheap:      `Morgen zijn de stroomprijzen relatief laag.`,
      normal:     `Morgen liggen de stroomprijzen op een normaal niveau.`,
      pricey:     `Morgen zijn de stroomprijzen aan de hoge kant.`,
      very_pricey:`Morgen zijn de stroomprijzen hoog.`,
    }[cls] || `Morgen liggen de stroomprijzen op een normaal niveau.`;

    let parts = [
      `${levelZin} Het daggemiddelde bedraagt <strong>${fmtNum(avgCt, 1)} ct/kWh</strong> all-in.`,
    ];

    if (negHours.length > 0) {
      parts.push(`Bijzonder: <strong>${negHours.length} uur${negHours.length > 1 ? "en" : ""} met negatieve prijs</strong>
        (${fmtTime(negHours[0].time)}–${endTime(negHours[negHours.length - 1].time)}) — stroom is dan gratis.`);
    } else if (cheapHrs.length > 0) {
      parts.push(`Er zijn <strong>${cheapHrs.length} goedkope uren</strong> (onder 22 ct/kWh),
        waarvan het goedkoopste om <strong>${fmtTime(valley.time)}</strong>
        (${fmtNum(priceCents(valley.price, "inclusive"), 1)} ct/kWh).`);
    }

    if (bestM) {
      parts.push(`Het beste 2-uurs venster is van <strong>${fmtTime(bestM.startIso)} tot ${endTime(bestM.endIso)}</strong>
        (gem. ${fmtNum(priceCents(bestM.avg, "inclusive"), 1)} ct/kWh)
        — ideaal voor EV laden, wasmachine of warmtepomp.`);
    }

    if (priceCents(peak.price, "inclusive") > 28) {
      parts.push(`Het duurste uur is om <strong>${fmtTime(peak.time)}</strong>
        (${fmtNum(priceCents(peak.price, "inclusive"), 1)} ct/kWh) — zware apparaten dan liever uitstellen.`);
    }

    el.className = `morgen-summary-box morgen-summary-${cls}`;
    el.innerHTML = parts.join(" ");
  }

  // ---- Render: vandaag vs. morgen vergelijkingstabel ----
  function renderTodayVsTomorrow() {
    const el = document.getElementById("morgen-today-vs-tomorrow");
    if (!el) return;

    if (!state.todayPrices.length) {
      const sec = document.getElementById("morgen-cmp-section");
      if (sec) sec.hidden = true;
      return;
    }

    function priceStats(prices) {
      const avg     = prices.reduce((s, p) => s + p.price, 0) / prices.length;
      const cheapest = prices.reduce((a, b) => a.price <= b.price ? a : b);
      const priciest = prices.reduce((a, b) => a.price >= b.price ? a : b);
      const negHrs  = prices.filter(p => priceCents(p.price, "inclusive") < 0).length;
      const cheapHrs = prices.filter(p => { const c = classify(p.price); return c === "cheap" || c === "very_cheap"; }).length;
      const expHrs  = prices.filter(p => { const c = classify(p.price); return c === "pricey" || c === "very_pricey"; }).length;
      const spread  = priceCents(priciest.price, "inclusive") - priceCents(cheapest.price, "inclusive");
      return { avg, cheapest, priciest, negHrs, cheapHrs, expHrs, spread };
    }

    const T = priceStats(state.todayPrices);
    const M = priceStats(state.tomorrowPrices);

    const todAvgCt = priceCents(T.avg, "inclusive");
    const tomAvgCt = priceCents(M.avg, "inclusive");
    const diffCt   = tomAvgCt - todAvgCt;
    const diffPct  = todAvgCt !== 0 ? Math.abs(diffCt / todAvgCt * 100) : 0;

    // helper: richting-pijl (groen/rood)
    function arrow(todNum, tomNum, lowerIsBetter) {
      const d = tomNum - todNum;
      if (Math.abs(d) < 0.05) return `<span class="cmp-neutral">≈</span>`;
      const better = lowerIsBetter ? d < 0 : d > 0;
      return `<span class="cmp-arrow ${better ? "cmp-better" : "cmp-worse"}">${d < 0 ? "↓" : "↑"}</span>`;
    }

    function diffStr(val, unit, lowerIsBetter) {
      if (Math.abs(val) < 0.05) return "—";
      const sign = val < 0 ? "−" : "+";
      const better = lowerIsBetter ? val < 0 : val > 0;
      return `<span class="${better ? "cmp-diff-better" : "cmp-diff-worse"}">${sign}${fmtNum(Math.abs(val), unit === "%" ? 0 : 1)} ${unit}</span>`;
    }

    const rows = [];

    // Gemiddelde
    rows.push({
      label: "Daggemiddelde",
      today: `${fmtNum(todAvgCt, 1)} ct/kWh`,
      tomorrow: `${fmtNum(tomAvgCt, 1)} ct/kWh`,
      arrow: arrow(todAvgCt, tomAvgCt, true),
      diff: diffStr(diffCt < 0 ? -diffPct : diffPct, "%", true).replace(
        fmtNum(diffPct, 0), fmtNum(diffPct, 0)
      ) || (Math.abs(diffCt) > 0.5
        ? `<span class="${diffCt < 0 ? "cmp-diff-better" : "cmp-diff-worse"}">${diffCt < 0 ? "−" : "+"}${fmtNum(diffPct, 0)}%</span>`
        : `<span class="cmp-neutral">≈</span>`),
    });

    // Negatieve uren (alleen als minstens één dag ze heeft)
    if (T.negHrs > 0 || M.negHrs > 0) {
      rows.push({
        label: "Negatieve uren ⚡",
        today:    T.negHrs > 0 ? `${T.negHrs} uur` : "geen",
        tomorrow: M.negHrs > 0 ? `${M.negHrs} uur` : "geen",
        arrow:  arrow(T.negHrs, M.negHrs, false),
        diff:   M.negHrs !== T.negHrs
          ? `<span class="${M.negHrs > T.negHrs ? "cmp-diff-better" : "cmp-diff-worse"}">${M.negHrs > T.negHrs ? "+" : ""}${M.negHrs - T.negHrs} uur</span>`
          : `<span class="cmp-neutral">≈</span>`,
      });
    }

    // Goedkoopste uur
    rows.push({
      label: "Goedkoopste uur",
      today: `${fmtTime(T.cheapest.time)} · ${fmtNum(priceCents(T.cheapest.price, "inclusive"), 1)} ct`,
      tomorrow: `${fmtTime(M.cheapest.time)} · ${fmtNum(priceCents(M.cheapest.price, "inclusive"), 1)} ct`,
      arrow: arrow(priceCents(T.cheapest.price, "inclusive"), priceCents(M.cheapest.price, "inclusive"), true),
      diff: "",
    });

    // Duurste uur
    rows.push({
      label: "Duurste uur",
      today: `${fmtTime(T.priciest.time)} · ${fmtNum(priceCents(T.priciest.price, "inclusive"), 1)} ct`,
      tomorrow: `${fmtTime(M.priciest.time)} · ${fmtNum(priceCents(M.priciest.price, "inclusive"), 1)} ct`,
      arrow: arrow(priceCents(T.priciest.price, "inclusive"), priceCents(M.priciest.price, "inclusive"), true),
      diff: "",
    });

    // Spreiding
    rows.push({
      label: "Prijsspreiding (goedkoop↔duur)",
      today:    `${fmtNum(T.spread, 1)} ct`,
      tomorrow: `${fmtNum(M.spread, 1)} ct`,
      arrow: arrow(T.spread, M.spread, true),
      diff: "",
    });

    // Goedkope uren
    rows.push({
      label: "Goedkope uren (<22 ct)",
      today:    `${T.cheapHrs} uur`,
      tomorrow: `${M.cheapHrs} uur`,
      arrow: arrow(T.cheapHrs, M.cheapHrs, false),
      diff: M.cheapHrs !== T.cheapHrs
        ? `<span class="${M.cheapHrs > T.cheapHrs ? "cmp-diff-better" : "cmp-diff-worse"}">${M.cheapHrs > T.cheapHrs ? "+" : ""}${M.cheapHrs - T.cheapHrs} uur</span>`
        : `<span class="cmp-neutral">≈</span>`,
    });

    // Dure uren
    rows.push({
      label: "Dure uren (>28 ct)",
      today:    `${T.expHrs} uur`,
      tomorrow: `${M.expHrs} uur`,
      arrow: arrow(T.expHrs, M.expHrs, true),
      diff: M.expHrs !== T.expHrs
        ? `<span class="${M.expHrs < T.expHrs ? "cmp-diff-better" : "cmp-diff-worse"}">${M.expHrs > T.expHrs ? "+" : ""}${M.expHrs - T.expHrs} uur</span>`
        : `<span class="cmp-neutral">≈</span>`,
    });

    el.innerHTML = `
      <div class="morgen-cmp-wrap">
        <table class="morgen-cmp-table">
          <thead>
            <tr>
              <th scope="col" class="cmp-th-label"></th>
              <th scope="col" class="cmp-th-today">Vandaag</th>
              <th scope="col" class="cmp-th-tomorrow">Morgen</th>
              <th scope="col" class="cmp-th-diff">Verschil</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(r => `
              <tr>
                <th scope="row" class="cmp-row-label">${r.label}</th>
                <td class="cmp-today">${r.today}</td>
                <td class="cmp-tomorrow">${r.tomorrow} ${r.arrow}</td>
                <td class="cmp-diff">${r.diff}</td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>`;
  }

  // ---- Render: prijsanalyse als nieuwsbericht ----
  function renderNewsArticle() {
    const el = document.getElementById("morgen-why-content");
    if (!el) return;

    const prices   = state.tomorrowPrices;
    const avgEpex  = prices.reduce((s, p) => s + p.price, 0) / prices.length;
    const avgCt    = priceCents(avgEpex, "inclusive");
    const negHours = prices.filter(p => priceCents(p.price, "inclusive") < 0);
    const cheapHrs = prices.filter(p => { const c = classify(p.price); return c === "cheap" || c === "very_cheap"; });
    const expHrs   = prices.filter(p => { const c = classify(p.price); return c === "pricey" || c === "very_pricey"; });
    const peak     = prices.reduce((a, b) => a.price >= b.price ? a : b);
    const valley   = prices.reduce((a, b) => a.price <= b.price ? a : b);
    const cls      = classify(avgEpex);
    const dateStr  = state.tomorrowDate.toLocaleDateString("nl-NL", { weekday: "long", day: "numeric", month: "long" });

    // — Alinea 1: openingszin + vergelijking met vandaag —
    const openingZin = {
      negative:   `Met ${negHours.length} uur${negHours.length > 1 ? "en" : ""} negatieve consumentenprijs belooft morgen, ${dateStr}, een bijzondere dag te worden op de elektriciteitsmarkt.`,
      very_cheap: `Morgen, ${dateStr}, zijn de stroomprijzen uitstekend laag — een goede dag om grote verbruikers in te plannen.`,
      cheap:      `Morgen, ${dateStr}, liggen de stroomprijzen relatief laag, wat gunstige mogelijkheden biedt voor bewust verbruik.`,
      normal:     `Morgen, ${dateStr}, zijn de stroomprijzen normaal — geen bijzondere uitslagen, maar timing blijft loont.`,
      pricey:     `Morgen, ${dateStr}, zijn de stroomprijzen aan de hoge kant. Timing van zware verbruikers wordt dan extra relevant.`,
      very_pricey:`Morgen, ${dateStr}, zijn de stroomprijzen hoog. Zware apparaten zijn het best te vermijden buiten de nachtdal.`,
    }[cls] || `Morgen, ${dateStr}, liggen de stroomprijzen op een normaal niveau.`;

    let para1 = openingZin;
    if (state.todayPrices.length) {
      const todayEpex = state.todayPrices.reduce((s, p) => s + p.price, 0) / state.todayPrices.length;
      const todayCt   = priceCents(todayEpex, "inclusive");
      const diff      = avgCt - todayCt;
      const pct       = todayCt !== 0 ? Math.abs(diff / todayCt * 100) : 0;
      if (Math.abs(diff) > 1) {
        para1 += ` Het daggemiddelde van <strong>${fmtNum(avgCt, 1)} ct/kWh</strong> is
          ${diff < 0 ? `<strong>${fmtNum(pct, 0)}% lager</strong>` : `<strong>${fmtNum(pct, 0)}% hoger</strong>`}
          dan het gemiddelde van vandaag (${fmtNum(todayCt, 1)} ct/kWh).`;
      } else {
        para1 += ` Met een daggemiddelde van <strong>${fmtNum(avgCt, 1)} ct/kWh</strong>
          is dat vrijwel gelijk aan vandaag (${fmtNum(todayCt, 1)} ct/kWh).`;
      }
    } else {
      para1 += ` Het daggemiddelde bedraagt <strong>${fmtNum(avgCt, 1)} ct/kWh</strong> all-in.`;
    }

    // — Alinea 2: goedkope uren / beste momenten —
    let para2 = "";
    const bestM = findBestMoments(prices, 1, 2)[0];
    const valCt = fmtNum(priceCents(valley.price, "inclusive"), 1);

    if (negHours.length > 0) {
      const negStart = fmtTime(negHours[0].time);
      const negEnd   = endTime(negHours[negHours.length - 1].time);
      para2 = `Bijzonder zijn de <strong>${negHours.length} uren met negatieve consumentenprijs</strong>
        van ${negStart} tot ${negEnd}. In die uren kost stroom effectief niets — sommige leveranciers
        betalen je zelfs voor afname. Het absolute dieptepunt valt om ${fmtTime(valley.time)}
        (${valCt} ct/kWh). Dit zijn uitgelezen momenten voor intensief verbruik: EV volladen,
        wasmachine, droger en warmtepomp op volle kracht.`;
    } else if (cheapHrs.length >= 2) {
      const bestCt = fmtNum(priceCents(bestM.avg, "inclusive"), 1);
      para2 = `Er zijn morgen <strong>${cheapHrs.length} goedkope uren</strong> (onder 22 ct/kWh).
        Het goedkoopste uur valt om ${fmtTime(valley.time)} (${valCt} ct/kWh). Het beste
        aaneengesloten blok van twee uur loopt van <strong>${fmtTime(bestM.startIso)}
        tot ${endTime(bestM.endIso)}</strong> met een gemiddelde van ${bestCt} ct/kWh —
        het ideale moment voor je wasmachine, EV of warmtepomp.`;
    } else {
      const bestCt = bestM ? fmtNum(priceCents(bestM.avg, "inclusive"), 1) : valCt;
      para2 = `De goedkoopste uren morgen vallen rond ${fmtTime(valley.time)} (${valCt} ct/kWh).
        Ook in een relatief duur scenario loont het om zware apparaten naar het goedkoopste
        venster te verschuiven${bestM ? ` — het beste 2-uurs blok loopt van ${fmtTime(bestM.startIso)}
        tot ${endTime(bestM.endIso)} (gem. ${bestCt} ct/kWh)` : ""}.`;
    }

    // — Alinea 3: dure uren / spreiding —
    let para3 = "";
    if (expHrs.length > 0) {
      const peakCt = fmtNum(priceCents(peak.price, "inclusive"), 1);
      const spread = fmtNum(priceCents(peak.price, "inclusive") - priceCents(valley.price, "inclusive"), 1);
      para3 = `De piek concentreert zich rond <strong>${fmtTime(peak.time)}</strong>
        (${peakCt} ct/kWh). Het prijsverschil tussen goedkoopste en duurste uur bedraagt
        <strong>${spread} ct/kWh</strong> — ruim genoeg om bewust te timen.
        Met name de droger, elektrische oven en het opladen van grote accu's zijn het
        best te vermijden in het dure venster.`;
    } else if (priceCents(peak.price, "inclusive") > 22) {
      const peakCt = fmtNum(priceCents(peak.price, "inclusive"), 1);
      const spread = fmtNum(priceCents(peak.price, "inclusive") - priceCents(valley.price, "inclusive"), 1);
      para3 = `Het duurste uur valt om ${fmtTime(peak.time)} (${peakCt} ct/kWh),
        met een spreiding van ${spread} ct/kWh ten opzichte van het goedkoopste uur.
        Geen extreme pieken, maar tijdsbewust verbruik blijft de moeite waard.`;
    }

    // — Alinea 4: forecast-factoren als verhaal —
    let para4 = "";
    if (state.tomorrowForecasts.length > 0) {
      const factorAcc = {};
      let hrs = 0;
      state.tomorrowForecasts.forEach(f => {
        hrs++;
        (f.factors || []).forEach(fc => {
          if (!factorAcc[fc.name]) factorAcc[fc.name] = { name: fc.name, pts: 0, reason: fc.reason };
          factorAcc[fc.name].pts += fc.points;
        });
      });
      const factors = Object.values(factorAcc)
        .map(f => ({ ...f, avg: Math.round(f.pts / hrs) }))
        .filter(f => Math.abs(f.avg) >= 1)
        .sort((a, b) => Math.abs(b.avg) - Math.abs(a.avg))
        .slice(0, 3);

      if (factors.length) {
        const factorZinnen = factors.map(f => {
          const richting = f.avg > 0 ? "drukt de prijs omhoog" : "helpt de prijs laag houden";
          const reden = f.reason ? f.reason.charAt(0).toLowerCase() + f.reason.slice(1) : f.name;
          return `${reden} (${richting})`;
        });
        para4 = `Volgens het <a href="/over/voorspelling">transparante voorspellingsmodel</a>
          van Stroomvoorspeller.nl spelen morgen de volgende factoren de hoofdrol:
          ${factorZinnen.join("; ")}.
          De combinatie van deze factoren verklaart het grootste deel van het prijsverloop morgen.`;
      }
    }

    // Samenstellen
    let html = `<div class="morgen-news-article">`;
    html += `<p class="morgen-news-lead">${para1}</p>`;
    if (para2) html += `<p>${para2}</p>`;
    if (para3) html += `<p>${para3}</p>`;
    if (para4) {
      html += `<p class="morgen-news-model">${para4}</p>`;
    } else {
      html += `<p class="morgen-news-model">Meer weten over hoe de prijs tot stand komt?
        Lees de uitleg over het <a href="/over/voorspelling">transparante voorspellingsmodel</a>.</p>`;
    }
    html += `</div>`;

    el.innerHTML = html;
  }

  // ---- Render: FAQ (zichtbaar + JSON-LD) ----
  function renderFAQ() {
    const prices = state.tomorrowPrices;
    if (!prices.length) return;

    const avgEpex  = prices.reduce((s, p) => s + p.price, 0) / prices.length;
    const avgCt    = fmtNum(priceCents(avgEpex, "inclusive"), 1);
    const cheapest = prices.reduce((a, b) => a.price <= b.price ? a : b);
    const cheapCt  = fmtNum(priceCents(cheapest.price, "inclusive"), 1);
    const cheapT   = fmtTime(cheapest.time);
    const negHrs   = prices.filter(p => priceCents(p.price, "inclusive") < 0).length;

    const cls = classify(avgEpex);
    let goedkoopAntw;
    if (cls === "very_cheap" || cls === "cheap") {
      goedkoopAntw = `Ja, morgen is stroom relatief goedkoop. Het daggemiddelde is ${avgCt} ct/kWh (all-in). Het goedkoopste uur is om ${cheapT} (${cheapCt} ct/kWh). Goed moment om apparaten in te plannen.`;
    } else if (cls === "pricey" || cls === "very_pricey") {
      goedkoopAntw = `Morgen zijn de stroomprijzen aan de hoge kant. Het daggemiddelde is ${avgCt} ct/kWh (all-in). Plan zware apparaten bij voorkeur in de nacht of vroege ochtend — het goedkoopste uur is om ${cheapT} (${cheapCt} ct/kWh).`;
    } else {
      goedkoopAntw = `Morgen zijn de stroomprijzen normaal. Het daggemiddelde is ${avgCt} ct/kWh (all-in). Het goedkoopste uur is om ${cheapT} (${cheapCt} ct/kWh).`;
    }

    const bestM   = findBestMoments(prices, 1, 2)[0];
    const bestCt  = fmtNum(priceCents(bestM.avg, "inclusive"), 1);
    const bestVan = fmtTime(bestM.startIso);
    const bestTot = endTime(bestM.endIso);

    const negAntw = negHrs > 0
      ? `Morgen zijn er ${negHrs} uur(en) met een negatieve stroomprijs. Stroom is dan gratis of je wordt betaald voor afname. Zet grote verbruikers aan in die uren.`
      : "Morgen zijn er geen negatieve uren verwacht. De prijzen blijven positief voor alle 24 uur.";

    const faqItems = [
      {
        q: "Is stroom morgen goedkoop?",
        a: goedkoopAntw,
      },
      {
        q: "Wanneer is stroom morgen het goedkoopst?",
        a: `Het goedkoopste aaneengesloten 2-uurs blok morgen is van ${bestVan} tot ${bestTot} (gemiddeld ${bestCt} ct/kWh all-in). Dit is het ideale moment voor je wasmachine, droger, EV-laden of warmtepomp.`,
      },
      {
        q: "Zijn er morgen negatieve stroomprijzen?",
        a: negAntw,
      },
      {
        q: "Wat is de gemiddelde stroomprijs voor morgen?",
        a: `De gemiddelde stroomprijs voor morgen is ${avgCt} ct/kWh all-in (energiebelasting + btw + gemiddelde leveranciersopslag). De exacte prijs hangt af van jouw leverancier en opslag.`,
      },
    ];

    // Zichtbaar FAQ
    setHTML("morgen-faq-items", faqItems.map(item => `
      <div class="morgen-faq-item">
        <h3>${item.q}</h3>
        <p>${item.a}</p>
      </div>`).join(""));

    // JSON-LD updaten
    const ldScript = document.getElementById("faq-json-ld");
    if (ldScript) {
      ldScript.textContent = JSON.stringify({
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": faqItems.map(item => ({
          "@type": "Question",
          "name": item.q,
          "acceptedAnswer": { "@type": "Answer", "text": item.a },
        })),
      });
    }
  }

  // ---- Render: aanbieder-keuzelijst ----
  function renderSupplierSelect() {
    const select = document.getElementById("morgen-supplier-select");
    if (!select) return;
    // Eenmalig vullen
    if (!select.dataset.populated) {
      (state.config.suppliers || [])
        .filter(s => s.id !== "custom")
        .forEach(s => {
          const opt = document.createElement("option");
          opt.value = s.id;
          opt.textContent = `${s.name}  (€${fmtNum(s.markup_per_kwh, 4)}/kWh opslag)`;
          select.appendChild(opt);
        });
      select.dataset.populated = "1";
    }
    select.value = state.supplierId;
  }

  // ---- Init: aanbieder-keuzelijst ----
  function initSupplierSelect() {
    const select = document.getElementById("morgen-supplier-select");
    if (!select) return;
    select.addEventListener("change", () => {
      state.supplierId = select.value;
      try { localStorage.setItem(STORAGE_KEYS.supplier, state.supplierId); } catch (e) {}
      renderAll();
    });
  }

  // ---- Init: kwartier-toggle (PT15M checkbox) ----
  function initResolutionToggle() {
    const wrap = document.getElementById("morgen-quarter-wrap");
    const cb   = document.getElementById("morgen-quarter-toggle");
    if (!cb) return;
    // Verberg toggle als er geen PT15M-data beschikbaar is voor morgen
    if (wrap) wrap.hidden = !(state.hasPt15m && state.tomorrowPrices15m.length > 0);
    cb.addEventListener("change", () => {
      state.chartRes = cb.checked ? "quarter" : "hourly";
      const heading = document.getElementById("morgen-chart-heading");
      if (heading) heading.textContent = cb.checked
        ? "Stroomprijs per kwartier morgen"
        : "Stroomprijs per uur morgen";
      renderChart();
    });
  }

  // ---- Render: alles ----
  function renderAll() {
    renderDateLabels();
    renderModeToggle();
    renderSupplierSelect();
    renderHighlights();
    renderChart();
    renderMoments();
    renderSupplierTable();
    renderWhy();
    renderFAQ();
  }

  // ---- Mode toggle ----
  function initModeToggle() {
    document.querySelectorAll("[data-mode-btn]").forEach(btn => {
      btn.addEventListener("click", () => {
        state.mode = btn.dataset.modeBtn;
        try { localStorage.setItem(STORAGE_KEYS.mode, state.mode); } catch (e) {}
        renderAll();
      });
    });
  }

  // ---- Bootstrap ----
  async function init() {
    state.mode       = loadStored(STORAGE_KEYS.mode, "inclusive");
    state.supplierId = loadStored(STORAGE_KEYS.supplier, "average");
    const cm = parseFloat(loadStored(STORAGE_KEYS.customMarkup, "0.025"));
    state.customMarkup = Number.isFinite(cm) && cm >= 0 ? cm : 0.025;

    try {
      const [config, pricesData] = await Promise.all([
        fetchJSON("/data/config.json"),
        fetchJSON("/data/prices.json"),
      ]);

      state.config      = config;
      state.tomorrowDate = getTomorrow();
      const today        = getToday();

      state.tomorrowPrices = pricesData.prices.filter(p => isSameLocalDay(p.time, state.tomorrowDate));
      state.todayPrices    = pricesData.prices.filter(p => isSameLocalDay(p.time, today));

      // PT15M kwartierdata (alleen vandaag + morgen, al gefilterd door backend)
      state.hasPt15m          = pricesData.has_pt15m === true;
      state.tomorrowPrices15m = (pricesData.prices_15m || [])
        .filter(p => isSameLocalDay(p.time, state.tomorrowDate));

      // Forecast optioneel (geen harde fout als niet beschikbaar)
      try {
        const fc = await fetchJSON("/data/forecast.json");
        state.tomorrowForecasts = (fc.forecasts || []).filter(p => isSameLocalDay(p.time, state.tomorrowDate));
      } catch (_) { /* niet kritisch */ }

      if (!state.tomorrowPrices.length) { showNoDataState(); return; }

      initModeToggle();
      initSupplierSelect();
      initResolutionToggle();
      renderAll();

    } catch (e) {
      console.error("Laad-fout morgen.js:", e);
      setHTML("morgen-content", `<div class="container morgen-no-data">
        <p class="morgen-no-data-icon" aria-hidden="true">⚠</p>
        <h2>Data kon niet worden geladen</h2>
        <p>Probeer de pagina te vernieuwen. Lukt het niet, kom dan later terug.</p>
        <p><a href="/">← Terug naar de homepage</a></p>
      </div>`);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
