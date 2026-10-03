#!/usr/bin/env python3
"""
refit_model_v5.py — schat de gewichten van model v5 opnieuw (wekelijks, GitHub Actions).

Leert van alle dagen sinds februari 2024. Per trainingsrij gebruikt het script
precies wat het model op dat moment had kunnen weten:
  - prijzen t/m de laatst bekende dag k;
  - het weer voor de doeldag zoals het L+1 dagen vooraf verwacht werd
    (Open-Meteo Previous Runs API), niet het weer zoals het achteraf was;
  - de gasprijs van dag k.

Schrijft public/data/model_v5.json: per uur van de dag de gewichten, plus wanneer en
waarop ze geschat zijn. run_forecast.py leest dat bestand; ontbreekt het, dan valt
het model terug op v4.

Gebruik:
    python scripts/refit_model_v5.py                 # haalt alles zelf op
    python scripts/refit_model_v5.py --lab exp-data/lab_v5.json.gz   # offline
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import model_v5 as m5  # noqa: E402

ROOT = SCRIPT_DIR.parent
OUT = ROOT / "public" / "data" / "model_v5.json"
PRICES_JSON = ROOT / "public" / "data" / "prices.json"
TRAIN_FROM = date(2024, 2, 1)


def lead_vars(L: int) -> tuple[str, str, str]:
    suf = "" if L == 0 else f"_previous_day{L}"
    return (f"wind_speed_100m{suf}", f"shortwave_radiation{suf}", f"temperature_2m{suf}")


def load_prices(start: date) -> dict:
    from load_archive import load_range
    from zoneinfo import ZoneInfo
    ams = ZoneInfo("Europe/Amsterdam")
    hist = load_range(datetime(start.year, start.month, start.day, tzinfo=ams),
                      datetime.now(ams) + timedelta(days=2))
    try:
        hist += json.loads(PRICES_JSON.read_text(encoding="utf-8")).get("prices", [])
    except (OSError, ValueError):
        pass
    return m5.prices_by_day(hist)


def ttf_gas_by_day(ttf: dict):
    days = sorted(ttf)

    def gas(k: date) -> float:
        ks = [d for d in days if d <= k.isoformat()][-31:]
        if len(ks) < 5:
            return 0.0
        cur = ttf[ks[-1]]
        avg = sum(ttf[d] for d in ks[:-1]) / (len(ks) - 1)
        return cur / avg - 1.0 if avg else 0.0
    return gas


def build_rows(P: dict, idx_by_lead: dict, gas_fn, k_from: date, k_to: date):
    """Trainingsrijen: per (k, L) 24 x (kenmerken, doel=werkelijk - basis)."""
    obs = idx_by_lead[0]
    rows = []
    k = k_from
    while k <= k_to:
        if k in P:
            ref = m5.reference(obs, k)
            g = gas_fn(k)
            for L in range(1, 7):
                target = k + timedelta(L)
                fc = idx_by_lead.get(min(L + 1, 7), {}).get(target)
                if ref is None or fc is None or target not in P:
                    continue
                b4 = m5.baseline_v4(P, k, target)
                if b4 is None:
                    continue
                an = m5.analog_profile(P, obs, k, target, sum(fc["s"]) / 24, sum(fc["w"]) / 24)
                base = ([(1 - m5.ANALOG_MIX) * b + m5.ANALOG_MIX * a for b, a in zip(b4, an)]
                        if an else b4)
                X = m5.hour_features(target, fc, ref, g)
                y = [P[target][h] - base[h] for h in range(24)]
                rows.append({"k": k, "L": L, "target": target, "X": X, "y": y, "base": base})
        k += timedelta(1)
    return rows


def fit(rows: list) -> list:
    return [m5.ridge_fit([r["X"][h] for r in rows], [r["y"][h] for r in rows])
            for h in range(24)]


def fetch_weather(start: date, end: date) -> dict:
    from dump_lab_v5 import collect_weather
    return collect_weather(start, end)


def fetch_ttf(start: date) -> dict:
    from dump_lab_v5 import collect_ttf
    return collect_ttf(start)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default="", help="gebruik een labbestand i.p.v. ophalen")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()

    today = date.today()
    if a.lab:
        lab = json.load(gzip.open(a.lab, "rt", encoding="utf-8"))
        weather, ttf = lab["weather"], lab["ttf"]
    else:
        weather = fetch_weather(date(2024, 1, 1), today - timedelta(days=1))
        ttf = fetch_ttf(date(2023, 11, 1))
    if not weather.get("time") or not ttf:
        print("[err] weer of TTF ontbreekt; model_v5.json blijft ongewijzigd", file=sys.stderr)
        return 1

    idx_by_lead = {L: m5.daily_indices(weather, *lead_vars(L)) for L in range(0, 8)}
    P = load_prices(date(2023, 12, 1))
    last_price_day = max(P)
    rows = build_rows(P, idx_by_lead, ttf_gas_by_day(ttf), TRAIN_FROM, last_price_day - timedelta(1))
    if len(rows) < 500:
        print(f"[err] te weinig trainingsrijen ({len(rows)}); niets geschreven", file=sys.stderr)
        return 1
    hours = fit(rows)

    # In-sample fout ter controle (geen backtest: die staat in het A/B-rapport).
    import statistics as st
    err_base, err_model = [], []
    for r in rows:
        for h in range(24):
            d, _ = m5.apply_hour(hours[h], r["X"][h])
            y = r["y"][h]
            err_base.append(abs(y))
            err_model.append(abs(y - m5.lead_alpha(r["L"]) * d))
    payload = {
        "model_version": m5.MODEL_VERSION,
        "fitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "train_from": str(rows[0]["target"]),
        "train_to": str(max(r["target"] for r in rows)),
        "n_rows": len(rows),
        "ridge_lambda": m5.RIDGE_LAMBDA,
        "features": m5.FEATURES,
        "in_sample_mae": {"basis": round(st.mean(err_base), 2),
                          "model": round(st.mean(err_model), 2)},
        "hours": [{"mu": [round(v, 6) for v in hm["mu"]],
                   "sd": [round(v, 6) for v in hm["sd"]],
                   "beta": [round(v, 6) for v in hm["beta"]],
                   "intercept": round(hm["intercept"], 6)} for hm in hours],
    }
    Path(a.out).write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"[ok] {a.out}: {len(rows)} rijen, {payload['train_from']} t/m {payload['train_to']}, "
          f"in-sample MAE basis {payload['in_sample_mae']['basis']} -> "
          f"model {payload['in_sample_mae']['model']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
