/*
 * Interesse-teller (GoatCounter, zonder cookies).
 *
 * Telt hoeveel mensen op een "zou je dit willen?"-knop drukken, voor iets dat nog
 * niet bestaat. Er wordt niets opgeslagen behalve één vlag in localStorage, zodat
 * dezelfde browser niet twee keer telt en bij een volgend bezoek het bedankje ziet.
 *
 * GoatCounter laadt pas bij de klik. Wie niet klikt, maakt geen verbinding met
 * GoatCounter. Paginaweergaven telt het bewust niet (no_onload).
 *
 * Gebruik:
 *   <div data-interesse="dagmail">
 *     <button type="button" data-interesse-knop>Ja, die wil ik</button>
 *     <p data-interesse-bedankt hidden>Dank je.</p>
 *   </div>
 *   <script src="/interesse.js" defer></script>
 *
 * Event in GoatCounter:  interesse-<naam>
 */
(function () {
  var TELLER = "https://stroomvoorspeller.goatcounter.com/count";
  var BRON = "https://gc.zgo.at/count.js";
  var SLEUTEL = "sv.interesse.";

  function al(naam) {
    try { return !!localStorage.getItem(SLEUTEL + naam); } catch (e) { return false; }
  }

  function onthoud(naam) {
    try { localStorage.setItem(SLEUTEL + naam, new Date().toISOString().slice(0, 10)); } catch (e) {}
  }

  function toonBedankt(blok) {
    var knop = blok.querySelector("[data-interesse-knop]");
    var bedankt = blok.querySelector("[data-interesse-bedankt]");
    if (knop) knop.hidden = true;
    if (bedankt) bedankt.hidden = false;
  }

  function stuur(naam) {
    try {
      window.goatcounter.count({
        path: "interesse-" + naam,
        title: "Interesse: " + naam,
        event: true
      });
    } catch (e) {}
  }

  function tel(naam) {
    if (window.goatcounter && window.goatcounter.count) { stuur(naam); return; }
    window.goatcounter = window.goatcounter || {};
    window.goatcounter.no_onload = true;
    var s = document.createElement("script");
    s.async = true;
    s.src = BRON;
    s.setAttribute("data-goatcounter", TELLER);
    s.onload = function () { stuur(naam); };
    document.head.appendChild(s);
  }

  var blokken = document.querySelectorAll("[data-interesse]");
  Array.prototype.forEach.call(blokken, function (blok) {
    var naam = blok.getAttribute("data-interesse");
    if (!naam) return;
    if (al(naam)) { toonBedankt(blok); return; }
    var knop = blok.querySelector("[data-interesse-knop]");
    if (!knop) return;
    knop.addEventListener("click", function (e) {
      if (!e.isTrusted || al(naam)) return;
      onthoud(naam);
      tel(naam);
      toonBedankt(blok);
    });
  });
})();
