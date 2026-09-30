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
  var jaarEl = $("rekenjaar");
  var vbalk = $("aanb-vbalk"), vnamen = $("aanb-vbalk-namen"), vopen = $("vergelijk-open"), vwis = $("vergelijk-wis");
  var vglDialog = $("vgl-dialog"), fltDialog = $("flt-dialog"), vglBody = $("vgl-body"), alleenV = $("alleen-verschillen");

  var aanbieders = [];
  var cfg = null;
  var batData = null, batLaden = null;   // data/batterij-effect.json: batterij-effect + beursprijs per kWh
  var netEl = $("netbeheerder");
  var batKwhEl = $("bat-kwh");
  var batAlEl = $("bat-al");
  var sitForm = $("aanb-situatie");
  var OPSLAG = "sv.aanbieders";   // eigen situatie op dit apparaat (alleen gemak; mag leeg of geblokkeerd zijn)
  var state = { gekozen: [], open: null, toonOnbekend: false };
  var terugNaar = null;

  function max() { return window.matchMedia("(max-width: 759px)").matches ? 2 : 3; }
  function getal(el, standaard) {
    var v = parseInt(el.value, 10);
    return isNaN(v) || v < 0 ? standaard : Math.min(v, 100000);
  }
  function situatie() {
    var jaar = parseInt(jaarEl.value, 10) || 2026;
    var c = {
      afname: getal(velden.afname, 0), terug: getal(velden.terug, 0), evKwh: getal(velden.evKwh, 0), jaar: jaar,
      btw: btwEl.checked, zon: chips.zon.checked, ev: chips.ev.checked, bat: chips.bat.checked, api: chips.api.checked
    };
    c.netbeheerder = netEl ? netEl.value : "";
    c.batNieuw = c.bat && batAlEl.value === "nee";
    c.batEffect = c.batNieuw ? batterijEffect(c) : null;
    c.basis = batData ? R.rekenbasis(cfg, batData, c, c.bat ? parseInt(batKwhEl.value, 10) : 0) : null;
    return c;
  }

  // Nieuwe batterij: kies het rekenscenario dat het dichtst bij je teruglevering ligt
  // (zonder zonnepanelen: het scenario zonder panelen) en neem de verschuiving in kWh over.
  function batterijEffect(c) {
    if (!batData) { laadBatData(); return null; }
    var s = R.kiesScenario(batData, c), g = s && s.groottes[batKwhEl.value];
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
    el.innerHTML = "Een nieuwe batterij van " + e.kwh + " kWh maakt je hele stroomrekening <strong>± " + R.eur0(e.besparing) + " per jaar</strong> lager" +
      (c.zon ? " (" + (c.jaar === 2026 ? "2026, met saldering" : "vanaf 2027") + ")" : "") +
      ". Dat zit al in de bedragen: je betaalt gemiddeld " + String(e.prijsNa).replace(".", ",") + " in plaats van " + String(e.prijsVoor).replace(".", ",") +
      " cent per kWh van het net, bij elke aanbieder even veel. " +
      "Schatting voor " + basis + " en 2.900 kWh, op de uurprijzen van " + R.maandJaar(batData.periode.van) + " tot en met " + R.maandJaar(batData.periode.tot) +
      "; precies uitrekenen doe je met de <a href=\"/batterij-berekenen\">batterijcalculator</a>.";
    el.hidden = false;
  }
  function actieveFilters() {
    return filterEls.filter(function (el) { return el.checked; }).map(function (el) { return el.getAttribute("data-filter"); });
  }
  function byId(id) {
    for (var i = 0; i < aanbieders.length; i++) if (aanbieders[i].id === id) return aanbieders[i];
    return null;
  }

  // ── situatie in één regel, opslaan en inklappen ──
  function samenvatting(c) {
    var d = [R.duizend(c.afname) + " kWh van het net"];
    if (c.netbeheerder) d.push(R.esc(c.netbeheerder));
    if (c.zon) d.push("zonnepanelen, " + R.duizend(c.terug) + " kWh terug (" + (c.jaar === 2026 ? "2026" : "vanaf 2027") + ")");
    if (c.ev) d.push("auto " + R.duizend(c.evKwh) + " kWh");
    if (c.bat) d.push("batterij " + batKwhEl.value + " kWh" + (c.batNieuw ? ", nieuw" : ""));
    if (c.api) d.push("Home Assistant / API");
    return "<b>Jouw situatie:</b> " + d.join(" · ");
  }
  function bewaar() {
    var c = situatie();
    var s = { afname: c.afname, net: c.netbeheerder, zon: c.zon, terug: c.terug, jaar: c.jaar, ev: c.ev, evKwh: c.evKwh,
              bat: c.bat, batAl: batAlEl.value, batKwh: batKwhEl.value, api: c.api };
    try { localStorage.setItem(OPSLAG, JSON.stringify(s)); } catch (e) { /* geen opslag: niets aan de hand */ }
  }
  function herstel() {
    var s = null;
    try { s = JSON.parse(localStorage.getItem(OPSLAG) || "null"); } catch (e) { s = null; }
    if (!s || typeof s !== "object") return false;
    var zet = function (el, v) { if (v != null && v !== "") el.value = v; };
    zet(velden.afname, s.afname); zet(velden.terug, s.terug); zet(velden.evKwh, s.evKwh);
    if (s.net != null && Array.prototype.some.call(netEl.options, function (o) { return o.value === s.net; })) netEl.value = s.net;
    if (s.jaar === 2026 || s.jaar === 2027) jaarEl.value = String(s.jaar);
    if (s.batAl === "ja" || s.batAl === "nee") batAlEl.value = s.batAl;
    if (["5", "10", "15", "20"].indexOf(String(s.batKwh)) !== -1) batKwhEl.value = String(s.batKwh);
    chips.zon.checked = !!s.zon; chips.ev.checked = !!s.ev; chips.bat.checked = !!s.bat; chips.api.checked = !!s.api;
    return true;
  }
  function klap(dicht) {
    sitForm.classList.toggle("dicht", dicht);
    $("sit-wijzig").setAttribute("aria-expanded", String(!dicht));
    if (dicht) $("sit-wijzig").focus(); else velden.afname.focus();
  }
  $("sit-wijzig").addEventListener("click", function () { klap(false); });
  $("sit-klaar").addEventListener("click", function () { klap(true); });

  // ── uitleg-knopjes (?) bij de extra vragen ──
  sitForm.addEventListener("click", function (e) {
    var k = e.target.closest ? e.target.closest(".aanb-info") : null;
    if (!k) return;
    var open = k.getAttribute("aria-expanded") !== "true";
    k.setAttribute("aria-expanded", String(open));
    $(k.getAttribute("aria-controls")).hidden = !open;
  });

  // ── "Hoe we rekenen" in de intro opent de uitleg onder de lijst ──
  function openHoe() { var d = $("hoe"); if (d) d.open = true; }
  Array.prototype.forEach.call(document.querySelectorAll('a[href="#hoe"]'), function (a) { a.addEventListener("click", openHoe); });
  if (window.location.hash === "#hoe") openHoe();

  function render() {
    var c = situatie();
    $("x-zon").hidden = !c.zon;
    $("x-ev").hidden = !c.ev;
    $("x-bat").hidden = !c.bat;
    $("bat-maat-wrap").hidden = !c.bat;
    $("sit-klaar").parentElement.hidden = !(c.zon || c.ev || c.bat);   // zonder extra vragen is het formulier al kort
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
    var so = sortEl.options[sortEl.selectedIndex];
    countEl.textContent = delen.ja.length + " van " + aanbieders.length + " aanbieders" + (f.length ? ", gefilterd" : "") +
      ". Volgorde: " + (so.getAttribute("data-lang") || so.text.toLowerCase()) + ". Geen enkele aanbieder betaalt voor een plek.";
    $("afname-hint").textContent = c.ev
      ? 'Beide staan op je jaarafrekening. Neem de "levering"; de auto telt er apart bij op.'
      : 'Beide staan op je jaarafrekening. Neem de "levering", zonder je teruglevering eraf te halen.';
    $("sit-tekst").innerHTML = samenvatting(c);
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
    if (sitForm.contains(el)) bewaar();
  });
  Object.keys(velden).forEach(function (k) { velden[k].addEventListener("input", function () { render(); bewaar(); }); });

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
    .then(function (geladen) {
      cfg = geladen;
      laadBatData();
      aanbieders = (cfg.suppliers || []).filter(function (s) {
        return s.consumer !== false && s.id !== "average" && s.id !== "custom";
      });
      var page = cfg.aanbieders_page || {};
      if (page.default_verbruik_kwh) velden.afname.value = page.default_verbruik_kwh;

      // Eerder ingevulde situatie op dit apparaat, anders die van de homepage (EV / zon / batterij).
      // In beide gevallen staat het formulier ingeklapt tot één regel (dat doet het scriptje in de HTML al).
      if (!herstel()) {
        try {
          var p = JSON.parse(localStorage.getItem("sv.profile") || "null");
          if (p) { chips.ev.checked = !!p.ev; chips.zon.checked = !!p.solar; chips.bat.checked = !!p.battery; }
        } catch (e) { /* geen opslag beschikbaar */ }
      }

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
