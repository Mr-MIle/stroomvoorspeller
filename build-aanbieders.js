#!/usr/bin/env node
/*
 * build-aanbieders.js
 * -------------------------------------------------------------------------
 * Genereert de statische aanbieder-inhoud uit public/data/config.json en
 * plakt die in public/aanbieders.html:
 *   - de uitgelichte top 3 tussen de BUILD:TOP-markers
 *   - de compacte regels tussen de BUILD:CARDS-markers
 *   - de samenvatting bovenaan (goedkoopste + checkdatum) tussen BUILD:SAMENVATTING
 *   - de FAQ met FAQPage-schema tussen de BUILD:FAQ-markers
 *
 * Waarom: de pagina rendert normaal met JavaScript (sorteren + filteren).
 * Zoekmachines en bezoekers zonder JS zien dan een lege lijst. Dit script
 * bakt dezelfde inhoud één keer statisch in, in de standaardvolgorde
 * (geschatte jaarkosten, laag -> hoog) en bij het standaardverbruik. De JS
 * rendert er bij het laden identiek overheen, dus geen dubbele inhoud of
 * sprong.
 *
 * LET OP: de opmaak hieronder moet gelijk blijven aan rowHtml()/topKaartHtml()
 * in public/aanbieders.html. Wijzig je daar iets, wijzig het hier ook.
 *
 * Gebruik:  node build-aanbieders.js
 * Draai dit telkens nadat je config.json hebt aangepast, vóór je pusht.
 * -------------------------------------------------------------------------
 */
"use strict";

const fs = require("fs");
const path = require("path");

const CONFIG_PATH = path.join(__dirname, "public", "data", "config.json");
const HTML_PATH = path.join(__dirname, "public", "aanbieders.html");
const CARDS_START = "<!-- BUILD:CARDS:START — automatisch gegenereerd door build-aanbieders.js; niet handmatig bewerken -->";
const CARDS_END = "<!-- BUILD:CARDS:END -->";
const TOP_START = "<!-- BUILD:TOP:START — automatisch gegenereerd door build-aanbieders.js; niet handmatig bewerken -->";
const TOP_END = "<!-- BUILD:TOP:END -->";
const SAM_START = "<!-- BUILD:SAMENVATTING:START — automatisch gegenereerd door build-aanbieders.js; niet handmatig bewerken -->";
const SAM_END = "<!-- BUILD:SAMENVATTING:END -->";
const FAQ_START = "<!-- BUILD:FAQ:START — automatisch gegenereerd door build-aanbieders.js; niet handmatig bewerken -->";
const FAQ_END = "<!-- BUILD:FAQ:END -->";
const MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"];

// ── opmaak-helpers (identiek aan de browser-render in aanbieders.html) ──────
function fmtCt(m) { return (m * 100).toFixed(2).replace(".", ","); }
function fmtEur2(v) { return v.toFixed(2).replace(".", ","); }
function fmtEur0(v) { return String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, "."); }
function esc(t) {
  return String(t == null ? "" : t)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
// c = { v: verbruik, t: teruglevering in kWh, jaar: 2026 of 2027 }. Identiek aan aanbieders.html.
function jaarkosten(s, c) {
  let k = s.markup_per_kwh * c.v + s.fixed_per_month * 12;
  const tl = s.teruglevering;
  if (c.t > 0 && tl && tl.opslag_per_kwh) {
    const kwh = (tl.methode === "jaaroverschot" && c.jaar === 2026) ? Math.max(0, c.t - c.v) : c.t;
    k -= tl.opslag_per_kwh * kwh;
  }
  return k;
}
function fmtJk(v) { return v < 0 ? "+ € " + fmtEur0(-v) + " terug" : "± € " + fmtEur0(v); }
function scoreClass(score) {
  if (score == null) return "score-matig";
  if (score >= 4) return "score-goed";
  if (score >= 3) return "score-matig";
  return "score-slecht";
}
function scoreTxt(score) {
  return score == null ? "n.v.t." : score.toFixed(1).replace(".", ",") + " ★";
}
function badgesHtml(s) {
  let b = "";
  if (s.groen) b += '<span class="aanb-badge badge-groen">groen</span>';
  if (s.smart) b += '<span class="aanb-badge badge-smart">slim laden</span>';
  if (s.no_feedin_cost) b += '<span class="aanb-badge badge-tlv">geen tlv-kosten</span>';
  if (s.gas === false) b += '<span class="aanb-badge badge-let">geen gas</span>';
  return b ? '<span class="aanb-badges">' + b + "</span>" : "";
}
function eersteZin(t) {
  const m = String(t || "").match(/^[^.]{20,180}\./);
  return m ? m[0] : String(t || "").slice(0, 150);
}

// Teruglevering: kale beursprijs + opslag van de aanbieder, zonder energiebelasting
// en zonder btw. De zonnebonussen staan er los bij, want die gelden alleen voor
// zonnestroom en niet voor stroom uit een thuisbatterij. Bron: config.json.
function terugleverTekst(s) {
  var t = s.teruglevering;
  if (!t) return "";
  var o = t.opslag_per_kwh || 0;
  var ct = Math.abs(o * 100).toFixed(2).replace(".", ",");
  var r;
  if (!o) r = "Je krijgt de kale beursprijs, zonder opslag en zonder inhouding.";
  else if (o > 0) r = "Je krijgt de kale beursprijs plus " + ct + " ct per kWh.";
  else r = "Je krijgt de kale beursprijs min " + ct + " ct per kWh.";
  if (t.methode === "jaaroverschot") r += " Die inhouding geldt nu alleen over je jaaroverschot; vanaf 2027 over alles, want dan is er geen saldering meer.";
  if (t.bonus) {
    var pct = Math.round(t.bonus.pct * 100);
    r += " Daarbovenop " + pct + "% " + t.bonus.naam + ", maar alleen over zonnestroom (" + t.bonus.venster +
         (t.bonus.max_kwh ? ", tot " + String(t.bonus.max_kwh).replace(/\B(?=(\d{3})+(?!\d))/g, ".") + " kWh per jaar" : "") +
         ") — niet over stroom die een thuisbatterij teruglevert.";
  }
  if (t.voorlopig) r += " Dit bedrag is nog een aanname; de aanbieder maakt het niet openbaar.";
  return r;
}

function rowHtml(s, c) {
  const vastPrefix = s.fixed_unconfirmed ? "≈ " : "";
  const jk = jaarkosten(s, c);
  const url = esc(s.website || "#");
  const id = esc(s.id);
  const letOp = s.let_op ? `          <div class="aanb-let-op">⚠️ ${esc(s.let_op)}</div>\n` : "";

  return `      <div class="aanb-rij" data-id="${id}">
        <div class="aanb-rij-kop">
          <label class="aanb-vergelijk" title="Zet naast een andere aanbieder"><input type="checkbox" class="vergelijk-check" data-id="${id}"><span class="aanb-sr">Vergelijk ${esc(s.name)}</span></label>
          <button class="aanb-open" type="button" aria-expanded="false" aria-controls="det-${id}">
            <span class="aanb-naam">${esc(s.name)}${badgesHtml(s)}</span>
            <span class="aanb-cel" data-k="opslag"><span class="aanb-cel-label">opslag</span><strong>${fmtCt(s.markup_per_kwh)} ct</strong></span>
            <span class="aanb-cel" data-k="vast"><span class="aanb-cel-label">vast/mnd</span><strong>${vastPrefix}€ ${fmtEur2(s.fixed_per_month)}</strong></span>
            <span class="aanb-cel aanb-jk"><span class="aanb-cel-label">per jaar</span><strong>${fmtJk(jk)}</strong></span>
            <span class="aanb-score ${scoreClass(s.score)}">${scoreTxt(s.score)}</span>
            <span class="aanb-caret" aria-hidden="true">▾</span>
          </button>
        </div>
        <div class="aanb-details" id="det-${id}" hidden>
${letOp}          <div><p class="aanb-sectie-label">App &amp; slim laden</p><p>${esc(s.app_text)}</p></div>
          <div><p class="aanb-sectie-label">Voor wie geschikt?</p><p>${esc(s.wie_text)}</p></div>
          <div class="aanb-teruglever"><p class="aanb-sectie-label">Teruglevering (zonnepanelen of batterij)</p><p>${esc(terugleverTekst(s))}</p></div>
          <p class="aanb-opzeg">⏱ Opzegtermijn: ${esc(s.opzeg || "—")} &middot; <a href="${url}" target="_blank" rel="noopener">naar de website ↗</a></p>
        </div>
      </div>`;
}

function topKaartHtml(item, c) {
  const s = item.s;
  return `      <article class="top-kaart" data-id="${esc(s.id)}">
        <span class="top-label">${esc(item.label)}</span>
        <p class="top-naam"><a href="${esc(s.website || "#")}" target="_blank" rel="noopener">${esc(s.name)}</a></p>
        <p class="top-jk">${fmtJk(jaarkosten(s, c))} <span>/ jaar</span></p>
        <p class="top-sub">${fmtCt(s.markup_per_kwh)} ct opslag &middot; € ${fmtEur2(s.fixed_per_month)} vast &middot; ${scoreTxt(s.score)}</p>
        <p class="top-waarom">${esc(eersteZin(s.wie_text))}</p>
        <label class="top-vergelijk"><input type="checkbox" class="vergelijk-check" data-id="${esc(s.id)}"> Vergelijk</label>
      </article>`;
}

// Identiek aan kiesTop() in aanbieders.html.
function kiesTop(list, c) {
  if (list.length < 6) return [];
  const uit = [], gebruikt = {};
  function pak(label, sub, kandidaten, beter) {
    const k = kandidaten.filter((s) => !gebruikt[s.id]);
    if (!k.length) return;
    const w = k.reduce((a, b) => (beter(a, b) ? a : b));
    gebruikt[w.id] = true;
    uit.push({ label, sub, s: w });
  }
  pak("Laagste kosten", "Goedkoopst bij " + fmtEur0(c.v) + " kWh", list,
      (a, b) => jaarkosten(a, c) <= jaarkosten(b, c));
  pak("Best beoordeeld", "Hoogste klanttevredenheid",
      list.filter((s) => s.score != null && !s.score_caveat),
      (a, b) => a.score >= b.score);
  pak("Slimste app", "Slim laden / Home Assistant",
      list.filter((s) => s.smart === true),
      (a, b) => (a.score || 0) >= (b.score || 0));
  return uit;
}

function vervang(html, startTag, endTag, inhoud) {
  const start = html.indexOf(startTag);
  const end = html.indexOf(endTag);
  if (start === -1 || end === -1 || end < start) {
    console.error("FOUT: markers " + startTag.slice(0, 24) + "... niet gevonden. Niets gewijzigd.");
    process.exit(1);
  }
  return html.slice(0, start + startTag.length) + "\n" + inhoud + "\n      " + html.slice(end);
}

function datumNl(iso) {
  const d = String(iso || "").split("-").map(Number);
  return d.length === 3 && d[0] ? d[2] + " " + MAANDEN[d[1] - 1] + " " + d[0] : "";
}
function plat(t) { return String(t).replace(/<[^>]+>/g, "").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim(); }

function samenvattingHtml(sorted, c, datum) {
  const a = sorted[0], b = sorted[1], d = sorted[2];
  return `      <p class="aanb-samenvatting"><strong>Goedkoopst bij ${fmtEur0(c.v)} kWh per jaar:</strong> ${esc(a.name)} (± € ${fmtEur0(jaarkosten(a, c))} aan opslag en vaste kosten zonder btw), gevolgd door ${esc(b.name)} (± € ${fmtEur0(jaarkosten(b, c))}) en ${esc(d.name)} (± € ${fmtEur0(jaarkosten(d, c))}). Tarieven gecontroleerd op ${datum}.</p>`;
}

function faqItems(sorted, top, c, datum, n) {
  const a = sorted[0], b = sorted[1], d = sorted[2];
  const best = top.find((t) => t.label === "Best beoordeeld");
  const slim = top.find((t) => t.label === "Slimste app");
  const items = [];
  items.push(["Wat is de goedkoopste dynamische energieleverancier?",
    `Bij ${fmtEur0(c.v)} kWh per jaar is dat nu ${esc(a.name)}, met ± € ${fmtEur0(jaarkosten(a, c))} aan opslag en vaste kosten zonder btw, gevolgd door ${esc(b.name)} (± € ${fmtEur0(jaarkosten(b, c))}) en ${esc(d.name)} (± € ${fmtEur0(jaarkosten(d, c))}). De stroomprijs per uur is bij alle aanbieders gelijk. Heb je zonnepanelen, vul dan je teruglevering in; dan kan de volgorde flink veranderen.`]);
  let beste = "Dat hangt af van wat je belangrijk vindt.";
  if (best) beste += ` Op klanttevredenheid scoort ${esc(best.s.name)} het hoogst, met ${best.s.score.toFixed(1).replace(".", ",")} op Trustpilot.`;
  if (slim) beste += ` Voor slim laden en een koppeling met Home Assistant springt ${esc(slim.s.name)} eruit.`;
  beste += " Met zonnepanelen telt vooral wat je krijgt of betaalt voor teruglevering.";
  items.push(["Wat is de beste dynamische energieleverancier?", beste]);
  items.push(["Hoe vergelijk je dynamische energiecontracten?",
    "De stroomprijs per uur is overal gelijk. Je vergelijkt de opslag per kWh, de vaste kosten per maand en, als je zonnepanelen hebt, de vergoeding of inhouding op teruglevering. Tel die op bij jouw verbruik, dan zie je welke aanbieder het goedkoopst is. Kijk daarna naar de app en de klanttevredenheid."]);
  items.push(["Wat is de opslag of inkoopvergoeding?",
    "Het bedrag dat een aanbieder per kWh bovenop de beursprijs rekent. Bij de meeste aanbieders is dat 1 tot 2 cent per kWh. Op de aanbiederslijst staan de bedragen zonder btw; op je rekening komt er 21% btw bij."]);
  items.push(["Telt teruglevering mee in de jaarkosten?",
    "Alleen als je invult hoeveel kWh je per jaar teruglevert. Dan rekent de vergelijker per aanbieder mee wat die bijbetaalt of inhoudt. Sommige aanbieders houden in 2026 alleen iets in over je jaaroverschot; vanaf 2027, als de saldering stopt, telt elke kWh. Daarom kun je kiezen tussen 2026 en 2027. Zonnebonussen zitten er niet in."]);
  items.push(["Hoe actueel zijn de tarieven?",
    `De tarieven van alle ${n} aanbieders controleer ik met de hand, via de websites en prijscalculators van de aanbieders zelf. De laatste controle was op ${datum}. Aanbieders kunnen hun tarieven tussendoor aanpassen; kijk voor je overstapt altijd op hun eigen site.`]);
  return items;
}

function faqHtml(items) {
  const vis = items.map(([q, a]) => `      <h3>${q}</h3>\n      <p>${a}</p>`).join("\n");
  const ld = {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    "mainEntity": items.map(([q, a]) => ({ "@type": "Question", "name": plat(q), "acceptedAnswer": { "@type": "Answer", "text": plat(a) } })),
  };
  return `      <h2>Veelgestelde vragen</h2>\n${vis}\n      <script type="application/ld+json">\n${JSON.stringify(ld, null, 2)}\n      </script>`;
}

function build() {
  const cfg = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
  const page = cfg.aanbieders_page || {};
  const verbruik = page.default_verbruik_kwh || 2900;

  const suppliers = (cfg.suppliers || []).filter(function (s) {
    return s.consumer !== false && s.id !== "average" && s.id !== "custom";
  });
  const c = { v: verbruik, t: 0, jaar: 2026 };
  suppliers.sort(function (a, b) { return jaarkosten(a, c) - jaarkosten(b, c); });
  const datum = datumNl(cfg.updated);

  const top = kiesTop(suppliers, c);
  const uitgelicht = {};
  top.forEach((t) => { uitgelicht[t.s.id] = true; });
  const rest = suppliers.filter((s) => !uitgelicht[s.id]);

  let html = fs.readFileSync(HTML_PATH, "utf8");
  html = vervang(html, TOP_START, TOP_END, top.map((t) => topKaartHtml(t, c)).join("\n"));
  html = vervang(html, CARDS_START, CARDS_END, rest.map((s) => rowHtml(s, c)).join("\n"));
  html = vervang(html, SAM_START, SAM_END, samenvattingHtml(suppliers, c, datum));
  html = vervang(html, FAQ_START, FAQ_END, faqHtml(faqItems(suppliers, top, c, datum, suppliers.length)));

  // Het aantal aanbieders staat ook in <title> en <h1>; waarschuw als dat niet meer klopt.
  const m = html.match(/<title>[^<]*?(\d+) aanbieders/);
  if (m && Number(m[1]) !== suppliers.length) {
    console.warn("LET OP: <title> noemt " + m[1] + " aanbieders, config heeft er " + suppliers.length + ". Pas title, h1, meta en intro aan.");
  }
  fs.writeFileSync(HTML_PATH, html, "utf8");

  console.log("OK: " + top.length + " topkaarten + " + rest.length + " regels gegenereerd (verbruik " + verbruik + " kWh, sortering jaarkosten).");
  top.forEach((t) => console.log("  " + t.label + ": " + t.s.name + " (± € " + fmtEur0(jaarkosten(t.s, c)) + "/jaar)"));
}

build();
