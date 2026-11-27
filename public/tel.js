/*
 * Gebruikstelling voor de rekentools (GoatCounter, zonder cookies).
 *
 * Telt één keer per paginabezoek dat iemand een rekentool echt gebruikt: de
 * eerste keer dat hij zelf een invoer wijzigt of een keuzeknop aantikt. Wie
 * alleen kijkt, telt niet mee. Wie twintig keer schuift, telt één keer.
 *
 * Het seintje gaat naar /api/klik op onze eigen server, die het doorgeeft aan
 * GoatCounter (zo houden adblockers het niet tegen). Bezoekers die niets
 * invullen, sturen niets. Paginaweergaven komen uit Vercel en Cloudflare.
 *
 * Gebruik:  <script src="/tel.js" data-tool="batterij" defer></script>
 * Event in GoatCounter:  rekentool-<tool>
 */
(function () {
  var script = document.currentScript;
  var tool = script && script.getAttribute("data-tool");
  if (!tool) return;

  var KNOPPEN = "button[data-v], button[data-c], button[data-m], #crisisBtn";
  var BUITEN = "header, nav, footer, .partner-slot, .partner-card, .install-tip, [data-geen-tel]";

  var geteld = false;

  function telt(el) {
    if (!el || !el.closest) return false;
    if (el.closest(BUITEN)) return false;
    var tag = el.tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return true;
    return !!el.closest(KNOPPEN);
  }

  function stuur() {
    var body = JSON.stringify({ e: "rekentool-" + tool, t: "Rekentool gebruikt: " + tool });
    try {
      if (navigator.sendBeacon && navigator.sendBeacon("/api/klik", body)) return;
      fetch("/api/klik", { method: "POST", body: body, keepalive: true }).catch(function () {});
    } catch (e) {}
  }

  function tel(e) {
    if (geteld || !e.isTrusted) return;
    var el = e.type === "click" ? e.target.closest && e.target.closest(KNOPPEN) : e.target;
    if (!telt(el)) return;
    geteld = true;
    document.removeEventListener("input", tel, true);
    document.removeEventListener("change", tel, true);
    document.removeEventListener("click", tel, true);

    stuur();
  }

  document.addEventListener("input", tel, true);
  document.addEventListener("change", tel, true);
  document.addEventListener("click", tel, true);
})();
