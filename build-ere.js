#!/usr/bin/env node
/*
 * build-ere.js
 * -------------------------------------------------------------------------
 * Genereert de statische inhoud van de ERE-vergelijker uit
 * public/data/ere.json en plakt die in public/ere-vergelijken.html:
 *   - de marktreeks (prijs per ERE per kwartaal) tussen de BUILD:MARKT-markers
 *   - de uitbetaaltabel tussen de BUILD:BETAALD-markers
 *   - de inboeker-regels tussen de BUILD:ERE-markers
 *   - de standregel (ERE-prijs + datum) tussen de BUILD:STAND-markers
 *   - het korte antwoord bovenaan tussen de BUILD:KORT-markers; het script
 *     waarschuwt als nummer 1 of de laagste commissie niet meer past bij de
 *     vaste zinnen in ere.json -> kort
 *
 * Waarom: de pagina rendert normaal met JavaScript (sorteren + filteren).
 * Zoekmachines en bezoekers zonder JS zien dan een lege pagina. Dit script
 * bakt dezelfde inhoud één keer statisch in, in de standaardvolgorde
 * (opbrengst per jaar, hoog -> laag) bij het standaardverbruik uit
 * aannames.kwh_per_jaar. De JS rendert er bij het laden identiek overheen.
 *
 * LET OP: de opmaak hieronder moet gelijk blijven aan rijHtml() en
 * renderVast(), standTekst() en kortHtml() in ere-vergelijken.html. Wijzig je daar iets, wijzig het hier
 * ook.
 *
 * Gebruik:  node build-ere.js
 * Draai dit telkens nadat je public/data/ere.json hebt aangepast, vóór je
 * pusht.
 * -------------------------------------------------------------------------
 */
"use strict";

const fs = require("fs");
const path = require("path");

const DATA_PATH = path.join(__dirname, "public", "data", "ere.json");
const HTML_PATH = path.join(__dirname, "public", "ere-vergelijken.html");

const BLOKKEN = [
  ["MARKT", "<!-- BUILD:MARKT:START — automatisch gegenereerd door build-ere.js; niet handmatig bewerken -->", "<!-- BUILD:MARKT:END -->"],
  ["BETAALD", "<!-- BUILD:BETAALD:START — automatisch gegenereerd door build-ere.js; niet handmatig bewerken -->", "<!-- BUILD:BETAALD:END -->"],
  ["ERE", "<!-- BUILD:ERE:START — automatisch gegenereerd door build-ere.js; niet handmatig bewerken -->", "<!-- BUILD:ERE:END -->"],
  ["STAND", "<!-- BUILD:STAND:START — automatisch gegenereerd door build-ere.js; niet handmatig bewerken -->", "<!-- BUILD:STAND:END -->"],
  ["KORT", "<!-- BUILD:KORT:START — automatisch gegenereerd door build-ere.js; niet handmatig bewerken -->", "<!-- BUILD:KORT:END -->"]
];

// ── opmaak-helpers (identiek aan de browser-render) ────────────────────────
function esc(t) {
  return String(t == null ? "" : t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function ct(v) { return (v * 100).toFixed(1).replace(".", ",") + " ct"; }
function eur0(v) { return "€ " + String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, "."); }
function pct(v) { return Math.round(v * 100) + "%"; }
function komma(v, n) { return v.toFixed(n).replace(".", ","); }

function marktPrijs(d) {
  const r = d.markt.reeks;
  return r.length ? r[r.length - 1].ere_prijs : d.markt.prijs_nu;
}
function bruto(d, prijs) { return prijs * d.markt.ere_per_kwh; }
function netto(d, inb) {
  if (inb.commissie == null) return null;
  let n = bruto(d, marktPrijs(d)) * (1 - inb.commissie);
  if (inb.garantie_per_kwh != null && inb.garantie_per_kwh > n) n = inb.garantie_per_kwh;
  return n;
}
function perJaar(d, inb, v) {
  const n = netto(d, inb);
  return n == null ? null : n * v - (inb.vaste_kosten || 0);
}

const MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"];
function datumNL(iso) {
  const p = String(iso).split("-");
  return p.length === 3 ? parseInt(p[2], 10) + " " + MAANDEN[parseInt(p[1], 10) - 1] + " " + p[0] : String(iso);
}
function getal(v) { return String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, "."); }
function namen(l) { return l.length < 2 ? l.join("") : l.slice(0, -1).join(", ") + " en " + l[l.length - 1]; }
function subRegel(inb) { return (inb.commissie == null ? "servicekosten niet openbaar" : inb.commissie_tekst) + " · " + inb.uitbetaling; }

function standTekst(d) {
  const r = d.markt.reeks[d.markt.reeks.length - 1];
  return "Gerekend met € " + komma(marktPrijs(d), 4) + " per ERE (" + esc(r.periode.replace("-", " ")) +
    (r.verwacht ? ", verwacht" : ", verkocht") + "). Bijgewerkt op " + esc(datumNL(d.bijgewerkt)) + ".";
}

function kortStand(d, v) {
  const met = d.inboekers.filter((i) => perJaar(d, i, v) != null)
    .sort((a, b) => perJaar(d, b, v) - perJaar(d, a, v));
  const metPct = d.inboekers.filter((i) => i.commissie != null);
  const laag = metPct.reduce((m, i) => (i.commissie < m.commissie ? i : m), metPct[0]);
  const betaald = d.inboekers.filter((i) => i.uitbetaald && i.uitbetaald.length);
  const bedragen = [].concat.apply([], betaald.map((i) => i.uitbetaald.map((u) => u.netto_per_kwh)));
  return { met: met, top: met[0], laatste: met[met.length - 1], laag: laag, betaald: betaald,
    min: Math.min.apply(null, bedragen), max: Math.max.apply(null, bedragen) };
}

function kortHtml(d, v) {
  const k = kortStand(d, v), vast = d.kort || {};
  const noot = (sleutel, inb) => (vast[sleutel] && vast[sleutel].id === inb.id ? " " + esc(vast[sleutel].noot) : "");
  let h = "<p><strong>Kort antwoord</strong> bij " + getal(v) + " kWh per jaar:</p><ul>";
  h += "<li><strong>Hoogste bedrag:</strong> " + esc(k.top.naam) + ", " + eur0(perJaar(d, k.top, v)) + "." + noot("hoogste", k.top) + "</li>";
  h += "<li><strong>Laagste servicekosten:</strong> " + esc(k.laag.naam) + ", " + Math.round(k.laag.commissie * 100) + " procent." + noot("laagste_commissie", k.laag) + "</li>";
  if (k.betaald.length) {
    h += "<li><strong>Al uitbetaald:</strong> " + esc(namen(k.betaald.map((i) => i.naam))) + ", netto " +
      (k.min === k.max ? komma(k.min * 100, 1) : komma(k.min * 100, 1) + " tot " + komma(k.max * 100, 1)) + " cent per kWh.</li>";
  }
  h += "</ul><p>Tussen nummer 1 en nummer " + k.met.length + " zit <strong>" + eur0(perJaar(d, k.top, v) - perJaar(d, k.laatste, v)) +
    "</strong> per jaar. Of je laadpaal meedoet, maakt meer uit. <a href=\"#ere-checks\">3 dingen om eerst te checken ↓</a></p>";
  return h;
}

function badges(inb) {
  let b = "";
  if (inb.garantie_per_kwh != null) b += (inb.garantie_label ? '<span class="ere-badge badge-aangekondigd">' + esc(inb.garantie_label) + " " : '<span class="ere-badge badge-garantie">bodem ') + ct(inb.garantie_per_kwh) + "</span>";
  if (inb.uitbetaald && inb.uitbetaald.length) b += '<span class="ere-badge badge-betaald">uitbetaald</span>';
  if (inb.eis_klant) b += '<span class="ere-badge badge-klant">alleen klanten</span>';
  if (inb.disclosure) b += '<span class="ere-badge badge-mij">ik ben hier klant</span>';
  return b ? '<span class="ere-badges">' + b + "</span>" : "";
}

function rijHtml(d, inb, v) {
  const n = netto(d, inb), j = perJaar(d, inb, v);
  let h = '      <div class="ere-rij" data-id="' + esc(inb.id) + '">\n';
  h += '        <button class="ere-open" type="button" aria-expanded="false" aria-controls="ere-det-' + esc(inb.id) + '">';
  h += '<span class="ere-naam">' + esc(inb.naam) + badges(inb) + '<span class="ere-sub">' + esc(subRegel(inb)) + "</span></span>";
  h += '<span class="ere-cel"><span class="ere-cel-label">netto/kWh</span>' + (n == null ? "–" : ct(n)) + "</span>";
  h += '<span class="ere-cel ere-jaar"><span class="ere-cel-label">per jaar</span><strong>' + (j == null ? "–" : eur0(j)) + "</strong></span>";
  h += '<span class="ere-caret" aria-hidden="true">▾</span></button>\n';
  h += '        <div class="ere-details" id="ere-det-' + esc(inb.id) + '" hidden>';
  h += '<p class="ere-label">Servicekosten</p><p>' + esc(inb.commissie_tekst) + (inb.commissie_detail ? " " + esc(inb.commissie_detail) : "") + "</p>";
  h += '<p class="ere-label">Uitbetaling</p><p>' + esc(inb.uitbetaling.charAt(0).toUpperCase() + inb.uitbetaling.slice(1)) + "." + (inb.uitbetaling_detail ? " " + esc(inb.uitbetaling_detail) : "") + "</p>";
  h += '<p class="ere-label">Voorwaarden</p><p>' + esc(inb.contract) + (inb.eis_klant ? " " + esc(inb.eis_klant) : "") + "</p>";
  h += '<p class="ere-label">Laadpaal</p><p>' + esc(inb.laadpalen) + "</p>";
  if (inb.uitbetaald && inb.uitbetaald.length) {
    const lijst = inb.uitbetaald.map((u) => esc(u.periode) + ": " + ct(u.netto_per_kwh)).join(" &middot; ");
    h += '<p class="ere-label">Werkelijk uitbetaald</p><p>' + lijst + "</p>";
  }
  if (inb.sterk) h += '<p class="ere-label">Sterk punt</p><p>' + esc(inb.sterk) + "</p>";
  if (inb.let_op) h += '<p class="ere-label">Let op</p><p>' + esc(inb.let_op) + "</p>";
  if (inb.disclosure) h += '<p class="ere-disclosure">' + esc(inb.disclosure) + "</p>";
  h += '<p class="ere-bronregel">Gecontroleerd op ' + esc(inb.gecontroleerd) + ' &middot; <a href="' + esc(inb.bron) + '" target="_blank" rel="noopener">bron ↗</a> &middot; <a href="' + esc(inb.url) + '" target="_blank" rel="noopener">naar de website ↗</a></p>';
  h += "</div>\n      </div>";
  return h;
}

function marktHtml(d) {
  return d.markt.reeks.map((r) =>
    '        <tr' + (r.verwacht ? ' class="ere-verwacht"' : "") + "><td>" + esc(r.periode) + '</td><td class="num">€ ' +
    komma(r.ere_prijs, 4) + '</td><td class="num">' + ct(r.ere_prijs * d.markt.ere_per_kwh) + "</td><td>" +
    esc(r.toelichting) + "</td></tr>"
  ).join("\n");
}

function betaaldHtml(d) {
  return d.inboekers.filter((i) => i.uitbetaald && i.uitbetaald.length).map((i) => {
    const vind = (p) => {
      const u = i.uitbetaald.filter((x) => x.periode === p)[0];
      return u ? ct(u.netto_per_kwh) : "–";
    };
    return '        <tr><td>' + esc(i.naam) + '</td><td class="num">' + vind("2026-Q1") + '</td><td class="num">' +
      vind("2026-Q2") + '</td><td><a href="' + esc(i.uitbetaald[0].bron) + '" target="_blank" rel="noopener">melding ↗</a></td></tr>';
  }).join("\n");
}

// ── uitvoeren ──────────────────────────────────────────────────────────────
const data = JSON.parse(fs.readFileSync(DATA_PATH, "utf8"));
const v = data.aannames.kwh_per_jaar;

const lijst = data.inboekers.slice().sort((a, b) => {
  const x = perJaar(data, a, v), y = perJaar(data, b, v);
  if (x == null) return 1;
  if (y == null) return -1;
  return y - x;
});

const inhoud = {
  MARKT: marktHtml(data),
  BETAALD: betaaldHtml(data),
  ERE: lijst.map((i) => rijHtml(data, i, v)).join("\n"),
  STAND: "        " + standTekst(data),
  KORT: "        " + kortHtml(data, v)
};

let html = fs.readFileSync(HTML_PATH, "utf8");
for (const [naam, start, eind] of BLOKKEN) {
  const a = html.indexOf(start);
  const b = html.indexOf(eind);
  if (a === -1 || b === -1 || b < a) {
    console.error("Markers voor " + naam + " niet gevonden in " + HTML_PATH);
    process.exit(1);
  }
  html = html.slice(0, a + start.length) + "\n" + inhoud[naam] + "\n        " + html.slice(b);
}
fs.writeFileSync(HTML_PATH, html, "utf8");

console.log("ere-vergelijken.html bijgewerkt: " + lijst.length + " inboekers, " +
  data.markt.reeks.length + " kwartalen, marktprijs € " + komma(marktPrijs(data), 4) + " per ERE.");
const ks = kortStand(data, v), vk = data.kort || {};
console.log("Kort antwoord: hoogste " + ks.top.naam + ", laagste commissie " + ks.laag.naam + ", verschil " +
  eur0(perJaar(data, ks.top, v) - perJaar(data, ks.laatste, v)) + " per jaar.");
if (!vk.hoogste || vk.hoogste.id !== ks.top.id)
  console.warn("LET OP: nummer 1 is nu " + ks.top.naam + ", niet " + (vk.hoogste ? vk.hoogste.id : "?") +
    ". De noot bij 'Hoogste bedrag' valt weg; zet kort.hoogste in ere.json opnieuw en lees het korte antwoord na.");
if (!vk.laagste_commissie || vk.laagste_commissie.id !== ks.laag.id)
  console.warn("LET OP: laagste commissie is nu " + ks.laag.naam + ", niet " + (vk.laagste_commissie ? vk.laagste_commissie.id : "?") +
    ". De noot bij 'Laagste servicekosten' valt weg; zet kort.laagste_commissie in ere.json opnieuw en lees het korte antwoord na.");
lijst.forEach((i) => {
  const j = perJaar(data, i, v);
  console.log("  " + i.naam.padEnd(28) + (j == null ? "onbekend" : eur0(j) + " / jaar"));
});
