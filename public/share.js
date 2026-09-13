// share.js — deelknoppen voor stroomvoorspeller.nl
// Bouwt een korte deeltekst (incl. link naar de juiste pagina) uit /data/ha.json
// en bedraadt WhatsApp, X, Kopiëren en native delen (Web Share API).
// Zet daaronder ook de beginscherm-strip neer voor mobiele bezoekers (#84).
(function () {
  "use strict";

  var SITE = "https://stroomvoorspeller.nl";

  // EUR/kWh -> ct/kWh, NL-notatie met 1 decimaal (bv. 0.07827 -> "7,8")
  function ct(eurPerKwh) {
    return (Number(eurPerKwh) * 100).toLocaleString("nl-NL", {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1
    });
  }

  // Uur uit ISO-start (bv. "2026-06-07T14:00:00+02:00" -> "14:00")
  function hour(iso) {
    var m = String(iso).match(/T(\d{2}:\d{2})/);
    return m ? m[1] : "";
  }

  var WEEKDAYS = ["zo", "ma", "di", "wo", "do", "vr", "za"];
  var MONTHS = ["januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december"];

  // "2026-06-07" -> "zo 7 juni"
  function dutchDate(ymd) {
    var p = String(ymd).split("-");
    if (p.length !== 3) return "";
    var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
    return WEEKDAYS[d.getUTCDay()] + " " + (+p[2]) + " " + MONTHS[+p[1] - 1];
  }

  function buildText(day, block) {
    var cheap = block.cheapest, exp = block.most_expensive;
    if (day === "tomorrow") {
      return "⚡ Stroomprijzen morgen (" + dutchDate(block.date) + "): " +
        "goedkoopst om " + hour(cheap.start) + " (" + ct(cheap.all_in) + " ct/kWh), " +
        "duurst om " + hour(exp.start) + " (" + ct(exp.all_in) + " ct). " +
        "Gemiddeld " + ct(block.average_all_in) + " ct/kWh incl. btw.\n\n" +
        "👉 Alle 24 uren: " + SITE + "/morgen";
    }
    return "⚡ Stroomprijzen vandaag (" + dutchDate(block.date) + "): " +
      "goedkoopst om " + hour(cheap.start) + " (" + ct(cheap.all_in) + " ct/kWh), " +
      "duurst om " + hour(exp.start) + " (" + ct(exp.all_in) + " ct). " +
      "Gemiddeld " + ct(block.average_all_in) + " ct/kWh incl. btw.\n\n" +
      "👉 Bekijk alle uurprijzen: " + SITE;
  }

  function init() {
    initInstall();

    var row = document.getElementById("share-row");
    if (!row) return;
    var container = document.querySelector(".share-tip");
    var day = row.getAttribute("data-share-day") === "tomorrow" ? "tomorrow" : "today";

    fetch("/data/ha.json", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("ha.json HTTP " + r.status); return r.json(); })
      .then(function (data) {
        var block = data[day];
        // Geen (volledige) data → rij verbergen i.p.v. een leeg bericht delen.
        if (!block || block.available === false || block.complete === false ||
            !block.cheapest || !block.most_expensive) {
          if (container) container.hidden = true;
          return;
        }

        var text = buildText(day, block);
        var url = day === "tomorrow" ? SITE + "/morgen" : SITE;

        wire(row, text, url);
        if (container) container.hidden = false;
      })
      .catch(function () {
        if (container) container.hidden = true;
      });
  }

  function wire(row, text, url) {
    var enc = encodeURIComponent(text);

    var wa = row.querySelector(".share-wa");
    if (wa) {
      wa.addEventListener("click", function () {
        window.open("https://wa.me/?text=" + enc, "_blank", "noopener");
      });
    }

    var x = row.querySelector(".share-x");
    if (x) {
      x.addEventListener("click", function () {
        window.open("https://twitter.com/intent/tweet?text=" + enc, "_blank", "noopener");
      });
    }

    var copy = row.querySelector(".share-copy");
    if (copy) {
      copy.addEventListener("click", function () {
        var done = function () {
          copy.classList.add("is-copied");
          setTimeout(function () { copy.classList.remove("is-copied"); }, 1600);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
        } else {
          fallbackCopy(text);
          done();
        }
      });
    }

    var native = row.querySelector(".share-native");
    if (native) {
      if (navigator.share) {
        native.hidden = false;
        native.addEventListener("click", function () {
          navigator.share({ text: text, url: url }).catch(function () {});
        });
      } else {
        native.hidden = true;
      }
    }
  }

  /* Beginscherm-strip (#84)
   * Mobiele bezoeker wijzen op de webapp. Android kan het in één tik via
   * beforeinstallprompt; iOS heeft daar geen API voor, dus daar tonen we de
   * ene handeling die wél werkt: het deelmenu.
   * Onafhankelijk van ha.json — deze strip hangt niet aan de prijzen.
   */
  var INSTALL_OPSLAG = "sv-install-weg";
  var INSTALL_DAGEN = 60;

  // Zet op true zodra op een echte iPhone is vastgesteld dat "Zet op
  // beginscherm" in het web-deelmenu (navigator.share) staat. Dan opent de
  // knop dat menu direct in plaats van alleen uit te leggen waar het zit.
  var IOS_DEELMENU_WERKT = false;

  // Het deelicoon van iOS, zodat de uitleg laat zien waar je moet tikken.
  var DEEL_ICOON = '<svg class="install-deelicoon" viewBox="0 0 24 24" width="15" height="15" ' +
    'aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" ' +
    'stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M12 3.5v10M12 3.5L8.8 6.7M12 3.5l3.2 3.2"/>' +
    '<path d="M7.5 10.5H5.5v10h13v-10h-2"/></svg>';

  // Chrome kan dit event al afvuren voordat de DOM klaar is, dus luisteren we
  // meteen bij het laden van dit script en niet pas in initInstall().
  var wachtendePrompt = null;
  var meldPrompt = null;
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    wachtendePrompt = e;
    if (meldPrompt) meldPrompt();
  });

  function installWeggeklikt() {
    try {
      var v = window.localStorage.getItem(INSTALL_OPSLAG);
      if (!v) return false;
      var t = parseInt(v, 10);
      return !!t && Date.now() - t < INSTALL_DAGEN * 86400000;
    } catch (e) { return false; }
  }

  function onthoudInstallWegklik() {
    try { window.localStorage.setItem(INSTALL_OPSLAG, String(Date.now())); } catch (e) {}
  }

  function alsAppGeopend() {
    try {
      if (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) return true;
    } catch (e) {}
    return navigator.standalone === true;
  }

  function isMobiel() {
    try {
      return window.matchMedia("(pointer: coarse)").matches || window.innerWidth <= 720;
    } catch (e) { return window.innerWidth <= 720; }
  }

  function isIOS() {
    var ua = navigator.userAgent || "";
    if (/iPad|iPhone|iPod/.test(ua)) return true;
    // iPad met iPadOS 13+ meldt zich als Mac met aanraakscherm
    return /Macintosh/.test(ua) && navigator.maxTouchPoints > 1;
  }

  // In-app-browsers (Facebook, Instagram, LinkedIn) hebben geen "Zet op
  // beginscherm". Daar is de enige goede raad: open de pagina in Safari.
  function isInAppBrowser() {
    return /FBAN|FBAV|Instagram|LinkedInApp|Line\//.test(navigator.userAgent || "");
  }

  function installStijl() {
    if (document.getElementById("install-stijl")) return;
    var s = document.createElement("style");
    s.id = "install-stijl";
    s.textContent =
      ".install-tip{position:relative;margin:-0.6rem 0 1.4rem;padding:0.6rem 2.9rem 0.6rem 0.85rem;" +
      "border:1px solid var(--c-border);border-radius:var(--radius-sm,8px);background:var(--c-surface,#fff);" +
      "display:flex;align-items:center;gap:0.6rem;flex-wrap:wrap}" +
      ".install-tip-tekst{flex:1 1 9rem;margin:0;font-size:0.9rem;color:var(--c-text-soft)}" +
      ".install-tip-knop{flex:0 0 auto;min-height:44px;padding:0 0.9rem;border:1px solid var(--c-brand,#0f6cbd);" +
      "border-radius:var(--radius-sm,8px);background:var(--c-brand,#0f6cbd);color:#fff;font:inherit;" +
      "font-size:0.9rem;font-weight:600;cursor:pointer}" +
      ".install-tip-knop:active{transform:scale(0.97)}" +
      ".install-tip-sluit{position:absolute;top:0;right:0;width:44px;height:44px;border:0;background:none;" +
      "color:var(--c-text-soft);font-size:1.1rem;line-height:1;cursor:pointer}" +
      ".install-tip-hulp{flex:1 1 100%;margin:0.5rem 0 0;font-size:0.88rem;color:var(--c-text-soft);line-height:1.45}" +
      ".install-tip-hulp[hidden]{display:none}" +
      ".install-deelicoon{vertical-align:-3px;margin:0 2px}";
    document.head.appendChild(s);
  }

  function initInstall() {
    if (!isMobiel() || alsAppGeopend() || installWeggeklikt()) return;
    var anker = document.querySelector(".share-tip");
    if (!anker || document.querySelector(".install-tip")) return;

    var ios = isIOS();
    var strip = document.createElement("section");
    strip.className = "install-tip";
    strip.setAttribute("aria-label", "Stroomvoorspeller op je beginscherm");

    var tekst = document.createElement("p");
    tekst.className = "install-tip-tekst";
    tekst.textContent = "Elke dag de prijzen checken? Zet deze site op je beginscherm.";

    var knop = document.createElement("button");
    knop.type = "button";
    knop.className = "install-tip-knop";
    knop.textContent = ios ? "Uitleg" : "Toevoegen";

    var sluit = document.createElement("button");
    sluit.type = "button";
    sluit.className = "install-tip-sluit";
    sluit.setAttribute("aria-label", "Niet meer tonen");
    sluit.innerHTML = "&times;";

    var hulp = document.createElement("p");
    hulp.className = "install-tip-hulp";
    hulp.hidden = true;

    sluit.addEventListener("click", function () {
      onthoudInstallWegklik();
      strip.remove();
    });

    knop.addEventListener("click", function () {
      if (wachtendePrompt) {
        var p = wachtendePrompt;
        wachtendePrompt = null;
        p.prompt();
        p.userChoice.then(function (keuze) {
          if (keuze && keuze.outcome === "accepted") {
            onthoudInstallWegklik();
            strip.remove();
          } else {
            knop.textContent = "Toevoegen";
          }
        }).catch(function () {});
        return;
      }
      if (ios && IOS_DEELMENU_WERKT && navigator.share) {
        navigator.share({ url: SITE }).catch(function () {});
      }
      if (isInAppBrowser()) {
        hulp.textContent = "Open deze pagina eerst in Safari. In deze app bestaat de knop " +
          "“Zet op beginscherm” niet.";
      } else if (ios) {
        hulp.innerHTML = "Tik onderin op het deelicoon " + DEEL_ICOON +
          " en kies “Zet op beginscherm”.";
      } else {
        hulp.textContent = "Open het menu van je browser (⋮) en kies “App installeren” " +
          "of “Toevoegen aan beginscherm”.";
      }
      hulp.hidden = false;
      knop.hidden = true;
    });

    strip.appendChild(tekst);
    strip.appendChild(knop);
    strip.appendChild(sluit);
    strip.appendChild(hulp);
    installStijl();
    anker.parentNode.insertBefore(strip, anker.nextSibling);

    // Android/Chrome: echte installatie in één tik. Het event kan vóór of ná
    // het bouwen van de strip binnenkomen; beide gevallen komen hier uit.
    meldPrompt = function () {
      knop.hidden = false;
      hulp.hidden = true;
      knop.textContent = "Toevoegen";
    };
    if (wachtendePrompt) meldPrompt();

    window.addEventListener("appinstalled", function () {
      onthoudInstallWegklik();
      strip.remove();
    });
  }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "absolute";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
