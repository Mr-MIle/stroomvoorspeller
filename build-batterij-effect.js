#!/usr/bin/env node
/*
 * build-batterij-effect.js
 * -------------------------------------------------------------------------
 * Rekent uit wat een NIEUWE thuisbatterij doet met je afname en teruglevering,
 * en hoeveel lager je stroomrekening wordt door goedkopere uurprijzen.
 * Schrijft public/data/batterij-effect.json; /aanbieders gebruikt dat als
 * iemand aangeeft dat de batterij er nog niet het hele jaar was.
 *
 * Rekenmodel: public/dyn-model.js (hetzelfde als /dynamisch-berekenen en de
 * batterijcalculator), op de uurprijzen van de laatste twaalf volledige
 * maanden uit public/data/archief/. Huishouden 2.900 kWh, gemiddelde opslag en
 * terugleverkosten uit config.json (id "average").
 *
 * Draait in update-prices.yml; de uitkomst verandert alleen als er een maand
 * bij komt. Ontbreekt het archief (zoals lokaal in OneDrive), dan laat het
 * script het bestaande JSON-bestand staan.
 * -------------------------------------------------------------------------
 */
"use strict";

const fs = require("fs");
const path = require("path");
const M = require("./public/dyn-model.js");

const ARCHIEF = path.join(__dirname, "public", "data", "archief");
const CONFIG = path.join(__dirname, "public", "data", "config.json");
const UIT = path.join(__dirname, "public", "data", "batterij-effect.json");
const VERBRUIK = 2900;
const PANELEN = [0, 6, 10, 14, 18];
const GROOTTES = [5, 10, 15, 20];

function pad(n) { return String(n).padStart(2, "0"); }

function main() {
  const maanden = M.lastTwelveMonths(new Date());
  const prijzen = {};
  for (const { y, m } of maanden) {
    const f = path.join(ARCHIEF, y + "-" + pad(m + 1) + ".json");
    if (!fs.existsSync(f)) {
      console.warn("[batterij-effect] archief " + y + "-" + pad(m + 1) + " ontbreekt; bestaande " + path.basename(UIT) + " blijft staan.");
      return;
    }
    for (const p of JSON.parse(fs.readFileSync(f, "utf8")).prices || []) {
      const dag = p.time.slice(0, 10), uur = parseInt(p.time.slice(11, 13), 10);
      (prijzen[dag] = prijzen[dag] || new Array(24).fill(null))[uur] = p.price;
    }
  }
  function epexFor(y, m, d) {
    const a = (prijzen[y + "-" + pad(m + 1) + "-" + pad(d)] || new Array(24).fill(null)).slice();
    let vorige = null;
    for (let h = 0; h < 24; h++) { if (a[h] == null) a[h] = vorige; else vorige = a[h]; }
    for (let h = 23; h >= 0; h--) { if (a[h] == null) a[h] = h < 23 ? a[h + 1] : 0; }
    return a;
  }

  const gem = (JSON.parse(fs.readFileSync(CONFIG, "utf8")).suppliers || []).find((s) => s.id === "average") || {};
  const markup = gem.markup_per_kwh || 0.017;
  const tlv = -((gem.teruglevering && gem.teruglevering.opslag_per_kwh) || -0.0133);
  const basis = { months: maanden, epexFor, contract: "dyn", markup, tlv, verbruik: VERBRUIK };
  const jaar = (extra) => M.runYear(Object.assign({}, basis, extra));

  const scenarios = PANELEN.map((panelen) => {
    const nul = { 2026: jaar({ panelen, batKwh: 0, saldering: true }), 2027: jaar({ panelen, batKwh: 0, saldering: false }) };
    const groottes = {};
    for (const kwh of GROOTTES) {
      const b26 = jaar({ panelen, batKwh: kwh, saldering: true });
      const b27 = jaar({ panelen, batKwh: kwh, saldering: false });
      groottes[kwh] = {
        afname: Math.round(b27.impKwh - nul[2027].impKwh),
        terug: Math.round(b27.expKwh - nul[2027].expKwh),
        besparing: { 2026: Math.round(nul[2026].jaarkosten - b26.jaarkosten), 2027: Math.round(nul[2027].jaarkosten - b27.jaarkosten) },
        prijs_ct: Math.round(b27.avgImp * 1000) / 10
      };
    }
    return { panelen, afname: Math.round(nul[2027].impKwh), terug: Math.round(nul[2027].expKwh),
             prijs_ct: Math.round(nul[2027].avgImp * 1000) / 10, groottes };
  });

  const eerste = maanden[0], laatste = maanden[maanden.length - 1];
  const uit = {
    toelichting: "Effect van een nieuwe thuisbatterij, berekend met public/dyn-model.js. afname/terug = verschil in kWh per jaar; besparing = lagere stroomrekening in euro per jaar (stroom, energiebelasting en btw, gemiddelde aanbieder), apart voor 2026 (saldering) en 2027. prijs_ct = gemiddelde prijs per afgenomen kWh incl. belasting. Gegenereerd door build-batterij-effect.js.",
    periode: { van: eerste.y + "-" + pad(eerste.m + 1), tot: laatste.y + "-" + pad(laatste.m + 1) },
    verbruik_kwh: VERBRUIK,
    batterij: "bruikbaar 90% van de capaciteit, vermogen 0,5C, 95% rendement laden en ontladen, laadt ook van het net als dat loont",
    scenarios
  };
  const tekst = JSON.stringify(uit, null, 2) + "\n";
  if (fs.existsSync(UIT) && fs.readFileSync(UIT, "utf8") === tekst) { console.log("[batterij-effect] ongewijzigd"); return; }
  fs.writeFileSync(UIT, tekst, "utf8");
  console.log("[batterij-effect] bijgewerkt: " + uit.periode.van + " t/m " + uit.periode.tot);
}

main();
