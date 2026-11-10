// Telt Home Assistant-installaties die /data/ha.json ophalen.
//
// Draait alleen op dat ene pad. Het bestand zelf blijft statisch uit de CDN
// komen: deze middleware geeft niets terug en telt op de achtergrond
// (waitUntil), dus gebruikers merken geen vertraging.
//
// Opslag: Upstash Redis via de REST-API (KV_REST_API_URL/TOKEN, gezet door
// de Vercel-integratie). Er worden geen IP-adressen bewaard: het IP gaat
// gehasht (met geheime sleutel + datum) een HyperLogLog in, die alleen een
// schatting van het aantal unieke waarden bijhoudt.
//
// Steekproef: 1 op de SAMPLE verzoeken wordt geteld, om binnen het gratis
// Upstash-tegoed te blijven. Een installatie met de standaard scan_interval
// van 30 min komt 48x per dag langs; de kans dat die een hele dag gemist
// wordt is (5/6)^48 ≈ 0,02%. Totalen worden in /api/ha-stats teruggeschaald.

export const config = { matcher: '/data/ha.json' };

export const SAMPLE = 6;
const TTL = 400 * 86400; // sleutels verlopen na ruim een jaar

export default function middleware(request, context) {
  if (Math.random() * SAMPLE >= 1) return;
  if (!process.env.KV_REST_API_URL || !process.env.KV_REST_API_TOKEN) return;

  const task = tel(request).catch(() => {});
  if (context && typeof context.waitUntil === 'function') context.waitUntil(task);
}

export async function tel(request, now = new Date()) {
  const ua = request.headers.get('user-agent') || '';
  const ip =
    request.headers.get('x-real-ip') ||
    (request.headers.get('x-forwarded-for') || '').split(',')[0].trim() ||
    'onbekend';
  const dag = now.toISOString().slice(0, 10); // UTC-dag

  const m = ua.match(/^HomeAssistant\/(\d+)\.(\d+)/);
  let cmds;
  if (m) {
    const versie = `${m[1]}.${m[2]}`;
    const hash = await sha256(`${process.env.HA_SALT || process.env.KV_REST_API_TOKEN}|${dag}|${ip}`);
    const kUniek = `ha:uniek:${dag}`;
    const kReq = `ha:req:${dag}`;
    const kVersie = `ha:versie:${dag}`;
    cmds = [
      ['PFADD', kUniek, hash.slice(0, 32)],
      ['INCR', kReq],
      ['HINCRBY', kVersie, versie, 1],
      ['EXPIRE', kUniek, TTL],
      ['EXPIRE', kReq, TTL],
      ['EXPIRE', kVersie, TTL],
    ];
  } else {
    const kOverig = `ha:overig:${dag}`;
    cmds = [
      ['INCR', kOverig],
      ['EXPIRE', kOverig, TTL],
    ];
  }

  await fetch(`${process.env.KV_REST_API_URL}/pipeline`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${process.env.KV_REST_API_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(cmds),
  });
  return cmds;
}

async function sha256(s) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}
