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
import math


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
# v1.10 (2026-05-04): verder verlaagd naar 0.015 na backtest op echte ENTSO-E data
# (60 dagen, mrt-mei 2026): bias +7.5 EUR/MWh op 1d. Puntenverdeling structureel
# positief (gem +1.03), wat samen met POINT_WEIGHT 0.02 de opwaartse bias verklaart.
# Zie 01-documenten/backtest-resultaat-v1.md.
# v2.0 (2026-05-06): verdubbeld naar 0.030 na analyse prediction_log 6 mei.
# MAE 32.4 EUR/MWh; dynamische range bij 0.015 onvoldoende om grote
# baseline-afwijkingen te overbruggen (max ±21% bij 14 punten → nu ±42%).
# Verdere kalibratie gepland op 18 mei zodra meer data beschikbaar is.
POINT_WEIGHT = 0.030

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
# v1.10: "nonlinear" toegevoegd — zie nonlinear_correction() hieronder.
ENABLED_FACTORS = {"zon", "wind", "temperatuur", "gas", "dagtype", "uurpatroon", "vorige_dag", "nonlinear"}

# v1.6: zondag-boost voor weersfactoren.
# Backtest v3 toonde een hardnekkige bias van +27 EUR/MWh op zondag-uren die niet
# door de v1.4 weekend-baseline-fix werd opgelost. Op zondag is de basale stroomvraag
# lager (geen industrie, weinig commercieel) dus dezelfde MWh aan zon- en
# windproductie drukt de prijs sterker. Een zonnige+winderige zondag laat prijzen
# diep zakken; een bewolkte+windstille zondag piekt de prijs juist. v1.6 versterkt
# alleen op zondag de zon- en wind-factoren met deze multiplier; andere dagen
# ongewijzigd. Andere factoren (temperatuur, gas, dagtype, uurpatroon) blijven 1x.
ZONDAG_BOOST = 2

# ---- Marktregimes (v1.7 sectie 5) ----
# Het model detecteert eerst het regime, dat bepaalt welke aanvullende
# correcties van toepassing zijn (o.a. niet-lineaire oversupply-factor).
REGIME_NORMAL     = "normaal"       # Normaal Evenwicht
REGIME_OVERSUPPLY = "oversupply"    # Hernieuwbare Oversupply
REGIME_SCARCITY   = "schaarste"     # Schaarste / Dunkelflaute
REGIME_TRANSITION = "transitie"     # Transitie / Volatiliteit (toekomstig)


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
    regime: str = REGIME_NORMAL          # v1.7: gedetecteerd marktregime
    extreme_event_prob: float = 0.0      # v1.7: kans op negatieve prijs (0..1)

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

def compute_baseline(
    target_dt: datetime,
    history: list[dict],
    regime: str = "",
) -> Optional[float]:
    """
    Robuuste baseline-prijs voor hetzelfde uur en hetzelfde dagtype.

    Window-keuze:
    - werkdag/feestdag normaal:           laatste 7 dagen  (~5 werkdag-punten).
    - weekend normaal:                    laatste 14 dagen (v1.4, geeft 2 punten/uur).
    - werkdag/feestdag oversupply:        laatste 4 dagen  (v1.11).
    - weekend oversupply:                 laatste 7 dagen  (v1.11).
    - werkdag/feestdag oversupply 9-17h:  laatste 2 dagen  (v1.12, zie hieronder).

    v1.11: korter baseline-window bij REGIME_OVERSUPPLY.
    Backtest (mrt-mei 2026) toonde een oversupply-bias van +19 EUR/MWh ondanks
    sterkere factoren en niet-lineaire correctie. Oorzaak: de 7d-baseline loopt
    1-2 weken achter op een structurele prijsdaling door toenemende zon. Tijdens
    een aanhoudend oversupply-regime (meerdere zonnige dagen op rij) reflecteert
    een 4-daags window de actuele markt veel beter dan 7 dagen. Fallback naar
    7 dagen als <2 datapunten beschikbaar zijn.

    v1.12: solar-piekuren (9-17h) krijgen een nog korter 2-daags window.
    De prijzen tijdens solar-piek veranderen het snelst: een patroon van toenemende
    zonnepanelen in mrt-mei duwt de middagprijzen structureel elke week lager. Een
    2-daags window pikt dit sneller op dan 4 dagen. Fallback naar 7d als er minder
    dan 2 matches zijn.

    v1.7: cross-border feestdagen worden uitgesloten van de werkdag-baseline.
    v1.9: mediaan in plaats van gemiddelde (robuuster tegen uitschieters).

    history: lijst van {time: ISO-string, price: float in EUR/MWh}
    regime:  REGIME_OVERSUPPLY verkort het window; andere waarden gebruiken standaard.
    Return:  baseline in EUR/MWh, of None als er geen data is.
    """
    target_hour = target_dt.hour
    target_type = dagtype(target_dt)

    # Window-keuze: oversupply gebruikt kortere windows om sneller te adaptieren.
    # v1.12: solar-piekuren (9-17h) krijgen extra-kort 2-daags window.
    if regime == REGIME_OVERSUPPLY:
        if target_type == "weekend":
            window_days, fallback_days = 7, 14
        elif 9 <= target_hour <= 17:
            window_days, fallback_days = 2, 7   # v1.12: solar-piek extra kort
        else:
            window_days, fallback_days = 4, 7   # v1.11: overige oversupply-uren
    else:
        window_days = 14 if target_type == "weekend" else 7
        fallback_days = 14

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
            if target_type == "werkdag" and is_crossborder_feestdag(t):
                continue
            matches.append(entry["price"])
        return matches

    matches = _collect(cutoff_start)

    # Fallback: te weinig datapunten — verleng window
    if len(matches) < 2 and window_days < fallback_days:
        matches = _collect(target_dt - timedelta(days=fallback_days))

    if not matches:
        return None
    s = sorted(matches)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


# ---- Factor 1: Zonproductie ----

def factor_zon(shortwave_ratio: float) -> FactorScore:
    """
    shortwave_ratio: voorspelde dagelijkse straling / seizoengemiddelde.

    v1.10: extra trap voor solar_ratio > 2.0 toegevoegd. Backtest (mrt-mei 2026)
    toonde dat de max -3 bij ratio > 1.5 grofweg gelijk bleef voor extreem zonnige
    dagen (ratio 1.6-2.5+), terwijl de werkelijke prijsdaling daar veel sterker was.
    """
    if shortwave_ratio < 0.50:
        pts, reason = +3, f"bewolkt ({shortwave_ratio*100:.0f}% van seizoen)"
    elif shortwave_ratio < 0.80:
        pts, reason = +1, f"iets minder zon ({shortwave_ratio*100:.0f}%)"
    elif shortwave_ratio <= 1.20:
        pts, reason = 0, f"normaal ({shortwave_ratio*100:.0f}%)"
    elif shortwave_ratio <= 1.50:
        pts, reason = -1, f"zonnig ({shortwave_ratio*100:.0f}%)"
    elif shortwave_ratio <= 2.00:
        pts, reason = -3, f"heel zonnig ({shortwave_ratio*100:.0f}%)"
    else:
        pts, reason = -5, f"extreem zonnig ({shortwave_ratio*100:.0f}%)"
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
#
# v1.10: zomerpatroon 15-18h herzien. Backtest (mrt-mei 2026) toonde een
# bias van +23 tot +33 EUR/MWh op 14:00-18:00 uur. Zonne-energie in het
# voorjaar/zomer drukt de prijs ook in de namiddag en vroege avond —
# de traditionele "avondspits" schuift in de zomer op naar 19-20h.
# Nieuw zomer: 15-16h: 0→-1 (zonneplateau),  17-18h: +1→0 (geen avondspits meer).

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
        pts = -1 if zomer else 0
        return FactorScore("uurpatroon", pts, f"{season}, namiddag ({h}:00)")
    if 17 <= h <= 18:
        pts = 0 if zomer else +2
        return FactorScore("uurpatroon", pts, f"{season}, vroege avond ({h}:00)")
    if 19 <= h <= 20:
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


# ---- Regime detectie (v1.7 sectie 5) ----

def detect_regime(solar_ratio: float, wind_ms: float, temp_c: float, dt: datetime) -> str:
    """
    Detecteer marktregime voor een uur op basis van zon, wind en temperatuur.

    Regime 3 (Schaarste/Dunkelflaute): alle drie drempels gelijktijdig overschreden.
      solar < 60% EN wind < 5 m/s EN temp < 8°C — gasprijs bepaalt de markt.
    Regime 2 (Oversupply): sterke hernieuwbare productie + lage vraag.
      - Zon-trigger: solar > 140% EN uur 8-18  (v1.12: daglichturen; v1.13:
        verkleind tot 8-18h omdat uren 19-20h juist HOGE avondprijzen hebben
        door ramp-up vraag na zonsondergang — bias was −15 tot −22 EUR/MWh).
      - Wind-trigger: wind > 14 m/s AND (weekend/feestdag/warm) 24/7  (v1.12:
        drempel verhoogd van 12→14 m/s om fout-positieven te verminderen).
    Regime 1 (Normaal): alles overig.
    Regime 4 (Transitie): vereist Δ-weersverwachting als input — nog niet geïmplementeerd.

    v1.12: backtest (mrt-mei 2026): zon-trigger beperkt tot 7-20h, winddrempel 14 m/s.
    v1.13: zon-trigger verder ingeperkt naar 8-18h. Backtest toonde −15 tot −22 EUR/MWh
    bias op uren 19-20h (hoge avondprijzen, kortere baseline trok voorspelling te laag).
    """
    is_low_demand = dt.weekday() >= 5 or is_feestdag(dt) or temp_c > 10.0

    # Schaarste: lage zon + windstil + koud
    if solar_ratio < 0.60 and wind_ms < 5.0 and temp_c < 8.0:
        return REGIME_SCARCITY

    # Oversupply zon: alleen tijdens daglichturen (zon heeft 's nachts geen effect)
    if solar_ratio > 1.40 and 8 <= dt.hour <= 18 and is_low_demand:  # v1.13: 8-18h
        return REGIME_OVERSUPPLY

    # Oversupply wind: geldt 24/7, maar hogere drempel (14 m/s) om fout-positieven
    # te beperken — wind bij 12-13 m/s verhoogt weliswaar productie maar duwt
    # nacht-prijzen in NL zelden negatief.
    if wind_ms > 14.0 and is_low_demand:
        return REGIME_OVERSUPPLY

    return REGIME_NORMAL


# ---- Factor 8: Niet-lineaire oversupply correctie (v1.7 sectie 8) ----
# De lineaire factoren zon (-3 max) en wind (-3 max) onderschatten extreme events.
# Bij solar_ratio = 2.0 of wind = 20 m/s drukt de markt de prijs exponentieel omlaag.
# Deze correctie is ALLEEN actief in REGIME_OVERSUPPLY; in andere regimes 0.
#
# Formule (kwadratisch):
#   solar_penalty = -(solar_ratio - 1.3)² × 14   [punten] (only if > 1.3)
#   wind_penalty  = -(wind_ms - 16)² × 0.25       [punten] (only if > 16 m/s)
#
# Effect op de voorspelling (POINT_WEIGHT = 0.015):
#   solar_ratio = 1.4 → solar_penalty = -(0.1)² × 14 = -0.14 → 0 pt
#   solar_ratio = 1.5 → solar_penalty = -(0.2)² × 14 = -0.56 → -1 pt → -1.5% baseline
#   solar_ratio = 1.8 → solar_penalty = -(0.5)² × 14 = -3.5  → -4 pt → -6% baseline
#   solar_ratio = 2.0 → solar_penalty = -(0.7)² × 14 = -6.86 → -7 pt → -10% baseline
#   solar_ratio = 2.5 → solar_penalty = -(1.2)² × 14 = -20.2 → -20 pt → -30% baseline
#   wind_ms = 20     → wind_penalty  = -(4)² × 0.25   = -4.0  → -4 pt → -6% baseline
#
# v1.10: multiplier zon 8→14 na backtest die toonde dat oversupply-bias +18.8 EUR/MWh
# was; de eerdere correctie was te klein om extreem zonnige voorjaarsdagen te vangen.
# v1.12: drempel zon 1.5→1.3 zodat de correctie al actief is voor typische oversupply
# (solar_ratio 1.4-1.8). Backtest toonde dat correctie bij drempel 1.5 pas kickte bij
# solar_ratio > 1.5, terwijl de trigger al 1.4 is. Nu actief voor vrijwel alle
# oversupply-uren.

def nonlinear_correction(solar_ratio: float, wind_ms: float, regime: str) -> FactorScore:
    """Niet-lineaire correctie voor extreme oversupply (v1.7 sectie 8, v1.10, v1.12)."""
    if regime != REGIME_OVERSUPPLY:
        return FactorScore("nonlinear", 0, "n.v.t.")

    solar_extra = -(max(0.0, solar_ratio - 1.3) ** 2) * 14.0  # v1.12: drempel 1.5→1.3
    wind_extra  = -(max(0.0, wind_ms - 16.0) ** 2) * 0.25
    total_float = solar_extra + wind_extra
    pts = round(total_float)

    parts = []
    if solar_extra < -0.05:
        parts.append(f"zon {solar_extra:.1f}p")
    if wind_extra < -0.05:
        parts.append(f"wind {wind_extra:.1f}p")
    reason = "oversupply niet-lineair: " + (", ".join(parts) if parts else "grensgeval")
    return FactorScore("nonlinear", pts, reason)


# ---- Extreme event probabiliteit (v1.7 sectie 9) ----

def calc_extreme_event_prob(solar_ratio: float, wind_ms: float, regime: str) -> float:
    """
    Kans op negatieve EPEX-prijs (0..0.95) bij extreme oversupply (v1.7 sectie 9).

    Logistische functie op severity = max(solar_ratio/1.4, wind_ms/12).
    Drempel: severity 1.2 → P ≈ 50%  (solar ≈ 1.68 of wind ≈ 14.4 m/s).
    Alleen zinvol in REGIME_OVERSUPPLY; anders 0.0.
    """
    if regime != REGIME_OVERSUPPLY:
        return 0.0
    severity = max(
        solar_ratio / 1.4 if solar_ratio > 1.4 else 0.0,
        wind_ms / 12.0 if wind_ms > 12.0 else 0.0,
    )
    if severity <= 0.0:
        return 0.0
    x = 2.5 * (severity - 1.2)
    p = 1.0 / (1.0 + math.exp(-x))
    return round(min(p, 0.95), 3)


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
    # v1.7: regime detectie — vóór baseline zodat window-keuze regime-bewust is
    regime = detect_regime(shortwave_ratio, wind_ms, temp_c, target_dt)

    # v1.11: geef regime door aan baseline zodat oversupply kortere window gebruikt
    baseline = compute_baseline(target_dt, history, regime=regime)
    if baseline is None:
        return None

    # Factor 7: vorige dag — normaliseer de prior_price op zijn eigen baseline.
    # Gebruik standaard window (geen regime-override) voor de prior_baseline:
    # de vorige dag was een andere dag met mogelijk ander regime.
    prior_ratio: Optional[float] = None
    if prior_day_price is not None:
        prior_dt = target_dt - timedelta(days=1)
        prior_baseline = compute_baseline(prior_dt, history)
        if prior_baseline and prior_baseline != 0:
            prior_ratio = prior_day_price / prior_baseline

    # v2.0: uurpatroon-blokkering bij sterk bewolkt uur (sw_ratio_h < 0.30).
    # De uurpatroon-factor gaat ervan uit dat middag goedkoop is door zon
    # (zomer: -1 punt voor 9-16h). Op een bewolkte dag is die aanname onjuist
    # en onderdrukt hij het correcte signaal van factor_zon. Als het uurlijkse
    # zonratio < 0.30 is (minder dan 30% van normaal), wordt uurpatroon op 0
    # gezet zodat factor_zon ongehinderd kan corrigeren.
    _uurpatroon = factor_uurpatroon(target_dt)
    if shortwave_ratio < 0.30 and _uurpatroon.points != 0:
        _uurpatroon = FactorScore(
            "uurpatroon", 0,
            f"geblokkeerd (bewolkt: sw_h={shortwave_ratio:.2f}<0.30)"
        )

    factors = [
        factor_zon(shortwave_ratio),
        factor_wind(wind_ms),
        factor_temperatuur(temp_c),
        factor_gas(ttf_ratio),
        factor_dagtype(target_dt),
        _uurpatroon,
        factor_vorige_dag(prior_ratio),
        nonlinear_correction(shortwave_ratio, wind_ms, regime),  # v1.7
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
    ep = calc_extreme_event_prob(shortwave_ratio, wind_ms, regime)  # v1.7

    return Forecast(
        target_iso=target_dt.isoformat(),
        baseline=round(baseline, 2),
        factors=factors,
        total_points=total,
        predicted=round(predicted, 2),
        uncertainty_pct=round(unc, 4),
        days_ahead=days_ahead,
        regime=regime,
        extreme_event_prob=ep,
    )


# ---- Self-test (eenvoudige sanity check) ----
# Verwacht resultaat voor v2.0-model (werkdag-casus, donderdag 19:00 winter):
#   factor zon (45% van seizoen): +3  [sw_ratio_h=0.45 > 0.30 → uurpatroon NIET geblokkeerd]
#   factor wind (6 m/s zwakke wind): +1
#   factor temperatuur (8°C, koud): +1
#   factor gas (105% van 30d gem.): 0
#   factor dagtype (donderdag werkdag): 0
#   factor uurpatroon (19:00 winter avondspits): +2  [19h = avondspits winter]
#   factor nonlinear: 0  [geen oversupply regime]
#   Totaal: +7.  Baseline mediaan 26.0 EUR/MWh.
#   Voorspelling: 26.0 × (1 + 7 × 0.030) = 26.0 × 1.21 = 31.46 EUR/MWh.
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
    base_prices = [24.0, 26.0, 25.0, 26.0, 26.0]  # mediaan = 26.0 (gesorteerd: 24,25,26,26,26)
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
    print(f"Onzekerheid: +/-{f.uncertainty_pct*100:.0f}%  (band {f.lower:.2f} - {f.upper:.2f})")

    # v2.0: baseline = mediaan [24,25,26,26,26] = 26.0
    # Voorspelling: 26.0 * (1 + 7 * 0.030) = 26.0 * 1.21 = 31.46
    assert abs(f.baseline - 26.0) < 0.01, f"Verwachtte baseline 26.0 (mediaan), kreeg {f.baseline}"
    assert f.total_points == 7, f"Verwachtte 7 punten, kreeg {f.total_points}"
    assert abs(f.predicted - 31.46) < 0.1, f"Verwachtte ~31.46, kreeg {f.predicted}"
    assert abs(f.uncertainty_pct - 0.25) < 0.001, f"Verwachtte +/-25%, kreeg {f.uncertainty_pct}"

    # Test v1.7: factor_dagtype 1 mei
    mei1 = datetime(2026, 5, 1, 13, 0)
    score = factor_dagtype(mei1)
    assert score.points == -2, f"Verwachtte -2 voor 1 mei, kreeg {score.points}"
    print(f"\n[ok] factor_dagtype 1 mei: {score.points} ({score.reason})")

    # Test v1.7: baseline sluit 1 mei uit
    target_vr = datetime(2026, 5, 8, 13, 0)
    history_met_mei1 = [
        {"time": "2026-04-27T13:00:00", "price": 50.0},
        {"time": "2026-04-28T13:00:00", "price": 50.0},
        {"time": "2026-04-29T13:00:00", "price": 50.0},
        {"time": "2026-04-30T13:00:00", "price": 50.0},
        {"time": "2026-05-01T13:00:00", "price": -300.0},  # EU-feestdag: moet worden uitgesloten
    ]
    baseline_vr = compute_baseline(target_vr, history_met_mei1)
    assert baseline_vr is not None
    assert abs(baseline_vr - 50.0) < 0.01, f"Verwachtte 50.0 (1 mei uitgesloten), kreeg {baseline_vr}"
    print(f"[ok] baseline vrijdag (1 mei uitgesloten, mediaan): {baseline_vr} EUR/MWh")

    # Test v1.8/v1.9: factor_vorige_dag
    assert factor_vorige_dag(None).points == 0
    assert factor_vorige_dag(1.40).points == +2
    assert factor_vorige_dag(0.65).points == -2
    assert factor_vorige_dag(1.00).points == 0

    # Integratie: prior 40 EUR/MWh, baseline mediaan 26.0 -> ratio ~1.54 -> +2 punten
    # total = 7+2 = 9; predicted = 26.0 * (1 + 9*0.030) = 26.0 * 1.27 = 33.02
    f2 = forecast_one(
        target_dt=target,
        history=history,
        shortwave_ratio=0.45,
        wind_ms=6.0,
        temp_c=8.0,
        ttf_ratio=1.05,
        days_ahead=2,
        prior_day_price=40.0,
    )
    assert f2 is not None
    vd = next(x for x in f2.factors if x.name == "vorige_dag")
    assert vd.points == +2, f"Verwachtte +2, kreeg {vd.points}"
    assert f2.total_points == 9, f"Verwachtte 9 punten, kreeg {f2.total_points}"
    assert abs(f2.predicted - 33.02) < 0.1, f"Verwachtte ~33.02, kreeg {f2.predicted}"
    print("[ok] factor_vorige_dag: alle gevallen ok")

    # Test v1.7/v1.10: regime detectie
    # Donderdag werkdag winter
    thu_winter = datetime(2025, 12, 11, 14, 0)
    assert detect_regime(1.0, 7.0, 5.0, thu_winter) == REGIME_NORMAL, "Verwachtte normaal"
    # Oversupply: zonnige zaterdag (lage vraag, hoge zon)
    sat_sunny = datetime(2025, 6, 14, 13, 0)  # zaterdag
    assert detect_regime(1.6, 8.0, 18.0, sat_sunny) == REGIME_OVERSUPPLY, "Verwachtte oversupply"
    # Dunkelflaute: donker + windstil + koud
    assert detect_regime(0.4, 3.0, 2.0, thu_winter) == REGIME_SCARCITY, "Verwachtte schaarste"
    print("[ok] detect_regime: alle gevallen ok")

    # Test v1.10/v1.12: nonlinear_correction
    # v1.12: drempel 1.5→1.3, dus correctie begint eerder
    nl_normal = nonlinear_correction(1.0, 8.0, REGIME_NORMAL)
    assert nl_normal.points == 0, f"Verwachtte 0 bij normaal regime, kreeg {nl_normal.points}"
    nl_oversupply_mild = nonlinear_correction(1.5, 10.0, REGIME_OVERSUPPLY)
    assert nl_oversupply_mild.points == -1, f"Verwachtte -1 bij solar=1.5 (drempel 1.3), kreeg {nl_oversupply_mild.points}"
    nl_oversupply_extreme = nonlinear_correction(2.0, 8.0, REGIME_OVERSUPPLY)
    # -(2.0-1.3)^2 * 14 = -(0.7)^2 * 14 = -6.86 -> round(-6.86) = -7
    assert nl_oversupply_extreme.points == -7, f"Verwachtte -7 bij solar=2.0 (drempel 1.3), kreeg {nl_oversupply_extreme.points}"
    print("[ok] nonlinear_correction: alle gevallen ok")

    # Test v1.12: detect_regime uur-restrictie zon
    sat_noon = datetime(2025, 6, 14, 13, 0)    # zaterdag 13u -> oversupply
    sat_night = datetime(2025, 6, 14, 2, 0)     # zaterdag 02u -> normaal (zon niet actief)
    sat_night_wind = datetime(2025, 6, 14, 2, 0)
    assert detect_regime(1.6, 8.0, 18.0, sat_noon) == REGIME_OVERSUPPLY
    assert detect_regime(1.6, 8.0, 18.0, sat_night) == REGIME_NORMAL, "Nacht+zon moet NORMAAL zijn"
    assert detect_regime(0.0, 15.0, 18.0, sat_night_wind) == REGIME_OVERSUPPLY, "Wind>14 's nachts = oversupply"
    assert detect_regime(0.0, 12.0, 18.0, sat_night_wind) == REGIME_NORMAL, "Wind 12 m/s < drempel 14"
    print("[ok] detect_regime v1.12: uur-restrictie zon + winddrempel 14 m/s")

    # Test v1.12: compute_baseline 2d solar-piekuur
    # Vrijdag 9 jan, werkdag, 12u; history: ma-do met dalende prijs
    target_12u = datetime(2026, 1, 9, 12, 0)
    history_dalend = [
        {"time": datetime(2026, 1, d, 12, 0).isoformat(), "price": float(100 - (d-5)*10)}
        for d in range(5, 9)  # ma=100, di=90, wo=80, do=70
    ]
    b_os_2d = compute_baseline(target_12u, history_dalend, regime=REGIME_OVERSUPPLY)
    b_nm_7d = compute_baseline(target_12u, history_dalend)
    # OS 2d: vr 9 - 2d = wo 7. Matches: wo 7 (80), do 8 (70) -> mediaan 75
    # Normal 7d: ma t/m do: 100, 90, 80, 70 -> mediaan 85
    assert abs(b_os_2d - 75.0) < 0.01, f"Baseline oversupply 12u (2d): verwacht 75.0, kreeg {b_os_2d}"
    assert abs(b_nm_7d - 85.0) < 0.01, f"Baseline normaal 12u (7d): verwacht 85.0, kreeg {b_nm_7d}"
    print("[ok] compute_baseline v1.12: 2d solar-piek window vs 7d normaal")

    # Test v1.10: calc_extreme_event_prob
    assert calc_extreme_event_prob(1.0, 8.0, REGIME_NORMAL) == 0.0
    ep_mild = calc_extreme_event_prob(1.6, 8.0, REGIME_OVERSUPPLY)
    assert 0.3 < ep_mild < 0.6, f"Verwachtte ~0.46, kreeg {ep_mild}"
    ep_extreme = calc_extreme_event_prob(2.0, 8.0, REGIME_OVERSUPPLY)
    assert ep_extreme > 0.55, f"Verwachtte >0.55, kreeg {ep_extreme}"
    print(f"[ok] calc_extreme_event_prob: mild={ep_mild:.2f}, extreme={ep_extreme:.2f}")

    # Integratie: regime + nonlinear in forecast_one op zonnige zomerzondag
    zomerzondag = datetime(2026, 7, 5, 12, 0)  # zondag
    history_zomer = [
        {"time": f"2026-06-2{i}T12:00:00", "price": 20.0} for i in range(1, 6)
    ] + [
        {"time": "2026-06-27T12:00:00", "price": 15.0},
        {"time": "2026-06-28T12:00:00", "price": 15.0},
    ]
    f_oversupply = forecast_one(
        target_dt=zomerzondag,
        history=history_zomer,
        shortwave_ratio=1.8,
        wind_ms=8.0,
        temp_c=18.0,
        ttf_ratio=1.0,
        days_ahead=1,
    )
    assert f_oversupply is not None
    assert f_oversupply.regime == REGIME_OVERSUPPLY, f"Verwachtte oversupply, kreeg {f_oversupply.regime}"
    assert f_oversupply.extreme_event_prob > 0.3, f"Verwachtte EP > 0.3, kreeg {f_oversupply.extreme_event_prob}"
    print(f"[ok] forecast_one oversupply: regime={f_oversupply.regime}, EP={f_oversupply.extreme_event_prob:.2f}")

    print("\n[ok] Self-test geslaagd; v1.12 hour-restricted oversupply + korter solar-piek window.")
