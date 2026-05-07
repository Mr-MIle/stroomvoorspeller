"""
run_forecast.py — Genereert public/data/forecast.json voor de live site.

Wordt aangeroepen door GitHub Actions na `fetch_prices.py` in dezelfde 3u-cron.

Pijplijn:
1. Lees `public/data/prices.json` voor history (minimaal 14 dagen nodig).
2. Fetch Open-Meteo forecast voor De Bilt voor de komende 7 dagen.
3. Fetch Yahoo Finance TTF=F voor laatste ~35 dagen -> current TTF + 30d gemiddelde.
4. Voor elk uur van "morgen 00:00" t/m "+7 dagen 23:00" Amsterdam roep
   `forecast_one()` aan met de juiste inputs.
5. Voer EVENT_PLAUSIBILITY_LAYER uit per uur (v2.1).
6. Schrijf `public/data/forecast.json` met de resultaten.

Faalmodi:
- Geen prices.json beschikbaar -> exit 1 (zonder history geen baseline).
- Open-Meteo niet beschikbaar -> schrijf forecast.json met `error` veld en
  lege `forecasts` lijst, zodat de frontend kan tonen "voorspelling tijdelijk
  niet beschikbaar".
- Yahoo TTF niet beschikbaar -> val terug op `ttf_ratio = 1.0` (neutraal,
  factor_gas geeft 0). De andere factoren blijven werken.

Output-format (v2.1):
{
  "generated_at": ISO-timestamp,
  "currency": "EUR",
  "unit": "EUR/MWh",
  "tz": "Europe/Amsterdam",
  "model_version": "2.1",
  "horizon_start": ISO-timestamp,
  "horizon_end": ISO-timestamp,
  "forecasts": [
    {"time": ISO, "baseline": 25.40, "predicted": 27.43, "lower": 21.40,
     "upper": 33.46, "uncertainty_pct": 0.22, "total_points": 4,
     "days_ahead": 2, "regime": "normaal",
     "sw_ratio_h": 2.15, "sw_ratio_daily": 1.82,
     "wind_ms": 7.5, "temp_c": 14.2, "P_negative": 0.0,
     "event_plausibility_score": 0.52, "event_plausibility_label": "NORMAL",
     "analog_sample_size": 7,
     "factors": [{"name": "zon", "points": -3, "reason": "..."}, ...]},
    ...
  ]
}
"""

from __future__ import annotations

import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Importeer modelmodules
sys.path.insert(0, str(Path(__file__).resolve().parent))
from forecast import forecast_one, POINT_WEIGHT          # noqa: E402
from fetch_prices import amsterdam_now                   # noqa: E402
from event_plausibility import compute_event_plausibility  # noqa: E402  (v2.1)

# Modelversie — komt mee in de output zodat de frontend hem kan tonen.
# v2.1: EVENT_PLAUSIBILITY_LAYER toegevoegd als post-processing stap.
MODEL_VERSION = "2.1"

# ---------------------------------------------------------------------------
# Paden
# ---------------------------------------------------------------------------
PROJECT_ROOT       = Path(__file__).resolve().parent.parent
PRICES_FILE        = PROJECT_ROOT / "public" / "data" / "prices.json"
FORECAST_FILE      = PROJECT_ROOT / "public" / "data" / "forecast.json"
PREDICTION_LOG_FILE = PROJECT_ROOT / "03-data" / "prediction_log.json"

# Hoeveel dagen we prediction-log bewaren (voor bias-correctie en analog search)
PREDICTION_LOG_MAX_DAYS = 90

# ---------------------------------------------------------------------------
# Externe endpoints
# ---------------------------------------------------------------------------
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
YAHOO_TTF           = "https://query1.finance.yahoo.com/v8/finance/chart/TTF=F"

# Locatie De Bilt
DEBILT_LAT = 52.10
DEBILT_LON = 5.18

# ---------------------------------------------------------------------------
# Seizoennorm zonneproductie (KNMI klimatologie De Bilt)
# ---------------------------------------------------------------------------
MONTHLY_SOLAR_NORM_MJ = {
    1: 2.5, 2: 5.0, 3: 9.0, 4: 14.0, 5: 17.5, 6: 18.5,
    7: 18.0, 8: 15.5, 9: 11.0, 10: 6.5, 11: 3.0, 12: 2.0,
}

# Gemiddelde zonsopkomst / -ondergang lokale tijd (Amsterdam) per maand.
# Gebaseerd op KNMI De Bilt (lat 52.1 deg) klimatologie; DST verwerkt in mrt/apr/okt.
DAYLIGHT_HOURS: dict[int, tuple[float, float]] = {
    1:  (8.8, 16.8),
    2:  (8.2, 17.7),
    3:  (7.5, 19.5),
    4:  (6.4, 20.5),
    5:  (5.7, 21.3),
    6:  (5.3, 21.8),
    7:  (5.5, 21.8),
    8:  (6.2, 21.1),
    9:  (7.0, 20.0),
    10: (7.5, 18.5),
    11: (7.8, 17.0),
    12: (8.5, 16.6),
}


# ---------------------------------------------------------------------------
# Hulpfuncties — zon
# ---------------------------------------------------------------------------

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


def hourly_solar_norm_wh(dt: datetime) -> float:
    """Verwachte W/m2 voor dit specifieke uur in De Bilt (KNMI klimatologie).

    Verdeling: sinusvormig profiel tussen zonsopkomst en -ondergang per maand,
    genormaliseerd zodat het dagintegraal overeenkomt met MONTHLY_SOLAR_NORM_MJ.
    Geeft 0.0 voor nachtelijke uren (< zonsopkomst of > zonsondergang).
    """
    daily_norm_mj = seasonal_solar_norm_mj(dt)
    daily_norm_wh = daily_norm_mj * 1000.0 / 3.6   # MJ/m2 -> Wh/m2

    m = dt.month
    rise, sett = DAYLIGHT_HOURS[m]
    h_mid = dt.hour + 0.5

    if h_mid <= rise or h_mid >= sett:
        return 0.0

    raw = math.sin(math.pi * (h_mid - rise) / (sett - rise))
    norm_sum = sum(
        math.sin(math.pi * (hh + 0.5 - rise) / (sett - rise))
        for hh in range(24)
        if rise < hh + 0.5 < sett
    )
    if norm_sum == 0.0:
        return 0.0

    return daily_norm_wh * raw / norm_sum


# ---------------------------------------------------------------------------
# Data-ophaalfuncties
# ---------------------------------------------------------------------------

def fetch_open_meteo_forecast(forecast_days: int = 7) -> tuple[dict, dict]:
    """Haal weersvoorspelling op voor De Bilt.

    Return:
        daily   : {YYYY-MM-DD: {shortwave_mj, wind_ms (op 100m), temp_c}}
        hourly  : {"YYYY-MM-DDTHH:00": W/m2}
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
        "hourly": "shortwave_radiation",
        "wind_speed_unit": "ms",
        "timezone": "Europe/Amsterdam",
        "forecast_days": forecast_days,
    }
    url = f"{OPEN_METEO_FORECAST}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "stroomvoorspeller/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    daily = data.get("daily", {})
    times  = daily.get("time", [])
    sw     = daily.get("shortwave_radiation_sum", [])
    wmean  = daily.get("wind_speed_10m_mean", [])
    wmax   = daily.get("wind_speed_10m_max", [])
    tmean  = daily.get("temperature_2m_mean", [])

    out: dict = {}
    for i, day in enumerate(times):
        w_ms_10m = None
        if i < len(wmean) and wmean[i] is not None:
            w_ms_10m = float(wmean[i])
        elif i < len(wmax) and wmax[i] is not None:
            w_ms_10m = 0.7 * float(wmax[i])
        w_ms_100m = w_ms_10m * 1.38 if w_ms_10m is not None else None
        out[day] = {
            "shortwave_mj": float(sw[i]) if i < len(sw) and sw[i] is not None else None,
            "wind_ms": w_ms_100m,
            "temp_c": float(tmean[i]) if i < len(tmean) and tmean[i] is not None else None,
        }

    hourly_data = data.get("hourly", {})
    h_times = hourly_data.get("time", [])
    h_sw    = hourly_data.get("shortwave_radiation", [])
    hourly_radiation: dict = {}
    for i, t in enumerate(h_times):
        if i < len(h_sw) and h_sw[i] is not None:
            hourly_radiation[t[:16]] = float(h_sw[i])

    return out, hourly_radiation


def fetch_yahoo_ttf(days_back: int = 35) -> dict:
    """Haal dagelijkse TTF=F closes op. Return: {YYYY-MM-DD: close EUR/MWh}."""
    end   = datetime.now(tz=timezone.utc)
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
    series     = result[0]
    timestamps = series.get("timestamp", []) or []
    quote      = series.get("indicators", {}).get("quote", [{}])[0]
    closes     = quote.get("close", []) or []

    out: dict = {}
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        out[dt.strftime("%Y-%m-%d")] = float(close)
    return out


def compute_ttf_ratio(ttf_series: dict) -> float:
    """Bereken huidige TTF / 30d gemiddelde. 1.0 als data ontbreekt (neutraal)."""
    if not ttf_series:
        return 1.0
    sorted_days = sorted(ttf_series.keys())
    if not sorted_days:
        return 1.0
    current    = ttf_series[sorted_days[-1]]
    history_30 = [ttf_series[d] for d in sorted_days[:-1][-30:]]
    if not history_30:
        return 1.0
    avg = sum(history_30) / len(history_30)
    if avg == 0:
        return 1.0
    return current / avg


# ---------------------------------------------------------------------------
# Prediction log (opslaan + lezen)
# ---------------------------------------------------------------------------

def load_prediction_log(log_file: Path) -> list:
    """Lees prediction_log.json; retourneer [] bij ontbrekend of corrupt bestand."""
    if not log_file.exists():
        return []
    try:
        raw = log_file.read_bytes().rstrip(b"\x00")
        return json.loads(raw) if raw else []
    except (json.JSONDecodeError, ValueError):
        return []


def log_predictions(forecasts: list, log_file: Path) -> None:
    """Schrijf nieuwe voorspellingen weg naar prediction_log.json.

    Schema per entry (v2.1):
        target_time       : ISO-string van het voorspelde uur
        days_ahead        : hoeveel dagen vooruit (2 t/m 7)
        predicted         : EUR/MWh voorspeld
        baseline          : EUR/MWh baseline (regime-aware)
        total_points      : som van alle factorpunten
        model_version     : voor vergelijking na model-updates
        sw_ratio_h        : uurspecifieke zonratio (v1.14)
        sw_ratio_daily    : daggemiddelde zonratio
        wind_ms           : windsnelheid op 100m (v2.1, voor analog search)
        temp_c            : dagtemperatuur (v2.1, voor analog search)
        regime            : gedetecteerd marktregime (v2.1)
        P_negative        : kans op negatieve prijs (v2.1)
        plausibility_score: event plausibility score (v2.1)
        plausibility_label: event plausibility label (v2.1)
        analog_sample_size: aantal historische analogen gevonden (v2.1)
        actual            : werkelijke EPEX-prijs (null totdat update_log.py vult)

    Entries ouder dan PREDICTION_LOG_MAX_DAYS worden gesnoeid.
    """
    existing: list = []
    if log_file.exists():
        try:
            raw = log_file.read_bytes().rstrip(b"\x00")
            existing = json.loads(raw) if raw else []
        except (json.JSONDecodeError, ValueError):
            existing = []

    logged_times: set = {e["target_time"] for e in existing}

    cutoff_str = (datetime.now(timezone.utc) - timedelta(days=PREDICTION_LOG_MAX_DAYS)).isoformat()
    existing = [e for e in existing if e.get("target_time", "") >= cutoff_str[:10]]

    added = 0
    for fc in forecasts:
        t = fc.get("time", "")
        if t in logged_times:
            continue
        existing.append({
            # Voorspellings-metadata
            "target_time":    t,
            "days_ahead":     fc.get("days_ahead"),
            "predicted":      fc.get("predicted"),
            "baseline":       fc.get("baseline"),
            "total_points":   fc.get("total_points"),
            "model_version":  MODEL_VERSION,
            # Weers- en regime-context (v2.1: uitgebreid voor analog search)
            "sw_ratio_h":     fc.get("sw_ratio_h"),
            "sw_ratio_daily": fc.get("sw_ratio_daily"),
            "wind_ms":        fc.get("wind_ms"),
            "temp_c":         fc.get("temp_c"),
            "regime":         fc.get("regime"),
            "P_negative":     fc.get("P_negative"),
            # Plausibility (v2.1)
            "plausibility_score": fc.get("event_plausibility_score"),
            "plausibility_label": fc.get("event_plausibility_label"),
            "analog_sample_size": fc.get("analog_sample_size"),
            # Evaluatie: wordt later aangevuld door update_log.py
            "actual": None,
        })
        added += 1

    if added == 0:
        return

    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_bytes(json.dumps(existing, indent=2, ensure_ascii=False).encode("utf-8"))
    print(f"[info] prediction_log: {added} nieuwe entries toegevoegd "
          f"(totaal {len(existing)}).", file=sys.stderr)


# ---------------------------------------------------------------------------
# Hoofdfunctie
# ---------------------------------------------------------------------------

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

    now_ams     = amsterdam_now()
    today_start = now_ams.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon_start = today_start + timedelta(days=1)
    horizon_end   = today_start + timedelta(days=8)  # exclusive

    # Open-Meteo
    print("[info] Open-Meteo forecast ophalen...", file=sys.stderr)
    try:
        weather, hourly_radiation = fetch_open_meteo_forecast(forecast_days=8)
        print(f"[info] {len(weather)} dagen dagrapporten; "
              f"{len(hourly_radiation)} uurwaarden straling.", file=sys.stderr)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError) as exc:
        print(f"[err] Open-Meteo fout: {exc}", file=sys.stderr)
        weather           = {}
        hourly_radiation  = {}

    # Yahoo TTF
    print("[info] Yahoo TTF historie ophalen...", file=sys.stderr)
    ttf_series = fetch_yahoo_ttf(days_back=35)
    ttf_ratio  = compute_ttf_ratio(ttf_series)
    print(f"[info] TTF ratio (current/30d): {ttf_ratio:.3f}", file=sys.stderr)

    # Vorige-dag-prijs lookup (voor factor_vorige_dag)
    prices_by_iso: dict = {}
    for entry in history:
        t_str = entry.get("time", "")
        try:
            t_norm = datetime.fromisoformat(t_str).replace(
                minute=0, second=0, microsecond=0, tzinfo=None
            ).isoformat()
            prices_by_iso[t_norm] = float(entry["price"])
        except (ValueError, KeyError):
            continue
    print(f"[info] {len(prices_by_iso)} uurprijzen geindexeerd voor prior-day lookup.",
          file=sys.stderr)

    # v2.1: laad prediction_log voor EVENT_PLAUSIBILITY_LAYER.
    # Eenmalig geladen; meegegeven als read-only referentie aan compute_event_plausibility().
    print("[info] prediction_log laden voor plausibility-berekening...", file=sys.stderr)
    historical_log = load_prediction_log(PREDICTION_LOG_FILE)
    print(f"[info] {len(historical_log)} historische entries beschikbaar "
          f"voor analogie-zoekopdracht.", file=sys.stderr)

    forecasts: list = []
    skipped = 0
    cursor = horizon_start
    while cursor < horizon_end:
        day_key = cursor.strftime("%Y-%m-%d")
        wx = weather.get(day_key)
        if not wx or any(wx.get(k) is None for k in ("shortwave_mj", "wind_ms", "temp_c")):
            skipped += 24
            cursor += timedelta(hours=24)
            continue

        sw_ratio_daily = wx["shortwave_mj"] / seasonal_solar_norm_mj(cursor)
        wind           = wx["wind_ms"]
        temp           = wx["temp_c"]
        days_ahead     = (cursor.replace(hour=0) - today_start).days

        for hour in range(24):
            target_dt = cursor.replace(hour=hour, minute=0, second=0, microsecond=0)

            # v1.14: uurlijkse solar_ratio
            hour_key    = target_dt.strftime("%Y-%m-%dT%H:00")
            measured_wh = hourly_radiation.get(hour_key)
            norm_wh     = hourly_solar_norm_wh(target_dt)

            if measured_wh is not None and norm_wh >= 10.0:
                sw_ratio_h = measured_wh / norm_wh
            else:
                sw_ratio_h = sw_ratio_daily

            # v1.8: vorige-dag-prijs voor factor_vorige_dag
            prior_dt       = target_dt - timedelta(days=1)
            prior_dt_naive = prior_dt.replace(tzinfo=None)
            prior_day_price = prices_by_iso.get(prior_dt_naive.isoformat())

            fc = forecast_one(
                target_dt=target_dt,
                history=history,
                shortwave_ratio=sw_ratio_h,
                wind_ms=wind,
                temp_c=temp,
                ttf_ratio=ttf_ratio,
                days_ahead=days_ahead,
                prior_day_price=prior_day_price,
            )
            if fc is None:
                skipped += 1
                continue

            # Bouw het forecast-dict voor dit uur
            fc_dict = {
                "time":            target_dt.isoformat(),
                "baseline":        fc.baseline,
                "predicted":       fc.predicted,
                "lower":           round(fc.lower, 2),
                "upper":           round(fc.upper, 2),
                "uncertainty_pct": fc.uncertainty_pct,
                "total_points":    fc.total_points,
                "days_ahead":      fc.days_ahead,
                "regime":          fc.regime,
                "sw_ratio_h":      round(sw_ratio_h, 3),
                "sw_ratio_daily":  round(sw_ratio_daily, 3),
                # v2.1: weerswaarden en extreme-event-kans voor plausibility layer
                "wind_ms":         round(wind, 2),
                "temp_c":          round(temp, 1),
                "P_negative":      fc.extreme_event_prob,
                "factors":         [
                    {"name": fs.name, "points": fs.points, "reason": fs.reason}
                    for fs in fc.factors
                ],
            }

            # v2.1: EVENT_PLAUSIBILITY_LAYER
            # Wijzigt fc_dict["predicted"] NIET. Voegt plausibility-metadata toe.
            plausibility_input = {
                "target_time": fc_dict["time"],
                "predicted":   fc_dict["predicted"],
                "solar_ratio": fc_dict["sw_ratio_h"],
                "wind_ms":     fc_dict["wind_ms"],
                "temp_c":      fc_dict["temp_c"],
                "P_negative":  fc_dict["P_negative"],
            }
            plausibility = compute_event_plausibility(plausibility_input, historical_log)
            fc_dict.update(plausibility)

            forecasts.append(fc_dict)

        cursor += timedelta(hours=24)

    print(f"[info] {len(forecasts)} voorspellingen gegenereerd; {skipped} overgeslagen.",
          file=sys.stderr)

    payload = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "currency":      "EUR",
        "unit":          "EUR/MWh",
        "tz":            "Europe/Amsterdam",
        "model_version": MODEL_VERSION,
        "horizon_start": horizon_start.isoformat(),
        "horizon_end":   (horizon_end - timedelta(seconds=1)).isoformat(),
        "forecasts":     forecasts,
    }
    if not weather:
        payload["error"] = "Open-Meteo niet beschikbaar; geen voorspelling deze run."
    elif not forecasts:
        payload["error"] = "Geen voorspellingen kunnen genereren (insufficient history?)"

    FORECAST_FILE.parent.mkdir(parents=True, exist_ok=True)
    FORECAST_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[ok] Geschreven: {FORECAST_FILE}", file=sys.stderr)

    # Log predictions voor bias-correctie en plausibility-evaluatie
    if forecasts:
        try:
            log_predictions(forecasts, PREDICTION_LOG_FILE)
        except Exception as exc:
            print(f"[warn] prediction_log schrijven mislukt: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
