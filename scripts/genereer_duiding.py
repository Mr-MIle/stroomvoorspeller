#!/usr/bin/env python3
"""
genereer_duiding.py — automatische prijsduiding voor stroomvoorspeller.nl.

Filosofie (zelfde als forecast.py / event_plausibility.py):
- Deterministisch en herleidbaar. Geen ML, geen verzonnen cijfers.
- De FEITEN komen uit data (werkelijke prijzen + de factor-attributie die het
  forecast-model al per uur schrijft). De TAAL komt los daarvan tot stand.
- Wat niet in het skelet staat, mag niet in de tekst belanden.

Pijplijn:
    prijzen (+ optioneel forecast-factoren)
        -> bouw_skelet()     # de feiten (technisch, voor controle/anomalie)
        -> bouw_publiek()    # de B2-tekst die de bezoeker leest (duiding.json)

Twee databronnen voor de drivers, in volgorde van voorkeur:
  1. forecast.json `factors` (zon/wind/temp/gas/uurpatroon) — exact, uit het model.
     In productie draait dit script ná run_forecast.py en kan het die factoren lezen.
  2. Geen factoren beschikbaar -> "prijs-modus": de driver wordt afgeleid uit de
     vorm van de prijscurve zelf (middagdal = zon, avondpiek = wegvallende zon)
     plus het deterministische uurpatroon. Duidelijk gelabeld als afleiding.

Belasting/all-in volgt de site-conventie (ha.json):
    all_in = (markt[EUR/kWh] + energiebelasting + gem. opslag) * btw
en de regel: "gratis" alleen als all_in < 0; anders "markt negatief".

Gebruik:
    genereer_duiding.py --prod [ha.json] [duiding.json]    # productie
    genereer_duiding.py <prijzen.json> [--datum YYYY-MM-DD] # backtest
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Belastingconfig — spiegelt ha.json. Pas hier aan als het jaar wisselt.
# ---------------------------------------------------------------------------
ENERGY_TAX = 0.0916      # EUR/kWh
AVG_MARKUP = 0.0178      # EUR/kWh
VAT = 1.21

WEEKDAGEN_NL = ["maandag", "dinsdag", "woensdag", "donderdag",
                "vrijdag", "zaterdag", "zondag"]

# NL-feestdagen 2026 (minimaal; vul aan indien nodig).
NL_FEESTDAGEN_2026 = {
    "2026-01-01", "2026-04-05", "2026-04-06", "2026-04-27",
    "2026-05-14", "2026-05-24", "2026-05-25", "2026-12-25", "2026-12-26",
}

# Drempels voor anomalie-detectie (de "event_plausibility"-gedachte).
ANOM_PIEK_FACTOR = 3.0   # piek > 3x het dagmidden -> mogelijk extra oorzaak
ANOM_PIEK_ABS = 300.0    # of piek > 300 EUR/MWh absoluut

BRONNEN = {
    "prijs": "ENTSO-E day-ahead (EPEX NL) — https://transparency.entsoe.eu",
    "weer": "Open-Meteo De Bilt (zon/wind/temp) — https://open-meteo.com",
    "gas": "TTF front-month — ICE / Yahoo Finance TTF=F",
    "outages": "ENTSO-E Transparency, Unavailability of Production Units",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def all_in(market_mwh: float) -> float:
    """Kale marktprijs (EUR/MWh) -> indicatieve all-in consumentenprijs (EUR/kWh)."""
    return (market_mwh / 1000.0 + ENERGY_TAX + AVG_MARKUP) * VAT


def is_feestdag(d: str) -> bool:
    return d in NL_FEESTDAGEN_2026


def daytype(dt: datetime, d: str) -> str:
    if is_feestdag(d):
        return "feestdag"
    return "weekend" if dt.weekday() >= 5 else "werkdag"


def _is_zomer(month: int) -> bool:
    return 4 <= month <= 9


def median(xs: list) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _runs(hours: list) -> list:
    """Groepeer een gesorteerde lijst uren in aaneengesloten blokken (start, eind)."""
    if not hours:
        return []
    hours = sorted(hours)
    blocks = []
    start = prev = hours[0]
    for h in hours[1:]:
        if h == prev + 1:
            prev = h
        else:
            blocks.append((start, prev))
            start = prev = h
    blocks.append((start, prev))
    return blocks


def fmt_uur(h: int) -> str:
    return f"{h:02d}:00"


def fmt_window(a: int, b: int) -> str:
    return f"{a:02d}-{(b + 1) % 24:02d}u" if a != b else f"{a:02d}-{(a + 1) % 24:02d}u"


# ---------------------------------------------------------------------------
# Kern: bouw het feiten-skelet voor één dag (technisch)
# ---------------------------------------------------------------------------
def bouw_skelet(date: str, prices: list, factors_per_hour: list | None = None) -> dict:
    """
    date:   'YYYY-MM-DD'
    prices: 24 (of 23) waarden EUR/MWh; None voor een ontbrekend uur.
    factors_per_hour: optioneel, de `factors`-array per uur uit forecast.json.
    """
    dt = datetime.fromisoformat(date)
    dtype = daytype(dt, date)

    pairs = [(h, p) for h, p in enumerate(prices) if p is not None]
    vals = [p for _, p in pairs]
    missing = [h for h, p in enumerate(prices) if p is None]

    day_avg = sum(vals) / len(vals)
    day_med = median(vals)
    min_h, min_p = min(pairs, key=lambda x: x[1])
    max_h, max_p = max(pairs, key=lambda x: x[1])

    neg_hours = [h for h, p in pairs if p < 0]
    allin_neg = [h for h, p in pairs if all_in(p) < 0]

    # Daluren (zonnedal): aaneengesloten goedkoopste blok midden op de dag.
    dal_drempel = min(0.25 * day_med, 5.0)
    dal_hours = [h for h, p in pairs if p <= dal_drempel]
    dal_block = next((b for b in _runs(dal_hours) if b[0] <= min_h <= b[1]), None)

    # Piekblok: aaneengesloten duurste blok rond het dagmaximum.
    piek_drempel = 0.80 * max_p
    piek_hours = [h for h, p in pairs if p >= piek_drempel]
    piek_block = next((b for b in _runs(piek_hours) if b[0] <= max_h <= b[1]), None)

    windows = []
    if dal_block:
        a, b = dal_block
        drivers = ["zon"] if any(9 <= h <= 17 for h in range(a, b + 1)) else ["lage vraag"]
        mag = "diep negatief" if min_p < 0 else ("rond nul" if min_p < 5 else "laag")
        windows.append({
            "kind": "dal", "window": fmt_window(a, b),
            "extreme_hour": fmt_uur(min_h), "extreme_market": round(min_p, 2),
            "extreme_all_in": round(all_in(min_p), 4), "magnitude": mag, "drivers": drivers,
        })

    if any(6 <= h <= 8 for h, p in pairs):
        om_h, om_p = max([(h, p) for h, p in pairs if 6 <= h <= 8], key=lambda x: x[1])
        if om_p >= 0.92 * day_med and not (piek_block and piek_block[0] <= om_h <= piek_block[1]):
            windows.append({
                "kind": "ochtendpiek", "window": "06-09u",
                "extreme_hour": fmt_uur(om_h), "extreme_market": round(om_p, 2),
                "extreme_all_in": round(all_in(om_p), 4), "magnitude": "verhoogd",
                "drivers": ["ochtendvraag", "weinig zon"],
            })

    if piek_block:
        a, b = piek_block
        in_avond = any(17 <= h <= 22 for h in range(a, b + 1))
        ratio = max_p / day_med if day_med else 0
        drivers = []
        if in_avond:
            drivers += ["wegvallende zon", "avondvraag"]
            if ratio >= ANOM_PIEK_FACTOR or max_p >= ANOM_PIEK_ABS:
                drivers.append("krappe marge / lage wind")
        else:
            drivers.append("hoge vraag")
        windows.append({
            "kind": "piek", "window": fmt_window(a, b),
            "extreme_hour": fmt_uur(max_h), "extreme_market": round(max_p, 2),
            "extreme_all_in": round(all_in(max_p), 4),
            "magnitude": "extreem" if (ratio >= ANOM_PIEK_FACTOR or max_p >= ANOM_PIEK_ABS) else "verhoogd",
            "drivers": drivers, "ratio_vs_midden": round(ratio, 2),
        })

    anomalie = {"flag": False, "reden": "", "bronnen": []}
    ratio = max_p / day_med if day_med else 0
    if ratio >= ANOM_PIEK_FACTOR or max_p >= ANOM_PIEK_ABS:
        anomalie = {
            "flag": True,
            "reden": (f"avondpiek {max_p:.0f} EUR/MWh is {ratio:.1f}x het dagmidden "
                      f"({day_med:.0f}); structureel patroon verklaart de hoogte niet "
                      f"volledig — check externe oorzaak (lage wind, onbeschikbare centrale)"),
            "bronnen": [BRONNEN["outages"], BRONNEN["weer"]],
        }

    if neg_hours:
        neg_label = "all-in negatief (gratis)" if allin_neg else "markt negatief, all-in nog positief"
    else:
        neg_label = "geen negatieve uren"

    return {
        "date": date, "weekdag": WEEKDAGEN_NL[dt.weekday()], "daytype": dtype,
        "n_uren": len(vals), "ontbrekende_uren": [fmt_uur(h) for h in missing],
        "dag": {
            "gem_markt": round(day_avg, 2), "midden_markt": round(day_med, 2),
            "gem_all_in": round(all_in(day_avg), 4),
            "goedkoopste": {"uur": fmt_uur(min_h), "markt": round(min_p, 2), "all_in": round(all_in(min_p), 4)},
            "duurste": {"uur": fmt_uur(max_h), "markt": round(max_p, 2), "all_in": round(all_in(max_p), 4)},
            "spreiding": round(max_p - min_p, 2),
        },
        "negatief": {"aantal": len(neg_hours), "uren": [fmt_uur(h) for h in neg_hours], "label": neg_label},
        "vensters": windows, "anomalie": anomalie,
        "bronnen": [BRONNEN["prijs"], BRONNEN["weer"], BRONNEN["gas"]],
        "weer_modus": "model-factoren" if factors_per_hour else "afgeleid-uit-prijs",
    }


# ---------------------------------------------------------------------------
# Publieke duiding (B2-taal, cent/kWh, geen vaktermen) — dit leest de bezoeker
# ---------------------------------------------------------------------------
# Regels: maximaal B2. Geen "mediaan", "marktprijs", "EUR/MWh", "factor",
# "anomalie". Bedragen in hele centen per kWh. Concreet, kort, to the point.

def ct_kwh(market_mwh: float) -> float:
    """Kale marktprijs (EUR/MWh) -> all-in cent per kWh (wat de bezoeker betaalt)."""
    return all_in(market_mwh) * 100.0


def _niveau_klasse(typ_ct: float) -> str:
    if typ_ct < 18:
        return "goedkoop"
    if typ_ct < 26:
        return "normaal"
    return "duur"


def lees_factoren(forecast_path: str, date: str) -> dict | None:
    """
    Leest de gemeten weersfactoren voor één dag uit forecast.json.

    Het forecast-model schrijft per uur een `factors`-lijst met o.a.
    zon/wind/gas/temperatuur; die zijn per dag constant. We pakken het
    mediane puntenaantal en een representatieve reden, en halen de waarde
    (m/s, %, °C) uit de tekst tussen haakjes, bv. "zwakke wind (5.1 m/s)".

    Geeft None als forecast.json ontbreekt of de datum er niet in staat —
    dan valt de duiding terug op afleiding uit de prijsvorm.
    """
    try:
        fc = json.loads(Path(forecast_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rows = [f for f in fc.get("forecasts", []) if str(f.get("time", "")).startswith(date)]
    if not rows:
        return None
    out = {}
    for naam in ("zon", "wind", "gas", "temperatuur"):
        punten, reason = [], ""
        for r in rows:
            for f in r.get("factors", []):
                if f.get("name") == naam:
                    punten.append(f.get("points", 0))
                    reason = f.get("reason", "") or reason
        if not punten:
            continue
        m = re.search(r"\(([-\d.]+)", reason)
        waarde = None
        if m:
            try:
                waarde = float(m.group(1))
            except ValueError:
                waarde = None
        out[naam] = {"points": round(median(punten)), "reason": reason, "waarde": waarde}
    return out or None


def _publieke_redenen(heeft_dal: bool, avond_piek: bool, anom: bool,
                      weer: dict | None) -> list:
    """Begrijpelijke 'waarom'-punten. Gebruikt gemeten weer als dat er is,
    anders een afleiding uit de prijsvorm."""
    redenen = []

    # --- Zon ---
    if weer and "zon" in weer:
        zp = weer["zon"]["points"]
        if zp < 0:
            redenen.append({"label": "Zon", "type": "laag", "tekst": "Veel zon overdag, dat drukt de prijs"})
        elif zp > 0:
            redenen.append({"label": "Zon", "type": "hoog", "tekst": "Weinig zon overdag, dat houdt de prijs hoog"})
        else:
            redenen.append({"label": "Zon", "type": "neutraal", "tekst": "Een normale hoeveelheid zon"})
    elif heeft_dal:
        redenen.append({"label": "Zon", "type": "laag", "tekst": "Overdag veel zon, dat maakt stroom goedkoop"})
    else:
        redenen.append({"label": "Zon", "type": "neutraal", "tekst": "Weinig zon overdag, dus geen goedkoop middagdal"})

    # --- Wind (gemeten uit het model, anders voorzichtige afleiding) ---
    if weer and "wind" in weer:
        wp = weer["wind"]["points"]
        ms = weer["wind"]["waarde"]
        mss = f", rond {round(ms)} m/s" if ms is not None else ""
        if wp >= 3:
            redenen.append({"label": "Wind", "type": "hoog", "tekst": f"Bijna geen wind{mss}, dat houdt de prijs hoog"})
        elif wp > 0:
            redenen.append({"label": "Wind", "type": "hoog", "tekst": f"Weinig wind{mss}, dat houdt de prijs hoog"})
        elif wp == 0:
            redenen.append({"label": "Wind", "type": "neutraal", "tekst": f"Een normale wind{mss}"})
        elif wp == -2:
            redenen.append({"label": "Wind", "type": "laag", "tekst": f"Veel wind{mss}, dat drukt de prijs"})
        else:
            redenen.append({"label": "Wind", "type": "laag", "tekst": f"Harde wind{mss}, dat drukt de prijs flink"})
    elif anom:
        redenen.append({"label": "Wind", "type": "hoog", "tekst": "Waarschijnlijk weinig wind, dat houdt de prijs hoog"})

    # --- Vraag ---
    if avond_piek:
        redenen.append({"label": "Vraag", "type": "hoog", "tekst": "'s Avonds gebruikt het hele land veel stroom"})

    # --- Gas (alleen als het sterk meespeelt) ---
    if weer and "gas" in weer and abs(weer["gas"]["points"]) >= 2:
        gp = weer["gas"]["points"]
        if gp > 0:
            redenen.append({"label": "Gas", "type": "hoog", "tekst": "Gas is duur, dat tilt de hele prijs op"})
        else:
            redenen.append({"label": "Gas", "type": "laag", "tekst": "Gas is goedkoop, dat drukt de prijs"})

    return redenen


def bouw_publiek(date: str, prices: list, weer: dict | None = None) -> dict:
    """Bouwt de publieke duiding (B2) uit het feiten-skelet.

    weer: optioneel de gemeten weersfactoren uit lees_factoren(); als die er
    zijn, gebruikt de duiding de gemeten wind/zon in plaats van een afleiding.
    """
    s = bouw_skelet(date, prices, weer)
    dt = datetime.fromisoformat(date)

    pairs = [(h, p) for h, p in enumerate(prices) if p is not None]
    cents = [ct_kwh(p) for _, p in pairs]
    typ = sorted(cents)[len(cents) // 2]
    typ_ct = round(typ)
    duur = s["dag"]["duurste"]
    goed = s["dag"]["goedkoopste"]
    duur_ct = round(ct_kwh(duur["markt"]))
    goed_ct = round(ct_kwh(goed["markt"]))
    duur_uur = duur["uur"]
    goed_uur = goed["uur"]

    avond_piek = any(w["kind"] == "piek" and 17 <= int(w["extreme_hour"][:2]) <= 23
                     for w in s["vensters"])
    heeft_dal = goed_ct <= 16
    anom = s["anomalie"]["flag"]

    # Kop (de conclusie, eerst).
    if anom and avond_piek:
        kop = "Morgen overdag normaal, maar begin van de avond extreem duur"
    elif heeft_dal and avond_piek:
        kop = "Morgen overdag heel goedkoop, in de avond juist duur"
    elif typ < 18:
        kop = "Morgen is stroom de hele dag goedkoop"
    elif typ < 26:
        kop = "Morgen liggen de stroomprijzen op een normaal niveau"
    else:
        kop = "Morgen is stroom de hele dag aan de prijzige kant"

    # Uitleg: 2 korte zinnen met concrete centen, geen ratio-jargon.
    zinnen = []
    if heeft_dal:
        zinnen.append(f"Overdag is stroom goedkoop, rond {goed_uur} zelfs maar {goed_ct} cent per kWh.")
    elif typ >= 26 and not anom:
        zinnen.append(f"Overdag is de prijs al aan de hoge kant, rond {typ_ct} cent per kWh.")
    else:
        zinnen.append(f"Overdag betaal je een normale prijs, rond {typ_ct} cent per kWh.")
    if avond_piek:
        slot = " Dat is uitzonderlijk hoog." if anom else ""
        zinnen.append(f"Maar in de avond loopt het hard op: {duur_ct} cent per kWh om {duur_uur}.{slot}")
    else:
        zinnen.append(f"Het duurste moment is om {duur_uur}, {duur_ct} cent per kWh.")
    uitleg = " ".join(zinnen)

    waarschuwing = ""
    if anom:
        wind = weer.get("wind") if weer else None
        if wind and wind["points"] > 0:
            ms = wind["waarde"]
            mss = f" (rond {round(ms)} m/s)" if ms is not None else ""
            waarschuwing = (f"De avondprijs is ongewoon hoog. Er staat weinig wind{mss} en de zon "
                            f"levert 's avonds niets meer, terwijl het hele land dan stroom gebruikt.")
        elif wind:
            waarschuwing = ("De avondprijs is ongewoon hoog. De wind is niet bijzonder laag, dus er "
                            "speelt waarschijnlijk iets anders mee, zoals een centrale die stilligt "
                            "of veel vraag in het buitenland.")
        else:
            waarschuwing = ("De avondprijs is ongewoon hoog. Dat komt waarschijnlijk doordat er "
                            "weinig wind staat en de zon 's avonds niets meer levert, terwijl het "
                            "hele land dan stroom gebruikt.")

    if avond_piek:
        tip = (f"Zet wasmachine, droger of vaatwasser het liefst rond {goed_uur} aan "
               f"en niet tussen 18:00 en 22:00.")
    else:
        tip = f"Het goedkoopste moment is rond {goed_uur}."

    return {
        "datum": date,
        "weekdag": WEEKDAGEN_NL[dt.weekday()],
        "kop": kop,
        "uitleg": uitleg,
        "waarom": _publieke_redenen(heeft_dal, avond_piek, anom, weer),
        "weer_modus": s["weer_modus"],
        "waarschuwing": waarschuwing,
        "tip": tip,
        "niveau": "duur" if anom else _niveau_klasse(typ),
        "bron": "Gebaseerd op de officiële stroomprijzen voor morgen en het weer. "
                "Automatisch gemaakt, niet door iemand nagekeken.",
        "gegenereerd": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _prod_uit_ha(ha_path: str, out_path: str, forecast_path: str) -> int:
    """
    Productiemodus. Leest public/data/ha.json (morgen-prijzen) en de gemeten
    weersfactoren uit public/data/forecast.json, en schrijft public/data/duiding.json
    met de publieke B2-duiding. Draait in de GitHub Actions-pijplijn, ná
    generate_ha.py én run_forecast.py (beide moeten vers zijn).

    ha.json levert `market` in EUR/kWh; de engine rekent in EUR/MWh (x1000).
    """
    ha = json.loads(Path(ha_path).read_text(encoding="utf-8"))
    morgen = ha.get("tomorrow") or {}
    if not morgen.get("available") or not morgen.get("hours"):
        print("[duiding] geen complete morgen-prijzen in ha.json — niets te doen.")
        return 0
    date = morgen["date"]
    prices = [None] * 24
    for h in morgen["hours"]:
        hour = datetime.fromisoformat(h["start"]).hour
        prices[hour] = h["market"] * 1000.0          # EUR/kWh -> EUR/MWh

    weer = lees_factoren(forecast_path, date)
    bron = "gemeten weer" if weer else "afgeleid uit de prijzen"

    publiek = bouw_publiek(date, prices, weer)
    Path(out_path).write_text(
        json.dumps(publiek, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[duiding] {out_path} geschreven voor {date} ({bron}): {publiek['kop']}")
    return 0


def main(argv: list) -> int:
    if "--prod" in argv:
        rest = [a for a in argv[2:] if not a.startswith("--")]
        ha = rest[0] if len(rest) > 0 else "public/data/ha.json"
        out = rest[1] if len(rest) > 1 else "public/data/duiding.json"
        forecast = rest[2] if len(rest) > 2 else "public/data/forecast.json"
        return _prod_uit_ha(ha, out, forecast)

    if len(argv) < 2:
        print("gebruik: genereer_duiding.py <prijzen.json> [--datum YYYY-MM-DD]\n"
              "         genereer_duiding.py --prod [ha.json] [duiding.json]", file=sys.stderr)
        return 2
    data = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    days = data["days"]
    only = argv[argv.index("--datum") + 1] if "--datum" in argv else None

    out_skelet, out_publiek = {}, {}
    for date in sorted(days):
        if only and date != only:
            continue
        out_skelet[date] = bouw_skelet(date, days[date])
        out_publiek[date] = bouw_publiek(date, days[date])

    Path("duiding_skelet.json").write_text(
        json.dumps(out_skelet, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("duiding_publiek.json").write_text(
        json.dumps(out_publiek, ensure_ascii=False, indent=2), encoding="utf-8")

    for date, p in out_publiek.items():
        print(f"{date} {p['weekdag']:9s} | {p['kop']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
