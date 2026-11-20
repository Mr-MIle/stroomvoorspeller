/*
 * batterijen-render.js
 * Opmaak van /batterijen-vergelijken uit /data/batterijen.json.
 * Draait in Node (build-batterijen.js bakt de lijst statisch in de HTML) en in de browser (filter, datumcheck).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.BatRender = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var DAGPRIJS = {
    zelf: { kort: "Handelt zelf op de dagprijs", cls: "b-zelf" },
    "via-leverancier": { kort: "Handelt via je leverancier", cls: "b-lev" },
    beperkt: { kort: "Handelt niet zelf op de dagprijs", cls: "b-beperkt" }
  };
  var MND = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"];
  var OUD_DAGEN = 90;

  function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
  function nl(x) { return (+x).toLocaleString("nl-NL", { maximumFractionDigits: 2 }); }
  function eur(x) { return "€" + Math.round(x).toLocaleString("nl-NL"); }
  function datum(iso) { var d = String(iso).split("-").map(Number); return d[2] + " " + MND[d[1] - 1] + " " + d[0]; }
  function dagenOud(iso, nu) { var d = String(iso).split("-").map(Number); return Math.floor((nu - Date.UTC(d[0], d[1] - 1, d[2])) / 86400000); }

  // laagste prijs per kWh van een model (de uitvoering die het goedkoopst per kWh is)
  function perKwh(b) { return Math.min.apply(null, b.uitvoeringen.map(function (u) { return u.prijs / u.kwh; })); }

  function sorteer(lijst) {
    return lijst.slice().sort(function (a, b) {
      if (a.type !== b.type) return a.type === "stekker" ? -1 : 1;
      return perKwh(a) - perKwh(b);
    });
  }

  function lijstje(titel, items, cls) {
    if (!items || !items.length) return "";
    return '<div class="bv-vt ' + cls + '"><h4>' + titel + "</h4><ul>" + items.map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</ul></div>";
  }
  function spec(label, waarde) { return waarde ? "<dt>" + label + "</dt><dd>" + esc(waarde) + "</dd>" : ""; }

  function kaart(b, std) {
    std = std || {};
    var dp = DAGPRIJS[b.dagprijs] || DAGPRIJS.beperkt;
    var caps = b.uitvoeringen.map(function (u) { return nl(u.kwh); }).join(" of ") + " kWh";
    var kw = b.type === "stekker" ? "800 W via het stopcontact" : b.uitvoeringen.map(function (u) { return nl(u.kw); }).filter(function (x, i, a) { return a.indexOf(x) === i; }).join(" of ") + " kW";
    var prijzen = b.uitvoeringen.map(function (u) { return eur(u.prijs); }).join(" / ");
    var rekenen = b.uitvoeringen.map(function (u, i) {
      return '<a class="bv-reken" href="/batterij-berekenen?mod=' + encodeURIComponent(b.id + ":" + i) + '">Reken ' + (b.uitvoeringen.length > 1 ? nl(u.kwh) + " kWh " : "") + "door</a>";
    }).join("");
    var rend = b.rendement_tekst || "";
    var sluip = b.sluip_w != null ? b.sluip_w + " watt (" + b.sluip_bron + ")" : "niet opgegeven; wij rekenen met " + (b.type === "stekker" ? std.sluip_stekker_w || 10 : std.sluip_vast_w || 15) + " watt";
    return '<article class="bv-kaart" id="' + esc(b.id) + '" data-type="' + b.type + '" data-dagprijs="' + esc(b.dagprijs) + '" data-gecontroleerd="' + esc(b.gecontroleerd) + '">' +
      '<div class="bv-kop"><h3 class="bv-naam">' + esc(b.merk) + ' <span>' + esc(b.model) + "</span></h3>" +
      '<span class="bv-type">' + (b.type === "stekker" ? "Stekker" : "Vast, met installateur") + "</span></div>" +
      '<div class="bv-cellen"><span class="bv-cel"><b>' + caps + "</b></span><span class=\"bv-cel\">" + kw + "</span>" +
      '<span class="bv-cel">' + (b.type === "stekker" ? "" : "met installatie ") + "<b>" + prijzen + "</b></span>" +
      '<span class="bv-cel">' + eur(perKwh(b)) + " per kWh</span></div>" +
      '<p class="bv-sturing"><span class="bv-badge ' + dp.cls + '">' + dp.kort + "</span> " + esc(b.sturing) + "</p>" +
      '<div class="bv-voortegen">' + lijstje("Voor", b.voor, "voor") + lijstje("Tegen", b.tegen, "tegen") + "</div>" +
      '<details class="bv-meer"><summary>Garantie, rendement en bronnen</summary><dl>' +
      spec("Prijs", b.prijs_tekst) + spec("Garantie", b.garantie) + spec("Rendement", rend) + spec("Sluipverbruik", sluip) +
      spec("Uit te breiden", b.uitbreidbaar) + spec("Noodstroom", b.noodstroom) + spec("Plaatsing", b.plaatsing) + spec("Aansluiting", b.fase) +
      "</dl><p class=\"bv-bron\">Bronnen: " + b.bronnen.map(function (x) { return '<a href="' + esc(x.url) + '" rel="noopener" target="_blank">' + esc(x.naam) + "</a>"; }).join(" · ") +
      '. <span class="bv-datum">Gecontroleerd op ' + datum(b.gecontroleerd) + ".</span></p></details>" +
      '<div class="bv-acties">' + rekenen + "</div>" +
      "</article>";
  }

  function lijstHtml(data) {
    return sorteer(data.batterijen).map(function (b) { return kaart(b, data.standaard); }).join("\n");
  }
  function nietHtml(data) {
    return "<ul>" + (data.niet_opgenomen || []).map(function (n) { return "<li><b>" + esc(n.naam) + "</b>: " + esc(n.reden) + ".</li>"; }).join("") + "</ul>";
  }
  function telling(data) {
    var s = data.batterijen.filter(function (b) { return b.type === "stekker"; }).length;
    return { totaal: data.batterijen.length, stekker: s, vast: data.batterijen.length - s };
  }

  return { lijstHtml: lijstHtml, nietHtml: nietHtml, telling: telling, datum: datum, dagenOud: dagenOud, OUD_DAGEN: OUD_DAGEN, sorteer: sorteer, perKwh: perKwh };
});
