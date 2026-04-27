// Stroomvoorspeller — frontend logica
// Laadt prijzen uit data/prices.json en tekent de grafiek + samenvatting.

(function () {
  "use strict";

  // ---- Configuratie ----
  // Drempels in EUR/MWh voor de "goedkoop / normaal / duur" kleurcode.
  // We kiezen iets ruime drempels die in 2025-2026 redelijk zijn.
  const THRESHOLDS = { cheap: 50, pricey: 110 };

  // Schatting van consumentenkosten bovenop EPEX (EUR per kWh, excl. btw)
  // Energiebelasting 2026 ~ €0.1316; opslag leverancier ~ €0.025 — beide schattingen.
  const CONSUMER_FIXED_PER_KWH = 0.1316 + 0.025;
  const VAT_FACTOR = 1.21;

  const MWH_TO_CT_PER_KWH = (eurMwh) => eurMwh / 10; // 100 EUR/MWh = 10 ct/kWh

  // Helper: EPEX EUR/MWh -> all-in ct/kWh inclusief belasting + btw
  function allInCents(eurMwh) {
    const epexEurPerKwh = eurMwh / 1000;
    const subtotal = epexEurPerKwh + CONSUMER_FIXED_PER_KWH;
    const inclBtw = subtotal * VAT_FACTOR;
    return inclBtw * 100; // naar cent
  }

  function classify(eurMwh) {
    if (eurMwh < THRESHOLDS.cheap) return "cheap";
    if (eurMwh > THRESHOLDS.pricey) return "pricey";
    return "normal";
  }

  function statusLabel(status) {
    if (status === "cheap")  return "goedkoop";
    if (status === "pricey") return "duur";
    return "normaal";
  }

  function fmtCents(eurMwh, digits = 1) {
    const ct = MWH_TO_CT_PER_KWH(eurMwh);
    return (Math.round(ct * Math.pow(10, digits)) / Math.pow(10, digits))
      .toLocaleString("nl-NL", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function fmtAllIn(eurMwh) {
    const ct = allInCents(eurMwh);
    return (Math.round(ct * 10) / 10)
      .toLocaleString("nl-NL", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  }

  function fmtTime(iso, opts) {
    const d = new Date(iso);
    return d.toLocaleTimeString("nl-NL", Object.assign({ hour: "2-digit", minute: "2-digit" }, opts || {}));
  }

  function fmtDateTime(iso) {
    const d = new Date(iso);
    const date = d.toLocaleDateString("nl-NL", { weekday: "short", day: "numeric", month: "short" });
    return `${date} ${fmtTime(iso)}`;
  }

  function setText(field, text) {
    document.querySelectorAll(`[data-field="${field}"]`).forEach((el) => {
      el.textContent = text;
    });
  }

  function isSameLocalDay(isoA, isoB) {
    const a = new Date(isoA);
    const b = new Date(isoB);
    return a.getFullYear() === b.getFullYear() &&
           a.getMonth() === b.getMonth() &&
           a.getDate() === b.getDate();
  }

  function findCurrentIndex(prices, now) {
    // Vind het uur waarvan de timestamp <= now en het volgende uur > now (of laatste uur).
    let idx = -1;
    for (let i = 0; i < prices.length; i++) {
      const t = new Date(prices[i].time).getTime();
      if (t <= now.getTime()) idx = i; else break;
    }
    return idx;
  }

  function findBestMoments(prices, fromIdx, count = 3, windowHours = 2) {
    // Vind de goedkoopste niet-overlappende vensters van `windowHours` uur,
    // beginnend vanaf fromIdx.
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
    const status = classify(eurMwh);
    if (status === "cheap")  return "#2f9e44";
    if (status === "pricey") return "#c92a2a";
    return "#d4a017";
  }

  function render(payload) {
    const prices = payload.prices || [];
    if (!prices.length) {
      setText("now-cents", "—");
      return;
    }
    const now = new Date();
    const nowIdx = findCurrentIndex(prices, now);
    const currentPrice = nowIdx >= 0 ? prices[nowIdx] : prices[0];

    // Now-card
    const status = classify(currentPrice.price);
    const card = document.querySelector(".now-card");
    if (card) card.dataset.status = status;
    setText("now-cents", fmtCents(currentPrice.price));
    setText("now-allin", fmtAllIn(currentPrice.price));
    setText("now-time", `Nu, ${fmtTime(currentPrice.time)}`);
    const statusEl = document.querySelector(".status-value");
    if (statusEl) statusEl.textContent = statusLabel(status);

    // Vandaag samenvatting
    const today = prices.filter((p) => isSameLocalDay(p.time, currentPrice.time));
    if (today.length) {
      const cheapest = today.reduce((a, b) => (a.price <= b.price ? a : b));
      const priciest = today.reduce((a, b) => (a.price >= b.price ? a : b));
      const avg = today.reduce((s, p) => s + p.price, 0) / today.length;
      setText("cheapest-today", `${fmtCents(cheapest.price)} ct · ${fmtTime(cheapest.time)}`);
      setText("priciest-today", `${fmtCents(priciest.price)} ct · ${fmtTime(priciest.time)}`);
      setText("avg-today", `${fmtCents(avg)} ct/kWh`);
    }

    // Best moments (vanaf nu)
    const fromIdx = nowIdx >= 0 ? nowIdx : 0;
    const moments = findBestMoments(prices, fromIdx, 3, 2);
    const list = document.querySelector('[data-field="best-moments"]');
    if (list) {
      list.innerHTML = "";
      if (!moments.length) {
        list.innerHTML = '<li class="moment-loading">Nog geen vensters beschikbaar.</li>';
      } else {
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
    }

    // Footer meta
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

    // Chart
    drawChart(prices, nowIdx);
  }

  function drawChart(prices, nowIdx) {
    const canvas = document.getElementById("dayChart");
    if (!canvas || typeof Chart === "undefined") return;

    const labels = prices.map((p) => fmtTime(p.time));
    const data = prices.map((p) => MWH_TO_CT_PER_KWH(p.price));
    const colors = prices.map((p, i) => i === nowIdx ? "#0f6cbd" : pointColor(p.price));
    const radii  = prices.map((_, i) => i === nowIdx ? 6 : 3);

    // Achtergrondkleur per "dag" (subtiele zonering vandaag/morgen)
    const firstDay = new Date(prices[0].time).getDate();
    const tomorrowStart = prices.findIndex((p) => new Date(p.time).getDate() !== firstDay);

    new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "ct/kWh (EPEX)",
          data,
          tension: 0.25,
          borderColor: "#2e75b6",
          borderWidth: 2,
          pointBackgroundColor: colors,
          pointBorderColor: colors,
          pointRadius: radii,
          pointHoverRadius: (ctx) => (ctx.dataIndex === nowIdx ? 7 : 5),
          fill: { target: "origin", above: "rgba(46,117,182,0.08)" },
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 350 },
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
              title: (items) => {
                const idx = items[0].dataIndex;
                return fmtDateTime(prices[idx].time);
              },
              label: (item) => {
                const idx = item.dataIndex;
                const eurMwh = prices[idx].price;
                return [
                  `EPEX: ${fmtCents(eurMwh, 2)} ct/kWh`,
                  `Incl. belasting & btw: ${fmtAllIn(eurMwh)} ct/kWh`,
                ];
              },
            },
          },
          annotation: tomorrowStart > 0 ? {
            annotations: {
              tomorrowDivider: {
                type: "line",
                xMin: tomorrowStart - 0.5,
                xMax: tomorrowStart - 0.5,
                borderColor: "rgba(15,108,189,0.3)",
                borderWidth: 1,
                borderDash: [4, 4],
              },
            },
          } : {},
        },
      },
    });
  }

  // ---- Boot ----
  fetch("data/prices.json", { cache: "no-store" })
    .then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(render)
    .catch((err) => {
      console.error("Kon prijzen niet laden", err);
      const card = document.querySelector(".now-card");
      if (card) card.dataset.status = "error";
      setText("now-cents", "—");
      setText("now-time", "Kon data niet laden");
    });
})();
