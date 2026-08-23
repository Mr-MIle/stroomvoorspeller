/*
 * partner.js — verwijslink-blokken voor stroomvoorspeller.nl
 * -------------------------------------------------------------------------
 * Eén bron voor alle partnerblokken: public/data/partners.json.
 * Zet op een pagina waar het blok mag staan:
 *
 *     <div class="partner-slot" data-partner="joulo-ere-top"></div>
 *
 * en dit script vult die plek met een kaart. Staat de slot-id niet in de
 * JSON, of staat de partner op "actief": false, dan blijft de plek leeg —
 * zo zet je alles in één keer uit zonder pagina's te bewerken.
 *
 * De uitgaande link wijst altijd naar een interne tussenpagina (/uit/<naam>),
 * zodat Cloudflare Web Analytics de klik als paginaweergave telt. De echte
 * verwijslink staat alleen in die tussenpagina.
 *
 * Wegklikken wordt per partner onthouden in localStorage (standaard 60 dagen).
 * -------------------------------------------------------------------------
 */
(function () {
  "use strict";

  var BRON = "/data/partners.json";
  var OPSLAG_PREFIX = "sv-partner-weg:";
  var STANDAARD_DAGEN = 60;

  function maak(tag, cls, tekst) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (tekst != null) n.textContent = tekst;
    return n;
  }

  function isWeggeklikt(sleutel, dagen) {
    try {
      var v = window.localStorage.getItem(OPSLAG_PREFIX + sleutel);
      if (!v) return false;
      var t = parseInt(v, 10);
      if (!t) return false;
      return Date.now() - t < dagen * 86400000;
    } catch (e) {
      return false;
    }
  }

  function onthoudWegklik(sleutel) {
    try {
      window.localStorage.setItem(OPSLAG_PREFIX + sleutel, String(Date.now()));
    } catch (e) {
      /* privémodus of opslag vol: dan is wegklikken alleen voor deze pagina */
    }
  }

  function klikUrl(basis, bron) {
    if (!bron) return basis;
    return basis + (basis.indexOf("?") === -1 ? "?" : "&") + "bron=" + encodeURIComponent(bron);
  }

  function bouwKaart(def, partner, partnerSleutel) {
    var kaart = maak("aside", "partner-card");
    kaart.setAttribute("role", "complementary");
    kaart.setAttribute("aria-label", partner.label || "Verwijslink");
    kaart.setAttribute("data-partner-key", partnerSleutel);

    var body = maak("div", "partner-body");
    body.appendChild(maak("span", "partner-label", partner.label || "Verwijslink"));
    body.appendChild(maak("p", "partner-title", def.titel || ""));
    if (def.tekst) body.appendChild(maak("p", "partner-text", def.tekst));

    if (def.meer && def.meer.url && def.meer.tekst) {
      var meerP = maak("p", "partner-meer");
      var meerA = maak("a", null, def.meer.tekst);
      meerA.href = def.meer.url;
      meerP.appendChild(meerA);
      body.appendChild(meerP);
    }
    kaart.appendChild(body);

    var cta = maak("a", "partner-cta", (def.cta || partner.cta || "Bekijken") + " →");
    cta.href = klikUrl(partner.url, def.bron);
    cta.setAttribute("rel", "sponsored nofollow noopener noreferrer");
    cta.setAttribute("target", "_blank");
    kaart.appendChild(cta);

    if (partner.wegklikbaar !== false) {
      var weg = maak("button", "partner-dismiss", "×");
      weg.type = "button";
      weg.setAttribute("aria-label", "Verberg dit blok");
      weg.setAttribute("title", "Verberg dit blok");
      weg.addEventListener("click", function () {
        onthoudWegklik(partnerSleutel);
        var alle = document.querySelectorAll('.partner-card[data-partner-key="' + partnerSleutel + '"]');
        for (var i = 0; i < alle.length; i++) alle[i].remove();
      });
      kaart.appendChild(weg);
    }

    return kaart;
  }

  function vul(data, plekken) {
    var slots = data.slots || {};
    var partners = data.partners || {};

    plekken.forEach(function (plek) {
      var def = slots[plek.getAttribute("data-partner")];
      if (!def) return;
      var partnerSleutel = def.partner;
      var partner = partners[partnerSleutel];
      if (!partner || partner.actief === false || !partner.url) return;
      if (isWeggeklikt(partnerSleutel, partner.verberg_dagen || STANDAARD_DAGEN)) return;
      plek.appendChild(bouwKaart(def, partner, partnerSleutel));
    });
  }

  function start() {
    var plekken = Array.prototype.slice.call(document.querySelectorAll(".partner-slot[data-partner]"));
    if (!plekken.length) return;
    if (!window.fetch) return;

    fetch(BRON, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data) vul(data, plekken); })
      .catch(function () { /* geen partnerblok is geen probleem */ });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
