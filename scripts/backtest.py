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
) -> list[dict]:
    """Voor elke forecast_date x horizon x uur: forecast vs actual.

    Een forecast_date representeert de "vandaag waarop we voorspellen".
    De target is forecast_date + horizon dagen, en we voorspellen alle 24 uren ervan.
    """
    results: list[dict] = []
    skipped_no_baseline = 0
    skipped_no_actual = 0
    skipped_no_inputs = 0

    for fc_date in forecast_dates:
        # History = alles voor fc_date 23:59 (dwz. dag fc_date is bekend)
        cutoff = fc_date.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        history_full = slice_history_until(prices, cutoff)

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
                )
                if fc is None:
                    skipped_no_baseline += 1
                    continue

                # Naieve baseline (geen factoren) = compute_baseline output zonder punten
                naive = fc.baseline

                results.append({
                    "forecast_date": fc_date.strftime("%Y-%m-%d"),
                    "target_iso": target_dt.isoformat(),
                    "horizon_days": h,
                    "hour": hour,
                    "weekday": target_dt.weekday(),
                    "is_feestdag": is_feestdag(target_dt),
                    "actual": round(actual, 2),
                    "predicted": fc.predicted,
                    "naive_baseline": round(naive, 2),
                    "total_points": fc.total_points,
                    "factors": [
                        {"name": fs.name, "points": fs.points} for fs in fc.factors
                    ],
                    "uncertainty_pct": fc.uncertainty_pct,
                    "actual_cat": categorize(actual, thresholds),
                    "predicted_cat": categorize(fc.predicted, thresholds),
                })

    print(f"[info] Backtest: {len(results)} datapunten; "
          f"overgeslagen: baseline ontbreekt={skipped_no_baseline}, "
          f"actual ontbreekt={skipped_no_actual}, "
          f"inputs ontbreken={skipped_no_inputs}", file=sys.stderr)
    return results


# ---- Metrics ----

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
        "total_points": len(results),
        "per_horizon": metrics_per_horizon,
    }


# ---- Rapport ----

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
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Schrijf rapport en JSON naar deze map (handig in CI). "
                             "Default: rapport naar 01-documenten/, JSON naar 03-data/.")
    args = parser.parse_args()

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
    thresholds = config["thresholds_eur_per_mwh"]

    now = amsterdam_now()
    # Periode: testperiode = laatste `test_days` dagen, eindigend gisteren.
    # We hebben prijzen nodig vanaf (test_start - 7d) zodat de eerste forecast_date
    # 7 dagen baseline-history heeft. En het laatste target_day ligt op test_end + max(horizons).
    test_end_day = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    test_start_day = test_end_day - timedelta(days=test_days - 1)

    # Prijzen: ENTSO-E publiceert day-ahead, dus we kunnen tot test_end_day + 1 vragen
    # (dat is "vandaag" in onze context — soms al beschikbaar). Een grotere range veroorzaakt
    # geen errors maar levert ook geen extra data.
    # Weer/TTF: het Open-Meteo archive endpoint geeft HTTP 400 bij toekomstige datums en loopt
    # typisch 2-5 dagen achter op realtime. We cappen daarom op test_end_day. Forecast-targets
    # voorbij test_end_day worden later toch al overgeslagen via lookup_actual().
    fetch_prices_from = test_start_day - timedelta(days=7)
    fetch_prices_to = test_end_day + timedelta(days=2)
    weather_end_day = test_end_day
    ttf_end_day = test_end_day

    print(f"[info] Testperiode: {test_start_day.date()} t/m {test_end_day.date()} ({test_days} dagen)",
          file=sys.stderr)
    print(f"[info] Horizons: {horizons}", file=sys.stderr)
    print(f"[info] Output: rapport={report_path} raw={raw_path}", file=sys.stderr)
    print(f"[info] Prijshistorie nodig: {fetch_prices_from.date()} t/m {fetch_prices_to.date()}",
          file=sys.stderr)

    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    use_sample = args.sample or not token

    if use_sample:
        if not token and not args.sample:
            print("[warn] Geen ENTSOE_TOKEN - val terug op sample-modus. "
                  "Resultaten zeggen alleen iets over mechanica.", file=sys.stderr)
        prices = synth_prices(fetch_prices_from, days=(fetch_prices_to - fetch_prices_from).days + 1)
        all_days = date_range(fetch_prices_from, (fetch_prices_to - fetch_prices_from).days + 1)
        # TTF: 30 dagen voor het oudste forecast-moment ophalen
        all_days_ttf = date_range(fetch_prices_from - timedelta(days=30),
                                  (fetch_prices_to - fetch_prices_from).days + 31)
        weather = synth_weather(all_days)
        ttf = synth_ttf(all_days_ttf)
        source = "sample (synthetisch)"
    else:
        # Echte data
        print("[info] ENTSO-E historie ophalen...", file=sys.stderr)
        prices = fetch_entsoe_range(token, fetch_prices_from, fetch_prices_to)
        print(f"[info] {len(prices)} prijspunten verkregen.", file=sys.stderr)
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
        source = "ENTSO-E + Open-Meteo Archive + Yahoo Finance TTF=F"

    # Forecast-dates: elke dag in de testperiode
    forecast_dates = [test_start_day + timedelta(days=i) for i in range(test_days)]

    print(f"[info] Forecasts uitvoeren ({len(forecast_dates)} forecast-dagen x {len(horizons)} horizons x 24 uur)...",
          file=sys.stderr)
    results = run_backtest(prices, weather, ttf, forecast_dates, horizons, thresholds)

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
