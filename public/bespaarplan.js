/*
 * Bespaarplan (/bespaarplan): keuzehulp "Waar begin ik met besparen?"
 *
 * Negen vragen, daarna drie lagen maatregelen (gratis, klein geld, grote stap)
 * met kosten, opbrengst, terugverdientijd na subsidie en waar je het regelt.
 * Ontwerp en rekenkeuzes: Projects/02. Stroomvoorspeller/01-documenten/
 * bespaarplan-ontwerp-2026-11-24.md (§3b: getallen gecontroleerd 24 nov 2026).
 *
 * De antwoorden staan alleen in de URL (?e=koop&w=tus...). Er wordt niets
 * opgeslagen. Gebruik telt via GoatCounter-events (bp-*), zonder cookies, en
 * GoatCounter laadt pas bij het eerste antwoord. Zie §8 van het ontwerp.
 */
(() => {
const app = document.getElementById('bp-app');
if (!app) return;
const KB = '/kennisbank/';
const C = { gas: 1.35, stroom: 0.27, terug: 0.05, kwhPerM3: 8.8, scopHyb: 3.5, scopWp: 3.0, co2Gas: 1.78, co2Kwh: 0.25 };
/* Milieu Centraal, opgehaald 24-11-2026: van geen naar goede isolatie, matig geïsoleerde woning.
   [m³ gas/jr, kosten €, ISDE € bij 1 maatregel]. Appartement = halve tussenwoning (eigen aanname). */
const MC = {
  spouw: { app: [90, 500, 100], tus: [180, 800, 200], hoek: [400, 1800, 450], '2k': [400, 1900, 450], vrij: [600, 2700, 700] },
  dak:   { tus: [320, 6000, 850], hoek: [340, 6500, 900], '2k': [360, 6500, 950], vrij: [550, 10000, 1500] },
  vloer: { tus: [80, 2150, 260], hoek: [130, 2240, 280], '2k': [170, 2900, 350], vrij: [250, 4200, 500] },
  /* glas: MC noemt alleen hoekwoning (22 m²): HR++ € 4.700; t.o.v. dubbel glas 70 m³, t.o.v. enkel glas 260 m³.
     Andere typen geschaald met glasoppervlak (eigen aanname); ISDE € 25/m². */
  glasFac: { app: 0.6, tus: 0.8, hoek: 1, '2k': 1.1, vrij: 1.5 },
};
const RVO_ISOL = 'https://www.rvo.nl/subsidies-financiering/isde/woningeigenaren/isolatiemaatregelen';

const Q = [
  { key: 'e', step: 'Je huis', title: 'Woon je in een koop- of huurhuis?', sub: 'Dat bepaalt wat je zelf mag aanpassen.', opts: [
    ['koop', '🔑', 'Koophuis', 'Je beslist zelf'], ['vve', '🏢', 'Koop, in een VvE', 'Dak en gevel gaan via de VvE'], ['huur', '📄', 'Huurhuis', 'Er is meer mogelijk dan je denkt'] ] },
  { key: 'w', step: 'Je huis', title: 'Wat voor huis is het?', opts: [
    ['app', '🏢', 'Appartement'], ['tus', '🏘️', 'Tussenwoning'], ['hoek', '🏠', 'Hoekwoning'], ['2k', '🏡', 'Twee-onder-een-kap'], ['vrij', '🏰', 'Vrijstaand'] ] },
  { key: 'b', step: 'Je huis', title: 'Wanneer is het ongeveer gebouwd?', sub: 'Een schatting is goed genoeg. Het staat vaak op je WOZ-beschikking.', opts: [
    ['1', '🧱', 'Vóór 1945'], ['2', '🧱', '1945 – 1974', 'Vaak een lege spouwmuur'], ['3', '🧱', '1975 – 1991'], ['4', '🧱', '1992 – 2005'], ['5', '✨', 'Na 2005'] ] },
  { key: 'p', step: 'Je huis', title: 'Met hoeveel mensen woon je er?', sub: 'Nodig voor warm water en stroomverbruik.', opts: [
    ['1', '🙂', '1 persoon'], ['2', '👫', '2 personen'], ['3', '👨‍👩‍👦', '3 personen'], ['4', '👨‍👩‍👧‍👦', '4 of meer'] ] },
  { key: 'v', step: 'Je huis', title: 'Hoe verwarm je je huis?', opts: [
    ['cv', '🔥', 'Cv-ketel op gas'], ['hyb', '🌗', 'Hybride warmtepomp', 'Warmtepomp + cv-ketel'], ['wp', '🌀', 'Warmtepomp', 'Helemaal zonder gas'], ['sv', '🏭', 'Stadsverwarming'] ] },
  { key: 'h', multi: true, step: 'Wat heb je al?', title: 'Wat heb je al?', sub: 'Vink alles aan wat klopt. Niets? Ook prima.', opts: [
    ['led', '💡', 'Overal LED'], ['spouw', '🧱', 'Spouwmuur geïsoleerd'], ['dak', '🏠', 'Dak geïsoleerd'], ['vloer', '⬇️', 'Vloer geïsoleerd'], ['glas', '🪟', 'HR++ of triple glas'], ['enkel', '🥶', 'Nog (deels) enkel glas'],
    ['zp', '☀️', 'Zonnepanelen'], ['bat', '🔋', 'Thuisbatterij'], ['ev', '🚗', 'Elektrische auto'], ['p1', '📟', 'Energiemeter op P1-poort'], ['dyn', '📈', 'Dynamisch contract'] ] },
  { key: 'n', nums: true, step: 'Wat heb je al?', title: 'Hoeveel gebruik je per jaar?', sub: 'Dat staat op je jaarafrekening. Weet je het niet? Sla over, dan schatten we het.' },
  { key: 'd', step: 'Wat wil je?', title: 'Wat is voor jou het belangrijkst?', opts: [
    ['gas', '🔥', 'Minder gas', 'Van het gas af, stap voor stap'], ['geld', '💶', 'Lagere rekening', 'Wat levert het meeste op?'], ['zelf', '🔌', 'Minder afhankelijk', 'Zelf opwekken en bewaren'], ['klimaat', '🌍', 'Beter voor het klimaat', 'Meeste CO₂ per euro'] ] },
  { key: 'k', step: 'Wat wil je?', title: 'Hoeveel wil je de komende tijd uitgeven?', sub: 'Inclusief subsidie die je later terugkrijgt. Lenen kan ook, daar komen we op.', opts: [
    ['0', '🪙', 'Niks', 'Alleen gratis tips'], ['1', '💶', 'Tot € 500'], ['2', '💰', 'Tot € 5.000'], ['3', '🏦', 'Meer dan € 5.000'] ] },
];
const KEYS = ['e', 'w', 'b', 'p', 'v', 'd', 'k', 'g', 's'];
const NODIG = ['e', 'w', 'b', 'p', 'v', 'd', 'k'];
const GELDIG = {};
Q.forEach(q => { if (q.opts) GELDIG[q.key] = q.opts.map(o => o[0]); });

const st = { h: new Set() };
let idx = 0;
let viaUrl = false;   // plan geopend via een gedeelde of bewaarde link
const eur = n => '€ ' + Math.round(n).toLocaleString('nl-NL');
const r50 = n => n >= 1000 ? Math.round(n / 100) * 100 : Math.round(n / 10) * 10;   // geen schijnprecisie
const range = ([a, b]) => r50(a) === r50(b) ? eur(r50(a)) : eur(r50(a)) + ' – ' + r50(b).toLocaleString('nl-NL');

/* ---------- tellen (GoatCounter, zonder cookies) ---------- */
const TELLER = 'https://stroomvoorspeller.goatcounter.com/count';
const BRON = 'https://gc.zgo.at/count.js';
const geteld = new Set();
let wacht = [], laden = false;
function stuur(n) { try { window.goatcounter.count({ path: n, title: 'Bespaarplan: ' + n, event: true }); } catch (e) {} }
function tel(naam) {
  if (geteld.has(naam)) return;
  geteld.add(naam);
  if (window.goatcounter && window.goatcounter.count) return stuur(naam);
  wacht.push(naam);
  if (laden) return;
  laden = true;
  window.goatcounter = window.goatcounter || {};
  window.goatcounter.no_onload = true;
  const s = document.createElement('script');
  s.async = true; s.src = BRON; s.setAttribute('data-goatcounter', TELLER);
  s.onload = () => { const w = wacht; wacht = []; w.forEach(stuur); };
  document.head.appendChild(s);
}

/* ---------- URL ---------- */
function toUrl() {
  const p = new URLSearchParams();
  KEYS.forEach(k => { if (st[k] != null && st[k] !== '') p.set(k, st[k]); });
  if (st.h.size) p.set('h', [...st.h].join(','));
  try { history.replaceState(null, '', location.pathname + '?' + p.toString()); } catch (e) {}
}
function fromUrl() {
  const p = new URLSearchParams(location.search);
  KEYS.forEach(k => {
    if (!p.has(k)) return;
    const v = p.get(k);
    if (GELDIG[k] ? GELDIG[k].includes(v) : /^\d{1,6}$/.test(v)) st[k] = v;
  });
  const hOk = Q.find(q => q.key === 'h').opts.map(o => o[0]);
  if (p.get('h')) p.get('h').split(',').forEach(x => { if (hOk.includes(x)) st.h.add(x); });
  return NODIG.every(k => st[k] != null);
}

/* ---------- wizard ---------- */
function renderQ(focus) {
  const q = Q[idx];
  const pct = Math.round((idx + 1) / (Q.length + 1) * 100);
  let body = '';
  if (q.nums) {
    body = `<div class="bp-nums">
      <div><label for="bp-g">Gas</label><div class="bp-in"><input id="bp-g" inputmode="numeric" autocomplete="off" placeholder="bijv. 1200" value="${st.g || ''}"><span class="bp-unit">m³</span></div></div>
      <div><label for="bp-s">Stroom</label><div class="bp-in"><input id="bp-s" inputmode="numeric" autocomplete="off" placeholder="bijv. 2800" value="${st.s || ''}"><span class="bp-unit">kWh</span></div></div>
    </div><p class="bp-hint">Heb je zonnepanelen? Vul dan je stroom ván het net in, zoals op de afrekening.</p>
    <div class="bp-actions"><button type="button" class="bp-btn ghost" data-skip>Weet ik niet, schat het</button><button type="button" class="bp-btn prim" data-next>Verder →</button></div>`;
  } else {
    body = `<div class="bp-opts" role="group" aria-label="${q.title}">` + q.opts.map(([v, ic, l, s]) => {
      const on = q.multi ? st.h.has(v) : st[q.key] === v;
      return `<button type="button" class="bp-opt" data-v="${v}" aria-pressed="${on}"><span class="ic" aria-hidden="true">${ic}</span><span><b>${l}</b>${s ? `<small>${s}</small>` : ''}</span>${q.multi ? '<span class="tick" aria-hidden="true"></span>' : ''}</button>`;
    }).join('') + '</div>';
    if (q.multi) body += `<div class="bp-actions"><span></span><button type="button" class="bp-btn prim" data-next>${st.h.size ? 'Verder →' : 'Niets hiervan, verder →'}</button></div>`;
  }
  app.innerHTML = `<section class="bp-card" aria-labelledby="bp-qt">
    <div class="bp-top"><button type="button" class="bp-back" ${idx ? '' : 'hidden'} aria-label="Vorige vraag" data-back>←</button>
      <div class="bp-prog" role="progressbar" aria-valuemin="1" aria-valuemax="${Q.length}" aria-valuenow="${idx + 1}" aria-label="Voortgang"><i style="width:${pct}%"></i></div><span class="bp-count">${idx + 1} / ${Q.length}</span></div>
    <p class="bp-step">${q.step}</p><h2 class="bp-q" id="bp-qt" tabindex="-1">${q.title}</h2>${q.sub ? `<p class="bp-sub">${q.sub}</p>` : '<div class="bp-gap"></div>'}
    ${body}</section>`;
  if (focus) app.querySelector('#bp-qt').focus({ preventScroll: true });
}

function next() {
  tel('bp-v' + (idx + 1));
  idx++;
  toUrl();
  if (idx >= Q.length) renderResult(); else renderQ(true);
  const top = app.getBoundingClientRect().top + window.scrollY - 80;
  if (window.scrollY > top) window.scrollTo({ top, behavior: 'smooth' });
}

app.addEventListener('click', e => {
  const a = e.target.closest('a[data-ev]');
  if (a) { tel(a.dataset.ev); return; }
  const t = e.target.closest('button'); if (!t) return;
  if (t.hasAttribute('data-back')) { idx = Math.max(0, idx - 1); return renderQ(true); }
  if (t.hasAttribute('data-edit')) { viaUrl = false; idx = 0; renderQ(true); app.scrollIntoView({ behavior: 'smooth' }); return; }
  if (t.hasAttribute('data-print')) { tel('bp-print'); return window.print(); }
  if (t.hasAttribute('data-share')) { tel('bp-deel'); return kopieer(t); }
  if (t.dataset.nuttig) return duim(t);
  const q = Q[idx]; if (!q) return;
  tel('bp-start');
  if (t.hasAttribute('data-skip')) { tel('bp-schat'); st.g = ''; st.s = ''; return next(); }
  if (t.hasAttribute('data-next')) {
    if (q.nums) {
      st.g = (app.querySelector('#bp-g').value || '').replace(/\D/g, '').slice(0, 6);
      st.s = (app.querySelector('#bp-s').value || '').replace(/\D/g, '').slice(0, 6);
      if (!st.g || !st.s) tel('bp-schat');
    }
    return next();
  }
  if (t.dataset.v) {
    if (q.multi) {
      st.h.has(t.dataset.v) ? st.h.delete(t.dataset.v) : st.h.add(t.dataset.v);
      t.setAttribute('aria-pressed', String(st.h.has(t.dataset.v)));
      app.querySelector('[data-next]').textContent = st.h.size ? 'Verder →' : 'Niets hiervan, verder →';
      return;
    }
    st[q.key] = t.dataset.v;
    app.querySelectorAll('.bp-opt').forEach(b => b.setAttribute('aria-pressed', String(b === t)));
    setTimeout(next, 170);
  }
});
app.addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); const b = app.querySelector('[data-next]'); if (b) b.click(); }
});
app.addEventListener('toggle', e => {
  const d = e.target;
  if (d.tagName !== 'DETAILS' || !d.dataset.id) return;
  if (d.hasAttribute('data-auto')) { d.removeAttribute('data-auto'); return; }   // stond al open bij het tonen
  if (d.open) tel('bp-open-' + d.dataset.id);
}, true);
window.addEventListener('beforeprint', () => app.querySelectorAll('details').forEach(d => { d.open = true; }));

function kopieer(t) {
  const klaar = () => { t.textContent = 'Link gekopieerd ✓'; };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(location.href).then(klaar, () => prompt('Kopieer deze link:', location.href));
  else prompt('Kopieer deze link:', location.href);
}
function duim(t) {
  const v = t.dataset.nuttig, box = t.closest('.bp-duim');
  if (v === 'ja') { tel('bp-nuttig-ja'); box.innerHTML = '<span>Dank je! Dat helpt om dit beter te maken.</span>'; return; }
  if (v === 'nee') {
    tel('bp-nuttig-nee');
    box.innerHTML = '<span>Jammer. Wat miste je?</span><button type="button" data-nuttig="vaag">Te vaag</button><button type="button" data-nuttig="klopt">Klopt niet voor mijn huis</button><button type="button" data-nuttig="anders">Iets anders</button>';
    return;
  }
  tel('bp-nuttig-nee-' + v);
  box.innerHTML = '<span>Dank je, daar ga ik mee aan de slag.</span>';
}

/* ---------- rekenen ---------- */
function model() {
  const has = x => st.h.has(x);
  const p = +st.p || 2, b = +st.b || 3, w = st.w || 'tus', v = st.v || 'cv';
  const base = { app: 650, tus: 900, hoek: 1050, '2k': 1250, vrij: 1650 }[w];
  const fac = [0, 1.3, 1.2, 1.0, 0.8, 0.6][b];
  const nIsol = ['spouw', 'dak', 'vloer', 'glas'].filter(has).length;
  const heatDemand = base * fac * (1 - 0.08 * nIsol);            // in m³-gas-equivalent
  const wwDemand = Math.max(120, 70 * p);
  let heatGas = v === 'cv' ? heatDemand : v === 'hyb' ? heatDemand * 0.4 : 0;
  let wwGas = (v === 'cv' || v === 'hyb') ? wwDemand : 0;
  const gasGiven = +st.g > 0;
  if (gasGiven) { const g = +st.g; wwGas = (v === 'cv' || v === 'hyb') ? Math.min(wwDemand, g * 0.4) : 0; heatGas = Math.max(0, g - wwGas); }
  const stroom = +st.s > 0 ? +st.s : ({ 1: 1600, 2: 2500, 3: 3100, 4: 3600 }[p] + (has('ev') ? 2500 : 0)
      + (v === 'wp' ? (heatDemand + wwDemand) * C.kwhPerM3 / C.scopWp : 0) + (v === 'hyb' ? heatDemand * 0.6 * C.kwhPerM3 / C.scopHyb : 0));
  const heatRef = v === 'cv' || v === 'hyb' ? heatGas : heatDemand;
  const noGas = v === 'wp' || v === 'sv';
  return { has, p, b, w, v, heatDemand, heatGas, wwGas, gas: heatGas + wwGas, stroom, gasGiven: gasGiven || noGas, stroomGiven: +st.s > 0, heatRef };
}

function measures(M) {
  const { has, b, w, v } = M; const out = [];
  const gasE = m3 => m3 * C.gas, kwhE = k => k * C.stroom;
  const add = o => out.push(Object.assign({ gas: 0, kwh: 0, kosten: [0, 0], subsidie: 'Geen' }, o));
  const isGas = v === 'cv' || v === 'hyb';
  const koop = st.e === 'koop';

  /* laag 1 */
  if (isGas) add({ id: 'cv55', laag: 1, ic: '🌡️', t: 'Zet je cv-ketel op 55 °C', why: 'Bij koeler water haalt je HR-ketel zijn volle rendement. En het is meteen een gratis test of je huis klaar is voor een warmtepomp.',
    gas: M.heatGas * 0.03, how: ['Zoek in de handleiding van je ketel "aanvoertemperatuur" (vaak een knop met een radiator-icoon).', 'Zet hem van 70 of 80 °C op 55 °C. Het kraanwater blijft op 60 °C.', 'Blijft het op een koude dag warm genoeg? Dan past waarschijnlijk een hybride warmtepomp. Lukt het op 50 °C, dan ook een volledige.'],
    where: 'Zelf, op je ketel. Kost 10 minuten.', kb: ['gas-besparen-zonder-investeren#cv-ketel', 'Zo stel je je ketel in'] });
  if (M.heatRef > 0) add({ id: 'thermo', laag: 1, ic: '🎚️', t: 'Thermostaat een graad lager', why: isGas ? 'De verwarming is je grootste gaspost. Elke graad lager merk je direct op de rekening.' : 'Ook met een warmtepomp scheelt elke graad lager op je stroomrekening.',
    gas: isGas ? M.heatGas * 0.06 : 0, kwh: isGas ? 0 : M.heatDemand * C.kwhPerM3 / C.scopWp * 0.06,
    how: ['Overdag een graad lager, bijvoorbeeld 19 in plaats van 20 °C.', isGas ? 'Met een cv-ketel en radiatoren: \'s nachts en als je weg bent 15 °C.' : 'Met een warmtepomp of vloerverwarming: \'s nachts hooguit 1 of 2 graden lager.'],
    where: 'Zelf, op je thermostaat.', kb: ['gas-besparen-zonder-investeren#thermostaat', 'Gas besparen zonder te investeren'] });
  if (M.wwGas > 0) add({ id: 'douche', laag: 1, ic: '🚿', t: 'Douche 5 minuten in plaats van 7½', why: `Warm water kost jullie zo'n ${Math.round(M.wwGas)} m³ gas per jaar. Korter douchen scheelt volgens Milieu Centraal zo'n € 36 per persoon.`,
    gas: 26 * M.p, how: ['Een douchetimer of een liedje van 5 minuten helpt.'], where: 'Zelf.', kb: ['gas-besparen-zonder-investeren#douche', 'Meer over douchen'] });
  add({ id: 'sluip', laag: 1, ic: '🔌', t: 'Spoor sluipverbruik op', why: 'Apparaten op standby gebruiken in een gemiddeld huis 300 tot 500 kWh per jaar. Dat is € 80 tot € 135, voor niets.',
    kwh: 150, how: ['Kijk \'s nachts (alles uit) op je slimme meter hoeveel watt er doorloopt.', 'Schakel kastjes één voor één uit: tv-ontvanger, oude vriezer, router-spullen, laders.', 'Zet een stekkerdoos met schakelaar bij de tv-hoek.'],
    where: 'Zelf, met je slimme meter of P1-meter.', kb: ['led-en-sluipverbruik#sluipverbruik', 'Zo vind je sluipverbruik'] });
  if (!has('dyn')) { const actief = has('ev') || has('bat') || v !== 'cv'; add({ id: 'dyn', laag: 1, ic: '📈', t: 'Overstappen naar een dynamisch contract', why: actief ? 'Met een warmtepomp, auto of batterij kun je je grote verbruik naar de goedkope uren schuiven.' : 'Ook zonder grote apparaten scheelt het iets, als je de was in de goedkope uren doet.',
    kwh: 0, euro: actief ? 175 : 50, how: ['Reken eerst met je eigen verbruik uit of het voor jou loont.', 'Vergelijk de opslag en vaste kosten per aanbieder.', 'Let op: in een koude, windstille winterweek kan het duurder uitvallen.'],
    where: 'Online overstappen bij een aanbieder; je oude contract opzeggen gaat vanzelf (let op een opzegboete bij een vast contract).', cta: ['/dynamisch-berekenen', 'Reken het uit'], kb: ['variabel-of-dynamisch-contract', 'Variabel of dynamisch?'] }); }
  else add({ id: 'schuif', laag: 1, ic: '⏰', t: 'Was, droog en laad in de goedkope uren', why: 'Je hebt al een dynamisch contract. Haal er alles uit.', euro: 60,
    how: ['Kijk na 13:15 naar de prijzen van morgen.', 'Zet vaatwasser en wasmachine op uitgestelde start.'], where: 'Stroomvoorspeller.nl, elke dag.', cta: ['/morgen', 'Prijzen van morgen'] });
  if (has('ev')) add({ id: 'ere', laag: 1, ic: '🚗', t: 'Laat je thuisladen iets opleveren', why: 'Voor elke kWh die je thuis laadt, kun je ERE-certificaten laten verkopen.', euro: null,
    how: ['Vergelijk de aanbieders op uitbetaling en voorwaarden.', 'Je hebt een laadpaal met een MID-meter nodig.'], where: 'Online, bij een ERE-aanbieder.', cta: ['/ere-vergelijken', 'ERE-aanbieders vergelijken'], kb: ['ere-certificaten-laadpaal', 'ERE-certificaten uitgelegd'] });

  /* laag 2 */
  if (!has('led')) add({ id: 'led', laag: 2, ic: '💡', t: 'Vervang de laatste gloei- en halogeenlampen', why: 'Een halogeenspot van 35 W wordt een LED van 4 W. Tien spots die 3 uur per dag branden: zo\'n 300 kWh per jaar.',
    kwh: 300, kosten: [60, 120], how: ['Begin bij de lampen die het langst branden (keuken, woonkamer).', 'Let op lumen (lichtsterkte) en 2700 K voor warm licht.'], where: 'Bouwmarkt of online. Vervangen doe je zelf.', kb: ['led-en-sluipverbruik#led', 'LED en sluipverbruik'] });
  if (M.heatGas > 0) add({ id: 'tocht', laag: 2, ic: '🧣', t: 'Tochtstrips en radiatorfolie', why: 'Kieren bij deuren en ramen en een koude muur achter de radiator kosten warmte.',
    gas: M.heatGas * 0.04, kosten: [60, 120], how: ['Tochtstrips rond ramen en buitendeuren, een borstel onder de voordeur.', 'Radiatorfolie achter radiatoren op een buitenmuur.', 'Een borstel in de brievenbus.'], where: 'Bouwmarkt. Zelf aan te brengen in een middag.', kb: ['gas-besparen-zonder-investeren#kleine-dingen', 'Radiatoren, gordijnen en tocht'] });
  if (M.wwGas > 0) add({ id: 'douchekop', laag: 2, ic: '🚿', t: 'Waterbesparende douchekop', why: 'Zelfde douchegevoel, zo\'n 20% minder warm water. Milieu Centraal rekent met zo\'n € 25 per persoon per jaar.', gas: 18 * M.p, kosten: [20, 40],
    how: ['Kies een kop met maximaal 7,2 liter per minuut.'], where: 'Bouwmarkt. Zelf vastschroeven.' });
  if (!has('p1')) add({ id: 'p1', laag: 2, ic: '📟', t: 'Een meter op je P1-poort', why: 'Zie per minuut wat je huis verbruikt. Zo kun je alle andere stappen meten.', euro: null, kosten: [30, 60],
    how: ['Steek een P1-meter in de poort van je slimme meter.', 'Koppel hem aan de app of aan Home Assistant.'], where: 'Online te koop; zelf in te pluggen.', kb: ['p1-poort-uitlezen', 'P1-poort uitlezen'] });
  if (!has('zp') && (st.e !== 'koop' || w === 'app')) add({ id: 'balkon', laag: 2, ic: '🌤️', t: 'Balkonzonnepanelen', why: 'Geen dak nodig. Een set van 800 watt levert zo\'n 600 kWh per jaar, die je grotendeels meteen zelf gebruikt.',
    euro: 600 * 0.7 * C.stroom + 600 * 0.3 * C.terug, kosten: [500, 800], how: ['Kijk of je balkon of tuin op het zuiden, oosten of westen ligt.', st.e === 'huur' ? 'Vraag je verhuurder om toestemming voor de bevestiging.' : 'Check het reglement van je VvE.', 'Meld de panelen aan op energieleveren.nl.'],
    where: 'Winkel of online; aanmelden op energieleveren.nl.', cta: ['https://www.energieleveren.nl', 'energieleveren.nl'], kb: ['balkonzonnepanelen-dynamisch-contract', 'Balkonpanelen uitgelegd'] });
  if (has('zp') && !has('bat')) add({ id: 'stekkerbat', laag: 2, ic: '🔋', t: 'Stekkerbatterij', why: 'Na 2027 is zelf gebruiken meer waard dan terugleveren. Een stekkerbatterij is de goedkope instap.', euro: 170, kosten: [1000, 1400],
    how: ['Kies 2 tot 3 kWh; groter heeft bij een stekker weinig zin.', 'Met een dynamisch contract laadt hij ook in goedkope uren.'], where: 'Online te koop, in het stopcontact.', kb: ['stekker-thuisbatterij-vergelijken', 'Stekkerbatterijen vergeleken'] });

  /* laag 3 */
  const isdeNa = 'ISDE vraag je aan bij RVO ná uitvoering, binnen 24 maanden. Doe je 2 maatregelen (of isolatie + warmtepomp) binnen 24 maanden, dan verdubbelt het bedrag.';
  /* MC-getallen gelden voor een matig geïsoleerd huis. Gebruik je minder gas, dan schalen we de besparing mee. */
  const ref = { app: 650, tus: 900, hoek: 1050, '2k': 1250, vrij: 1650 }[w] * 1.2;
  const sf = Math.min(1.3, Math.max(0.4, M.heatGas / ref));
  const mc = soort => { const r = (MC[soort][w] || MC[soort].tus); return { gas: r[0] * sf, kosten: [r[1] * 0.85, r[1] * 1.15], sub: r[2] }; };
  const subLbl = s => eur(r50(s)) + (s ? ' (×2)' : '');
  const isolKb = ['https://www.milieucentraal.nl/energie-besparen/isoleren-en-besparen/', 'Achtergrond bij Milieu Centraal'];
  if (koop) {
    if (!has('spouw') && b <= 2) { const x = mc('spouw'); add({ id: 'spouw', laag: 3, isol: 1, ic: '🧱', t: 'Spouwmuur laten vullen', why: 'Huizen van vóór 1975 hebben vaak een lege spouw. Vullen is de goedkoopste grote stap die er is.',
      gas: x.gas, kosten: x.kosten, subsidie: subLbl(x.sub), subMid: x.sub, subTxt: isdeNa + ' Spouwmuur: € 5,25 per m².',
      how: ['Laat 2 of 3 offertes maken door gecertificeerde isolatiebedrijven (vraag naar een spouwscan).', 'Vraag na uitvoering ISDE aan bij RVO.', 'Check bij je gemeente of er extra subsidie of een collectieve actie is.'],
      where: 'Isolatiebedrijf; subsidie bij RVO; gratis advies bij je energieloket.', cta: [RVO_ISOL, 'ISDE isolatie bij RVO', 'isde'], kb: isolKb }); }
    if (!has('dak') && w !== 'app' && b <= 3) { const x = mc('dak'); add({ id: 'dak', laag: 3, isol: 1, ic: '🏠', t: 'Dak isoleren', why: 'Warmte stijgt op. Een ongeïsoleerd dak is vaak het grootste lek, en na de spouwmuur de grootste gaswinst.',
      gas: x.gas, kosten: x.kosten, subsidie: subLbl(x.sub), subMid: x.sub, subTxt: isdeNa + ' Dak: € 16,25 per m².',
      how: ['Isolatie aan de binnenkant van een schuin dak is het gebruikelijkst; laat 2 of 3 offertes maken.', 'Handig? Zelf doen kost aan materiaal zo\'n € 1.700 (Milieu Centraal).', 'Combineer met spouw of vloer binnen 24 maanden voor dubbele subsidie.'], where: 'Isolatiebedrijf of dakdekker; subsidie bij RVO.', cta: [RVO_ISOL, 'ISDE isolatie bij RVO', 'isde'], kb: isolKb }); }
    if (!has('vloer') && b <= 3 && w !== 'app') { const x = mc('vloer'); add({ id: 'vloer', laag: 3, isol: 1, ic: '⬇️', t: 'Vloer of bodem isoleren', why: 'Kruipruimte? Dan merk je het meteen aan je voeten, en het telt mee voor dubbele subsidie.',
      gas: x.gas, kosten: x.kosten, subsidie: subLbl(x.sub), subMid: x.sub, subTxt: isdeNa + ' Vloer: € 5,50 per m², bodem: € 3 per m².',
      how: ['Kijk of je een kruipruimte hebt van minstens 35 cm hoog.', 'Laat 2 of 3 offertes maken.'], where: 'Isolatiebedrijf; subsidie bij RVO.', cta: [RVO_ISOL, 'ISDE isolatie bij RVO', 'isde'], kb: isolKb }); }
    if (!has('glas')) { const gf = MC.glasFac[w], enkel = has('enkel'), m2 = 22 * gf;
      add({ id: 'glas', laag: 3, isol: 1, ic: '🪟', t: enkel ? 'Enkel glas vervangen door HR++' : 'Dubbel glas vervangen door HR++', why: enkel ? 'Enkel glas is het grootste raamlek dat er is. Vervangen voelt meteen warmer.' : 'Van gewoon dubbel glas naar HR++ scheelt weinig gas. Doe het vooral voor comfort, of als je kozijnen toch aan vervanging toe zijn.',
      gas: (enkel ? 260 : 70) * gf * sf, kosten: [4700 * gf * 0.85, 4700 * gf * 1.15], subsidie: subLbl(25 * m2), subMid: 25 * m2,
      subTxt: isdeNa + ' HR++: € 25 per m² (2027: € 30). Triple glas in nieuwe kozijnen: € 111 per m² in 2026, € 55 vanaf 2027.',
      how: ['Begin bij de grootste ramen in de woonkamer.', 'Triple glas met nieuwe kozijnen nog in 2026 bestellen scheelt de helft aan subsidie.'], where: 'Glaszetter; subsidie bij RVO.', cta: [RVO_ISOL, 'ISDE isolatie bij RVO', 'isde'], kb: isolKb }); }
    if (v === 'cv') { const m3 = M.heatGas * 0.6, k = m3 * C.kwhPerM3 / C.scopHyb; add({ id: 'hybride', laag: 3, ic: '🌗', t: 'Hybride warmtepomp', why: 'Doet het meeste stookwerk met stroom; de cv-ketel springt alleen bij op koude dagen. Je gas daalt met zo\'n 60%.',
      gas: m3, kwh: -k, kosten: [4500, 7000], subsidie: '± € 1.500', subMid: 1500, subTxt: 'ISDE voor een 4 kW lucht-water warmtepomp: vanaf 2027 € 1.025 + € 120 per kW, zo\'n € 1.500, plus € 200 als hij slim kan sturen en € 250 bij label A+++. Gekocht vóór 31 december 2026? Dan geldt nog € 225 per kW (zo\'n € 1.900). Isoleer je binnen 24 maanden, dan verdubbelt de isolatiesubsidie.',
      prereq: b <= 2 && !has('spouw') && !has('dak'),
      how: ['Doe eerst de 55 °C-test hierboven.', 'Laat een installateur een warmteverliesberekening maken en vraag 2 of 3 offertes.', 'Vraag na installatie ISDE aan bij RVO.'], where: 'Installateur; subsidie bij RVO; lenen kan bij het Warmtefonds.', cta: [KB + 'isde-subsidie-warmtepomp-2027', 'Subsidie: 2026 of 2027?'], kb: ['warmtepomp-dynamisch-contract', 'Warmtepomp op dynamisch'] }); }
    if (v === 'cv' || v === 'hyb') { const m3 = M.gas, k = (M.heatDemand + Math.max(120, 70 * M.p)) * C.kwhPerM3 / C.scopWp - (v === 'hyb' ? M.heatDemand * 0.6 * C.kwhPerM3 / C.scopHyb : 0);
      add({ id: 'wp', laag: 3, ic: '🌀', t: 'Volledige warmtepomp', why: 'Helemaal van het gas af. Daarna kun je ook je gasaansluiting opzeggen; dat scheelt nog eens zo\'n € 250 per jaar aan vaste kosten.',
      gas: m3, kwh: -k, kosten: [11000, 16000], subsidie: '± € 2.000', subMid: 2000, subTxt: 'ISDE voor een 8 kW lucht-water warmtepomp: vanaf 2027 € 1.025 + € 120 per kW, zo\'n € 2.000, plus € 200 (slim) en € 250 (A+++). Gekocht vóór 31 december 2026: zo\'n € 2.800. Reken eventueel € 3.500 extra voor grotere radiatoren of vloerverwarming (Milieu Centraal).',
      prereq: !(b >= 4 || (has('spouw') && has('dak') && has('glas'))),
      how: ['Je huis moet goed geïsoleerd zijn; de 50 °C-test op je ketel is een eerste check.', 'Laat een warmteverliesberekening maken (hoort bij een goede offerte).', 'Houd rekening met ruimte voor een boilervat.'], where: 'Installateur; subsidie bij RVO; lenen bij het Warmtefonds.', cta: [KB + 'warmtepomp-of-cv-ketel', 'Warmtepomp of cv-ketel?'], kb: ['stroomverbruik-warmtepomp', 'Stroomverbruik warmtepomp'] }); }
    if (!has('zp') && w !== 'app') { const prod = 3650, self = (has('ev') || v !== 'cv') ? 0.4 : 0.3;
      add({ id: 'zp', laag: 3, ic: '☀️', t: 'Zonnepanelen (± 10 stuks)', why: 'Ook zonder saldering lonen ze nog, vooral als je overdag veel verbruikt. Deze opbrengst rekent al zonder saldering.',
      euro: prod * self * C.stroom + prod * (1 - self) * C.terug, kwh: 0, kwhSaved: prod * self, kosten: [4500, 6000], subsidie: 'Geen', subTxt: 'Geen subsidie, wel 0% btw.',
      how: ['Laat 2 of 3 offertes maken; let op de terugleverkosten van je energieleverancier.', 'Meld de panelen aan op energieleveren.nl.'], where: 'Installateur; aanmelden op energieleveren.nl.', cta: ['/batterij-berekenen', 'Reken met je eigen dak'], kb: ['teruglevertarief-vergelijken', 'Terugleverkosten vergelijken'] }); }
    if (has('zp') && M.wwGas > 0) add({ id: 'wateraccu', laag: 3, ic: '💧', t: 'Wateraccu', why: 'Stuur je zonoverschot naar een elektrische boiler in plaats van het voor een paar cent terug te leveren. Veel goedkoper dan een thuisbatterij.',
      euro: 120, gas: 0, kosten: [1000, 1700], how: ['Je hebt ruimte nodig voor een boiler van 150 tot 200 liter.', 'Laat een installateur de regelunit en boiler aansluiten.'], where: 'Installateur of handige elektricien.', cta: [KB + 'wateraccu', 'Wateraccu uitgelegd'] });
    if (has('zp') && !has('bat')) add({ id: 'batterij', laag: 3, ic: '🔋', t: 'Thuisbatterij (10 kWh)', why: 'Bewaart je zonnestroom voor de avond en laadt op goedkope uren. Na 2027 levert hij meer op dan nu, maar terugverdienen blijft lastig.',
      euro: 330, kosten: [5500, 8500], how: ['Reken eerst met je eigen panelen en verbruik.', 'Vergelijk merken op prijs per kWh en garantie.'], where: 'Installateur; eerst zelf doorrekenen.', cta: ['/batterij-berekenen', 'Bereken jouw opbrengst'], kb: ['thuisbatterij-na-2027', 'Thuisbatterij na 2027'] });
  }
  if (st.e === 'huur') add({ id: 'verhuurder', laag: 3, ic: '📄', t: 'Vraag je verhuurder om isolatie', why: 'Isoleren en een warmtepomp zijn de taak van je verhuurder. Die kan daar zelf subsidie voor krijgen, en jij krijgt een lagere energierekening.',
    euro: null, how: ['Schrijf je verhuurder welke ramen tochten, of de spouw leeg is en welk energielabel het huis heeft.', 'Vraag het samen met buren aan; dat werkt vaak beter.', 'Je energieloket helpt gratis met een brief.'], where: 'Je verhuurder; hulp bij het energieloket van je gemeente.', cta: ['https://www.verbeterjehuis.nl/energieloket', 'Vind je energieloket'] });
  if (st.e === 'vve') add({ id: 'vve', laag: 3, ic: '🏢', t: 'Zet isolatie op de agenda van je VvE', why: 'Dak, gevel, glas en zonnepanelen op het dak gaan via de VvE. Het Warmtefonds heeft een aparte VvE-lening.',
    euro: null, how: ['Vraag het bestuur om een duurzaamheidsplan of meerjarenonderhoudsplan.', 'Stel voor om dit samen met gepland onderhoud te doen.'], where: 'Je VvE-vergadering; financiering via het Warmtefonds.', cta: ['https://www.warmtefonds.nl', 'Warmtefonds'] });

  /* euro's en score */
  out.forEach(m => {
    m.gm = m.isol || ['hybride', 'wp', 'zp', 'wateraccu', 'batterij', 'verhuurder', 'vve'].includes(m.id);
    if (m.euro === undefined) m.euro = gasE(m.gas) + kwhE(m.kwh);
    m.mid = (m.kosten[0] + m.kosten[1]) / 2;
    const netto = Math.max(0, m.mid - (m.subMid || 0));          // terugverdienen ná subsidie
    m.terug = m.euro > 0 && netto > 0 ? netto / m.euro : null;
    const co2 = (m.gas || 0) * C.co2Gas + ((m.kwh || 0) + (m.kwhSaved || 0)) * C.co2Kwh;
    const wt = { gas: ['spouw', 'dak', 'vloer', 'glas', 'hybride', 'wp', 'wateraccu'], zelf: ['zp', 'batterij', 'wateraccu', 'wp'] }[st.d] || [];
    const wgt = wt.includes(m.id) ? 1.5 : (st.d === 'gas' ? 0.6 : 1);
    m.score = st.d === 'klimaat' ? co2 / Math.max(netto, 1) : (m.euro || 0) / Math.max(netto, 1) * wgt;
  });
  return out;
}

function nietDoen(M) {
  const { has, b, v } = M; const nd = [];
  if (!has('zp') && !has('bat') && !has('dyn')) nd.push(['Een thuisbatterij kopen', 'Zonder zonnepanelen en zonder dynamisch contract heeft hij niets om mee te schuiven. Reken op € 100 tot € 250 per jaar; dan duurt terugverdienen ruim 20 jaar.', 'thuisbatterij-zonder-zonnepanelen', 'batterij']);
  if (st.e === 'koop' && b <= 2 && v === 'cv' && (!has('spouw') || !has('dak'))) nd.push(['Nu al een warmtepomp, vóór het isoleren', 'Je warmtepomp koopt dan warmte die door je muren en dak weglekt. Isoleer eerst; daarna volstaat een kleinere, goedkopere warmtepomp.', 'warmtepomp-bij-vorst', 'wp']);
  if (!has('zp')) nd.push(['Een wateraccu zonder zonnepanelen', 'Een wateraccu werkt op zonoverschot. Zonder panelen stook je je boiler met stroom van het net, en dat is duurder dan gas.', 'wateraccu', 'wateraccu']);
  if (st.e === 'koop' && !has('zp')) nd.push(['Panelen kopen voor de saldering', 'De saldering stopt op 1 januari 2027. Reken met wat je zelf gebruikt; onze getallen hierboven doen dat al.', 'salderingsregeling', 'saldering']);
  return nd.slice(0, 2);
}

/* ---------- uitkomst ---------- */
function link(href, tekst, cls, ev) {
  const extern = /^https?:/.test(href);
  return `<a class="${cls}" href="${href}" data-ev="${ev}"${extern ? ' target="_blank" rel="noopener"' : ''}>${tekst}</a>`;
}
function card(m, muted) {
  const terug = m.terug == null ? (m.mid === 0 ? 'Direct' : '–') : m.terug < 1 ? '< 1 jaar' : Math.round(m.terug) + ' jaar';
  const opbr = m.euro == null ? '–' : eur(m.euro) + '/jr';
  const kost = m.mid === 0 ? 'Gratis' : range(m.kosten);
  const cta = m.cta ? link(m.cta[0], m.cta[1] + ' →', 'm-cta', 'bp-stap-' + (m.cta[2] || m.id)) : '';
  const kb = m.kb ? link(/^https?:/.test(m.kb[0]) ? m.kb[0] : KB + m.kb[0], '📖 ' + m.kb[1], 'm-kb', 'bp-kb-' + m.id) : '';
  return `<article class="m${muted ? ' muted' : ''}"><div class="m-head">
    <div class="m-title"><span class="ic" aria-hidden="true">${m.ic}</span><div><h3>${m.t}</h3><p class="m-why">${m.why}</p></div></div>
    <dl class="m-stats"><div><dt>Kost</dt><dd>${kost}</dd></div><div class="pos"><dt>Levert op</dt><dd>${opbr}</dd></div><div><dt>${m.subMid ? 'Terug in (na subsidie)' : 'Terug in'}</dt><dd>${terug}</dd></div><div><dt>Subsidie</dt><dd>${m.subsidie}</dd>${m.gm ? '<a class="m-gm" href="#gemeente">+ check je gemeente</a>' : ''}</div></dl></div>
    <details data-id="${m.id}"${m.open ? ' open data-auto' : ''}><summary>Zo pak je het aan</summary><div class="m-how">
      <div class="m-where"><b>Waar regel je dit</b>${m.where}</div>
      <ol>${m.how.map(x => `<li>${x}</li>`).join('')}</ol>
      ${m.subTxt ? `<p class="m-note">Subsidie: ${m.subTxt}</p>` : ''}
      <div class="m-links">${cta}${kb}</div>
    </div></details></article>`;
}

function renderResult() {
  const M = model(); const all = measures(M);
  const budget = [0, 500, 5000, Infinity][+st.k || 0];
  const l1 = all.filter(m => m.laag === 1).sort((a, b) => (b.euro || 0) - (a.euro || 0));
  const l2 = all.filter(m => m.laag === 2).sort((a, b) => (b.euro || 0) - (a.euro || 0));
  const l3 = all.filter(m => m.laag === 3).sort((a, b) => b.score - a.score);
  const nu = l3.filter(m => !m.prereq && m.kosten[0] <= budget), later = l3.filter(m => m.prereq), ruimte = l3.filter(m => !m.prereq && m.kosten[0] > budget);
  if (l1[0]) l1[0].open = true; if (nu[0]) nu[0].open = true;
  const sum = a => a.reduce((s, m) => s + (m.euro || 0), 0);
  const gratis = sum(l1), klein = budget >= 500 ? sum(l2) : 0, groot = sum(nu.slice(0, 2));
  const rek = M.gas * C.gas + M.stroom * C.stroom;
  const geschat = !M.gasGiven || !M.stroomGiven;
  const nd = nietDoen(M);
  const huur = st.e === 'huur';
  const gemeente = `<section class="gm" id="gemeente" aria-labelledby="gm-t">
    <h3 id="gm-t">🏛️ Check ook wat je gemeente regelt</h3>
    <p>Bovenop de landelijke ISDE hebben veel gemeenten en provincies hun eigen regelingen. Wat er is, verschilt per plek en per jaar. Daarom rekenen we er niet mee, maar het kan ${huur ? 'ook voor huurders' : 'je plan'} flink goedkoper maken. Reken op 5 minuten zoeken.</p>
    <div class="gm-types">${(huur ? ['Gratis energiecoach aan huis', 'Klusteam voor kleine maatregelen', 'Bon of pakket met LED en tochtstrips', 'Hulp bij een brief aan je verhuurder'] : ['Eigen subsidie op isolatie of warmtepomp', 'Duurzaamheidslening met lage rente', 'Collectieve inkoop in je wijk', 'Gratis energiecoach aan huis', 'Klusteam voor kleine maatregelen']).map(t => `<span>${t}</span>`).join('')}</div>
    <ol>
      <li>Vul je postcode in bij de <b>Energiesubsidiewijzer</b>. Je ziet dan de regelingen van het Rijk, je provincie, je gemeente en je waterschap bij elkaar.</li>
      <li>Kijk op de site van je <b>energieloket</b> of bel ze. Vraag ook naar acties die niet in de wijzer staan, zoals een collectieve inkoop of een klusteam.</li>
      ${huur ? '<li>Vraag of je gemeente of je woningcorporatie ook iets voor huurders doet.</li>' : '<li>Zoek op de site van je gemeente op "duurzaamheidslening". Die loopt via SVn; de gemeente beslist of je in aanmerking komt.</li>'}
    </ol>
    ${huur ? '' : '<div class="gm-let"><b>Let op vóór je tekent:</b> lees de voorwaarden van de gemeente vóór je een offerte ondertekent. Sommige regelingen moet je vooraf aanvragen (de ISDE juist achteraf), budgetten zijn vaak op = op, en vraag na of je het mag combineren met de ISDE.</div>'}
    <div class="m-links">${link('https://www.verbeterjehuis.nl/energiesubsidiewijzer', 'Open de Energiesubsidiewijzer →', 'm-cta', 'bp-stap-subsidiewijzer')}${link('https://www.verbeterjehuis.nl/energieloket', '🏛️ Vind je energieloket', 'm-kb', 'bp-stap-energieloket')}</div>
  </section>`;
  const laag = (cls, nr, titel, uitleg, items) => items.length ? `<div class="bp-laag"><span class="pill ${cls}">Stap ${nr}</span><h2>${titel}</h2><p>${uitleg}</p></div>${items.map(m => card(m)).join('')}` : '';
  const doel = ({ gas: 'het meeste gas scheelt', geld: 'het meeste oplevert per euro', zelf: 'je het minst afhankelijk maakt', klimaat: 'de meeste CO₂ per euro scheelt' })[st.d] || 'het meeste oplevert';

  app.innerHTML = `
  <section class="bp-sum" aria-live="polite">
    <h2>Jouw bespaarplan</h2>
    <div class="big">± ${eur(gratis + klein + groot)} per jaar</div>
    <p>Dat leveren de eerste stappen hieronder samen op${groot ? ', inclusief de twee grote stappen die in je budget passen' : ''}. <b>${eur(gratis)} daarvan kost je niets.</b> Het is een ruwe optelsom: maatregelen overlappen deels.</p>
    <div class="bp-now"><div><small>Gas nu</small><b>${Math.round(M.gas).toLocaleString('nl-NL')} m³</b>${M.gasGiven ? '' : '<em> geschat</em>'}</div><div><small>Stroom nu</small><b>${Math.round(M.stroom).toLocaleString('nl-NL')} kWh</b>${M.stroomGiven ? '' : '<em> geschat</em>'}</div><div><small>Gas + stroom</small><b>${eur(rek)}/jr</b>${geschat ? '<em> geschat</em>' : ''}</div></div>
    <p class="bp-edit"><button type="button" data-edit>Antwoorden aanpassen</button>${geschat ? ' · Vul je jaarafrekening in voor een scherper beeld.' : ''}</p>
  </section>
  ${laag('l1', 1, 'Vandaag, gratis', 'Kost niets, alleen een kwartiertje.', l1)}
  ${laag('l2', 2, 'Klein geld, snel terug', budget < 500 ? 'Buiten je budget, maar de meeste verdien je binnen een jaar of twee terug.' : 'Tot € 500, meestal binnen een paar jaar terugverdiend.', l2)}
  ${l3.length ? `<div class="bp-laag"><span class="pill l3">Stap 3</span><h2>De grote stap die bij jouw huis past</h2><p>Gesorteerd op wat ${doel}. Isoleren komt altijd vóór een warmtepomp.</p></div>
    ${nu.slice(0, 4).map(m => card(m)).join('')}
    ${gemeente}
    ${nu.length > 4 ? `<div class="bp-group">Nog meer dat past</div>${nu.slice(4).map(m => card(m, true)).join('')}` : ''}
    ${later.length ? `<div class="bp-group">Later, na het isoleren</div>${later.map(m => card(m, true)).join('')}` : ''}
    ${ruimte.length ? `<div class="bp-group">Als je later meer ruimte hebt (of wilt lenen)</div>${ruimte.slice(0, 3).map(m => card(m, true)).join('')}` : ''}` : gemeente}
  ${nd.length ? `<div class="bp-laag"><h2>Dit zou ik niet doen</h2></div>${nd.map(([t, p, kb, id]) => `<div class="nd"><h3>✋ ${t}</h3><p>${p} ${link(KB + kb, 'Lees waarom →', 'nd-link', 'bp-nd-' + id)}</p></div>`).join('')}` : ''}
  <div class="bp-laag"><h2>Hulp nodig bij de volgende stap?</h2></div>
  <div class="bp-help">
    ${link('https://www.verbeterjehuis.nl/energieloket', '<b>🏛️ Energieloket</b><small>Gratis, onafhankelijk advies van je gemeente, soms aan huis.</small>', '', 'bp-stap-energieloket')}
    ${link('https://www.warmtefonds.nl', '<b>💶 Warmtefonds</b><small>Lenen voor isolatie en warmtepomp, bij een laag inkomen soms tegen 0% rente.</small>', '', 'bp-stap-warmtefonds')}
    ${link('https://www.verbeterjehuis.nl', '<b>🔎 Verbetercheck</b><small>Isolatie tot op de m² uitgerekend voor jouw woningtype.</small>', '', 'bp-stap-verbetercheck')}
  </div>
  <div class="bp-share bp-duim"><b>Had je hier iets aan?</b><button type="button" data-nuttig="ja">👍 Ja</button><button type="button" data-nuttig="nee">👎 Nee</button></div>
  <div class="bp-share"><button type="button" data-print>🖨️ Print of bewaar als PDF</button><button type="button" data-share>🔗 Kopieer link naar dit plan</button></div>
  <p class="bp-disc">Dit is een indicatie op basis van gemiddelden, geen maatwerkadvies. Prijzen: gas € 1,35/m³, stroom € 0,27/kWh, teruglevering na 2027 netto € 0,05/kWh. Isolatie: kosten en besparing per woningtype van Milieu Centraal (aangepast aan jouw gasverbruik), ISDE-bedragen van RVO, beide gecontroleerd op 24 november 2026. Je antwoorden staan alleen in de link; we bewaren niets.</p>`;

  if (!viaUrl) { tel('bp-klaar'); tel('bp-p-' + st.e); tel('bp-p-doel-' + st.d); tel('bp-p-' + st.v); }
}

/* start */
if (fromUrl()) { idx = Q.length; viaUrl = true; tel('bp-terug'); renderResult(); } else renderQ(false);
})();
