/* aanbieders.js — bediening van /aanbieders.
 * Rekenen en HTML staan in aanbieders-render.js (gedeeld met build-aanbieders.js).
 * Zonder JavaScript blijft de statisch ingebakken lijst staan.
 */
(function () {
  "use strict";
  var R = window.AanbRender;
  if (!R) return;
  var $ = function (id) { return document.getElementById(id); };

  var grid = $("aanbieder-grid"), countEl = $("aanb-count"), verschilEl = $("aanb-verschil");
  var sortEl = $("sort"), btwEl = $("btw");
  var velden = { afname: $("afname"), terug: $("terug"), evKwh: $("evkwh") };
  var chips = { zon: $("c-zon"), ev: $("c-ev"), bat: $("c-bat"), api: $("c-api") };
  var filterEls = Array.prototype.slice.call(document.querySelectorAll("[data-filter]"));
  var jaarEls = Array.prototype.slice.call(document.querySelectorAll('input[name="rekenjaar"]'));
  var vbalk = $("aanb-vbalk"), vnamen = $("aanb-vbalk-namen"), vopen = $("vergelijk-open"), vwis = $("vergelijk-wis");
  var vglDialog = $("vgl-dialog"), fltDialog = $("flt-dialog"), vglBody = $("vgl-body"), alleenV = $("alleen-verschillen");

  var aanbieders = [];
  var batData = null, batLaden = null;   // data/batterij-effect.json, pas geladen als het nodig is
  var batKwhEl = $("bat-kwh");
  var batAlEls = Array.prototype.slice.call(document.querySelectorAll('input[name="bat-al"]'));
  var state = { gekozen: [], open: null, toonOnbekend: false };
  var terugNaar = null;

  function max() { return window.matchMedia("(max-width: 759px)").matches ? 2 : 3; }
  function getal(el, standaard) {
    var v = parseInt(el.value, 10);
    return isNaN(v) || v < 0 ? standaard : Math.min(v, 100000);
  }
  function situatie() {
    var jaar = 2026;
    jaarEls.forEach(function (el) { if (el.checked) jaar = parseInt(el.value, 10); });
    var c = {
      afname: getal(velden.afname, 0), terug: getal(velden.terug, 0), evKwh: getal(velden.evKwh, 0), jaar: jaar,
      btw: btwEl.checked, zon: chips.zon.checked, ev: chips.ev.checked, bat: chips.bat.checked, api: chips.api.checked
    };
    c.batNieuw = c.bat && batAlEls.some(function (el) { return el.checked && el.value === "nee"; });
    c.batEffect = c.batNieuw ? batterijEffect(c) : null;
    return c;
  }

  // Nieuwe batterij: kies het rekenscenario dat het dichtst bij je teruglevering ligt
  // (zonder zonnepanelen: het scenario zonder panelen) en neem de verschuiving in kWh over.
  function batterijEffect(c) {
    if (!batData) { laadBatData(); return null; }
    var sc = batData.scenarios.filter(function (s) { return c.zon && c.terug > 0 ? s.panelen > 0 : s.panelen === 0; });
    if (!sc.length) return null;
    if (c.zon && c.terug > 0) sc.sort(function (a, b) { return Math.abs(a.terug - c.terug) - Math.abs(b.terug - c.terug); });
    var s = sc[0], g = s.groottes[batKwhEl.value];
    if (!g) return null;
    return { kwh: parseInt(batKwhEl.value, 10), panelen: s.panelen, afname: g.afname, terug: g.terug,
             besparing: g.besparing[c.zon ? c.jaar : 2027], prijsVoor: s.prijs_ct, prijsNa: g.prijs_ct };
  }
  function laadBatData() {
    if (batLaden) return;
    batLaden = fetch("/data/batterij-effect.json", { cache: "no-cache" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { batData = d; render(); })
      .catch(function () { batData = null; });
  }
  function batRegel(c) {
    var el = $("aanb-batregel"), e = c.batEffect;
    if (!e) { el.hidden = true; return; }
    var basis = e.panelen ? "ongeveer " + e.panelen + " zonnepanelen" : "een huis zonder zonnepanelen";
    function maandJaar(ym) { return R.datumNL(ym + "-01").replace(/^1 /, ""); }
    el.innerHTML = "Een nieuwe batterij van " + e.kwh + " kWh verlaagt daarnaast je hele stroomrekening met <strong>± " + R.eur0(e.besparing) + " per jaar</strong>" +
      (c.zon ? " (" + (c.jaar === 2026 ? "2026, met saldering" : "vanaf 2027") + ")" : "") +
      ": je betaalt gemiddeld " + String(e.prijsNa).replace(".", ",") + " in plaats van " + String(e.prijsVoor).replace(".", ",") +
      " cent per kWh van het net. Dat is bij elke aanbieder gelijk en telt niet mee in de volgorde. " +
      "Schatting voor " + basis + " en 2.900 kWh verbruik, op de uurprijzen van " + maandJaar(batData.periode.van) + " tot en met " + maandJaar(batData.periode.tot) +
      ". Precies uitrekenen: <a href=\"/batterij-berekenen\">batterijcalculator</a>.";
    el.hidden = false;
  }
  function actieveFilters() {
    return filterEls.filter(function (el) { return el.checked; }).map(function (el) { return el.getAttribute("data-filter"); });
  }
  function byId(id) {
    for (var i = 0; i < aanbieders.length; i++) if (aanbieders[i].id === id) return aanbieders[i];
    return null;
  }

  function render() {
    var c = situatie();
    $("x-zon").hidden = !c.zon;
    $("x-ev").hidden = !c.ev;
    $("x-bat").hidden = !c.bat;
    $("bat-maat-wrap").hidden = !c.batNieuw;
    batRegel(c);
    var optTerug = $("opt-terug");
    optTerug.hidden = optTerug.disabled = !c.zon;
    if (!c.zon && sortEl.value === "terug") sortEl.value = "kosten";
    $("kop-terug").style.visibility = c.zon ? "visible" : "hidden";

    var f = actieveFilters();
    var delen = R.filter(R.sorteer(aanbieders, sortEl.value, c), f);
    var opts = { open: state.open, gekozen: state.gekozen };
    var h = R.lijstHtml(delen.ja, c, opts);
    if (!delen.ja.length) h += '<p class="aanb-leeg">Geen aanbieder waarvan zeker is dat hij aan alle filters voldoet.</p>';
    if (delen.onbekend.length) {
      h += '<div class="aanb-groep-kop"><span>Nog niet uitgezocht voor dit filter: ' + delen.onbekend.length + " aanbieders</span>" +
           '<button type="button" class="aanb-knop" id="toon-onbekend" aria-expanded="' + state.toonOnbekend + '">' + (state.toonOnbekend ? "Verberg" : "Toon") + "</button></div>";
      if (state.toonOnbekend) { opts.start = delen.ja.length; h += R.lijstHtml(delen.onbekend, c, opts); }
    }
    grid.innerHTML = h;

    verschilEl.innerHTML = R.verschilTekst(aanbieders, c);
    $("flt-n").textContent = f.length ? "(" + f.length + ")" : "";
    countEl.textContent = delen.ja.length + " van " + aanbieders.length + " aanbieders" + (f.length ? ", gefilterd" : "") +
      ". Volgorde: " + sortEl.options[sortEl.selectedIndex].text.toLowerCase() + ". Geen enkele aanbieder betaalt voor een plek.";
    $("afname-hint").textContent = c.ev
      ? 'Staat op je jaarafrekening als "levering". De auto telt er apart bij op.'
      : 'Staat op je jaarafrekening als "levering". Trek je teruglevering er niet van af.';
    if (vglDialog.open) vergelijk();
  }

  // ── uitklappen: één regel tegelijk ──
  grid.addEventListener("click", function (e) {
    var t = e.target;
    if (t.id === "toon-onbekend") { state.toonOnbekend = !state.toonOnbekend; render(); $("toon-onbekend").focus(); return; }
    var knop = t.closest ? t.closest(".aanb-open") : null;
    if (!knop) return;
    var id = knop.closest(".aanb-rij").getAttribute("data-id");
    state.open = state.open === id ? null : id;
    render();
    var nieuw = grid.querySelector('.aanb-rij[data-id="' + id + '"] .aanb-open');
    if (nieuw) nieuw.focus();
  });

  // ── vergelijken ──
  function balk(melding) {
    vbalk.hidden = state.gekozen.length === 0;
    document.body.classList.toggle("heeft-vergelijk-bar", state.gekozen.length > 0);
    vnamen.textContent = melding || state.gekozen.map(function (id) { return byId(id).name; }).join(" · ") +
      (state.gekozen.length < 2 ? " · kies er nog één" : "");
    vopen.disabled = state.gekozen.length < 2;
  }
  function syncUrl() {
    try {
      var url = new URL(window.location.href);
      url.searchParams.delete("vergelijk");
      var rest = url.searchParams.toString();
      var q = state.gekozen.length ? "vergelijk=" + state.gekozen.map(encodeURIComponent).join(",") : "";
      var zoek = [rest, q].filter(Boolean).join("&");
      history.replaceState(null, "", url.pathname + (zoek ? "?" + zoek : "") + url.hash);
    } catch (e) { /* oude browser: niets aan de hand */ }
  }
  function vergelijk() {
    var sel = state.gekozen.map(byId).filter(Boolean);
    if (sel.length < 2) { if (vglDialog.open) vglDialog.close(); return; }
    vglBody.innerHTML = R.vergelijkHtml(sel, situatie(), alleenV.checked);
  }
  function openDialog(d, knop) {
    terugNaar = knop;
    if (typeof d.showModal === "function") d.showModal(); else d.setAttribute("open", "");
  }

  document.querySelector("main").addEventListener("change", function (e) {
    var el = e.target;
    if (el.classList.contains("vergelijk-check")) {
      var id = el.getAttribute("data-id"), i = state.gekozen.indexOf(id);
      if (el.checked && i === -1) {
        if (state.gekozen.length >= max()) {
          el.checked = false;
          balk("Maximaal " + max() + " tegelijk. Haal er eerst één weg.");
          return;
        }
        state.gekozen.push(id);
      }
      if (!el.checked && i !== -1) state.gekozen.splice(i, 1);
      syncUrl(); balk();
      if (vglDialog.open) vergelijk();
      return;
    }
    if (el === alleenV) { vergelijk(); return; }
    if (el.type === "number") return; // al verwerkt bij 'input'; opnieuw renderen bij blur slikt de eerstvolgende klik in
    render();
  });
  Object.keys(velden).forEach(function (k) { velden[k].addEventListener("input", render); });

  vopen.addEventListener("click", function () { vergelijk(); openDialog(vglDialog, vopen); });
  vwis.addEventListener("click", function () {
    state.gekozen = []; syncUrl(); balk(); render();
  });
  $("flt-open").addEventListener("click", function () { openDialog(fltDialog, $("flt-open")); });
  Array.prototype.forEach.call(document.querySelectorAll("[data-sluit]"), function (b) {
    b.addEventListener("click", function () { b.closest("dialog").close(); });
  });
  [vglDialog, fltDialog].forEach(function (d) {
    d.addEventListener("close", function () { if (terugNaar) terugNaar.focus(); });
    d.addEventListener("click", function (e) { if (e.target === d) d.close(); }); // klik op de achtergrond
  });

  // ── teller: bezoeken uit Cloudflare, dagelijks bijgewerkt door een workflow ──
  function teller() {
    fetch("/data/teller.json", { cache: "no-cache" }).then(function (r) { return r.ok ? r.json() : null; }).then(function (t) {
      if (!t || !t.totaal || !t.sinds) return;
      var el = $("aanb-teller");
      el.textContent = "Deze vergelijker is sinds " + R.datumNL(t.sinds) + " " + R.duizend(t.totaal) + " keer geopend.";
      el.title = "Paginaweergaven volgens Cloudflare Web Analytics, bijgewerkt " + R.datumNL(t.bijgewerkt) + ". Dit telt bezoeken, geen unieke personen.";
      el.hidden = false;
    }).catch(function () {});
  }

  // ── start ──
  fetch("/data/config.json", { cache: "no-cache" })
    .then(function (r) { return r.json(); })
    .then(function (cfg) {
      aanbieders = (cfg.suppliers || []).filter(function (s) {
        return s.consumer !== false && s.id !== "average" && s.id !== "custom";
      });
      var page = cfg.aanbieders_page || {};
      if (page.default_verbruik_kwh) velden.afname.value = page.default_verbruik_kwh;

      // Situatie van de homepage overnemen (EV / zon / batterij), als die er is.
      try {
        var p = JSON.parse(localStorage.getItem("sv.profile") || "null");
        if (p) { chips.ev.checked = !!p.ev; chips.zon.checked = !!p.solar; chips.bat.checked = !!p.battery; }
      } catch (e) { /* geen opslag beschikbaar */ }

      var pre = (new URLSearchParams(window.location.search).get("vergelijk") || "").split(",").filter(byId);
      state.gekozen = pre.slice(0, max());
      var anker = window.location.hash.slice(1);
      if (anker && byId(anker)) state.open = anker;

      render(); balk();
      if (anker && state.open) {
        var rij = document.getElementById(anker);
        if (rij) rij.scrollIntoView({ block: "start" });
      }
      if (state.gekozen.length >= 2) { vergelijk(); openDialog(vglDialog, vopen); }
      teller();
    })
    .catch(function () {
      // De statisch ingebakken lijst blijft staan; alleen de bediening werkt niet.
      countEl.textContent = "De actuele gegevens konden niet worden geladen. Je ziet de lijst bij 2.900 kWh; vernieuw de pagina om zelf te rekenen.";
    });
})();
