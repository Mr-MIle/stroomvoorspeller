// Bouwt public/data/voorspelarchief/YYYY-MM.json + index.json uit data/forecast_archive.
//
// Voor de pagina /over/voorspelarchief: per doeldag wat het model 2 t/m 7 dagen
// eerder voorspelde. De werkelijke prijzen haalt de pagina zelf uit
// /data/archief/YYYY-MM.json.
//
// Alleen snapshots vanaf 2026-09-10: vanaf die datum is elk archiefbestand op de
// dag zelf gecommit, zodat de git-geschiedenis bewijst dat de voorspelling vooraf
// bestond. Oudere bestanden (mei t/m 9 sep) kwamen op 10 sep in één keer in de
// repo en zijn daarom niet op die manier te controleren.
//
// Dag 1 laten we weg: de dagelijkse snapshot wordt pas geschreven als de
// day-ahead-prijzen voor morgen al bekend zijn (zelfde reden als op /over/performance).
//
// Gebruik: node build-voorspelarchief.js   (geen afhankelijkheden)

const fs = require("fs");
const path = require("path");

const ROOT = __dirname;
const SRC = path.join(ROOT, "data", "forecast_archive");
const OUT = path.join(ROOT, "public", "data", "voorspelarchief");
const VANAF = "2026-09-10";
const MIN_LEAD = 2;
const MAX_LEAD = 7;

const r1 = (x) => (x == null || !Number.isFinite(x) ? null : Math.round(x * 10) / 10);

function dagVerschil(a, b) {
  // a, b als "YYYY-MM-DD"; aantal kalenderdagen b - a
  return Math.round((Date.parse(b + "T12:00:00Z") - Date.parse(a + "T12:00:00Z")) / 86400000);
}

const bestanden = fs
  .readdirSync(SRC)
  .filter((f) => /^forecast_\d{4}-\d{2}-\d{2}\.json$/.test(f))
  .filter((f) => f.slice(9, 19) >= VANAF)
  .sort();

const maanden = {}; // "YYYY-MM" -> { dag -> { lead -> {...} } }

for (const f of bestanden) {
  const gemaakt = f.slice(9, 19);
  let j;
  try {
    j = JSON.parse(fs.readFileSync(path.join(SRC, f), "utf8"));
  } catch (e) {
    console.warn(`[WARN] onleesbaar: ${f}`);
    continue;
  }
  const versie = String(j.model_version || "");
  // De bestandsnaam volgt de UTC-datum. Een avondrun na middernacht Nederlandse
  // tijd heet dus nog naar de vorige dag. De horizon tellen we daarom vanaf de
  // lokale datum van vastleggen (days_ahead uit het bestand zelf waar het kan).
  const lokaal = j.generated_at
    ? new Date(j.generated_at).toLocaleDateString("sv-SE", { timeZone: "Europe/Amsterdam" })
    : gemaakt;
  const per = {}; // dag -> uren
  for (const x of j.forecasts || []) {
    if (!x.time || x.predicted == null) continue;
    const dag = x.time.slice(0, 10); // lokale datum (tijd staat met offset)
    const uur = Number(x.time.slice(11, 13));
    const lead = Number.isInteger(x.days_ahead) ? x.days_ahead : dagVerschil(lokaal, dag);
    if (lead < MIN_LEAD || lead > MAX_LEAD) continue;
    (per[dag] ||= { lead, uren: [] }).uren.push([uur, r1(x.predicted), r1(x.lower), r1(x.upper)]);
  }
  for (const [dag, { lead, uren }] of Object.entries(per)) {
    const m = dag.slice(0, 7);
    ((maanden[m] ||= {})[dag] ||= {})[lead] = {
      bestand: gemaakt, // datum in de bestandsnaam, voor de git-link
      gemaakt: lokaal,  // lokale datum van vastleggen
      om: j.generated_at || null,
      versie,
      uren,
    };
  }
}

fs.mkdirSync(OUT, { recursive: true });
const nu = new Date().toISOString();
const lijst = Object.keys(maanden).sort();
let geschreven = 0;
for (const m of lijst) {
  const dagen = {};
  for (const d of Object.keys(maanden[m]).sort()) dagen[d] = maanden[m][d];
  const uit = JSON.stringify({ maand: m, eenheid: "EUR/MWh", dagen });
  const p = path.join(OUT, `${m}.json`);
  const oud = fs.existsSync(p) ? fs.readFileSync(p, "utf8") : null;
  if (oud !== uit) {
    fs.writeFileSync(p, uit);
    geschreven++;
  }
}
const alleDagen = lijst.flatMap((m) => Object.keys(maanden[m])).sort();
const index = {
  bijgewerkt: nu,
  vanaf_snapshot: VANAF,
  eerste_dag: alleDagen[0] || null,
  laatste_dag: alleDagen[alleDagen.length - 1] || null,
  maanden: lijst,
  repo_pad: "https://github.com/mr-mile/stroomvoorspeller/commits/main/data/forecast_archive/",
};
fs.writeFileSync(path.join(OUT, "index.json"), JSON.stringify(index, null, 1));
console.log(`voorspelarchief: ${bestanden.length} snapshots, ${lijst.length} maanden, ${geschreven} gewijzigd`);
