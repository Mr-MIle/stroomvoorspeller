#!/usr/bin/env python3
"""
log_benchmark.py — legt de voorspelling van EpexPredictor vast als meetlat.

EpexPredictor (github.com/b3nn0/EpexPredictor, BSD-3) is het open-source model
achter dynamisch-tarief.nl. Door het elke dag op hetzelfde moment als onze eigen
snapshot vast te leggen, kan generate_performance.py beide modellen op exact
dezelfde uren vergelijken. Alleen meten: hun cijfers gaan nooit ons model in.

Schrijft data/benchmark_archive/epexpredictor_YYYY-MM-DD.json in hetzelfde formaat
als data/forecast_archive/ ({"forecasts": [{"time", "price"}]}, uurgemiddelden in
EUR/MWh). Mislukt het ophalen, dan gebeurt er niets (exit 0): dit mag de
voorspelling nooit blokkeren.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "benchmark_archive"
URL = "https://epexpredictor.batzill.com/prices?region=NL&unit=EUR_PER_MWH"


def main() -> int:
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "stroomvoorspeller.nl/benchmark"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] EpexPredictor niet bereikbaar: {exc}", file=sys.stderr)
        return 0

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
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = OUT_DIR / f"epexpredictor_{day}.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bron": URL,
        "known_until": data.get("knownUntil"),
        "unit": "EUR/MWh",
        "forecasts": rows,
    }, indent=1), encoding="utf-8")
    print(f"[ok] {out.name}: {len(rows)} uren", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
