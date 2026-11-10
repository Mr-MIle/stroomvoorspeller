// Leest de Home Assistant-telling uit (zie middleware.js).
//
// GET /api/ha-stats?token=...&dagen=30
// Beveiligd met HA_STATS_TOKEN (Vercel-omgevingsvariabele). Leest met de
// read-only Upstash-token.
//
// Per UTC-dag:
//   installaties  unieke IP's met een HomeAssistant-user-agent (≈ installaties)
//   verzoeken     geschat totaal aantal HA-verzoeken (steekproef x SAMPLE)
//   overig        geschat aantal verzoeken zonder HA-user-agent
//   versies       aandeel per HA-versie (op basis van verzoeken)

const SAMPLE = 6; // moet gelijk zijn aan SAMPLE in middleware.js

export default async function handler(req, res) {
  const url = new URL(req.url, 'http://x');
  const token = url.searchParams.get('token') || (req.headers.authorization || '').replace(/^Bearer /, '');
  const expected = process.env.HA_STATS_TOKEN;
  if (!expected || token !== expected) {
    res.status(401).json({ error: 'niet geautoriseerd' });
    return;
  }

  const kvUrl = process.env.KV_REST_API_URL;
  const kvToken = process.env.KV_REST_API_READ_ONLY_TOKEN || process.env.KV_REST_API_TOKEN;
  if (!kvUrl || !kvToken) {
    res.status(500).json({ error: 'Upstash-variabelen ontbreken' });
    return;
  }

  const n = Math.min(Math.max(parseInt(url.searchParams.get('dagen') || '30', 10) || 30, 1), 400);
  const dagen = [];
  const vandaag = Date.now();
  for (let i = 0; i < n; i++) {
    dagen.push(new Date(vandaag - i * 86400000).toISOString().slice(0, 10));
  }

  const cmds = dagen.flatMap((d) => [
    ['PFCOUNT', `ha:uniek:${d}`],
    ['GET', `ha:req:${d}`],
    ['GET', `ha:overig:${d}`],
    ['HGETALL', `ha:versie:${d}`],
  ]);

  const r = await fetch(`${kvUrl}/pipeline`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${kvToken}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(cmds),
  });
  if (!r.ok) {
    res.status(502).json({ error: `Upstash gaf ${r.status}` });
    return;
  }
  const out = await r.json();

  const rijen = dagen.map((dag, i) => {
    const [uniek, req, overig, versie] = out.slice(i * 4, i * 4 + 4).map((x) => x.result);
    const versies = {};
    const lijst = Array.isArray(versie) ? versie : [];
    let totaal = 0;
    for (let j = 0; j < lijst.length; j += 2) totaal += Number(lijst[j + 1]);
    for (let j = 0; j < lijst.length; j += 2) {
      versies[lijst[j]] = Math.round((Number(lijst[j + 1]) / totaal) * 1000) / 10;
    }
    return {
      dag,
      installaties: Number(uniek) || 0,
      verzoeken: (Number(req) || 0) * SAMPLE,
      overig: (Number(overig) || 0) * SAMPLE,
      versies_pct: versies,
    };
  });

  res.setHeader('Cache-Control', 'no-store');
  res.status(200).json({
    toelichting:
      'Dag = UTC. installaties = unieke IP-adressen met HomeAssistant-user-agent (ondergrens: meerdere installaties achter één IP tellen als één). verzoeken en overig zijn geschat uit een steekproef van 1 op ' +
      SAMPLE +
      '.',
    dagen: rijen,
  });
}
