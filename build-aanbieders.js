#!/usr/bin/env node
/*
 * build-aanbieders.js
 * -------------------------------------------------------------------------
 * Bakt de inhoud van /aanbieders statisch in public/aanbieders.html, zodat
 * zoekmachines, AI-crawlers en bezoekers zonder JavaScript dezelfde lijst
 * zien als wie de pagina met JavaScript opent.
 *
 * Rekenen en HTML komen uit public/aanbieders-render.js, dezelfde module die
 * de browser gebruikt. Er is dus maar één plek om iets aan de regels of de
 * rekensom te veranderen.
 *
 * Blokken (tussen <!-- BUILD:NAAM:START --> en <!-- BUILD:NAAM:END -->):
 *   VERSCHIL      zin "scheelt € X per jaar" bij het standaardverbruik
 *   CARDS         de 25 regels, gesorteerd op kosten bij het standaardverbruik
 *   SAMENVATTING  goedkoopste drie + controledatum
 *   SITUATIES     drie secties: zonnepanelen, thuisbatterij, elektrische auto
 *   FAQ           vragen + FAQPage-JSON-LD
 *   ITEMLIST      ItemList-JSON-LD met alle aanbieders
 *
 * Gebruik: node build-aanbieders.js   (na elke wijziging aan config.json of
 * aanbieders-render.js, vóór je pusht). Idempotent: twee keer draaien geeft
 * een byte-identiek bestand.
 * -------------------------------------------------------------------------
 */
"use strict";

const fs = require("fs");
const path = require("path");
const R = require("./public/aanbieders-render.js");

const CONFIG_PATH = path.join(__dirname, "public", "data", "config.json");
const HTML_PATH = path.join(__dirname, "public", "aanbieders.html");

function vervang(html, naam, inhoud) {
  const start = "<!-- BUILD:" + naam + ":START -->";
  const eind = "<!-- BUILD:" + naam + ":END -->";
  const i = html.indexOf(start), j = html.indexOf(eind);
  if (i === -1 || j === -1 || j < i) {
    console.error("Marker BUILD:" + naam + " niet gevonden in aanbieders.html");
    process.exit(1);
  }
  return html.slice(0, i + start.length) + inhoud + html.slice(j);
}
function plat(t) {
  return String(t).replace(/<[^>]+>/g, "").replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/\s+/g, " ").trim();
}
function namen(lijst) {
  const n = lijst.map((s) => s.name);
  return n.length < 2 ? n.join("") : n.slice(0, -1).join(", ") + " en " + n[n.length - 1];
}
function jsonLd(obj, inspringing) {
  return inspringing + '<script type="application/ld+json">\n' + JSON.stringify(obj, null, 2) + "\n" + inspringing + "</script>";
}

function build() {
  const cfg = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
  const lijst = (cfg.suppliers || []).filter((s) => s.consumer !== false && s.id !== "average" && s.id !== "custom");
  const c = R.standaardSituatie(cfg.aanbieders_page);
  const EFFECT = path.join(__dirname, "public", "data", "batterij-effect.json");
  const effect = fs.existsSync(EFFECT) ? JSON.parse(fs.readFileSync(EFFECT, "utf8")) : null;
  c.basis = R.rekenbasis(cfg, effect, c, 0);
  if (!c.basis) console.warn("LET OP: geen batterij-effect.json of netbeheer in config; de lijst toont alleen wat de aanbieder rekent.");
  const datum = R.datumNL(cfg.updated);
  const kwhTxt = R.duizend(c.afname) + " kWh";
  const gesorteerd = R.sorteer(lijst, "kosten", c);
  const kost = (s, sit) => R.eur0(R.kosten(s, sit || c).totaal);
  let html = fs.readFileSync(HTML_PATH, "utf8");

  // VERSCHIL en CARDS
  html = vervang(html, "VERSCHIL", R.verschilTekst(lijst, c));
  html = vervang(html, "CARDS", "\n" + R.lijstHtml(gesorteerd, c, {}) + "\n      ");

  // SAMENVATTING
  const top3 = gesorteerd.slice(0, 3);
  const voorbehoud = top3.filter((s) => R.isVoorlopig(s, c));
  let sam = "Goedkoopst bij " + kwhTxt + " per jaar, inclusief btw: " + top3[0].name + " (" + kost(top3[0]) +
    " aan opslag en vaste kosten), gevolgd door " + top3[1].name + " (" + kost(top3[1]) + ") en " + top3[2].name + " (" + kost(top3[2]) + ").";
  if (voorbehoud.length) sam += " Bij " + namen(voorbehoud) + " komt een deel van de tarieven uit een tweede bron.";
  if (c.basis) {
    const maand = (s) => R.eur0(R.kosten(s, c).rekening / 12);
    sam += " Je hele stroomrekening komt dan op ± " + maand(top3[0]) + " per maand bij " + top3[0].name + " en ± " +
           maand(gesorteerd[gesorteerd.length - 1]) + " bij de duurste, inclusief beursprijs, energiebelasting, netbeheer en de vermindering energiebelasting.";
  }
  sam += " Tarieven gecontroleerd op " + datum + ".";
  html = vervang(html, "SAMENVATTING", '\n    <p class="aanb-samenvatting">' + sam + "</p>\n    ");

  // SITUATIES
  const zon26 = Object.assign({}, c, { afname: 3500, zon: true, terug: 3000, jaar: 2026 });
  const zon27 = Object.assign({}, zon26, { jaar: 2027 });
  const zTop26 = R.sorteer(lijst, "kosten", zon26).slice(0, 3);
  const zTop27 = R.sorteer(lijst, "kosten", zon27).slice(0, 3);
  const bonus = lijst.filter((s) => s.teruglevering && s.teruglevering.bonus);
  const met = (f) => lijst.filter((s) => R.kenmerk(s, f).status === "ja");
  const bat = met("batterij"), ev = met("ev"), kw = met("kwartier");
  const lijstjes = (arr, f) => "<ul>" + arr.map((s) => "<li><strong>" + R.esc(s.name) + "</strong>: " + R.esc(R.kenmerk(s, f).hoe || "ja") +
    (R.kenmerk(s, f).voorwaarde ? " (" + R.esc(R.kenmerk(s, f).voorwaarde) + ")" : "") + "</li>").join("") + "</ul>";

  let sit = "\n";
  sit += '      <h2 id="zonnepanelen">Dynamisch contract met zonnepanelen</h2>\n';
  sit += "      <p>Met zonnepanelen telt vooral wat de aanbieder doet met je teruglevering. Bij 3.500 kWh afname en 3.000 kWh teruglevering zijn in 2026 " +
         zTop26.map((s) => R.esc(s.name) + " (" + kost(s, zon26) + ")").join(", ") + " het goedkoopst, als je kijkt naar wat de aanbieder rekent. Vanaf 2027, als de saldering stopt en de tarieven gelijk blijven, zijn dat " +
         zTop27.map((s) => R.esc(s.name) + " (" + kost(s, zon27) + ")").join(", ") + ".</p>\n";
  sit += "      <p>" + namen(bonus) + " geven daarbovenop een bonus over zonnestroom. Die zit niet in de bedragen, omdat hij afhangt van wanneer je teruglevert. Meer uitleg staat in <a href=\"/kennisbank/teruglevertarief-vergelijken\">teruglevertarieven vergelijken</a>.</p>\n";
  sit += '      <h2 id="thuisbatterij">Dynamisch contract met een thuisbatterij</h2>\n';
  sit += "      <p>Volgens hun eigen websites kunnen deze " + bat.length + " aanbieders je thuisbatterij automatisch aansturen:</p>\n      " + lijstjes(bat, "batterij") + "\n";
  sit += "      <p>Per kwartier rekenen volgens een tweede bron " + namen(kw) + ". Van de andere aanbieders hebben we het nog niet vastgesteld. Let op: de zonnebonussen gelden niet voor stroom die een thuisbatterij teruglevert. Wat een batterij jou oplevert, reken je uit met <a href=\"/batterij-berekenen\">de batterijcalculator</a>.</p>\n";
  sit += '      <h2 id="elektrische-auto">Dynamisch contract met een elektrische auto</h2>\n';
  sit += "      <p>Deze " + ev.length + " aanbieders laden je auto volgens hun eigen websites automatisch op goedkope uren:</p>\n      " + lijstjes(ev, "ev") + "\n";
  sit += "      <p>Met Home Assistant of een slimme laadpaal kun je bij elke aanbieder zelf op de beursprijs sturen; zie <a href=\"/home-assistant\">Home Assistant</a>. Thuisladen levert daarnaast een ERE-vergoeding op, los van je contract: <a href=\"/ere-vergelijken\">de ERE-vergelijker</a>.</p>\n      ";
  html = vervang(html, "SITUATIES", sit);

  // FAQ
  const genoegReviews = lijst.filter((s) => s.score != null && (s.score_reviews || 0) >= 100).sort((a, b) => b.score - a.score);
  const hoogste = genoegReviews.filter((s) => s.score === genoegReviews[0].score);
  const opslagIncl = lijst.map((s) => s.markup_per_kwh * R.BTW);
  const faq = [
    ["Wat is de goedkoopste dynamische energieleverancier?",
     "Bij " + kwhTxt + " per jaar zonder zonnepanelen is dat nu " + top3[0].name + ", met " + kost(top3[0]) + " per jaar aan opslag en vaste kosten inclusief btw, gevolgd door " +
     top3[1].name + " (" + kost(top3[1]) + ") en " + top3[2].name + " (" + kost(top3[2]) + "). De stroomprijs per uur is bij alle aanbieders gelijk. Heb je zonnepanelen, vul dan je teruglevering in; dan verandert de volgorde flink."],
    ...(c.basis ? [["Wat kost een dynamisch energiecontract per maand?",
     "Bij " + kwhTxt + " stroom per jaar, zonder zonnepanelen, komt je hele stroomrekening op ± " + R.eur0(R.kosten(top3[0], c).rekening / 12) +
     " per maand bij de goedkoopste aanbieder en ± " + R.eur0(R.kosten(gesorteerd[gesorteerd.length - 1], c).rekening / 12) +
     " bij de duurste. Daarin zitten de stroom tegen de beursprijs van de afgelopen twaalf maanden, energiebelasting, netbeheer, de vaste kosten en opslag van de aanbieder, min de vermindering energiebelasting. Gas telt niet mee. Wat je precies betaalt, hangt af van wanneer je stroom gebruikt."]] : []),
    ["Wat is de beste dynamische energieleverancier?",
     "Dat hangt af van je situatie. Met zonnepanelen telt vooral de teruglevering, met een elektrische auto of thuisbatterij of de aanbieder die kan aansturen. Op Trustpilot heeft " +
     namen(hoogste) + " de hoogste score (" + hoogste[0].score.toFixed(1).replace(".", ",") + ") van de aanbieders met minstens 100 reviews."],
    ["Hoe vergelijk je dynamische energiecontracten?",
     "De stroomprijs per uur is overal gelijk. Je vergelijkt de opslag per kWh, de vaste kosten per maand en, als je zonnepanelen hebt, wat de aanbieder bijbetaalt of inhoudt op je teruglevering. Tel die op bij jouw afname van het net, dan zie je welke aanbieder het goedkoopst is. Kijk daarna of de aanbieder je auto of thuisbatterij kan aansturen."],
    ["Wat is de opslag of inkoopvergoeding?",
     "Het bedrag dat een aanbieder per kWh bovenop de beursprijs rekent. Bij de " + lijst.length + " aanbieders op deze pagina ligt dat tussen " +
     (Math.min.apply(null, opslagIncl) * 100).toFixed(1).replace(".", ",") + " en " + (Math.max.apply(null, opslagIncl) * 100).toFixed(1).replace(".", ",") + " cent per kWh inclusief btw."],
    ["Telt teruglevering mee in de kosten?",
     "Alleen als je aangeeft dat je zonnepanelen hebt en invult hoeveel kWh je per jaar teruglevert. Sommige aanbieders houden in 2026 alleen iets in over je jaaroverschot; vanaf 2027, als de saldering stopt, telt elke kWh. Zonnebonussen zitten niet in het bedrag."],
    ["Welke aanbieders sturen een thuisbatterij of elektrische auto aan?",
     "Een thuisbatterij: " + namen(bat) + ". Een elektrische auto of laadpaal: " + namen(ev) + ". Dat staat op hun eigen websites; bij de andere aanbieders hebben we het nog niet vastgesteld. De voorwaarden verschillen, bijvoorbeeld een eigen laadpaal of batterij van de aanbieder."],
    ["Hoe actueel zijn de tarieven?",
     "De tarieven van alle " + lijst.length + " aanbieders controleer ik met de hand, via de websites en prijscalculators van de aanbieders zelf. De laatste controle was op " + datum +
     ". Waar een aanbieder iets niet openbaar maakt, staat dat bij het bedrag. Kijk voor je overstapt altijd op de site van de aanbieder."],
  ];
  let fq = '\n      <h2 id="vragen">Veelgestelde vragen</h2>\n';
  faq.forEach((q) => { fq += "      <h3>" + q[0] + "</h3>\n      <p>" + R.esc(q[1]) + "</p>\n"; });
  fq += jsonLd({
    "@context": "https://schema.org", "@type": "FAQPage",
    mainEntity: faq.map((q) => ({ "@type": "Question", name: q[0], acceptedAnswer: { "@type": "Answer", text: plat(q[1]) } }))
  }, "      ") + "\n      ";
  html = vervang(html, "FAQ", fq);

  // ITEMLIST
  const itemList = {
    "@context": "https://schema.org", "@type": "ItemList",
    name: "Aanbieders van een dynamisch energiecontract, gesorteerd op kosten bij " + kwhTxt,
    numberOfItems: gesorteerd.length,
    itemListElement: gesorteerd.map((s, i) => ({ "@type": "ListItem", position: i + 1, name: s.name, url: s.website }))
  };
  html = vervang(html, "ITEMLIST", "\n" + jsonLd(itemList, "    ") + "\n    ");

  // Controles
  const m = html.match(/<title>[^<]*?(\d+) aanbieders/);
  if (m && parseInt(m[1], 10) !== lijst.length) {
    console.warn("LET OP: <title> noemt " + m[1] + " aanbieders, config heeft er " + lijst.length + ". Pas title, h1 en meta aan.");
  }
  const oud = lijst.filter((s) => R.dagenOud(s.verified) > R.VEROUDERD_DAGEN);
  if (oud.length) console.warn("LET OP: tarieven ouder dan " + R.VEROUDERD_DAGEN + " dagen bij " + namen(oud) + ".");

  fs.writeFileSync(HTML_PATH, html, "utf8");
  console.log("aanbieders.html bijgewerkt: " + gesorteerd.length + " aanbieders, goedkoopst " + top3[0].name + " (" + kost(top3[0]) + " bij " + kwhTxt + ").");
}

build();
