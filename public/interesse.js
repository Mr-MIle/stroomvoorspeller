/*
 * Interesse-teller (GoatCounter, zonder cookies).
 *
 * Telt hoeveel mensen op een "zou je dit willen?"-knop drukken, voor iets dat nog
 * niet bestaat. Er wordt niets opgeslagen behalve één vlag in localStorage, zodat
 * dezelfde browser niet twee keer telt en bij een volgend bezoek het bedankje ziet.
 *
 * Pas bij de klik gaat er een seintje naar /api/klik op onze eigen server, die
 * het doorgeeft aan GoatCounter (zo houden adblockers het niet tegen).
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

  function tel(naam) {
    var body = JSON.stringify({ e: "interesse-" + naam, t: "Interesse: " + naam });
    try {
      if (navigator.sendBeacon && navigator.sendBeacon("/api/klik", body)) return;
      fetch("/api/klik", { method: "POST", body: body, keepalive: true }).catch(function () {});
    } catch (e) {}
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
