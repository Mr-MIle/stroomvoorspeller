#!/usr/bin/env python3
"""Jaaroverzicht van de stroomprijzen (jaarverloop op /historisch).

Leest public/data/archief/YYYY-MM.json en schrijft:

  public/data/jaar/YYYY.json   per dag gemiddelde/laagste/hoogste kale prijs,
                               aantal uren en de negatieve uurprijzen, plus
                               maand- en jaarcijfers (~15 kB per jaar)
  public/data/jaar/index.json  welke jaren er zijn
  public/historisch/YYYY.html  crawlbare jaarpagina (/historisch/2025)
  public/historisch.html       het blok tussen BUILD:JAREN:START/END met
                               links naar de jaarpagina's

Alle prijzen in de JSON zijn kale EPEX-prijzen in EUR/MWh. De pagina telt
opslag, energiebelasting en btw er in de browser bij op, met het
belastingtarief van dat jaar uit config.json -> belasting_per_jaar.

Bestanden worden alleen herschreven als de inhoud verandert, en er staat
geen tijdstempel in: afgeronde jaren blijven byte-identiek, zodat alleen
het lopende jaar dagelijks een kleine diff geeft.

Gebruik:
    python scripts/generate_jaaroverzicht.py
    python scripts/generate_jaaroverzicht.py --root /tmp/kopie
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SITE = "https://stroomvoorspeller.nl"

MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni",
           "juli", "augustus", "september", "oktober", "november", "december"]
MAANDEN_KORT = ["jan", "feb", "mrt", "apr", "mei", "jun",
                "jul", "aug", "sep", "okt", "nov", "dec"]

AMS = ZoneInfo("Europe/Amsterdam")

BLOK_START = "<!-- BUILD:JAREN:START -->"
BLOK_EIND = "<!-- BUILD:JAREN:END -->"


# ── Hulpfuncties ──────────────────────────────────────────────────────────

def nl(value: float, digits: int = 1) -> str:
    """Nederlandse notatie: komma als decimaalteken, echt minteken."""
    s = f"{value:.{digits}f}"
    if s.startswith("-") and float(s) == 0:
        s = s[1:]
    return s.replace("-", "−").replace(".", ",")


def r2(x: float) -> float:
    return round(x + 0.0, 2)


def datum_lang(d: date) -> str:
    return f"{d.day} {MAANDEN[d.month - 1]}"


def schrijf_als_anders(path: Path, inhoud: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == inhoud:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(inhoud, encoding="utf-8")
    return True


# ── Belasting (zelfde regels als historisch.html) ─────────────────────────

class Belasting:
    def __init__(self, cfg: dict):
        tab = cfg.get("belasting_per_jaar") or {}
        self.jaren = tab.get("jaren", {})
        self.btw_std = float(tab.get("btw_factor", cfg["taxes"]["btw_factor"]))
        self.uitz = tab.get("btw_uitzonderingen", [])
        self.actueel = cfg["taxes"]

    def eb(self, jaar: int) -> tuple[float, float]:
        """(energiebelasting, ode) in EUR/kWh excl. btw voor dat jaar."""
        r = self.jaren.get(str(jaar))
        if r is None:
            return float(self.actueel["energiebelasting_per_kwh"]), 0.0
        return float(r["eb"]), float(r.get("ode", 0))

    def btw(self, dag: str) -> float:
        for u in self.uitz:
            if u["van"] <= dag <= u["tot"]:
                return float(u["btw_factor"])
        return self.btw_std

    def consument_ct(self, eur_mwh: float, dag: str, opslag: float) -> float:
        eb, ode = self.eb(int(dag[:4]))
        return (eur_mwh / 1000 + opslag + eb + ode) * self.btw(dag) * 100


# ── Data ──────────────────────────────────────────────────────────────────

def vul_a03(punten: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Herstel uren die ENTSO-E weglaat (curveType A03: een weggelaten uur heeft
    dezelfde prijs als het uur ervoor). Het archief van vóór de A03-fix in
    fetch_prices.py (24 juni 2026) mist die uren nog, vooral in vlakke stukken
    rond 0 euro. Vult per kalenderdag de gaten tussen twee uren en de staart tot
    23:00. Rekent in UTC en labelt in Amsterdamse tijd, zodat zomer- en
    wintertijd kloppen. Is het archief compleet, dan verandert er niets."""
    if not punten:
        return punten
    uur = timedelta(hours=1)
    per_dag: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for t, v in punten:
        per_dag[t[:10]].append((datetime.fromisoformat(t).astimezone(timezone.utc), v))
    uit: list[tuple[str, float]] = []
    for dag in sorted(per_dag):
        rij = sorted(per_dag[dag])
        # Op de dag van de wintertijd komt 02:00 twee keer voor; het archief bewaart
        # er meestal maar één. Dat is geen A03-gat, dus een kloktijd die al in de dag
        # staat vullen we niet nog eens.
        al_er = {a.astimezone(AMS).hour for a, _ in rij}
        for i, (a, va) in enumerate(rij):
            uit.append((a.astimezone(AMS).isoformat(), va))
            grens = rij[i + 1][0] if i + 1 < len(rij) else None
            x = a + uur
            while True:
                if grens is not None and x >= grens:
                    break
                lokaal = x.astimezone(AMS)
                if lokaal.date().isoformat() != dag:
                    break
                if lokaal.hour not in al_er:
                    uit.append((lokaal.isoformat(), va))
                x += uur
    return uit


def lees_archief(archief: Path) -> dict[int, list[tuple[str, float]]]:
    """{jaar: [(iso-tijd, EUR/MWh), ...]} gesorteerd, ontdubbeld op tijd."""
    per_jaar: dict[int, dict[str, float]] = defaultdict(dict)
    for f in sorted(archief.glob("????-??.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[warn] {f.name} niet leesbaar: {exc}", file=sys.stderr)
            continue
        for p in data.get("prices") or []:
            try:
                t = p["time"]
                v = float(p["price"])
            except (KeyError, TypeError, ValueError):
                continue
            per_jaar[int(t[:4])][t] = v
    return {j: vul_a03(sorted(d.items())) for j, d in sorted(per_jaar.items())}


def jaar_data(jaar: int, punten: list[tuple[str, float]], vandaag: date) -> dict:
    dagen: dict[str, list[float]] = defaultdict(list)
    for t, v in punten:
        dagen[t[:10]].append(v)

    rijen = []
    neg = {}
    for d in sorted(dagen):
        vs = dagen[d]
        rijen.append([d, r2(sum(vs) / len(vs)), r2(min(vs)), r2(max(vs)), len(vs)])
        n = sorted(r2(v) for v in vs if v < 0)
        if n:
            neg[d] = n

    maanden = []
    per_maand: dict[str, list[float]] = defaultdict(list)
    for t, v in punten:
        per_maand[t[:7]].append(v)
    for ym in sorted(per_maand):
        vs = per_maand[ym]
        maanden.append({
            "maand": ym,
            "gem": r2(sum(vs) / len(vs)),
            "min": r2(min(vs)),
            "max": r2(max(vs)),
            "uren_onder_0": sum(1 for v in vs if v < 0),
            "uren": len(vs),
            "dagen": len({t[:10] for t, _ in punten if t.startswith(ym)}),
        })

    vals = [v for _, v in punten]
    lo = min(punten, key=lambda tv: tv[1])
    hi = max(punten, key=lambda tv: tv[1])
    eerste, laatste = rijen[0][0], rijen[-1][0]
    compleet = (eerste == f"{jaar}-01-01" and laatste == f"{jaar}-12-31"
                and jaar < vandaag.year)

    return {
        "jaar": jaar,
        "eenheid": "EUR/MWh, kale EPEX day-ahead-prijs zonder opslag en belasting",
        "eerste_dag": eerste,
        "laatste_dag": laatste,
        "compleet": compleet,
        "velden": ["datum", "gem", "min", "max", "uren"],
        "dagen": rijen,
        "negatief": neg,
        "maanden": maanden,
        "jaar_cijfers": {
            "gem": r2(sum(vals) / len(vals)),
            "min": r2(lo[1]), "min_tijd": lo[0],
            "max": r2(hi[1]), "max_tijd": hi[0],
            "uren_onder_0": sum(1 for v in vals if v < 0),
            "uren": len(vals),
            "dagen": len(rijen),
        },
    }


def gem_met_belasting(jd: dict, bel: Belasting, opslag: float) -> float:
    """Uurgewogen gemiddelde consumentenprijs in ct/kWh voor dat jaar."""
    som = n = 0.0
    for d, gem, _mn, _mx, uren in jd["dagen"]:
        som += bel.consument_ct(gem, d, opslag) * uren
        n += uren
    return som / n


# ── Jaarpagina ────────────────────────────────────────────────────────────

def periode_tekst(jd: dict) -> str:
    """'2025', '2015 (vanaf 5 januari)', '2026 tot en met 7 oktober'."""
    j = jd["jaar"]
    e = date.fromisoformat(jd["eerste_dag"])
    l = date.fromisoformat(jd["laatste_dag"])
    if jd["compleet"]:
        return str(j)
    delen = []
    if e != date(j, 1, 1):
        delen.append(f"vanaf {datum_lang(e)}")
    if l != date(j, 12, 31):
        delen.append(f"tot en met {datum_lang(l)}")
    return f"{j} ({' '.join(delen)})" if delen else str(j)


def belasting_zin(jaar: int, bel: Belasting) -> str:
    eb, ode = bel.eb(jaar)
    btw_pct = round((bel.btw_std - 1) * 100)
    if ode:
        zin = (f"In {jaar} was de energiebelasting {nl(eb * 100, 2)} cent per kWh. "
               f"Daar kwam {nl(ode * 100, 2)} cent opslag duurzame energie bij (die heffing "
               f"bestond tot en met 2022). Samen {nl((eb + ode) * 100, 2)} cent, "
               f"zonder btw.")
    else:
        zin = (f"In {jaar} was de energiebelasting {nl(eb * 100, 2)} cent per kWh, "
               f"zonder btw ({nl(eb * bel.btw_std * 100, 2)} cent met {btw_pct}% btw).")
    if jaar == 2022:
        zin += (" Van 1 juli tot 1 januari 2023 was de btw op energie tijdelijk 9% "
                "in plaats van 21%.")
    if jaar == 2023:
        zin += (" In 2023 gold daarnaast het prijsplafond: tot 2.900 kWh per jaar betaalde "
                "je hoogstens 40 cent per kWh. De cijfers op deze pagina zijn zonder plafond.")
    return zin


def vergelijk_zin(jd: dict, alle: dict[int, dict]) -> str:
    """Vergelijk met het jaar ervoor, over dezelfde kalenderdagen. Zo telt een
    half jaar 2026 niet tegen heel 2025, en 2016 niet tegen 2015 dat op
    5 januari begint."""
    vorig = alle.get(jd["jaar"] - 1)
    if not vorig:
        return ""

    def per_mmdd(d: dict) -> dict[str, tuple[float, int]]:
        return {r[0][5:]: (r[1], r[4]) for r in d["dagen"]}

    a, b = per_mmdd(jd), per_mmdd(vorig)
    samen = sorted(set(a) & set(b))
    if len(samen) < 28:
        return ""

    def gem(m: dict[str, tuple[float, int]]) -> float:
        som = sum(m[k][0] * m[k][1] for k in samen)
        return som / sum(m[k][1] for k in samen)

    cur, ref = gem(a), gem(b)
    if ref == 0:
        return ""
    d = (cur - ref) / abs(ref) * 100
    if abs(d) <= 0.5:
        rel = f"vrijwel gelijk aan {vorig['jaar']}"
    else:
        rel = f"{nl(abs(d), 0)}% {'hoger' if d > 0 else 'lager'} dan in {vorig['jaar']}"
    s = f"De gemiddelde beursprijs was {rel} ({nl(cur / 10)} tegen {nl(ref / 10)} cent per kWh"
    if len(samen) < 365:
        j = jd["jaar"]
        van = date.fromisoformat(f"{j}-{samen[0]}")
        tot = date.fromisoformat(f"{j}-{samen[-1]}")
        s += f", allebei van {datum_lang(van)} tot en met {datum_lang(tot)}"
    return s + ")."


def dekking_zin(jd: dict) -> str:
    """Eerlijk melden als het archief na vul_a03 nog uren mist."""
    e = date.fromisoformat(jd["eerste_dag"])
    l = date.fromisoformat(jd["laatste_dag"])
    verwacht = ((l - e).days + 1) * 24
    mist = verwacht - jd["jaar_cijfers"]["uren"]
    if mist < 24:
        return ""
    return (f" Van deze periode ontbreken {mist} van de {verwacht} uurprijzen in ons archief "
            f"({nl(mist / verwacht * 100)}%). Tellingen zoals het aantal uren onder nul "
            f"kunnen daardoor iets lager uitvallen.")


def bouw_pagina(jd: dict, alle: dict[int, dict], bel: Belasting,
                opslag_gem: float, footer_html: str,
                maandpaginas: set[str] | None = None) -> str:
    j = jd["jaar"]
    jc = jd["jaar_cijfers"]
    url = f"{SITE}/historisch/{j}"
    periode = periode_tekst(jd)
    jaren = sorted(alle)
    i = jaren.index(j)
    vorige = jaren[i - 1] if i > 0 else None
    volgende = jaren[i + 1] if i + 1 < len(jaren) else None

    gem_ct = jc["gem"] / 10
    incl_ct = gem_met_belasting(jd, bel, opslag_gem)
    lo_t = datetime.fromisoformat(jc["min_tijd"])
    hi_t = datetime.fromisoformat(jc["max_tijd"])

    titel = f"Stroomprijzen {j}: gemiddeld {nl(gem_ct)} cent per kWh"
    if jc["uren_onder_0"]:
        neg_kort = f"{jc['uren_onder_0']} uur onder nul"
    else:
        neg_kort = "geen uur onder nul"
    beschrijving = (
        f"Stroomprijzen {periode}: gemiddeld {nl(gem_ct)} cent per kWh op de "
        f"stroombeurs, {nl(incl_ct)} cent met belasting en opslag. {neg_kort.capitalize()}. "
        f"Per maand en per dag terug te kijken.")

    rijen = []
    for m in jd["maanden"]:
        mi = int(m["maand"][5:]) - 1
        neg = str(m["uren_onder_0"]) if m["uren_onder_0"] else "—"
        naam = MAANDEN[mi].capitalize()
        if maandpaginas and m["maand"] in maandpaginas:
            naam = f'<a href="/historisch/{m["maand"]}">{naam}</a>'
        rijen.append(
            f"              <tr><td>{naam}</td>"
            f"<td>{nl(m['gem'] / 10)}</td><td>{nl(m['min'] / 10)}</td>"
            f"<td>{nl(m['max'] / 10)}</td><td>{neg}</td></tr>")
    tabel = "\n".join(rijen)

    nav = []
    if vorige:
        nav.append(f'<a class="jaar-nav-link" href="/historisch/{vorige}">← {vorige}</a>')
    nav.append(f'<a class="jaar-nav-link" href="/historisch?jaar={j}#jaarverloop">Jaarverloop per aanbieder</a>')
    if volgende:
        nav.append(f'<a class="jaar-nav-link" href="/historisch/{volgende}">{volgende} →</a>')
    nav_html = "\n          ".join(nav)

    vergelijk = vergelijk_zin(jd, alle)
    vergelijk_html = f'\n        <p class="jaar-vergelijk">{vergelijk}</p>' if vergelijk else ""

    if jd["compleet"]:
        sub = (f"Wat stroom in {j} kostte op de stroombeurs, per maand. De bedragen in de "
               f"tabel zijn zonder belasting en zonder opslag van je leverancier.")
    else:
        sub = (f"Wat stroom in {periode} kostte op de stroombeurs, per maand. De bedragen in "
               f"de tabel zijn zonder belasting en zonder opslag van je leverancier.")

    jsonld = json.dumps({
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebPage",
                "@id": url, "url": url,
                "name": titel,
                "description": beschrijving,
                "inLanguage": "nl-NL",
                "isPartOf": {"@id": f"{SITE}/#website"},
                "breadcrumb": {
                    "@type": "BreadcrumbList",
                    "itemListElement": [
                        {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{SITE}/"},
                        {"@type": "ListItem", "position": 2, "name": "Historische prijzen", "item": f"{SITE}/historisch"},
                        {"@type": "ListItem", "position": 3, "name": f"Stroomprijzen {j}", "item": url},
                    ],
                },
            },
            {
                "@type": "Dataset",
                "name": f"Day-ahead stroomprijzen Nederland {j}",
                "description": (f"Gemiddelde, laagste en hoogste EPEX day-ahead-prijs per dag "
                                f"voor Nederland in {periode}, op basis van {jc['uren']} uurprijzen."),
                "temporalCoverage": f"{jd['eerste_dag']}/{jd['laatste_dag']}",
                "spatialCoverage": "Nederland",
                "inLanguage": "nl-NL",
                "isBasedOn": "https://transparency.entsoe.eu",
                "creator": {"@type": "Organization", "name": "Stroomvoorspeller.nl", "url": SITE},
                "distribution": [{
                    "@type": "DataDownload",
                    "encodingFormat": "application/json",
                    "contentUrl": f"{SITE}/data/jaar/{j}.json",
                }],
            },
        ],
    }, ensure_ascii=False, indent=2)

    return f"""<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>{titel}</title>
  <meta name="description" content="{beschrijving}" />
  <meta name="theme-color" content="#0f6cbd" />
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  <link rel="canonical" href="{url}" />

  <meta property="og:type" content="website" />
  <meta property="og:locale" content="nl_NL" />
  <meta property="og:site_name" content="Stroomvoorspeller.nl" />
  <meta property="og:title" content="{titel}" />
  <meta property="og:description" content="{beschrijving}" />
  <meta property="og:url" content="{url}" />
  <meta property="og:image" content="{SITE}/og-image.png" />

  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="{titel}" />
  <meta name="twitter:description" content="{beschrijving}" />
  <meta name="twitter:image" content="{SITE}/og-image.png" />

  <link rel="stylesheet" href="/styles.css" />

  <script type="application/ld+json">
{jsonld}
  </script>

  <style>
    .jaar-hero {{ background: var(--c-surface); border-bottom: 1px solid var(--c-border); padding: 28px 0 22px; }}
    .jaar-hero h1 {{ margin: 0 0 6px; }}
    .jaar-hero .jaar-sub {{ color: var(--c-text-soft); margin: 0; max-width: 720px; }}
    .jaar-breadcrumb {{ font-size: 0.85rem; color: var(--c-text-mute); margin: 0 0 8px; }}
    .jaar-breadcrumb a {{ color: var(--c-text-mute); }}
    .jaar-kern {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 22px 0; }}
    .jaar-kern .kern-item {{ background: var(--c-surface); border: 1px solid var(--c-border); border-radius: 10px; padding: 14px 16px; }}
    .jaar-kern .kern-label {{ display: block; font-size: 0.8rem; color: var(--c-text-mute); margin-bottom: 4px; }}
    .jaar-kern .kern-value {{ font-size: 1.25rem; font-weight: 700; }}
    .jaar-kern .kern-detail {{ display: block; font-size: 0.8rem; color: var(--c-text-soft); margin-top: 2px; }}
    .jaar-vergelijk {{ color: var(--c-text-soft); max-width: 720px; }}
    .jaar-tabel {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
    .jaar-tabel th, .jaar-tabel td {{ padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--c-border); white-space: nowrap; }}
    .jaar-tabel th:first-child, .jaar-tabel td:first-child {{ text-align: left; }}
    .jaar-tabel th {{ font-size: 0.8rem; color: var(--c-text-mute); font-weight: 600; }}
    .jaar-nav {{ display: flex; flex-wrap: wrap; gap: 16px; justify-content: space-between; margin: 26px 0 8px; }}
    .jaar-nav-link {{ font-weight: 600; }}
    .jaar-uitleg {{ max-width: 720px; }}
    @media (max-width: 640px) {{ .jaar-kern {{ grid-template-columns: 1fr 1fr; }} }}
  </style>
  <script defer src="/_vercel/insights/script.js"></script>
</head>
<body>

  <header class="site-header">
    <div class="container header-row">
      <a class="brand" href="/">
        <img src="/favicon.svg" width="28" height="28" alt="" aria-hidden="true" class="brand-mark">
        <span class="brand-name">stroomvoorspeller<span class="brand-tld">.nl</span></span>
      </a>
      <nav class="primary-nav" aria-label="hoofdmenu">
        <a href="/">Prijzen</a>
        <a href="/morgen">Morgen</a>
        <a href="/aanbieders">Aanbieders</a>
        <a href="/kennisbank">Kennisbank</a>
        <a href="/historisch" aria-current="page">Historisch</a>
        <a href="/batterij">Batterij</a>
        <a href="/integraties">Integraties</a>
      </nav>
    </div>
  </header>

  <main>
    <section class="jaar-hero">
      <div class="container">
        <p class="jaar-breadcrumb"><a href="/historisch">← Historische prijzen</a></p>
        <h1>Stroomprijzen {j}</h1>
        <p class="jaar-sub">{sub}</p>
      </div>
    </section>

    <section class="section">
      <div class="container">
        <div class="jaar-kern">
          <div class="kern-item"><span class="kern-label">Gemiddeld op de beurs</span><span class="kern-value">{nl(gem_ct)} ct</span><span class="kern-detail">per kWh, zonder belasting</span></div>
          <div class="kern-item"><span class="kern-label">Met belasting en opslag</span><span class="kern-value">{nl(incl_ct)} ct</span><span class="kern-detail">per kWh, gemiddelde aanbieder</span></div>
          <div class="kern-item"><span class="kern-label">Duurste uur</span><span class="kern-value">{nl(jc['max'] / 10)} ct</span><span class="kern-detail">beursprijs, {datum_lang(hi_t.date())} {hi_t.strftime('%H:%M')} uur</span></div>
          <div class="kern-item"><span class="kern-label">Uren onder nul</span><span class="kern-value">{jc['uren_onder_0']}</span><span class="kern-detail">beursprijs, laagste {nl(jc['min'] / 10)} ct op {datum_lang(lo_t.date())}</span></div>
        </div>{vergelijk_html}

        <h2>Stroomprijzen per maand in {j}</h2>
        <p class="scroll-hint" aria-hidden="true">Veeg opzij voor alle kolommen →</p>
        <div class="scroll-x">
          <table class="jaar-tabel">
            <thead>
              <tr><th>Maand</th><th>Gemiddeld (ct/kWh)</th><th>Laagste uur</th><th>Duurste uur</th><th>Uren onder 0</th></tr>
            </thead>
            <tbody>
{tabel}
            </tbody>
          </table>
        </div>

        <nav class="jaar-nav" aria-label="jaarnavigatie">
          {nav_html}
        </nav>

        <div class="jaar-uitleg">
          <h2>Wat je zelf betaalde</h2>
          <p>Met een dynamisch contract betaal je de beursprijs van elk uur. Je leverancier telt daar een opslag bij op, en de overheid energiebelasting en btw. {belasting_zin(j, bel)}</p>
          <p>Het blok "Met belasting en opslag" rekent met dat tarief en met de opslag die aanbieders nu vragen: gemiddeld {nl(opslag_gem * bel.btw_std * 100)} cent per kWh met btw. Die opslag was in {j} misschien anders. Vaste kosten per maand en de korting op de energiebelasting per aansluiting zitten er niet in.</p>
          <p>Wil je het verloop van {j} per dag zien, voor jouw aanbieder? Dat kan bij het <a href="/historisch?jaar={j}#jaarverloop">jaarverloop op de historisch-pagina</a>. Bron van de prijzen: <a href="https://transparency.entsoe.eu" rel="noopener">ENTSO-E Transparency</a>.{dekking_zin(jd)}</p>
        </div>
      </div>
    </section>
  </main>

{footer_html}

  <!-- Cloudflare Web Analytics -->
  <script defer src="https://static.cloudflareinsights.com/beacon.min.js" data-cf-beacon='{{"token": "b0c666a71b274ee7b092122def7755e8"}}'></script>
  <script src="/nav.js" defer></script>
</body>
</html>
"""


def footer_uit(historisch_html: str) -> str:
    """Neem de footer letterlijk over van historisch.html, zodat de jaarpagina's
    meelopen als de footer daar verandert."""
    m = re.search(r'  <footer class="site-footer">.*?</footer>', historisch_html, re.S)
    if not m:
        raise SystemExit("footer niet gevonden in historisch.html")
    return m.group(0)


def jaren_blok(alle: dict[int, dict]) -> str:
    links = []
    for j in sorted(alle, reverse=True):
        links.append(f'<a href="/historisch/{j}">{j}</a>')
    return (f'{BLOK_START}\n'
            f'          <p class="jaar-links">Overzicht per jaar: {" · ".join(links)}</p>\n'
            f'          {BLOK_EIND}')


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=PROJECT_ROOT)
    ap.add_argument("--vandaag", type=date.fromisoformat, default=date.today(),
                    help="alleen voor tests")
    args = ap.parse_args()

    pub = args.root / "public"
    cfg = json.loads((pub / "data" / "config.json").read_text(encoding="utf-8"))
    bel = Belasting(cfg)
    avg = next((s for s in cfg["suppliers"] if s["id"] == "average"), None)
    opslag_gem = float(avg["markup_per_kwh"]) if avg else 0.0

    archief = lees_archief(pub / "data" / "archief")
    if not archief:
        print("[fout] geen archief gevonden", file=sys.stderr)
        return 1

    alle = {j: jaar_data(j, pts, args.vandaag) for j, pts in archief.items() if pts}

    hist_path = pub / "historisch.html"
    hist = hist_path.read_text(encoding="utf-8")
    footer = footer_uit(hist)

    geschreven = 0
    jaar_dir = pub / "data" / "jaar"
    # Maandpagina's (generate_historisch_pages.py) die al bestaan: link vanuit de tabel.
    maandpaginas = {f.stem for f in (pub / "historisch").glob("????-??.html")}
    for j, jd in alle.items():
        s = json.dumps(jd, ensure_ascii=False, separators=(",", ":"))
        geschreven += schrijf_als_anders(jaar_dir / f"{j}.json", s + "\n")
        html = bouw_pagina(jd, alle, bel, opslag_gem, footer, maandpaginas)
        geschreven += schrijf_als_anders(pub / "historisch" / f"{j}.html", html)

    index = {
        "jaren": [{
            "jaar": j,
            "eerste_dag": jd["eerste_dag"],
            "laatste_dag": jd["laatste_dag"],
            "compleet": jd["compleet"],
            "gem": jd["jaar_cijfers"]["gem"],
        } for j, jd in sorted(alle.items())],
    }
    geschreven += schrijf_als_anders(
        jaar_dir / "index.json",
        json.dumps(index, ensure_ascii=False, indent=1) + "\n")

    if BLOK_START in hist and BLOK_EIND in hist:
        nieuw = re.sub(re.escape(BLOK_START) + r".*?" + re.escape(BLOK_EIND),
                       lambda _m: jaren_blok(alle), hist, count=1, flags=re.S)
        geschreven += schrijf_als_anders(hist_path, nieuw)
    else:
        print("[warn] BUILD:JAREN-markers ontbreken in historisch.html", file=sys.stderr)

    print(f"{len(alle)} jaren ({min(alle)}-{max(alle)}), {geschreven} bestand(en) bijgewerkt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
