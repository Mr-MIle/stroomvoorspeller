// Eenmalige vulling van het kwartierarchief public/data/archief15/YYYY-MM.json
// met NL day-ahead kwartierprijzen van Energy-Charts (Fraunhofer ISE, CC BY 4.0,
// data van Bundesnetzagentur | SMARD.de). De day-ahead-markt werkt sinds
// 1 oktober 2025 per kwartier; daarvóór bestaan er geen kwartierprijzen.
//
// Daarna vult scripts/fetch_prices.py het archief elke run aan met de
// ENTSO-E-kwartieren (append-only, zelfde formaat).
//
// Gebruik:  node scripts/backfill_archief15.mjs [YYYY-MM-van] [YYYY-MM-tot]
// Default:  2025-10 t/m de huidige maand. Bestaande tijdstempels blijven staan.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "public", "data", "archief15");
const pad = n => String(n).padStart(2, "0");
const now = new Date();
const [fromArg = "2025-10", toArg = `${now.getFullYear()}-${pad(now.getMonth() + 1)}`] = process.argv.slice(2);

// "2026-06-01T00:15:00+02:00": lokale Amsterdamse tijd met offset, net als archief/.
function amsIso(unix) {
  const d = new Date(unix * 1000);
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Amsterdam", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).formatToParts(d).map(p => [p.type, p.value]));
  const local = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
  const off = Math.round((local - d.getTime()) / 60000);
  const sign = off >= 0 ? "+" : "-";
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}${sign}${pad(Math.floor(Math.abs(off) / 60))}:${pad(Math.abs(off) % 60)}`;
}

fs.mkdirSync(DIR, { recursive: true });
let [y, m] = fromArg.split("-").map(Number);
const [ty, tm] = toArg.split("-").map(Number);
while (y < ty || (y === ty && m <= tm)) {
  const key = `${y}-${pad(m)}`;
  // ruime grenzen; daarna filteren op de maand in Amsterdamse tijd
  const nk = m === 12 ? `${y + 1}-01` : `${y}-${pad(m + 1)}`;
  const url = `https://api.energy-charts.info/price?bzn=NL&start=${key}-01&end=${nk}-01`;
  // Energy-Charts begrenst het aantal verzoeken: bij 429 even wachten en opnieuw.
  let r;
  for (let poging = 1; ; poging++) {
    r = await fetch(url, { headers: { "User-Agent": "stroomvoorspeller.nl archief" } });
    if (r.status !== 429 || poging >= 6) break;
    console.error(`[wacht] ${key}: HTTP 429, poging ${poging}, 90 s pauze`);
    await new Promise(res => setTimeout(res, 90000));
  }
  if (!r.ok) { console.error(`[fout] ${key}: HTTP ${r.status}`); process.exit(1); }
  const j = await r.json();
  const file = path.join(DIR, `${key}.json`);
  const existing = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")).prices || [] : [];
  const seen = new Set(existing.map(p => p.time.slice(0, 19)));
  let added = 0;
  j.unix_seconds.forEach((u, i) => {
    const price = j.price[i];
    if (price == null) return;
    const time = amsIso(u);
    if (time.slice(0, 7) !== key || seen.has(time.slice(0, 19))) return;
    existing.push({ time, price: Math.round(price * 100) / 100 });
    seen.add(time.slice(0, 19)); added++;
  });
  existing.sort((a, b) => (a.time < b.time ? -1 : 1));
  fs.writeFileSync(file, JSON.stringify({ month: key, updated_at: new Date().toISOString(), interval_minutes: 15, prices: existing }, null, 2));
  console.log(`[ok] ${key}: ${added} kwartieren toegevoegd (${existing.length} totaal)`);
  if (++m > 12) { m = 1; y++; }
  await new Promise(res => setTimeout(res, 6000));
}
