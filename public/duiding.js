/*
 * duiding.js — toont de automatische prijsduiding bij de morgen-grafiek.
 * Standalone: haalt /data/duiding.json op en plaatst de strook (optie 1) plus
 * de uitklap "Waarom is dit zo?" (optie 3) direct onder de samenvatting.
 *
 * Plaatsen: <script defer src="/duiding.js"></script> in morgen.html,
 * en <link rel="stylesheet" href="/duiding.css"> in de <head>.
 * Geen wijziging aan morgen.js nodig.
 */
(function () {
  "use strict";

  var DATA_URL = "/data/duiding.json";

  var ICON = {
    bulb: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12c.7.7 1 1.3 1 2h6c0-.7.3-1.3 1-2a7 7 0 0 0-4-12Z"/></svg>',
    warn: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg>',
    chevron: '<svg class="duiding-chevron" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>',
    tip: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12c.7.7 1 1.3 1 2h6c0-.7.3-1.3 1-2a7 7 0 0 0-4-12Z"/></svg>'
  };

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function chipClass(type) {
    if (type === "laag") return "duiding-chip--laag";
    if (type === "hoog") return "duiding-chip--hoog";
    return "duiding-chip--neutraal";
  }

  function render(d) {
    var wrap = document.createElement("div");
    wrap.className = "duiding";
    wrap.setAttribute("data-niveau", d.niveau || "normaal");

    var waarschuwing = d.waarschuwing
      ? '<div class="duiding-waarschuwing">' + ICON.warn + "<span>" + esc(d.waarschuwing) + "</span></div>"
      : "";

    var chips = (d.waarom || []).map(function (r) {
      return '<span class="duiding-chip ' + chipClass(r.type) + '"><strong>' +
        esc(r.label) + "</strong> " + esc(r.tekst) + "</span>";
    }).join("");

    var datum = d.gegenereerd ? esc(d.gegenereerd.replace("T", " ").slice(0, 16)) : "";

    wrap.innerHTML =
      '<div class="duiding-strook">' +
        '<span class="duiding-icon" aria-hidden="true">' + ICON.bulb + "</span>" +
        "<div>" +
          '<p class="duiding-kop">' + esc(d.kop) + "</p>" +
          '<p class="duiding-uitleg">' + esc(d.uitleg) + "</p>" +
          waarschuwing +
        "</div>" +
      "</div>" +
      '<button class="duiding-toggle" type="button" aria-expanded="false" aria-controls="duiding-detail">' +
        "Waarom is dit zo? " + ICON.chevron +
      "</button>" +
      '<div class="duiding-detail" id="duiding-detail" hidden>' +
        '<div class="duiding-chips">' + chips + "</div>" +
        '<p class="duiding-tip">' + ICON.tip + "<span>" + esc(d.tip) + "</span></p>" +
        '<p class="duiding-bron">' + esc(d.bron) + (datum ? " &middot; " + datum : "") + "</p>" +
      "</div>";

    var btn = wrap.querySelector(".duiding-toggle");
    var detail = wrap.querySelector(".duiding-detail");
    btn.addEventListener("click", function () {
      var open = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", open ? "false" : "true");
      detail.hidden = open;
    });
    return wrap;
  }

  function todayISO() {
    var n = new Date();
    return n.getFullYear() + "-" +
      String(n.getMonth() + 1).padStart(2, "0") + "-" +
      String(n.getDate()).padStart(2, "0");
  }

  function mount() {
    var anchor = document.getElementById("morgen-summary-box");
    if (!anchor) return;
    fetch(DATA_URL, { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.kop) return;
        if (d.datum && d.datum < todayISO()) return;   // verberg verouderde duiding
        if (document.querySelector(".duiding")) return;
        anchor.insertAdjacentElement("afterend", render(d));
      })
      .catch(function () { /* stil falen: liever geen duiding dan een fout */ });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
