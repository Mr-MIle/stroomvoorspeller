/* ere-render.js — rekensom en HTML voor /ere-vergelijken.
 *
 * Eén bron voor twee gebruikers, net als aanbieders-render.js:
 *   - de browser (window.EreRender), die de lijst live bijwerkt;
 *   - build-ere.js (module.exports), dat dezelfde HTML statisch in
 *     ere-vergelijken.html bakt voor zoekmachines en bezoekers zonder JavaScript.
 * Wijzig je hier iets, draai dan `node build-ere.js` en verhoog de ?v= van
 * dit script in ere-vergelijken.html.
 *
 * Data: public/data/ere.json. Netto per kWh = ERE-prijs × ere_per_kwh ×
 * (1 − commissie), met een bodem als de inboeker een bedrag per kWh noemt.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.EreRender = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var TOP = 3;

  // Beeldmerken in /logos, van de eigen site van de inboeker (apple-touch-icon,
  // favicon of logo, geschaald naar 180 px). Zonder bestand toont de lijst een letter;
  // Lekkerladen heeft er nog geen (lekkerladen.nl had op 15 nov 2026 een ongeldig certificaat).
  var LOGO = {
    zeres: "zeres.png", laadbeloning: "laadbeloning.png", stekker: "stekker.png", joulo: "joulo.png", laadloon: "laadloon.png",
    zonneplan: "zonneplan.png", "50five": "50five.svg", vattenfall: "vattenfall.svg", eneco: "eneco.png"
  };

  // ── opmaak-helpers ──────────────────────────────────────────────────
  function esc(t) {
    return String(t == null ? "" : t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function pct(v) {
    var x = Math.round(v * 1000) / 10;
    return (x % 1 ? komma(x, 1) : String(x)) + "%";
  }
  function ct(v) { return (v * 100).toFixed(1).replace(".", ",") + " ct"; }
  function getal(v) { return String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, "."); }
  function eur0(v) { return "€ " + getal(v); }
  function komma(v, n) { return v.toFixed(n).replace(".", ","); }
  function namen(l) { return l.length < 2 ? l.join("") : l.slice(0, -1).join(", ") + " en " + l[l.length - 1]; }
  function hoofdletter(t) { return t ? t.charAt(0).toUpperCase() + t.slice(1) : t; }

  var MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"];
  function datumNL(iso) {
    var p = String(iso).split("-");
    return p.length === 3 ? parseInt(p[2], 10) + " " + MAANDEN[parseInt(p[1], 10) - 1] + " " + p[0] : String(iso);
  }

  // Tarief ouder dan dit aantal dagen: waarschuwing, net als op /aanbieders.
  var VEROUDERD_DAGEN = 90;
  function dagenOud(iso, nu) {
    var t = Date.parse(iso || "");
    return isNaN(t) ? Infinity : ((nu || Date.now()) - t) / 864e5;
  }
  function verouderd(inb, nu) { return dagenOud(inb.gecontroleerd, nu) > VEROUDERD_DAGEN; }

  // ── rekensom ────────────────────────────────────────────────────────
  function marktPrijs(d) {
    var r = d.markt.reeks;
    return r.length ? r[r.length - 1].ere_prijs : d.markt.prijs_nu;
  }
  function marktNetto(d, inb) {
    return inb.commissie == null ? null : marktPrijs(d) * d.markt.ere_per_kwh * (1 - inb.commissie);
  }
  function netto(d, inb) {
    var n = marktNetto(d, inb);
    if (n == null) return null;
    if (inb.garantie_per_kwh != null && inb.garantie_per_kwh > n) n = inb.garantie_per_kwh;
    return n;
  }
  function perJaar(d, inb, v) {
    var n = netto(d, inb);
    return n == null ? null : n * v - (inb.vaste_kosten || 0);
  }
  // Het bedrag rust op een aangekondigd (niet vastgelegd) bedrag per kWh.
  function opAankondiging(d, inb) {
    var m = marktNetto(d, inb);
    return !!inb.garantie_label && inb.garantie_per_kwh != null && m != null && inb.garantie_per_kwh > m;
  }

  function filter(lijst, keuze) {
    if (keuze === "vrij") return lijst.filter(function (i) { return !i.eis_klant; });
    if (keuze === "betaald") return lijst.filter(function (i) { return i.uitbetaald && i.uitbetaald.length; });
    return lijst.slice();
  }
  function sorteer(d, lijst, v) {
    return lijst.slice().sort(function (a, b) {
      var x = perJaar(d, a, v), y = perJaar(d, b, v);
      if (x == null) return y == null ? 0 : 1;
      if (y == null) return -1;
      return y - x;
    });
  }

  // Nog niet erkend als inboekdienstverlener door de NEa.
  function nietErkend(inb) { return inb.erkenning === "aangevraagd"; }
  function erkenningUitleg(inb) {
    var n = esc(inb.naam);
    return "Wie laadstroom wil laten inboeken, heeft een inboekdienstverlener nodig die de Nederlandse Emissieautoriteit (NEa) daarvoor heeft erkend. " +
      n + " heeft die erkenning aangevraagd, maar heeft haar nog niet. Zolang dat zo is, kan " + n + " niets voor je inboeken, en dus ook geen ERE's verkopen of uitbetalen. " +
      "Aanmelden en de machtiging tekenen kan al wel. Wordt de aanvraag afgewezen of duurt ze lang, dan moet je alsnog naar een andere inboeker. " +
      "De opbrengst hierboven is een rekensom met de servicekosten van " + n + ", geen bedrag dat al is waargemaakt.";
  }

  // ── kenmerken (rondjes) ─────────────────────────────────────────────
  function badges(inb) {
    var b = "";
    if (nietErkend(inb)) b += '<span class="ere-badge badge-aangekondigd" title="Aanvraag bij de NEa loopt nog">nog niet erkend door de NEa</span>';
    if (inb.garantie_per_kwh != null) b += (inb.garantie_label ? '<span class="ere-badge badge-aangekondigd">' + esc(inb.garantie_label) + " " : '<span class="ere-badge badge-garantie">bodem ') + ct(inb.garantie_per_kwh) + "</span>";
    if (inb.uitbetaald && inb.uitbetaald.length) b += '<span class="ere-badge badge-betaald">uitbetaald</span>';
    if (inb.eis_klant) b += '<span class="ere-badge badge-klant">alleen klanten</span>';
    if (inb.disclosure) b += '<span class="ere-badge badge-mij">ik ben hier klant</span>';
    if (verouderd(inb)) b += '<span class="ere-badge badge-aangekondigd" title="Laatst gecontroleerd op ' + esc(datumNL(inb.gecontroleerd)) + '">⚠ mogelijk verouderd</span>';
    return b;
  }

  function logoHtml(inb, maat) {
    if (LOGO[inb.id]) return '<img class="ere-logo" src="/logos/' + LOGO[inb.id] + '" alt="" width="' + maat + '" height="' + maat + '" loading="lazy" decoding="async">';
    return '<span class="ere-logo ere-logo-letter" aria-hidden="true">' + esc((inb.naam || "?").charAt(0).toUpperCase()) + "</span>";
  }

  // ── details (uitklap) ───────────────────────────────────────────────
  function detailsHtml(inb) {
    var id = esc(inb.id);
    var h = '<div class="ere-details" id="ere-det-' + id + '" hidden>';
    if (verouderd(inb)) h += '<p class="ere-let">⚠ Laatst gecontroleerd op ' + esc(datumNL(inb.gecontroleerd)) + ', meer dan ' + VEROUDERD_DAGEN + ' dagen geleden. Servicekosten en bedragen kunnen inmiddels anders zijn; kijk ze na op de website van ' + esc(inb.naam) + '.</p>';
    if (nietErkend(inb)) h += '<p class="ere-label">Nog niet erkend: wat betekent dat?</p><p class="ere-let">' + erkenningUitleg(inb) + "</p>";
    h += '<p class="ere-label" id="ere-det-' + id + '-kosten">Servicekosten</p><p>' + esc(inb.commissie_tekst) + (inb.commissie_detail ? " " + esc(inb.commissie_detail) : "") + "</p>";
    h += '<p class="ere-label" id="ere-det-' + id + '-uitbetaling">Uitbetaling</p><p>' + esc(hoofdletter(inb.uitbetaling)) + "." + (inb.uitbetaling_detail ? " " + esc(inb.uitbetaling_detail) : "") + "</p>";
    if (inb.uitbetaald && inb.uitbetaald.length) {
      var lijst = inb.uitbetaald.map(function (u) { return esc(u.periode) + ": " + ct(u.netto_per_kwh); }).join(" &middot; ");
      h += '<p class="ere-label">Werkelijk uitbetaald</p><p>' + lijst + "</p>";
    }
    h += '<p class="ere-label" id="ere-det-' + id + '-voorwaarden">Voorwaarden</p><p>' + esc(inb.contract) + (inb.eis_klant ? " " + esc(inb.eis_klant) : "") + "</p>";
    h += '<p class="ere-label" id="ere-det-' + id + '-laadpaal">Laadpaal</p><p>' + esc(inb.laadpalen) + "</p>";
    if (inb.sterk) h += '<p class="ere-label">Sterk punt</p><p>' + esc(inb.sterk) + "</p>";
    if (inb.let_op) h += '<p class="ere-label">Let op</p><p class="ere-let">' + esc(inb.let_op) + "</p>";
    if (inb.disclosure) h += '<p class="ere-disclosure">' + esc(inb.disclosure) + "</p>";
    h += '<p class="ere-bronregel">Gecontroleerd op ' + esc(datumNL(inb.gecontroleerd)) + ' &middot; <a href="' + esc(inb.bron) + '" target="_blank" rel="noopener">bron ↗</a> &middot; <a href="' + esc(inb.url) + '" target="_blank" rel="noopener">naar de website ↗</a></p>';
    h += "</div>";
    return h;
  }

  // ── één inboeker: kaart (top 3) of regel ────────────────────────────
  function kaartHtml(d, inb, v, nr, eerste, tweede) {
    var id = esc(inb.id), n = netto(d, inb), j = perJaar(d, inb, v);
    var badge = "", soort = "";
    if (nr === 1 && j != null) {
      if (nietErkend(inb)) { badge = "Hoogste bedrag, nog niet erkend door de NEa"; soort = " met-badge badge-voorzichtig"; }
      else if (opAankondiging(d, inb)) { badge = "Hoogste bedrag, nog niet vastgelegd"; soort = " met-badge badge-voorzichtig"; }
      else { badge = "Hoogste opbrengst bij jouw laadgedrag"; soort = " met-badge"; }
    }
    var h = '<div class="ere-rij ere-kaart' + soort + '" data-id="' + id + '" id="' + id + '">';
    if (badge) h += '<span class="ere-kbadge">' + esc(badge) + "</span>";
    h += '<button class="ere-open" type="button" aria-expanded="false" aria-controls="ere-det-' + id + '">';
    h += '<span class="ere-k-logo"><span class="ere-k-nr" aria-hidden="true">' + nr + "</span>" + logoHtml(inb, 44) + "</span>";
    h += '<span class="ere-naam"><span class="sr">' + nr + ". </span>" + esc(inb.naam) + "</span>";
    h += '<span class="ere-cellen">';
    h += '<span class="ere-cel">servicekosten <b>' + (inb.commissie == null ? "niet openbaar" : pct(inb.commissie)) + "</b>" +
      (inb.commissie_daarna != null ? " in jaar 1, daarna <b>" + pct(inb.commissie_daarna) + "</b>" : "") + "</span>";
    h += '<span class="ere-cel">netto <b>' + (n == null ? "–" : ct(n)) + "</b> per kWh</span>";
    h += '<span class="ere-cel">uitbetaling <b>' + esc(inb.uitbetaling) + "</b></span>";
    h += "</span>";
    var b = badges(inb);
    if (b) h += '<span class="ere-kenm">' + b + "</span>";
    var delta = "";
    if (j != null && eerste != null) {
      if (nr === 1 && tweede != null && j - tweede >= 0.5) delta = '<small class="ere-delta goed">' + eur0(j - tweede) + " meer dan nr. 2</small>";
      else if (nr > 1 && eerste - j >= 0.5) delta = '<small class="ere-delta">' + eur0(eerste - j) + " minder dan nr. 1</small>";
      else if (nr > 1) delta = '<small class="ere-delta">gelijk aan nr. 1</small>';
    }
    h += '<span class="ere-bedrag"><small>opbrengst</small><span class="ere-bedrag-getal">' + (j == null ? "–" : eur0(j)) + "</span><small>per jaar</small>" + delta + "</span>";
    h += "</button>";
    var snel = [["kosten", "Servicekosten"], ["uitbetaling", "Uitbetaling"], ["voorwaarden", "Voorwaarden"], ["laadpaal", "Laadpaal"]];
    h += '<nav class="ere-snel" aria-label="Details van ' + esc(inb.naam) + '">' + snel.map(function (p) {
      return '<a href="#ere-det-' + id + "-" + p[0] + '" data-id="' + id + '">' + p[1] + "</a>";
    }).join("") + "</nav>";
    h += detailsHtml(inb);
    h += "</div>";
    return h;
  }

  function regelHtml(d, inb, v, nr) {
    var id = esc(inb.id), n = netto(d, inb), j = perJaar(d, inb, v);
    var sub = (inb.commissie == null ? "servicekosten niet openbaar" : inb.commissie_tekst) + " · " + inb.uitbetaling;
    var b = badges(inb);
    var h = '<div class="ere-rij" data-id="' + id + '" id="' + id + '">';
    h += '<button class="ere-open" type="button" aria-expanded="false" aria-controls="ere-det-' + id + '">';
    h += '<span class="ere-naam"><span class="ere-nr">' + nr + ".</span> " + logoHtml(inb, 32) + '<span class="ere-naam-tekst">' + esc(inb.naam) +
         (b ? '<span class="ere-badges">' + b + "</span>" : "") + '<span class="ere-sub">' + esc(sub) + "</span></span></span>";
    h += '<span class="ere-cel"><span class="ere-cel-label">netto/kWh</span>' + (n == null ? "–" : ct(n)) + "</span>";
    h += '<span class="ere-cel ere-jaar"><span class="ere-cel-label">per jaar</span><strong>' + (j == null ? "–" : eur0(j)) + "</strong></span>";
    h += '<span class="ere-caret" aria-hidden="true">▾</span></button>';
    h += detailsHtml(inb);
    h += "</div>";
    return h;
  }

  // Top 3 als kaart, de rest als regels in één lijst.
  function lijstHtml(d, v, keuze) {
    var lijst = sorteer(d, filter(d.inboekers, keuze), v);
    if (!lijst.length) return '<p class="ere-leeg">Geen inboekers bij deze keuze.</p>';
    var bedragen = lijst.map(function (i) { return perJaar(d, i, v); });
    var top = [], rest = [];
    lijst.forEach(function (i, k) { (k < TOP && bedragen[k] != null ? top : rest).push(k); });
    var h = "";
    if (top.length) {
      h += '<div class="ere-top">' + top.map(function (k) {
        return kaartHtml(d, lijst[k], v, k + 1, bedragen[0], bedragen.length > 1 ? bedragen[1] : null);
      }).join("\n") + "</div>";
    }
    if (rest.length) {
      h += '<div class="ere-lijst"><div class="ere-koprij" aria-hidden="true"><span>Inboeker</span><span class="r">Netto per kWh</span><span class="r">Per jaar</span><span></span></div>' +
           rest.map(function (k) { return regelHtml(d, lijst[k], v, k + 1); }).join("\n") + "</div>";
    }
    return h;
  }

  // ── teksten rond de lijst ───────────────────────────────────────────
  function standTekst(d) {
    var r = d.markt.reeks[d.markt.reeks.length - 1];
    return "Gerekend met € " + komma(marktPrijs(d), 4) + " per ERE (" + esc(r.periode.replace("-", " ")) +
      (r.verwacht ? ", verwacht" : ", verkocht") + "). Bijgewerkt op " + esc(datumNL(d.bijgewerkt)) + ".";
  }

  function kortStand(d, v) {
    var met = sorteer(d, d.inboekers.filter(function (i) { return perJaar(d, i, v) != null; }), v);
    var metPct = d.inboekers.filter(function (i) { return i.commissie != null; });
    var laag = metPct.reduce(function (m, i) { return i.commissie < m.commissie ? i : m; }, metPct[0]);
    var betaald = d.inboekers.filter(function (i) { return i.uitbetaald && i.uitbetaald.length; });
    var bedragen = [].concat.apply([], betaald.map(function (i) { return i.uitbetaald.map(function (u) { return u.netto_per_kwh; }); }));
    return { met: met, top: met[0], laatste: met[met.length - 1], laag: laag, betaald: betaald,
      min: Math.min.apply(null, bedragen), max: Math.max.apply(null, bedragen) };
  }

  // Eén zin tussen de invoer en de lijst.
  function verschilHtml(d, v) {
    var k = kortStand(d, v);
    return "Bij " + getal(v) + " kWh thuisladen scheelt het <strong>" + eur0(perJaar(d, k.top, v) - perJaar(d, k.laatste, v)) +
      " per jaar</strong> tussen de hoogste en de laagste opbrengst. Of je laadpaal meedoet, maakt meer uit: " +
      '<a href="#ere-checks">3 dingen om eerst te checken ↓</a>';
  }

  function kortHtml(d, v) {
    var k = kortStand(d, v), vast = d.kort || {};
    function noot(sleutel, inb) { return vast[sleutel] && vast[sleutel].id === inb.id ? " " + esc(vast[sleutel].noot) : ""; }
    var h = "<p><strong>Kort antwoord</strong> bij " + getal(v) + " kWh per jaar:</p><ul>";
    h += "<li><strong>Hoogste bedrag:</strong> " + esc(k.top.naam) + ", " + eur0(perJaar(d, k.top, v)) + "." + noot("hoogste", k.top) + "</li>";
    h += "<li><strong>Laagste servicekosten:</strong> " + esc(k.laag.naam) + ", " + pct(k.laag.commissie).replace("%", " procent") + "." + noot("laagste_commissie", k.laag) + "</li>";
    if (k.betaald.length) {
      h += "<li><strong>Al uitbetaald:</strong> " + esc(namen(k.betaald.map(function (i) { return i.naam; }))) + ", netto " +
        (k.min === k.max ? komma(k.min * 100, 1) : komma(k.min * 100, 1) + " tot " + komma(k.max * 100, 1)) + " cent per kWh.</li>";
    }
    h += "</ul>";
    return h;
  }

  function hintTekst(d, v) {
    var km = Math.round(v * d.aannames.km_per_kwh / 500) * 500;
    return "Dat is ongeveer " + getal(km) + " km op thuisstroom. Weet je alleen je kilometers? Deel ze door " +
      d.aannames.km_per_kwh + ". Het precieze getal staat in de app van je laadpaal.";
  }

  function marktHtml(d) {
    return d.markt.reeks.map(function (r) {
      return "        <tr" + (r.verwacht ? ' class="ere-verwacht"' : "") + "><td>" + esc(r.periode) + '</td><td class="num">€ ' +
        komma(r.ere_prijs, 4) + '</td><td class="num">' + ct(r.ere_prijs * d.markt.ere_per_kwh) + "</td><td>" + esc(r.toelichting) + "</td></tr>";
    }).join("\n");
  }

  function betaaldHtml(d) {
    return d.inboekers.filter(function (i) { return i.uitbetaald && i.uitbetaald.length; }).map(function (i) {
      function vind(p) {
        var u = i.uitbetaald.filter(function (x) { return x.periode === p; })[0];
        return u ? ct(u.netto_per_kwh) : "–";
      }
      return "        <tr><td>" + esc(i.naam) + '</td><td class="num">' + vind("2026-Q1") + '</td><td class="num">' + vind("2026-Q2") +
        '</td><td><a href="' + esc(i.uitbetaald[0].bron) + '" target="_blank" rel="noopener">melding ↗</a></td></tr>';
    }).join("\n");
  }

  function nietHtml(d) {
    return "<ul>" + (d.niet_opgenomen || []).map(function (n) {
      return "<li><strong>" + esc(n.naam) + "</strong> — " + esc(n.reden) + "</li>";
    }).join("") + "</ul>";
  }

  function voetTekst(d) {
    return "Bijgewerkt op " + datumNL(d.bijgewerkt) + ", volgende controleronde " + datumNL(d.volgende_controle) +
      ". Gerekend met € " + komma(marktPrijs(d), 4) + " per ERE.";
  }

  return {
    esc: esc, ct: ct, eur0: eur0, getal: getal, komma: komma, datumNL: datumNL,
    VEROUDERD_DAGEN: VEROUDERD_DAGEN, dagenOud: dagenOud, verouderd: verouderd,
    pct: pct, nietErkend: nietErkend,
    marktPrijs: marktPrijs, netto: netto, perJaar: perJaar, opAankondiging: opAankondiging,
    filter: filter, sorteer: sorteer, lijstHtml: lijstHtml,
    standTekst: standTekst, kortStand: kortStand, verschilHtml: verschilHtml, kortHtml: kortHtml, hintTekst: hintTekst,
    marktHtml: marktHtml, betaaldHtml: betaaldHtml, nietHtml: nietHtml, voetTekst: voetTekst
  };
});
