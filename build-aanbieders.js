#!/usr/bin/env node
/*
 * build-aanbieders.js
 * -------------------------------------------------------------------------
 * Genereert de statische aanbieder-inhoud uit public/data/config.json en
 * plakt die in public/aanbieders.html:
 *   - de uitgelichte top 3 tussen de BUILD:TOP-markers
 *   - de compacte regels tussen de BUILD:CARDS-markers
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

function rowHtml(s, verbruik) {
  const vastPrefix = s.fixed_unconfirmed ? "≈ " : "";
  const jk = jaarkosten(s, verbruik);
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
            <span class="aanb-cel aanb-jk"><span class="aanb-cel-label">per jaar</span><strong>± € ${fmtEur0(jk)}</strong></span>
            <span class="aanb-score ${scoreClass(s.score)}">${scoreTxt(s.score)}</span>
            <span class="aanb-caret" aria-hidden="true">▾</span>
          </button>
        </div>
        <div class="aanb-details" id="det-${id}" hidden>
${letOp}          <div><p class="aanb-sectie-label">App &amp; slim laden</p><p>${esc(s.app_text)}</p></div>
          <div><p class="aanb-sectie-label">Voor wie geschikt?</p><p>${esc(s.wie_text)}</p></div>
          <p class="aanb-opzeg">⏱ Opzegtermijn: ${esc(s.opzeg || "—")} &middot; <a href="${url}" target="_blank" rel="noopener">naar de website ↗</a></p>
        </div>
      </div>`;
}

function topKaartHtml(item, verbruik) {
  const s = item.s;
  return `      <article class="top-kaart" data-id="${esc(s.id)}">
        <span class="top-label">${esc(item.label)}</span>
        <p class="top-naam"><a href="${esc(s.website || "#")}" target="_blank" rel="noopener">${esc(s.name)}</a></p>
        <p class="top-jk">± € ${fmtEur0(jaarkosten(s, verbruik))} <span>/ jaar</span></p>
        <p class="top-sub">${fmtCt(s.markup_per_kwh)} ct opslag &middot; € ${fmtEur2(s.fixed_per_month)} vast &middot; ${scoreTxt(s.score)}</p>
        <p class="top-waarom">${esc(eersteZin(s.wie_text))}</p>
        <label class="top-vergelijk"><input type="checkbox" class="vergelijk-check" data-id="${esc(s.id)}"> Vergelijk</label>
      </article>`;
}

// Identiek aan kiesTop() in aanbieders.html.
function kiesTop(list, verbruik) {
  if (list.length < 6) return [];
  const uit = [], gebruikt = {};
  function pak(label, sub, kandidaten, beter) {
    const k = kandidaten.filter((s) => !gebruikt[s.id]);
    if (!k.length) return;
    const w = k.reduce((a, b) => (beter(a, b) ? a : b));
    gebruikt[w.id] = true;
    uit.push({ label, sub, s: w });
  }
  pak("Laagste kosten", "Goedkoopst bij " + fmtEur0(verbruik) + " kWh", list,
      (a, b) => jaarkosten(a, verbruik) <= jaarkosten(b, verbruik));
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

function build() {
  const cfg = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
  const page = cfg.aanbieders_page || {};
  const verbruik = page.default_verbruik_kwh || 2900;

  const suppliers = (cfg.suppliers || []).filter(function (s) {
    return s.consumer !== false && s.id !== "average" && s.id !== "custom";
  });
  suppliers.sort(function (a, b) { return jaarkosten(a, verbruik) - jaarkosten(b, verbruik); });

  const top = kiesTop(suppliers, verbruik);
  const uitgelicht = {};
  top.forEach((t) => { uitgelicht[t.s.id] = true; });
  const rest = suppliers.filter((s) => !uitgelicht[s.id]);

  let html = fs.readFileSync(HTML_PATH, "utf8");
  html = vervang(html, TOP_START, TOP_END, top.map((t) => topKaartHtml(t, verbruik)).join("\n"));
  html = vervang(html, CARDS_START, CARDS_END, rest.map((s) => rowHtml(s, verbruik)).join("\n"));
  fs.writeFileSync(HTML_PATH, html, "utf8");

  console.log("OK: " + top.length + " topkaarten + " + rest.length + " regels gegenereerd (verbruik " + verbruik + " kWh, sortering jaarkosten).");
  top.forEach((t) => console.log("  " + t.label + ": " + t.s.name + " (± € " + fmtEur0(jaarkosten(t.s, verbruik)) + "/jaar)"));
}

build();
