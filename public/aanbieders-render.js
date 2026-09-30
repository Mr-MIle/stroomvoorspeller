/* aanbieders-render.js — rekensom en HTML voor /aanbieders.
 *
 * Eén bron voor twee gebruikers:
 *   - de browser (window.AanbRender), die de lijst live bijwerkt;
 *   - build-aanbieders.js (module.exports), dat dezelfde HTML statisch in
 *     aanbieders.html bakt voor zoekmachines en bezoekers zonder JavaScript.
 * Wijzig je hier iets, draai dan `node build-aanbieders.js`.
 *
 * Data: public/data/config.json -> suppliers[]. Bedragen in config staan
 * zonder btw. Btw (21%) gaat over opslag en vaste kosten; terugleverbedragen
 * blijven zoals gepubliceerd.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.AanbRender = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var BTW = 1.21;
  var WEINIG_REVIEWS = 100;
  var VEROUDERD_DAGEN = 90;

  var HERKOMST = {
    aanbieder:   { teken: "",  kort: "van de aanbieder", lang: "Overgenomen van de website of prijscalculator van de aanbieder zelf." },
    tweede_bron: { teken: "◐", kort: "tweede bron",      lang: "De aanbieder publiceert dit niet openbaar; overgenomen uit een andere bron." },
    aanname:     { teken: "○", kort: "aanname",          lang: "Niet gevonden; we rekenen met een aangenomen waarde." },
    nakijken:    { teken: "⚠", kort: "nakijken",         lang: "Bronnen spreken elkaar tegen of de aanbieder noemt inmiddels iets anders." }
  };

  var KENMERK_LABEL = {
    ev:       { kort: "auto",          lang: "Stuurt je auto of laadpaal aan" },
    batterij: { kort: "batterij",      lang: "Stuurt je thuisbatterij aan" },
    kwartier: { kort: "kwartierprijs", lang: "Kwartierprijzen" },
    api:      { kort: "open API",      lang: "Officiële open API" }
  };

  // ── opmaak ───────────────────────────────────────────────────────────
  function esc(t) {
    return String(t == null ? "" : t)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function duizend(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, "."); }
  function eur0(v) { return "€ " + duizend(Math.round(Math.abs(v))); }
  function eur2(v) { return "€ " + v.toFixed(2).replace(".", ","); }
  function ct(v) { return (v * 100).toFixed(2).replace(".", ","); }
  function kwh(n) { return duizend(Math.round(n)) + " kWh"; }
  var MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus",
                 "september", "oktober", "november", "december"];
  function datumNL(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    return m ? parseInt(m[3], 10) + " " + MAANDEN[parseInt(m[2], 10) - 1] + " " + m[1] : "onbekend";
  }
  function dagenOud(iso, nu) {
    var t = Date.parse(iso || "");
    return isNaN(t) ? Infinity : ((nu || Date.now()) - t) / 864e5;
  }
  function bedragTekst(v) { return v < 0 ? "+ " + eur0(v) + " terug" : eur0(v); }

  // ── situatie ─────────────────────────────────────────────────────────
  // c = { afname, evKwh, terug, jaar, btw (bool), zon, ev, bat, api }
  function standaardSituatie(page) {
    return { afname: (page && page.default_verbruik_kwh) || 2900, evKwh: 0, terug: 0, jaar: 2026,
             btw: true, zon: false, ev: false, bat: false, api: false };
  }
  function netAfname(c) { return Math.max(0, c.afname || 0) + (c.ev ? Math.max(0, c.evKwh || 0) : 0); }

  // Leverancierskosten per jaar, per onderdeel.
  function kosten(s, c) {
    var f = c.btw ? BTW : 1;
    var v = netAfname(c);
    var t = c.zon ? Math.max(0, c.terug || 0) : 0;
    var opslag = s.markup_per_kwh * v * f;
    var vast = s.fixed_per_month * 12 * f;
    var tl = s.teruglevering || {};
    var o = tl.opslag_per_kwh || 0;
    var tlKwh = 0, terug = 0;
    if (t > 0 && o) {
      tlKwh = (tl.methode === "jaaroverschot" && c.jaar === 2026) ? Math.max(0, t - v) : t;
      terug = -o * tlKwh;   // negatief = scheelt je geld
    }
    return { opslag: opslag, vast: vast, terug: terug, tlKwh: tlKwh, afname: v, totaal: opslag + vast + terug };
  }

  // ── herkomst ─────────────────────────────────────────────────────────
  function verouderd(s) { return dagenOud(s.verified) > VEROUDERD_DAGEN; }
  function herkomstVan(s, veld) {
    if ((veld === "opslag" || veld === "vast") && verouderd(s)) return "nakijken";
    if (s.nakijken && (s.nakijken.veld === veld || (s.nakijken.veld === "tarief" && (veld === "opslag" || veld === "vast")))) return "nakijken";
    if (veld === "teruglevering") return s.teruglevering && s.teruglevering.voorlopig ? "aanname" : "aanbieder";
    return (s.herkomst && s.herkomst[veld]) || "aanbieder";
  }
  // In de lijst krijgt alleen de uitzondering een teken; in de details altijd.
  function teken(soort, opts) {
    opts = opts || {};
    var h = HERKOMST[soort];
    if (!h || (opts.lijst && !h.teken)) return "";
    var t = h.teken || "●";
    return '<span class="herk herk-' + soort + '" title="' + esc(h.lang) + '">' +
           '<span aria-hidden="true">' + t + "</span><span class=\"sr\"> (" + h.kort + ")</span></span>";
  }
  function isVoorlopig(s, c) {
    return herkomstVan(s, "opslag") !== "aanbieder" || herkomstVan(s, "vast") !== "aanbieder" ||
           (c.zon && c.terug > 0 && herkomstVan(s, "teruglevering") !== "aanbieder");
  }

  // ── teruglevering ────────────────────────────────────────────────────
  function terugKort(s) {
    var o = (s.teruglevering && s.teruglevering.opslag_per_kwh) || 0;
    if (!o) return "beursprijs";
    return "beurs " + (o > 0 ? "+ " : "− ") + ct(Math.abs(o)) + " ct";
  }
  function terugLang(s) {
    var t = s.teruglevering;
    if (!t) return "Niet bekend.";
    var o = t.opslag_per_kwh || 0, r;
    if (!o) r = "Je krijgt de kale beursprijs, zonder opslag en zonder inhouding.";
    else if (o > 0) r = "Je krijgt de kale beursprijs plus " + ct(o) + " ct per kWh.";
    else r = "Je krijgt de kale beursprijs min " + ct(-o) + " ct per kWh.";
    if (t.methode === "jaaroverschot") r += " In 2026 geldt dat alleen voor wat je meer teruglevert dan je afneemt; vanaf 2027 voor elke kWh.";
    if (t.bonus) {
      r += " Daarbovenop " + Math.round(t.bonus.pct * 100) + "% " + esc(t.bonus.naam) + " over zonnestroom (" + esc(t.bonus.venster) +
           (t.bonus.max_kwh ? ", tot " + duizend(t.bonus.max_kwh) + " kWh per jaar" : "") + ")" +
           (t.bonus.eis ? "; voorwaarde: " + esc(t.bonus.eis.charAt(0).toLowerCase() + t.bonus.eis.slice(1)) : "") +
           ". Geldt niet voor stroom uit een thuisbatterij.";
    }
    if (t.voorlopig) r += " Dit bedrag maakt de aanbieder niet openbaar; we rekenen met een aanname.";
    return r;
  }

  // ── kenmerken ────────────────────────────────────────────────────────
  function kenmerk(s, f) { return (s.kenmerken && s.kenmerken[f]) || { status: "onbekend" }; }
  function kenmerkWoord(k) {
    if (k.status === "ja") return "ja";
    if (k.status === "nee") return "nee";
    return k.noot ? "niet bekend" : "nog niet uitgezocht";
  }
  function kenmerkChip(s, f) {
    var k = kenmerk(s, f), cls = k.status === "ja" ? "k-ja" : k.status === "nee" ? "k-nee" : "k-onb";
    var extra = f === "kwartier" && k.status === "nee" ? " (per uur)" : "";
    return '<span class="kenm-item ' + cls + '">' + KENMERK_LABEL[f].kort + ": " + kenmerkWoord(k) + extra +
           (k.status !== "onbekend" ? teken(k.herkomst, { lijst: true }) : "") + "</span>";
  }
  function kenmerkZin(s, f) {
    var k = kenmerk(s, f), d = [];
    if (k.hoe) d.push(esc(k.hoe));
    if (k.voorwaarde) d.push(esc(k.voorwaarde));
    if (k.noot) d.push(esc(k.noot));
    var h = "<li><b>" + KENMERK_LABEL[f].lang + ":</b> " + kenmerkWoord(k) + (d.length ? ". " + hoofdletter(d.join(". ")) + "." : ".");
    if (k.bron) h += ' <span class="bron">' + teken(k.herkomst || "aanbieder") + " bron: " + esc(k.bron) + "</span>";
    return h + "</li>";
  }
  function hoofdletter(t) { return t ? t.charAt(0).toUpperCase() + t.slice(1) : t; }

  function scoreTekst(s, lang) {
    if (s.score == null) return "geen score";
    var sc = s.score.toFixed(1).replace(".", ",");
    if (!lang) return sc + (s.score_reviews != null && s.score_reviews < WEINIG_REVIEWS ? "*" : "");
    return sc + " van 5 op Trustpilot, " + duizend(s.score_reviews || 0) + " reviews" +
           (s.score_reviews < WEINIG_REVIEWS ? " (weinig reviews)" : "") + ", opgehaald " + datumNL(s.score_datum);
  }

  // ── één regel in de lijst ────────────────────────────────────────────
  function regelHtml(s, c, nr, opts) {
    opts = opts || {};
    var k = kosten(s, c), id = esc(s.id), f = c.btw ? BTW : 1;
    var open = opts.open === s.id, gekozen = opts.gekozen && opts.gekozen.indexOf(s.id) !== -1;
    var h = '<div class="aanb-rij' + (open ? " open" : "") + '" data-id="' + id + '" id="' + id + '">';
    h += '<div class="aanb-rij-kop">';
    h += '<button type="button" class="aanb-open" aria-expanded="' + (open ? "true" : "false") + '" aria-controls="det-' + id + '">';
    h += '<span class="aanb-naam"><span class="aanb-nr">' + nr + '.</span> ' + esc(s.name) + "</span>";
    h += '<span class="aanb-cellen">';
    h += '<span class="aanb-cel"><span class="aanb-lbl">opslag </span><b>' + ct(s.markup_per_kwh * f) + " ct</b>" + teken(herkomstVan(s, "opslag"), { lijst: true }) + "</span>";
    h += '<span class="aanb-cel"><span class="aanb-lbl">vast </span><b>' + (s.fixed_unconfirmed ? "≈ " : "") + eur2(s.fixed_per_month * f) + "</b>" + teken(herkomstVan(s, "vast"), { lijst: true }) + "</span>";
    h += '<span class="aanb-cel aanb-terug' + (c.zon ? "" : " leeg") + '"><span class="aanb-lbl">terug </span><b>' + terugKort(s) + "</b>" + teken(herkomstVan(s, "teruglevering"), { lijst: true }) + "</span>";
    h += '<span class="aanb-cel aanb-score"><span class="aanb-lbl">score </span>' + scoreTekst(s) + "</span>";
    h += "</span>";
    h += '<span class="aanb-bedrag">' + bedragTekst(k.totaal) + "<small>per jaar</small></span>";
    var km = "";
    if (c.ev) km += kenmerkChip(s, "ev");
    if (c.bat) km += kenmerkChip(s, "batterij") + kenmerkChip(s, "kwartier");
    if (c.api) km += kenmerkChip(s, "api") + (c.bat ? "" : kenmerkChip(s, "kwartier"));
    if (km) h += '<span class="aanb-kenm">' + km + "</span>";
    h += '<span class="aanb-caret" aria-hidden="true">▾</span>';
    h += "</button>";
    h += '<label class="aanb-vgl"><input type="checkbox" class="vergelijk-check" data-id="' + id + '"' + (gekozen ? " checked" : "") +
         '><span class="aanb-vgl-vak" aria-hidden="true"></span><span class="sr">Vergelijk ' + esc(s.name) + "</span></label>";
    h += "</div>";
    h += detailsHtml(s, c, k, open);
    h += "</div>";
    return h;
  }

  function detailsHtml(s, c, k, open) {
    var f = c.btw ? BTW : 1, btw = c.btw ? "inclusief btw" : "zonder btw";
    var h = '<div class="aanb-details" id="det-' + esc(s.id) + '"' + (open ? "" : " hidden") + ">";
    if (s.nakijken) h += '<p class="aanb-nakijken">' + teken("nakijken") + " " + esc(s.nakijken.tekst) + "</p>";
    if (verouderd(s)) h += '<p class="aanb-nakijken">' + teken("nakijken") + " Tarief laatst gecontroleerd op " + datumNL(s.verified) + "; mogelijk verouderd.</p>";
    if (s.let_op) h += '<p class="aanb-let">' + esc(s.let_op) + "</p>";
    if (s.omschrijving) h += '<p class="aanb-omschr">' + esc(s.omschrijving) + "</p>";

    h += '<h3 class="aanb-kopje">Kosten bij jouw situatie</h3><p class="aanb-som">';
    var evDeel = c.ev ? Math.max(0, c.evKwh || 0) : 0;
    h += "Opslag " + ct(s.markup_per_kwh * f) + " ct × " + kwh(k.afname) +
         (evDeel ? " (" + kwh(k.afname - evDeel) + " huis + " + kwh(evDeel) + " thuisladen)" : "") +
         " = " + eur0(k.opslag) + " " + teken(herkomstVan(s, "opslag")) + "<br>";
    h += "Vaste kosten " + eur2(s.fixed_per_month * f) + " × 12 = " + eur0(k.vast) + " " + teken(herkomstVan(s, "vast")) + "<br>";
    if (c.zon && c.terug > 0) {
      var o = (s.teruglevering && s.teruglevering.opslag_per_kwh) || 0;
      h += "Teruglevering: " + (k.terug <= 0 ? eur0(k.terug) + " eraf" : eur0(k.terug) + " erbij") +
           (o ? " (" + ct(Math.abs(o)) + " ct × " + kwh(k.tlKwh) + ")" : " (kale beursprijs)") + "<br>";
    }
    h += "<b>Samen " + bedragTekst(k.totaal) + " per jaar</b>, " + btw + " over opslag en vaste kosten.</p>";

    h += '<h3 class="aanb-kopje">Teruglevering</h3><p>' + terugLang(s) + " " + teken(herkomstVan(s, "teruglevering")) +
         (s.teruglevering && s.teruglevering.bron ? ' <span class="bron">bron: ' + esc(s.teruglevering.bron) + "</span>" : "") + "</p>";

    h += '<h3 class="aanb-kopje">Slim sturen</h3><ul class="aanb-kenmerken">' +
         kenmerkZin(s, "ev") + kenmerkZin(s, "batterij") + kenmerkZin(s, "kwartier") + kenmerkZin(s, "api") + "</ul>";
    h += '<p class="aanb-noot">Home Assistant kan bij elke aanbieder sturen op de beursprijs, want die is overal gelijk. Zie <a href="/home-assistant">Home Assistant</a>.</p>';

    var et = s.stroometiket || { status: "onbekend" };
    h += '<h3 class="aanb-kopje">Contract en stroom</h3><p>Opzeggen: ' + esc(s.opzeg || "onbekend") + ". Gas: " + (s.gas ? "ja" : "nee, alleen stroom") + ".<br>" +
         "Stroometiket: " + (et.status === "onbekend" ? "nog niet nagekeken." : esc(et.tekst) + ". " + teken(et.herkomst) + ' <span class="bron">bron: ' + esc(et.bron) + "</span>") + "</p>";

    h += '<h3 class="aanb-kopje">Klantervaring</h3><p>' + scoreTekst(s, true) + ". Wij controleren deze reviews niet.</p>";

    h += '<h3 class="aanb-kopje">Bron tarieven</h3><p>' + esc(s.source || "onbekend") + ". Gecontroleerd op " + datumNL(s.verified) + ".</p>";
    h += '<p class="aanb-link"><a href="' + esc(s.website || "#") + '" target="_blank" rel="noopener">Naar de website van ' + esc(s.name) + " ↗</a></p>";
    h += "</div>";
    return h;
  }

  // ── sorteren en filteren ─────────────────────────────────────────────
  function sorteer(lijst, sleutel, c) {
    var kopie = lijst.slice();
    kopie.sort(function (a, b) {
      switch (sleutel) {
        case "opslag": return a.markup_per_kwh - b.markup_per_kwh || a.name.localeCompare(b.name, "nl");
        case "vast":   return a.fixed_per_month - b.fixed_per_month || a.name.localeCompare(b.name, "nl");
        case "terug":
          return ((b.teruglevering && b.teruglevering.opslag_per_kwh) || 0) - ((a.teruglevering && a.teruglevering.opslag_per_kwh) || 0) ||
                 kosten(a, c).totaal - kosten(b, c).totaal;
        case "naam":   return a.name.localeCompare(b.name, "nl");
        default:       return kosten(a, c).totaal - kosten(b, c).totaal || a.name.localeCompare(b.name, "nl");
      }
    });
    return kopie;
  }
  // Filters: gas, ev, batterij, kwartier, api, etiket. "nee" valt af,
  // "onbekend" komt in een apart blok zodat niemand verdwijnt omdat wij iets nog niet weten.
  function filter(lijst, filters) {
    var ja = [], onbekend = [];
    lijst.forEach(function (s) {
      var ok = true, onb = false;
      filters.forEach(function (f) {
        if (f === "gas") { if (!s.gas) ok = false; return; }
        var st = f === "etiket" ? (s.stroometiket || {}).status : kenmerk(s, f).status;
        if (st === "nee") ok = false;
        else if (st !== "ja") onb = true;
      });
      if (ok && !onb) ja.push(s); else if (ok) onbekend.push(s);
    });
    return { ja: ja, onbekend: onbekend };
  }

  function lijstHtml(lijst, c, opts) {
    return lijst.map(function (s, i) { return regelHtml(s, c, (opts && opts.start || 0) + i + 1, opts); }).join("\n");
  }

  function verschilTekst(lijst, c) {
    if (lijst.length < 2) return "";
    var t = lijst.map(function (s) { return kosten(s, c).totaal; }).sort(function (a, b) { return a - b; });
    return "Bij jouw situatie scheelt het <strong>" + eur0(t[t.length - 1] - t[0]) + " per jaar</strong> tussen de goedkoopste en de duurste aanbieder. Bedragen " +
           (c.btw ? "inclusief" : "zonder") + " btw.";
  }

  // ── vergelijken ──────────────────────────────────────────────────────
  function vergelijkHtml(sel, c, alleenVerschillen) {
    var K = sel.map(function (s) { return kosten(s, c); });
    var f = c.btw ? BTW : 1, rijen = [];
    function r(groep, label, waarden, getallen, twijfel) { rijen.push({ g: groep, l: label, w: waarden, n: getallen, tw: twijfel }); }

    r("Kosten", "Per jaar bij jouw situatie", K.map(function (k) { return bedragTekst(k.totaal); }), K.map(function (k) { return k.totaal; }),
      sel.some(function (s) { return isVoorlopig(s, c); }));
    r("Kosten", "Opslag per kWh", sel.map(function (s) { return ct(s.markup_per_kwh * f) + " ct " + teken(herkomstVan(s, "opslag"), { lijst: true }); }),
      sel.map(function (s) { return s.markup_per_kwh; }), sel.some(function (s) { return herkomstVan(s, "opslag") !== "aanbieder"; }));
    r("Kosten", "Vaste kosten per maand", sel.map(function (s) { return (s.fixed_unconfirmed ? "≈ " : "") + eur2(s.fixed_per_month * f) + " " + teken(herkomstVan(s, "vast"), { lijst: true }); }),
      sel.map(function (s) { return s.fixed_per_month; }), sel.some(function (s) { return herkomstVan(s, "vast") !== "aanbieder"; }));
    r("Teruglevering", "Per kWh", sel.map(function (s) { return terugKort(s) + " " + teken(herkomstVan(s, "teruglevering"), { lijst: true }); }),
      sel.map(function (s) { return -((s.teruglevering && s.teruglevering.opslag_per_kwh) || 0); }),
      sel.some(function (s) { return herkomstVan(s, "teruglevering") !== "aanbieder"; }));
    r("Teruglevering", "Inhouding telt over", sel.map(function (s) {
      var t = s.teruglevering || {};
      return !t.opslag_per_kwh ? "n.v.t." : t.methode === "jaaroverschot" ? "2026: alleen jaaroverschot" : "elke kWh";
    }));
    r("Teruglevering", "Zonnebonus", sel.map(function (s) {
      var b = s.teruglevering && s.teruglevering.bonus;
      return b ? Math.round(b.pct * 100) + "% " + esc(b.naam) + ", niet voor batterijstroom" : "geen";
    }));
    ["ev", "batterij", "kwartier", "api"].forEach(function (fk) {
      r("Slim sturen", KENMERK_LABEL[fk].lang, sel.map(function (s) {
        var k = kenmerk(s, fk);
        return kenmerkWoord(k) + (k.hoe ? ": " + esc(k.hoe) : "") + (k.status !== "onbekend" ? " " + teken(k.herkomst, { lijst: true }) : "");
      }));
    });
    r("Contract en stroom", "Opzeggen", sel.map(function (s) { return esc(s.opzeg || "onbekend"); }));
    r("Contract en stroom", "Levert ook gas", sel.map(function (s) { return s.gas ? "ja" : "nee"; }));
    r("Contract en stroom", "Stroometiket 100% hernieuwbaar", sel.map(function (s) {
      var e = s.stroometiket || {}; return e.status === "ja" ? "ja" : e.status === "nee" ? "nee" : "nog niet nagekeken";
    }));
    r("Klantervaring", "Trustpilot", sel.map(function (s) {
      return s.score == null ? "—" : s.score.toFixed(1).replace(".", ",") + " (" + duizend(s.score_reviews || 0) + " reviews)";
    }));

    var volg = K.map(function (k, i) { return i; }).sort(function (a, b) { return K[a].totaal - K[b].totaal; });
    var a = volg[0], z = volg[volg.length - 1], verschil = K[z].totaal - K[a].totaal;
    var zin;
    if (verschil < 1) {
      zin = "Bij jouw situatie kosten deze aanbieders per jaar vrijwel hetzelfde. Kijk naar de andere verschillen hieronder.";
    } else {
      var delen = [["opslag", "de opslag"], ["vast", "de vaste kosten"], ["terug", "de teruglevering"]].map(function (p) {
        return [Math.abs(K[z][p[0]] - K[a][p[0]]), p[1]];
      }).sort(function (x, y) { return y[0] - x[0]; });
      zin = "Bij jouw situatie is <b>" + esc(sel[a].name) + " " + eur0(verschil) + " per jaar goedkoper</b> dan " + esc(sel[z].name) +
            ". Het grootste verschil zit in " + delen[0][1] + ".";
    }
    if (rijen[0].tw) zin += " Let op: een van de bedragen rust op een tweede bron of aanname.";

    var h = '<p class="vgl-samenvatting">' + zin + "</p>";
    h += '<table class="vgl-tabel"><thead><tr><th scope="col"><span class="sr">Eigenschap</span></th>';
    sel.forEach(function (s) { h += '<th scope="col">' + esc(s.name) + "</th>"; });
    h += "</tr></thead><tbody>";
    var laatste = "", getoond = 0;
    rijen.forEach(function (rw) {
      var kaal = rw.w.map(function (w) { return String(w).replace(/<[^>]+>/g, "").trim(); });
      var gelijk = kaal.every(function (w) { return w === kaal[0]; });
      if (alleenVerschillen && gelijk) return;
      getoond++;
      if (rw.g !== laatste) { h += '<tr class="vgl-groep"><th scope="colgroup" colspan="' + (sel.length + 1) + '">' + rw.g + "</th></tr>"; laatste = rw.g; }
      var best = -1;
      if (rw.n && !rw.tw && !gelijk) { best = 0; for (var i = 1; i < rw.n.length; i++) if (rw.n[i] < rw.n[best]) best = i; }
      h += '<tr><th scope="row">' + rw.l + "</th>";
      rw.w.forEach(function (w, i) {
        h += "<td" + (i === best ? ' class="vgl-laagst"' : "") + ">" + w + (i === best ? '<span class="sr"> (laagste)</span>' : "") + "</td>";
      });
      h += "</tr>";
    });
    if (!getoond) h += '<tr><td colspan="' + (sel.length + 1) + '">Geen verschillen gevonden.</td></tr>';
    h += "</tbody></table>";
    h += '<p class="vgl-noot">Groen = laagste van deze selectie, alleen als geen van de bedragen op een tweede bron of aanname rust. Bedragen ' +
         (c.btw ? "inclusief" : "zonder") + " btw over opslag en vaste kosten.</p>";
    return h;
  }

  return {
    BTW: BTW, HERKOMST: HERKOMST, KENMERK_LABEL: KENMERK_LABEL, VEROUDERD_DAGEN: VEROUDERD_DAGEN,
    esc: esc, duizend: duizend, eur0: eur0, eur2: eur2, ct: ct, datumNL: datumNL, dagenOud: dagenOud, bedragTekst: bedragTekst,
    standaardSituatie: standaardSituatie, netAfname: netAfname, kosten: kosten,
    herkomstVan: herkomstVan, verouderd: verouderd, teken: teken, isVoorlopig: isVoorlopig,
    terugKort: terugKort, terugLang: terugLang, kenmerk: kenmerk,
    regelHtml: regelHtml, lijstHtml: lijstHtml, sorteer: sorteer, filter: filter,
    verschilTekst: verschilTekst, vergelijkHtml: vergelijkHtml
  };
});
