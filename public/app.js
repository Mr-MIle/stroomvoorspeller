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
    dismissedNegAlert: "sv.dismissedNegAlert", // ISO-tijd van het event waarvoor de banner gesloten is
  };

  const state = {
    config: null,
    payload: null,
    forecastPayload: null,
    prices: [],          // alle prijzen uit prices.json (14d historie + 2d toekomst)
    dayPrices: [],       // gefilterd: alleen vandaag + morgen voor now-card/grafiek
    forecasts: [],       // overmorgen t/m +7d uit forecast.json
    nowIdx: -1,          // index in dayPrices
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

  // Bereken ct/kWh voor een specifieke supplier (gebruikt door de aanbieders-tabel).
  // Gebruikt altijd "incl. belasting" omdat de tabel die kolom toont; mode-toggle
  // raakt deze functie niet.
  function priceCentsForSupplier(eurMwh, supplier) {
    const epex_per_kwh = eurMwh / 1000;
    const markup = Number(supplier.markup_per_kwh) || 0;
    const t = state.config.taxes;
    const subtotal = epex_per_kwh + markup + (t.energiebelasting_per_kwh || 0);
    return subtotal * (t.btw_factor || 1) * 100;
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

  // Bouw twee Sets van feestdagen uit config.json:
  //   nl          — officiële NL vrije dagen
  //   crossborder — dagen waarop DE+BE (en vaak FR) ook vrij zijn
  // Beide sets bevatten datumstrings "YYYY-MM-DD".
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
    // Knip alle prijzen weg vóór "vandaag 00:00" en na "overmorgen 00:00".
    // De volledige prices.json bevat 14d historie + 2d toekomst sinds v1.6;
    // de UI toont alleen de relevante 48 uur.
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0, 0);
    const dayAfterTomorrow = new Date(todayStart.getTime() + 48 * 3600 * 1000);
    return prices.filter((p) => {
      const t = new Date(p.time);
      return t >= todayStart && t < dayAfterTomorrow;
    });
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

  // ---- Negatieve-prijs detectie ----
  // Zoek aaneengesloten windows van uren met all-in prijs (incl. btw, met de
  // gekozen leverancier-opslag) onder de drempel uit config.negative_price_alert.
  // Verleden uren tellen niet mee — we waarschuwen alleen voor het nu/aankomend.
  function findNegativePriceWindows() {
    const cfg = (state.config && state.config.negative_price_alert) || {};
    if (!cfg.enabled) return [];
    const threshold = Number(cfg.threshold_cents_inclusive);
    if (!Number.isFinite(threshold)) return [];

    const prices = state.dayPrices;
    const now = Date.now();
    const windows = [];
    let current = null;
    for (let i = 0; i < prices.length; i++) {
      const p = prices[i];
      // Een uur is afgelopen als z'n eindtijd (start + 1u) al voorbij is
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
    // Event-ID = starttijd eerste negatief uur. Bij dismiss onthouden we die,
    // zodat de banner weg blijft tot er een NIEUW event ontstaat (andere starttijd).
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

  // ---- Rendering ----
  function renderAll() {
    if (!state.config || !state.dayPrices.length) return;
    renderNegativeAlert();
    renderSettingsPanel();
    renderSettingsToggle();
    renderModeBadges();
    renderNowCard();
    renderSummary();
    renderSupplierTable();
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
    const prices = state.dayPrices;
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
    const prices = state.dayPrices;
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
    const prices = state.dayPrices;
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

  // Update de header-knop met de naam van de huidige leverancier zodat duidelijk is
  // dat je daar (1) je leverancier kiest en (2) welke nu actief is.
  function renderSettingsToggle() {
    const supplier = getSupplier();
    setText("settings-toggle-value", supplier.name || "—");
  }

  // Tabel met alle aanbieders + hun all-in prijs voor het huidige uur, gesorteerd
  // op prijs oplopend. De geselecteerde aanbieder wordt visueel gemarkeerd.
  // 'custom' wordt overgeslagen — dat is geen echte aanbieder, alleen een eigen waarde.
  function renderSupplierTable() {
    const tbody = document.querySelector('[data-field="suppliers-tbody"]');
    if (!tbody) return;
    const prices = state.dayPrices;
    if (!prices.length) return;
    const current = state.nowIdx >= 0 ? prices[state.nowIdx] : prices[0];
    if (!current) return;

    // Tijd-label voor de sectie-kop
    setText("suppliers-now-time", `Nu, ${fmtTime(current.time)}`);

    // Pak de oudste 'verified' datum als veilige conservatieve weergave —
    // vertelt eerlijk wanneer we voor het laatst alle tarieven hebben gecontroleerd.
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
      .map((s) => ({
        supplier: s,
        cents: priceCentsForSupplier(current.price, s),
      }))
      .sort((a, b) => a.cents - b.cents);

    tbody.innerHTML = "";
    rows.forEach((r) => {
      const s = r.supplier;
      const tr = document.createElement("tr");
      if (s.id === state.supplierId) tr.className = "is-mine";

      // Cel 1: aanbieder-naam, eventueel als hyperlink naar hun website
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

      // Cel 2: prijs nu incl. btw
      const tdPrice = document.createElement("td");
      tdPrice.className = "td-price";
      tdPrice.textContent = fmtNum(r.cents, 1);
      tr.appendChild(tdPrice);

      // Cel 3: opslag €/kWh excl. btw
      const tdMarkup = document.createElement("td");
      tdMarkup.className = "td-markup";
      tdMarkup.textContent = `€${fmtNum(s.markup_per_kwh, 4)}`;
      tr.appendChild(tdMarkup);

      // Cel 4: vast/maand (kan 0 zijn voor 'average')
      const tdFixed = document.createElement("td");
      tdFixed.className = "td-fixed";
      const fx = Number(s.fixed_per_month) || 0;
      tdFixed.textContent = fx ? `€${fmtNum(fx, 2)}` : "—";
      tr.appendChild(tdFixed);

      // Cel 5: Kies-knop (item #40 — v2-uitbreiding van #38).
      // Hergebruikt exact dezelfde flow als de dropdown in het settings-paneel:
      // state.supplierId update + localStorage write + renderAll().
      // Actieve rij toont een disabled "Geselecteerd" zodat de feedback-loop
      // duidelijk is zonder een tweede klikbare zone op de eigen keuze.
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
        // 'average' is conceptueel een fallback, geen specifieke leverancier.
        // Eigen label maakt duidelijk dat je dan "geen voorkeur" kiest.
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

  // ---- Chart.js plugin: gekleurde dag-banden ----
  // Tekent subtiele achtergrondkleuren achter elk uur in de grafiek op basis van
  // het dagtype: NL feestdag, EU-feestdag (buurlanden vrij maar NL open),
  // NL+EU feestdag, of weekend. Dag-type met het sterkste effect per dag wint.
  //
  // Werking: itereer de timeline, groepeer aaneengesloten indices per kalenderdag,
  // check de feestdagen-Sets, teken een rechthoek over de chartArea-hoogte.
  // Tekst-label bovenaan de band alleen als de band breed genoeg is (>50px).
  const dayBandPlugin = {
    id: "svDayBand",
    beforeDatasetsDraw(chart, _args, opts) {
      const { ctx, chartArea } = chart;
      const timeline = opts.timeline;
      const holidays = opts.holidays;
      if (!timeline || timeline.length < 2 || !chartArea) return;

      const n = timeline.length;
      const step = (chartArea.right - chartArea.left) / n;

      // Groepeer indices per kalenderdag (eerste en laatste index per datum)
      const dayMap = Object.create(null);
      timeline.forEach((pt, i) => {
        const date = pt.time.slice(0, 10); // "YYYY-MM-DD"
        if (!dayMap[date]) dayMap[date] = { first: i, last: i };
        else dayMap[date].last = i;
      });

      ctx.save();

      Object.entries(dayMap).forEach(([date, { first, last }]) => {
        const isNL = holidays.nl.has(date);
        const isCB = holidays.crossborder.has(date);
        const dow  = new Date(date + "T12:00:00").getDay(); // 0=zo, 6=za
        const isWeekend = dow === 0 || dow === 6;

        let bg, label, labelColor;
        if (isNL && isCB) {
          bg = "rgba(255, 193, 7, 0.18)";
          label = "🗓 NL + EU feestdag";
          labelColor = "rgba(110, 70, 0, 0.82)";
        } else if (isNL) {
          bg = "rgba(255, 193, 7, 0.13)";
          label = "🗓 NL feestdag";
          labelColor = "rgba(110, 70, 0, 0.78)";
        } else if (isCB) {
          bg = "rgba(255, 140, 0, 0.13)";
          label = "🌍 EU-feestdag (NL open)";
          labelColor = "rgba(140, 70, 0, 0.80)";
        } else if (isWeekend) {
          bg = "rgba(100, 100, 180, 0.06)";
          label = null;
        } else {
          return; // gewone werkdag — niets tekenen
        }

        const x1 = chartArea.left + first * step;
        const x2 = chartArea.left + (last + 1) * step;
        const bandW = x2 - x1;

        // Achtergrondrechthoek
        ctx.fillStyle = bg;
        ctx.fillRect(x1, chartArea.top, bandW, chartArea.bottom - chartArea.top);

        // Tekst-label bovenaan (alleen als de band breed genoeg is)
        if (label && bandW > 50) {
          ctx.save();
          ctx.beginPath();
          ctx.rect(x1 + 2, chartArea.top, bandW - 4, chartArea.bottom - chartArea.top);
          ctx.clip();
          ctx.fillStyle = labelColor;
          ctx.font = "bold 9px system-ui, -apple-system, sans-serif";
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.fillText(label, (x1 + x2) / 2, chartArea.top + 5);
          ctx.restore();
        }
      });

      ctx.restore();
    },
  };

  function fmtChartLabel(iso) {
    // Voor de chart x-axis: korte labels. Voor "vandaag/morgen" alleen HH:MM,
    // voor verdere voorspelling-uren een weekdag-prefix om te onderscheiden.
    const d = new Date(iso);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const dayDiff = Math.floor((d - today) / 86400000);
    if (dayDiff <= 1) {
      return fmtTime(iso);
    }
    const wd = d.toLocaleDateString("nl-NL", { weekday: "short" });
    return `${wd} ${fmtTime(iso)}`;
  }

  function renderChart() {
    const canvas = document.getElementById("dayChart");
    if (!canvas || typeof Chart === "undefined") return;
    if (state.chart) { state.chart.destroy(); state.chart = null; }

    const dayPrices = state.dayPrices;
    const forecasts = state.forecasts;

    // Bouw één gecombineerde tijdlijn: dayPrices + forecasts. dayPrices komen
    // eerst (vandaag + morgen), forecasts daarna (overmorgen t/m +7d).
    const timeline = [
      ...dayPrices.map((p) => ({ kind: "actual", time: p.time, price: p.price })),
      ...forecasts.map((f) => ({ kind: "forecast", time: f.time, forecast: f })),
    ];
    const labels = timeline.map((t) => fmtChartLabel(t.time));

    // Dataset 1: actuele prijzen (solid line, gekleurde punten)
    const actualData = timeline.map((t) => t.kind === "actual" ? priceCents(t.price) : null);
    const actualColors = timeline.map((t, i) => {
      if (t.kind !== "actual") return "transparent";
      return i === state.nowIdx ? "#0f6cbd" : pointColor(t.price);
    });
    const actualRadii = timeline.map((t, i) => {
      if (t.kind !== "actual") return 0;
      return i === state.nowIdx ? 6 : 3;
    });

    // Datasets 2 & 3: voorspellingsband. We tekenen lower met fill naar upper
    // (Chart.js: fill: '+1' = vul tot volgend dataset). Beide linkers transparant.
    const forecastLower = timeline.map((t) => t.kind === "forecast" ? priceCents(t.forecast.lower) : null);
    const forecastUpper = timeline.map((t) => t.kind === "forecast" ? priceCents(t.forecast.upper) : null);

    // Dataset 4: voorspelde lijn (gestippeld)
    const forecastPredicted = timeline.map((t) => t.kind === "forecast" ? priceCents(t.forecast.predicted) : null);

    state.chart = new Chart(canvas, {
      type: "line",
      plugins: [dayBandPlugin],
      data: {
        labels,
        datasets: [
          {
            label: `ct/kWh (${modeLabel()})`,
            data: actualData,
            tension: 0.25,
            borderColor: "#2e75b6",
            borderWidth: 2,
            pointBackgroundColor: actualColors,
            pointBorderColor: actualColors,
            pointRadius: actualRadii,
            pointHoverRadius: (ctx) => (ctx.dataIndex === state.nowIdx ? 7 : 5),
            fill: { target: "origin", above: "rgba(46,117,182,0.08)" },
            spanGaps: false,
          },
          {
            label: "_forecast_lower",
            data: forecastLower,
            borderColor: "transparent",
            backgroundColor: "rgba(46,117,182,0.12)",
            pointRadius: 0,
            fill: "+1",
            tension: 0.25,
            spanGaps: false,
          },
          {
            label: "_forecast_upper",
            data: forecastUpper,
            borderColor: "transparent",
            pointRadius: 0,
            fill: false,
            tension: 0.25,
            spanGaps: false,
          },
          {
            label: "voorspelling",
            data: forecastPredicted,
            borderColor: "#0f6cbd",
            borderDash: [4, 4],
            borderWidth: 2,
            tension: 0.25,
            pointRadius: 2,
            pointBackgroundColor: "#0f6cbd",
            pointHoverRadius: 5,
            fill: false,
            spanGaps: false,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 250 },
        interaction: { mode: "index", intersect: false },
        scales: {
          x: {
            ticks: { autoSkip: true, maxTicksLimit: 14, color: "#7c8a99", font: { size: 11 } },
            grid: { color: "rgba(0,0,0,0.04)" },
          },
          y: {
            ticks: { color: "#7c8a99", font: { size: 11 }, callback: (v) => v + " ct" },
            grid: { color: "rgba(0,0,0,0.06)" },
          },
        },
        plugins: {
          svDayBand: { timeline, holidays: buildHolidayLookup() },
          legend: {
            display: true,
            labels: {
              filter: (item) => !item.text.startsWith("_"),
              boxWidth: 18, boxHeight: 2, font: { size: 11 },
            },
          },
          tooltip: {
            filter: (item) => !item.dataset.label || !item.dataset.label.startsWith("_"),
            callbacks: {
              title: (items) => fmtDateTime(timeline[items[0].dataIndex].time),
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
                // Forecast: toon voorspelling, band en factor-bijdrage
                const f = t.forecast;
                const lines = [
                  `Voorspeld: ${fmtNum(priceCents(f.predicted), 2)} ct/kWh`,
                  `Band: ${fmtNum(priceCents(f.lower), 2)} – ${fmtNum(priceCents(f.upper), 2)} ct/kWh`,
                  `Baseline: ${fmtNum(priceCents(f.baseline), 2)} ct/kWh; ${f.total_points >= 0 ? "+" : ""}${f.total_points} punten`,
                ];
                (f.factors || []).forEach((fact) => {
                  lines.push(`  ${fact.name}: ${fact.points >= 0 ? "+" : ""}${fact.points} (${fact.reason})`);
                });
                return lines;
              },
            },
          },
        },
      },
    });

    // Feestdag-kleurlegenda onder de grafiek (éénmalig aanmaken; blijft staan
    // bij volgende renderChart-aanroepen zoals leverancier- of modeswitch).
    if (!document.getElementById("chart-holiday-legend")) {
      const note = document.createElement("p");
      note.id = "chart-holiday-legend";
      note.setAttribute("aria-hidden", "true");
      note.style.cssText =
        "font-size:11px;color:#374151;margin:6px 0 2px;line-height:1.7;";
      note.innerHTML =
        '<span style="background:rgba(255,193,7,0.32);padding:1px 6px;border-radius:3px;margin-right:4px;">\u{1F5D3} NL feestdag</span>' +
        '<span style="background:rgba(255,140,0,0.30);padding:1px 6px;border-radius:3px;margin-right:4px;">\u{1F30D} EU-feestdag (NL open)</span>' +
        '<span style="background:rgba(255,193,7,0.32);outline:1px solid rgba(200,110,0,0.40);outline-offset:-1px;padding:1px 6px;border-radius:3px;margin-right:8px;">\u{1F5D3}+\u{1F30D} NL &amp; EU feestdag</span>' +
        'Op deze dagen valt de stroomprijs vaak extra laag door verminderde vraag in buurlanden.';
      canvas.insertAdjacentElement("afterend", note);
    }
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

    // Negatieve-prijs banner sluiten — onthoud event-ID zodat de banner weg blijft
    // tot er een nieuw event ontstaat (andere starttijd).
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

  // forecast.json mag falen (recent toegevoegd; oude deploys hebben hem niet) —
  // de site werkt dan zonder voorspellingslijn maar prijzen blijven wel zichtbaar.
  Promise.all([
    fetch("data/config.json", { cache: "no-store" }).then((r) => { if (!r.ok) throw new Error("config HTTP " + r.status); return r.json(); }),
    fetch("data/prices.json", { cache: "no-store" }).then((r) => { if (!r.ok) throw new Error("prices HTTP " + r.status); return r.json(); }),
    fetch("data/forecast.json", { cache: "no-store" }).then((r) => r.ok ? r.json() : null).catch(() => null),
  ])
    .then(([config, payload, forecastPayload]) => {
      state.config = config;
      state.payload = payload;
      state.forecastPayload = forecastPayload;
      state.prices = payload.prices || [];
      state.forecasts = (forecastPayload && forecastPayload.forecasts) || [];
      const now = new Date();
      state.dayPrices = filterTodayTomorrow(state.prices, now);
      state.nowIdx = findCurrentIndex(state.dayPrices, now);
      applyConfigDefaults();
      wireUI();
      renderAll();
    })
    .catch((err) => {
      console.error("Kon data niet laden", err);
      showError("Kon data niet laden — probeer pagina te verversen");
    });
})();
