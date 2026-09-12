#!/usr/bin/env python3
"""
Schrijft de actuele cijfers als platte HTML in de pagina's.

Waarom: alles op de site wordt client-side getekend door app.js. Crawlers die geen
JavaScript uitvoeren - GPTBot, PerplexityBot, ClaudeBot - zien daardoor overal een
liggend streepje in plaats van een prijs. Dit script vult vaste blokken tussen
HTML-markers met dezelfde cijfers, zodat ze ook zonder JavaScript in de pagina staan.

Blokken en doelbestanden:
  STATIC:PRICES    -> public/index.html            (uurprijzen vandaag + morgen, weekvoorspelling)
  STATIC:TOMORROW  -> public/morgen.html           (uurprijzen morgen)
  STATIC:ACCURACY  -> public/over/performance.html (gemeten nauwkeurigheid)

Het script is idempotent: het vervangt alleen wat tussen de markers staat. Ontbreekt
een marker, dan slaat het dat bestand over zonder te falen. Geen externe dependencies.

Gebruik:
    python scripts/render_static.py
    python scripts/render_static.py --root .   (expliciete repo-root)
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MAANDEN = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]


# ---------------------------------------------------------------- hulpfuncties

def amsterdam_offset(now_utc=None):
    """Zomertijd-offset voor Europe/Amsterdam zonder zoneinfo-afhankelijkheid."""
    now_utc = now_utc or datetime.now(timezone.utc)
    year = now_utc.year
    march = datetime(year, 3, 31, 1, 0, tzinfo=timezone.utc)
    while march.weekday() != 6:
        march -= timedelta(days=1)
    october = datetime(year, 10, 31, 1, 0, tzinfo=timezone.utc)
    while october.weekday() != 6:
        october -= timedelta(days=1)
    return timedelta(hours=2) if march <= now_utc < october else timedelta(hours=1)


def nu_amsterdam():
    return datetime.now(timezone.utc) + amsterdam_offset()


def lees_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - bewust breed: ontbrekend bestand mag niet fataal zijn
        print(f"[render_static] kan {path} niet lezen: {exc}", file=sys.stderr)
        return None


def nl_datum(datum_str, met_dag=True):
    """'2026-09-11' -> 'vrijdag 11 september 2026'."""
    d = datetime.strptime(datum_str, "%Y-%m-%d")
    kern = f"{d.day} {MAANDEN[d.month - 1]} {d.year}"
    return f"{DAGEN[d.weekday()]} {kern}" if met_dag else kern


def ct(waarde, decimalen=1):
    """Getal met Nederlandse komma."""
    return f"{waarde:.{decimalen}f}".replace(".", ",")


def duizend(getal):
    """1234 -> '1.234' (Nederlandse duizendtalscheiding)."""
    return f"{int(getal):,}".replace(",", ".")


def esc(tekst):
    return (
        str(tekst)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------- rekenwerk

class Rekenaar:
    """Zet kale EPEX-prijzen (EUR/MWh) om naar all-in consumentenprijs in ct/kWh."""

    def __init__(self, config):
        taxes = (config or {}).get("taxes", {})
        self.belasting = float(taxes.get("energiebelasting_per_kwh", 0.0916))
        self.btw = float(taxes.get("btw_factor", 1.21))
        self.opslag = 0.0178
        for supplier in (config or {}).get("suppliers", []):
            if supplier.get("id") == "average":
                self.opslag = float(supplier.get("markup_per_kwh", self.opslag))
                break

    def all_in_ct(self, eur_per_mwh):
        kale_eur_kwh = eur_per_mwh / 1000.0
        return (kale_eur_kwh + self.opslag + self.belasting) * self.btw * 100.0

    def kale_ct(self, eur_per_mwh):
        return eur_per_mwh / 10.0


def uren_op_datum(prijzen, datum_str):
    return [p for p in prijzen if str(p.get("time", ""))[:10] == datum_str]


def dag_statistiek(uren, rek):
    if not uren:
        return None
    waarden = [(u["time"][11:16], rek.all_in_ct(u["price"]), rek.kale_ct(u["price"])) for u in uren]
    goedkoopste = min(waarden, key=lambda x: x[1])
    duurste = max(waarden, key=lambda x: x[1])
    gemiddelde = sum(w[1] for w in waarden) / len(waarden)
    negatief = [w for w in waarden if w[1] < 0]
    return {
        "uren": waarden,
        "goedkoopste": goedkoopste,
        "duurste": duurste,
        "gemiddelde": gemiddelde,
        "negatief": negatief,
    }


# Een dag is pas bruikbaar als er vrijwel een volledige set uurprijzen in staat.
# 20 in plaats van 24, zodat de twee dagen per jaar met een zomertijdsprong
# (23 of 25 uur) niet onterecht als incompleet gelden.
MIN_UREN_PER_DAG = 20

# Boven deze leeftijd krijgt het blok een waarschuwende regel. Het blankt de cijfers
# NIET: day-ahead prijzen liggen vast zodra ze gepubliceerd zijn, dus een bestand van
# gisteravond met alle uren van vandaag erin klopt gewoon. De prijsupdate draait
# 's nachts niet, dus een leeftijd van 12 tot 16 uur is doodnormaal.
WAARSCHUW_VANAF_UREN = 30


def leeftijd_uren(generated_at):
    """Hoeveel uur geleden is deze data weggeschreven? None als de stempel onleesbaar is."""
    if not generated_at:
        return None
    try:
        stempel = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stempel.tzinfo is None:
        stempel = stempel.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stempel).total_seconds() / 3600.0


def blok_verouderd(prices, wat):
    """Vervangt het cijferblok als de prijzen voor de gevraagde dag ontbreken.

    Zonder dit vangnet blijft bij een vastgelopen pipeline het oude blok staan, en dan
    beweert de pagina in platte tekst dat de prijs van eergisteren die van vandaag is.
    Een streepje was verkeerd; een verkeerd cijfer met stelligheid is erger.
    """
    stempel = str(prices.get("generated_at", ""))[:16].replace("T", " ")
    return "\n".join([
        '    <section class="static-prices container is-secondary">',
        f'      <h2>{wat}</h2>',
        "      <p>De prijsdata op deze pagina is op dit moment niet compleet genoeg om hier "
        f"als cijfer neer te zetten. De laatste geslaagde update was op {esc(stempel)} UTC. "
        "De grafieken hierboven tonen wat er wél binnen is; de actuele day-ahead prijzen "
        'staan altijd bij <a href="https://transparency.entsoe.eu" rel="noopener">ENTSO-E '
        "Transparency</a>.</p>",
        "    </section>",
    ])


def uur_tot_venster(hhmm):
    uur = int(hhmm[:2])
    return f"{uur:02d}:00&ndash;{(uur + 1) % 24:02d}:00"


# ---------------------------------------------------------------- HTML-blokken

def tabel_html(stat, datum_str):
    rijen = "\n".join(
        f"          <tr><td>{uur_tot_venster(t)}</td>"
        f"<td class=\"num\">{ct(allin)}</td>"
        f"<td class=\"num\">{ct(kaal)}</td></tr>"
        for t, allin, kaal in stat["uren"]
    )
    return (
        '      <div class="static-table-wrap">\n'
        '        <table class="static-price-table">\n'
        f'          <caption>Stroomprijs per uur op {esc(nl_datum(datum_str))}</caption>\n'
        "          <thead><tr><th scope=\"col\">Uur</th>"
        "<th scope=\"col\">All-in (ct/kWh)</th>"
        "<th scope=\"col\">Kale EPEX (ct/kWh)</th></tr></thead>\n"
        "          <tbody>\n"
        f"{rijen}\n"
        "          </tbody>\n"
        "        </table>\n"
        "      </div>"
    )


def zin_over_dag(label, datum_str, stat):
    goed, duur = stat["goedkoopste"], stat["duurste"]
    zin = (
        f"      <p>{label} ({esc(nl_datum(datum_str))}) is de gemiddelde all-in stroomprijs "
        f"<strong>{ct(stat['gemiddelde'])} ct/kWh</strong>. Het goedkoopste uur is "
        f"<strong>{uur_tot_venster(goed[0])} met {ct(goed[1])} ct/kWh</strong>, het duurste "
        f"<strong>{uur_tot_venster(duur[0])} met {ct(duur[1])} ct/kWh</strong>."
    )
    if stat["negatief"]:
        zin += f" In {len(stat['negatief'])} uur is de all-in prijs negatief."
    return zin + "</p>"


def blok_prices(prices, forecast, config):
    rek = Rekenaar(config)
    alle = prices.get("prices", [])
    vandaag = nu_amsterdam().strftime("%Y-%m-%d")
    morgen = (nu_amsterdam() + timedelta(days=1)).strftime("%Y-%m-%d")

    stat_vandaag = dag_statistiek(uren_op_datum(alle, vandaag), rek)
    stat_morgen = dag_statistiek(uren_op_datum(alle, morgen), rek)

    leeftijd = leeftijd_uren(prices.get("generated_at"))
    if not stat_vandaag or len(stat_vandaag["uren"]) < MIN_UREN_PER_DAG:
        n = len(stat_vandaag["uren"]) if stat_vandaag else 0
        print(f"[render_static] STATIC:PRICES -> vangnet ({n} uurprijzen voor vandaag)",
              file=sys.stderr)
        return blok_verouderd(prices, "Stroomprijzen per uur, in cijfers")

    delen = [
        '    <section class="static-prices container is-secondary" id="uurprijzen" aria-labelledby="uurprijzen-h2">',
        '      <h2 id="uurprijzen-h2">Stroomprijzen per uur, in cijfers</h2>',
        zin_over_dag("Vandaag", vandaag, stat_vandaag),
    ]
    if stat_morgen:
        delen.append(zin_over_dag("Morgen", morgen, stat_morgen))
    else:
        delen.append(
            "      <p>De prijzen voor morgen zijn nog niet bekend. EPEX Spot publiceert ze "
            "doorgaans rond 14:00 Nederlandse tijd.</p>"
        )

    delen.append(
        "      <p>All-in betekent: kale EPEX-prijs plus de gemiddelde inkoopvergoeding van "
        f"{ct(rek.opslag * 100, 2)} ct/kWh en de energiebelasting van "
        f"{ct(rek.belasting * 100, 2)} ct/kWh, alles maal {ct(rek.btw, 2)} btw.</p>"
    )

    delen.append('      <details class="static-details">')
    delen.append("        <summary>Alle uurprijzen van vandaag in een tabel</summary>")
    delen.append(tabel_html(stat_vandaag, vandaag))
    delen.append("      </details>")
    if stat_morgen:
        delen.append('      <details class="static-details">')
        delen.append("        <summary>Alle uurprijzen van morgen in een tabel</summary>")
        delen.append(tabel_html(stat_morgen, morgen))
        delen.append("      </details>")

    voorspel = blok_voorspelling_tabel(forecast, rek)
    if voorspel:
        delen.append(voorspel)

    bron = prices.get("generated_at", "")
    meta = ('      <p class="static-prices-meta">Bron: EPEX Spot via ENTSO-E Transparency '
            f"(NL bidding zone). Cijfers bijgewerkt op {esc(bron[:16].replace('T', ' '))} UTC.")
    if leeftijd is not None and leeftijd > WAARSCHUW_VANAF_UREN:
        meta += (f" Let op: dat is {leeftijd:.0f} uur geleden, langer dan gebruikelijk. "
                 "De prijzen hieronder kloppen wel, maar de voorspelling verderop kan "
                 "achterlopen.")
    delen.append(meta + "</p>")
    delen.append("    </section>")
    return "\n".join(delen)


def blok_voorspelling_tabel(forecast, rek):
    if not forecast:
        return None
    per_dag = {}
    for f in forecast.get("forecasts", []):
        datum = str(f.get("time", ""))[:10]
        if not datum:
            continue
        per_dag.setdefault(datum, []).append(f)
    if not per_dag:
        return None

    rijen = []
    for datum in sorted(per_dag)[:7]:
        uren = per_dag[datum]
        allin = [rek.all_in_ct(u["predicted"]) for u in uren if u.get("predicted") is not None]
        if not allin:
            continue
        laagste = min(allin)
        hoogste = max(allin)
        gemiddeld = sum(allin) / len(allin)
        goedkoopste_uur = min(uren, key=lambda u: u["predicted"])["time"][11:16]
        rijen.append(
            f"          <tr><td>{esc(nl_datum(datum))}</td>"
            f"<td class=\"num\">{ct(gemiddeld)}</td>"
            f"<td class=\"num\">{ct(laagste)}</td>"
            f"<td class=\"num\">{ct(hoogste)}</td>"
            f"<td>{uur_tot_venster(goedkoopste_uur)}</td></tr>"
        )
    if not rijen:
        return None

    versie = esc(forecast.get("model_version", ""))
    return (
        '      <h3>Voorspelling voor de komende dagen</h3>\n'
        "      <p>Onderstaande cijfers komen uit het eigen voorspellingsmodel "
        f"(versie {versie}) en zijn all-in ct/kWh. Het zijn schattingen, geen "
        "day-ahead prijzen: die staan pas vast zodra EPEX Spot ze publiceert.</p>\n"
        '      <div class="static-table-wrap">\n'
        '        <table class="static-price-table">\n'
        "          <caption>Voorspelde stroomprijs per dag, all-in ct/kWh</caption>\n"
        '          <thead><tr><th scope="col">Dag</th><th scope="col">Gemiddeld</th>'
        '<th scope="col">Laagste</th><th scope="col">Hoogste</th>'
        '<th scope="col">Goedkoopste uur</th></tr></thead>\n'
        "          <tbody>\n" + "\n".join(rijen) + "\n"
        "          </tbody>\n"
        "        </table>\n"
        "      </div>"
    )


def blok_tomorrow(prices, config):
    rek = Rekenaar(config)
    alle = prices.get("prices", [])
    morgen = (nu_amsterdam() + timedelta(days=1)).strftime("%Y-%m-%d")
    stat = dag_statistiek(uren_op_datum(alle, morgen), rek)
    if not stat:
        return (
            '    <section class="static-prices container">\n'
            "      <p>De stroomprijzen voor morgen zijn nog niet gepubliceerd. EPEX Spot "
            "maakt ze doorgaans rond 14:00 Nederlandse tijd bekend.</p>\n"
            "    </section>"
        )
    return "\n".join([
        '    <section class="static-prices container is-secondary" aria-labelledby="morgen-cijfers-h2">',
        '      <h2 id="morgen-cijfers-h2">De prijzen van morgen in cijfers</h2>',
        zin_over_dag("Morgen", morgen, stat),
        '      <details class="static-details" open>',
        "        <summary>Alle uurprijzen van morgen in een tabel</summary>",
        tabel_html(stat, morgen),
        "      </details>",
        '      <p class="static-prices-meta">Bron: EPEX Spot via ENTSO-E Transparency '
        f"(NL bidding zone). Bijgewerkt op {esc(str(prices.get('generated_at', ''))[:16].replace('T', ' '))} UTC.</p>",
        "    </section>",
    ])


def blok_accuracy(performance):
    if not performance:
        return None
    overall = performance.get("overall") or {}
    mae_ct = overall.get("mae_eur_mwh")
    if mae_ct is None:
        return None
    mae_ct = mae_ct / 10.0
    richting = (overall.get("direction_hit_rate") or 0) * 100
    band = (overall.get("within_band_pct") or 0) * 100
    naive = overall.get("mae_vs_naive_pct")
    uren = overall.get("n_hours", 0)
    versie = esc(performance.get("model_version", ""))
    eerste = performance.get("first_date", "")
    laatste = performance.get("last_date", "")

    horizon_rijen = []
    for h in performance.get("by_horizon", []):
        if h.get("mae_eur_mwh") is None:
            continue
        horizon_rijen.append(
            f"          <tr><td>{int(h.get('horizon_days', 0))} dagen vooruit</td>"
            f"<td class=\"num\">{ct(h['mae_eur_mwh'] / 10.0, 2)}</td>"
            f"<td class=\"num\">{ct((h.get('direction_hit_rate') or 0) * 100)}%</td>"
            f"<td class=\"num\">{int(h.get('n_hours', 0))}</td></tr>"
        )

    beter = ""
    if naive is not None:
        beter = (
            f" Dat is {ct(abs(naive))}% "
            f"{'beter' if naive < 0 else 'slechter'} dan de simpele benchmark: "
            "dezelfde dag vorige week."
        )

    h_min = performance.get("horizon_min_days", 2)
    h_max = performance.get("horizon_max_days", 7)
    dagen = performance.get("window_days_actual")
    venster = f", een venster van {dagen} dagen" if dagen else ""

    delen = [
        '    <section class="perf-section static-prices" aria-labelledby="cijfers-h2">',
        '      <h2 id="cijfers-h2">De cijfers, in tekst</h2>',
        f"      <p>Het voorspellingsmodel (versie {versie}) zit gemiddeld "
        f"<strong>{ct(mae_ct, 2)} ct/kWh</strong> naast de werkelijke prijs op een horizon "
        f"van <strong>{h_min} tot {h_max} dagen vooruit</strong>, gemeten over "
        f"<strong>{duizend(uren)} uur</strong> tussen {esc(nl_datum(eerste, met_dag=False))} "
        f"en {esc(nl_datum(laatste, met_dag=False))}{venster}.{beter} "
        f"De richting - duurder of goedkoper dan normaal - klopt in "
        f"<strong>{ct(richting)}%</strong> van de uren, en "
        f"<strong>{ct(band)}%</strong> van de voorspellingen valt binnen de getoonde "
        "onzekerheidsband.</p>",
        "      <p>Dag 1 telt niet mee in dat cijfer. De prijzen voor morgen worden rond "
        "14:00 door EPEX gepubliceerd en staan bij de dagelijkse meting dus al vast; "
        "meetellen zou de uitslag meten in plaats van de voorspelling. Cijfers van "
        "anderen die één dag vooruit meten, gaan over een makkelijkere opgave.</p>",
    ]

    d1 = performance.get("d1_preauction")
    if d1 and d1.get("mae_eur_mwh") is not None:
        delen.append(
            "      <p>Apart gemeten, en wél vergelijkbaar met een cijfer van "
            "één dag vooruit: een voorspelling die ’s ochtends wordt "
            "vastgelegd, vóórdat de veiling sluit, zit gemiddeld "
            f"<strong>{ct(d1['mae_eur_mwh'] / 10.0, 2)} ct/kWh</strong> naast de prijs "
            f"die EPEX later die dag publiceert, over {duizend(d1['n_hours'])} uur "
            f"verdeeld over {d1['n_days']} dagen.</p>"
        )
    if horizon_rijen:
        delen += [
            '      <div class="static-table-wrap">',
            '        <table class="static-price-table">',
            "          <caption>Nauwkeurigheid per voorspelhorizon</caption>",
            '          <thead><tr><th scope="col">Horizon</th>'
            '<th scope="col">Gemiddeld ernaast (ct/kWh)</th>'
            '<th scope="col">Richting goed</th>'
            '<th scope="col">Uren gemeten</th></tr></thead>',
            "          <tbody>",
            "\n".join(horizon_rijen),
            "          </tbody>",
            "        </table>",
            "      </div>",
        ]
    delen.append(
        '      <p class="static-prices-meta">Elke voorspelling wordt achteraf getoetst aan '
        "de werkelijke ENTSO-E-prijzen. Bij een nieuwe modelversie begint de meting "
        "opnieuw, dus het venster kan korter zijn dan 30 dagen.</p>"
    )
    delen.append("    </section>")
    return "\n".join(delen)


def blok_freshness(prices, url, webpage_id, naam, ook_dataset=False):
    """JSON-LD-blok met dateModified voor een pagina die elke paar uur verandert.

    Het @id is gelijk aan dat van een eventueel bestaande WebPage-node, zodat de
    twee blokken in de JSON-LD-graaf samensmelten in plaats van elkaar tegen te
    spreken.
    """
    gegenereerd = str(prices.get("generated_at", ""))
    if not gegenereerd:
        return None
    # Microseconden eruit: schema.org wil ISO 8601, zoekmachines lezen dit netter.
    gegenereerd = re.sub(r"\.\d+(?=[+\-Z])", "", gegenereerd)
    nodes = [{
        "@type": "WebPage",
        "@id": webpage_id,
        "url": url,
        "name": naam,
        "inLanguage": "nl-NL",
        "isPartOf": {"@id": "https://stroomvoorspeller.nl/#website"},
        "dateModified": gegenereerd,
    }]
    if ook_dataset:
        nodes.append({
            "@type": "Dataset",
            "@id": "https://stroomvoorspeller.nl/#dataset-prices",
            "dateModified": gegenereerd,
            "temporalCoverage": temporele_dekking(prices),
        })
    payload = {"@context": "https://schema.org", "@graph": nodes}
    return ('  <script type="application/ld+json">\n  '
            + json.dumps(payload, ensure_ascii=False, indent=2).replace("\n", "\n  ")
            + "\n  </script>")


def temporele_dekking(prices):
    tijden = [str(p.get("time", "")) for p in prices.get("prices", []) if p.get("time")]
    if not tijden:
        return None
    return f"{min(tijden)}/{max(tijden)}"


FRESHNESS_PAGINAS = [
    ("public/index.html", "https://stroomvoorspeller.nl/",
     "https://stroomvoorspeller.nl/#webpage",
     "Stroomprijzen vandaag, morgen en komende week", True),
    ("public/morgen.html", "https://stroomvoorspeller.nl/morgen",
     "https://stroomvoorspeller.nl/morgen",
     "Stroomprijs morgen", False),
    ("public/aanbieders.html", "https://stroomvoorspeller.nl/aanbieders",
     "https://stroomvoorspeller.nl/aanbieders",
     "Dynamische energieaanbieders vergeleken", False),
    ("public/historisch.html", "https://stroomvoorspeller.nl/historisch",
     "https://stroomvoorspeller.nl/historisch",
     "Historische stroomprijzen", False),
]


# ---------------------------------------------------------------- schrijven

def vervang_blok(pad, naam, inhoud):
    """Vervangt alles tussen <!-- naam:START --> en <!-- naam:END -->."""
    path = Path(pad)
    if not path.exists():
        print(f"[render_static] {pad} bestaat niet - overgeslagen", file=sys.stderr)
        return False
    html = path.read_text(encoding="utf-8")
    start = f"<!-- {naam}:START -->"
    eind = f"<!-- {naam}:END -->"
    patroon = re.compile(r"(?P<indent>[ \t]*)" + re.escape(start) + r".*?" + re.escape(eind), re.S)
    m = patroon.search(html)
    if not m:
        print(f"[render_static] marker {naam} niet gevonden in {pad} - overgeslagen", file=sys.stderr)
        return False
    indent = m.group("indent")
    nieuw = patroon.sub(lambda _m: f"{indent}{start}\n{inhoud}\n{indent}{eind}", html, count=1)
    if nieuw == html:
        print(f"[render_static] {naam} in {pad}: ongewijzigd")
        return False
    path.write_text(nieuw, encoding="utf-8")
    print(f"[render_static] {naam} in {pad}: bijgewerkt")
    return True


def main():
    parser = argparse.ArgumentParser(description="Schrijf actuele cijfers als statische HTML.")
    parser.add_argument("--root", default=".", help="repo-root (standaard: huidige map)")
    args = parser.parse_args()
    root = Path(args.root)
    data = root / "public" / "data"

    prices = lees_json(data / "prices.json")
    config = lees_json(data / "config.json")
    forecast = lees_json(data / "forecast.json")
    performance = lees_json(data / "performance.json")

    if not prices:
        print("[render_static] prices.json ontbreekt - niets te doen", file=sys.stderr)
        return 0

    gewijzigd = 0

    blok = blok_prices(prices, forecast, config)
    if blok:
        gewijzigd += vervang_blok(root / "public" / "index.html", "STATIC:PRICES", blok)

    blok = blok_tomorrow(prices, config)
    if blok:
        gewijzigd += vervang_blok(root / "public" / "morgen.html", "STATIC:TOMORROW", blok)

    blok = blok_accuracy(performance)
    if blok:
        gewijzigd += vervang_blok(
            root / "public" / "over" / "performance.html", "STATIC:ACCURACY", blok
        )

    for pad, url, webpage_id, naam, ook_dataset in FRESHNESS_PAGINAS:
        blok = blok_freshness(prices, url, webpage_id, naam, ook_dataset)
        if blok:
            gewijzigd += vervang_blok(root / pad, "STATIC:FRESHNESS", blok)

    print(f"[render_static] klaar - {gewijzigd} blok(ken) gewijzigd")
    return 0


if __name__ == "__main__":
    sys.exit(main())
