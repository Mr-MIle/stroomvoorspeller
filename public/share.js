// share.js — deelknoppen voor stroomvoorspeller.nl
// Bouwt een korte deeltekst (incl. link naar de juiste pagina) uit /data/ha.json
// en bedraadt WhatsApp, X, Kopiëren en native delen (Web Share API).
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
