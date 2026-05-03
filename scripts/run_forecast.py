"""
run_forecast.py — Genereert public/data/forecast.json voor de live site.

Wordt aangeroepen door GitHub Actions na `fetch_prices.py` in dezelfde 3u-cron.

Pijplijn:
1. Lees `public/data/prices.json` voor history (minimaal 14 dagen nodig).
2. Fetch Open-Meteo forecast voor De Bilt voor de komende 7 dagen.
3. Fetch Yahoo Finance TTF=F voor laatste ~35 dagen → current TTF + 30d gemiddelde.
4. Voor elk uur van "overmorgen 00:00" t/m "+7 dagen 23:00" Amsterdam roep
   `forecast_one()` aan met de juiste inputs.
5. Schrijf `public/data/forecast.json` met de resultaten.

Faalmodi:
- Geen prices.json beschikbaar → exit 1 (zonder history geen baseline).
- Open-Meteo niet beschikbaar → schrijf forecast.json met `error` veld en
  lege `forecasts` lijst, zodat de frontend kan tonen "voorspelling tijdelijk
  niet beschikbaar".
- Yahoo TTF niet beschikbaar → val terug op `ttf_ratio = 1.0` (neutraal,
  factor_gas geeft 0). De andere factoren blijven werken.

Output-format:
{
  "generated_at": ISO-timestamp,
  "currency": "EUR",
  "unit": "EUR/MWh",
  "tz": "Europe/Amsterdam",
  "model_version": "1.6",
  "horizon_start": ISO-timestamp,
  "horizon_end": ISO-timestamp,
  "forecasts": [
    {"time": ISO, "baseline": 25.40, "predicted": 27.43, "lower": 21.40,
     "upper": 33.46, "uncertainty_pct": 0.22, "total_points": 4,
     "days_ahead": 2,
     "factors": [{"name": "zon", "points": -3, "reason": "..."}, ...]},
    ...
  ]
}
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Importeer uit forecast.py en fetch_prices.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
from forecast import forecast_one, POINT_WEIGHT  # noqa: E402
from fetch_prices import amsterdam_now  # noqa: E402

# Modelversie — komt mee in de output zodat de frontend hem kan tonen.
MODEL_VERSION = "1.8"

# Paden
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRICES_FILE = PROJECT_ROOT / "public" / "data" / "prices.json"
FORECAST_FILE = PROJECT_ROOT / "public" / "data" / "forecast.json"

# Externe endpoints
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
YAHOO_TTF = "https://query1.finance.yahoo.com/v8/finance/chart/TTF=F"

# Locatie De Bilt
DEBILT_LAT = 52.10
DEBILT_LON = 5.18

# Seizoennorm zonneproductie (zelfde als backtest.py — KNMI klimatologie)
MONTHLY_SOLAR_NORM_MJ = {
    1: 2.5, 2: 5.0, 3: 9.0, 4: 14.0, 5: 17.5, 6: 18.5,
    7: 18.0, 8: 15.5, 9: 11.0, 10: 6.5, 11: 3.0, 12: 2.0,
}


def seasonal_solar_norm_mj(dt: datetime) -> float:
    """Seizoennorm met lineaire interpolatie tussen maandgemiddelden."""
    m = dt.month
    d = dt.day
    if d <= 15:
        prev_m = 12 if m == 1 else m - 1
        frac = (d + 15) / 30
        return MONTHLY_SOLAR_NORM_MJ[prev_m] * (1 - frac) + MONTHLY_SOLAR_NORM_MJ[m] * frac
    next_m = 1 if m == 12 else m + 1
    frac = (d - 15) / 30
    return MONTHLY_SOLAR_NORM_MJ[m] * (1 - frac) + MONTHLY_SOLAR_NORM_MJ[next_m] * frac


def fetch_open_meteo_forecast(forecast_days: int = 7) -> dict[str, dict]:
    """Haal weersvoorspelling op voor De Bilt voor de komende `forecast_days` dagen.

    Return: {YYYY-MM-DD: {shortwave_mj, wind_ms (op 100m), temp_c}}
    """
    params = {
        "latitude": DEBILT_LAT,
        "longitude": DEBILT_LON,
        "daily": ",".join([
            "shortwave_radiation_sum",
            "wind_speed_10m_max",
            "wind_speed_10m_mean",
            "temperature_2m_mean",
        ]),
        "wind_speed_unit": "ms",
        "timezone": "Europe/Amsterdam",
        "forecast_days": forecast_days,
    }
    url = f"{OPEN_METEO_FORECAST}?{urllib.parse.urlencode(params)}"
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
        w_ms_10m = None
        if i < len(wmean) and wmean[i] is not None:
            w_ms_10m = float(wmean[i])
        elif i < len(wmax) and wmax[i] is not None:
            w_ms_10m = 0.7 * float(wmax[i])
        # Schaal 10m → 100m met machtswet (alpha=0.14)
        w_ms_100m = w_ms_10m * 1.38 if w_ms_10m is not None else None

        out[day] = {
            "shortwave_mj": float(sw[i]) if i < len(sw) and sw[i] is not None else None,
            "wind_ms": w_ms_100m,
            "temp_c": float(tmean[i]) if i < len(tmean) and tmean[i] is not None else None,
        }
    return out


def fetch_yahoo_ttf(days_back: int = 35) -> dict[str, float]:
    """Haal dagelijkse TTF=F closes op voor de laatste `days_back` dagen.

    Return: {YYYY-MM-DD: close in EUR/MWh}, of {} bij fout.
    """
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=days_back)
    params = {
        "period1": int(start.timestamp()),
        "period2": int(end.timestamp()),
        "interval": "1d",
        "events": "history",
    }
    url = f"{YAHOO_TTF}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; stroomvoorspeller/0.1)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"[warn] Yahoo TTF fout: {exc}", file=sys.stderr)
        return {}

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


def compute_ttf_ratio(ttf_series: dict[str, float]) -> float:
    """Bereken huidige TTF / 30d gemiddelde. 1.0 als data ontbreekt (neutraal)."""
    if not ttf_series:
        return 1.0
    sorted_days = sorted(ttf_series.keys())
    if not sorted_days:
        return 1.0
    current = ttf_series[sorted_days[-1]]
    history_30 = [ttf_series[d] for d in sorted_days[:-1][-30:]]
    if not history_30:
        return 1.0
    avg = sum(history_30) / len(history_30)
    if avg == 0:
        return 1.0
    return current / avg


def main() -> int:
    if not PRICES_FILE.exists():
        print(f"[err] {PRICES_FILE} ontbreekt; draai eerst fetch_prices.py.", file=sys.stderr)
        return 1

    prices_payload = json.loads(PRICES_FILE.read_text(encoding="utf-8"))
    history = prices_payload.get("prices", [])
    if not history:
        print("[err] prices.json bevat geen prijzen.", file=sys.stderr)
        return 1
    print(f"[info] {len(history)} prijzen ingelezen uit prices.json", file=sys.stderr)

    now_ams = amsterdam_now()
    today_start = now_ams.replace(hour=0, minute=0, second=0, microsecond=0)
    # Day-ahead dekt vandaag + morgen via fetch_prices.py.
    # Voorspelling start vanaf "overmorgen 00:00" tot "+7 dagen 23:00".
    horizon_start = today_start + timedelta(days=2)
    horizon_end = today_start + timedelta(days=8)  # exclusive

    # Open-Meteo: vraag forecast voor de komende 8 dagen (today + 7).
    print("[info] Open-Meteo forecast ophalen...", file=sys.stderr)
    try:
        weather = fetch_open_meteo_forecast(forecast_days=8)
        print(f"[info] {len(weather)} dagen weersvoorspelling.", file=sys.stderr)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError) as exc:
        print(f"[err] Open-Meteo fout: {exc}", file=sys.stderr)
        weather = {}

    # Yahoo TTF
    print("[info] Yahoo TTF historie ophalen...", file=sys.stderr)
    ttf_series = fetch_yahoo_ttf(days_back=35)
    ttf_ratio = compute_ttf_ratio(ttf_series)
    print(f"[info] TTF ratio (current/30d): {ttf_ratio:.3f}", file=sys.stderr)

    # v1.8: bouw een snelle ISO-string → prijs lookup op basis van prices.json.
    # Wordt gebruikt om de vorige-dag-prijs per uur door te geven aan forecast_one().
    # Sleutelformaat: "YYYY-MM-DDTHH:00:00" (zonder timezone-suffix, zoals history).
    prices_by_iso: dict[str, float] = {}
    for entry in history:
        t_str = entry.get("time", "")
        # Normaliseer: strip eventuele timezone-suffix en seconden zodat het format
        # consistent is met target_dt.isoformat() dat we later aanmaken.
        try:
            t_norm = datetime.fromisoformat(t_str).replace(
                minute=0, second=0, microsecond=0, tzinfo=None
            ).isoformat()
            prices_by_iso[t_norm] = float(entry["price"])
        except (ValueError, KeyError):
            continue
    print(f"[info] {len(prices_by_iso)} uurprijzen geïndexeerd voor prior-day lookup.",
          file=sys.stderr)

    forecasts: list[dict] = []
    skipped = 0
    cursor = horizon_start
    while cursor < horizon_end:
        day_key = cursor.strftime("%Y-%m-%d")
        wx = weather.get(day_key)
        if not wx or any(wx.get(k) is None for k in ("shortwave_mj", "wind_ms", "temp_c")):
            skipped += 24
            cursor += timedelta(hours=24)
            continue

        sw_ratio = wx["shortwave_mj"] / seasonal_solar_norm_mj(cursor)
        wind = wx["wind_ms"]
        temp = wx["temp_c"]
        days_ahead = (cursor.replace(hour=0) - today_start).days

        for hour in range(24):
            target_dt = cursor.replace(hour=hour, minute=0, second=0, microsecond=0)

            # v1.8: vorige-dag-prijs opzoeken voor hetzelfde uur op D-1.
            # Alleen meegeven als die prijs daadwerkelijk in prices.json staat
            # (d.w.z. gepubliceerde day-ahead data). Voor D+3 en verder is die
            # prijs nog niet bekend en geeft factor_vorige_dag() 0 terug.
            prior_dt = target_dt - timedelta(days=1)
            prior_day_price = prices_by_iso.get(prior_dt.isoformat())

            fc = forecast_one(
                target_dt=target_dt,
                history=history,
                shortwave_ratio=sw_ratio,
                wind_ms=wind,
                temp_c=temp,
                ttf_ratio=ttf_ratio,
                days_ahead=days_ahead,
                prior_day_price=prior_day_price,
            )
            if fc is None:
                skipped += 1
                continue

            forecasts.append({
                "time": target_dt.isoformat(),
                "baseline": fc.baseline,
                "predicted": fc.predicted,
                "lower": round(fc.lower, 2),
                "upper": round(fc.upper, 2),
                "uncertainty_pct": fc.uncertainty_pct,
                "total_points": fc.total_points,
                "days_ahead": fc.days_ahead,
                "factors": [
                    {"name": fs.name, "points": fs.points, "reason": fs.reason}
                    for fs in fc.factors
                ],
            })

        cursor += timedelta(hours=24)

    print(f"[info] {len(forecasts)} voorspellingen gegenereerd; {skipped} overgeslagen.",
          file=sys.stderr)

    payload: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "currency": "EUR",
        "unit": "EUR/MWh",
        "tz": "Europe/Amsterdam",
        "model_version": MODEL_VERSION,
        "horizon_start": horizon_start.isoformat(),
        "horizon_end": (horizon_end - timedelta(seconds=1)).isoformat(),
        "forecasts": forecasts,
    }
    if not weather:
        payload["error"] = "Open-Meteo niet beschikbaar; geen voorspelling deze run."
    elif not forecasts:
        payload["error"] = "Geen voorspellingen kunnen genereren (insufficient history?)"

    FORECAST_FILE.parent.mkdir(parents=True, exist_ok=True)
    FORECAST_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[ok] Geschreven: {FORECAST_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
