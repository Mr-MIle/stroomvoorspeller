// Geeft tellingen van de site door aan GoatCounter.
//
// POST /api/klik   body: {"e":"bp-klaar","t":"Bespaarplan: bp-klaar"}
//
// De browser praat alleen met stroomvoorspeller.nl; deze functie stuurt het
// event via de GoatCounter-API door. Zo houden adblockers (die gc.zgo.at en
// goatcounter.com tegenhouden) de telling niet meer tegen.
//
// Het IP-adres en de user-agent van de bezoeker gaan mee, zodat GoatCounter
// zelf bots filtert, unieke bezoeken telt en het land bepaalt, net als bij een
// rechtstreekse telling. GoatCounter slaat het IP-adres niet op.
//
// Vereist GOATCOUNTER_TOKEN (Vercel-omgevingsvariabele): een API-token van
// stroomvoorspeller.goatcounter.com met het recht "Record pageviews".

const API = 'https://stroomvoorspeller.goatcounter.com/api/v0/count';

// Alleen eigen eventnamen; de rest weigeren we, zodat niemand rommel in de
// statistieken kan schieten.
const TOEGESTAAN = /^(bp-[a-z0-9-]{1,60}|rekentool-[a-z0-9-]{1,30}|interesse-[a-z0-9-]{1,30})$/;

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    res.status(405).end();
    return;
  }

  let body = req.body;
  if (typeof body === 'string') {
    try { body = JSON.parse(body); } catch (e) { body = null; }
  }
  const naam = body && typeof body.e === 'string' ? body.e : '';
  if (!TOEGESTAAN.test(naam)) {
    res.status(400).end();
    return;
  }
  const titel = typeof body.t === 'string' ? body.t.slice(0, 120) : naam;

  const token = process.env.GOATCOUNTER_TOKEN;
  if (!token) {
    res.status(503).end();
    return;
  }

  const ip = String(req.headers['x-forwarded-for'] || req.headers['x-real-ip'] || '').split(',')[0].trim();
  const ua = String(req.headers['user-agent'] || '').slice(0, 512);

  try {
    const r = await fetch(API, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        no_sessions: false,
        hits: [{ path: naam, title: titel, event: true, ip, user_agent: ua }],
      }),
    });
    res.status(r.ok ? 204 : 502).end();
  } catch (e) {
    res.status(502).end();
  }
}
