"""
generate_performance.py — stroomvoorspeller.nl
Genereert public/data/performance.json op basis van gearchiveerde forecasts
en werkelijke ENTSO-E prijzen.

Vereist:
  - public/data/prices.json (bevat 'history' dict met werkelijke EPEX-prijzen)
  - public/data/forecast_archive/forecast_YYYY-MM-DD.json (dagelijkse snapshots)

Gebruik:
  python scripts/generate_performance.py

Output:
  public/data/performance.json
"""

import json
import os
import glob
import statistics
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Paden
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
PRICES_JSON = ROOT / "public" / "data" / "prices.json"
ARCHIVE_DIR = ROOT / "public" / "data" / "forecast_archive"
OUTPUT_JSON = ROOT / "public" / "data" / "performance.json"

# Modelversie-geschiedenis (handmatig bijhouden)
MODEL_HISTORY = [
    {"version": "1.0", "date": "2026-04-01", "mae_eur_mwh": None,  "notes": "Initieel model"},
    {"version": "1.3", "date": "2026-04-05", "mae_eur_mwh": None,  "notes": "Gewicht 4%→2%, factor-symmetrisering"},
    {"version": "1.6", "date": "2026-04-29", "mae_eur_mwh": 14.8,  "notes": "Zondag-boost, plafond bereikt"},
    {"version": "2.0", "date": "2026-05-06", "mae_eur_mwh": 13.1,  "notes": "Gewicht 1,5%→3%, regime-detectie v1.12"},
    {"version": "2.1", "date": "2026-05-07", "mae_eur_mwh": None,  "notes": "Mediaan baseline, zon-blokkering uurpatroon"},
]

CURRENT_MODEL_VERSION = "2.1"

# Evaluatievenster: afgelopen N dagen
EVAL_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_prices():
    """Laad prices.json en retourneer dict {iso_hour_str: epex_eur_mwh}."""
    with open(PRICES_JSON, encoding="utf-8") as f:
        data = json.load(f)

    actuals = {}
    # prices.json structuur: {"prices": [{"hour": "2026-04-14T08:00:00Z", "epex": 52.1, ...}, ...]}
    for entry in data.get("prices", []):
        hour_str = entry.get("hour") or entry.get("time") or entry.get("timestamp")
        epex = entry.get("epex") or entry.get("epex_eur_mwh")
        if hour_str and epex is not None:
            # Normaliseer naar UTC ISO-string zonder subseconden
            try:
                dt = datetime.fromisoformat(hour_str.replace("Z", "+00:00"))
                key = dt.strftime("%Y-%m-%dT%H:00:00Z")
                actuals[key] = float(epex)
            except ValueError:
                pass

    return actuals


def load_archive():
    """Laad alle forecast-archiefbestanden. Retourneer lijst van (date, forecasts_dict)."""
    if not ARCHIVE_DIR.exists():
        print(f"[WARN] Archief-map niet gevonden: {ARCHIVE_DIR}")
        return []

    archives = []
    for path in sorted(ARCHIVE_DIR.glob("forecast_*.json")):
        # Bestandsnaam = forecast_YYYY-MM-DD.json
        stem = path.stem  # "forecast_2026-04-14"
        parts = stem.split("_", 1)
        if len(parts) != 2:
            continue
        try:
            snap_date = date.fromisoformat(parts[1])
        except ValueError:
            continue

        with open(path, encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                print(f"[WARN] Ongeldig JSON: {path}")
                continue

        # forecast.json structuur: {"forecasts": [{"hour": "...", "epex_forecast": 55.0, "band_low": ..., "band_high": ...}]}
        fc_dict = {}
        for entry in data.get("forecasts", []):
            hour_str = entry.get("hour") or entry.get("time")
            fc_val = entry.get("epex_forecast") or entry.get("forecast_eur_mwh")
            band_low = entry.get("band_low")
            band_high = entry.get("band_high")
            if hour_str and fc_val is not None:
                try:
                    dt = datetime.fromisoformat(hour_str.replace("Z", "+00:00"))
                    key = dt.strftime("%Y-%m-%dT%H:00:00Z")
                    fc_dict[key] = {
                        "forecast": float(fc_val),
                        "band_low": float(band_low) if band_low is not None else None,
                        "band_high": float(band_high) if band_high is not None else None,
                    }
                except ValueError:
                    pass

        archives.append((snap_date, fc_dict))

    return archives


def classify(epex_eur_mwh):
    """Klassificeer EPEX-prijs naar bucket (zelfde logica als app.js classify())."""
    # Omreken naar ct/kWh incl. belasting (gem. opslag + EB + btw)
    # Aanname: gem. opslag = 0.021 EUR/kWh, EB = 0.0916, btw = 1.21
    inclusive_ct = (epex_eur_mwh / 1000 + 0.021 + 0.0916) * 1.21 * 100
    if inclusive_ct < 0:
        return "negatief"
    elif inclusive_ct < 14:
        return "very_cheap"
    elif inclusive_ct < 22:
        return "cheap"
    elif inclusive_ct < 28:
        return "normal"
    elif inclusive_ct < 38:
        return "pricey"
    else:
        return "very_pricey"


def naive_forecast(actuals, target_dt, daytype):
    """Naïeve voorspelling: mediaan van zelfde uur, 7d terug (werkdag) of 14d terug (weekend)."""
    window = 14 if daytype == "weekend" else 7
    candidates = []
    for d in range(1, window + 1):
        candidate_dt = target_dt - timedelta(days=d)
        key = candidate_dt.strftime("%Y-%m-%dT%H:00:00Z")
        if key in actuals:
            candidates.append(actuals[key])
    if candidates:
        return statistics.median(candidates)
    return None


def day_type(dt):
    """Werkdag of weekend (vereenvoudigd, geen feestdagen)."""
    return "weekend" if dt.weekday() >= 5 else "werkdag"


# ---------------------------------------------------------------------------
# Hoofd-berekening
# ---------------------------------------------------------------------------

def compute_performance():
    print("[INFO] Laden prices.json...")
    actuals = load_prices()
    print(f"[INFO] {len(actuals)} werkelijke uren geladen.")

    print("[INFO] Laden forecast-archief...")
    archives = load_archive()
    print(f"[INFO] {len(archives)} archief-snapshots gevonden.")

    if not archives:
        print("[WARN] Geen archief-data. performance.json wordt leeg gegenereerd.")

    # Evaluatievenster
    today = date.today()
    window_start = today - timedelta(days=EVAL_WINDOW_DAYS)

    # Verzamel matched pairs: (actual, forecast, horizon_days, hour_of_day, bucket_actual, within_band, naive)
    pairs = []

    for snap_date, fc_dict in archives:
        if snap_date < window_start:
            continue  # buiten evaluatievenster

        for hour_str, fc_entry in fc_dict.items():
            try:
                target_dt = datetime.fromisoformat(hour_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            target_date = target_dt.date()
            horizon = (target_date - snap_date).days

            # Alleen horizon 2–7; dag 0/1 zijn exacte ENTSO-E-prijzen
            if horizon < 2 or horizon > 7:
                continue

            # Actual beschikbaar?
            if hour_str not in actuals:
                continue

            actual = actuals[hour_str]
            forecast = fc_entry["forecast"]
            band_low = fc_entry.get("band_low")
            band_high = fc_entry.get("band_high")

            # Naïef
            naive = naive_forecast(actuals, target_dt, day_type(target_dt))

            # Within band
            within_band = False
            if band_low is not None and band_high is not None:
                within_band = band_low <= actual <= band_high

            pairs.append({
                "actual": actual,
                "forecast": forecast,
                "naive": naive,
                "horizon": horizon,
                "hour": target_dt.hour,
                "date": str(target_date),
                "bucket_actual": classify(actual),
                "bucket_forecast": classify(forecast),
                "within_band": within_band,
                "error": abs(forecast - actual),
                "signed_error": forecast - actual,
            })

    # ---------------------------------------------------------------------------
    # Aggregeer metrics
    # ---------------------------------------------------------------------------

    def mae(ps):
        if not ps:
            return None
        return round(sum(p["error"] for p in ps) / len(ps), 2)

    def mae_vs_naive_pct(ps):
        valid = [p for p in ps if p["naive"] is not None]
        if not valid:
            return None
        mae_model = sum(p["error"] for p in valid) / len(valid)
        mae_naive = sum(abs(p["actual"] - p["naive"]) for p in valid) / len(valid)
        if mae_naive == 0:
            return None
        return round((mae_model - mae_naive) / mae_naive * 100, 1)

    def direction_hit(ps):
        valid = [p for p in ps if p["naive"] is not None]
        if not valid:
            return None
        hits = sum(
            1 for p in valid
            if (p["forecast"] - p["naive"]) * (p["actual"] - p["naive"]) > 0
        )
        return round(hits / len(valid), 3)

    def within_band_pct(ps):
        if not ps:
            return None
        return round(sum(1 for p in ps if p["within_band"]) / len(ps), 3)

    # Overall
    overall = {
        "n_hours": len(pairs),
        "mae_eur_mwh": mae(pairs),
        "mae_vs_naive_pct": mae_vs_naive_pct(pairs),
        "direction_hit_rate": direction_hit(pairs),
        "within_band_pct": within_band_pct(pairs),
    }

    # Per horizon
    by_horizon = []
    for h in range(2, 8):
        hp = [p for p in pairs if p["horizon"] == h]
        by_horizon.append({
            "horizon_days": h,
            "n_hours": len(hp),
            "mae_eur_mwh": mae(hp),
            "mae_vs_naive_pct": mae_vs_naive_pct(hp),
            "direction_hit_rate": direction_hit(hp),
        })

    # Per uur van de dag
    by_hour_of_day = []
    for h in range(24):
        hp = [p for p in pairs if p["hour"] == h]
        by_hour_of_day.append({
            "hour": h,
            "n_hours": len(hp),
            "mae_eur_mwh": mae(hp),
        })

    # Per prijsbucket
    buckets = ["negatief", "very_cheap", "cheap", "normal", "pricey", "very_pricey"]
    by_price_bucket = []
    for b in buckets:
        actual_in_bucket = [p for p in pairs if p["bucket_actual"] == b]
        correct = [p for p in actual_in_bucket if p["bucket_forecast"] == b]
        recall = round(len(correct) / len(actual_in_bucket), 3) if actual_in_bucket else None
        by_price_bucket.append({
            "bucket": b,
            "n_hours": len(actual_in_bucket),
            "mae_eur_mwh": mae(actual_in_bucket),
            "recall": recall,
        })

    # Dagelijkse serie
    daily_map = {}
    for p in pairs:
        d = p["date"]
        if d not in daily_map:
            daily_map[d] = []
        daily_map[d].append(p)

    daily_series = []
    for d in sorted(daily_map):
        dp = daily_map[d]
        daily_series.append({
            "date": d,
            "n_hours": len(dp),
            "mae_eur_mwh": mae(dp),
            "avg_actual_eur_mwh": round(sum(p["actual"] for p in dp) / len(dp), 2),
            "avg_forecast_eur_mwh": round(sum(p["forecast"] for p in dp) / len(dp), 2),
        })

    # ---------------------------------------------------------------------------
    # Samenvoegen en schrijven
    # ---------------------------------------------------------------------------
    dates = sorted(daily_map.keys()) if daily_map else []
    result = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_version": CURRENT_MODEL_VERSION,
        "evaluation_window_days": EVAL_WINDOW_DAYS,
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "overall": overall,
        "by_horizon": by_horizon,
        "by_hour_of_day": by_hour_of_day,
        "by_price_bucket": by_price_bucket,
        "daily_series": daily_series,
        "model_history": MODEL_HISTORY,
    }

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[OK] performance.json geschreven ({len(pairs)} matched pairs, {len(dates)} dagen).")


if __name__ == "__main__":
    compute_performance()
