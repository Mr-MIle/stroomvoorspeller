#!/usr/bin/env python3
"""
dump_lab_v5.py — backtestdata voor model v5, opgehaald in GitHub Actions.

Waarom: de backtest van v4 rekende met het weer zoals het achteraf was (ERA5).
Dat flatteert alles wat op weer leunt. De Previous Runs API van Open-Meteo bewaart
de weersverwachting zoals die 1 t/m 7 dagen vooraf was, vanaf januari 2024. Met
die data kan de backtest per horizon precies de verwachting gebruiken die het
model op dat moment had.

Inhoud (gzip-JSON, exp-data/lab_v5.json.gz):
    weather  {time: [...], wind: {var: {punt: [...]}}, solar: ..., temp: ...}
             var = basisvariabele (lead 0) en _previous_day1 .. _previous_day7
    ttf      {YYYY-MM-DD: close}
    epex     ruwe voorbeeldresponsen van de EpexPredictor-API (formaat-check)
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from weather_points import PREVIOUS_RUNS_URL, fetch_point_series  # noqa: E402

YAHOO_TTF = "https://query1.finance.yahoo.com/v8/finance/chart/TTF=F"
LEADS = range(1, 8)
BASE_VARS = {
    "wind": "wind_speed_100m",
    "solar": "shortwave_radiation",
    "temp": "temperature_2m",
}


def _vars(base: str) -> list[str]:
    return [base] + [f"{base}_previous_day{k}" for k in LEADS]


def collect_weather(start: date, end: date) -> dict:
    merged: dict = {"time": [], "wind": {}, "solar": {}, "temp": {}}
    cur = start
    while cur <= end:
        chunk_end = min(end, cur + timedelta(days=90))
        print(f"[info] previous-runs {cur} t/m {chunk_end}", file=sys.stderr)
        part = None
        for attempt in range(3):
            try:
                part = fetch_point_series(
                    PREVIOUS_RUNS_URL,
                    {g: _vars(b) for g, b in BASE_VARS.items()},
                    {"start_date": cur.isoformat(), "end_date": chunk_end.isoformat()},
                )
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] chunk {cur} poging {attempt + 1}: {exc}", file=sys.stderr)
                time.sleep(20)
        if part and part.get("time"):
            merged["time"].extend(part["time"])
            n = len(part["time"])
            for g in ("wind", "solar", "temp"):
                for var, by_pt in part.get(g, {}).items():
                    tgt = merged[g].setdefault(var, {})
                    for pt, series in by_pt.items():
                        s = list(series) + [None] * (n - len(series))
                        tgt.setdefault(pt, []).extend(s[:n])
        cur = chunk_end + timedelta(days=1)
        time.sleep(2)
    print(f"[info] weer: {len(merged['time'])} uren", file=sys.stderr)
    return merged


def collect_ttf(start: date) -> dict:
    out: dict = {}
    p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.now(timezone.utc).timestamp())
    q = {"period1": p1, "period2": p2, "interval": "1d", "events": "history"}
    req = urllib.request.Request(f"{YAHOO_TTF}?{urllib.parse.urlencode(q)}", headers={
        "User-Agent": "Mozilla/5.0 (compatible; stroomvoorspeller-lab/5.0)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        res = data["chart"]["result"][0]
        closes = res["indicators"]["quote"][0]["close"]
        for ts, c in zip(res["timestamp"], closes):
            if c is not None:
                out[datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")] = float(c)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] TTF mislukt: {exc}", file=sys.stderr)
    print(f"[info] TTF: {len(out)} dagen", file=sys.stderr)
    return out


def collect_epex_samples() -> dict:
    urls = {
        "prices": "https://epexpredictor.batzill.com/prices?region=NL",
        "prices_short": "https://epexpredictor.batzill.com/prices_short?region=NL&hours=120",
        "prices_eval": "https://epexpredictor.batzill.com/prices?region=NL&evaluation=true",
        "prices_mwh": "https://epexpredictor.batzill.com/prices?region=NL&unit=EUR_PER_MWH",
    }
    out = {}
    for k, u in urls.items():
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "stroomvoorspeller-lab/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                txt = r.read().decode("utf-8")
            out[k] = {"url": u, "body": txt[:200000]}
        except Exception as exc:  # noqa: BLE001
            out[k] = {"url": u, "error": str(exc)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--out", default="exp-data/lab_v5.json.gz")
    a = ap.parse_args()
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else date.today() - timedelta(days=1)

    payload = {
        "meta": {"generated": datetime.now(timezone.utc).isoformat(),
                 "start": str(start), "end": str(end)},
        "epex": collect_epex_samples(),
        "ttf": collect_ttf(start - timedelta(days=60)),
        "weather": collect_weather(start, end),
    }
    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print(f"[ok] {p} ({p.stat().st_size / 1e6:.1f} MB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
