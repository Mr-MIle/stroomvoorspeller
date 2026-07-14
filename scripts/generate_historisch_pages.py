#!/usr/bin/env python3
"""Genereer statische maandpagina's uit het prijsarchief (backlog H2 + #55).

Leest public/data/archief/YYYY-MM.json en schrijft per afgeronde maand een
crawlbare HTML-pagina naar public/historisch/YYYY-MM.html (Vercel cleanUrls
maakt daar /historisch/YYYY-MM van). Alleen volledige maanden; de lopende
maand wordt overgeslagen. Bestanden worden alleen herschreven als de inhoud
verandert, zodat git-diffs klein blijven.

Alle bedragen op de pagina's zijn kale EPEX-marktprijzen in ct/kWh, zonder
belasting en opslag. Bewust: belastingtarieven verschillen per jaar, dus een
"incl. belasting"-bedrag over 2015-2026 zou niet kloppen.

Gebruik:
    python scripts/generate_historisch_pages.py            # alles
    python scripts/generate_historisch_pages.py --out /tmp/x --months 2026-06
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = PROJECT_ROOT / "public" / "data" / "archief"
DEFAULT_OUT = PROJECT_ROOT / "public" / "historisch"
SITE = "https://stroomvoorspeller.nl"

MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni",
           "juli", "augustus", "september", "oktober", "november", "december"]
DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def nl(value: float, digits: int = 1) -> str:
    """Nederlandse notatie: komma als decimaalteken, echt minteken."""
    s = f"{value:.{digits}f}".replace("-", "−").replace(".", ",")
    return s


def ct(eur_mwh: float) -> float:
    """EUR/MWh -> ct/kWh (kale markt)."""
    return eur_mwh / 10.0


def month_label(ym: str) -> str:
    y, m = ym.split("-")
    return f"{MAANDEN[int(m) - 1]} {y}"


def load_month(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[warn] {path.name} niet leesbaar: {exc}", file=sys.stderr)
        return None
    if not data.get("prices"):
        return None
    return data


def is_complete(ym: str, prices: list[dict]) -> bool:
    """Volledige maand: elke kalenderdag heeft prijspunten."""
    y, m = (int(x) for x in ym.split("-"))
    days_seen = {datetime.fromisoformat(p["time"]).date().day for p in prices}
    return days_seen >= set(range(1, calendar.monthrange(y, m)[1] + 1))


def month_stats(prices: list[dict]) -> dict:
    pts = [(datetime.fromisoformat(p["time"]), float(p["price"])) for p in prices]
    pts.sort(key=lambda t: t[0])
    vals = [v for _, v in pts]
    lo = min(pts, key=lambda t: t[1])
    hi = max(pts, key=lambda t: t[1])
    days: dict[date, list[float]] = {}
    for ts, v in pts:
        days.setdefault(ts.date(), []).append(v)
    day_rows = []
    for d in sorted(days):
        dv = days[d]
        day_rows.append({
            "date": d,
            "avg": sum(dv) / len(dv),
            "min": min(dv),
            "max": max(dv),
            "neg": sum(1 for v in dv if v < 0),
        })
    return {
        "avg": sum(vals) / len(vals),
        "min": lo[1], "min_ts": lo[0],
        "max": hi[1], "max_ts": hi[0],
        "neg_hours": sum(1 for v in vals if v < 0),
        "hours": len(vals),
        "days": day_rows,
    }


def pct_diff(cur: float, ref: float) -> float | None:
    if ref == 0:
        return None
    return (cur - ref) / abs(ref) * 100.0


def vergelijk_zin(ym: str, avg: float, all_stats: dict[str, dict]) -> str:
    """Eén vergelijkingszin met vorige maand en (indien aanwezig) vorig jaar."""
    y, m = (int(x) for x in ym.split("-"))
    prev = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    yoy = f"{y - 1}-{m:02d}"
    parts = []
    if prev in all_stats:
        d = pct_diff(avg, all_stats[prev]["avg"])
        if d is not None:
            richting = "hoger dan" if d > 0.5 else ("lager dan" if d < -0.5 else "vrijwel gelijk aan")
            pct = f"{nl(abs(d), 0)}% " if abs(d) > 0.5 else ""
            parts.append(f"{pct}{richting} {month_label(prev)}")
    if yoy in all_stats:
        d = pct_diff(avg, all_stats[yoy]["avg"])
        if d is not None:
            richting = "hoger dan" if d > 0.5 else ("lager dan" if d < -0.5 else "vrijwel gelijk aan")
            pct = f"{nl(abs(d), 0)}% " if abs(d) > 0.5 else ""
            parts.append(f"{pct}{richting} {month_label(yoy)}")
    if not parts:
        return ""
    return "Het maandgemiddelde was " + " en ".join(parts) + "."


def build_page(ym: str, st: dict, all_months: list[str], all_stats: dict[str, dict]) -> str:
    label = month_label(ym)
    label_cap = label[0].upper() + label[1:]
    y, m = (int(x) for x in ym.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    url = f"{SITE}/historisch/{ym}"

    idx = all_months.index(ym)
    prev_ym = all_months[idx - 1] if idx > 0 else None
    next_ym = all_months[idx + 1] if idx + 1 < len(all_months) else None

    titel = f"Stroomprijzen {label} — gemiddeld {nl(ct(st['avg']))} ct/kWh"
    if st["neg_hours"] > 0:
        neg_zin = f"{st['neg_hours']} uur met negatieve prijzen"
    else:
        neg_zin = "geen negatieve prijzen"
    beschrijving = (
        f"Stroomprijzen {label}: gemiddeld {nl(ct(st['avg']))} ct/kWh op de EPEX-groothandelsmarkt, "
        f"{neg_zin}. Laagste uur {nl(ct(st['min']))} ct, hoogste {nl(ct(st['max']))} ct. "
        f"Alle dagen in één tabel.")

    vergelijk = vergelijk_zin(ym, st["avg"], all_stats)

    min_d = st["min_ts"]; max_d = st["max_ts"]
    min_str = f"{min_d.day} {MAANDEN[min_d.month - 1]}, {min_d.strftime('%H:%M')}"
    max_str = f"{max_d.day} {MAANDEN[max_d.month - 1]}, {max_d.strftime('%H:%M')}"

    rows = []
    for r in st["days"]:
        d = r["date"]
        dag = f"{DAGEN[d.weekday()][:2]} {d.day} {MAANDEN[d.month - 1][:3]}"
        neg = str(r["neg"]) if r["neg"] else "—"
        rows.append(
            f"        <tr><td>{dag}</td><td>{nl(ct(r['avg']))}</td>"
            f"<td>{nl(ct(r['min']))}</td><td>{nl(ct(r['max']))}</td><td>{neg}</td></tr>")
    tabel = "\n".join(rows)

    nav_links = []
    if prev_ym:
        nav_links.append(f'<a class="maand-nav-link" href="/historisch/{prev_ym}">← {month_label(prev_ym)}</a>')
    nav_links.append('<a class="maand-nav-link" href="/historisch">Alle maanden (interactief)</a>')
    if next_ym:
        nav_links.append(f'<a class="maand-nav-link" href="/historisch/{next_ym}">{month_label(next_ym)} →</a>')
    maand_nav = "\n      ".join(nav_links)

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
                        {"@type": "ListItem", "position": 3, "name": label_cap, "item": url},
                    ],
                },
            },
            {
                "@type": "Dataset",
                "name": f"Day-ahead stroomprijzen Nederland — {label}",
                "description": f"Uurlijkse EPEX day-ahead elektriciteitsprijzen voor Nederland in {label} ({st['hours']} uurprijzen).",
                "temporalCoverage": f"{ym}-01/{ym}-{last_day:02d}",
                "spatialCoverage": "Nederland",
                "inLanguage": "nl-NL",
                "isBasedOn": "https://transparency.entsoe.eu",
                "creator": {"@type": "Organization", "name": "Stroomvoorspeller.nl", "url": SITE},
                "distribution": [{
                    "@type": "DataDownload",
                    "encodingFormat": "application/json",
                    "contentUrl": f"{SITE}/data/archief/{ym}.json",
                }],
            },
        ],
    }, ensure_ascii=False, indent=2)

    vergelijk_html = f"\n        <p class=\"maand-vergelijk\">{vergelijk}</p>" if vergelijk else ""

    return f"""<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>{titel} | Stroomvoorspeller.nl</title>
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
    .maand-hero {{ background: var(--c-surface); border-bottom: 1px solid var(--c-border); padding: 28px 0 22px; }}
    .maand-hero h1 {{ margin: 0 0 6px; }}
    .maand-hero .maand-sub {{ color: var(--c-text-soft); margin: 0; }}
    .maand-kern {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 22px 0; }}
    .maand-kern .kern-item {{ background: var(--c-surface); border: 1px solid var(--c-border); border-radius: 10px; padding: 14px 16px; }}
    .maand-kern .kern-label {{ display: block; font-size: 0.8rem; color: var(--c-text-mute); margin-bottom: 4px; }}
    .maand-kern .kern-value {{ font-size: 1.25rem; font-weight: 700; }}
    .maand-kern .kern-detail {{ display: block; font-size: 0.8rem; color: var(--c-text-soft); margin-top: 2px; }}
    .maand-vergelijk {{ color: var(--c-text-soft); }}
    .maand-tabel {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
    .maand-tabel th, .maand-tabel td {{ padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--c-border); white-space: nowrap; }}
    .maand-tabel th:first-child, .maand-tabel td:first-child {{ text-align: left; }}
    .maand-tabel th {{ font-size: 0.8rem; color: var(--c-text-mute); font-weight: 600; }}
    .maand-nav {{ display: flex; flex-wrap: wrap; gap: 16px; justify-content: space-between; margin: 26px 0 8px; }}
    .maand-nav-link {{ font-weight: 600; }}
    .maand-uitleg {{ max-width: 720px; }}
    @media (max-width: 640px) {{ .maand-kern {{ grid-template-columns: 1fr 1fr; }} }}
  </style>
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
    <section class="maand-hero">
      <div class="container">
        <h1>Stroomprijzen {label}</h1>
        <p class="maand-sub">Kale marktprijzen (EPEX day-ahead) in ct/kWh, zonder belasting en opslag. Wat je zelf betaalt hangt af van je contract; op <a href="/">de actuele pagina</a> rekenen we belasting en opslag wel mee.</p>
      </div>
    </section>

    <section class="section">
      <div class="container">
        <div class="maand-kern">
          <div class="kern-item"><span class="kern-label">Gemiddeld</span><span class="kern-value">{nl(ct(st['avg']))} ct/kWh</span></div>
          <div class="kern-item"><span class="kern-label">Laagste uur</span><span class="kern-value">{nl(ct(st['min']))} ct</span><span class="kern-detail">{min_str}</span></div>
          <div class="kern-item"><span class="kern-label">Hoogste uur</span><span class="kern-value">{nl(ct(st['max']))} ct</span><span class="kern-detail">{max_str}</span></div>
          <div class="kern-item"><span class="kern-label">Negatieve uren</span><span class="kern-value">{st['neg_hours']}</span><span class="kern-detail">kale markt onder 0</span></div>
        </div>{vergelijk_html}

        <h2>Alle dagen van {label}</h2>
        <p class="scroll-hint" aria-hidden="true">Veeg opzij voor alle kolommen →</p>
        <div class="scroll-x">
          <table class="maand-tabel">
            <thead>
              <tr><th>Dag</th><th>Gemiddeld (ct/kWh)</th><th>Laagste</th><th>Hoogste</th><th>Uren onder 0</th></tr>
            </thead>
            <tbody>
{tabel}
            </tbody>
          </table>
        </div>

        <nav class="maand-nav" aria-label="maandnavigatie">
      {maand_nav}
        </nav>

        <div class="maand-uitleg">
          <h2>Over deze cijfers</h2>
          <p>De prijzen komen van de EPEX day-ahead veiling, waar stroom voor elk uur van de volgende dag wordt verhandeld. Dit zijn de prijzen die je terugziet in een dynamisch energiecontract, vóórdat je leverancier er opslag, energiebelasting en btw bij optelt. Negatieve uren betekenen dat producenten betaalden om hun stroom kwijt te kunnen — hoe dat werkt lees je in <a href="/kennisbank/negatieve-stroomprijzen">ons artikel over negatieve stroomprijzen</a>.</p>
          <p>Wil je een losse dag bekijken of maanden naast elkaar leggen? Dat kan op de <a href="/historisch">interactieve historisch-pagina</a>. Bron: <a href="https://transparency.entsoe.eu" rel="noopener">ENTSO-E Transparency</a>.</p>
        </div>
      </div>
    </section>
  </main>

  <footer class="site-footer">
    <div class="container footer-grid">
      <div>
        <p class="footer-brand"><strong>stroomvoorspeller.nl</strong></p>
        <p class="footer-tag">Onafhankelijk, eenvoudig, eerlijk over wat we wel en niet weten.</p>
        <p class="footer-meta">Dagelijkse updates: <a href="https://x.com/stroomtarief" rel="noopener noreferrer">@stroomtarief op X ↗</a></p>
        <p class="footer-meta">
          <a href="/privacy">Privacy</a> ·
          <a href="/cookies">Cookies</a> ·
          <a href="/disclaimer">Disclaimer</a> ·
          <a href="/over/voorspelling">Over de voorspelling</a> ·
          <a href="/aanbieders">Aanbieders</a>
        </p>
      </div>
      <div>
        <p class="footer-meta">
          Bron: <a href="https://transparency.entsoe.eu" rel="noopener">ENTSO-E Transparency</a>
        </p>
        <p class="footer-meta-small">Hobby-project van één persoon.</p>
      </div>
    </div>
  </footer>

  <script src="/nav.js" defer></script>
  <script defer src="https://static.cloudflare.com/beacon.min.js" data-cf-beacon='{{"token": "b0c666a71b274ee7b092122def7755e8"}}'></script>
  <script defer src="/_vercel/insights/script.js"></script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--archive", type=Path, default=ARCHIVE_DIR)
    ap.add_argument("--months", nargs="*", help="alleen deze maanden (YYYY-MM), default: alle volledige")
    args = ap.parse_args()

    today = date.today()
    current_ym = f"{today.year}-{today.month:02d}"

    all_stats: dict[str, dict] = {}
    for f in sorted(args.archive.glob("????-??.json")):
        ym = f.stem
        if ym >= current_ym:
            continue  # lopende (of toekomstige) maand: nooit publiceren
        data = load_month(f)
        if data is None:
            continue
        if not is_complete(ym, data["prices"]):
            print(f"[skip] {ym}: onvolledig", file=sys.stderr)
            continue
        all_stats[ym] = month_stats(data["prices"])

    all_months = sorted(all_stats)
    if args.months:
        targets = [m for m in args.months if m in all_stats]
        missing = set(args.months) - set(targets)
        if missing:
            print(f"[warn] niet beschikbaar/volledig: {', '.join(sorted(missing))}", file=sys.stderr)
    else:
        targets = all_months

    args.out.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    for ym in targets:
        html = build_page(ym, all_stats[ym], all_months, all_stats)
        out_file = args.out / f"{ym}.html"
        if out_file.exists() and out_file.read_text(encoding="utf-8") == html:
            skipped += 1
            continue
        out_file.write_text(html, encoding="utf-8")
        written += 1
    print(f"{written} pagina('s) geschreven, {skipped} ongewijzigd, {len(targets)} totaal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
