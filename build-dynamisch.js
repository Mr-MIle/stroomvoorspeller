#!/usr/bin/env node
/*
 * build-dynamisch.js - bakt de standaarduitkomst statisch in /dynamisch-berekenen.
 *
 * De pagina rekent alles client-side, maar zoekmachines en AI-crawlers voeren geen
 * JavaScript uit. Dit script draait hetzelfde model (public/dyn-model.js) over het
 * prijsarchief en schrijft de uitkomst tussen de BUILD-markers in de HTML.
 *
 * Draaien vanuit de repo-root:  node build-dynamisch.js
 * Het script is idempotent: twee keer draaien geeft hetzelfde bestand.
 *
 * LET OP: de opmaak hieronder moet gelijk blijven aan render() in de pagina zelf.
 * Verander je de tabel daar, pas hem hier dan ook aan.
 */
"use strict";
const fs = require("fs");
const path = require("path");
const M = require("./public/dyn-model.js");

const PAGE = path.join(__dirname, "public", "dynamisch-berekenen.html");
const ARCH = path.join(__dirname, "public", "data", "archief");
const MNAMES = ["januari","februari","maart","april","mei","juni","juli","augustus","september","oktober","november","december"];

// ---------- prijzen ----------
const REAL = {};
const seen = {};
function loadMonth(y, m) {
  const key = y + "-" + String(m + 1).padStart(2, "0");
  if (seen[key]) return true;
  const f = path.join(ARCH, key + ".json");
  if (!fs.existsSync(f)) return false;
  const j = JSON.parse(fs.readFileSync(f, "utf8"));
  for (const p of (j.prices || [])) {
    const dk = p.time.slice(0, 10).split("-").map(Number);
    const k = dk[0] + "-" + (dk[1] - 1) + "-" + dk[2];
    const h = parseInt(p.time.slice(11, 13), 10);
    if (!REAL[k]) REAL[k] = new Array(24).fill(null);
    REAL[k][h] = p.price;
  }
  seen[key] = true;
  return true;
}
function epexFor(y, m, d) {
  const a = REAL[y + "-" + m + "-" + d];
  if (!a) return new Array(24).fill(90);
  for (let h = 0; h < 24; h++) if (a[h] == null) a[h] = h > 0 ? a[h - 1] : (a[h + 1] == null ? 0 : a[h + 1]);
  return a;
}

// ---------- opmaak ----------
// In de HTML schrijven we het euroteken als entiteit, zodat het bestand puur ASCII blijft.
const EURO = "&euro;";
function nl(n) { return n.toLocaleString("nl-NL"); }
function eur(x) { return (x < 0 ? "-" : "") + EURO + nl(Math.round(Math.abs(x))); }
function ct(x) { return x.toLocaleString("nl-NL", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + " ct"; }
function logEur(x) { return "EUR " + nl(Math.round(x)); }

// ---------- rekenen ----------
const months = M.lastTwelveMonths(new Date());
let missing = 0;
for (const o of months) if (!loadMonth(o.y, o.m)) missing++;
if (missing) {
  console.error("Ontbrekende maanden in het archief: " + missing + ". Niets weggeschreven.");
  process.exit(1);
}

let uren = 0, som = 0, neg = 0;
for (const o of months) {
  for (let d = 1; d <= M.daysInMonth(o.y, o.m); d++) {
    for (const p of epexFor(o.y, o.m, d)) { som += p; uren++; if (p < 0) neg++; }
  }
}
const gemMarkt = som / uren;

// de standaardinvoer van de pagina
const base = {
  months, epexFor,
  verbruik: 2900, panelen: 0, evKwh: 0, hpKwh: 0,
  evMode: "slim", profiel: "avond", batKwh: 10,
  vastPrijs: 0.30, vastTerug: 0.05, markup: 0.0178, tlv: 0.020
};
const run = (extra) => M.runYear(Object.assign({}, base, extra));
const la_vast = run({ contract: "vast", saldering: false, batKwh: 0 });
const la_dyn = run({ contract: "dyn", saldering: false, batKwh: 0 });
const bat_vast = run({ contract: "vast", saldering: false, batKwh: base.batKwh });
const bat_dyn = run({ contract: "dyn", saldering: false, batKwh: base.batKwh });

const verschil = la_vast.jaarkosten - la_dyn.jaarkosten;
const kop = verschil >= 0
  ? "Met een dynamisch contract was je het afgelopen jaar <b>" + eur(verschil) + " per jaar goedkoper</b> uit. Je gemiddelde inkoopprijs komt dan op " + ct(la_dyn.avgImp * 100) + " per kWh, tegen " + ct(base.vastPrijs * 100) + " vast."
  : "Met een dynamisch contract was je het afgelopen jaar <b>" + eur(-verschil) + " per jaar duurder</b> uit. Je gemiddelde inkoopprijs komt dan op " + ct(la_dyn.avgImp * 100) + " per kWh, tegen " + ct(base.vastPrijs * 100) + " vast.";

const rows = [
  ["Zoals je nu stookt", la_vast, la_dyn],
  ["Met een thuisbatterij van " + base.batKwh + " kWh", bat_vast, bat_dyn]
];
let tbody = "";
for (const r of rows) {
  const a = r[1].jaarkosten, b = r[2].jaarkosten, win = b < a;
  tbody += '<tr' + (win ? ' class="win"' : "") + "><td>" + r[0] + "</td>" +
    '<td class="num' + (win ? "" : " best") + '">' + eur(a) + "</td>" +
    '<td class="num' + (win ? " best" : "") + '">' + eur(b) + "</td></tr>";
}
const tabel = '<table class="res" id="tabel"><thead><tr><th>Stroomkosten per jaar</th>' +
  '<th class="num">Vast contract</th><th class="num">Dynamisch</th></tr></thead><tbody>' +
  tbody + "</tbody></table>";

// crisisjaar oktober 2021 t/m september 2022, alleen voor het marktgemiddelde in de tekst
const crisis = [];
{ let y = 2021, m = 9; for (let i = 0; i < 12; i++) { crisis.push({ y, m }); m++; if (m > 11) { m = 0; y++; } } }
let cSom = 0, cN = 0, cOk = true;
for (const o of crisis) if (!loadMonth(o.y, o.m)) cOk = false;
if (cOk) {
  for (const o of crisis) {
    for (let d = 1; d <= M.daysInMonth(o.y, o.m); d++) {
      for (const p of epexFor(o.y, o.m, d)) { cSom += p; cN++; }
    }
  }
}

const eerste = months[0], laatst = months[months.length - 1];
const periode = MNAMES[eerste.m] + " " + eerste.y + " tot en met " + MNAMES[laatst.m] + " " + laatst.y;

// ---------- wegschrijven ----------
let html = fs.readFileSync(PAGE, "utf8");
const before = html;
function vul(naam, waarde) {
  const re = new RegExp("(<!-- BUILD:" + naam + ":START -->)[\\s\\S]*?(<!-- BUILD:" + naam + ":END -->)");
  if (!re.test(html)) { console.error("Marker " + naam + " niet gevonden."); process.exit(1); }
  html = html.replace(re, "$1" + waarde + "$2");
}
vul("KOP", kop);
vul("TABEL", tabel);
vul("MARKT", nl(Math.round(gemMarkt)));
if (cOk) vul("CRISIS", nl(Math.round(cSom / cN)));
vul("UREN", nl(uren));
vul("NEG", nl(neg));
vul("PERIODE", periode);

// dateModified bijwerken als die er staat
html = html.replace(/("dateModified":")[^"]*(")/, "$1" + new Date().toISOString().slice(0, 10) + "$2");

fs.writeFileSync(PAGE, html);
console.log("dynamisch-berekenen.html bijgewerkt" + (html === before ? " (geen wijziging)" : ""));
console.log("  periode        " + periode);
console.log("  uurprijzen     " + uren + ", waarvan " + neg + " negatief");
console.log("  markt          " + gemMarkt.toFixed(2) + " EUR/MWh" + (cOk ? ", crisisjaar " + (cSom / cN).toFixed(2) : ""));
console.log("  vast           " + logEur(la_vast.jaarkosten) + "  dynamisch " + logEur(la_dyn.jaarkosten));
console.log("  met batterij   " + logEur(bat_vast.jaarkosten) + "  dynamisch " + logEur(bat_dyn.jaarkosten));
