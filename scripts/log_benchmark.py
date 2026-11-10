#!/usr/bin/env python3
"""
log_benchmark.py — legt de voorspellingen van andere modellen vast als meetlat.

- EpexPredictor (github.com/b3nn0/EpexPredictor, BSD-3): het open-source model
  achter dynamisch-tarief.nl.
- WattWanneer (wattwanneer.nl): de openbare weekvoorspelling die hun eigen site
  laadt (/public/week_forecast.json). Sinds 10 nov 2026.

Door ze elke dag op hetzelfde moment als onze eigen snapshot vast te leggen, kan
generate_performance.py de modellen op exact dezelfde uren vergelijken. Alleen
meten: hun cijfers gaan nooit ons model in.

Schrijft data/benchmark_archive/<bron>_YYYY-MM-DD.json in hetzelfde formaat als
data/forecast_archive/ ({"forecasts": [{"time", "price"}]}, uurgemiddelden in
EUR/MWh). Mislukt het ophalen, dan gebeurt er niets (exit 0): dit mag de
voorspelling nooit blokkeren.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "benchmark_archive"
UA = {"User-Agent": "stroomvoorspeller.nl/benchmark"}
AMS = ZoneInfo("Europe/Amsterdam")

EPEXPREDICTOR_URL = "https://epexpredictor.batzill.com/prices?region=NL&unit=EUR_PER_MWH"
WATTWANNEER_URL = "https://wattwanneer.nl/public/week_forecast.json"


def _get_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _write(name: str, meta: dict, rows: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = OUT_DIR / f"{name}_{day}.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **meta,
        "unit": "EUR/MWh",
        "forecasts": rows,
    }, indent=1), encoding="utf-8")
    print(f"[ok] {out.name}: {len(rows)} uren", file=sys.stderr)


def log_epexpredictor() -> None:
    try:
        data = _get_json(EPEXPREDICTOR_URL)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] EpexPredictor niet bereikbaar: {exc}", file=sys.stderr)
        return

    per_hour: dict[str, list[float]] = {}
    for p in data.get("prices", []):
        try:
            t = datetime.fromisoformat(p["startsAt"])
            v = float(p["total"])
        except (KeyError, ValueError, TypeError):
            continue
        key = t.replace(minute=0, second=0, microsecond=0).isoformat()
        per_hour.setdefault(key, []).append(v)
    rows = [{"time": k, "price": round(sum(v) / len(v), 2)}
            for k, v in sorted(per_hour.items()) if len(v) >= 2]
    if not rows:
        print("[warn] EpexPredictor gaf geen bruikbare prijzen", file=sys.stderr)
        return
    _write("epexpredictor", {"bron": EPEXPREDICTOR_URL,
                             "known_until": data.get("knownUntil")}, rows)


def log_wattwanneer() -> None:
    """WattWanneer ververst één keer per dag (rond 13:20 NL-tijd).

    We bewaren alleen een run van vandaag: draait deze stap 's ochtends, dan staat
    daar nog de run van gisteren, en die hoort niet onder de datum van vandaag. Een
    latere stap op dezelfde dag overschrijft het bestand met de verse run.

    "price" = hun modelwaarde (price_model_eur_kwh). Die tonen ze op de site voor
    de uren zonder day-ahead prijs; voor de uren mét day-ahead tonen ze de echte
    prijs, maar het model blijft dan staan als voorspelling. p50 en de
    bias-gecorrigeerde variant gaan mee voor latere analyse.
    """
    try:
        data = _get_json(WATTWANNEER_URL)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] WattWanneer niet bereikbaar: {exc}", file=sys.stderr)
        return

    entries = list(data.values()) if isinstance(data, dict) else list(data or [])
    run = str(entries[0].get("generated_at", "")) if entries else ""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    if not run.startswith(today):
        print(f"[info] WattWanneer-run {run or '?'} is niet van vandaag; niets vastgelegd",
              file=sys.stderr)
        return

    def eur_mwh(v):
        return round(float(v) * 1000, 2) if v is not None else None

    rows = []
    for e in entries:
        try:
            t = datetime.strptime(e["datetime"], "%Y-%m-%d %H:%M").replace(tzinfo=AMS)
            price = eur_mwh(e.get("price_model_eur_kwh"))
        except (KeyError, ValueError, TypeError):
            continue
        if price is None:
            continue
        rows.append({
            "time": t.isoformat(),
            "price": price,
            "p50": eur_mwh(e.get("predicted_price_p50")),
            "corrected": eur_mwh(e.get("corrected_price_eur_kwh")),
            "source": e.get("source"),
        })
    if not rows:
        print("[warn] WattWanneer gaf geen bruikbare prijzen", file=sys.stderr)
        return
    _write("wattwanneer", {"bron": WATTWANNEER_URL, "run": run}, rows)


def main() -> int:
    log_epexpredictor()
    log_wattwanneer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
