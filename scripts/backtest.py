"""
backtest.py — Retrospectieve evaluatie van het 6-puntenmodel uit forecast.py.

Doel
----
Voor de afgelopen N dagen (default 30) simuleren we voor elke dag een voorspelling
op horizonten 1, 3, 5 en 7 dagen vooruit, met de inputs zoals die *toen* bekend waren
(prijshistorie tot en met die dag). De voorspelde prijs wordt vergeleken met de prijs
die later daadwerkelijk plaatsvond.

Rapportage:
    - MAE per horizon (1d, 3d, 5d, 7d)
    - Bias per horizon
    - Hit-rate "goedkoop" / "duur" categorisatie
    - Vergelijking met een naïeve baseline (alleen 7d-gemiddelde, zonder factoren)

Output:
    - Markdown-rapport: 01-documenten/backtest-resultaat-v1.md (default)
    - JSON met ruwe datapunten:  03-data/backtest-results.json (default)
    - Met --output-dir gaan beide files naar één map (handig in CI).
    - Korte samenvatting op stderr.

Bronnen
-------
    - ENTSO-E day-ahead prijzen (zelfde token als fetch_prices.py)
    - Open-Meteo Historical Weather API (geen registratie, geen key)
    - Yahoo Finance TTF=F (geen registratie, User-Agent header verplicht)

Caveats (worden ook in het rapport opgenomen)
-------
    - We gebruiken de werkelijke gemeten weers- en TTF-data op de target-dag
      (perfect-foresight weather). De toenmalige weersvoorspelling is niet gratis
      historisch op te halen. Dit overschat de modelkwaliteit licht; bij echte
      live-gebruik vervuilt de weersvoorspellingsfout het model bovenop wat hier
      gemeten wordt. Beslispunten zijn iets soepeler te interpreteren.
    - Seizoenpgemiddelde zonneproductie is hardgecodeerd uit De Bilt klimatologie
      (12 maandgemiddelden). Verbeterbaar later met een dag-resolutie norm.
    - Wind, temperatuur, zon en TTF gebruiken één locatie / één ticker.

Gebruik
-------
    ENTSOE_TOKEN=xxx python scripts/backtest.py
    python scripts/backtest.py --days 30
    python scripts/backtest.py --sample      # zonder ENTSO-E, met synthetische data
    python scripts/backtest.py --output-dir ./out      # alle output naar één map
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Importeer model en hulpfuncties uit forecast.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
from forecast import (  # noqa: E402
    POINT_WEIGHT,
    REGIME_NORMAL, REGIME_OVERSUPPLY, REGIME_SCARCITY, REGIME_SCARCITY_SUMMER,
    REGIME_TRANSITION,
    Forecast,
    FactorScore,
    compute_baseline,
    factor_dagtype,
    factor_gas,
    factor_temperatuur,
    factor_uurpatroon,
    factor_wind,
    factor_zon,
    forecast_one,
    is_feestdag,
    uncertainty,
)

# Hergebruik fetch helpers uit fetch_prices.py
from fetch_prices import (  # noqa: E402
    aggregate_to_hourly,
    amsterdam_now,
    entsoe_period,
    fetch_entsoe,
    parse_entsoe_xml,
    utc_to_amsterdam,
)

# Archief-lezer: maakt backtests over meerdere jaren mogelijk (load_archive.py).
try:
    from load_archive import (
        load_range as _archive_load_range,
        load_same_period as _archive_same_period,
    )
except Exception:  # noqa: BLE001
    _archive_load_range = None
    _archive_same_period = None

# Live MOS bias-correctie hergebruiken zodat de backtest de productie-pijplijn weerspiegelt.
try:
    from run_forecast import (
        apply_bias_correction as _rf_apply_bias,
        load_bias_corrections as _rf_load_bias,
        BIAS_CORRECTIONS_FILE as _RF_BIAS_FILE,
    )
    _HAS_BIAS = True
except Exception:  # noqa: BLE001
    _HAS_BIAS = False


# ---- Paden ----

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_FILE = PROJECT_ROOT.parent / "01-documenten" / "backtest-resultaat-v1.md"
DEFAULT_RAW_FILE = PROJECT_ROOT.parent / "03-data" / "backtest-results.json"
CONFIG_FILE = PROJECT_ROOT / "public" / "data" / "config.json"


# ---- Externe API endpoints ----

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
YAHOO_TTF = "https://query1.finance.yahoo.com/v8/finance/chart/TTF=F"
STOOQ_TTF_FALLBACK = "https://stooq.com/q/d/l/"  # ?s=ttf.f&d1=YYYYMMDD&d2=YYYYMMDD&i=d

# Locatie De Bilt voor klimatologisch representatieve weerdata
DEBILT_LAT = 52.10
DEBILT_LON = 5.18

# Seizoengemiddelde dagstraling in MJ/m²/dag voor De Bilt (klimatologie 1991-2020).
# Bron: KNMI klimaatdata, afgerond. Gebruikt voor `factor_zon` als noemer.
MONTHLY_SOLAR_NORM_MJ = {
    1: 2.5, 2: 5.0, 3: 9.0, 4: 14.0, 5: 17.5, 6: 18.5,
    7: 18.0, 8: 15.5, 9: 11.0, 10: 6.5, 11: 3.0, 12: 2.0,
}


def derive_eur_mwh_thresholds(config: dict) -> dict:
    """Leid kale-EPEX drempels (EUR/MWh) af voor categorisatie.

    De config bewaart drempels als consumentenprijs in ct/kWh incl. energiebelasting,
    btw en leverancieropslag (`thresholds_ct_kwh_inclusive`). De backtest werkt op
    kale EPEX-prijzen in EUR/MWh, dus we rekenen terug:

        consument_eur_kwh = (EPEX_eur_mwh/1000 + opslag + energiebelasting) * btw_factor
        => EPEX_eur_mwh    = (drempel_ct/100 / btw_factor - energiebelasting - opslag) * 1000

    Als de config al een expliciete `thresholds_eur_per_mwh` bevat, krijgt die voorrang.
    """
    if "thresholds_eur_per_mwh" in config:
        return config["thresholds_eur_per_mwh"]

    ct = config["thresholds_ct_kwh_inclusive"]
    taxes = config.get("taxes", {})
    energiebelasting = float(taxes.get("energiebelasting_per_kwh", 0.0916))
    btw = float(taxes.get("btw_factor", 1.21))

    # Gemiddelde leverancieropslag uit de 'average'-supplier (anders eerste, anders fallback).
    markup = 0.0178
    for sup in config.get("suppliers", []):
        if sup.get("id") == "average":
            markup = float(sup.get("markup_per_kwh", markup))
            break
    else:
        sups = config.get("suppliers", [])
        if sups:
            markup = float(sups[0].get("markup_per_kwh", markup))

    def to_epex(drempel_ct: float) -> float:
        return round((drempel_ct / 100.0 / btw - energiebelasting - markup) * 1000.0, 2)

    return {
        "very_cheap": to_epex(ct["very_cheap"]),
        "cheap":      to_epex(ct["cheap"]),
        "pricey":     to_epex(ct["pricey"]),
        "very_pricey": to_epex(ct["very_pricey"]),
    }


# ---- ENTSO-E historie (uitgebreide range) ----

def fetch_entsoe_range(token: str, start_ams: datetime, end_ams: datetime) -> list[dict]:
    """Haal ENTSO-E day-ahead prijzen op voor een ruimere historische periode.

    ENTSO-E accepteert grote ranges in één call (we hebben tot ~40 dagen probleemloos
    gezien). Bij failure splitsen we in chunks van 14 dagen.
    """
    start_utc = start_ams.astimezone(timezone.utc)
    end_utc = end_ams.astimezone(timezone.utc)

    try:
        prices = fetch_entsoe(token, start_utc, end_utc)
        if prices:
            return aggregate_to_hourly(prices)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] enkele range-call mislukt ({exc}); val terug op chunks.", file=sys.stderr)

    # Chunked fallback
    all_prices: list[dict] = []
    chunk = timedelta(days=14)
    cursor = start_utc
    while cursor < end_utc:
        chunk_end = min(cursor + chunk, end_utc)
        try:
            part = fetch_entsoe(token, cursor, chunk_end)
            all_prices.extend(part)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] chunk {cursor.date()}-{chunk_end.date()} mislukt: {exc}",
                  file=sys.stderr)
        cursor = chunk_end

    return aggregate_to_hourly(all_prices)


def load_prices_from_archive(start_ams: datetime, end_ams: datetime) -> list[dict]:
    """Lees uurprijzen uit het historische archief (public/data/archief/).

    Maakt backtests over willekeurige historische periodes mogelijk zonder ENTSO-E
    opnieuw te bevragen. end_ams is exclusief.
    """
    if _archive_load_range is None:
        raise RuntimeError(
            "load_archive.py niet gevonden naast backtest.py; kan het archief niet lezen."
        )
    prices = _archive_load_range(start_ams, end_ams)
    print(f"[info] {len(prices)} uurprijzen uit archief geladen "
          f"({start_ams.date()} t/m {(end_ams - timedelta(days=1)).date()}).", file=sys.stderr)
    return prices


# ---- Open-Meteo Historical Weather ----

def fetch_open_meteo_archive(start_date: str, end_date: str) -> dict[str, dict]:
    """Haal dagelijkse weerstatistieken voor De Bilt op tussen start_date en end_date.

    start_date / end_date: ISO-datum strings (YYYY-MM-DD). Inclusief beide.
    Return: {YYYY-MM-DD: {shortwave_mj, wind_ms, temp_c}}
    """
    params = {
        "latitude": DEBILT_LAT,
        "longitude": DEBILT_LON,
        "start_date": start_date,
        "end_date": end_date,
        "daily": ",".join([
            "shortwave_radiation_sum",   # MJ/m²/dag
            "wind_speed_10m_max",        # km/h - daily max
            "temperature_2m_mean",       # °C
            "wind_speed_10m_mean",       # km/h - daily mean (sinds eind 2024 in archive)
        ]),
        "wind_speed_unit": "ms",
        "timezone": "Europe/Amsterdam",
    }
    url = f"{OPEN_METEO_ARCHIVE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "stroomvoorspeller/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    daily = data.get("daily", {})
    times = daily.get("time", [])
    sw = daily.get("shortwave_radiation_sum", [])
    wmean = daily.get("wind_speed_10m_mean", [])
    wmax = daily.get("wind_speed_10m_max", [])
    tmean = daily.get("temperature_2m_mean", [])

    out: dict[str, dict] = {}
    for i, day in enumerate(times):
        # Voorkeur voor mean wind; valt anders terug op 0.7 * max (heuristiek)
        w_ms_10m = None
        if i < len(wmean) and wmean[i] is not None:
            w_ms_10m = float(wmean[i])
        elif i < len(wmax) and wmax[i] is not None:
            w_ms_10m = 0.7 * float(wmax[i])
        # Schaal 10m -> 100m via simpele machtswet (alpha=0.14, open terrein):
        # v100 = v10 * (100/10)^0.14 ~ v10 * 1.38
        w_ms_100m = w_ms_10m * 1.38 if w_ms_10m is not None else None

        out[day] = {
            "shortwave_mj": float(sw[i]) if i < len(sw) and sw[i] is not None else None,
            "wind_ms": w_ms_100m,
            "temp_c": float(tmean[i]) if i < len(tmean) and tmean[i] is not None else None,
        }
    return out


def seasonal_solar_norm_mj(dt: datetime) -> float:
    """Seizoenpgemiddelde dagstraling in MJ/m²/dag voor De Bilt - lineaire interpolatie
    tussen maandgemiddelden zodat de overgangen niet hard zijn."""
    m = dt.month
    d = dt.day
    # Gebruik dag 15 als midden van de maand
    if d <= 15:
        prev_m = 12 if m == 1 else m - 1
        frac = (d + 15) / 30  # van 0 (15e vorige maand) -> 1 (15e deze maand)
        return MONTHLY_SOLAR_NORM_MJ[prev_m] * (1 - frac) + MONTHLY_SOLAR_NORM_MJ[m] * frac
    next_m = 1 if m == 12 else m + 1
    frac = (d - 15) / 30
    return MONTHLY_SOLAR_NORM_MJ[m] * (1 - frac) + MONTHLY_SOLAR_NORM_MJ[next_m] * frac


# ---- Yahoo Finance TTF ----

def fetch_yahoo_ttf(start_date: datetime, end_date: datetime) -> dict[str, float]:
    """Haal dagelijkse close-koersen voor TTF=F op.

    Return: {YYYY-MM-DD: close_eur_per_mwh}. Yahoo levert TTF=F al in EUR/MWh.
    """
    p1 = int(start_date.replace(tzinfo=timezone.utc).timestamp())
    p2 = int(end_date.replace(tzinfo=timezone.utc).timestamp())
    params = {
        "period1": p1,
        "period2": p2,
        "interval": "1d",
        "events": "history",
    }
    url = f"{YAHOO_TTF}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; stroomvoorspeller/0.1)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    result = data.get("chart", {}).get("result", [])
    if not result:
        return {}
    series = result[0]
    timestamps = series.get("timestamp", []) or []
    quote = series.get("indicators", {}).get("quote", [{}])[0]
    closes = quote.get("close", []) or []

    out: dict[str, float] = {}
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        out[dt.strftime("%Y-%m-%d")] = float(close)
    return out


def ttf_for_date(ttf_series: dict[str, float], target: datetime, max_lookback: int = 5) -> float | None:
    """Haal TTF voor target-datum, val terug op meest recente eerdere koers (weekend/holiday)."""
    for back in range(max_lookback):
        d = (target - timedelta(days=back)).strftime("%Y-%m-%d")
        if d in ttf_series:
            return ttf_series[d]
    return None


def ttf_30d_average(ttf_series: dict[str, float], reference: datetime) -> float | None:
    """Gemiddelde TTF van de 30 kalenderdagen vóór reference (exclusief)."""
    vals: list[float] = []
    for back in range(1, 31):
        d = (reference - timedelta(days=back)).strftime("%Y-%m-%d")
        if d in ttf_series:
            vals.append(ttf_series[d])
    if not vals:
        return None
    return sum(vals) / len(vals)


# ---- Sample-data generators (voor lokaal testen zonder API's) ----

def synth_prices(start_ams: datetime, days: int) -> list[dict]:
    """Synthetische uurprijzen - vergelijkbaar met fetch_prices.generate_sample_prices."""
    rng = random.Random(1234)
    prices = []
    cursor = start_ams.replace(hour=0, minute=0, second=0, microsecond=0)
    for h in range(days * 24):
        t = cursor + timedelta(hours=h)
        hr = t.hour
        if 0 <= hr <= 5:
            base = 35 + rng.uniform(-8, 8)
        elif 6 <= hr <= 8:
            base = 95 + rng.uniform(-15, 15)
        elif 9 <= hr <= 14:
            base = 30 + rng.uniform(-40, 25)
        elif 15 <= hr <= 16:
            base = 65 + rng.uniform(-15, 15)
        elif 17 <= hr <= 20:
            base = 130 + rng.uniform(-20, 30)
        else:
            base = 70 + rng.uniform(-15, 15)
        if t.weekday() >= 5:
            base *= 0.9
        prices.append({"time": t.isoformat(), "price": round(base, 2)})
    return prices


def synth_weather(days: list[str]) -> dict[str, dict]:
    rng = random.Random(7)
    out = {}
    for d in days:
        dt = datetime.fromisoformat(d)
        norm = seasonal_solar_norm_mj(dt)
        out[d] = {
            "shortwave_mj": norm * rng.uniform(0.5, 1.4),
            "wind_ms": rng.uniform(3, 14),
            "temp_c": 12 + 8 * math.sin((dt.timetuple().tm_yday - 80) / 365 * 2 * math.pi)
                       + rng.uniform(-3, 3),
        }
    return out


def synth_ttf(days: list[str]) -> dict[str, float]:
    rng = random.Random(99)
    out = {}
    base = 28.0  # EUR/MWh - typisch 2026 niveau
    for d in days:
        out[d] = round(base + rng.uniform(-5, 5), 2)
    return out


# ---- Backtest-kern ----

def date_range(start: datetime, days: int) -> list[str]:
    """Lijst van YYYY-MM-DD strings voor `days` opeenvolgende kalenderdagen vanaf start."""
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def slice_history_until(prices: list[dict], cutoff_dt: datetime) -> list[dict]:
    """Subset van prices met time < cutoff_dt."""
    return [p for p in prices if datetime.fromisoformat(p["time"]) < cutoff_dt]


def lookup_actual(prices: list[dict], target_dt: datetime) -> float | None:
    """Haal werkelijke EPEX-prijs op voor een specifiek uur."""
    iso = target_dt.isoformat()
    for p in prices:
        if p["time"] == iso:
            return p["price"]
    # Als de string niet exact matcht, probeer datetime-vergelijking
    for p in prices:
        try:
            t = datetime.fromisoformat(p["time"])
            if t == target_dt:
                return p["price"]
        except ValueError:
            continue
    return None


def categorize(price: float, thresholds: dict) -> str:
    """Categoriseer prijs in goedkoop/normaal/duur volgens config-drempels."""
    if price < thresholds["cheap"]:
        return "goedkoop"
    if price > thresholds["pricey"]:
        return "duur"
    return "normaal"


def run_backtest(
    prices: list[dict],
    weather: dict[str, dict],
    ttf: dict[str, float],
    forecast_dates: list[datetime],
    horizons: list[int],
    thresholds: dict,
    bias_corrections: dict | None = None,
    seasonal_fn=None,
    rolling_bias: dict | None = None,
) -> list[dict]:
    """Voor elke forecast_date x horizon x uur: forecast vs actual.

    Een forecast_date representeert de "vandaag waarop we voorspellen".
    De target is forecast_date + horizon dagen, en we voorspellen alle 24 uren ervan.

    rolling_bias (v3.2, optioneel): walk-forward rolling MOS-simulatie. Dict met
    keys half_life, horizon_decay, min_neff, min_eur, cap. Per run-datum worden
    correcties per (regime, uur) berekend uit de fouten van doel-uren die op dat
    moment al bekend zijn (target <= fc_date — geen look-ahead), exponentieel
    gewogen op recentheid. Spiegelt compute_bias.py --mode rolling; per doel-uur
    telt de hervoorspelling met de kleinste horizon (= 'predicted_raw_latest' live).
    Sluit legacy bias_corrections uit (rolling heeft voorrang).
    """
    results: list[dict] = []
    skipped_no_baseline = 0
    skipped_no_actual = 0
    skipped_no_inputs = 0

    # v3.2: walk-forward foutgeheugen voor rolling MOS.
    # raw_errors[target_dt] = (horizon, regime, uur, fout) — kleinste horizon wint.
    raw_errors: dict[datetime, tuple] = {}

    for fc_date in forecast_dates:
        # History = alles voor fc_date 23:59 (dwz. dag fc_date is bekend)
        cutoff = fc_date.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        history_full = slice_history_until(prices, cutoff)

        # v3.2: rolling MOS-correcties voor deze run-datum (alleen bekende fouten).
        rolling_corr: dict[tuple, float] = {}
        if rolling_bias:
            acc: dict[tuple, list] = {}
            for t_dt, (_h, _regime, _hour, _err) in raw_errors.items():
                if t_dt >= cutoff:
                    continue
                age_days = (cutoff - t_dt).total_seconds() / 86400.0
                w = 0.5 ** (age_days / rolling_bias["half_life"])
                a = acc.setdefault((_regime, _hour), [0.0, 0.0])
                a[0] += w * _err
                a[1] += w
            for cell, (we, wsum) in acc.items():
                if wsum >= rolling_bias["min_neff"]:
                    b = we / wsum
                    if abs(b) >= rolling_bias["min_eur"]:
                        rolling_corr[cell] = b

        # TTF op fc_date
        ttf_now = ttf_for_date(ttf, fc_date)
        ttf_avg = ttf_30d_average(ttf, fc_date)
        if ttf_now is None or ttf_avg is None or ttf_avg == 0:
            skipped_no_inputs += 24 * len(horizons)
            continue
        ttf_ratio = ttf_now / ttf_avg

        for h in horizons:
            target_day = fc_date + timedelta(days=h)
            day_key = target_day.strftime("%Y-%m-%d")
            wx = weather.get(day_key)
            if not wx or any(wx.get(k) is None for k in ("shortwave_mj", "wind_ms", "temp_c")):
                skipped_no_inputs += 24
                continue

            sw_ratio = wx["shortwave_mj"] / seasonal_solar_norm_mj(target_day)
            wind = wx["wind_ms"]
            temp = wx["temp_c"]

            seasonal_history = seasonal_fn(target_day) if seasonal_fn else None

            for hour in range(24):
                target_dt = target_day.replace(hour=hour, minute=0, second=0, microsecond=0)

                actual = lookup_actual(prices, target_dt)
                if actual is None:
                    skipped_no_actual += 1
                    continue

                fc = forecast_one(
                    target_dt=target_dt,
                    history=history_full,
                    shortwave_ratio=sw_ratio,
                    wind_ms=wind,
                    temp_c=temp,
                    ttf_ratio=ttf_ratio,
                    days_ahead=h,
                    seasonal_history=seasonal_history,
                )
                if fc is None:
                    skipped_no_baseline += 1
                    continue

                # Naieve baseline (geen factoren) = compute_baseline output zonder punten
                naive = fc.baseline

                # Optioneel: dezelfde MOS bias-correctie als de live-pijplijn (run_forecast.py),
                # zodat de backtest het model meet zoals bezoekers het zien.
                predicted_val = fc.predicted
                bias_applied = 0.0
                if rolling_bias:
                    # v3.2: walk-forward rolling MOS — correctie uit fouten die op
                    # fc_date bekend waren, met horizon-verval en cap.
                    b = rolling_corr.get((fc.regime, hour))
                    if b is not None:
                        decay = rolling_bias["horizon_decay"] ** max(0, h - 2)
                        cap = rolling_bias["cap"]
                        bias_applied = max(-cap, min(cap, b * decay))
                        predicted_val = round(fc.predicted + bias_applied, 2)
                    # Fout bijschrijven voor láátere run-datums (kleinste horizon =
                    # meest recente hervoorspelling wint, zoals live 'latest').
                    prev = raw_errors.get(target_dt)
                    if prev is None or h < prev[0]:
                        raw_errors[target_dt] = (h, fc.regime, hour, actual - fc.predicted)
                elif bias_corrections:
                    _fcd = {
                        "time": target_dt.isoformat(),
                        "predicted": fc.predicted,
                        "lower": fc.lower,
                        "upper": fc.upper,
                        "regime": fc.regime,
                        "sw_ratio_h": sw_ratio,
                        "sw_ratio_daily": sw_ratio,
                        "factors": [],
                    }
                    _rf_apply_bias(_fcd, bias_corrections, target_dt)
                    predicted_val = _fcd["predicted"]

                results.append({
                    "forecast_date": fc_date.strftime("%Y-%m-%d"),
                    "target_iso": target_dt.isoformat(),
                    "horizon_days": h,
                    "hour": hour,
                    "weekday": target_dt.weekday(),
                    "is_feestdag": is_feestdag(target_dt),
                    "actual": round(actual, 2),
                    "predicted": predicted_val,
                    "bias_applied": round(bias_applied, 2),   # v3.2: rolling MOS-diagnose
                    "naive_baseline": round(naive, 2),
                    "total_points": fc.total_points,
                    "factors": [
                        {"name": fs.name, "points": fs.points} for fs in fc.factors
                    ],
                    "uncertainty_pct": fc.uncertainty_pct,
                    "actual_cat": categorize(actual, thresholds),
                    "predicted_cat": categorize(predicted_val, thresholds),
                    "regime": fc.regime,                             # v1.7
                    "extreme_event_prob": fc.extreme_event_prob,     # v1.7
                })

    print(f"[info] Backtest: {len(results)} datapunten; "
          f"overgeslagen: baseline ontbreekt={skipped_no_baseline}, "
          f"actual ontbreekt={skipped_no_actual}, "
          f"inputs ontbreken={skipped_no_inputs}", file=sys.stderr)
    return results


# ---- Metrics ----

def compute_rank_metrics(results: list[dict]) -> dict:
    """
    v1.7 sectie 10: gebruiksgerichte metrics.

    1. Rank accuracy (Spearman ρ): rangcorrelatie van uur-voor-uur prijsvolgorde
       per dag. Gemiddeld over alle (forecast_date × horizon)-combinaties met ≥12 uren.
       ρ = 1.0 = perfecte rangvolgorde; 0.0 = random; < 0 = omgekeerd.

    2. Cheap-hour hit rate: van de goedkoopste 25% uren per dag (=6 uur bij 24),
       welk percentage valt ook in de voorspelde goedkoopste 25%?
       Dit is de meest directe maatstaf voor gebruikerswaarde (slimme oplaad-adviezen).

    3. Negatieve prijs detectie: precision en recall voor uren waarop de werkelijke
       EPEX-prijs < 0 EUR/MWh. Geeft inzicht in hoe goed het model extreme events herkent.
    """
    # Groepeer per dag (forecast_date + horizon + target_date)
    by_day: dict[tuple, list] = {}
    for r in results:
        key = (r["forecast_date"], r["horizon_days"], r["target_iso"][:10])
        by_day.setdefault(key, []).append(r)

    spearman_vals: list[float] = []
    cheap_hits = 0
    cheap_total = 0
    neg_tp = neg_fp = neg_fn = 0

    for rows in by_day.values():
        if len(rows) < 12:
            continue
        rows_s = sorted(rows, key=lambda x: x["hour"])
        actuals = [r["actual"] for r in rows_s]
        preds   = [r["predicted"] for r in rows_s]
        n = len(rows_s)

        # --- Spearman rangcorrelatie ---
        # Bepaal rang van elk element (laagste waarde = rang 0)
        rank_a = [0] * n
        rank_p = [0] * n
        for rank, idx in enumerate(sorted(range(n), key=lambda i: actuals[i])):
            rank_a[idx] = rank
        for rank, idx in enumerate(sorted(range(n), key=lambda i: preds[i])):
            rank_p[idx] = rank
        d2 = sum((rank_a[i] - rank_p[i]) ** 2 for i in range(n))
        rho = 1.0 - 6.0 * d2 / (n * (n * n - 1))
        spearman_vals.append(rho)

        # --- Cheap-hour hit rate: goedkoopste 25% ---
        k = max(1, n // 4)
        actual_cheap = set(sorted(range(n), key=lambda i: actuals[i])[:k])
        pred_cheap   = set(sorted(range(n), key=lambda i: preds[i])[:k])
        cheap_hits  += len(actual_cheap & pred_cheap)
        cheap_total += k

        # --- Negatieve prijs detectie ---
        for r in rows_s:
            a_neg = r["actual"] < 0
            p_neg = r["predicted"] < 0
            if a_neg and p_neg:
                neg_tp += 1
            elif not a_neg and p_neg:
                neg_fp += 1
            elif a_neg and not p_neg:
                neg_fn += 1

    neg_precision = neg_tp / (neg_tp + neg_fp) if (neg_tp + neg_fp) > 0 else None
    neg_recall    = neg_tp / (neg_tp + neg_fn) if (neg_tp + neg_fn) > 0 else None

    return {
        "spearman_mean":       round(statistics.mean(spearman_vals), 3) if spearman_vals else None,
        "spearman_n_days":     len(spearman_vals),
        "cheap_hour_hit_rate": round(cheap_hits / cheap_total, 3) if cheap_total > 0 else None,
        "cheap_total_slots":   cheap_total,
        "neg_price_precision": round(neg_precision, 3) if neg_precision is not None else None,
        "neg_price_recall":    round(neg_recall, 3) if neg_recall is not None else None,
        "neg_tp": neg_tp, "neg_fp": neg_fp, "neg_fn": neg_fn,
    }


def compute_regime_breakdown(results: list[dict]) -> dict:
    """
    v1.7: verdeling van datapunten over regimes, met MAE per regime.
    Geeft inzicht in welk marktregime het model het best/slechtst presteert.
    """
    by_regime: dict[str, list] = {}
    for r in results:
        reg = r.get("regime", REGIME_NORMAL)
        by_regime.setdefault(reg, []).append(r)

    out = {}
    for reg, rows in by_regime.items():
        abs_errors = [abs(r["predicted"] - r["actual"]) for r in rows]
        errors     = [r["predicted"] - r["actual"] for r in rows]
        out[reg] = {
            "n":    len(rows),
            "mae":  round(statistics.mean(abs_errors), 2) if abs_errors else None,
            "bias": round(statistics.mean(errors), 2)     if errors else None,
        }
    return out


def compute_metrics(results: list[dict]) -> dict:
    """Aggregeer MAE, bias, hit-rate per horizon."""
    by_horizon: dict[int, list[dict]] = {}
    for r in results:
        by_horizon.setdefault(r["horizon_days"], []).append(r)

    metrics_per_horizon: dict[int, dict] = {}
    for h, rows in sorted(by_horizon.items()):
        errors = [r["predicted"] - r["actual"] for r in rows]
        abs_errors = [abs(e) for e in errors]
        naive_errors = [r["naive_baseline"] - r["actual"] for r in rows]
        naive_abs = [abs(e) for e in naive_errors]

        # Hit-rate per categorie
        cats = ["goedkoop", "normaal", "duur"]
        hit_per_cat = {}
        for c in cats:
            actual_in_cat = [r for r in rows if r["actual_cat"] == c]
            if actual_in_cat:
                hit = sum(1 for r in actual_in_cat if r["predicted_cat"] == c)
                hit_per_cat[c] = {
                    "n": len(actual_in_cat),
                    "hit": hit,
                    "rate": hit / len(actual_in_cat),
                }
            else:
                hit_per_cat[c] = {"n": 0, "hit": 0, "rate": None}

        # Overall directional hit (was de richting tov. baseline juist?)
        direction_hits = 0
        direction_total = 0
        for r in rows:
            sign_pred = (r["predicted"] - r["naive_baseline"])
            sign_actual = (r["actual"] - r["naive_baseline"])
            if abs(sign_actual) < 1e-9:
                continue
            direction_total += 1
            if sign_pred * sign_actual > 0:
                direction_hits += 1

        metrics_per_horizon[h] = {
            "n": len(rows),
            "mae": statistics.mean(abs_errors) if abs_errors else None,
            "bias": statistics.mean(errors) if errors else None,
            "rmse": math.sqrt(statistics.mean(e * e for e in errors)) if errors else None,
            "naive_mae": statistics.mean(naive_abs) if naive_abs else None,
            "naive_bias": statistics.mean(naive_errors) if naive_errors else None,
            "improvement_vs_naive_pct": (
                (statistics.mean(naive_abs) - statistics.mean(abs_errors)) / statistics.mean(naive_abs) * 100
                if naive_abs and statistics.mean(naive_abs) > 0 else None
            ),
            "hit_per_cat": hit_per_cat,
            "directional_hit_rate": direction_hits / direction_total if direction_total else None,
        }

    return {
        "total_points":    len(results),
        "per_horizon":     metrics_per_horizon,
        "rank_metrics":    compute_rank_metrics(results),        # v1.7
        "regime_breakdown": compute_regime_breakdown(results),   # v1.7
    }


# ---- Rapport ----

# v3.2: de gebruikte CLI-flags, gezet in main(). Zonder dit was uit een rapport
# niet te herleiden met welke instellingen (bias-mode, scarcity, ...) het draaide.
RUN_SETTINGS: str = ""


def write_report(
    metrics: dict,
    results: list[dict],
    period_start: datetime,
    period_end: datetime,
    source: str,
    config: dict,
    report_path: Path,
    raw_path: Path,
) -> None:
    """Schrijf het backtest-rapport (markdown) en de ruwe data (JSON)."""
    report_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("# Backtest-resultaat v1 - voorspellingsmodel")
    lines.append("")
    lines.append(f"**Datum**: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"**Periode**: {period_start.strftime('%Y-%m-%d')} t/m {period_end.strftime('%Y-%m-%d')}")
    lines.append(f"**Databron**: {source}")
    lines.append(f"**Datapunten**: {metrics['total_points']}")
    if RUN_SETTINGS:
        lines.append(f"**Instellingen**: `{RUN_SETTINGS}`")
    lines.append("")
    lines.append("Dit rapport is automatisch gegenereerd door `02-code/scripts/backtest.py`.")
    lines.append("Het evalueert het 6-puntenmodel uit `forecast.py` retrospectief.")
    lines.append("")
    lines.append("## Beslissingscriteria (uit methodologie sectie 8.4)")
    lines.append("")
    lines.append("- MAE op 1-3 dagen vooruit moet beter zijn dan de naieve baseline (alleen 7d-gemiddelde, geen factoren). Anders dragen de factoren niets bij.")
    lines.append("- Bias dicht bij nul. Structurele afwijking duidt op verkeerde drempels.")
    lines.append("- Hit-rate goedkoop/duur > 65% = klaar voor live; < 55% = terug naar tekentafel.")
    lines.append("")
    lines.append("## Samenvatting per horizon")
    lines.append("")
    lines.append("| Horizon | n | MAE (EUR/MWh) | Bias | Naieve MAE | Verbetering | Goedkoop hit-rate | Duur hit-rate | Richting-hit |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

    def fmt_pct(v):
        return f"{v*100:.0f}%" if v is not None else "-"

    def fmt_signed_pct(v):
        return f"{v:+.1f}%" if v is not None else "-"

    for h in sorted(metrics["per_horizon"].keys()):
        m = metrics["per_horizon"][h]
        if m["mae"] is None:
            lines.append(f"| {h}d | 0 | - | - | - | - | - | - | - |")
            continue
        cheap_rate = m["hit_per_cat"]["goedkoop"]["rate"]
        pricey_rate = m["hit_per_cat"]["duur"]["rate"]
        improv = m["improvement_vs_naive_pct"]
        dir_rate = m["directional_hit_rate"]
        cheap_n = m["hit_per_cat"]["goedkoop"]["n"]
        pricey_n = m["hit_per_cat"]["duur"]["n"]
        lines.append(
            f"| {h}d | {m['n']} | {m['mae']:.2f} | {m['bias']:+.2f} | "
            f"{m['naive_mae']:.2f} | {fmt_signed_pct(improv)} | "
            f"{fmt_pct(cheap_rate)} ({cheap_n}) | "
            f"{fmt_pct(pricey_rate)} ({pricey_n}) | "
            f"{fmt_pct(dir_rate)} |"
        )

    lines.append("")
    lines.append("**Lezen**: \"Verbetering\" is de relatieve daling van MAE t.o.v. de naieve baseline (alleen 7d-gemiddelde, geen factoren). Positief = factoren helpen. \"Richting-hit\" = % van uren waar het model de juiste *richting* van afwijking t.o.v. baseline aangaf (niet de magnitude).")
    lines.append("")
    lines.append("## Categorisatie-drempels (uit config.json)")
    lines.append("")
    th = config["thresholds_eur_per_mwh"]
    lines.append(f"- Goedkoop: < EUR {th['cheap']}/MWh")
    lines.append(f"- Normaal: EUR {th['cheap']} - EUR {th['pricey']}/MWh")
    lines.append(f"- Duur: > EUR {th['pricey']}/MWh")
    lines.append("")

    # Conclusie / aanbeveling
    lines.append("## Conclusie")
    lines.append("")
    h1 = metrics["per_horizon"].get(1)
    h7 = metrics["per_horizon"].get(7)
    if h1 and h1["mae"] is not None:
        improv1 = h1.get("improvement_vs_naive_pct")
        bias1 = h1["bias"]
        cheap_rate = h1["hit_per_cat"]["goedkoop"]["rate"] or 0
        pricey_rate = h1["hit_per_cat"]["duur"]["rate"] or 0

        # Verbetering-check
        if improv1 is None:
            lines.append("- **Naieve baseline-vergelijking**: kon niet worden berekend (geen baseline-data).")
        elif improv1 > 0:
            lines.append(f"- **Factoren leveren waarde** op horizon 1d: MAE is {improv1:.1f}% lager dan naieve baseline.")
        else:
            lines.append(f"- **Factoren verslechteren** op horizon 1d: MAE is {-improv1:.1f}% hoger dan naieve baseline. Drempels of gewichten herzien.")

        # Bias-check
        if abs(bias1) < 5:
            lines.append(f"- **Bias** op 1d ({bias1:+.2f} EUR/MWh) is acceptabel klein.")
        else:
            lines.append(f"- **Bias** op 1d ({bias1:+.2f} EUR/MWh) wijst op systematische over-/onderschatting.")

        # Hit-rate-check
        worst = min(cheap_rate, pricey_rate)
        if worst > 0.65:
            lines.append(f"- **Hit-rate** goedkoop/duur is op 1d >=65% - klaar voor live overweging.")
        elif worst > 0.55:
            lines.append(f"- **Hit-rate** goedkoop/duur ligt tussen 55-65% op 1d - twijfelgeval, factoren tweaken voor live.")
        else:
            lines.append(f"- **Hit-rate** goedkoop/duur < 55% op 1d - terug naar tekentafel volgens criterium.")
    else:
        lines.append("- Geen 1-dagshorizon-data; conclusie kan niet worden getrokken.")

    if h7 and h7["mae"] is not None and h1 and h1["mae"] is not None:
        ratio = h7["mae"] / h1["mae"] if h1["mae"] > 0 else None
        if ratio:
            lines.append(f"- **Schaalverloop** MAE 7d/1d = {ratio:.2f}x - verwacht is een factor 1,5-2,5.")

    lines.append("")

    # --- v1.7: Rank metrics ---
    rm = metrics.get("rank_metrics", {})
    lines.append("## Gebruiksgerichte metrics (v1.7)")
    lines.append("")
    lines.append("### Rank accuracy (Spearman ρ)")
    lines.append("")
    sp = rm.get("spearman_mean")
    sp_n = rm.get("spearman_n_days", 0)
    if sp is not None:
        lines.append(f"Gemiddelde Spearman ρ over {sp_n} dag×horizon-combinaties: **{sp:.3f}**")
        if sp > 0.6:
            lines.append("→ Model sorteert uren redelijk goed van goedkoop naar duur.")
        elif sp > 0.35:
            lines.append("→ Matige rangvolgorde; model heeft een globale richting maar mist details.")
        else:
            lines.append("→ Zwakke rangvolgorde; ruimte voor verbetering in relatieve uur-ranking.")
    else:
        lines.append("Geen data.")
    lines.append("")
    lines.append("### Cheap-hour hit rate (goedkoopste 25% uren)")
    lines.append("")
    chr_val = rm.get("cheap_hour_hit_rate")
    chr_n   = rm.get("cheap_total_slots", 0)
    if chr_val is not None:
        lines.append(f"Van de werkelijk goedkoopste 6 uren per dag zit **{chr_val*100:.0f}%** ook in de voorspelde goedkoopste 6 uren (n={chr_n} slots).")
        if chr_val > 0.60:
            lines.append("→ Bruikbaar voor slimme laadadvies-toepassingen.")
        elif chr_val > 0.40:
            lines.append("→ Redelijk; beter dan random (random ≈ 25%).")
        else:
            lines.append("→ Dicht bij random (25%); model herkent goedkope uren onvoldoende.")
    else:
        lines.append("Geen data.")
    lines.append("")
    lines.append("### Negatieve prijs detectie")
    lines.append("")
    neg_p = rm.get("neg_price_precision")
    neg_r = rm.get("neg_price_recall")
    neg_tp = rm.get("neg_tp", 0)
    neg_fp = rm.get("neg_fp", 0)
    neg_fn = rm.get("neg_fn", 0)
    lines.append(f"TP={neg_tp}, FP={neg_fp}, FN={neg_fn}")
    if neg_p is not None:
        lines.append(f"Precision: **{neg_p*100:.0f}%** | Recall: **{neg_r*100:.0f}%**")
    else:
        lines.append("Geen negatieve prijsuren in testperiode (of model voorspelt nooit negatief).")
    lines.append("")

    # --- v1.7: Regime breakdown ---
    rb = metrics.get("regime_breakdown", {})
    lines.append("## Regime-uitslag (v1.7)")
    lines.append("")
    lines.append("| Regime | n | MAE (EUR/MWh) | Bias |")
    lines.append("|:---|---:|---:|---:|")
    regime_labels = {
        REGIME_NORMAL:     "Normaal",
        REGIME_OVERSUPPLY: "Oversupply (hernieuwbaar)",
        REGIME_SCARCITY:   "Schaarste / Dunkelflaute",
        REGIME_SCARCITY_SUMMER: "Zomerschaarste (windstille hitte)",  # v3.2 #71
        REGIME_TRANSITION: "Transitie",
    }
    for reg in [REGIME_NORMAL, REGIME_OVERSUPPLY, REGIME_SCARCITY,
                REGIME_SCARCITY_SUMMER, REGIME_TRANSITION]:
        rd = rb.get(reg)
        if rd and rd["n"] > 0:
            label = regime_labels.get(reg, reg)
            lines.append(f"| {label} | {rd['n']} | {rd['mae']:.2f} | {rd['bias']:+.2f} |")
    lines.append("")
    lines.append("")

    # v3.2: MAE per uurblok — de avonduren zijn de kritieke zone (#71) en waren
    # eerder alleen uit de ruwe JSON te halen.
    lines.append("## MAE per uurblok")
    lines.append("")
    lines.append("| Uurblok | n | MAE (EUR/MWh) | Bias |")
    lines.append("|:---|---:|---:|---:|")
    uurblokken = [
        ("00-05u nacht",     range(0, 6)),
        ("06-08u ochtend",   range(6, 9)),
        ("09-16u midden",    range(9, 17)),
        ("17-18u vooravond", range(17, 19)),
        ("19-21u avondpiek", range(19, 22)),
        ("22-23u laat",      range(22, 24)),
    ]
    for blok_naam, blok_uren in uurblokken:
        blok_rs = [r for r in results if r["hour"] in blok_uren]
        if not blok_rs:
            continue
        blok_errs = [r["actual"] - r["predicted"] for r in blok_rs]
        blok_mae  = statistics.mean(abs(e) for e in blok_errs)
        blok_bias = statistics.mean(blok_errs)
        lines.append(f"| {blok_naam} | {len(blok_rs)} | {blok_mae:.2f} | {blok_bias:+.2f} |")
    lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append("1. **Perfect-foresight weather**: deze backtest gebruikt de werkelijke gemeten weersgegevens op de targetdag, niet de voorspelde weersdata van het moment van forecast. Dit overschat de modelkwaliteit licht. In productie introduceert weervoorspellingsfout extra variantie. Voor de 7-dagen horizon kan dat substantieel zijn.")
    lines.append("2. **Een locatie per weervariabele**: De Bilt voor zon en temperatuur, idem voor wind. De methodologie noemt drie windlocaties; voor een latere iteratie kan dat verfijnen.")
    lines.append("3. **Seizoennorm zonneproductie**: hardgecodeerde 12-maand-tabel voor De Bilt klimatologie; ruwe interpolatie tussen maandgemiddelden.")
    lines.append("4. **TTF**: een ticker (Yahoo TTF=F front-month), close-to-close. Weekend/feestdagen vullen we vooruit met laatst-bekende koers.")
    lines.append("5. **Sample-modus**: bij gebruik van `--sample` is de evaluatie zelf-circulair (synthetische prijzen vs. dezelfde structuur) en zegt alleen iets over de mechanica, niets over voorspelkracht.")
    lines.append("")

    # Voorstellen voor drempel-aanpassingen
    lines.append("## Voorstellen op basis van metrics")
    lines.append("")
    if h1 and h1.get("bias") is not None:
        if h1["bias"] > 5:
            lines.append("- Bias structureel positief: voorspellingen zitten te hoog. Overweeg POINT_WEIGHT van 4% naar 3% te verlagen, of de positieve drempels van factoren strenger te maken.")
        elif h1["bias"] < -5:
            lines.append("- Bias structureel negatief: voorspellingen zitten te laag. Overweeg POINT_WEIGHT iets te verhogen of negatieve drempels strenger te maken.")
        else:
            lines.append("- Bias is binnen redelijke marge - geen aanleiding tot drempel-tweaking op basis van bias alleen.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*Ruwe datapunten: zie `03-data/backtest-results.json` (of de map opgegeven met --output-dir).*")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[ok] Rapport geschreven: {report_path}", file=sys.stderr)

    # Ruwe JSON
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period_start": period_start.strftime("%Y-%m-%d"),
            "period_end": period_end.strftime("%Y-%m-%d"),
            "source": source,
            "metrics": metrics,
            "results": results,
        }, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"[ok] Ruwe data geschreven: {raw_path}", file=sys.stderr)


# ---- Main ----

def main() -> int:
    parser = argparse.ArgumentParser(description="Retrospectieve evaluatie van het voorspellingsmodel.")
    parser.add_argument("--days", type=int, default=30,
                        help="Aantal dagen testperiode (default 30).")
    parser.add_argument("--horizons", type=str, default="1,3,5,7",
                        help="Comma-gescheiden lijst horizonten in dagen (default 1,3,5,7).")
    parser.add_argument("--sample", action="store_true",
                        help="Gebruik synthetische data - voor mechanica-test zonder API-keys.")
    parser.add_argument("--source", choices=["entsoe", "archive", "sample"], default=None,
                        help="Prijsbron: 'archive' = historisch archief (meerjarig), "
                             "'entsoe' = live ENTSO-E (recent), 'sample' = synthetisch. "
                             "Default: archive als beschikbaar, anders entsoe.")
    parser.add_argument("--end-date", type=str, default=None,
                        help="Anker het einde van de testperiode op YYYY-MM-DD "
                             "(default gisteren). Handig om een specifieke winter/zomer te testen.")
    parser.add_argument("--apply-bias", action="store_true",
                        help="Pas dezelfde MOS bias-correctie toe als de live-site (run_forecast.py). "
                             "Maakt de backtest representatief voor wat bezoekers werkelijk zien. "
                             "Alias voor --bias-mode live.")
    parser.add_argument("--bias-mode", choices=["off", "live", "rolling"], default=None,
                        help="off = geen MOS (default). live = huidige bias_corrections.json "
                             "toepassen (= --apply-bias). rolling = v3.2 walk-forward simulatie: "
                             "recentheidsgewogen correcties per (regime, uur), per run-datum "
                             "herberekend uit alleen dan-bekende fouten (geen look-ahead). "
                             "Voor A/B: zelfde periode met --bias-mode off vs rolling.")
    parser.add_argument("--bias-half-life", type=float, default=5.0,
                        help="Rolling: halfwaardetijd recentheidsgewicht in dagen (default 5). "
                             "Tuning-knop: 2 = agressief volgen, 10 = traag/stabiel.")
    parser.add_argument("--bias-horizon-decay", type=float, default=1.0,
                        help="Rolling: verval van de correctie per horizondag voorbij d2 "
                             "(default 1.0 = geen verval, zoals live MOS vlak toepast). "
                             "Tuning-knop: 0.9 = d7 krijgt ~59%% van de d2-correctie.")
    parser.add_argument("--bias-min-neff", type=float, default=5.0,
                        help="Rolling: minimale effectieve n (som gewichten) per cel (default 5).")
    parser.add_argument("--bias-min-eur", type=float, default=5.0,
                        help="Rolling: minimale absolute bias in EUR/MWh om te corrigeren (default 5).")
    parser.add_argument("--bias-cap", type=float, default=50.0,
                        help="Rolling: maximale toegepaste correctie in EUR/MWh (default 50, = live cap).")
    parser.add_argument("--seasonal", action="store_true",
                        help="Zet de experimentele seizoensfactor aan (archief van voorgaande jaren). "
                             "Voor A/B: draai dezelfde periode met en zonder deze vlag.")
    parser.add_argument("--seasonal-years", type=int, default=3,
                        help="Aantal voorgaande jaren voor de seizoensbaseline (default 3).")
    parser.add_argument("--seasonal-window", type=int, default=10,
                        help="Venster in dagen rond dezelfde kalenderdatum (default 10).")
    parser.add_argument("--scarcity", action="store_true",
                        help="Zet de experimentele schaarste-amplifier (v3.1) aan: "
                             "niet-lineaire opwaartse correctie tijdens Dunkelflaute "
                             "(REGIME_SCARCITY). Voor A/B: draai dezelfde periode met en "
                             "zonder deze vlag en vergelijk de bias in het regime-overzicht.")
    parser.add_argument("--scarcity-scale", type=float, default=1.5,
                        help="Globale schaal op de schaarste-amplifier (default 1.5 = live). "
                             "Tuning-knop: 0.5 = halve correctie, 1.5 = sterker. Alleen "
                             "actief samen met --scarcity.")
    parser.add_argument("--summer-scarcity", action="store_true",
                        help="Zet het experimentele zomer-schaarste-regime + amplifier "
                             "(v3.2, #71) aan: windstille hitte tijdens de avondramp "
                             "(18-22u) wordt als schaars herkend en omhoog gecorrigeerd. "
                             "Voor A/B: draai dezelfde periode met en zonder deze vlag en "
                             "vergelijk 'Zomerschaarste' in het regime-overzicht plus de "
                             "avonduren-MAE.")
    parser.add_argument("--summer-scarcity-scale", type=float, default=1.0,
                        help="Globale schaal op de zomerschaarste-amplifier (default 1.0). "
                             "Alleen actief samen met --summer-scarcity.")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Schrijf rapport en JSON naar deze map (handig in CI). "
                             "Default: rapport naar 01-documenten/, JSON naar 03-data/.")
    args = parser.parse_args()

    # v3.2: instellingen-echo in het rapport (herleidbaarheid van A/B-runs)
    global RUN_SETTINGS
    RUN_SETTINGS = " ".join(sys.argv[1:]) or "(defaults)"

    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    test_days = args.days

    if args.output_dir:
        outdir = Path(args.output_dir)
        report_path = outdir / "backtest-resultaat-v1.md"
        raw_path = outdir / "backtest-results.json"
    else:
        report_path = DEFAULT_REPORT_FILE
        raw_path = DEFAULT_RAW_FILE

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    thresholds = derive_eur_mwh_thresholds(config)
    # Zorg dat het rapport (dat config["thresholds_eur_per_mwh"] leest) dezelfde waarden toont.
    config["thresholds_eur_per_mwh"] = thresholds
    print(f"[info] Categorisatie-drempels (EUR/MWh): goedkoop<{thresholds['cheap']}, "
          f"duur>{thresholds['pricey']}.", file=sys.stderr)

    # v3.2: --bias-mode bepaalt de MOS-variant; --apply-bias blijft werken als
    # alias voor 'live'. Default (geen van beide): off.
    bias_mode = args.bias_mode or ("live" if getattr(args, "apply_bias", False) else "off")

    bias_corrections: dict = {}
    if bias_mode == "live":
        if not _HAS_BIAS:
            print("[warn] --bias-mode live gevraagd, maar run_forecast-bias-functies niet "
                  "beschikbaar; backtest draait zonder bias-correctie.", file=sys.stderr)
        else:
            try:
                bias_corrections = _rf_load_bias(_RF_BIAS_FILE)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] bias-correcties laden mislukt: {exc}", file=sys.stderr)
            n_active = sum(1 for v in bias_corrections.values() if v.get("apply"))
            print(f"[info] MOS bias-correctie AAN: {len(bias_corrections)} cellen, "
                  f"{n_active} actief.", file=sys.stderr)

    rolling_bias: dict | None = None
    if bias_mode == "rolling":
        rolling_bias = {
            "half_life":     args.bias_half_life,
            "horizon_decay": args.bias_horizon_decay,
            "min_neff":      args.bias_min_neff,
            "min_eur":       args.bias_min_eur,
            "cap":           args.bias_cap,
        }
        print(f"[info] Rolling MOS AAN (walk-forward): half-life={args.bias_half_life}d, "
              f"horizon-decay={args.bias_horizon_decay}, min-neff={args.bias_min_neff}, "
              f"min-eur={args.bias_min_eur}, cap={args.bias_cap}.", file=sys.stderr)

    seasonal_fn = None
    if getattr(args, "seasonal", False):
        if _archive_same_period is None:
            print("[warn] --seasonal gevraagd, maar load_archive niet beschikbaar; "
                  "seizoensfactor blijft uit.", file=sys.stderr)
        else:
            import forecast as _fc_mod
            _fc_mod.ENABLED_FACTORS.add("seizoen")
            _seasonal_cache: dict = {}

            def seasonal_fn(target_day, _yb=args.seasonal_years, _wd=args.seasonal_window):
                key = target_day.strftime("%Y-%m-%d")
                if key not in _seasonal_cache:
                    _seasonal_cache[key] = _archive_same_period(
                        target_day, years_back=_yb, window_days=_wd)
                return _seasonal_cache[key]

            print(f"[info] Seizoensfactor AAN: {args.seasonal_years} jaar terug, "
                  f"+/-{args.seasonal_window} dagen venster.", file=sys.stderr)

    # v3.1: schaarste-amplifier (mirror van de oversupply-correctie, omhoog).
    # Strikt gated op REGIME_SCARCITY; raakt het normaal-regime per definitie niet.
    if getattr(args, "scarcity", False):
        import forecast as _fc_mod
        _fc_mod.ENABLED_FACTORS.add("scarcity")
        _fc_mod.SCARCITY_SCALE = args.scarcity_scale
        print(f"[info] Schaarste-amplifier AAN (scale={args.scarcity_scale}). "
              f"A/B: vergelijk de bias bij 'Schaarste / Dunkelflaute' in het "
              f"regime-overzicht met de run zonder --scarcity.", file=sys.stderr)

    # v3.2 (#71): zomer-schaarste-regime + amplifier. Zet zowel de regime-detectie
    # als de factor aan; strikt gated, raakt het normaal-regime per definitie niet.
    if getattr(args, "summer_scarcity", False):
        import forecast as _fc_mod
        _fc_mod.ENABLE_SUMMER_SCARCITY_REGIME = True
        _fc_mod.ENABLED_FACTORS.add("zomerschaarste")
        _fc_mod.SUMMER_SCARCITY_SCALE = args.summer_scarcity_scale
        print(f"[info] Zomerschaarste-amplifier AAN (scale={args.summer_scarcity_scale}). "
              f"A/B: vergelijk 'Zomerschaarste' in het regime-overzicht en de "
              f"avonduren-MAE met de run zonder --summer-scarcity.", file=sys.stderr)

    now = amsterdam_now()
    # Periode: testperiode = laatste `test_days` dagen, eindigend gisteren.
    # We hebben prijzen nodig vanaf (test_start - 7d) zodat de eerste forecast_date
    # 7 dagen baseline-history heeft. En het laatste target_day ligt op test_end + max(horizons).
    if args.end_date:
        test_end_day = datetime.fromisoformat(args.end_date).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=now.tzinfo)
    else:
        test_end_day = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    test_start_day = test_end_day - timedelta(days=test_days - 1)

    # Prijzen: ENTSO-E publiceert day-ahead, dus we kunnen tot test_end_day + 1 vragen
    # (dat is "vandaag" in onze context — soms al beschikbaar). Een grotere range veroorzaakt
    # geen errors maar levert ook geen extra data.
    # Weer/TTF: het Open-Meteo archive endpoint geeft HTTP 400 bij toekomstige datums en loopt
    # typisch 2-5 dagen achter op realtime. We cappen daarom op test_end_day. Forecast-targets
    # voorbij test_end_day worden later toch al overgeslagen via lookup_actual().
    # 14 dagen pre-history zodat de v1.4 weekend-baseline (14d window) genoeg
    # datapunten heeft voor de eerste forecast-dagen in de testperiode.
    fetch_prices_from = test_start_day - timedelta(days=14)
    _max_h = max(horizons) if horizons else 7
    fetch_prices_to = test_end_day + timedelta(days=_max_h + 1)
    # Bij een historische testperiode liggen de target-dagen (test_end + horizon) ook in het
    # verleden; haal weer/TTF dan t/m die laatste target-dag op, gecapt op gisteren omdat het
    # Open-Meteo archive enkele dagen achterloopt op realtime.
    _yesterday0 = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    weather_end_day = min(test_end_day + timedelta(days=_max_h), _yesterday0)
    ttf_end_day = min(test_end_day + timedelta(days=_max_h), _yesterday0)

    print(f"[info] Testperiode: {test_start_day.date()} t/m {test_end_day.date()} ({test_days} dagen)",
          file=sys.stderr)
    print(f"[info] Horizons: {horizons}", file=sys.stderr)
    print(f"[info] Output: rapport={report_path} raw={raw_path}", file=sys.stderr)
    print(f"[info] Prijshistorie nodig: {fetch_prices_from.date()} t/m {fetch_prices_to.date()}",
          file=sys.stderr)

    token = os.environ.get("ENTSOE_TOKEN", "").strip()

    # Kies prijsbron. Default: archief als beschikbaar (meerjarig, reproduceerbaar),
    # anders live ENTSO-E. --source overschrijft; --sample blijft werken.
    if args.source:
        source_mode = args.source
    elif args.sample:
        source_mode = "sample"
    elif _archive_load_range is not None:
        source_mode = "archive"
    else:
        source_mode = "entsoe"
    if source_mode == "entsoe" and not token:
        print("[warn] Geen ENTSOE_TOKEN voor entsoe-bron - val terug op sample-modus.",
              file=sys.stderr)
        source_mode = "sample"

    if source_mode == "sample":
        if not token and not args.sample:
            print("[warn] Geen ENTSOE_TOKEN - val terug op sample-modus. "
                  "Resultaten zeggen alleen iets over mechanica.", file=sys.stderr)
        prices = synth_prices(fetch_prices_from, days=(fetch_prices_to - fetch_prices_from).days + 1)
        all_days = date_range(fetch_prices_from, (fetch_prices_to - fetch_prices_from).days + 1)
        all_days_ttf = date_range(fetch_prices_from - timedelta(days=30),
                                  (fetch_prices_to - fetch_prices_from).days + 31)
        weather = synth_weather(all_days)
        ttf = synth_ttf(all_days_ttf)
        source = "sample (synthetisch)"
    else:
        if source_mode == "archive":
            print("[info] Prijzen uit historisch archief laden...", file=sys.stderr)
            prices = load_prices_from_archive(fetch_prices_from, fetch_prices_to)
            price_label = "Archief"
        else:
            print("[info] ENTSO-E historie ophalen...", file=sys.stderr)
            prices = fetch_entsoe_range(token, fetch_prices_from, fetch_prices_to)
            print(f"[info] {len(prices)} prijspunten verkregen.", file=sys.stderr)
            price_label = "ENTSO-E"
        if not prices:
            print("[err] Geen prijsdata; afbreken.", file=sys.stderr)
            return 1

        print("[info] Open-Meteo historische weerdata ophalen...", file=sys.stderr)
        weather_start = fetch_prices_from.strftime("%Y-%m-%d")
        weather_end = weather_end_day.strftime("%Y-%m-%d")
        try:
            weather = fetch_open_meteo_archive(weather_start, weather_end)
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            # Archive lagt soms verder dan 1 dag; retry met end - 5d
            fallback_end = (weather_end_day - timedelta(days=5)).strftime("%Y-%m-%d")
            print(f"[warn] Open-Meteo {exc}; retry met end={fallback_end}", file=sys.stderr)
            try:
                weather = fetch_open_meteo_archive(weather_start, fallback_end)
            except (urllib.error.URLError, urllib.error.HTTPError) as exc2:
                print(f"[err] Open-Meteo blijft falen: {exc2}; afbreken.", file=sys.stderr)
                return 1
        print(f"[info] {len(weather)} dagen weerdata.", file=sys.stderr)

        print("[info] Yahoo Finance TTF historie ophalen...", file=sys.stderr)
        ttf_start = fetch_prices_from - timedelta(days=35)
        ttf_end = ttf_end_day
        try:
            ttf = fetch_yahoo_ttf(ttf_start, ttf_end)
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"[warn] Yahoo Finance fout: {exc}; geen TTF-data - backtest stopt.", file=sys.stderr)
            return 1
        print(f"[info] {len(ttf)} TTF-koersen.", file=sys.stderr)
        source = f"{price_label} + Open-Meteo Archive + Yahoo Finance TTF=F"

    if bias_corrections:
        source = source + " + MOS bias-correctie"
    if seasonal_fn:
        source = source + f" + seizoensfactor (Nj={args.seasonal_years}, w={args.seasonal_window})"
    if getattr(args, "scarcity", False):
        source = source + f" + schaarste-amplifier (scale={args.scarcity_scale})"

    # Forecast-dates: elke dag in de testperiode
    forecast_dates = [test_start_day + timedelta(days=i) for i in range(test_days)]

    print(f"[info] Forecasts uitvoeren ({len(forecast_dates)} forecast-dagen x {len(horizons)} horizons x 24 uur)...",
          file=sys.stderr)
    results = run_backtest(prices, weather, ttf, forecast_dates, horizons, thresholds, bias_corrections=bias_corrections, seasonal_fn=seasonal_fn, rolling_bias=rolling_bias)

    if not results:
        print("[err] Geen resultaten; rapport wordt niet geschreven.", file=sys.stderr)
        return 1

    metrics = compute_metrics(results)

    # Korte samenvatting
    print("\n=== Backtest samenvatting ===", file=sys.stderr)
    print(f"Periode: {test_start_day.date()} t/m {test_end_day.date()}", file=sys.stderr)
    print(f"Bron: {source}", file=sys.stderr)
    print(f"Datapunten: {metrics['total_points']}", file=sys.stderr)
    for h, m in sorted(metrics["per_horizon"].items()):
        if m["mae"] is None:
            continue
        cheap = m["hit_per_cat"]["goedkoop"]
        pricey = m["hit_per_cat"]["duur"]
        cheap_str = f"{cheap['rate']*100:.0f}%" if cheap["rate"] is not None else "n/a"
        pricey_str = f"{pricey['rate']*100:.0f}%" if pricey["rate"] is not None else "n/a"
        improv = m.get("improvement_vs_naive_pct")
        improv_str = f"{improv:+.1f}%" if improv is not None else "n/a"
        print(f"  {h}d: MAE={m['mae']:6.2f}  bias={m['bias']:+6.2f}  "
              f"vs naive={improv_str}  goedkoop={cheap_str}  duur={pricey_str}",
              file=sys.stderr)

    write_report(metrics, results, test_start_day, test_end_day, source, config,
                 report_path=report_path, raw_path=raw_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
