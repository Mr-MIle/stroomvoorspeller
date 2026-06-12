// Stroomvoorspeller — frontend logica
// Laadt configuratie + prijzen, rendert now-card, samenvatting, slimste momenten en grafiek.
// Houdt rekening met gebruikersinstellingen: weergavemodus, leverancieropslag en grafiekresolutie.
// v2.2: adaptieve grafiek met PT15M/PT60M toggle (dag 0+1 kwartierweergave vs. volledige weekprognose).

(function () {
  "use strict";

  // ---- Storage keys ----
  const STORAGE_KEYS = {
    mode:              "sv.viewMode",           // 'inclusive' | 'exclusive'
    supplier:          "sv.supplierId",          // id uit config.suppliers
    customMarkup:      "sv.customMarkup",        // string (euro per kWh)
    dismissedNegAlert: "sv.dismissedNegAlert",  // ISO-tijd van het event waarvoor de banner gesloten is
    chartRes:          "sv.chartRes",           // 'hourly' | 'quarter'
  };

  const state = {
    config: null,
    payload: null,
    forecastPayload: null,
    prices: [],          // alle uurprijzen uit prices.json (14d historie + 2d toekomst)
    prices15m: [],       // ruwe kwartierdata voor vandaag + morgen (uit prices.json.prices_15m)
    dayPrices: [],       // gefilterd: vandaag + morgen, uurresolutie, voor now-card/grafiek/model
    dayPrices15m: [],    // gefilterd: vandaag + morgen, kwartierresolutie, voor chart in quarter-mode
    forecasts: [],       // dag 2–7 voorspellingen uit forecast.json
    nowIdx: -1,          // index in dayPrices (uurresolutie)
    nowIdx15m: -1,       // index in dayPrices15m (kwartierresolutie)
    hasPt15m: false,     // True als prices_15m echte PT15M-data bevat
    chartResolution: "hourly",  // 'hourly' | 'quarter'
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
      return (epex_per_kwh + effectiveMarkup()) * 100;
    }
    const t = state.config.taxes;
    const subtotal = epex_per_kwh + effectiveMarkup() + (t.energiebelasting_per_kwh || 0);
    return subtotal * (t.btw_factor || 1) * 100;
  }
  function priceCentsRaw(eurMwh) {
    return (eurMwh / 1000) * 100;
  }
  function priceCentsForSupplier(eurMwh, supplier) {
    const epex_per_kwh = eurMwh / 1000;
    const markup = Number(supplier.markup_per_kwh) || 0;
    const t = state.config.taxes;
    const subtotal = epex_per_kwh + markup + (t.energiebelasting_per_kwh || 0);
    return subtotal * (t.btw_factor || 1) * 100;
  }

  function classify(eurMwh) {
    // Classificeer altijd op basis van de consumentenprijs incl. energiebelasting + btw
    // + leverancieropslag (= wat de bezoeker daadwerkelijk betaalt), niet op kale EPEX.
    const ct = priceCents(eurMwh, "inclusive");
    const t  = state.config.thresholds_ct_kwh_inclusive || {};
    if (ct < 0)                          return "negative";
    if (ct < (t.very_cheap ?? 14))       return "very_cheap";
    if (ct < (t.cheap      ?? 22))       return "cheap";
    if (ct > (t.very_pricey ?? 38))      return "very_pricey";
    if (ct > (t.pricey      ?? 28))      return "pricey";
    return "normal";
  }
  function classifyToCard(c) {
    if (c === "negative")                    return "free";
    if (c === "very_cheap" || c === "cheap") return "cheap";
    if (c === "pricey" || c === "very_pricey") return "pricey";
    return "normal";
  }
  function statusLabel(c) {
    if (c === "negative")    return "gratis";
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

  function buildHolidayLookup() {
    const cfg = state.config || {};
    return {
      nl:          new Set(cfg.feestdagen_nl || []),
      crossborder: new Set(cfg.feestdagen_crossborder || []),
    };
  }

  function findCurrentIndex(prices, now) {
    let idx = -1;
    for (let i = 0; i < prices.length; i++) {
      if (new Date(prices[i].time).getTime() <= now.getTime()) idx = i; else break;
    }
    return idx;
  }

  function filterTodayTomorrow(prices, now) {
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0, 0);
    const dayAfterTomorrow = new Date(todayStart.getTime() + 48 * 3600 * 1000);
    return prices.filter((p) => {
      const t = new Date(p.time);
      return t >= todayStart && t < dayAfterTomorrow;
    });
  }

  function findMostExpensiveBlock(prices, windowHours) {
    if (prices.length < windowHours) return null;
    let best = null;
    for (let i = 0; i <= prices.length - windowHours; i++) {
      let sum = 0;
      for (let k = 0; k < windowHours; k++) sum += prices[i + k].price;
      const avg = sum / windowHours;
      if (!best || avg > best.avg) {
        best = { startIso: prices[i].time, endIso: prices[i + windowHours - 1].time, avg };
      }
    }
    return best;
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
    if (c === "negative")    return "#7048e8";
    if (c === "very_cheap")  return "#1a7a31";
    if (c === "cheap")       return "#2f9e44";
    if (c === "very_pricey") return "#9c1a1a";
    if (c === "pricey")      return "#c92a2a";
    return "#d4a017";
  }

  // ---- Negatieve-prijs detectie ----
  function findNegativePriceWindows() {
    const cfg = (state.config && state.config.negative_price_alert) || {};
    if (!cfg.enabled) return [];
    const threshold = Number(cfg.threshold_cents_inclusive);
    if (!Number.isFinite(threshold)) return [];

    const prices = state.dayPrices;  // altijd uurresolutie, onafhankelijk van chartResolution
    const now = Date.now();
    const windows = [];
    let current = null;
    for (let i = 0; i < prices.length; i++) {
      const p = prices[i];
      const endMs = new Date(p.time).getTime() + 3600000;
      if (endMs <= now) continue;
      const cents = priceCents(p.price, "inclusive");
      if (cents <= threshold) {
        if (current) {
          current.endIso = p.time;
          current.minCents = Math.min(current.minCents, cents);
        } else {
          current = { startIso: p.time, endIso: p.time, minCents: cents };
        }
      } else if (current) {
        windows.push(current);
        current = null;
      }
    }
    if (current) windows.push(current);
    return windows;
  }

  function dayLabelFor(iso) {
    const d = new Date(iso);
    const today = new Date();
    if (d.getFullYear() === today.getFullYear() && d.getMonth() === today.getMonth() && d.getDate() === today.getDate()) {
      return "vandaag";
    }
    const tomorrow = new Date(today.getTime() + 86400000);
    if (d.getFullYear() === tomorrow.getFullYear() && d.getMonth() === tomorrow.getMonth() && d.getDate() === tomorrow.getDate()) {
      return "morgen";
    }
    return d.toLocaleDateString("nl-NL", { weekday: "long" });
  }

  function hideNegAlert(banner) {
    banner.setAttribute("hidden", "");
    document.body.classList.remove("has-neg-alert");
  }
  function renderNegativeAlert() {
    const banner = document.getElementById("neg-alert");
    if (!banner) return;
    const windows = findNegativePriceWindows();
    if (!windows.length) {
      hideNegAlert(banner);
      return;
    }
    const eventKey = windows[0].startIso;
    const dismissed = loadStored(STORAGE_KEYS.dismissedNegAlert, "");
    if (dismissed === eventKey) {
      hideNegAlert(banner);
      return;
    }

    const parts = windows.map((w) => {
      const startTime = fmtTime(w.startIso);
      const endDate = new Date(new Date(w.endIso).getTime() + 3600000);
      const endTime = endDate.toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
      return `${dayLabelFor(w.startIso)} ${startTime}–${endTime}`;
    });
    const overallMin = Math.min.apply(null, windows.map((w) => w.minCents));
    const intro = parts.length === 1
      ? parts[0]
      : parts.slice(0, -1).join(", ") + " en " + parts[parts.length - 1];
    const text = `Stroom is ${intro} uitzonderlijk goedkoop (tot ${fmtNum(overallMin, 1)} ct/kWh incl. btw, met jouw leverancier). Goed moment voor wasmachine, droger, EV-laden of warmtepomp.`;
    setText("neg-alert-text", text);
    banner.dataset.eventKey = eventKey;
    banner.removeAttribute("hidden");
    document.body.classList.add("has-neg-alert");
  }

  // ---- Morgen-tip banner ----
  // Verschijnt als morgen-data beschikbaar is (≥12 uren) met een opvallend scenario.
  // Prioriteit: negatieve uren > goedkoop blok > duur spitsuur > geen banner.
  // Verdwijnt vanzelf bij middernacht (morgen wordt vandaag, geen morgen-data meer).
  function renderTomorrowTip() {
    const banner = document.getElementById("tomorrow-tip");
    if (!banner) return;

    const now = new Date();
    const tomorrowStart      = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 0, 0, 0, 0);
    const dayAfterTomorrow   = new Date(tomorrowStart.getTime() + 24 * 3600 * 1000);

    // Filter morgen-uren uit dayPrices (uurresolutie)
    const tp = state.dayPrices.filter((p) => {
      const t = new Date(p.time);
      return t >= tomorrowStart && t < dayAfterTomorrow;
    });

    // Minimaal 12 uren nodig — anders nog niet gepubliceerd
    if (tp.length < 12) {
      banner.setAttribute("hidden", "");
      renderMorgenLink(false);
      return;
    }

    renderMorgenLink(true);

    const tipTextEl = document.getElementById("tomorrow-tip-text");
    if (!tipTextEl) return;

    const t  = (state.config && state.config.thresholds_ct_kwh_inclusive) || {};
    const cheapThreshold  = t.very_cheap ?? 14;
    const priceyThreshold = t.pricey    ?? 28;

    // 1. Negatieve uren?
    const negHours = tp.filter((p) => priceCents(p.price, "inclusive") < 0);
    if (negHours.length > 0) {
      const first = negHours[0];
      const last  = negHours[negHours.length - 1];
      const endDate = new Date(new Date(last.time).getTime() + 3600000);
      const startT = fmtTime(first.time);
      const endT   = endDate.toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
      banner.className = "tomorrow-tip tomorrow-tip--neg";
      tipTextEl.innerHTML =
        `<strong>Morgen negatieve prijzen</strong> tussen ${startT}–${endT} — gratis stroom, apparaten aan! ` +
        `<a href="/morgen">Bekijk alle morgen-uren →</a>`;
      banner.removeAttribute("hidden");
      return;
    }

    // 2. Goedkoop 2-uurs blok (gem. < cheapThreshold ct incl. btw)?
    const cheapMoments = findBestMoments(tp, 0, 1, 2);
    if (cheapMoments.length > 0) {
      const m = cheapMoments[0];
      const avgCents = priceCents(m.avg, "inclusive");
      if (avgCents < cheapThreshold) {
        const endDate = new Date(new Date(m.endIso).getTime() + 3600000);
        const startT = fmtTime(m.startIso);
        const endT   = endDate.toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
        banner.className = "tomorrow-tip tomorrow-tip--cheap";
        tipTextEl.innerHTML =
          `<strong>Morgen goedkoop laden</strong> tussen ${startT}–${endT} ` +
          `(gem. ${fmtNum(avgCents, 1)} ct/kWh incl. btw). ` +
          `<a href="/morgen">Bekijk alle morgen-uren →</a>`;
        banner.removeAttribute("hidden");
        return;
      }
    }

    // 3. Duur spitsuur (gem. > priceyThreshold ct incl. btw)?
    const expBlock = findMostExpensiveBlock(tp, 2);
    if (expBlock && priceCents(expBlock.avg, "inclusive") > priceyThreshold) {
      const endDate = new Date(new Date(expBlock.endIso).getTime() + 3600000);
      const startT = fmtTime(expBlock.startIso);
      const endT   = endDate.toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
      banner.className = "tomorrow-tip tomorrow-tip--pricey";
      tipTextEl.innerHTML =
        `<strong>Morgen duur</strong> tussen ${startT}–${endT} — zware apparaten liever 's nachts. ` +
        `<a href="/morgen">Bekijk alle morgen-uren →</a>`;
      banner.removeAttribute("hidden");
      return;
    }

    // Geen bijzonderheden — banner verbergen
    banner.setAttribute("hidden", "");
  }

  // ---- Morgen-knop bij de grafiekkop ----
  // Alleen zichtbaar als morgen-data beschikbaar is (≥12 uren).
  function renderMorgenLink(hasTomorrowData) {
    const btn = document.getElementById("morgen-link-btn");
    if (!btn) return;
    if (hasTomorrowData) {
      btn.removeAttribute("hidden");
    } else {
      btn.setAttribute("hidden", "");
    }
  }

  // ---- Hero-verdict: morgen/nu-samenvatting bovenaan (#58) ----
  // Vervangt de voormalige tomorrow-tip banner — zelfde logica, zelfde kleurvarianten,
  // maar boven de now-card in plaats van tussen hero en grafiek.
  function renderHeroVerdict() {
    const el     = document.getElementById("hero-verdict");
    const iconEl = document.getElementById("hero-verdict-icon");
    const textEl = document.getElementById("hero-verdict-text");
    if (!el || !iconEl || !textEl || !state.config) return;

    const now              = new Date();
    const tomorrowStart    = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 0, 0, 0, 0);
    const dayAfterTomorrow = new Date(tomorrowStart.getTime() + 24 * 3600 * 1000);
    const tp = state.dayPrices.filter((p) => {
      const t = new Date(p.time);
      return t >= tomorrowStart && t < dayAfterTomorrow;
    });

    const thr             = state.config.thresholds_ct_kwh_inclusive || {};
    const cheapThreshold  = thr.very_cheap ?? 14;
    const priceyThreshold = thr.pricey     ?? 28;

    function fmtEnd(isoEnd) {
      return new Date(new Date(isoEnd).getTime() + 3600000)
        .toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
    }
    function setVariant(variant, icon) {
      el.className = `hero-verdict hero-verdict--${variant}`;
      iconEl.textContent = icon;
    }

    renderMorgenLink(tp.length >= 12);

    if (tp.length >= 12) {
      // ── Morgen-data beschikbaar ────────────────────────────────────────────────
      const negHours = tp.filter((p) => priceCents(p.price, "inclusive") < 0);

      if (negHours.length > 0) {
        const first = negHours[0];
        const last  = negHours[negHours.length - 1];
        setVariant("neg", "⚡");
        textEl.innerHTML =
          `<strong>Morgen negatieve prijzen</strong> tussen ${fmtTime(first.time)}–${fmtEnd(last.time)} — gratis stroom, apparaten aan! ` +
          `<a href="/morgen">Bekijk alle morgen-uren →</a>`;

      } else {
        const cheapMoments = findBestMoments(tp, 0, 1, 2);
        if (cheapMoments.length > 0 && priceCents(cheapMoments[0].avg, "inclusive") < cheapThreshold) {
          const m     = cheapMoments[0];
          const avgCt = fmtNum(priceCents(m.avg, "inclusive"), 1);
          setVariant("cheap", "⚡");
          textEl.innerHTML =
            `<strong>Morgen goedkoop laden</strong> tussen ${fmtTime(m.startIso)}–${fmtEnd(m.endIso)} ` +
            `(gem. ${avgCt} ct/kWh incl. btw). ` +
            `<a href="/morgen">Bekijk alle morgen-uren →</a>`;

        } else {
          const expBlock = findMostExpensiveBlock(tp, 2);
          if (expBlock && priceCents(expBlock.avg, "inclusive") > priceyThreshold) {
            setVariant("pricey", "⚠");
            textEl.innerHTML =
              `<strong>Morgen duur</strong> tussen ${fmtTime(expBlock.startIso)}–${fmtEnd(expBlock.endIso)} — zware apparaten liever 's nachts. ` +
              `<a href="/morgen">Bekijk alle morgen-uren →</a>`;
          } else {
            const avg = tp.reduce((s, p) => s + priceCents(p.price, "inclusive"), 0) / tp.length;
            setVariant("neutral", "📊");
            textEl.innerHTML =
              `Morgen normaal · daggemiddelde ${fmtNum(avg, 1)} ct/kWh. ` +
              `<a href="/morgen">Bekijk alle morgen-uren →</a>`;
          }
        }
      }
      el.removeAttribute("hidden");

    } else {
      // ── Nog geen morgen-data → huidige status + beste venster vandaag ──────────
      const prices = state.dayPrices;
      if (!prices.length) { el.setAttribute("hidden", ""); return; }
      const nowIdx  = state.nowIdx >= 0 ? state.nowIdx : 0;
      const current = prices[nowIdx];
      const cls     = classify(current.price);
      const ct      = fmtNum(priceCents(current.price, "inclusive"), 1);
      const moments = findBestMoments(prices, nowIdx, 1, 2);
      let text = `Nu ${statusLabel(cls)} · ${ct} ct/kWh`;
      if (moments.length > 0) {
        const m   = moments[0];
        const mCt = fmtNum(priceCents(m.avg, "inclusive"), 1);
        text += ` · goedkoopst ${fmtTime(m.startIso)}–${fmtEnd(m.endIso)} (${mCt} ct/kWh)`;
      }
      setVariant("neutral", "⚡");
      textEl.textContent = text;
      el.removeAttribute("hidden");
    }
  }

  // ---- Rendering ----
  function renderAll() {
    if (!state.config || !state.dayPrices.length) return;
    renderSourceAlert();
    renderNegativeAlert();
    checkStaleData();
    renderHeroVerdict(); // vervangt renderTomorrowTip — logica samengevoegd (#58)
    renderSettingsPanel();
    renderSettingsToggle();
    renderModeBadges();
    renderNowCard();
    renderSummary();
    renderSupplierTable();
    renderMoments();
    renderFooterMeta();
    renderResolutionToggle();
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
    const prices = state.dayPrices;  // altijd uurresolutie
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
    const prices = state.dayPrices;  // altijd uurresolutie voor dagstatistieken
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
    const prices = state.dayPrices;  // altijd uurresolutie voor 2-uurs vensters
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
      const consumerCents = priceCents(m.avg);
      const isNeg = consumerCents <= 0;
      li.className = isNeg ? "moment is-negative" : "moment";
      const badge = isNeg ? `<span class="moment-badge-free">⚡ gratis</span>` : "";
      li.innerHTML = `
        <span class="moment-rank">${i + 1}</span>
        <span class="moment-when">
          ${fmtDateTime(m.startIso)} – ${fmtTime(m.endIso, { hour: "2-digit", minute: "2-digit" })}
          <small>(2 uur)</small>
        </span>
        <span class="moment-price">${fmtCents(m.avg)} ct/kWh${badge}</span>
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

  // ---- Source-fout banner ----
  // Toont een rode waarschuwing als prices.json sample-data bevat (ENTSO-E was onbereikbaar
  // én er was geen bestaande echte data om te bewaren — zie fetch_prices.py).
  function renderSourceAlert() {
    const banner = document.getElementById("source-alert");
    if (!banner) return;
    const payload = state.payload || {};
    if (payload.source === "sample") {
      banner.removeAttribute("hidden");
    } else {
      banner.setAttribute("hidden", "");
    }
  }

  // ---- Stale-data banner ----
  // Toont een subtiele waarschuwing als prices.json ouder is dan 6 uur.
  // Geen sluitknop nodig — banner verdwijnt vanzelf bij de volgende paginalading met verse data.
  function checkStaleData() {
    const banner = document.getElementById("stale-alert");
    if (!banner) return;
    const payload = state.payload || {};
    if (!payload.generated_at) return;
    const generatedAt = new Date(payload.generated_at);
    const ageMs = Date.now() - generatedAt.getTime();
    const STALE_THRESHOLD_MS = 28 * 60 * 60 * 1000; // 28 uur (prijzen 1× per dag ~14:00 CEST)
    if (ageMs > STALE_THRESHOLD_MS) {
      const timeStr = generatedAt.toLocaleString("nl-NL", { hour: "2-digit", minute: "2-digit" });
      const dateStr = generatedAt.toLocaleString("nl-NL", { weekday: "short", day: "numeric", month: "short" });
      const el = document.getElementById("stale-alert-time");
      if (el) el.textContent = `${dateStr} ${timeStr}`;
      banner.removeAttribute("hidden");
    } else {
      banner.setAttribute("hidden", "");
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
    setText("config-energiebelasting", `€${fmtNum(eb_incl, 4)}/kWh (incl. btw)`);
    setText("config-btw", `${Math.round((t.btw_factor - 1) * 100)}%`);
    setText("config-year", String(t.year));

    const m = effectiveMarkup();
    const m_incl = m * (t.btw_factor || 1);
    setText("current-markup", `€${fmtNum(m, 4)}/kWh excl. btw  (= €${fmtNum(m_incl, 4)} incl. btw)`);
  }

  function renderSettingsToggle() {
    const supplier = getSupplier();
    setText("settings-toggle-value", supplier.name || "—");
  }

  function renderSupplierTable() {
    const tbody = document.querySelector('[data-field="suppliers-tbody"]');
    if (!tbody) return;
    const prices = state.dayPrices;
    if (!prices.length) return;
    const current = state.nowIdx >= 0 ? prices[state.nowIdx] : prices[0];
    if (!current) return;

    setText("suppliers-now-time", `Nu, ${fmtTime(current.time)}`);

    const verifiedDates = (state.config.suppliers || [])
      .map((s) => s.verified)
      .filter((v) => typeof v === "string" && v.length === 10);
    if (verifiedDates.length) {
      const oldest = verifiedDates.sort()[0];
      const d = new Date(oldest + "T00:00:00");
      setText("suppliers-verified", d.toLocaleDateString("nl-NL", { day: "numeric", month: "long", year: "numeric" }));
    }

    const rows = (state.config.suppliers || [])
      .filter((s) => s.id !== "custom")
      .map((s) => ({ supplier: s, cents: priceCentsForSupplier(current.price, s) }))
      .sort((a, b) => a.cents - b.cents);

    tbody.innerHTML = "";
    rows.forEach((r) => {
      const s = r.supplier;
      const tr = document.createElement("tr");
      if (s.id === state.supplierId) tr.className = "is-mine";

      const tdName = document.createElement("td");
      tdName.className = "td-supplier";
      if (s.website) {
        const a = document.createElement("a");
        a.href = s.website;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = s.name;
        tdName.appendChild(a);
      } else {
        tdName.textContent = s.name;
      }
      if (s.id === state.supplierId) {
        const badge = document.createElement("span");
        badge.className = "supplier-mine-badge";
        badge.textContent = "jouw keuze";
        tdName.appendChild(badge);
      }
      tr.appendChild(tdName);

      const tdPrice = document.createElement("td");
      tdPrice.className = "td-price";
      tdPrice.textContent = fmtNum(r.cents, 1);
      tr.appendChild(tdPrice);

      const tdMarkup = document.createElement("td");
      tdMarkup.className = "td-markup";
      tdMarkup.textContent = `€${fmtNum(s.markup_per_kwh, 4)}`;
      tr.appendChild(tdMarkup);

      const tdFixed = document.createElement("td");
      tdFixed.className = "td-fixed";
      const fx = Number(s.fixed_per_month) || 0;
      tdFixed.textContent = fx ? `€${fmtNum(fx, 2)}` : "—";
      tr.appendChild(tdFixed);

      const tdAction = document.createElement("td");
      tdAction.className = "td-action";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "supplier-pick-btn";
      if (s.id === state.supplierId) {
        btn.disabled = true;
        btn.textContent = "✓ Geselecteerd";
        btn.setAttribute("aria-label", `${s.name} is jouw geselecteerde leverancier`);
      } else {
        btn.textContent = s.id === "average" ? "Standaard" : "Kies";
        btn.setAttribute("aria-label", `Kies ${s.name} als jouw leverancier`);
        btn.addEventListener("click", () => {
          state.supplierId = s.id;
          saveStored(STORAGE_KEYS.supplier, s.id);
          renderAll();
        });
      }
      tdAction.appendChild(btn);
      tr.appendChild(tdAction);
      tbody.appendChild(tr);
    });
  }

  // ---- Resolution toggle ----
  function renderResolutionToggle() {
    const toggleWrap = document.getElementById("chart-res-toggle");
    if (!toggleWrap) return;

    // Toon toggle alleen als er echte PT15M-data beschikbaar is.
    const available = state.hasPt15m && state.dayPrices15m.length > 0;
    toggleWrap.hidden = !available;

    if (!available) {
      // Zorg dat de heading de juiste tekst toont als toggle verborgen is.
      const heading = document.getElementById("chart-heading");
      if (heading) heading.textContent = "Vandaag & morgen, per uur";
      return;
    }

    // Zet actief knop-state.
    document.querySelectorAll("[data-res-btn]").forEach((btn) => {
      const active = btn.dataset.resBtn === state.chartResolution;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });

    // Pas de sectie-heading en subtitle aan op de actuele resolutie.
    const heading = document.getElementById("chart-heading");
    const sub = document.getElementById("chart-sub");
    const note = document.getElementById("res-toggle-note");
    if (state.chartResolution === "quarter") {
      if (heading) heading.textContent = "Vandaag & morgen, per kwartier";
      if (sub) sub.innerHTML =
        `Kwartierlijkse day-ahead prijzen (EPEX Spot, v.a. okt&nbsp;2025), weergegeven als <span data-field="mode-label">${modeLabel()}</span>. ` +
        `Tooltip toont alle varianten.`;
      if (note) note.hidden = false;
    } else {
      if (heading) heading.textContent = "Vandaag & morgen";
      if (sub) sub.innerHTML =
        `Day-ahead prijzen van EPEX Spot, weergegeven als <span data-field="mode-label">${modeLabel()}</span>. ` +
        `Tooltip toont alle drie de varianten.`;
      if (note) note.hidden = true;
    }
  }

  // ---- Chart: plugins ----

  // Dag-indeling: per kalenderdag de eerste/laatste timeline-index.
  function svBuildDayMap(timeline) {
    const m = Object.create(null);
    timeline.forEach((pt, i) => {
      const date = pt.time.slice(0, 10);
      if (!m[date]) m[date] = { first: i, last: i };
      else m[date].last = i;
    });
    return m;
  }
  function svDayMeta(date, holidays) {
    const dObj = new Date(date + "T12:00:00");
    const dow = dObj.getDay();
    return {
      dObj,
      isNL: holidays.nl.has(date),
      isCB: holidays.crossborder.has(date),
      isWeekend: dow === 0 || dow === 6,
    };
  }

  // Daglint-plugin (variant B): weekend-/feestdagbanden + middernacht-scheidslijnen
  // achter de data, en weekdag-/feestdaglabels bovenin (bóven de data, met licht
  // plaatje voor leesbaarheid). Zo zijn de dagen los van de uur-as af te lezen.
  const dayBandPlugin = {
    id: "svDayBand",
    beforeDatasetsDraw(chart, _args, opts) {
      const { ctx, chartArea } = chart;
      const { timeline, holidays } = opts;
      if (!timeline || timeline.length < 2 || !chartArea) return;
      const step = (chartArea.right - chartArea.left) / timeline.length;
      const dayMap = svBuildDayMap(timeline);
      const h = chartArea.bottom - chartArea.top;

      ctx.save();
      let dayIdx = 0;
      Object.entries(dayMap).forEach(([date, { first, last }]) => {
        const { isNL, isCB, isWeekend } = svDayMeta(date, holidays);
        let bg = null;
        if (isNL && isCB) bg = "rgba(255, 193, 7, 0.18)";
        else if (isNL)    bg = "rgba(255, 193, 7, 0.13)";
        else if (isCB)    bg = "rgba(255, 140, 0, 0.13)";
        else if (isWeekend) bg = "rgba(99, 102, 170, 0.13)";   // duidelijker weekend

        const x1 = chartArea.left + first * step;
        const x2 = chartArea.left + (last + 1) * step;
        if (bg) {
          ctx.fillStyle = bg;
          ctx.fillRect(x1, chartArea.top, x2 - x1, h);
        }
        // Middernacht-scheidslijn aan het begin van elke dag behalve de eerste.
        if (dayIdx > 0) {
          ctx.beginPath();
          ctx.strokeStyle = "rgba(100, 116, 139, 0.22)";
          ctx.lineWidth = 1;
          ctx.setLineDash([2, 3]);
          ctx.moveTo(x1, chartArea.top);
          ctx.lineTo(x1, chartArea.bottom);
          ctx.stroke();
          ctx.setLineDash([]);
        }
        dayIdx++;
      });
      ctx.restore();
    },
    afterDatasetsDraw(chart, _args, opts) {
      const { ctx, chartArea } = chart;
      const { timeline, holidays } = opts;
      if (!timeline || timeline.length < 2 || !chartArea) return;
      const step = (chartArea.right - chartArea.left) / timeline.length;
      const dayMap = svBuildDayMap(timeline);

      ctx.save();
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      Object.entries(dayMap).forEach(([date, { first, last }]) => {
        const { dObj, isNL, isCB, isWeekend } = svDayMeta(date, holidays);
        const x1 = chartArea.left + first * step;
        const x2 = chartArea.left + (last + 1) * step;
        const bandW = x2 - x1;
        const cx = (x1 + x2) / 2;

        let text, color, minW;
        if (isNL && isCB) { text = "NL + EU feestdag"; color = "rgba(110, 70, 0, 0.90)"; minW = 92; }
        else if (isNL)    { text = "NL feestdag";      color = "rgba(110, 70, 0, 0.88)"; minW = 64; }
        else if (isCB)    { text = "EU-feestdag";      color = "rgba(140, 70, 0, 0.90)"; minW = 60; }
        else {
          const wd = dObj.toLocaleDateString("nl-NL", { weekday: "short" }).replace(".", "");
          text = `${wd} ${dObj.getDate()}`;
          color = isWeekend ? "rgba(67, 56, 202, 0.98)" : "rgba(51, 65, 85, 0.95)";
          minW = 30;
        }
        if (bandW < minW) return;

        // Geen witte achtergrond — de tekst gaat op in de grafiek. Contrast komt uit
        // een zwaardere, donkere letter in de bovenmarge, waar de prijslijn zelden komt.
        ctx.font = "bold 11px system-ui, -apple-system, sans-serif";
        ctx.fillStyle = color;
        ctx.fillText(text, cx, chartArea.top + 4);
      });
      ctx.restore();
    },
  };

  // Tekent verticale scheidslijn(en) op opgegeven index-posities in de timeline.
  // Gebruikt voor: vandaag→morgen-grens (quarter mode) en day-ahead→prognose-grens (hourly mode).
  const svBoundaryPlugin = {
    id: "svBoundary",
    // Achtergrond-tint voor het voorspelgebied — vóór de data getekend, zodat de
    // curve en stippen er bovenop blijven liggen. Markeert "vanaf hier schatting"
    // zonder een tweede stippellijn (die botste met de referentielijn, #63).
    beforeDatasetsDraw(chart, _args, opts) {
      const { ctx, chartArea } = chart;
      const { boundaries = [], n } = opts;
      if (!boundaries.length || !n || !chartArea) return;
      const step = (chartArea.right - chartArea.left) / n;
      ctx.save();
      for (const b of boundaries) {
        if (!b.tint || b.index < 0 || b.index > n) continue;
        const x = chartArea.left + b.index * step;
        ctx.fillStyle = "rgba(100, 116, 139, 0.07)";
        ctx.fillRect(x, chartArea.top, chartArea.right - x, chartArea.bottom - chartArea.top);
      }
      ctx.restore();
    },
    afterDatasetsDraw(chart, _args, opts) {
      const { ctx, chartArea } = chart;
      const { boundaries = [], n } = opts;
      if (!boundaries.length || !n || !chartArea) return;

      const step = (chartArea.right - chartArea.left) / n;

      ctx.save();
      for (const { index, label, labelSide = "right", tint = false } of boundaries) {
        if (index < 0 || index > n) continue;
        const x = chartArea.left + index * step;

        // Grens-edge. Een tint-grens (voorspelling) krijgt een dunne SOLIDE lijn als
        // rand van het waasje; een gewone grens (bv. 'Morgen') blijft gestippeld.
        // Zo is "gestippeld" voortaan exclusief van de horizontale referentielijn.
        ctx.beginPath();
        ctx.strokeStyle = "rgba(100, 116, 139, 0.38)";
        ctx.lineWidth = tint ? 1 : 1.5;
        ctx.setLineDash(tint ? [] : [4, 4]);
        ctx.moveTo(x, chartArea.top + 2);
        ctx.lineTo(x, chartArea.bottom);
        ctx.stroke();
        ctx.setLineDash([]);

        // Optioneel label (bv. "Morgen" of "Voorspelling")
        if (label) {
          const pad = 5;
          const textX = labelSide === "left" ? x - pad : x + pad;
          ctx.fillStyle = "rgba(71, 85, 105, 0.75)";
          ctx.font = "bold 10px system-ui, -apple-system, sans-serif";
          ctx.textAlign = labelSide === "left" ? "right" : "left";
          ctx.textBaseline = "top";
          // Eigen regel onder de dag-labels, zodat "Voorspelling" nooit door een dagnaam loopt.
          ctx.fillText(label, textX, chartArea.top + 22);
        }
      }
      ctx.restore();
    },
  };

  // ---- Prijszone-achtergrond plugin (#57) ----
  // Tekent horizontale gekleurde zones achter de grafiek, gebaseerd op prijsdrempels.
  // opacity 7-10%: voelbaar, niet dominant (Buienradar-principe).
  const svPriceZonePlugin = {
    id: "svPriceZone",
    beforeDatasetsDraw(chart, _args, opts) {
      const { ctx, chartArea, scales } = chart;
      const y = scales.y;
      if (!y || !chartArea || !opts.zones || !opts.zones.length) return;
      ctx.save();
      for (const zone of opts.zones) {
        const yTop = zone.max !== null
          ? Math.max(y.getPixelForValue(zone.max), chartArea.top)
          : chartArea.top;
        const yBot = zone.min !== null
          ? Math.min(y.getPixelForValue(zone.min), chartArea.bottom)
          : chartArea.bottom;
        if (yTop >= yBot) continue;
        ctx.fillStyle = zone.color;
        ctx.fillRect(chartArea.left, yTop, chartArea.right - chartArea.left, yBot - yTop);
      }
      ctx.restore();
    },
  };

  // Regime-kleuren en plausibility-configuratie
  const REGIME_COLOR = { oversupply: "#f59e0b", schaarste: "#ef4444", normaal: "#0f6cbd" };

  const PLAUSIBILITY_BAND_COLOR = {
    HIGH:            "rgba(147,197,253,0.25)",
    NORMAL:          "rgba(147,197,253,0.18)",
    LOW:             "rgba(253,186,116,0.25)",
    VERY_RARE_EVENT: "rgba(252,165,165,0.30)",
  };
  const PLAUSIBILITY_LABELS_ORDERED = ["HIGH", "NORMAL", "LOW", "VERY_RARE_EVENT"];

  const PLAUSIBILITY_TOOLTIP = {
    HIGH:            "✓ Normaal",
    NORMAL:          "✓ Normaal",
    LOW:             "⚠ Zelden gezien",
    VERY_RARE_EVENT: "⚠⚠ Historisch zeldzaam",
  };

  function getForecastRegime(f) {
    if (!f) return "normaal";
    if (f.regime) return f.regime;
    if (!f.factors) return "normaal";
    for (const fact of f.factors) {
      const r = (fact.reason || "").toLowerCase();
      if (r.includes("oversupply")) return "oversupply";
      if (r.includes("schaarste")) return "schaarste";
    }
    return "normaal";
  }

  // ---- Chart label formatters ----

  function fmtChartLabel(iso, isQuarterMode) {
    const d = new Date(iso);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const dayDiff = Math.floor((d - today) / 86400000);
    // In quarter-mode: vandaag+morgen altijd als tijd (HH:MM). Forecast-uren (>1) met dag erbij.
    if (dayDiff <= 1) return fmtTime(iso);
    const wd = d.toLocaleDateString("nl-NL", { weekday: "short" });
    return `${wd} ${fmtTime(iso)}`;
  }

  // ---- Chart bouw-helpers ----

  function buildBandDatasets(tl) {
    const sets = [];
    for (const label of PLAUSIBILITY_LABELS_ORDERED) {
      const lower = tl.map((t) => {
        if (t.kind !== "forecast") return null;
        return (t.forecast.event_plausibility_label || "NORMAL") === label
          ? priceCents(t.forecast.lower) : null;
      });
      const upper = tl.map((t) => {
        if (t.kind !== "forecast") return null;
        return (t.forecast.event_plausibility_label || "NORMAL") === label
          ? priceCents(t.forecast.upper) : null;
      });
      sets.push({
        label: `_forecast_lower_${label}`,
        data: lower, borderColor: "transparent",
        backgroundColor: PLAUSIBILITY_BAND_COLOR[label],
        pointRadius: 0, fill: "+1", tension: 0.25, spanGaps: false,
      });
      sets.push({
        label: `_forecast_upper_${label}`,
        data: upper, borderColor: "transparent",
        pointRadius: 0, fill: false, tension: 0.25, spanGaps: false,
      });
    }
    return sets;
  }

  // ---- renderResolutionToggle (alleen chart + toggle, geen volledige re-render) ----
  // Gebruikt intern door wireUI om alleen de grafiek opnieuw te tekenen zonder
  // alle andere DOM-elementen te beroeren. Snel en smooth.
  function switchResolution(res) {
    if (res !== "hourly" && res !== "quarter") return;
    if (res === state.chartResolution) return;
    state.chartResolution = res;
    saveStored(STORAGE_KEYS.chartRes, res);
    renderResolutionToggle();
    renderChart();
  }

  // ---- Hoofd chart-renderer ----
  function renderChart() {
    const canvas = document.getElementById("dayChart");
    if (!canvas || typeof Chart === "undefined") return;
    if (state.chart) { state.chart.destroy(); state.chart = null; }

    const holidays = buildHolidayLookup();
    const isQuarter = state.chartResolution === "quarter" && state.dayPrices15m.length > 0;

    // ── Prijszones (#57): drempelwaarden altijd in inclusive ct, omrekenen naar huidige modus ──
    function thToChart(ct_incl) {
      if (state.mode === "inclusive") return ct_incl;
      const taxes = state.config.taxes;
      return ct_incl / (taxes.btw_factor || 1.21) - (taxes.energiebelasting_per_kwh || 0) * 100;
    }
    const thr = (state.config && state.config.thresholds_ct_kwh_inclusive) || {};
    const priceZones = [
      { min: null,                            max: thToChart(0),                       color: "rgba(112,72,232,0.10)" },
      { min: thToChart(0),                    max: thToChart(thr.very_cheap ?? 14),    color: "rgba(26,122,49,0.08)"  },
      { min: thToChart(thr.very_cheap ?? 14), max: thToChart(thr.cheap ?? 22),         color: "rgba(47,158,68,0.07)"  },
      // normaal: geen kleur
      { min: thToChart(thr.pricey ?? 28),     max: thToChart(thr.very_pricey ?? 38),   color: "rgba(201,42,42,0.08)"  },
      { min: thToChart(thr.very_pricey ?? 38), max: null,                              color: "rgba(156,26,26,0.10)"  },
    ];

    // ── Timeline opbouwen ──────────────────────────────────────────────────────
    let timeline, chartNowIdx, boundaries;

    if (isQuarter) {
      // Quarter-mode: alleen vandaag + morgen in PT15M, geen prognose.
      timeline = state.dayPrices15m.map((p) => ({ kind: "actual", time: p.time, price: p.price }));
      chartNowIdx = state.nowIdx15m;

      // Grens tussen vandaag en morgen markeren.
      const tomorrowStr = (() => {
        const d = new Date();
        d.setDate(d.getDate() + 1);
        return d.toISOString().slice(0, 10);
      })();
      const tomorrowIdx = timeline.findIndex((pt) => pt.time.slice(0, 10) === tomorrowStr);
      boundaries = tomorrowIdx >= 0 ? [{ index: tomorrowIdx, label: "Morgen" }] : [];

    } else {
      // Hourly-mode: vandaag + morgen actueel + prognose dag 2+
      timeline = [
        ...state.dayPrices.map((p) => ({ kind: "actual", time: p.time, price: p.price })),
        ...state.forecasts.map((f) => ({ kind: "forecast", time: f.time, forecast: f })),
      ];
      chartNowIdx = state.nowIdx;

      // Grens tussen actuals en prognose markeren (enkel als er beide zijn).
      const boundaryIdx = state.dayPrices.length;
      boundaries = state.forecasts.length > 0
        ? [{ index: boundaryIdx, label: "Voorspelling", labelSide: "right", tint: true }]
        : [];
    }

    const n = timeline.length;
    const labels = timeline.map((t) => fmtChartLabel(t.time, isQuarter));

    // ── Dataset: actuele prijzen ───────────────────────────────────────────────
    const actualData = timeline.map((t) => t.kind === "actual" ? priceCents(t.price) : null);
    const actualColors = timeline.map((t, i) => {
      if (t.kind !== "actual") return "transparent";
      return i === chartNowIdx ? "#0f6cbd" : pointColor(t.price);
    });
    // In quarter-mode: kleinere punten (veel datapunten), grotere 'nu'-stip.
    const actualRadii = timeline.map((t, i) => {
      if (t.kind !== "actual") return 0;
      if (i === chartNowIdx) return 6;
      return isQuarter ? 2 : 3;
    });
    const actualHoverRadii = timeline.map((_, i) => i === chartNowIdx ? 7 : (isQuarter ? 4 : 5));

    // ── Datasets: prognose-banden en -lijn ────────────────────────────────────
    const FORECAST_DOT = "rgba(100, 116, 139, 0.75)";
    const forecastPointColors = timeline.map((t) =>
      t.kind === "forecast" ? FORECAST_DOT : "transparent"
    );
    const forecastPredicted = timeline.map((t) =>
      t.kind === "forecast" ? priceCents(t.forecast.predicted) : null
    );

    // ── X-as tick-configuratie per modus ──────────────────────────────────────
    // Hourly-mode (variant B): alleen kloktijden op vaste stappen — geen weekdag,
    // want de dag staat nu bovenin als label. 12u op breed scherm, 24u op smal.
    // De weekdag/datum komt uit dayBandPlugin; de uur-as blijft daardoor rustig.
    const narrow = typeof window !== "undefined" && window.innerWidth < 640;
    const hStep = narrow ? 24 : 12;
    const xTickCallback = isQuarter
      ? function (value, index) {
          const iso = timeline[index] && timeline[index].time;
          if (!iso) return "";
          const d = new Date(iso);
          return d.getMinutes() === 0 ? fmtTime(iso) : "";
        }
      : function (value, index) {
          const iso = timeline[index] && timeline[index].time;
          if (!iso) return "";
          const d = new Date(iso);
          return (d.getMinutes() === 0 && d.getHours() % hStep === 0) ? fmtTime(iso) : "";
        };

    const maxTicksLimit = isQuarter ? 24 : undefined;

    // ── Chart aanmaken ─────────────────────────────────────────────────────────
    state.chart = new Chart(canvas, {
      type: "line",
      plugins: [svPriceZonePlugin, dayBandPlugin, svBoundaryPlugin],
      data: {
        labels,
        datasets: [
          {
            label: `ct/kWh (${modeLabel()})`,
            data: actualData,
            tension: 0.25,
            borderColor: "#2e75b6",
            borderWidth: isQuarter ? 1.5 : 2,
            pointBackgroundColor: actualColors,
            pointBorderColor: actualColors,
            pointRadius: actualRadii,
            pointHoverRadius: actualHoverRadii,
            fill: { target: "origin", above: "rgba(46,117,182,0.08)" },
            spanGaps: false,
          },
          ...(isQuarter ? [] : buildBandDatasets(timeline)),
          ...(isQuarter ? [] : [{
            label: "voorspelling",
            data: forecastPredicted,
            borderColor: "rgba(46,117,182,0.5)",
            borderDash: [4, 4],
            borderWidth: 2,
            tension: 0.25,
            pointRadius: 3,
            pointBackgroundColor: forecastPointColors,
            pointBorderColor: forecastPointColors,
            pointHoverRadius: 5,
            fill: false,
            spanGaps: false,
          }]),
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 280 },
        interaction: { mode: "index", intersect: false },
        scales: {
          x: {
            ticks: {
              autoSkip: isQuarter,        // hourly: vaste 12u-stappen, geen autoSkip-misalignment
              maxTicksLimit,
              maxRotation: 0,             // nooit kantelen — voorkomt de chaotische schuine labels
              minRotation: 0,
              color: "#7c8a99",
              font: { size: 11 },
              callback: xTickCallback,
            },
            // Geen per-categorie gridlijnen meer (zou met autoSkip:false 100+ lijntjes
            // geven). De middernacht-scheidslijnen uit dayBandPlugin geven de structuur.
            grid: { display: false, drawTicks: false },
          },
          y: {
            ticks: { color: "#7c8a99", font: { size: 11 }, callback: (v) => v + " ct" },
            grid: { color: "rgba(0,0,0,0.06)" },
          },
        },
        plugins: {
          svPriceZone: { zones: priceZones },
          svDayBand: { timeline, holidays },
          svBoundary: { boundaries, n },
          legend: { display: false },
          tooltip: {
            filter: (item) => !item.dataset._isRef && (!item.dataset.label || !item.dataset.label.startsWith("_")),
            callbacks: {
              title: (items) => {
                const t = timeline[items[0].dataIndex];
                if (!t) return "";
                if (isQuarter && t.kind === "actual") {
                  // Kwartier-modus: toon "08:15 – 08:30"
                  const start = new Date(t.time);
                  const end   = new Date(start.getTime() + 15 * 60 * 1000);
                  const startFmt = start.toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
                  const endFmt   = end.toLocaleTimeString("nl-NL",   { hour: "2-digit", minute: "2-digit" });
                  const dateFmt  = start.toLocaleDateString("nl-NL", { weekday: "short", day: "numeric", month: "short" });
                  return `${dateFmt}  ${startFmt} – ${endFmt}`;
                }
                return fmtDateTime(t.time);
              },
              label: (item) => {
                const idx = item.dataIndex;
                const t = timeline[idx];
                if (t.kind === "actual") {
                  const eurMwh = t.price;
                  return [
                    `Kale EPEX: ${fmtNum(priceCentsRaw(eurMwh), 2)} ct/kWh`,
                    `Excl. belasting: ${fmtNum(priceCents(eurMwh, "exclusive"), 2)} ct/kWh`,
                    `Incl. belasting: ${fmtNum(priceCents(eurMwh, "inclusive"), 2)} ct/kWh`,
                  ];
                }
                if (!t.forecast) return [];
                const f = t.forecast;
                const regime = getForecastRegime(f);
                const regimeLbl = { oversupply: "Oversupply ☀️", schaarste: "Schaarste ❄️", normaal: "Normaal" }[regime];
                const halfBand = (f.upper - f.lower) / 2;
                const dateStr = t.time.slice(0, 10);
                const isNL = holidays.nl.has(dateStr);
                const isCross = holidays.crossborder.has(dateStr);
                const lines = [
                  `Regime: ${regimeLbl}`,
                  `Voorspeld: ${fmtNum(priceCents(f.predicted), 2)} ct/kWh`,
                  `Verwachte fout: ±${fmtNum(halfBand / 10, 1)} ct/kWh`,
                  `Baseline: ${fmtNum(priceCents(f.baseline), 2)} ct/kWh`,
                ];
                if (isNL && isCross) lines.push("NL + EU feestdag — lage prijs verwacht");
                else if (isNL) lines.push("NL feestdag — lage prijs verwacht");
                else if (isCross) lines.push("EU feestdag (buurlanden) — mogelijk lagere prijs");
                const plLabel = f.event_plausibility_label || "NORMAL";
                const plText  = PLAUSIBILITY_TOOLTIP[plLabel] || PLAUSIBILITY_TOOLTIP.NORMAL;
                lines.push(`Situatie: ${plText}`);
                const plN = f.analog_sample_size;
                // plN != null is false voor zowel null (fallback-weer) als undefined (pre-v2.1).
                if ((plLabel === "LOW" || plLabel === "VERY_RARE_EVENT") && plN != null) {
                  lines.push(`Vergelijkbare uren in log: ${plN}`);
                }
                if (f.realistic_negative_probability !== undefined) {
                  const pct = Math.round(f.realistic_negative_probability * 100);
                  lines.push(`Kans negatieve prijs: ~${pct}%`);
                }
                return lines;
              },
            },
          },
        },
      },
    });

    // ── Legenda onder de grafiek ───────────────────────────────────────────────
    const _existingLegend = document.getElementById("chart-regime-legend");
    if (_existingLegend) _existingLegend.remove();
    {
      const wrap = document.createElement("div");
      wrap.id = "chart-regime-legend";
      wrap.setAttribute("aria-hidden", "true");
      wrap.style.cssText = "margin:6px 0 0;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;line-height:1.8;";
      const dot = (color, label) =>
        `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:#374151;">` +
        `<span style="width:10px;height:10px;border-radius:50%;background:${color};flex-shrink:0;"></span>` +
        `<strong>${label}</strong></span>`;
      wrap.innerHTML =
        dot("#7048e8", "Gratis/negatief") +
        dot("#2f9e44", "Goedkoop") +
        dot("#d4a017", "Normaal") +
        dot("#c92a2a", "Duur") +
        dot("#0f6cbd", "Nu") +
        (isQuarter
          ? `<span style="font-size:11px;color:#6b7280;flex-basis:100%;">Kwartierlijkse day-ahead prijzen voor vandaag en morgen. Schakel naar "Per uur" voor de prognose tot volgende week.</span>`
          : `<span style="font-size:11px;color:#6b7280;flex-basis:100%;">Gekleurde blokken zijn feestdagen (geel NL, oranje EU) — op die dagen valt de prijs vaak extra laag.</span>`
        );
      (canvas.closest(".chart-wrapper") || canvas).insertAdjacentElement("afterend", wrap);

      // Context-zin (#63, optie 2): hoe liggen vandaag+morgen t.o.v. het 30-daags
      // gemiddelde? Vervangt de oude referentielijn — concreter en zonder grafiek-ruis.
      const _existingCtx = document.getElementById("chart-ref-context");
      if (_existingCtx) _existingCtx.remove();
      const avgSup = (state.config.suppliers || []).find((s) => s.id === "average");
      if (state.avg30d != null && state.dayPrices.length && avgSup) {
        const wk = state.dayPrices.reduce((a, p) => a + priceCentsForSupplier(p.price, avgSup), 0) / state.dayPrices.length;
        const pct = Math.round((wk - state.avg30d) / state.avg30d * 100);
        const avgTxt = fmtNum(state.avg30d, 0);
        const zin = Math.abs(pct) < 3
          ? `Vandaag en morgen liggen rond het gemiddelde van de afgelopen 30 dagen (${avgTxt} ct/kWh, incl. belasting).`
          : `Vandaag en morgen liggen gemiddeld ${Math.abs(pct)}% ${pct < 0 ? "onder" : "boven"} het gemiddelde van de afgelopen 30 dagen (${avgTxt} ct/kWh, incl. belasting).`;
        const ctxEl = document.createElement("p");
        ctxEl.id = "chart-ref-context";
        ctxEl.style.cssText = "margin:8px 0 0;font-size:12px;color:#6b7280;";
        ctxEl.textContent = zin;
        wrap.insertAdjacentElement("afterend", ctxEl);
      }
    }
  }

  // ---- Event wiring ----
  function wireUI() {
    // Incl./excl. belasting toggle
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

    // Instellingen-panel
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

    // Leverancier-selectie
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

    // Negatief-alert sluiten
    const negCloseBtn = document.getElementById("neg-alert-close");
    if (negCloseBtn) {
      negCloseBtn.addEventListener("click", () => {
        const banner = document.getElementById("neg-alert");
        if (!banner) return;
        const eventKey = banner.dataset.eventKey;
        if (eventKey) saveStored(STORAGE_KEYS.dismissedNegAlert, eventKey);
        hideNegAlert(banner);
      });
    }

    // Resolutie-toggle (per uur / per kwartier)
    document.querySelectorAll("[data-res-btn]").forEach((btn) => {
      btn.addEventListener("click", () => {
        switchResolution(btn.dataset.resBtn);
      });
    });
  }

  // ---- Boot ----
  function loadInitialState() {
    state.mode = loadStored(STORAGE_KEYS.mode, "inclusive") === "exclusive" ? "exclusive" : "inclusive";
    state.supplierId = loadStored(STORAGE_KEYS.supplier, "average");
    const cm = parseFloat(loadStored(STORAGE_KEYS.customMarkup, "0.025"));
    state.customMarkup = Number.isFinite(cm) ? cm : 0.025;
    const storedRes = loadStored(STORAGE_KEYS.chartRes, "hourly");
    state.chartResolution = storedRes === "quarter" ? "quarter" : "hourly";
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
    fetch("data/config.json",  { cache: "no-store" }).then((r) => { if (!r.ok) throw new Error("config HTTP " + r.status); return r.json(); }),
    fetch("data/prices.json",  { cache: "no-store" }).then((r) => { if (!r.ok) throw new Error("prices HTTP " + r.status); return r.json(); }),
    fetch("data/forecast.json", { cache: "no-store" }).then((r) => r.ok ? r.json() : null).catch(() => null),
  ])
    .then(([config, payload, forecastPayload]) => {
      state.config          = config;
      state.payload         = payload;
      state.forecastPayload = forecastPayload;

      // Uurprijzen (16d historie + 2d toekomst) — gebruikt door model, now-card, samenvatting, momenten.
      state.prices    = payload.prices || [];
      state.hasPt15m  = payload.has_pt15m === true;
      state.prices15m = payload.prices_15m || [];

      // Referentielijn 'wat is normaal' (#63): 30-daags gemiddelde consumentenprijs (ct incl. btw).
      state.avg30d       = (typeof payload.avg_30d_inclusive_ct === "number") ? payload.avg_30d_inclusive_ct : null;
      state.avg30dWindow = payload.avg_30d_window || null;

      const now = new Date();
      state.dayPrices   = filterTodayTomorrow(state.prices, now);
      state.nowIdx      = findCurrentIndex(state.dayPrices, now);

      // Kwartierdata voor vandaag + morgen (al gefilterd door de backend).
      state.dayPrices15m = state.prices15m;
      state.nowIdx15m    = findCurrentIndex(state.dayPrices15m, now);

      // Prognoses: dag 2+ (skip morgen als morgenochtend 00:00 al in dayPrices zit).
      const allForecasts = (forecastPayload && forecastPayload.forecasts) || [];
      const tomorrowStart       = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 0, 0, 0, 0);
      const dayAfterTomorrowStart = new Date(tomorrowStart.getTime() + 24 * 3600 * 1000);
      const hasTomorrowActuals  = state.dayPrices.some((p) => {
        const t = new Date(p.time);
        return t >= tomorrowStart && t < dayAfterTomorrowStart;
      });
      state.forecasts = hasTomorrowActuals
        ? allForecasts.filter((f) => new Date(f.time) >= dayAfterTomorrowStart)
        : allForecasts;

      applyConfigDefaults();
      wireUI();
      renderAll();
    })
    .catch((err) => {
      console.error("[stroomvoorspeller] Fatal:", err);
      showError();
    });
})();
