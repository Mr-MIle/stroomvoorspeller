"""
forecast.py — 6-puntensysteem voor Nederlandse day-ahead prijzen.

Implementeert het model uit 01-documenten/methodologie-voorspellingsmodel.md.
Wordt gebruikt door zowel de live forecast als door backtest.py.

Alle bedragen zijn in EUR/MWh tenzij anders vermeld.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


# Officiële NL vrije dagen 2025-2027.
# Let op: Bevrijdingsdag (5 mei) is alleen vrij in lustrum-jaren (2020, 2025, 2030).
# 2026 en 2027 zijn GEEN lustrum, dus niet opgenomen.
# 2027: Pasen = 28 maart (berekend via Gregoriaanse methode).
NL_FEESTDAGEN = {
    # 2025
    "2025-01-01", "2025-04-18", "2025-04-20", "2025-04-21", "2025-04-27",
    "2025-05-05",  # Bevrijdingsdag 2025 — 80e lustrum ✓
    "2025-05-29", "2025-06-08", "2025-06-09",
    "2025-12-25", "2025-12-26",
    # 2026 — Pasen = 5 april
    "2026-01-01", "2026-04-03", "2026-04-05", "2026-04-06", "2026-04-27",
    "2026-05-14", "2026-05-24", "2026-05-25",  # Hemelvaart, Pinksteren
    "2026-12-25", "2026-12-26",
    # 2027 — Pasen = 28 maart
    "2027-01-01", "2027-03-26", "2027-03-28", "2027-03-29", "2027-04-27",
    "2027-05-06", "2027-05-16", "2027-05-17",  # Hemelvaart, Pinksteren
    "2027-12-25", "2027-12-26",
}

# Dagen waarop DE+BE (en vaak FR) vrij zijn maar NL NIET.
# v1.7: gebruikt om baseline-besmetting te voorkomen — als een historische
# werkdag toevallig een EU-feestdag was (bijv. 1 mei), zijn de prijzen van
# die dag structureel afwijkend (buurland-overschot drukt de prijs) en
# mogen ze niet meewegen in de baseline van een gewone werkdag.
CROSSBORDER_FEESTDAGEN = {
    "2026-05-01",  # Dag van de Arbeid (DE+BE+FR vrij, NL open)
    "2027-05-01",  # Dag van de Arbeid
}

# Gewicht per punt (zie methodologie sectie 3.2).
# v1.0-1.2: 0.04. v1.3 (2026-04-29): gehalveerd naar 0.02 nadat backtest v1
# een systematische over-voorspelling toonde (bias +8 EUR/MWh op 1d, oplopend
# naar +19 op 7d) en MAE iets boven de naïeve baseline lag.
# Zie 01-documenten/backtest-resultaat-v1.md.
POINT_WEIGHT = 0.02

# Welke factoren tellen mee in de som. Default: alle 7. Via deze set is het
# mogelijk individuele factoren uit te schakelen voor experimenten zonder de
# code zelf te wijzigen.
#
# Backtest v4 (v1.5) testte een simpel model met {"zon", "wind"} alleen — dat
# liet richting-hit zakken van 51% naar 42% op 1d (onder random). Conclusie:
# de gecombineerde factoren capteren wél subtiele richtingsignalen die individueel
# weinig lijken bij te dragen, en het volledige model blijft de productiekeuze.
#
# v1.8: "vorige_dag" toegevoegd — zie factor_vorige_dag() hieronder.
ENABLED_FACTORS = {"zon", "wind", "temperatuur", "gas", "dagtype", "uurpatroon", "vorige_dag"}

# v1.6: zondag-boost voor weersfactoren.
# Backtest v3 toonde een hardnekkige bias van +27 EUR/MWh op zondag-uren die niet
# door de v1.4 weekend-baseline-fix werd opgelost. Op zondag is de basale stroomvraag
# lager (geen industrie, weinig commercieel) dus dezelfde MWh aan zon- en
# windproductie drukt de prijs sterker. Een zonnige+winderige zondag laat prijzen
# diep zakken; een bewolkte+windstille zondag piekt de prijs juist. v1.6 versterkt
# alleen op zondag de zon- en wind-factoren met deze multiplier; andere dagen
# ongewijzigd. Andere factoren (temperatuur, gas, dagtype, uurpatroon) blijven 1x.
ZONDAG_BOOST = 2


@dataclass
class FactorScore:
    """Score van één factor met uitleg voor logging/UI."""
    name: str
    points: int
    reason: str


@dataclass
class Forecast:
    """Resultaat van één uurvoorspelling."""
    target_iso: str
    baseline: float            # EUR/MWh
    factors: list[FactorScore]
    total_points: int
    predicted: float           # EUR/MWh
    uncertainty_pct: float     # 0..1
    days_ahead: int

    @property
    def lower(self) -> float:
        return self.predicted * (1 - self.uncertainty_pct)

    @property
    def upper(self) -> float:
        return self.predicted * (1 + self.uncertainty_pct)


# ---- Hulpfuncties ----

def is_feestdag(dt: datetime) -> bool:
    return dt.strftime("%Y-%m-%d") in NL_FEESTDAGEN

def is_crossborder_feestdag(dt: datetime) -> bool:
    return dt.strftime("%Y-%m-%d") in CROSSBORDER_FEESTDAGEN

def dagtype(dt: datetime) -> str:
    """werkdag | weekend | feestdag — voor baseline-grouping."""
    if is_feestdag(dt):
        return "feestdag"
    wd = dt.weekday()
    return "weekend" if wd >= 5 else "werkdag"


def is_zomer(dt: datetime) -> bool:
    return 4 <= dt.month <= 9


# ---- Baseline (sectie 3.1) ----

def compute_baseline(target_dt: datetime, history: list[dict]) -> Optional[float]:
    """
    Gemiddelde EPEX-prijs voor hetzelfde uur en hetzelfde dagtype.

    Window-keuze:
    - werkdag/feestdag: laatste 7 dagen (typisch 5 werkdag-datapunten of 1-2 feestdag).
    - weekend: laatste 14 dagen (v1.4) — een 7d-window levert maar 1 datapunt per
      uur op (vorige zaterdag of vorige zondag), wat zeer ruisig is en in backtest
      v2 een bias-piek van +27 EUR/MWh op zondag opleverde. 14 dagen geeft 2
      datapunten per hour-of-week, een acceptabele middenweg.

    v1.7: cross-border feestdagen (zoals 1 mei) worden uitgesloten van de
    werkdag-baseline. Die dagen hebben structureel andere marktomstandigheden
    (buurland-overschot drukt de prijs negatief) en zijn niet representatief
    voor een gewone werkdag. Als na filtering te weinig datapunten overblijven
    (<2), wordt het window verlengd naar 14 dagen en opnieuw geprobeerd.

    history: lijst van {time: ISO-string, price: float in EUR/MWh}
    Return: baseline in EUR/MWh, of None als er geen data is.
    """
    target_hour = target_dt.hour
    target_type = dagtype(target_dt)
    window_days = 14 if target_type == "weekend" else 7
    cutoff_start = target_dt - timedelta(days=window_days)
    cutoff_end = target_dt

    def _collect(from_dt):
        matches = []
        for entry in history:
            t = datetime.fromisoformat(entry["time"])
            if t < from_dt or t >= cutoff_end:
                continue
            if t.hour != target_hour:
                continue
            if dagtype(t) != target_type:
                continue
            # v1.7: werkdag-baseline mag geen cross-border feestdagen bevatten.
            # Voorbeeld: 1 mei (EU-feestdag) is dagtype "werkdag" maar heeft
            # structureel lagere prijzen — niet representatief voor gewone werkdag.
            if target_type == "werkdag" and is_crossborder_feestdag(t):
                continue
            matches.append(entry["price"])
        return matches

    matches = _collect(cutoff_start)

    # Fallback: te weinig datapunten na filtering (bijv. feestdag gevolgd door
    # een tweede feestdag in het 7d-window). Verleng het window naar 14 dagen.
    if len(matches) < 2 and window_days <= 7:
        matches = _collect(target_dt - timedelta(days=14))

    if not matches:
        return None
    return sum(matches) / len(matches)


# ---- Factor 1: Zonproductie ----

def factor_zon(shortwave_ratio: float) -> FactorScore:
    """
    shortwave_ratio: voorspelde dagelijkse straling / seizoengemiddelde.
    """
    if shortwave_ratio < 0.50:
        pts, reason = +3, f"bewolkt ({shortwave_ratio*100:.0f}% van seizoen)"
    elif shortwave_ratio < 0.80:
        pts, reason = +1, f"iets minder zon ({shortwave_ratio*100:.0f}%)"
    elif shortwave_ratio <= 1.20:
        pts, reason = 0, f"normaal ({shortwave_ratio*100:.0f}%)"
    elif shortwave_ratio <= 1.50:
        pts, reason = -1, f"zonnig ({shortwave_ratio*100:.0f}%)"
    else:
        pts, reason = -3, f"heel zonnig ({shortwave_ratio*100:.0f}%)"
    return FactorScore("zon", pts, reason)


# ---- Factor 2: Windproductie ----

def factor_wind(wind_ms: float) -> FactorScore:
    """wind_ms: gemiddelde windsnelheid op 100m hoogte (m/s)."""
    if wind_ms < 4:
        pts, reason = +3, f"windstil ({wind_ms:.1f} m/s)"
    elif wind_ms < 8:
        pts, reason = +1, f"zwakke wind ({wind_ms:.1f} m/s)"
    elif wind_ms < 12:
        pts, reason = 0, f"normaal ({wind_ms:.1f} m/s)"
    elif wind_ms < 16:
        pts, reason = -2, f"stevige wind ({wind_ms:.1f} m/s)"
    else:
        pts, reason = -3, f"storm ({wind_ms:.1f} m/s)"
    return FactorScore("wind", pts, reason)


# ---- Factor 3: Temperatuur ----
# v1.3: drempels herzien. Voorheen was alleen koud/vorst positief; mild en
# warm gaven 0 of +1 zodat de factor structureel niet-negatief was. Dat droeg
# bij aan de bias in backtest v1. Nu: lekker weer (18-26 °C) geeft -1 (lagere
# ruimtevraag, mensen buiten, zon op piek), warm (>26 °C) is 0 (lichte
# airco-koeling balanceert overige effecten).

def factor_temperatuur(temp_c: float) -> FactorScore:
    if temp_c < 0:
        pts, reason = +2, f"vorst ({temp_c:.1f}°C)"
    elif temp_c < 10:
        pts, reason = +1, f"koud ({temp_c:.1f}°C)"
    elif temp_c < 18:
        pts, reason = 0, f"mild ({temp_c:.1f}°C)"
    elif temp_c <= 26:
        pts, reason = -1, f"lekker ({temp_c:.1f}°C)"
    else:
        pts, reason = 0, f"warm ({temp_c:.1f}°C)"
    return FactorScore("temperatuur", pts, reason)


# ---- Factor 4: Gasprijs (TTF) ----

def factor_gas(ttf_ratio: float) -> FactorScore:
    """ttf_ratio: huidige TTF / 30-dagen gemiddelde TTF."""
    if ttf_ratio < 0.70:
        pts, reason = -2, f"gas goedkoop ({ttf_ratio*100:.0f}% van 30d gem.)"
    elif ttf_ratio < 0.90:
        pts, reason = -1, f"gas iets goedkoper ({ttf_ratio*100:.0f}%)"
    elif ttf_ratio <= 1.10:
        pts, reason = 0, f"normaal ({ttf_ratio*100:.0f}%)"
    elif ttf_ratio <= 1.30:
        pts, reason = +1, f"gas iets duurder ({ttf_ratio*100:.0f}%)"
    else:
        pts, reason = +2, f"gas duur ({ttf_ratio*100:.0f}%)"
    return FactorScore("gas", pts, reason)


# ---- Factor 5: Type dag ----
# v1.3: werkdag-bonus van +1 naar 0. De baseline filtert al op dagtype, dus
# een expliciete +1 voor werkdagen telde dubbel — een belangrijke oorzaak
# van de bias in backtest v1. Weekend en feestdag houden hun negatieve
# gewicht omdat de baseline-window voor zaterdag/zondag/feestdag mager is
# (1-2 datapunten in 7 dagen) en daar de factor nog corrigerende waarde heeft.
#
# v1.7: ook cross-border feestdagen (EU-feestdag, NL open) krijgen -2 punten.
# Op die dagen is de effectieve marktprijs structureel lager door verminderde
# buurland-vraag én exportoverschot dat op het NL net drukt.

def factor_dagtype(dt: datetime) -> FactorScore:
    if is_feestdag(dt):
        return FactorScore("dagtype", -2, "NL feestdag")
    if is_crossborder_feestdag(dt):
        return FactorScore("dagtype", -2, "EU-feestdag (NL open, buurlanden vrij)")
    wd = dt.weekday()
    if wd == 6:
        return FactorScore("dagtype", -2, "zondag")
    if wd == 5:
        return FactorScore("dagtype", -1, "zaterdag")
    return FactorScore("dagtype", 0, "werkdag")


# ---- Factor 6: Uurpatroon ----
# v1.3: ochtendspits winter van +2 naar +1. De NL-markt heeft in 2026 een
# minder scherpe ochtendpiek dan vroeger — warmtepompen draaien al de hele
# nacht door, EV's laden 's nachts, en zonsopgang verlicht de ochtend al
# vroeg in de zomer. De avondspits 17-20 uur blijft wel scherp.

def factor_uurpatroon(dt: datetime) -> FactorScore:
    h = dt.hour
    zomer = is_zomer(dt)
    season = "zomer" if zomer else "winter"
    if 0 <= h <= 5:
        return FactorScore("uurpatroon", -2, f"{season}, nacht ({h}:00)")
    if 6 <= h <= 8:
        return FactorScore("uurpatroon", +1, f"{season}, ochtendspits ({h}:00)")
    if 9 <= h <= 14:
        pts = -1 if zomer else 0
        return FactorScore("uurpatroon", pts, f"{season}, midden van de dag ({h}:00)")
    if 15 <= h <= 16:
        return FactorScore("uurpatroon", 0, f"{season}, namiddag ({h}:00)")
    if 17 <= h <= 20:
        pts = +1 if zomer else +2
        return FactorScore("uurpatroon", pts, f"{season}, avondspits ({h}:00)")
    return FactorScore("uurpatroon", -1, f"{season}, late avond ({h}:00)")


# ---- Factor 7: Vorige dag (v1.8) ----
# Dagelijkse day-ahead prijzen vertonen sterke autocorrelatie: een dag met hoge
# prijzen wordt vaak gevolgd door een dag met relatief hoge prijzen (aanhoudend
# weerregime, gasprijsniveau, marktomstandigheden veranderen niet van dag op dag).
#
# Deze factor benut de bekende D+1-prijzen (gepubliceerd ~13:00) als signaal voor
# D+2, het eerste te voorspellen uur. Voor D+3 en verder ontbreken de vorige-dag-
# prijzen en geeft de factor 0 (neutraal).
#
# Werkwijze per te voorspellen uur H op dag D+2:
#   1. Zoek de werkelijke prijs van D+1 op datzelfde uur H  → prior_price
#   2. Bereken de historische baseline voor dat uur op D+1  → prior_baseline
#      (= gemiddelde van dezelfde dag/uur-combinatie in de voorgaande 7-14 dagen,
#       identiek aan hoe compute_baseline() normaal werkt)
#   3. ratio = prior_price / prior_baseline
#   4. Sla de ratio om in ±1 of ±2 punten
#
# Rationale voor de drempelkeuze (zelfde stijl als factor_gas):
#   ratio < 0.70 → de voorgaande dag was structureel goedkoop      → -2
#   ratio < 0.90 → iets goedkoper                                  → -1
#   ratio ≤ 1.10 → normaal                                         →  0
#   ratio ≤ 1.30 → iets duurder                                    → +1
#   ratio > 1.30 → structureel duur                                → +2
#
# Effect op de voorspelling (POINT_WEIGHT = 0.02):
#   max +2 punten → +4% op baseline  (bv. 30 EUR/MWh → 31.20 EUR/MWh)
#   max -2 punten → -4% op baseline
# Dit is bewust conservatief: de autocorrelatie is sterk maar niet volledig,
# en het model mag de historische baseline niet te ver overrulen.

def factor_vorige_dag(prior_ratio: Optional[float]) -> FactorScore:
    """
    prior_ratio: (prijs voorgaande dag uur H) / (baseline voorgaande dag uur H).
    None als de voorgaande-dag-prijs niet beschikbaar is (D+3 en verder).
    """
    if prior_ratio is None:
        return FactorScore("vorige_dag", 0, "niet beschikbaar (>1 dag vooruit)")
    if prior_ratio < 0.70:
        pts, reason = -2, f"vorige dag goedkoop ({prior_ratio:.2f}× baseline)"
    elif prior_ratio < 0.90:
        pts, reason = -1, f"vorige dag iets goedkoper ({prior_ratio:.2f}×)"
    elif prior_ratio <= 1.10:
        pts, reason = 0, f"vorige dag normaal ({prior_ratio:.2f}×)"
    elif prior_ratio <= 1.30:
        pts, reason = +1, f"vorige dag duurder ({prior_ratio:.2f}×)"
    else:
        pts, reason = +2, f"vorige dag duur ({prior_ratio:.2f}×)"
    return FactorScore("vorige_dag", pts, reason)


# ---- Onzekerheidsband ----

def uncertainty(days_ahead: int, abs_points: int) -> float:
    return 0.10 + 0.02 * days_ahead + 0.01 * abs_points


# ---- Hoofdfunctie: één forecast ----

def forecast_one(
    target_dt: datetime,
    history: list[dict],
    shortwave_ratio: float,
    wind_ms: float,
    temp_c: float,
    ttf_ratio: float,
    days_ahead: int,
    prior_day_price: Optional[float] = None,
) -> Optional[Forecast]:
    """
    Voorspel de prijs voor een specifiek toekomstig uur.

    prior_day_price: bekende day-ahead prijs van de voorgaande dag op hetzelfde
        uur (EUR/MWh). Alleen beschikbaar voor D+2 (eerste voorspeldag), waarbij
        de D+1-prijzen al gepubliceerd zijn. Geef None door voor D+3 en verder.

    Return: Forecast object, of None als baseline niet bepaald kon worden.
    """
    baseline = compute_baseline(target_dt, history)
    if baseline is None:
        return None

    # Factor 7: vorige dag — normaliseer de prior_price op zijn eigen baseline.
    prior_ratio: Optional[float] = None
    if prior_day_price is not None:
        prior_dt = target_dt - timedelta(days=1)
        prior_baseline = compute_baseline(prior_dt, history)
        if prior_baseline and prior_baseline != 0:
            prior_ratio = prior_day_price / prior_baseline

    factors = [
        factor_zon(shortwave_ratio),
        factor_wind(wind_ms),
        factor_temperatuur(temp_c),
        factor_gas(ttf_ratio),
        factor_dagtype(target_dt),
        factor_uurpatroon(target_dt),
        factor_vorige_dag(prior_ratio),
    ]

    # v1.6: zondag-boost. Op zondag tellen zon en wind ZWAARDER (×ZONDAG_BOOST)
    # omdat de basisvraag laag is en weersinvloed de prijs sterker beweegt.
    # We vervangen de FactorScore-objects zodat de boost zichtbaar blijft in
    # de uitleg (×N erbij in `reason`-string).
    if target_dt.weekday() == 6:  # zondag
        boosted = []
        for f in factors:
            if f.name in ("zon", "wind"):
                boosted.append(FactorScore(
                    name=f.name,
                    points=f.points * ZONDAG_BOOST,
                    reason=f"{f.reason} ×{ZONDAG_BOOST} (zondag)",
                ))
            else:
                boosted.append(f)
        factors = boosted

    # Alleen ENABLED_FACTORS tellen mee in totaal-score; andere factoren
    # blijven voor transparantie zichtbaar in `factors`-lijst maar dragen niet bij.
    total = sum(f.points for f in factors if f.name in ENABLED_FACTORS)
    predicted = baseline * (1 + total * POINT_WEIGHT)
    unc = uncertainty(days_ahead, abs(total))

    return Forecast(
        target_iso=target_dt.isoformat(),
        baseline=round(baseline, 2),
        factors=factors,
        total_points=total,
        predicted=round(predicted, 2),
        uncertainty_pct=round(unc, 4),
        days_ahead=days_ahead,
    )


# ---- Self-test (eenvoudige sanity check) ----
# Verwacht resultaat voor v1.3-model (ongewijzigd in v1.7 voor werkdag-casus):
#   factor zon (45% van seizoen): +3
#   factor wind (6 m/s zwakke wind): +1
#   factor temperatuur (8°C, koud): +1
#   factor gas (105% van 30d gem.): 0
#   factor dagtype (donderdag werkdag): 0
#   factor uurpatroon (19:00 winter avondspits): +2
#   Totaal: +7.  Baseline 25.40 EUR/MWh.
#   Voorspelling: 25.40 × (1 + 7 × 0.02) = 28.96 EUR/MWh.
#   Onzekerheid op 4d, |7| punten: 0.10 + 0.02×4 + 0.01×7 = 0.25 (±25%).

if __name__ == "__main__":
    target = datetime(2025, 12, 11, 19, 0)
    base_dates = [
        datetime(2025, 12, 4, 19, 0),   # do
        datetime(2025, 12, 5, 19, 0),   # vr
        datetime(2025, 12, 8, 19, 0),   # ma
        datetime(2025, 12, 9, 19, 0),   # di
        datetime(2025, 12, 10, 19, 0),  # wo
    ]
    base_prices = [24.0, 26.0, 25.0, 26.0, 26.0]  # gemiddelde = 25.40
    history = [{"time": d.isoformat(), "price": p} for d, p in zip(base_dates, base_prices)]

    f = forecast_one(
        target_dt=target,
        history=history,
        shortwave_ratio=0.45,
        wind_ms=6.0,
        temp_c=8.0,
        ttf_ratio=1.05,
        days_ahead=4,
    )
    assert f is not None, "Forecast moest lukken"
    print(f"Target: {f.target_iso}")
    print(f"Baseline: {f.baseline} EUR/MWh")
    for fs in f.factors:
        print(f"  Factor {fs.name:13s}: {fs.points:+d}  ({fs.reason})")
    print(f"Totaal punten: {f.total_points:+d}")
    print(f"Voorspelling: {f.predicted} EUR/MWh")
    print(f"Onzekerheid: ±{f.uncertainty_pct*100:.0f}%  (band {f.lower:.2f} - {f.upper:.2f})")

    assert abs(f.baseline - 25.40) < 0.01, f"Verwachtte baseline 25.40, kreeg {f.baseline}"
    assert f.total_points == 7, f"Verwachtte 7 punten (v1.7 werkdag), kreeg {f.total_points}"
    assert abs(f.predicted - 28.96) < 0.1, f"Verwachtte ~28.96 (v1.7 werkdag), kreeg {f.predicted}"
    assert abs(f.uncertainty_pct - 0.25) < 0.001, f"Verwachtte ±25%, kreeg ±{f.uncertainty_pct*100:.0f}%"

    # Extra test v1.7: 1 mei (EU-feestdag, NL open) krijgt -2 van factor_dagtype
    mei1 = datetime(2026, 5, 1, 13, 0)
    score = factor_dagtype(mei1)
    assert score.points == -2, f"Verwachtte -2 voor 1 mei (EU-feestdag), kreeg {score.points}"
    print(f"\n[ok] factor_dagtype 1 mei: {score.points} ({score.reason})")

    # Extra test v1.7: baseline voor werkdag sluit 1 mei uit
    target_vr = datetime(2026, 5, 8, 13, 0)  # volgende vrijdag
    history_met_mei1 = [
        {"time": "2026-04-27T13:00:00", "price": 50.0},  # werkdag (Koningsdag — feestdag)
        {"time": "2026-04-28T13:00:00", "price": 50.0},  # dinsdag werkdag
        {"time": "2026-04-29T13:00:00", "price": 50.0},  # woensdag werkdag
        {"time": "2026-04-30T13:00:00", "price": 50.0},  # donderdag werkdag
        {"time": "2026-05-01T13:00:00", "price": -300.0},  # vrijdag, EU-feestdag — moet NIET meewegen
    ]
    baseline_vr = compute_baseline(target_vr, history_met_mei1)
    assert baseline_vr is not None, "Baseline moest bepaald kunnen worden"
    assert baseline_vr > 0, f"Baseline moet positief zijn (1 mei uitgesloten): {baseline_vr}"
    assert abs(baseline_vr - 50.0) < 0.01, f"Verwachtte baseline 50.0 (1 mei uitgesloten), kreeg {baseline_vr}"
    print(f"[ok] baseline volgende vrijdag (1 mei uitgesloten): {baseline_vr} EUR/MWh")

    print("\n[ok] Self-test geslaagd; v1.7-model — werkdag-voorbeeld + cross-border feestdag filter.")

    # ---- Test v1.8: factor_vorige_dag ----
    # Geval 1: geen prior → 0 punten
    score_none = factor_vorige_dag(None)
    assert score_none.points == 0, f"Verwachtte 0 voor None, kreeg {score_none.points}"

    # Geval 2: vorige dag 140% van baseline → +2 punten
    score_duur = factor_vorige_dag(1.40)
    assert score_duur.points == +2, f"Verwachtte +2 voor ratio 1.40, kreeg {score_duur.points}"

    # Geval 3: vorige dag 65% van baseline → -2 punten
    score_goedkoop = factor_vorige_dag(0.65)
    assert score_goedkoop.points == -2, f"Verwachtte -2 voor ratio 0.65, kreeg {score_goedkoop.points}"

    # Geval 4: vorige dag 100% van baseline → 0 punten
    score_normaal = factor_vorige_dag(1.00)
    assert score_normaal.points == 0, f"Verwachtte 0 voor ratio 1.00, kreeg {score_normaal.points}"

    # Geval 5: integratie — forecast_one met prior_day_price duur (40 EUR/MWh,
    # baseline is 25.40 → ratio 1.575 → +2 punten extra t.o.v. geen prior).
    f_met_prior = forecast_one(
        target_dt=target,
        history=history,
        shortwave_ratio=0.45,
        wind_ms=6.0,
        temp_c=8.0,
        ttf_ratio=1.05,
        days_ahead=2,
        prior_day_price=40.0,  # fors boven baseline → +2 punten
    )
    assert f_met_prior is not None, "Forecast met prior moest lukken"
    vorige_dag_factor = next(f for f in f_met_prior.factors if f.name == "vorige_dag")
    assert vorige_dag_factor.points == +2, (
        f"Verwachtte +2 voor prior 40/25.40≈1.57, kreeg {vorige_dag_factor.points}"
    )
    # Met prior (+2 extra punten): total_points = 7+2 = 9
    # predicted = 25.40 × (1 + 9 × 0.02) = 25.40 × 1.18 = 29.972 ≈ 29.97
    assert f_met_prior.total_points == 9, f"Verwachtte 9 punten, kreeg {f_met_prior.total_points}"
    assert abs(f_met_prior.predicted - 29.97) < 0.1, (
        f"Verwachtte ~29.97 EUR/MWh, kreeg {f_met_prior.predicted}"
    )

    print("[ok] factor_vorige_dag: None→0, duur→+2, goedkoop→-2, normaal→0, integratie→ok")
    print("\n[ok] Self-test geslaagd; v1.8-model — vorige-dag factor toegevoegd.")
