/* vergelijk-vier.js — rekenblok en tabel van /kennisbank/dynamisch-contract-vergelijken.
 *
 * Eén bron voor twee gebruikers, net als aanbieders-render.js:
 *   - de browser, die de tabel bijwerkt als je je verbruik invult;
 *   - build-aanbieders.js, dat dezelfde regels statisch in het artikel bakt.
 * De rekensom is AanbRender.kosten(), dus dezelfde als op /aanbieders.
 * Gegevens: het JSON-blok #vier-data in het artikel (gebakken uit config.json).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(require("./aanbieders-render.js"));
  else root.VergelijkVier = factory(root.AanbRender);
})(typeof self !== "undefined" ? self : this, function (R) {
  "use strict";

  var OPSLAG = "sv.aanbieders";   // zelfde sleutel als /aanbieders, zodat je situatie meegaat

  function standaard() {
    return { afname: 3500, zon: false, terug: 2000, jaar: 2027, btw: true, evKwh: 0 };
  }
  function gesorteerd(lijst, c) {
    return lijst.map(function (s) { return { s: s, k: R.kosten(s, c) }; })
      .sort(function (a, b) { return a.k.totaal - b.k.totaal; });
  }
  function rijHtml(x) {
    var s = x.s;
    return '<tr><th scope="row">' + R.esc(s.name) + '</th>' +
      '<td class="vier-bedrag"><b>' + R.bedragTekst(x.k.totaal) + '</b></td>' +
      '<td class="vier-sub">opslag ' + R.ct(s.markup_per_kwh * R.BTW) + ' ct · vast ' + R.eur2(s.fixed_per_month * R.BTW) + '/mnd</td>' +
      '<td class="vier-sub"><span class="vier-m">teruglevering: </span>' + R.terugKort(s) + '</td>' +
      '<td class="vier-sub"><span class="vier-m">Home Assistant: </span>' + R.esc(s.ha) + '</td></tr>';
  }
  function rijenHtml(lijst, c) {
    return gesorteerd(lijst, c).map(rijHtml).join("\n");
  }
  function verschilZin(lijst, c) {
    var l = gesorteerd(lijst, c), a = l[0], z = l[l.length - 1];
    var sit = "Bij " + R.duizend(c.afname) + " kWh" +
      (c.zon ? " en " + R.duizend(c.terug) + " kWh teruglevering in " + c.jaar : " en geen zonnepanelen");
    return sit + ": " + a.s.name + " is het goedkoopst, " + R.eur0(z.k.totaal - a.k.totaal) + " per jaar minder dan " + z.s.name + ".";
  }

  function start(doc) {
    var $ = function (id) { return doc.getElementById(id); };
    var form = $("vier-reken"), data = $("vier-data");
    if (!form || !data) return;
    var lijst = JSON.parse(data.textContent);
    form.hidden = false;

    function situatie() {
      var c = standaard(), j = form.querySelector("input[name=vier-jaar]:checked");
      c.afname = Math.max(0, parseInt($("vier-afname").value, 10) || 0);
      c.zon = $("vier-zon").checked;
      c.terug = Math.max(0, parseInt($("vier-terug").value, 10) || 0);
      c.jaar = j ? parseInt(j.value, 10) : 2027;
      return c;
    }
    function bewaar(c) {
      try {
        var s = JSON.parse(localStorage.getItem(OPSLAG) || "null");
        if (!s || typeof s !== "object") s = {};
        s.afname = c.afname; s.zon = c.zon; s.terug = c.terug; s.jaar = c.jaar;
        localStorage.setItem(OPSLAG, JSON.stringify(s));
      } catch (e) { /* geen opslag: niets aan de hand */ }
    }
    function render() {
      var c = situatie();
      $("vier-zonvelden").hidden = !c.zon;
      $("vier-rijen").innerHTML = rijenHtml(lijst, c);
      $("vier-verschil").textContent = verschilZin(lijst, c);
      return c;
    }
    // Pas opslaan als je zelf iets invult, anders zet alleen het lezen van
    // dit artikel je situatie op /aanbieders vast.
    function wijzig() { bewaar(render()); }
    form.addEventListener("input", wijzig);
    form.addEventListener("change", wijzig);
    form.addEventListener("submit", function (e) { e.preventDefault(); });
    render();
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { start(document); });
    else start(document);
  }

  return { standaard: standaard, gesorteerd: gesorteerd, rijenHtml: rijenHtml, verschilZin: verschilZin };
});
