#!/usr/bin/env python3
"""
Records uit het prijsarchief: public/data/records.json + het blok op /records.

Waarom: het archief gaat terug tot 2015 en die reeks is nergens anders publiek te
vinden. Records zijn bovendien het soort feit dat mensen onthouden en dat AI-engines
graag citeren - mits het als platte tekst en als tabel in de pagina staat.

Alle bedragen zijn KALE EPEX-prijzen. Bewust geen all-in: de energiebelasting is sinds
2015 meermalen veranderd, dus een all-in vergelijking tussen 2015 en nu zou meer over
de belasting zeggen dan over de markt.

Gebruik:
    python scripts/generate_records.py
"""

import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIEF = ROOT / "public" / "data" / "archief"
UIT_JSON = ROOT / "public" / "data" / "records.json"
UIT_HTML = ROOT / "public" / "records.html"

DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni",
           "juli", "augustus", "september", "oktober", "november", "december"]


def nl_datum(d):
    return f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]} {d.year}"


def nl_maand(ym):
    jaar, maand = ym.split("-")
    return f"{MAANDEN[int(maand) - 1]} {jaar}"


def getal(x, decimalen=2):
    return f"{x:,.{decimalen}f}".replace(",", "~").replace(".", ",").replace("~", ".")


def esc(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def uur_venster(dt):
    return f"{dt.hour:02d}:00&ndash;{(dt.hour + 1) % 24:02d}:00"


# ---------------------------------------------------------------- inlezen

def lees_archief():
    """Alle uurprijzen uit het archief als lijst van (datetime, eur_per_mwh)."""
    if not ARCHIEF.exists():
        print(f"[records] archiefmap ontbreekt: {ARCHIEF}", file=sys.stderr)
        return []
    uren = []
    for pad in sorted(ARCHIEF.glob("*.json")):
        try:
            data = json.loads(pad.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[records] overslaan {pad.name}: {exc}", file=sys.stderr)
            continue
        for p in data.get("prices", []):
            t, prijs = p.get("time"), p.get("price")
            if t is None or prijs is None:
                continue
            try:
                uren.append((datetime.fromisoformat(t), float(prijs)))
            except ValueError:
                continue
    uren.sort(key=lambda x: x[0])
    return uren


# ---------------------------------------------------------------- rekenen

def bereken(uren):
    if not uren:
        return None

    per_dag = defaultdict(list)
    per_maand = defaultdict(list)
    per_jaar = defaultdict(list)
    for dt, prijs in uren:
        per_dag[dt.date()].append((dt, prijs))
        per_maand[dt.strftime("%Y-%m")].append(prijs)
        per_jaar[dt.year].append((dt, prijs))

    # Volledige dagen (>= 23 uur) voor dagrecords, anders vertekent een half
    # ingelezen dag het gemiddelde.
    volle_dagen = {d: v for d, v in per_dag.items() if len(v) >= 23}
    dag_gem = {d: sum(p for _, p in v) / len(v) for d, v in volle_dagen.items()}

    laagste_uur = min(uren, key=lambda x: x[1])
    hoogste_uur = max(uren, key=lambda x: x[1])
    goedkoopste_dag = min(dag_gem.items(), key=lambda x: x[1]) if dag_gem else None
    duurste_dag = max(dag_gem.items(), key=lambda x: x[1]) if dag_gem else None

    neg_per_dag = {d: sum(1 for _, p in v if p < 0) for d, v in per_dag.items()}
    meeste_neg_dag = max(neg_per_dag.items(), key=lambda x: x[1]) if neg_per_dag else None

    # Langste aaneengesloten reeks negatieve uren (gaten in de reeks breken de streak)
    langste, huidige, start, beste_start = 0, 0, None, None
    vorige_dt = None
    for dt, prijs in uren:
        aansluitend = vorige_dt is not None and (dt - vorige_dt).total_seconds() == 3600
        if prijs < 0:
            if huidige and aansluitend:
                huidige += 1
            else:
                huidige, start = 1, dt
            if huidige > langste:
                langste, beste_start = huidige, start
        else:
            huidige = 0
        vorige_dt = dt

    maand_gem = {m: sum(v) / len(v) for m, v in per_maand.items() if len(v) >= 300}
    neg_per_maand = {m: sum(1 for p in v if p < 0) for m, v in per_maand.items()}
    neg_per_jaar = {j: sum(1 for _, p in v if p < 0) for j, v in per_jaar.items()}
    uren_per_jaar = {j: len(v) for j, v in per_jaar.items()}

    return {
        "gegenereerd_op": datetime.now().astimezone().isoformat(timespec="seconds"),
        "bron": "ENTSO-E Transparency, NL bidding zone, kale EPEX day-ahead prijzen",
        "eenheid": "EUR/MWh",
        "dekking": {
            "eerste_uur": uren[0][0].isoformat(),
            "laatste_uur": uren[-1][0].isoformat(),
            "aantal_uren": len(uren),
            "aantal_dagen": len(per_dag),
        },
        "laagste_uurprijs": {"tijd": laagste_uur[0].isoformat(), "eur_mwh": round(laagste_uur[1], 2)},
        "hoogste_uurprijs": {"tijd": hoogste_uur[0].isoformat(), "eur_mwh": round(hoogste_uur[1], 2)},
        "goedkoopste_dag": ({"datum": str(goedkoopste_dag[0]), "gemiddeld_eur_mwh": round(goedkoopste_dag[1], 2)}
                            if goedkoopste_dag else None),
        "duurste_dag": ({"datum": str(duurste_dag[0]), "gemiddeld_eur_mwh": round(duurste_dag[1], 2)}
                        if duurste_dag else None),
        "goedkoopste_maand": (lambda m: {"maand": m[0], "gemiddeld_eur_mwh": round(m[1], 2)})(
            min(maand_gem.items(), key=lambda x: x[1])) if maand_gem else None,
        "duurste_maand": (lambda m: {"maand": m[0], "gemiddeld_eur_mwh": round(m[1], 2)})(
            max(maand_gem.items(), key=lambda x: x[1])) if maand_gem else None,
        "meeste_negatieve_uren_op_een_dag": ({"datum": str(meeste_neg_dag[0]), "uren": meeste_neg_dag[1]}
                                             if meeste_neg_dag and meeste_neg_dag[1] else None),
        "langste_reeks_negatieve_uren": ({"start": beste_start.isoformat(), "uren": langste}
                                         if langste else None),
        "meeste_negatieve_uren_in_een_maand": (lambda m: {"maand": m[0], "uren": m[1]})(
            max(neg_per_maand.items(), key=lambda x: x[1])) if neg_per_maand else None,
        "negatieve_uren_per_jaar": [
            {"jaar": j, "negatieve_uren": neg_per_jaar[j], "gemeten_uren": uren_per_jaar[j]}
            for j in sorted(neg_per_jaar)
        ],
        "totaal_negatieve_uren": sum(neg_per_jaar.values()),
    }


# ---------------------------------------------------------------- HTML

def ct(eur_mwh):
    """EUR/MWh naar ct/kWh."""
    return eur_mwh / 10.0


def blok_html(r):
    d = r["dekking"]
    eerste = datetime.fromisoformat(d["eerste_uur"])
    laatste = datetime.fromisoformat(d["laatste_uur"])
    lu = datetime.fromisoformat(r["laagste_uurprijs"]["tijd"])
    hu = datetime.fromisoformat(r["hoogste_uurprijs"]["tijd"])

    rijen = []

    def rij(wat, waarde, wanneer):
        rijen.append(f"          <tr><th scope=\"row\">{wat}</th>"
                     f"<td class=\"num\">{waarde}</td><td>{wanneer}</td></tr>")

    rij("Laagste uurprijs ooit",
        f"{getal(ct(r['laagste_uurprijs']['eur_mwh']))} ct/kWh",
        f"{esc(nl_datum(lu))}, {uur_venster(lu)}")
    rij("Hoogste uurprijs ooit",
        f"{getal(ct(r['hoogste_uurprijs']['eur_mwh']))} ct/kWh",
        f"{esc(nl_datum(hu))}, {uur_venster(hu)}")
    if r["goedkoopste_dag"]:
        dd = datetime.fromisoformat(r["goedkoopste_dag"]["datum"])
        rij("Goedkoopste dag (daggemiddelde)",
            f"{getal(ct(r['goedkoopste_dag']['gemiddeld_eur_mwh']))} ct/kWh", esc(nl_datum(dd)))
    if r["duurste_dag"]:
        dd = datetime.fromisoformat(r["duurste_dag"]["datum"])
        rij("Duurste dag (daggemiddelde)",
            f"{getal(ct(r['duurste_dag']['gemiddeld_eur_mwh']))} ct/kWh", esc(nl_datum(dd)))
    if r["goedkoopste_maand"]:
        rij("Goedkoopste maand",
            f"{getal(ct(r['goedkoopste_maand']['gemiddeld_eur_mwh']))} ct/kWh",
            esc(nl_maand(r["goedkoopste_maand"]["maand"])))
    if r["duurste_maand"]:
        rij("Duurste maand",
            f"{getal(ct(r['duurste_maand']['gemiddeld_eur_mwh']))} ct/kWh",
            esc(nl_maand(r["duurste_maand"]["maand"])))
    if r["meeste_negatieve_uren_op_een_dag"]:
        dd = datetime.fromisoformat(r["meeste_negatieve_uren_op_een_dag"]["datum"])
        rij("Meeste negatieve uren op één dag",
            f"{r['meeste_negatieve_uren_op_een_dag']['uren']} uur", esc(nl_datum(dd)))
    if r["langste_reeks_negatieve_uren"]:
        ds = datetime.fromisoformat(r["langste_reeks_negatieve_uren"]["start"])
        rij("Langste reeks negatieve uren achter elkaar",
            f"{r['langste_reeks_negatieve_uren']['uren']} uur",
            f"vanaf {esc(nl_datum(ds))}, {uur_venster(ds)}")
    if r["meeste_negatieve_uren_in_een_maand"]:
        rij("Meeste negatieve uren in één maand",
            f"{r['meeste_negatieve_uren_in_een_maand']['uren']} uur",
            esc(nl_maand(r["meeste_negatieve_uren_in_een_maand"]["maand"])))

    jaarrijen = "\n".join(
        f"          <tr><td>{j['jaar']}</td>"
        f"<td class=\"num\">{j['negatieve_uren']}</td>"
        f"<td class=\"num\">{getal(j['negatieve_uren'] / j['gemeten_uren'] * 100, 1)}%</td></tr>"
        for j in r["negatieve_uren_per_jaar"]
    )

    return "\n".join([
        '    <section class="static-prices container" aria-labelledby="records-h2">',
        '      <h2 id="records-h2">De records</h2>',
        f"      <p>Gemeten over <strong>{getal(d['aantal_uren'], 0)} uurprijzen</strong> "
        f"tussen {esc(nl_datum(eerste))} en {esc(nl_datum(laatste))} "
        f"({getal(d['aantal_dagen'], 0)} dagen). Alle bedragen zijn kale EPEX-prijzen, "
        "zonder energiebelasting, opslag en btw.</p>",
        '      <div class="static-table-wrap">',
        '        <table class="static-price-table">',
        "          <caption>Uiterste waarden in de Nederlandse day-ahead markt</caption>",
        '          <thead><tr><th scope="col">Record</th><th scope="col">Waarde</th>'
        '<th scope="col">Wanneer</th></tr></thead>',
        "          <tbody>",
        "\n".join(rijen),
        "          </tbody>",
        "        </table>",
        "      </div>",
        "      <h3>Negatieve uren per jaar</h3>",
        "      <p>Bij een negatieve prijs krijg je geld toe om stroom af te nemen, en betaal "
        f"je om terug te leveren. In totaal gebeurde dat <strong>{getal(r['totaal_negatieve_uren'], 0)} "
        "keer</strong> in deze reeks.</p>",
        '      <div class="static-table-wrap">',
        '        <table class="static-price-table">',
        "          <caption>Aantal uren met een negatieve day-ahead prijs, per jaar</caption>",
        '          <thead><tr><th scope="col">Jaar</th><th scope="col">Negatieve uren</th>'
        '<th scope="col">Aandeel van het jaar</th></tr></thead>',
        "          <tbody>",
        jaarrijen,
        "          </tbody>",
        "        </table>",
        "      </div>",
        '      <p class="static-prices-meta">Bron: ENTSO-E Transparency, NL bidding zone. '
        f"Bijgewerkt op {esc(r['gegenereerd_op'][:16].replace('T', ' '))}.</p>",
        "    </section>",
    ])


def vervang_blok(pad, naam, inhoud):
    path = Path(pad)
    if not path.exists():
        print(f"[records] {pad} bestaat niet - overgeslagen", file=sys.stderr)
        return False
    html = path.read_text(encoding="utf-8")
    start, eind = f"<!-- {naam}:START -->", f"<!-- {naam}:END -->"
    patroon = re.compile(r"(?P<indent>[ \t]*)" + re.escape(start) + r".*?" + re.escape(eind), re.S)
    m = patroon.search(html)
    if not m:
        print(f"[records] marker {naam} niet gevonden in {pad}", file=sys.stderr)
        return False
    ind = m.group("indent")
    nieuw = patroon.sub(lambda _m: f"{ind}{start}\n{inhoud}\n{ind}{eind}", html, count=1)
    if nieuw == html:
        print(f"[records] {naam}: ongewijzigd")
        return False
    path.write_text(nieuw, encoding="utf-8")
    print(f"[records] {naam}: bijgewerkt")
    return True


def main():
    uren = lees_archief()
    if not uren:
        print("[records] geen archiefdata - niets te doen", file=sys.stderr)
        return 0
    r = bereken(uren)
    UIT_JSON.parent.mkdir(parents=True, exist_ok=True)
    nieuw = json.dumps(r, ensure_ascii=False, indent=2)
    oud = UIT_JSON.read_text(encoding="utf-8") if UIT_JSON.exists() else None
    # gegenereerd_op verschilt altijd; vergelijk de inhoud zonder die regel
    def zonder_stempel(t):
        return re.sub(r'"gegenereerd_op": "[^"]*",\n', "", t) if t else t
    if zonder_stempel(oud) != zonder_stempel(nieuw):
        UIT_JSON.write_text(nieuw, encoding="utf-8")
        print(f"[records] records.json geschreven ({len(uren)} uren)")
    else:
        print("[records] records.json ongewijzigd")
    vervang_blok(UIT_HTML, "STATIC:RECORDS", blok_html(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
