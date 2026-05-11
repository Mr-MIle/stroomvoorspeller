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
    tomorrowPrices:    [],   // {time, price} voor morgen
    todayPrices:       [],   // {time, price} voor vandaag (vergelijking)
    tomorrowForecasts: [],   // forecast-entries voor morgen (optioneel)
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

    const prices = state.tomorrowPrices;
    const labels  = prices.map(p => fmtTime(p.time));
    const values  = prices.map(p => priceCents(p.price));
    const classes = prices.map(p => classify(p.price));
    const colors  = classes.map(c => CLASS_COLOR[c] || "#d4a017");

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
          borderWidth: 1.5,
          borderRadius: 4,
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
                return `${labels[i]}–${endTime(prices[i].time)}`;
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
            ticks: { font: { size: 11 }, maxRotation: 0 },
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

  // ---- Render: waarom-sectie ----
  function renderWhy() {
    const prices = state.tomorrowPrices;
    const el = document.getElementById("morgen-why-content");
    if (!el) return;

    const avgEpex  = prices.reduce((s, p) => s + p.price, 0) / prices.length;
    const avgCt    = priceCents(avgEpex, "inclusive");
    const negHours = prices.filter(p => priceCents(p.price, "inclusive") < 0);
    const cheapHrs = prices.filter(p => { const c = classify(p.price); return c === "cheap" || c === "very_cheap"; });
    const expHrs   = prices.filter(p => { const c = classify(p.price); return c === "pricey" || c === "very_pricey"; });
    const peak     = prices.reduce((a, b) => a.price >= b.price ? a : b);
    const valley   = prices.reduce((a, b) => a.price <= b.price ? a : b);

    let html = `<p class="morgen-why-intro">Morgen is stroom gemiddeld
      <strong>${fmtNum(avgCt, 1)} ct/kWh</strong> all-in (incl. energiebelasting &amp; btw).</p>`;

    if (state.todayPrices.length) {
      const todayEpex = state.todayPrices.reduce((s, p) => s + p.price, 0) / state.todayPrices.length;
      const todayCt   = priceCents(todayEpex, "inclusive");
      const diff      = avgCt - todayCt;
      const pct       = Math.abs((diff / todayCt) * 100);
      if (Math.abs(diff) > 0.5) {
        html += `<p>Dat is <strong>${diff < 0 ? fmtNum(pct, 0) + "% goedkoper" : fmtNum(pct, 0) + "% duurder"}</strong>
          dan vandaag (vandaag gemiddeld ${fmtNum(todayCt, 1)} ct/kWh).</p>`;
      } else {
        html += `<p>Dat is vergelijkbaar met vandaag (vandaag gemiddeld ${fmtNum(todayCt, 1)} ct/kWh).</p>`;
      }
    }

    html += `<ul class="morgen-why-list">`;
    html += `<li>🏔 <strong>Duurste uur:</strong> ${fmtTime(peak.time)} · ${fmtNum(priceCents(peak.price, "inclusive"), 1)} ct/kWh</li>`;
    html += `<li>🌿 <strong>Goedkoopste uur:</strong> ${fmtTime(valley.time)} · ${fmtNum(priceCents(valley.price, "inclusive"), 1)} ct/kWh</li>`;

    if (negHours.length > 0) {
      html += `<li>⚡ <strong>${negHours.length} negatief${negHours.length > 1 ? "e" : ""} uur${negHours.length > 1 ? " — rond" : ""} ${fmtTime(negHours[0].time)}–${endTime(negHours[negHours.length - 1].time)}:</strong>
        stroom is dan gratis of je krijgt betaald voor afname.</li>`;
    } else {
      html += `<li>✅ Geen negatieve uren verwacht morgen.</li>`;
    }

    if (cheapHrs.length > 0) {
      html += `<li>✅ <strong>${cheapHrs.length} goedkope uren</strong> (onder 22 ct/kWh)</li>`;
    }
    if (expHrs.length > 0) {
      html += `<li>⚠ <strong>${expHrs.length} dure uren</strong> (boven 28 ct/kWh) — zware apparaten dan uitstellen.</li>`;
    }
    html += `</ul>`;

    // Forecast-factoren (alleen als beschikbaar voor morgen)
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
        .sort((a, b) => Math.abs(b.avg) - Math.abs(a.avg));

      if (factors.length) {
        const emoji = { zon: "☀️", wind: "💨", temperatuur: "🌡", gas: "🔥", dagtype: "📅", uurpatroon: "🕐" };
        html += `<h3>Factoren die de prijs morgen sturen</h3>
          <ul class="morgen-why-factors">`;
        factors.forEach(f => {
          const e    = emoji[f.name] || "•";
          const sign = f.avg > 0 ? "🔴 duurder" : "🟢 goedkoper";
          const pts  = f.avg > 0 ? `+${f.avg}` : String(f.avg);
          html += `<li>${e} <strong>${f.name[0].toUpperCase() + f.name.slice(1)}:</strong>
            ${sign} (${pts} punten) — ${f.reason || "—"}</li>`;
        });
        html += `</ul>
          <p class="morgen-why-note">Factoren zijn afkomstig uit het
            <a href="/over/voorspelling">transparante voorspellingsmodel</a> van Stroomvoorspeller.nl.
            Na publicatie van de officiële day-ahead prijs kan de werkelijke prijs hiervan afwijken.</p>`;
      }
    }

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

  // ---- Render: leveranciersnota in subtitle ----
  function renderSupplierNote() {
    const s = getSupplier();
    document.querySelectorAll("[data-supplier-note]").forEach(el => {
      el.textContent = s.name || "Algemeen gemiddelde";
    });
  }

  // ---- Render: alles ----
  function renderAll() {
    renderDateLabels();
    renderModeToggle();
    renderSupplierNote();
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

      // Forecast optioneel (geen harde fout als niet beschikbaar)
      try {
        const fc = await fetchJSON("/data/forecast.json");
        state.tomorrowForecasts = (fc.forecasts || []).filter(p => isSameLocalDay(p.time, state.tomorrowDate));
      } catch (_) { /* niet kritisch */ }

      if (!state.tomorrowPrices.length) { showNoDataState(); return; }

      initModeToggle();
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
