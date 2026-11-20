#!/usr/bin/env node
/*
 * build-batterijen.js
 * -------------------------------------------------------------------------
 * Bakt de lijst van /batterijen-vergelijken statisch in public/batterijen-vergelijken.html,
 * uit public/data/batterijen.json. De opmaak komt uit public/batterijen-render.js.
 *
 * Blokken (tussen BUILD:<NAAM>:START en BUILD:<NAAM>:END):
 *   LIJST  alle modellen als kaart
 *   NIET   de merken die bewust ontbreken
 *   STAND  de datum van de laatste controle
 *
 * Waarom statisch: zonder JavaScript (zoekmachines) zou de lijst leeg zijn.
 * Gebruik:  node build-batterijen.js  (na elke wijziging in batterijen.json)
 * -------------------------------------------------------------------------
 */
"use strict";

const fs = require("fs");
const path = require("path");
const R = require("./public/batterijen-render.js");

const DATA_PATH = path.join(__dirname, "public", "data", "batterijen.json");
const HTML_PATH = path.join(__dirname, "public", "batterijen-vergelijken.html");
const NAMEN = ["STAND", "LIJST", "NIET"];
const startMarker = (n) => "<!-- BUILD:" + n + ":START — automatisch gegenereerd door build-batterijen.js; niet handmatig bewerken -->";
const eindMarker = (n) => "<!-- BUILD:" + n + ":END -->";

const data = JSON.parse(fs.readFileSync(DATA_PATH, "utf8"));
const t = R.telling(data);
const inhoud = {
  STAND: "        " + t.totaal + " modellen: " + t.stekker + " stekkerbatterijen en " + t.vast + " vaste batterijen. Prijzen en specs gecontroleerd op " + R.datum(data.bijgewerkt) + ".",
  LIJST: R.lijstHtml(data),
  NIET: "        " + R.nietHtml(data)
};

let html = fs.readFileSync(HTML_PATH, "utf8");
for (const naam of NAMEN) {
  const start = startMarker(naam), eind = eindMarker(naam);
  const a = html.indexOf(start), b = html.indexOf(eind);
  if (a === -1 || b === -1 || b < a) { console.error("Markers voor " + naam + " niet gevonden in " + HTML_PATH); process.exit(1); }
  html = html.slice(0, a + start.length) + "\n" + inhoud[naam] + "\n        " + html.slice(b);
}
fs.writeFileSync(HTML_PATH, html, "utf8");

const oud = data.batterijen.filter((b) => R.dagenOud(b.gecontroleerd, Date.now()) > R.OUD_DAGEN);
console.log("batterijen-vergelijken.html bijgewerkt: " + t.totaal + " modellen (" + t.stekker + " stekker, " + t.vast + " vast).");
if (oud.length) console.warn("LET OP: langer dan " + R.OUD_DAGEN + " dagen niet gecontroleerd: " + oud.map((b) => b.merk + " " + b.model).join(", "));
