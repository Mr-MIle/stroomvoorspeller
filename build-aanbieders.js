#!/usr/bin/env node
/*
 * build-aanbieders.js
 * -------------------------------------------------------------------------
 * Genereert de statische aanbieder-kaarten uit public/data/config.json en
 * plakt ze in public/aanbieders.html tussen de BUILD:CARDS-markers.
 *
 * Waarom: de pagina rendert de kaarten normaal met JavaScript (sorteren +
 * filteren). Zoekmachines en bezoekers zonder JS zien dan een lege grid.
 * Dit script bakt dezelfde kaarten één keer statisch in de HTML, in de
 * standaardvolgorde (geschatte jaarkosten, laag -> hoog). De JS rendert er
 * bij het laden identiek overheen, dus geen dubbele inhoud of sprong.
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
const START_TAG = "<!-- BUILD:CARDS:START — automatisch gegenereerd door build-aanbieders.js; niet handmatig bewerken -->";
const END_TAG = "<!-- BUILD:CARDS:END -->";

// ── opmaak-helpers (identiek aan de browser-render in aanbieders.html) ──────
function fmtCt(m) { return (m * 100).toFixed(2).replace(".", ","); }
function fmtEur2(v) { return v.toFixed(2).replace(".", ","); }
function fmtEur0(v) { return String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, "."); }
function esc(t) {
  return String(t == null ? "" : t)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function jaarkosten(s, v) { return s.markup_per_kwh * v + s.fixed_per_month * 12; }
function scoreClass(score) {
  if (score == null) return "score-matig";
  if (score >= 4) return "score-goed";
  if (score >= 3) return "score-matig";
  return "score-slecht";
}

function cardHtml(s, verbruik) {
  const scoreTxt = (s.score == null) ? "n.v.t." : (s.score.toFixed(1).replace(".", ",") + "/5 ★");
  const vastPrefix = s.fixed_unconfirmed ? "≈ " : "";
  const jk = jaarkosten(s, verbruik);
  const url = esc(s.website || "#");
  const letOp = s.let_op
    ? `          <div class="aanbieder-let-op">⚠️ ${esc(s.let_op)}</div>\n`
    : "";

  return `      <article class="aanbieder-card" data-id="${esc(s.id)}">
        <div class="aanbieder-card-header">
          <div class="aanbieder-naam"><a href="${url}" target="_blank" rel="noopener">${esc(s.name)}</a></div>
          <span class="aanbieder-score ${scoreClass(s.score)}">${scoreTxt}</span>
        </div>
        <div class="aanbieder-tarieven">
          <div class="tarief-blok"><span class="tarief-label">Opslag</span><span class="tarief-waarde">${fmtCt(s.markup_per_kwh)} ct<span class="tarief-eenheid">/kWh excl. btw</span></span></div>
          <div class="tarief-blok"><span class="tarief-label">Vast per maand</span><span class="tarief-waarde">${vastPrefix}€ ${fmtEur2(s.fixed_per_month)}<span class="tarief-eenheid">excl. btw</span></span></div>
        </div>
        <p class="aanbieder-jaarkosten">Geschatte leverancierskosten: <strong>± € ${fmtEur0(jk)} / jaar</strong><span class="jk-sub">opslag + vastrecht bij ${fmtEur0(verbruik)} kWh — excl. marktprijs, energiebelasting &amp; btw</span></p>
        <label class="aanbieder-vergelijk"><input type="checkbox" class="vergelijk-check" data-id="${esc(s.id)}"> Vergelijk</label>
        <button class="aanbieder-toggle" type="button" aria-expanded="false"><span class="aanbieder-toggle-label">Meer info</span> <span class="caret" aria-hidden="true">▾</span></button>
        <div class="aanbieder-details">
${letOp}          <div><p class="aanbieder-sectie-label">App &amp; slim laden</p><p class="aanbieder-app-tekst">${esc(s.app_text)}</p></div>
          <div><p class="aanbieder-sectie-label">Voor wie geschikt?</p><p class="aanbieder-wie-tekst">${esc(s.wie_text)}</p></div>
          <p class="aanbieder-opzegtermijn">⏱ Opzegtermijn: ${esc(s.opzeg || "—")}</p>
        </div>
      </article>`;
}

function build() {
  const cfg = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
  const page = cfg.aanbieders_page || {};
  const verbruik = page.default_verbruik_kwh || 2900;

  const suppliers = (cfg.suppliers || []).filter(function (s) {
    return s.consumer !== false && s.id !== "average" && s.id !== "custom";
  });
  suppliers.sort(function (a, b) { return jaarkosten(a, verbruik) - jaarkosten(b, verbruik); });

  const cards = suppliers.map(function (s) { return cardHtml(s, verbruik); }).join("\n");

  let html = fs.readFileSync(HTML_PATH, "utf8");
  const start = html.indexOf(START_TAG);
  const end = html.indexOf(END_TAG);
  if (start === -1 || end === -1 || end < start) {
    console.error("FOUT: BUILD:CARDS-markers niet gevonden in aanbieders.html. Niets gewijzigd.");
    process.exit(1);
  }
  const before = html.slice(0, start + START_TAG.length);
  const after = html.slice(end);
  html = before + "\n" + cards + "\n      " + after;

  fs.writeFileSync(HTML_PATH, html, "utf8");
  console.log("OK: " + suppliers.length + " kaarten gegenereerd (verbruik " + verbruik + " kWh, sortering jaarkosten).");
  console.log("Goedkoopste: " + suppliers[0].name + " (± € " + fmtEur0(jaarkosten(suppliers[0], verbruik)) + "/jaar)");
}

build();
