// Stroomvoorspeller — frontend logica
// Laadt configuratie + prijzen, rendert now-card, samenvatting, slimste momenten en grafiek.
// Houdt rekening met gebruikersinstellingen: weergavemodus en leverancieropslag.

(function () {
  "use strict";

  // ---- Storage keys ----
  const STORAGE_KEYS = {
    mode: "sv.viewMode",          // 'inclusive' | 'exclusive'
    supplier: "sv.supplierId",    // id uit config.suppliers
    customMarkup: "sv.customMarkup", // string (€ per kWh)
  };

  const state = {
    config: null,
    payload: null,
    prices: [],
    nowIdx: -1,
    mode: "inclusive",
    supplierId: "average",
    customMarkup: 0.025,
    chart: null,
  };

  // ---- Storage helpers ----
  function loadStored(key, fallback) {
    try {
      const v = localStorage.getItem(key);
      return v == null ? fallback : v;
    } catch (e) { return fallback; }
  }
  function saveStored(key, value) {
    try { localStorage.setItem(key, String(value)); } catch (e) { /* no-op */ }
  }

  // ---- Calculation helpers ----
  function getSupplier() {
    const list = (state.config && state.config.suppliers) || [];
    return list.find((s) => s.id === state.supplierId) || list[0] || { id: "fallback", markup_per_kwh: 0.025 };
  }
  function effectiveMarkup() {
    const supplier = getSupplier();
    if (supplier.id === "custom") {
      const n = Number(state.customMarkup);
      return Number.isFinite(n) && n >= 0 ? n : 0;
    }
    return Number(supplier.markup_per_kwh) || 0;
  }

  // EPEX EUR/MWh -> ct/kWh in de gekozen weergave-modus.
  function priceCents(eurMwh, mode = state.mode) {
    const epex_per_kwh = eurMwh / 1000;
    if (mode === "exclusive") {
      // Excl. belasting: EPEX + opslag, zonder energiebelasting en zonder btw.
      return (epex_per_kwh + effectiveMarkup()) * 100;
    }
    // Incl. belasting: EPEX + opslag + energiebelasting, dan × btw.
    const t = state.config.taxes;
    const subtotal = epex_per_kwh + effectiveMarkup() + (t.energiebelasting_per_kwh || 0);
    return subtotal * (t.btw_factor || 1) * 100;
  }
  function priceCentsRaw(eurMwh) {
    // Echt kale EPEX prijs (zonder opslag, zonder belasting). Voor de tooltip.
    return (eurMwh / 1000) * 100;
  }

  function classify(eurMwh) {
    const t = state.config.thresholds_eur_per_mwh;
    if (eurMwh < (t.very_cheap || 0)) return "very_cheap";
    if (eurMwh < (t.cheap || 50)) return "cheap";
    if (eurMwh > (t.very_pricey || 200)) return "very_pricey";
    if (eurMwh > (t.pricey || 110)) return "pricey";
    return "normal";
  }
  function classifyToCard(c) {
    if (c === "very_cheap" || c === "cheap") return "cheap";
    if (c === "pricey" || c === "very_pricey") return "pricey";
    return "normal";
  }
  function statusLabel(c) {
    if (c === "very_cheap")  return "uitstekend";
    if (c === "cheap")       return "goedkoop";
    if (c === "very_pricey") return "extreem duur";
    if (c === "pricey")      return "duur";
    return "normaal";
  }

  // ---- Format helpers ----
  function fmtNum(value, digits) {
    return Number(value).toLocaleString("nl-NL", {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    });
  }
  function fmtCents(eurMwh, digits = 1) { return fmtNum(priceCents(eurMwh), digits); }
  function fmtTime(iso, opts) {
    const d = new Date(iso);
    return d.toLocaleTimeString("nl-NL", Object.assign({ hour: "2-digit", minute: "2-digit" }, opts || {}));
  }
  function fmtDateTime(iso) {
    const d = new Date(iso);
    const date = d.toLocaleDateString("nl-NL", { weekday: "short", day: "numeric", month: "short" });
    return `${date} ${fmtTime(iso)}`;
  }
  function modeLabel(mode = state.mode) {
    return mode === "exclusive" ? "excl. belasting" : "incl. belasting";
  }
  function otherMode() { return state.mode === "inclusive" ? "exclusive" : "inclusive"; }

  function setText(field, text) {
    document.querySelectorAll(`[data-field="${field}"]`).forEach((el) => { el.textContent = text; });
  }

  function isSameLocalDay(isoA, isoB) {
    const a = new Date(isoA), b = new Date(isoB);
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }

  function findCurrentIndex(prices, now) {
    let idx = -1;
    for (let i = 0; i < prices.length; i++) {
      if (new Date(prices[i].time).getTime() <= now.getTime()) idx = i; else break;
    }
    return idx;
  }

  function findBestMoments(prices, fromIdx, count = 3, windowHours = 2) {
    const candidates = [];
    for (let i = fromIdx; i <= prices.length - windowHours; i++) {
      let sum = 0;
      for (let k = 0; k < windowHours; k++) sum += prices[i + k].price;
      candidates.push({ start: i, avg: sum / windowHours });
    }
    candidates.sort((a, b) => a.avg - b.avg);
    const chosen = [];
    const used = new Set();
    for (const c of candidates) {
      let overlap = false;
      for (let k = 0; k < windowHours; k++) {
        if (used.has(c.start + k)) { overlap = true; break; }
      }
      if (overlap) continue;
      for (let k = 0; k < windowHours; k++) used.add(c.start + k);
      chosen.push(c);
      if (chosen.length >= count) break;
    }
    return chosen.map((c) => ({
      startIso: prices[c.start].time,
      endIso: prices[c.start + windowHours - 1].time,
      avg: c.avg,
    }));
  }

  function pointColor(eurMwh) {
    const c = classify(eurMwh);
    if (c === "very_cheap")  return "#1a7a31";
    if (c === "cheap")       return "#2f9e44";
    if (c === "very_pricey") return "#9c1a1a";
    if (c === "pricey")      return "#c92a2a";
    return "#d4a017";
  }

  // ---- Rendering ----
  function renderAll() {
    if (!state.config || !state.prices.length) return;
    renderSettingsPanel();
    renderModeBadges();
    renderNowCard();
    renderSummary();
    renderMoments();
    renderFooterMeta();
    renderChart();
  }

  function renderModeBadges() {
    setText("mode-label", modeLabel());
    document.querySelectorAll("[data-mode-btn]").forEach((btn) => {
      const active = btn.dataset.modeBtn === state.mode;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function renderNowCard() {
    const prices = state.prices;
    const nowIdx = state.nowIdx;
    const current = nowIdx >= 0 ? prices[nowIdx] : prices[0];

    const cls = classify(current.price);
    const card = document.querySelector(".now-card");
    if (card) card.dataset.status = classifyToCard(cls);

    setText("now-cents", fmtCents(current.price, 1));
    setText("now-time", `Nu, ${fmtTime(current.time)}`);
    setText("now-secondary", `${fmtNum(priceCents(current.price, otherMode()), 1)} ct/kWh ${modeLabel(otherMode())}`);
    setText("now-epex", `Kale EPEX: ${fmtNum(priceCentsRaw(current.price), 2)} ct/kWh`);

    const statusEl = document.querySelector(".status-value");
    if (statusEl) statusEl.textContent = statusLabel(cls);
  }

  function renderSummary() {
    const prices = state.prices;
    const nowIdx = state.nowIdx;
    const current = nowIdx >= 0 ? prices[nowIdx] : prices[0];
    const today = prices.filter((p) => isSameLocalDay(p.time, current.time));
    if (!today.length) return;
    const cheapest = today.reduce((a, b) => (a.price <= b.price ? a : b));
    const priciest = today.reduce((a, b) => (a.price >= b.price ? a : b));
    const avg = today.reduce((s, p) => s + p.price, 0) / today.length;
    setText("cheapest-today", `${fmtCents(cheapest.price)} ct · ${fmtTime(cheapest.time)}`);
    setText("priciest-today", `${fmtCents(priciest.price)} ct · ${fmtTime(priciest.time)}`);
    setText("avg-today", `${fmtCents(avg)} ct/kWh`);
  }

  function renderMoments() {
    const prices = state.prices;
    const fromIdx = state.nowIdx >= 0 ? state.nowIdx : 0;
    const moments = findBestMoments(prices, fromIdx, 3, 2);
    const list = document.querySelector('[data-field="best-moments"]');
    if (!list) return;
    list.innerHTML = "";
    if (!moments.length) {
      list.innerHTML = '<li class="moment-loading">Nog geen vensters beschikbaar.</li>';
      return;
    }
    moments.forEach((m, i) => {
      const li = document.createElement("li");
      li.className = "moment";
      li.innerHTML = `
        <span class="moment-rank">${i + 1}</span>
        <span class="moment-when">
          ${fmtDateTime(m.startIso)} – ${fmtTime(m.endIso, { hour: "2-digit", minute: "2-digit" })}
          <small>(2 uur)</small>
        </span>
        <span class="moment-price">${fmtCents(m.avg)} ct/kWh</span>
      `;
      list.appendChild(li);
    });
  }

  function renderFooterMeta() {
    const payload = state.payload || {};
    if (payload.generated_at) {
      const updated = new Date(payload.generated_at);
      setText("generated-at", updated.toLocaleString("nl-NL", {
        weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
      }));
    }
    if (payload.source === "sample") {
      setText("source-note", "Let op: testdata, niet de echte day-ahead prijzen. Dit is een ontwikkelversie.");
    } else {
      setText("source-note", "");
    }
  }

  function renderSettingsPanel() {
    const select = document.getElementById("supplier-select");
    if (select && !select.dataset.populated) {
      const suppliers = state.config.suppliers || [];
      select.innerHTML = "";
      suppliers.forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.textContent = s.id === "custom"
          ? s.name
          : `${s.name} (€${fmtNum(s.markup_per_kwh, 4)}/kWh opslag, excl. btw)`;
        select.appendChild(opt);
      });
      select.dataset.populated = "1";
    }
    if (select) select.value = state.supplierId;

    const customWrap = document.getElementById("custom-markup-wrap");
    const isCustom = state.supplierId === "custom";
    if (customWrap) customWrap.hidden = !isCustom;

    const customInput = document.getElementById("custom-markup-input");
    if (customInput) customInput.value = state.customMarkup;

    const t = state.config.taxes;
    const eb_incl = (t.energiebelasting_per_kwh || 0) * (t.btw_factor || 1);
    // Toon incl-btw tarief — dat is wat leveranciers in hun app/tariefblad laten zien
    setText("config-energiebelasting", `€${fmtNum(eb_incl, 4)}/kWh (incl. btw)`);
    setText("config-btw", `${Math.round((t.btw_factor - 1) * 100)}%`);
    setText("config-year", String(t.year));

    // Toon huidige effectieve opslag boven de dropdown — beide varianten
    const m = effectiveMarkup();
    const m_incl = m * (t.btw_factor || 1);
    setText("current-markup", `€${fmtNum(m, 4)}/kWh excl. btw  (= €${fmtNum(m_incl, 4)} incl. btw)`);
  }

  function renderChart() {
    const canvas = document.getElementById("dayChart");
    if (!canvas || typeof Chart === "undefined") return;
    if (state.chart) { state.chart.destroy(); state.chart = null; }

    const prices = state.prices;
    const labels = prices.map((p) => fmtTime(p.time));
    const data = prices.map((p) => priceCents(p.price));
    const colors = prices.map((p, i) => i === state.nowIdx ? "#0f6cbd" : pointColor(p.price));
    const radii  = prices.map((_, i) => i === state.nowIdx ? 6 : 3);

    const firstDay = new Date(prices[0].time).getDate();
    const tomorrowStart = prices.findIndex((p) => new Date(p.time).getDate() !== firstDay);

    state.chart = new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: `ct/kWh (${modeLabel()})`,
          data,
          tension: 0.25,
          borderColor: "#2e75b6",
          borderWidth: 2,
          pointBackgroundColor: colors,
          pointBorderColor: colors,
          pointRadius: radii,
          pointHoverRadius: (ctx) => (ctx.dataIndex === state.nowIdx ? 7 : 5),
          fill: { target: "origin", above: "rgba(46,117,182,0.08)" },
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 250 },
        scales: {
          x: {
            ticks: { autoSkip: true, maxTicksLimit: 12, color: "#7c8a99", font: { size: 11 } },
            grid: { color: "rgba(0,0,0,0.04)" },
          },
          y: {
            ticks: { color: "#7c8a99", font: { size: 11 }, callback: (v) => v + " ct" },
            grid: { color: "rgba(0,0,0,0.06)" },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => fmtDateTime(prices[items[0].dataIndex].time),
              label: (item) => {
                const idx = item.dataIndex;
                const eurMwh = prices[idx].price;
                return [
                  `Kale EPEX: ${fmtNum(priceCentsRaw(eurMwh), 2)} ct/kWh`,
                  `Excl. belasting: ${fmtNum(priceCents(eurMwh, "exclusive"), 2)} ct/kWh`,
                  `Incl. belasting: ${fmtNum(priceCents(eurMwh, "inclusive"), 2)} ct/kWh`,
                ];
              },
            },
          },
        },
      },
    });
  }

  // ---- Event wiring ----
  function wireUI() {
    document.querySelectorAll("[data-mode-btn]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const mode = btn.dataset.modeBtn;
        if (mode !== "inclusive" && mode !== "exclusive") return;
        if (state.mode === mode) return;
        state.mode = mode;
        saveStored(STORAGE_KEYS.mode, mode);
        renderAll();
      });
    });

    const toggleBtn = document.getElementById("settings-toggle");
    const panel = document.getElementById("settings-panel");
    if (toggleBtn && panel) {
      toggleBtn.addEventListener("click", () => {
        const isHidden = panel.hasAttribute("hidden");
        if (isHidden) panel.removeAttribute("hidden");
        else panel.setAttribute("hidden", "");
        toggleBtn.setAttribute("aria-expanded", isHidden ? "true" : "false");
      });
    }

    const select = document.getElementById("supplier-select");
    if (select) {
      select.addEventListener("change", (e) => {
        state.supplierId = e.target.value;
        saveStored(STORAGE_KEYS.supplier, state.supplierId);
        renderAll();
      });
    }

    const customInput = document.getElementById("custom-markup-input");
    if (customInput) {
      customInput.addEventListener("input", (e) => {
        const n = parseFloat(String(e.target.value).replace(",", "."));
        if (Number.isFinite(n) && n >= 0) {
          state.customMarkup = n;
          saveStored(STORAGE_KEYS.customMarkup, n);
          renderAll();
        }
      });
    }
  }

  // ---- Boot ----
  function loadInitialState() {
    state.mode = loadStored(STORAGE_KEYS.mode, "inclusive") === "exclusive" ? "exclusive" : "inclusive";
    state.supplierId = loadStored(STORAGE_KEYS.supplier, "average");
    const cm = parseFloat(loadStored(STORAGE_KEYS.customMarkup, "0.025"));
    state.customMarkup = Number.isFinite(cm) ? cm : 0.025;
  }
  function applyConfigDefaults() {
    if (!state.config) return;
    if (!localStorage.getItem(STORAGE_KEYS.mode)) {
      state.mode = state.config.view && state.config.view.default_mode === "exclusive" ? "exclusive" : "inclusive";
    }
    if (!localStorage.getItem(STORAGE_KEYS.supplier)) {
      state.supplierId = state.config.default_supplier || "average";
    }
  }
  function showError(msg) {
    const card = document.querySelector(".now-card");
    if (card) card.dataset.status = "error";
    setText("now-cents", "—");
    setText("now-time", msg || "Kon data niet laden");
  }

  loadInitialState();

  Promise.all([
    fetch("data/config.json", { cache: "no-store" }).then((r) => { if (!r.ok) throw new Error("config HTTP " + r.status); return r.json(); }),
    fetch("data/prices.json", { cache: "no-store" }).then((r) => { if (!r.ok) throw new Error("prices HTTP " + r.status); return r.json(); }),
  ])
    .then(([config, payload]) => {
      state.config = config;
      state.payload = payload;
      state.prices = payload.prices || [];
      applyConfigDefaults();
      state.nowIdx = findCurrentIndex(state.prices, new Date());
      wireUI();
      renderAll();
    })
    .catch((err) => {
      console.error("Kon data niet laden", err);
      showError("Kon data niet laden — probeer pagina te verversen");
    });
})();
