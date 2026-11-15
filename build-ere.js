#!/usr/bin/env node
/*
 * build-ere.js
 * -------------------------------------------------------------------------
 * Bakt de inhoud van de ERE-vergelijker statisch in public/ere-vergelijken.html,
 * uit public/data/ere.json. De opmaak komt uit public/ere-render.js, dezelfde
 * code die de browser gebruikt; er is dus maar één plek om iets aan te passen.
 *
 * Blokken (tussen BUILD:<NAAM>:START en BUILD:<NAAM>:END):
 *   STAND    de standregel (ERE-prijs + datum)
 *   VERSCHIL de zin tussen invoer en lijst (verschil hoogste – laagste)
 *   ERE      de top 3 als kaart en de rest als regels
 *   KORT     het korte antwoord onder de lijst; het script waarschuwt als
 *            nummer 1 of de laagste commissie niet meer past bij de vaste
 *            zinnen in ere.json -> kort
 *   MARKT    de marktreeks (prijs per ERE per kwartaal)
 *   BETAALD  de uitbetaaltabel
 *   NIET     de inboekers die bewust ontbreken
 *
 * Waarom statisch: zonder JavaScript (zoekmachines) zou de lijst leeg zijn.
 * De standaardvolgorde is opbrengst per jaar, hoog -> laag, bij
 * aannames.kwh_per_jaar. De browser rendert er bij het laden identiek overheen.
 *
 * Gebruik:  node build-ere.js
 * Draai dit telkens nadat je ere.json of ere-render.js hebt aangepast.
 * -------------------------------------------------------------------------
 */
"use strict";

const fs = require("fs");
const path = require("path");
const R = require("./public/ere-render.js");

const DATA_PATH = path.join(__dirname, "public", "data", "ere.json");
const HTML_PATH = path.join(__dirname, "public", "ere-vergelijken.html");
const NAMEN = ["STAND", "VERSCHIL", "ERE", "KORT", "MARKT", "BETAALD", "NIET"];
const startMarker = (n) => "<!-- BUILD:" + n + ":START — automatisch gegenereerd door build-ere.js; niet handmatig bewerken -->";
const eindMarker = (n) => "<!-- BUILD:" + n + ":END -->";

const data = JSON.parse(fs.readFileSync(DATA_PATH, "utf8"));
const v = data.aannames.kwh_per_jaar;

const inhoud = {
  STAND: "        " + R.standTekst(data),
  VERSCHIL: "        " + R.verschilHtml(data, v),
  ERE: R.lijstHtml(data, v, "alle"),
  KORT: "        " + R.kortHtml(data, v),
  MARKT: R.marktHtml(data),
  BETAALD: R.betaaldHtml(data),
  NIET: "        " + R.nietHtml(data)
};

let html = fs.readFileSync(HTML_PATH, "utf8");
for (const naam of NAMEN) {
  const start = startMarker(naam), eind = eindMarker(naam);
  const a = html.indexOf(start);
  const b = html.indexOf(eind);
  if (a === -1 || b === -1 || b < a) {
    console.error("Markers voor " + naam + " niet gevonden in " + HTML_PATH);
    process.exit(1);
  }
  html = html.slice(0, a + start.length) + "\n" + inhoud[naam] + "\n        " + html.slice(b);
}
fs.writeFileSync(HTML_PATH, html, "utf8");

const lijst = R.sorteer(data, data.inboekers, v);
console.log("ere-vergelijken.html bijgewerkt: " + lijst.length + " inboekers, " +
  data.markt.reeks.length + " kwartalen, marktprijs € " + R.komma(R.marktPrijs(data), 4) + " per ERE.");
const ks = R.kortStand(data, v), vk = data.kort || {};
console.log("Kort antwoord: hoogste " + ks.top.naam + (R.opAankondiging(data, ks.top) ? " (op aankondiging)" : "") +
  ", laagste commissie " + ks.laag.naam + ", verschil " +
  R.eur0(R.perJaar(data, ks.top, v) - R.perJaar(data, ks.laatste, v)) + " per jaar.");
if (!vk.hoogste || vk.hoogste.id !== ks.top.id)
  console.warn("LET OP: nummer 1 is nu " + ks.top.naam + ", niet " + (vk.hoogste ? vk.hoogste.id : "?") +
    ". De noot bij 'Hoogste bedrag' valt weg; zet kort.hoogste in ere.json opnieuw en lees het korte antwoord na.");
if (!vk.laagste_commissie || vk.laagste_commissie.id !== ks.laag.id)
  console.warn("LET OP: laagste commissie is nu " + ks.laag.naam + ", niet " + (vk.laagste_commissie ? vk.laagste_commissie.id : "?") +
    ". De noot bij 'Laagste servicekosten' valt weg; zet kort.laagste_commissie in ere.json opnieuw en lees het korte antwoord na.");
lijst.forEach((i) => {
  const j = R.perJaar(data, i, v);
  console.log("  " + i.naam.padEnd(28) + (j == null ? "onbekend" : R.eur0(j) + " / jaar"));
});
