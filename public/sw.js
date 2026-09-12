/*
 * Service worker voor stroomvoorspeller.nl.
 *
 * Uitgangspunt: het net wint altijd. Prijzen die een dag oud zijn en er vers
 * uitzien, zijn erger dan een pagina die even laadt. De cache is er alleen voor
 * offline en voor een hapering; zodra het netwerk antwoordt, wint dat antwoord.
 *
 * Verhoog VERSIE bij elke wijziging aan dit bestand of aan de schil hieronder.
 */
var VERSIE = "v1";
var SCHIL_CACHE = "schil-" + VERSIE;
var DATA_CACHE = "data-" + VERSIE;
var PAGINA_CACHE = "paginas-" + VERSIE;

// Statische bestanden die altijd nodig zijn om iets te kunnen tonen.
var SCHIL = [
  "/",
  "/styles.css",
  "/app.js",
  "/nav.js",
  "/share.js",
  "/favicon.svg",
  "/icon-192.png",
  "/icon-512.png",
  "/apple-touch-icon.png",
  "/manifest.webmanifest"
];

var NET_TIMEOUT_MS = 5000;

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(SCHIL_CACHE).then(function (c) {
      // Per bestand, zodat één 404 niet de hele installatie sloopt.
      return Promise.all(SCHIL.map(function (u) {
        return c.add(new Request(u, { cache: "reload" })).catch(function () {});
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (namen) {
      return Promise.all(namen.map(function (n) {
        if (n !== SCHIL_CACHE && n !== DATA_CACHE && n !== PAGINA_CACHE) return caches.delete(n);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

// Handmatig verversen vanuit de pagina.
self.addEventListener("message", function (e) {
  if (e.data === "skipWaiting") self.skipWaiting();
});

function metTimeout(request) {
  return new Promise(function (resolve, reject) {
    var klaar = false;
    var t = setTimeout(function () {
      if (!klaar) { klaar = true; reject(new Error("timeout")); }
    }, NET_TIMEOUT_MS);
    fetch(request).then(function (r) {
      if (klaar) return;
      klaar = true; clearTimeout(t); resolve(r);
    }, function (err) {
      if (klaar) return;
      klaar = true; clearTimeout(t); reject(err);
    });
  });
}

function bewaar(cacheNaam, request, response) {
  if (!response || !response.ok || response.type === "opaque") return response;
  var kopie = response.clone();
  caches.open(cacheNaam).then(function (c) {
    // Zonder querystring bewaren: app.js hangt er ?nocache=<tijd> aan, anders
    // groeit de cache eindeloos en vindt de offline-lookup nooit een treffer.
    c.put(new Request(request.url.split("?")[0]), kopie);
  }).catch(function () {});
  return response;
}

function uitCache(cacheNaam, request) {
  return caches.open(cacheNaam).then(function (c) {
    return c.match(new Request(request.url.split("?")[0]));
  });
}

// Netwerk eerst, cache als vangnet. Voor pagina's en data.
function netEerst(cacheNaam, request, terugval) {
  return metTimeout(request).then(function (r) {
    return bewaar(cacheNaam, request, r);
  }).catch(function () {
    return uitCache(cacheNaam, request).then(function (c) {
      return c || terugval();
    });
  });
}

// Cache eerst, op de achtergrond verversen. Voor css, js en plaatjes.
function cacheEerstVerversLater(request) {
  return caches.open(SCHIL_CACHE).then(function (c) {
    return c.match(request).then(function (hit) {
      var net = fetch(request).then(function (r) {
        if (r && r.ok) c.put(request, r.clone());
        return r;
      }).catch(function () { return hit; });
      return hit || net;
    });
  });
}

function offlinePagina() {
  return caches.match("/").then(function (home) {
    if (home) return home;
    return new Response(
      "<!doctype html><html lang=nl><meta charset=utf-8>" +
      "<meta name=viewport content='width=device-width,initial-scale=1'>" +
      "<title>Geen verbinding</title>" +
      "<body style=\"font:16px/1.5 system-ui,sans-serif;margin:0;padding:2rem;background:#f0f6fc;color:#16324f\">" +
      "<h1 style=\"font-size:1.25rem\">Geen verbinding</h1>" +
      "<p>Stroomvoorspeller heeft geen internet en geen opgeslagen versie van deze pagina.</p>" +
      "<p><a href=\"/\" style=\"color:#0f6cbd\">Naar de voorpagina</a></p>",
      { headers: { "Content-Type": "text/html; charset=utf-8" } }
    );
  });
}

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;

  var url;
  try { url = new URL(req.url); } catch (err) { return; }
  if (url.origin !== self.location.origin) return;   // chart.js e.d. laten lopen
  if (url.pathname.indexOf("/embed") === 0) return;  // widget nooit onderscheppen

  // Prijzen en voorspellingen: altijd eerst het net proberen.
  if (url.pathname.indexOf("/data/") === 0) {
    e.respondWith(netEerst(DATA_CACHE, req, function () {
      return new Response(JSON.stringify({ error: "offline" }), {
        status: 503,
        headers: { "Content-Type": "application/json" }
      });
    }));
    return;
  }

  // Pagina's: ook net eerst, anders zie je de prijzen van gisteren.
  if (req.mode === "navigate") {
    e.respondWith(netEerst(PAGINA_CACHE, req, offlinePagina));
    return;
  }

  if (/\.(css|js|svg|png|jpg|webp|woff2?|ico)$/.test(url.pathname)) {
    e.respondWith(cacheEerstVerversLater(req));
  }
});
