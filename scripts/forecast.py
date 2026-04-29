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


# Vaste NL feestdagen 2025-2027 (kan later naar config.json)
NL_FEESTDAGEN = {
    "2025-01-01", "2025-04-18", "2025-04-20", "2025-04-21", "2025-04-27",
    "2025-05-05", "2025-05-29", "2025-06-08", "2025-06-09",
    "2025-12-25", "2025-12-26",
    "2026-01-01", "2026-04-03", "2026-04-05", "2026-04-06", "2026-04-27",
    "2026-05-05", "2026-05-14", "2026-05-24", "2026-05-25",
    "2026-12-25", "2026-12-26",
    "2027-01-01", "2027-03-26", "2027-03-28", "2027-03-29", "2027-04-27",
    "2027-05-05", "2027-05-06", "2027-05-16", "2027-05-17",
    "2027-12-25", "2027-12-26",
}

# Gewicht per punt (zie methodologie sectie 3.2)
POINT_WEIGHT = 0.04


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
    Gemiddelde EPEX-prijs van de laatste 7 dagen voor hetzelfde uur en hetzelfde dagtype.

    history: lijst van {time: ISO-string, price: float in EUR/MWh}
    Return: baseline in EUR/MWh, of None als er geen data is.
    """
    target_hour = target_dt.hour
    target_type = dagtype(target_dt)
    cutoff_start = target_dt - timedelta(days=7)
    cutoff_end = target_dt

    matches = []
    for entry in history:
        t = datetime.fromisoformat(entry["time"])
        if t < cutoff_start or t >= cutoff_end:
            continue
        if t.hour != target_hour:
            continue
        if dagtype(t) != target_type:
            continue
        matches.append(entry["price"])

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

def factor_temperatuur(temp_c: float) -> FactorScore:
    if temp_c < 0:
        pts, reason = +2, f"vorst ({temp_c:.1f}°C)"
    elif temp_c < 10:
        pts, reason = +1, f"koud ({temp_c:.1f}°C)"
    elif temp_c <= 25:
        pts, reason = 0, f"mild ({temp_c:.1f}°C)"
    else:
        pts, reason = +1, f"warm ({temp_c:.1f}°C)"
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

def factor_dagtype(dt: datetime) -> FactorScore:
    if is_feestdag(dt):
        return FactorScore("dagtype", -2, "feestdag")
    wd = dt.weekday()
    if wd == 6:
        return FactorScore("dagtype", -2, "zondag")
    if wd == 5:
        return FactorScore("dagtype", -1, "zaterdag")
    if wd in (1, 2, 3):
        return FactorScore("dagtype", +1, "werkdag (di/wo/do)")
    return FactorScore("dagtype", 0, "ma/vrijdag")


# ---- Factor 6: Uurpatroon ----

def factor_uurpatroon(dt: datetime) -> FactorScore:
    h = dt.hour
    zomer = is_zomer(dt)
    season = "zomer" if zomer else "winter"
    if 0 <= h <= 5:
        return FactorScore("uurpatroon", -2, f"{season}, nacht ({h}:00)")
    if 6 <= h <= 8:
        pts = +1 if zomer else +2
        return FactorScore("uurpatroon", pts, f"{season}, ochtendspits ({h}:00)")
    if 9 <= h <= 14:
        pts = -1 if zomer else 0
        return FactorScore("uurpatroon", pts, f"{season}, midden van de dag ({h}:00)")
    if 15 <= h <= 16:
        return FactorScore("uurpatroon", 0, f"{season}, namiddag ({h}:00)")
    if 17 <= h <= 20:
        pts = +1 if zomer else +2
        return FactorScore("uurpatroon", pts, f"{season}, avondspits ({h}:00)")
    return FactorScore("uurpatroon", -1, f"{season}, late avond ({h}:00)")


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
) -> Optional[Forecast]:
    """
    Voorspel de prijs voor een specifiek toekomstig uur.

    Return: Forecast object, of None als baseline niet bepaald kon worden.
    """
    baseline = compute_baseline(target_dt, history)
    if baseline is None:
        return None

    factors = [
        factor_zon(shortwave_ratio),
        factor_wind(wind_ms),
        factor_temperatuur(temp_c),
        factor_gas(ttf_ratio),
        factor_dagtype(target_dt),
        factor_uurpatroon(target_dt),
    ]
    total = sum(f.points for f in factors)
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

if __name__ == "__main__":
    # Self-test: winter-donderdag 19:00 met +8 punten (matcht methodologie sectie 5).
    # Gekozen: donderdag 11 december 2025 19:00 (echte winter-donderdag).
    target = datetime(2025, 12, 11, 19, 0)
    # Synthetic history: 5 werkdagen 19:00 in 7 dagen daarvoor (4 t/m 10 dec 2025).
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

    # Verwacht uit methodologie: baseline 25.40, totaal +8 punten, voorspelling ~33.53, onzekerheid 26%
    assert abs(f.baseline - 25.40) < 0.01, f"Verwachtte baseline 25.40, kreeg {f.baseline}"
    assert f.total_points == 8, f"Verwachtte 8 punten, kreeg {f.total_points}"
    assert abs(f.predicted - 33.53) < 0.1, f"Verwachtte ~33.53, kreeg {f.predicted}"
    assert abs(f.uncertainty_pct - 0.26) < 0.001, f"Verwachtte ±26%, kreeg ±{f.uncertainty_pct*100:.0f}%"
    print("\n[ok] Self-test geslaagd; voorspelling matcht methodologie sectie 5 voorbeeld.")
