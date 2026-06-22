#!/usr/bin/env python3
"""
genereer_duiding.py — Route C: deterministisch feiten-skelet + verwoordings-prompt
voor de dagelijkse prijsduiding op stroomvoorspeller.nl.

Filosofie (zelfde als forecast.py / event_plausibility.py):
- Deterministisch en herleidbaar. Geen ML, geen verzonnen cijfers.
- De FEITEN komen uit data (werkelijke prijzen + de factor-attributie die het
  forecast-model al per uur schrijft). De TAAL komt los daarvan tot stand.
- Wat niet in het skelet staat, mag niet in de tekst belanden.

Pijplijn:
    prijzen (+ optioneel forecast-factoren)
        -> bouw_skelet()          # de feiten
        -> template_concept()      # route A: kant-en-klare zinnen, geen LLM
        -> verwoordings_prompt()   # route C: prompt die ALLEEN het skelet verwoordt

Twee databronnen voor de drivers, in volgorde van voorkeur:
  1. forecast.json `factors` (zon/wind/temp/gas/uurpatroon) — exact, uit het model.
     In productie draait dit script ná run_forecast.py en leest het die factoren.
  2. Geen factoren beschikbaar -> "prijs-modus": de driver wordt afgeleid uit de
     vorm van de prijscurve zelf (middagdal = zon, avondpiek = wegvallende zon +
     krappe marge) plus het deterministische uurpatroon. Kwalitatief, duidelijk
     gelabeld als afleiding, niet als meting.

Belasting/all-in volgt de site-conventie (ha.json):
    all_in = (markt[EUR/kWh] + energiebelasting + gem. opslag) * btw
en de regel: "gratis" alleen als all_in < 0; anders "markt negatief".
"""

from __future__ import annotations

import json
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
ANOM_PIEK_FACTOR = 3.0   # piek > 3x de dagmediaan -> mogelijk extra oorzaak
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


def uurpatroon_label(hour: int, zomer: bool) -> str:
    """Deterministisch uurpatroon (vereenvoudigd uit forecast.factor_uurpatroon)."""
    if 0 <= hour <= 5:
        return "nacht"
    if 6 <= hour <= 8:
        return "ochtendspits"
    if 9 <= hour <= 14:
        return "midden van de dag"
    if 15 <= hour <= 16:
        return "namiddag"
    if 17 <= hour <= 18:
        return "vroege avond"
    if 19 <= hour <= 20:
        return "avondspits"
    return "late avond"


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _runs(hours: list[int]) -> list[tuple[int, int]]:
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
    # b is het laatste uur dat begint; het blok loopt tot b+1.
    return f"{a:02d}–{(b + 1) % 24:02d}u" if a != b else f"{a:02d}–{(a + 1) % 24:02d}u"


# ---------------------------------------------------------------------------
# Kern: bouw het feiten-skelet voor één dag
# ---------------------------------------------------------------------------
def bouw_skelet(date: str, prices: list, factors_per_hour: list | None = None) -> dict:
    """
    date:   'YYYY-MM-DD'
    prices: 24 (of 23) waarden EUR/MWh; None voor een ontbrekend uur.
    factors_per_hour: optioneel, de `factors`-array per uur uit forecast.json.
    """
    dt = datetime.fromisoformat(date)
    zomer = _is_zomer(dt.month)
    dtype = daytype(dt, date)

    # Geldige (uur, prijs)-paren.
    pairs = [(h, p) for h, p in enumerate(prices) if p is not None]
    vals = [p for _, p in pairs]
    missing = [h for h, p in enumerate(prices) if p is None]

    day_avg = sum(vals) / len(vals)
    day_med = median(vals)
    min_h, min_p = min(pairs, key=lambda x: x[1])
    max_h, max_p = max(pairs, key=lambda x: x[1])

    # Negatieve uren (kale markt < 0).
    neg_hours = [h for h, p in pairs if p < 0]
    allin_neg = [h for h, p in pairs if all_in(p) < 0]

    # --- Daluren (zonnedal): aaneengesloten goedkoopste blok midden op de dag ---
    dal_drempel = min(0.25 * day_med, 5.0)  # "bijna gratis"-zone
    dal_hours = [h for h, p in pairs if p <= dal_drempel]
    dal_blocks = _runs(dal_hours)
    # Kies het blok dat het dagminimum bevat.
    dal_block = next((b for b in dal_blocks if b[0] <= min_h <= b[1]), None)

    # --- Piekblok: aaneengesloten duurste blok rond het dagmaximum ---
    piek_drempel = 0.80 * max_p
    piek_hours = [h for h, p in pairs if p >= piek_drempel]
    piek_blocks = _runs(piek_hours)
    piek_block = next((b for b in piek_blocks if b[0] <= max_h <= b[1]), None)

    windows = []

    # Dal-venster
    if dal_block:
        a, b = dal_block
        drivers = ["zon"] if any(9 <= h <= 17 for h in range(a, b + 1)) else ["lage vraag"]
        if min_p < 0:
            mag = "diep negatief"
        elif min_p < 5:
            mag = "rond nul"
        else:
            mag = "laag"
        windows.append({
            "kind": "dal",
            "window": fmt_window(a, b),
            "extreme_hour": fmt_uur(min_h),
            "extreme_market": round(min_p, 2),
            "extreme_all_in": round(all_in(min_p), 4),
            "magnitude": mag,
            "drivers": drivers,
            "toelichting_feit": f"prijs zakt naar {min_p:.0f} EUR/MWh tussen {fmt_window(a, b)}",
        })

    # Ochtendpiek apart benoemen als die los van de avondpiek bestaat.
    if max(6, 0) and any(6 <= h <= 8 for h, p in pairs):
        om_vals = [(h, p) for h, p in pairs if 6 <= h <= 8]
        om_h, om_p = max(om_vals, key=lambda x: x[1])
        if om_p >= 0.92 * day_med and not (piek_block and piek_block[0] <= om_h <= piek_block[1]):
            windows.append({
                "kind": "ochtendpiek",
                "window": "06–09u",
                "extreme_hour": fmt_uur(om_h),
                "extreme_market": round(om_p, 2),
                "extreme_all_in": round(all_in(om_p), 4),
                "magnitude": "verhoogd",
                "drivers": ["ochtendvraag", "weinig zon"],
                "toelichting_feit": f"ochtendvraag tilt de prijs naar {om_p:.0f} EUR/MWh om {fmt_uur(om_h)}",
            })

    # Piek-venster (meestal avond)
    if piek_block:
        a, b = piek_block
        in_avond = any(17 <= h <= 22 for h in range(a, b + 1))
        ratio = max_p / day_med if day_med else 0
        drivers = []
        if in_avond:
            drivers.append("wegvallende zon")
            drivers.append("avondvraag")
            if ratio >= ANOM_PIEK_FACTOR or max_p >= ANOM_PIEK_ABS:
                drivers.append("krappe marge / lage wind")
        else:
            drivers.append("hoge vraag")
        windows.append({
            "kind": "piek",
            "window": fmt_window(a, b),
            "extreme_hour": fmt_uur(max_h),
            "extreme_market": round(max_p, 2),
            "extreme_all_in": round(all_in(max_p), 4),
            "magnitude": "extreem" if (ratio >= ANOM_PIEK_FACTOR or max_p >= ANOM_PIEK_ABS) else "verhoogd",
            "drivers": drivers,
            "ratio_vs_mediaan": round(ratio, 2),
            "toelichting_feit": f"piek {max_p:.0f} EUR/MWh om {fmt_uur(max_h)} ({ratio:.1f}x de dagmediaan)",
        })

    # --- Anomalie-flag (event_plausibility-gedachte) ---
    anomalie = {"flag": False, "reden": "", "bronnen": []}
    ratio = max_p / day_med if day_med else 0
    if ratio >= ANOM_PIEK_FACTOR or max_p >= ANOM_PIEK_ABS:
        anomalie = {
            "flag": True,
            "reden": (f"avondpiek {max_p:.0f} EUR/MWh is {ratio:.1f}x de dagmediaan "
                      f"({day_med:.0f}) — structureel patroon verklaart de hoogte niet volledig; "
                      f"check externe oorzaak (lage wind, onbeschikbare centrale, import)"),
            "bronnen": [BRONNEN["outages"], BRONNEN["weer"]],
        }

    # Negatief-label volgens de site-regel.
    if neg_hours:
        if allin_neg:
            neg_label = "all-in negatief (gratis)"
        else:
            neg_label = "markt negatief, all-in nog positief"
    else:
        neg_label = "geen negatieve uren"

    skelet = {
        "date": date,
        "weekdag": WEEKDAGEN_NL[dt.weekday()],
        "daytype": dtype,
        "n_uren": len(vals),
        "ontbrekende_uren": [fmt_uur(h) for h in missing],
        "dag": {
            "gem_markt": round(day_avg, 2),
            "mediaan_markt": round(day_med, 2),
            "gem_all_in": round(all_in(day_avg), 4),
            "goedkoopste": {"uur": fmt_uur(min_h), "markt": round(min_p, 2),
                            "all_in": round(all_in(min_p), 4)},
            "duurste": {"uur": fmt_uur(max_h), "markt": round(max_p, 2),
                        "all_in": round(all_in(max_p), 4)},
            "spreiding": round(max_p - min_p, 2),
        },
        "negatief": {
            "aantal": len(neg_hours),
            "uren": [fmt_uur(h) for h in neg_hours],
            "label": neg_label,
        },
        "vensters": windows,
        "anomalie": anomalie,
        "bronnen": [BRONNEN["prijs"], BRONNEN["weer"], BRONNEN["gas"]],
        "weer_modus": "model-factoren" if factors_per_hour else "afgeleid-uit-prijs",
    }
    return skelet


# ---------------------------------------------------------------------------
# Route A: deterministisch concept (geen LLM) — altijd beschikbaar als fallback
# ---------------------------------------------------------------------------
def template_concept(s: dict) -> str:
    d = s["dag"]
    zinnen = []
    # Openingsoordeel.
    med = d["mediaan_markt"]
    if med < 40:
        niveau = "laag"
    elif med < 90:
        niveau = "normaal"
    else:
        niveau = "aan de hoge kant"
    zinnen.append(
        f"De prijzen voor {s['weekdag']} {s['date']} liggen overdag {niveau} "
        f"(mediaan {med:.0f} EUR/MWh)."
    )
    for w in s["vensters"]:
        if w["kind"] == "dal":
            if w["magnitude"] == "diep negatief":
                zinnen.append(
                    f"Midden op de dag duwt de zon de markt {w['window']} onder nul "
                    f"(dieptepunt {w['extreme_market']:.0f} EUR/MWh om {w['extreme_hour']}); "
                    f"all-in blijft met {w['extreme_all_in']:.2f} EUR/kWh wel positief."
                )
            else:
                zinnen.append(
                    f"Rond het middaguur drukt de zon de prijs naar {w['extreme_market']:.0f} "
                    f"EUR/MWh ({w['window']})."
                )
        elif w["kind"] == "ochtendpiek":
            zinnen.append(
                f"'s Ochtends tilt de vraag de prijs kort naar {w['extreme_market']:.0f} "
                f"EUR/MWh om {w['extreme_hour']}."
            )
        elif w["kind"] == "piek":
            extra = " en vermoedelijk lage wind" if "krappe marge / lage wind" in w["drivers"] else ""
            zinnen.append(
                f"In de vroege avond valt de zon weg terwijl de vraag hoog blijft{extra}: "
                f"de prijs piekt naar {w['extreme_market']:.0f} EUR/MWh om {w['extreme_hour']} "
                f"({w.get('ratio_vs_mediaan', '?')}x de dagmediaan)."
            )
    if s["anomalie"]["flag"]:
        zinnen.append(f"Let op: {s['anomalie']['reden']}.")
    return " ".join(zinnen)


# ---------------------------------------------------------------------------
# Route C: prompt die ALLEEN het skelet verwoordt (voor LLM-stap)
# ---------------------------------------------------------------------------
VOICE_REGELS = (
    "Schrijf 2-3 zinnen Nederlandse prijsduiding voor een breed publiek. "
    "Conclusie eerst, dan oorzaak. Actief, concreet, exacte getallen uit het skelet. "
    "Geen jargon, geen marketingtaal, geen emoji, geen kapitalen voor nadruk. "
    "Gebruit UITSLUITEND feiten uit het skelet; voeg niets toe en verzin geen oorzaken. "
    "Noem bij een negatieve markt nooit 'gratis' tenzij all_in < 0; label anders 'markt negatief'. "
    "Als anomalie.flag waar is, benoem kort dat de hoogte een externe check vraagt, "
    "zonder een specifieke oorzaak te claimen."
)


def verwoordings_prompt(s: dict) -> str:
    return (
        "Je verwoordt een feiten-skelet in de huisstijl van stroomvoorspeller.nl.\n\n"
        f"REGELS:\n{VOICE_REGELS}\n\n"
        f"SKELET (JSON, dit zijn de enige toegestane feiten):\n"
        f"{json.dumps(s, ensure_ascii=False, indent=2)}\n\n"
        "Lever alleen de duiding-tekst."
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("gebruik: genereer_duiding.py <prijzen.json> [--datum YYYY-MM-DD]", file=sys.stderr)
        return 2
    data = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    days = data["days"]
    only = None
    if "--datum" in argv:
        only = argv[argv.index("--datum") + 1]

    out_skelet = {}
    out_concept = {}
    for date in sorted(days):
        if only and date != only:
            continue
        s = bouw_skelet(date, days[date])
        out_skelet[date] = s
        out_concept[date] = {
            "template_concept": template_concept(s),
            "verwoordings_prompt": verwoordings_prompt(s),
        }

    Path("duiding_skelet.json").write_text(
        json.dumps(out_skelet, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("duiding_concept.json").write_text(
        json.dumps(out_concept, ensure_ascii=False, indent=2), encoding="utf-8")

    # Korte console-samenvatting.
    for date, s in out_skelet.items():
        flag = "  [ANOMALIE]" if s["anomalie"]["flag"] else ""
        print(f"{date} {s['weekdag']:9s} mediaan {s['dag']['mediaan_markt']:6.0f}  "
              f"piek {s['dag']['duurste']['markt']:6.0f}@{s['dag']['duurste']['uur']}  "
              f"dal {s['dag']['goedkoopste']['markt']:7.0f}@{s['dag']['goedkoopste']['uur']}"
              f"{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
