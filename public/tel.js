/*
 * Gebruikstelling voor de rekentools (GoatCounter, zonder cookies).
 *
 * Telt één keer per paginabezoek dat iemand een rekentool echt gebruikt: de
 * eerste keer dat hij zelf een invoer wijzigt of een keuzeknop aantikt. Wie
 * alleen kijkt, telt niet mee. Wie twintig keer schuift, telt één keer.
 *
 * GoatCounter laadt pas op dat moment. Bezoekers die niets invullen, maken dus
 * geen enkele verbinding met GoatCounter. Paginaweergaven telt GoatCounter
 * bewust niet (no_onload); die komen uit Vercel en Cloudflare.
 *
 * Gebruik:  <script src="/tel.js" data-tool="batterij" defer></script>
 * Event in GoatCounter:  rekentool-<tool>
 */
(function () {
  var script = document.currentScript;
  var tool = script && script.getAttribute("data-tool");
  if (!tool) return;

  var TELLER = "https://stroomvoorspeller.goatcounter.com/count";
  var BRON = "https://gc.zgo.at/count.js";
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
    try {
      window.goatcounter.count({
        path: "rekentool-" + tool,
        title: "Rekentool gebruikt: " + tool,
        event: true
      });
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

    if (window.goatcounter && window.goatcounter.count) { stuur(); return; }
    window.goatcounter = window.goatcounter || {};
    window.goatcounter.no_onload = true;
    var s = document.createElement("script");
    s.async = true;
    s.src = BRON;
    s.setAttribute("data-goatcounter", TELLER);
    s.onload = stuur;
    document.head.appendChild(s);
  }

  document.addEventListener("input", tel, true);
  document.addEventListener("change", tel, true);
  document.addEventListener("click", tel, true);
})();
