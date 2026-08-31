/*
 * Gedeeld hoofdmenu voor stroomvoorspeller.nl.
 * Eén bron voor het menu: pas hieronder NAV aan en het werkt op alle pagina's.
 * Vervangt de (no-JS fallback) inhoud van <nav class="primary-nav"> door een
 * gegroepeerd menu met uitklapmenu's. Markeert de huidige pagina automatisch.
 */
(function () {
  "use strict";

  var NAV = [
    { label: "Prijzen", children: [
      { href: "/", label: "Nu" },
      { href: "/morgen", label: "Morgen" },
      { href: "/historisch", label: "Historisch" }
    ] },
    { href: "/aanbieders", label: "Aanbieders" },
    { href: "/kennisbank/", label: "Kennisbank" },
    { label: "Slim thuis", children: [
      { href: "/batterij", label: "Thuisbatterij" },
      { href: "/batterij-berekenen", label: "Batterij berekenen" },
      { href: "/ere-vergelijken", label: "ERE-vergelijker" },
      { href: "/integraties", label: "Integraties" },
      { href: "/home-assistant", label: "Home Assistant" }
    ] }
  ];

  function norm(p) {
    if (!p) return "/";
    p = p.replace(/index\.html$/, "").replace(/\.html$/, "");
    if (p.length > 1 && p.charAt(p.length - 1) === "/") p = p.slice(0, -1);
    return p === "" ? "/" : p;
  }

  function isActive(href, loc) {
    var h = norm(href), l = norm(loc);
    if (h === "/") return l === "/";
    return l === h || l.indexOf(h + "/") === 0;
  }

  function build(loc) {
    var nav = document.querySelector("nav.primary-nav");
    if (!nav) return;
    nav.innerHTML = "";

    NAV.forEach(function (item, idx) {
      if (!item.children) {
        var a = document.createElement("a");
        a.href = item.href;
        a.textContent = item.label;
        a.className = "nav-top";
        if (isActive(item.href, loc)) a.setAttribute("aria-current", "page");
        nav.appendChild(a);
        return;
      }

      var group = document.createElement("div");
      group.className = "nav-group";

      var anyActive = item.children.some(function (c) { return isActive(c.href, loc); });

      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "nav-top nav-toggle";
      btn.setAttribute("aria-expanded", "false");
      btn.setAttribute("aria-haspopup", "true");
      var menuId = "nav-menu-" + idx;
      btn.setAttribute("aria-controls", menuId);
      btn.innerHTML = item.label + ' <span class="nav-caret" aria-hidden="true">▾</span>';
      if (anyActive) btn.setAttribute("aria-current", "page");

      var menu = document.createElement("div");
      menu.className = "nav-menu";
      menu.id = menuId;
      menu.setAttribute("role", "menu");

      item.children.forEach(function (c) {
        var ca = document.createElement("a");
        ca.href = c.href;
        ca.textContent = c.label;
        ca.setAttribute("role", "menuitem");
        if (isActive(c.href, loc)) ca.setAttribute("aria-current", "page");
        menu.appendChild(ca);
      });

      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        var open = group.classList.toggle("open");
        btn.setAttribute("aria-expanded", open ? "true" : "false");
        closeOthers(group);
      });

      group.appendChild(btn);
      group.appendChild(menu);
      nav.appendChild(group);
    });

    document.addEventListener("click", function () { closeOthers(null); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeOthers(null);
    });
  }

  function closeOthers(keep) {
    var groups = document.querySelectorAll("nav.primary-nav .nav-group");
    Array.prototype.forEach.call(groups, function (g) {
      if (g === keep) return;
      g.classList.remove("open");
      var b = g.querySelector(".nav-toggle");
      if (b) b.setAttribute("aria-expanded", "false");
    });
  }

  // Terug-naar-boven-knop: verschijnt op elke pagina zodra je ver genoeg scrolt.
  function initToTop() {
    if (document.querySelector(".to-top")) return;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "to-top";
    btn.setAttribute("aria-label", "Terug naar boven");
    btn.innerHTML = '<span aria-hidden="true">↑</span>';
    btn.addEventListener("click", function () {
      var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      window.scrollTo({ top: 0, behavior: reduce ? "auto" : "smooth" });
    });
    document.body.appendChild(btn);

    var ticking = false;
    function update() {
      ticking = false;
      if (window.pageYOffset > 600) btn.classList.add("is-visible");
      else btn.classList.remove("is-visible");
    }
    window.addEventListener("scroll", function () {
      if (!ticking) { window.requestAnimationFrame(update); ticking = true; }
    }, { passive: true });
    update();
  }

  // Verwijslink-blokken (public/partner.js) alleen laden waar er ook een blok
  // kan verschijnen: een pagina met een .partner-slot, of een kennisbank-artikel
  // dat zijn categorie meldt via <meta name="kb-categorie">. Zo krijgt elk nieuw
  // artikel in zo'n categorie het blok vanzelf, zonder extra scripttag.
  function initPartner() {
    if (document.querySelector('script[src="/partner.js"]')) return;
    var nodig = document.querySelector(".partner-slot") ||
                document.querySelector('meta[name="kb-categorie"]');
    if (!nodig) return;
    var s = document.createElement("script");
    s.src = "/partner.js";
    s.defer = true;
    document.body.appendChild(s);
  }

  function init() {
    document.documentElement.classList.add("has-js");
    build(location.pathname);
    initToTop();
    initPartner();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
