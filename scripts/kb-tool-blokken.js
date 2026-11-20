// Zet onder elk kennisbankartikel een vast blok met de rekentool(s) die bij het
// onderwerp horen, direct boven "Lees ook". Idempotent: het blok staat tussen
// <!-- KB-TOOL:START --> en <!-- KB-TOOL:END --> en wordt bij elke run vervangen.
//
// Nieuw artikel? Voeg het toe aan KOPPELING en draai:  node scripts/kb-tool-blokken.js
// Artikelen die niet in KOPPELING staan, krijgen geen blok (en een melding).

const fs = require("fs");
const path = require("path");

const DIR = path.join(__dirname, "..", "public", "kennisbank");

const TOOLS = {
  batterij: { href: "/batterij-berekenen", titel: "Thuisbatterij berekenen", tekst: "Wat levert een batterij jou op, met en zonder saldering? Gerekend op echte uurprijzen." },
  dynamisch: { href: "/dynamisch-berekenen", titel: "Is dynamisch voordelig voor jou?", tekst: "Vijf vragen, dan zie je wat een dynamisch contract jou per jaar kost of oplevert." },
  aanbieders: { href: "/aanbieders", titel: "Aanbieders vergelijken", tekst: "Alle dynamische aanbieders op kosten bij jouw verbruik, inclusief btw en teruglevering." },
  batterijen: { href: "/batterijen-vergelijken", titel: "Thuisbatterijen vergelijken", tekst: "Modellen naast elkaar: capaciteit, vermogen, prijs, garantie en sluipverbruik." },
  ere: { href: "/ere-vergelijken", titel: "ERE-inboekers vergelijken", tekst: "Wat levert thuisladen jou op aan ERE-certificaten, per inboeker." },
  morgen: { href: "/morgen", titel: "Stroomprijs morgen per uur", tekst: "Alle 24 uren van morgen, met de goedkoopste blokken voor auto, wasmachine of warmtepomp." },
  prijzen: { href: "/", titel: "Prijs nu en voorspelling", tekst: "De actuele stroomprijs en de verwachting voor de komende dagen." },
  historisch: { href: "/historisch", titel: "Historische stroomprijzen", tekst: "Terugkijken per dag, maand of jaar, vanaf 2015." },
  records: { href: "/records", titel: "Records van de stroommarkt", tekst: "Laagste en hoogste uurprijs ooit en het aantal negatieve uren per jaar." },
  ha: { href: "/home-assistant", titel: "Home Assistant koppelen", tekst: "Laat apparaten zelf schakelen op de uurprijs. Gratis feed, kant-en-klare sensor." },
  mijnbatterij: { href: "/batterij", titel: "Mijn eigen thuisbatterij, dag voor dag", tekst: "Echte meetdata: wat mijn batterij laadt, levert en opbrengt." },
  archief: { href: "/over/voorspelarchief", titel: "Wat voorspelde het model vooraf?", tekst: "Kies een dag en vergelijk de voorspelling met de echte prijs." },
  nauwkeurigheid: { href: "/over/performance", titel: "Hoe nauwkeurig is de voorspelling?", tekst: "De gemeten afwijking over de laatste 30 dagen, per horizon." },
};

// artikel (zonder .html) -> tools, belangrijkste eerst, hooguit twee
const KOPPELING = {
  "ac-dc-wisselstroom-gelijkstroom": ["batterij", "batterijen"],
  "airco-dynamisch-tarief": ["dynamisch", "morgen"],
  "balkonzonnepanelen-dynamisch-contract": ["dynamisch", "aanbieders"],
  "begrippenlijst": ["prijzen", "dynamisch"],
  "beste-app-dynamische-stroomprijzen": ["prijzen", "ha"],
  "buurtbatterij": ["batterij", "batterijen"],
  "demand-response": ["dynamisch", "ha"],
  "dubbele-energiebelasting-thuisbatterij": ["batterij", "batterijen"],
  "dynamisch-contract-vergelijken": ["aanbieders", "dynamisch"],
  "dynamisch-gascontract": ["aanbieders", "dynamisch"],
  "dynamisch-of-vast-contract": ["dynamisch", "aanbieders"],
  "eigen-verbruik-zonne-energie": ["batterij", "dynamisch"],
  "elektrische-auto-laden-zonneoverschot": ["ere", "morgen"],
  "elektrische-auto-of-benzine-kosten": ["ere", "dynamisch"],
  "energiebelasting-2027": ["dynamisch", "aanbieders"],
  "energiebelasting-vaste-kosten-uitgelegd": ["aanbieders", "dynamisch"],
  "ere-certificaten-laadpaal": ["ere", "morgen"],
  "groepenkast-warmtepomp-laadpaal-batterij": ["batterijen", "batterij"],
  "hoe-wordt-de-stroomprijs-bepaald": ["prijzen", "historisch"],
  "hoeveel-bespaar-je-dynamisch-contract": ["dynamisch", "aanbieders"],
  "hybride-omvormer-thuisbatterij": ["batterijen", "batterij"],
  "isde-subsidie-warmtepomp-2027": ["dynamisch", "aanbieders"],
  "kosten-elektrische-auto-laden": ["ere", "morgen"],
  "kwartierprijzen-stroom": ["morgen", "prijzen"],
  "leverancier-overgenomen": ["aanbieders", "dynamisch"],
  "nadelen-dynamisch-energiecontract": ["dynamisch", "aanbieders"],
  "natrium-zoutwater-thuisbatterij": ["batterijen", "batterij"],
  "negatieve-stroomprijzen": ["morgen", "records"],
  "netbeheerkosten-capaciteitstarief": ["aanbieders", "dynamisch"],
  "netcongestie-aansluiting-2026": ["batterij", "ha"],
  "overstappen-dynamisch-energiecontract": ["aanbieders", "dynamisch"],
  "p1-data-gebruiken": ["dynamisch", "ha"],
  "p1-poort-uitlezen": ["ha", "dynamisch"],
  "salderingsregeling": ["batterij", "dynamisch"],
  "stekker-thuisbatterij-vergelijken": ["batterijen", "batterij"],
  "stroomprijzen-morgen-bekend": ["morgen", "prijzen"],
  "stroomverbruik-apparaten": ["dynamisch", "morgen"],
  "stroomverbruik-warmtepomp": ["dynamisch", "aanbieders"],
  "termijnbedrag-dynamisch-contract": ["dynamisch", "aanbieders"],
  "terugleverbeperking": ["batterij", "aanbieders"],
  "teruglevertarief-vergelijken": ["aanbieders", "batterij"],
  "thuisbatterij-brandveiligheid": ["batterijen", "batterij"],
  "thuisbatterij-energielabel": ["batterij", "batterijen"],
  "thuisbatterij-kosten": ["batterij", "batterijen"],
  "thuisbatterij-merken-vergelijken": ["batterijen", "batterij"],
  "thuisbatterij-na-2027": ["batterij", "mijnbatterij"],
  "thuisbatterij-noodstroom": ["batterijen", "batterij"],
  "thuisbatterij-onbalansmarkt": ["batterij", "mijnbatterij"],
  "thuisbatterij-subsidie-2026": ["batterij", "batterijen"],
  "thuisbatterij-winter": ["mijnbatterij", "batterij"],
  "thuisbatterij-zonder-zonnepanelen": ["batterij", "batterijen"],
  "tibber-bridge-solis-batterij": ["mijnbatterij", "batterij"],
  "v2g-bidirectioneel-laden": ["ere", "batterij"],
  "variabel-of-dynamisch-contract": ["dynamisch", "aanbieders"],
  "waarom-is-stroom-nu-zo-duur": ["prijzen", "historisch"],
  "waarom-stroomprijzen-moeilijk-te-voorspellen": ["archief", "nauwkeurigheid"],
  "warmtepomp-bij-vorst": ["dynamisch", "morgen"],
  "warmtepomp-dynamisch-contract": ["dynamisch", "ha"],
  "warmtepomp-of-cv-ketel": ["dynamisch", "aanbieders"],
  "wateraccu": ["batterij", "dynamisch"],
  "welke-thuisbatterij-past-bij-mij": ["batterij", "batterijen"],
  "zonnepanelen-uitzetten-negatieve-prijs": ["morgen", "ha"],
};

const START = "<!-- KB-TOOL:START -->";
const EIND = "<!-- KB-TOOL:END -->";
const ANKER = '<section class="kb-lees-ook">';

function blok(sleutels, nl) {
  const kaarten = sleutels.map((k) => {
    const t = TOOLS[k];
    if (!t) throw new Error(`onbekende tool: ${k}`);
    return [
      `        <a class="kb-tool-card" href="${t.href}">`,
      `          <span class="kb-tool-titel">${t.titel} →</span>`,
      `          <span class="kb-tool-tekst">${t.tekst}</span>`,
      `        </a>`,
    ].join(nl);
  });
  return [
    START,
    `    <section class="kb-tool" aria-label="Zelf narekenen">`,
    `      <p class="kb-tool-label">Zelf narekenen</p>`,
    `      <div class="kb-tool-grid">`,
    kaarten.join(nl),
    `      </div>`,
    `    </section>`,
    `    ${EIND}`,
  ].join(nl);
}

let gewijzigd = 0;
for (const f of fs.readdirSync(DIR).filter((f) => f.endsWith(".html") && f !== "index.html").sort()) {
  const naam = f.replace(/\.html$/, "");
  const p = path.join(DIR, f);
  let s = fs.readFileSync(p, "utf8");
  const nl = s.includes("\r\n") ? "\r\n" : "\n";
  const sleutels = KOPPELING[naam];
  if (!sleutels) {
    console.warn(`[let op] geen koppeling voor ${naam}`);
    continue;
  }
  const nieuw = blok(sleutels, nl);
  let uit;
  const a = s.indexOf(START);
  if (a !== -1) {
    const b = s.indexOf(EIND, a);
    uit = s.slice(0, a) + nieuw + s.slice(b + EIND.length);
  } else {
    const i = s.indexOf(ANKER);
    if (i === -1) {
      console.warn(`[let op] geen "Lees ook" in ${naam}, overgeslagen`);
      continue;
    }
    uit = s.slice(0, i) + nieuw + nl + nl + "    " + s.slice(i);
  }
  if (uit !== s) {
    fs.writeFileSync(p, uit);
    gewijzigd++;
  }
}
for (const k of Object.keys(KOPPELING)) {
  if (!fs.existsSync(path.join(DIR, k + ".html"))) console.warn(`[let op] koppeling zonder artikel: ${k}`);
}
console.log(`kb-tool-blokken: ${gewijzigd} artikelen bijgewerkt`);
