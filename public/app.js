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
    profile:           "sv.profile",            // JSON: { ev, solar, battery } — 'Mijn situatie'
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
    profile: { ev: false, solar: false, battery: false },  // 'Mijn situatie' — personalisatie
    momentWindow: 2,     // vensterduur (uren) voor de 'goedkoopste vensters'-lijst
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
  function loadProfile() {
    try {
      const raw = loadStored(STORAGE_KEYS.profile, "");
      if (!raw) return { ev: false, solar: false, battery: false };
      const o = JSON.parse(raw);
      return { ev: !!o.ev, solar: !!o.solar, battery: !!o.battery };
    } catch (e) { return { ev: false, solar: false, battery: false }; }
  }
  function saveProfile() {
    saveStored(STORAGE_KEYS.profile, JSON.stringify(state.profile));
  }
  function profileEmpty() {
    const p = state.profile || {};
    return !p.ev && !p.solar && !p.battery;
  }
  // Profiel-zin voor de nu-kaart. kind: 'laad' (goedkoop/gratis) of 'duur'. Leeg = geen toevoeging.
  function profielActie(kind) {
    const p = state.profile || {};
    if (kind === "laad") {
      const w = [];
      if (p.ev) w.push("EV");
      if (p.battery) w.push("batterij");
      if (!w.length) return "";
      return "Laad je " + (w.length === 2 ? "EV en batterij" : w[0]) + ".";
    }
    if (kind === "duur") {
      if (p.battery) return "Gebruik je thuisbatterij in plaats van het net.";
      if (p.ev) return "Stel EV-laden uit tot een goedkoper uur.";
      return "";
    }
    return "";
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

  // ---- #13: relatieve drempels (hybride, begrensd) ----
  // Goedkoop/duur volgen de kwartielen (P25/P75) van de getoonde prijzen, maar
  // begrensd op de vaste cheap/pricey-band uit config (22-28 ct): nooit "goedkoop"
  // boven 28, nooit "duur" onder 22. Negatief en de extremen (14/38) blijven
  // absoluut. Vlakke dagen (kwartielspreiding < 3 ct) vallen terug op de vaste
  // drempels; de banners blijven bewust op de absolute drempels (uitzonderings-
  // signalen, geen dagelijkse kwartielen).
  function relativeThresholds(cts, thr) {
    const fixedCheap  = thr.cheap  ?? 22;
    const fixedPricey = thr.pricey ?? 28;
    const vals = (cts || []).filter(Number.isFinite).sort((a, b) => a - b);
    if (vals.length < 12) return { cheap: fixedCheap, pricey: fixedPricey };
    const q = (frac) => {
      const i = (vals.length - 1) * frac, lo = Math.floor(i), hi = Math.ceil(i);
      return vals[lo] + (vals[hi] - vals[lo]) * (i - lo);
    };
    const p25 = q(0.25), p75 = q(0.75);
    if (p75 - p25 < 3) return { cheap: fixedCheap, pricey: fixedPricey };
    const clamp = (v) => Math.min(Math.max(v, fixedCheap), fixedPricey);
    return { cheap: clamp(p25), pricey: clamp(p75) };
  }
  let effThrCache = null;
  function effectiveThresholds() {
    const thr = (state.config && state.config.thresholds_ct_kwh_inclusive) || {};
    const d   = state.dayPrices || [];
    const key = state.supplierId + "|" + state.customMarkup + "|" + d.length + "|" + (d[0] ? d[0].time : "");
    if (effThrCache && effThrCache.key === key) return effThrCache;
    const cts = d.map((pp) => priceCents(pp.price, "inclusive"));
    const r = relativeThresholds(cts, thr);
    effThrCache = { key: key, cheap: r.cheap, pricey: r.pricey };
    return effThrCache;
  }

  function classify(eurMwh) {
    // Classificeer altijd op basis van de consumentenprijs incl. energiebelasting + btw
    // + leverancieropslag (= wat de bezoeker daadwerkelijk betaalt), niet op kale EPEX.
    const ct  = priceCents(eurMwh, "inclusive");
    const t   = state.config.thresholds_ct_kwh_inclusive || {};
    const eff = effectiveThresholds();
    if (ct < 0)                          return "negative";
    if (ct < (t.very_cheap ?? 14))       return "very_cheap";
    if (ct < eff.cheap)                  return "cheap";
    if (ct > (t.very_pricey ?? 38))      return "very_pricey";
    if (ct > eff.pricey)                 return "pricey";
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
      // #70: zelfde profiel-personalisatie als de nu-kaart en de week-strip.
      const laadZin = profielActie("laad");
      const duurZin = profielActie("duur");
      const negHours = tp.filter((p) => priceCents(p.price, "inclusive") < 0);

      if (negHours.length > 0) {
        const first = negHours[0];
        const last  = negHours[negHours.length - 1];
        setVariant("neg", "⚡");
        textEl.innerHTML =
          `<strong>Morgen negatieve prijzen</strong> tussen ${fmtTime(first.time)}–${fmtEnd(last.time)} — gratis stroom, apparaten aan! ` +
          (laadZin ? laadZin + " " : "") +
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
            (laadZin ? laadZin + " " : "") +
            `<a href="/morgen">Bekijk alle morgen-uren →</a>`;

        } else {
          const expBlock = findMostExpensiveBlock(tp, 2);
          if (expBlock && priceCents(expBlock.avg, "inclusive") > priceyThreshold) {
            setVariant("pricey", "⚠");
            textEl.innerHTML =
              `<strong>Morgen duur</strong> tussen ${fmtTime(expBlock.startIso)}–${fmtEnd(expBlock.endIso)} — zware apparaten liever 's nachts. ` +
              (duurZin ? duurZin + " " : "") +
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
      // ── Nog geen morgen-data → banner verbergen ────────────────────────────────
      // De huidige status + advies staat al in de now-card (zie renderNowAdvice).
      // Een "Nu ..."-regel hier zou die kaart dubbelen, dus tonen we niets tot
      // morgen-data beschikbaar is (dan verschijnt de morgen-vooruitblik hierboven).
      el.setAttribute("hidden", "");
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
    renderForecastHighlights();
    renderForecastChart();
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
    renderNowAdvice(prices, nowIdx, current, cls);
  }

  // ---- Instap-advies: vertaalt de prijsstatus naar een concrete actie in gewone taal ----
  // Bewust géén nieuw blok of dubbele cijfers: één advieszin + een optionele vooruitwijzing.
  // Verdict volgt classify() (altijd op de incl.-prijs), zodat de kaart de grafiek niet tegenspreekt.
  function renderNowAdvice(prices, nowIdx, current, cls) {
    const adviceEl = document.querySelector('[data-field="now-advice"]');
    const nextEl   = document.querySelector('[data-field="now-advice-next"]');
    if (!adviceEl) return;

    const advice = {
      negative:    "Stroom kost nu niets — een goed moment om wasmachine, droger of auto tegelijk aan te zetten.",
      very_cheap:  "Uitstekend moment. Wasmachine, droger, vaatwasser of auto laden kan nu.",
      cheap:       "Goed moment voor wasmachine, droger of auto laden.",
      normal:      "Prima moment. Kan het wachten, dan is het straks vaak goedkoper.",
      pricey:      "Aan de dure kant — stel zware apparaten liever even uit.",
      very_pricey: "Nu duur. Stel wasmachine, droger en auto laden uit tot een goedkoper uur.",
    };
    let adviceText = advice[cls] || advice.normal;
    const profielKind = (cls === "negative" || cls === "very_cheap" || cls === "cheap") ? "laad"
                      : (cls === "pricey" || cls === "very_pricey") ? "duur" : "";
    const profielZin = profielActie(profielKind);
    if (profielZin) adviceText += " " + profielZin;
    adviceEl.textContent = adviceText;

    if (!nextEl) return;
    const isCheapNow  = (cls === "negative" || cls === "very_cheap" || cls === "cheap");
    const fmtEnd2 = (isoEnd) =>
      new Date(new Date(isoEnd).getTime() + 3600000)
        .toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });

    let html = "";
    if (!isCheapNow) {
      // Wijs naar het eerstvolgende goedkopere 2-uurs venster van VANDAAG — niet morgen.
      // (Zonder dagfilter zou findBestMoments morgen-uren kunnen kiezen; die tonen
      //  we hier zonder datum, wat een onzinnig "wacht tot een al voorbij uur" oplevert.)
      const todayAhead = prices.filter(
        (p) => isSameLocalDay(p.time, current.time) && new Date(p.time) > new Date(current.time)
      );
      const upcoming = findBestMoments(todayAhead, 0, 1, 2);
      if (upcoming.length && upcoming[0].avg < current.price) {
        const m = upcoming[0];
        html = `Goedkoper rond ${fmtTime(m.startIso)}–${fmtEnd2(m.endIso)} · ${fmtCents(m.avg)} ct/kWh`;
      }
    } else {
      // Goedkoop nu: waarschuw alleen als er straks vandaag nog een duur uur aankomt.
      const rest = prices.filter(
        (p) => isSameLocalDay(p.time, current.time) && new Date(p.time) > new Date(current.time)
      );
      if (rest.length) {
        const peak = rest.reduce((a, b) => (a.price >= b.price ? a : b));
        const pc = classify(peak.price);
        if (pc === "pricey" || pc === "very_pricey") {
          html = `Duurste moment straks: ${fmtTime(peak.time)} · ${fmtCents(peak.price)} ct/kWh`;
        }
      }
    }

    if (html) { nextEl.textContent = html; nextEl.hidden = false; }
    else      { nextEl.hidden = true; }
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
    const prices = state.dayPrices;  // altijd uurresolutie voor de venster-lijst
    const fromIdx = state.nowIdx >= 0 ? state.nowIdx : 0;
    const w = state.momentWindow || 2;
    const moments = findBestMoments(prices, fromIdx, 3, w);
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
          <small>(${w} uur)</small>
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

    // 'Mijn situatie' — checkbox-status spiegelen aan state.profile
    const evCb = document.getElementById("profile-ev");
    const solarCb = document.getElementById("profile-solar");
    const batteryCb = document.getElementById("profile-battery");
    if (evCb) evCb.checked = !!state.profile.ev;
    if (solarCb) solarCb.checked = !!state.profile.solar;
    if (batteryCb) batteryCb.checked = !!state.profile.battery;
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
      if (note) note.hidden = true;
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
      const { timeline, holidays, hideWeekdayLabel } = opts;
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
        else if (hideWeekdayLabel) {
          return; // weekdag staat al als kolomkop boven de grafiek
        }
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

  // ---- Prijszones (#57): drempelwaarden altijd in inclusive ct, omrekenen naar huidige modus ----
  function buildPriceZones() {
    function thToChart(ct_incl) {
      if (state.mode === "inclusive") return ct_incl;
      const taxes = state.config.taxes;
      return ct_incl / (taxes.btw_factor || 1.21) - (taxes.energiebelasting_per_kwh || 0) * 100;
    }
    const thr = (state.config && state.config.thresholds_ct_kwh_inclusive) || {};
    const eff = effectiveThresholds(); // #13: cheap/pricey-grens beweegt mee met de dag
    return [
      { min: null,                            max: thToChart(0),                       color: "rgba(112,72,232,0.10)" },
      { min: thToChart(0),                    max: thToChart(thr.very_cheap ?? 14),    color: "rgba(26,122,49,0.08)"  },
      { min: thToChart(thr.very_cheap ?? 14), max: thToChart(eff.cheap),               color: "rgba(47,158,68,0.07)"  },
      // normaal: geen kleur
      { min: thToChart(eff.pricey),           max: thToChart(thr.very_pricey ?? 38),   color: "rgba(201,42,42,0.08)"  },
      { min: thToChart(thr.very_pricey ?? 38), max: null,                              color: "rgba(156,26,26,0.10)"  },
    ];
  }

  // ---- Gedeelde grafiek-builder ----
  // Tekent één prijs-grafiek (dag óf voorspelling) op het opgegeven canvas en geeft
  // de Chart-instance terug. De aanroeper levert de timeline + opties; legenda en
  // context-zin worden door de aanroeper toegevoegd.
  function buildPriceChart(canvas, { timeline, isQuarter, chartNowIdx, boundaries, holidays, priceZones, yMin, yMax, hideWeekdayLabel }) {
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
    const hStep = narrow ? 12 : 6;
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
    const chart = new Chart(canvas, {
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
            min: yMin,
            max: yMax,
            ticks: { color: "#7c8a99", font: { size: 11 }, callback: (v) => v + " ct" },
            grid: { color: "rgba(0,0,0,0.06)" },
          },
        },
        plugins: {
          svPriceZone: { zones: priceZones },
          svDayBand: { timeline, holidays, hideWeekdayLabel },
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

    return chart;
  }

  // ---- Dag-grafieken: vandaag en morgen als twee losse grafieken naast elkaar ----
  function renderChart() {
    const canvasToday    = document.getElementById("dayChartToday");
    const canvasTomorrow = document.getElementById("dayChartTomorrow");
    const splitEl        = document.getElementById("day-charts-split");
    if (!canvasToday || !canvasTomorrow || typeof Chart === "undefined") return;
    if (state.chartToday)    { state.chartToday.destroy();    state.chartToday = null; }
    if (state.chartTomorrow) { state.chartTomorrow.destroy(); state.chartTomorrow = null; }

    const holidays   = buildHolidayLookup();
    const priceZones = buildPriceZones();
    const isQuarter  = state.chartResolution === "quarter" && state.dayPrices15m.length > 0;

    // Actuals: vandaag + morgen (voor zover gepubliceerd).
    const timeline = isQuarter
      ? state.dayPrices15m.map((p) => ({ kind: "actual", time: p.time, price: p.price }))
      : state.dayPrices.map((p) => ({ kind: "actual", time: p.time, price: p.price }));
    const chartNowIdx = isQuarter ? state.nowIdx15m : state.nowIdx;

    // Is morgen nog niet bekend? Dan de voorspelling voor morgen tonen (alleen uur-modus).
    const tomorrowForecast = state.tomorrowForecast || [];
    if (!isQuarter && tomorrowForecast.length) {
      tomorrowForecast.forEach((f) => timeline.push({ kind: "forecast", time: f.time, forecast: f }));
    }

    // Splitsen op datum: het eerste dag-blok is vandaag, de rest is morgen.
    const firstDate  = timeline.length ? timeline[0].time.slice(0, 10) : null;
    const todayTL    = timeline.filter((t) => t.time.slice(0, 10) === firstDate);
    const tomorrowTL = timeline.filter((t) => t.time.slice(0, 10) !== firstDate);
    const tomorrowIsForecast = tomorrowTL.length > 0 && tomorrowTL.every((t) => t.kind === "forecast");

    // Gedeelde y-schaal, zodat de twee grafieken eerlijk te vergelijken zijn.
    const yVals = [];
    timeline.forEach((t) => {
      if (t.kind === "actual") yVals.push(priceCents(t.price));
      else if (t.forecast) yVals.push(priceCents(t.forecast.lower), priceCents(t.forecast.upper));
    });
    let yMin, yMax;
    if (yVals.length) {
      const lo = Math.min(...yVals), hi = Math.max(...yVals);
      const pad = Math.max(2, (hi - lo) * 0.08);
      yMin = Math.floor(lo - pad);
      yMax = Math.ceil(hi + pad);
    }

    // Kolomkoppen met de datum.
    const titleFmt = (d) => d.toLocaleDateString("nl-NL", { weekday: "short", day: "numeric", month: "short" }).replace(/\./g, "");
    const todayD = new Date(); todayD.setHours(0, 0, 0, 0);
    const tomorrowD = new Date(todayD); tomorrowD.setDate(tomorrowD.getDate() + 1);
    const tEl = document.getElementById("day-title-today");
    const mEl = document.getElementById("day-title-tomorrow");
    if (tEl) tEl.textContent = `Vandaag · ${titleFmt(todayD)}`;
    if (mEl) mEl.textContent = tomorrowTL.length
      ? `Morgen · ${titleFmt(tomorrowD)}${tomorrowIsForecast ? " · voorspelling" : ""}`
      : "Morgen";

    // Vandaag: met 'nu'-stip.
    state.chartToday = buildPriceChart(canvasToday, {
      timeline: todayTL, isQuarter, chartNowIdx, boundaries: [], holidays, priceZones,
      yMin, yMax, hideWeekdayLabel: true,
    });

    // Morgen: geen 'nu'-stip. Bij nog niet gepubliceerde prijzen tonen we de voorspelling
    // (gestippelde lijn) — de kolomkop meldt dat al. Is er niets, verberg de kolom.
    const tomorrowCol = canvasTomorrow.closest(".day-chart-col");
    if (tomorrowTL.length) {
      if (tomorrowCol) tomorrowCol.style.display = "";
      state.chartTomorrow = buildPriceChart(canvasTomorrow, {
        timeline: tomorrowTL, isQuarter, chartNowIdx: -1, boundaries: [], holidays, priceZones,
        yMin, yMax, hideWeekdayLabel: true,
      });
    } else if (tomorrowCol) {
      tomorrowCol.style.display = "none";
    }

    // ── Gedeelde legenda onder beide grafieken ─────────────────────────────────
    const _existingLegend = document.getElementById("chart-regime-legend");
    if (_existingLegend) _existingLegend.remove();
    if (splitEl) {
      const wrap = document.createElement("div");
      wrap.id = "chart-regime-legend";
      wrap.setAttribute("aria-hidden", "true");
      wrap.style.cssText = "margin:10px 0 0;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;line-height:1.8;";
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
          ? `<span style="font-size:11px;color:#6b7280;flex-basis:100%;">Kwartierlijkse day-ahead prijzen. Beide grafieken delen dezelfde schaal.</span>`
          : `<span style="font-size:11px;color:#6b7280;flex-basis:100%;">Gekleurde blokken zijn feestdagen (geel NL, oranje EU) — op die dagen valt de prijs vaak extra laag. Beide grafieken delen dezelfde schaal.</span>`
        ) +
        (tomorrowIsForecast
          ? `<span style="font-size:11px;color:#6b7280;flex-basis:100%;">De <strong>gestippelde lijn</strong> bij morgen is de voorspelling — de day-ahead prijzen voor morgen zijn nog niet gepubliceerd.</span>`
          : ``);
      splitEl.insertAdjacentElement("afterend", wrap);

      // Context-zin (#63, optie 2): hoe liggen vandaag+morgen t.o.v. het 30-daags gemiddelde?
      const _existingCtx = document.getElementById("chart-ref-context");
      if (_existingCtx) _existingCtx.remove();
      const avgSup = (state.config.suppliers || []).find((s) => s.id === "average");
      if (state.avg30d != null && state.dayPrices.length && avgSup) {
        const wk = state.dayPrices.reduce((a, p) => a + priceCentsForSupplier(p.price, avgSup), 0) / state.dayPrices.length;
        const pct = Math.round((wk - state.avg30d) / state.avg30d * 100);
        const avgTxt = fmtNum(state.avg30d, 0);
        const dagLabel = state.hasTomorrowActuals ? "Vandaag en morgen liggen" : "Vandaag ligt";
        const zin = Math.abs(pct) < 3
          ? `${dagLabel} rond het gemiddelde van de afgelopen 30 dagen (${avgTxt} ct/kWh, incl. belasting).`
          : `${dagLabel} gemiddeld ${Math.abs(pct)}% ${pct < 0 ? "onder" : "boven"} het gemiddelde van de afgelopen 30 dagen (${avgTxt} ct/kWh, incl. belasting).`;
        const ctxEl = document.createElement("p");
        ctxEl.id = "chart-ref-context";
        ctxEl.style.cssText = "margin:8px 0 18px;font-size:12px;color:#6b7280;";
        ctxEl.textContent = zin;
        wrap.insertAdjacentElement("afterend", ctxEl);
      }
    }
  }

  // ---- Voorspelling-hoogtepunten: gedateerde regels boven de weekgrafiek ----
  // Optie B (#kop-van-de-week), gepersonaliseerd op 'Mijn situatie' (state.profile).
  // Drempelgestuurd; dagnamen voluit (dinsdagmiddag). De markt-negatief-melding is
  // verplaatst naar een eigen zon-kaart die alleen verschijnt met zonnepanelen aan,
  // zodat de generieke strip niet tegenstrijdig is met de incl-belasting grafiek.
  function renderForecastHighlights() {
    const el    = document.getElementById("forecast-highlights");
    const nudge = document.getElementById("profile-nudge");
    if (!el || !state.config) return;

    const fc = state.forecasts || [];
    if (fc.length === 0) {
      el.setAttribute("hidden", ""); el.innerHTML = "";
      if (nudge) nudge.setAttribute("hidden", "");
      return;
    }

    const thr      = state.config.thresholds_ct_kwh_inclusive || {};
    const NEG_PROB = 0.30; // model-kans op negatieve kale marktprijs
    const p        = state.profile || {};

    const hours = fc.map((f) => {
      const ct   = priceCents(f.predicted, "inclusive");
      const prob = typeof f.P_negative === "number" ? f.P_negative : null;
      return { time: f.time, t: new Date(f.time), ct, prob };
    });

    // #13: goedkoopst/duurst relatief aan het voorspelvenster (begrensd), zodat
    // er ook in vlakke zomers/winters altijd een bruikbaar signaal is.
    const effW    = relativeThresholds(hours.map((h) => h.ct), thr);
    const cheapT  = effW.cheap;
    const priceyT = effW.pricey;

    function dagdeelLabel(d) {
      const h    = d.getHours();
      const deel = h < 6 ? "nacht" : h < 12 ? "ochtend" : h < 18 ? "middag" : "avond";
      const dag  = d.toLocaleDateString("nl-NL", { weekday: "long" }); // voluit: "dinsdag"
      const s    = dag + deel;
      return s.charAt(0).toUpperCase() + s.slice(1);
    }
    function rondTijd(d) { return "rond " + String(d.getHours()).padStart(2, "0") + ":00"; }

    // Profiel-actie per situatie. Leeg = geen actieregel.
    function laadActie() {
      const w = [];
      if (p.ev) w.push("EV");
      if (p.battery) w.push("batterij");
      if (!w.length) return "";
      return "Laad je " + (w.length === 2 ? "EV en batterij" : w[0]);
    }
    function duurActie() {
      if (p.battery) return "Gebruik je batterij, niet het net";
      if (p.ev) return "Stel EV-laden uit tot een goedkoper uur";
      return "";
    }

    const lead = [];  // gratis (vooraan)
    const mid  = [];  // goedkoopst, duurste
    const tail = [];  // zon-overschot (achteraan, alleen met zonnepanelen)
    const used = new Set();

    // Gratis stroom — alleen als je incl. belasting écht onder nul betaalt (klopt met grafiek).
    const gratis = hours.filter((h) => h.ct < 0).sort((a, b) => a.ct - b.ct)[0];
    if (gratis) {
      used.add(gratis.time);
      lead.push({ cls: "fh-neg", icon: "⚡", label: "Gratis stroom",
        title: dagdeelLabel(gratis.t),
        detail: `incl. belasting ~${fmtNum(gratis.ct, 0)} ct ${rondTijd(gratis.t)}`,
        action: laadActie() });
    } else if (p.solar) {
      // Zon-overschot — kale markt onder nul. Alleen voor zonnepaneel-bezitters: dan loont
      // zelf verbruiken meer dan terugleveren. Geen prijsgetal (incl. blijft positief), dus
      // geen verwarrende vergelijking met 'Goedkoopst'.
      const markt = hours
        .filter((h) => h.prob !== null && h.prob >= NEG_PROB)
        .sort((a, b) => (b.prob ?? 0) - (a.prob ?? 0))[0];
      if (markt) {
        used.add(markt.time);
        tail.push({ cls: "fh-solar", icon: "☀", label: "Zon-overschot",
          title: dagdeelLabel(markt.t),
          detail: `markt onder nul ${rondTijd(markt.t)}`,
          action: "Verbruik je eigen zon" });
      }
    }

    // Goedkoopst — laagste niet-negatieve prijs onder de drempel.
    let minH = null;
    hours.forEach((h) => { if (!used.has(h.time) && h.ct >= 0 && (!minH || h.ct < minH.ct)) minH = h; });
    if (minH && minH.ct < cheapT) {
      used.add(minH.time);
      mid.push({ cls: "fh-cheap", icon: "💰", label: "Goedkoopst",
        title: dagdeelLabel(minH.t),
        detail: `~${fmtNum(minH.ct, 0)} ct ${rondTijd(minH.t)}`,
        action: laadActie() });
    }

    // Duurste moment — hoogste prijs boven de drempel.
    let maxH = null;
    hours.forEach((h) => { if (!used.has(h.time) && (!maxH || h.ct > maxH.ct)) maxH = h; });
    if (maxH && maxH.ct > priceyT) {
      used.add(maxH.time);
      mid.push({ cls: "fh-pricey", icon: "⚠", label: "Duurste moment",
        title: dagdeelLabel(maxH.t),
        detail: `~${fmtNum(maxH.ct, 0)} ct ${rondTijd(maxH.t)}`,
        action: duurActie() });
    }

    let cards = [...lead, ...mid, ...tail];

    // Niets haalt een drempel → één neutrale regel met het weekgemiddelde.
    if (cards.length === 0) {
      const avg = hours.reduce((s, h) => s + h.ct, 0) / hours.length;
      cards = [{ cls: "fh-neutral", icon: "📊", label: "Komende week",
        title: "Stabiele prijzen", detail: `gemiddeld rond ${fmtNum(avg, 0)} ct/kWh` }];
    }

    el.innerHTML = cards.map((c) =>
      `<div class="fh-card ${c.cls}">` +
        `<div class="fh-label"><span class="fh-icon" aria-hidden="true">${c.icon}</span>${c.label}</div>` +
        `<div class="fh-title">${c.title}</div>` +
        `<div class="fh-detail">${c.detail}</div>` +
        (c.action ? `<div class="fh-action">${c.action}</div>` : "") +
      `</div>`
    ).join("");
    el.removeAttribute("hidden");

    // Nudge tonen zolang er nog geen profiel is ingesteld.
    if (nudge) {
      if (profileEmpty()) nudge.removeAttribute("hidden");
      else nudge.setAttribute("hidden", "");
    }
  }

  // ---- Voorspellingsgrafiek: dag 2 t/m 7 ----
  function renderForecastChart() {
    const canvas = document.getElementById("forecastChart");
    if (!canvas || typeof Chart === "undefined") return;
    if (state.forecastChart) { state.forecastChart.destroy(); state.forecastChart = null; }

    const wrapper = canvas.closest(".chart-wrapper");
    if (!state.forecasts || state.forecasts.length === 0) {
      // Geen prognose beschikbaar — grafiek verbergen, de uitleg eronder blijft staan.
      if (wrapper) wrapper.hidden = true;
      return;
    }
    if (wrapper) wrapper.hidden = false;

    const holidays   = buildHolidayLookup();
    const priceZones = buildPriceZones();
    const timeline   = state.forecasts.map((f) => ({ kind: "forecast", time: f.time, forecast: f }));

    // Dag-scheidslijnen (middernacht) tekent dayBandPlugin al; losse boundary niet nodig.
    state.forecastChart = buildPriceChart(canvas, {
      timeline, isQuarter: false, chartNowIdx: -1, boundaries: [], holidays, priceZones,
    });

    // ── Legenda onder de voorspellingsgrafiek ──────────────────────────────────
    const _existingFc = document.getElementById("forecast-regime-legend");
    if (_existingFc) _existingFc.remove();
    {
      const wrap = document.createElement("div");
      wrap.id = "forecast-regime-legend";
      wrap.setAttribute("aria-hidden", "true");
      wrap.style.cssText = "margin:6px 0 0;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;line-height:1.8;";
      wrap.innerHTML =
        `<span style="display:inline-flex;align-items:center;gap:6px;font-size:11px;color:#374151;">` +
        `<span style="width:18px;border-top:2px dashed rgba(46,117,182,0.7);flex-shrink:0;"></span>` +
        `<strong>Voorspelde prijs</strong></span>` +
        `<span style="display:inline-flex;align-items:center;gap:6px;font-size:11px;color:#374151;">` +
        `<span style="width:14px;height:10px;border-radius:2px;background:rgba(147,197,253,0.45);flex-shrink:0;"></span>` +
        `<strong>Onzekerheidsband</strong></span>` +
        `<span style="font-size:11px;color:#6b7280;flex-basis:100%;">Eigen model voor de dagen ná morgen. Hoe verder vooruit, hoe breder de band. Geen garantie — bij extreme situaties (PV-overschot, gascrisis, centrale-uitval) kan de prijs erbuiten vallen.</span>`;
      (canvas.closest(".chart-wrapper") || canvas).insertAdjacentElement("afterend", wrap);
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

    // Vensterduur-chips bij 'goedkoopste vensters' (1/2/3/5 uur)
    document.querySelectorAll("#moment-chips button").forEach((btn) => {
      btn.addEventListener("click", () => {
        const h = parseInt(btn.dataset.hours, 10);
        if (!h || h === state.momentWindow) return;
        state.momentWindow = h;
        document.querySelectorAll("#moment-chips button").forEach((b) => b.classList.toggle("on", b === btn));
        renderMoments();
      });
    });

    // Trefzekerheid-regel onder de voorspelling-intro (zelfde bron als /over/performance)
    const accEl = document.getElementById("forecast-accuracy");
    if (accEl) {
      fetch("/data/performance.json?nocache=" + Date.now())
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          const dist = d && d.overall && d.overall.error_distribution;
          const share = dist && Array.isArray(dist.share_within)
            ? dist.share_within.find((x) => x.ct === 5) : null;
          if (!share || !share.pct) return;
          const tienden = Math.round(share.pct * 10);
          const periode = d.evaluation_window_days ? `afgelopen ${d.evaluation_window_days} dagen` : "afgelopen weken";
          accEl.innerHTML = `In de ${periode} zat ${tienden} van de 10 voorspelde uren binnen ±5 ct van de echte prijs — elke dag opnieuw gemeten. <a href="/over/performance">Bekijk alle cijfers →</a>`;
          accEl.hidden = false;
        })
        .catch(() => {});
    }

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

    // 'Mijn situatie' — kenmerken aan/uit
    [["profile-ev", "ev"], ["profile-solar", "solar"], ["profile-battery", "battery"]].forEach(([id, key]) => {
      const cb = document.getElementById(id);
      if (!cb) return;
      cb.addEventListener("change", (e) => {
        state.profile[key] = !!e.target.checked;
        saveProfile();
        renderAll();
      });
    });

    // Nudge boven de week-strip → open instellingen en scroll erheen
    const nudge = document.getElementById("profile-nudge");
    if (nudge) {
      nudge.addEventListener("click", () => {
        const panel = document.getElementById("settings-panel");
        const toggleBtn = document.getElementById("settings-toggle");
        if (panel) {
          panel.removeAttribute("hidden");
          if (toggleBtn) toggleBtn.setAttribute("aria-expanded", "true");
          panel.scrollIntoView({ behavior: "smooth", block: "center" });
          const evCb = document.getElementById("profile-ev");
          if (evCb) evCb.focus({ preventScroll: true });
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
    state.profile = loadProfile();
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

      // Prognoses splitsen over twee grafieken.
      const allForecasts = (forecastPayload && forecastPayload.forecasts) || [];
      const tomorrowStart       = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 0, 0, 0, 0);
      const dayAfterTomorrowStart = new Date(tomorrowStart.getTime() + 24 * 3600 * 1000);
      const hasTomorrowActuals  = state.dayPrices.some((p) => {
        const t = new Date(p.time);
        return t >= tomorrowStart && t < dayAfterTomorrowStart;
      });
      state.hasTomorrowActuals = hasTomorrowActuals;

      // Onderste grafiek: altijd vanaf overmorgen.
      state.forecasts = allForecasts.filter((f) => new Date(f.time) >= dayAfterTomorrowStart);

      // Bovenste grafiek: voorspelling vóór morgen alleen tonen zolang de day-ahead
      // prijzen voor morgen nog niet gepubliceerd zijn (meestal vóór ~14:00 op werkdagen).
      state.tomorrowForecast = hasTomorrowActuals
        ? []
        : allForecasts.filter((f) => {
            const t = new Date(f.time);
            return t >= tomorrowStart && t < dayAfterTomorrowStart;
          });

      applyConfigDefaults();
      wireUI();
      renderAll();
    })
    .catch((err) => {
      console.error("[stroomvoorspeller] Fatal:", err);
      showError();
    });
})();
