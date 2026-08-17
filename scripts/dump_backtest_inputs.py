#!/usr/bin/env python3
"""
dump_backtest_inputs.py — bouwt één gecomprimeerd inputbestand voor modelexperimenten.

Waarom dit bestaat: de ontwikkelomgeving heeft geen netwerktoegang tot ENTSO-E,
Open-Meteo en Yahoo. Deze dump draait één keer in GitHub Actions (die wél netwerk
heeft), commit het resultaat terug naar de experimentbranch, en daarna kunnen
tientallen modelvarianten offline en in seconden op exact dezelfde inputs worden
vergeleken.

Inhoud van de dump (gzip-JSON):
    prices          [{time, price}]            uurprijzen EUR/MWh, Amsterdam-lokaal
    weather_daily   {YYYY-MM-DD: {...}}        dagwaarden zoals backtest.py ze nu gebruikt
    weather_hourly  {YYYY-MM-DDTHH: {...}}     uurwaarden straling/wind100m/temp (nieuw)
    ttf             {YYYY-MM-DD: close}        TTF-slotkoers EUR/MWh

Gebruik:
    python3 scripts/dump_backtest_inputs.py --start 2021-01-01 --end 2026-08-16 \
        --out exp-data/inputs.json.gz
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from load_archive import load_range as archive_load_range  # noqa: E402
from fetch_prices import fetch_entsoe, aggregate_to_hourly  # noqa: E402

AMS = ZoneInfo("Europe/Amsterdam")
DEBILT_LAT, DEBILT_LON = 52.10, 5.18
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
YAHOO_TTF = "https://query1.finance.yahoo.com/v8/finance/chart/TTF=F"


def _get_json(url: str, tries: int = 4, timeout: int = 90) -> dict:
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; stroomvoorspeller-lab/1.0)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            wait = 3 * (attempt + 1)
            print(f"[warn] poging {attempt + 1} mislukt ({exc}); {wait}s wachten",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"ophalen mislukt na {tries} pogingen: {last}")


# ---------------- prijzen ----------------

def collect_prices(start: datetime, end: datetime) -> list[dict]:
    """Archief eerst; het gat tot vandaag via ENTSO-E (archief-backfill loopt achter)."""
    prices = archive_load_range(start, end + timedelta(days=1))
    print(f"[info] archief: {len(prices)} uurprijzen", file=sys.stderr)

    have_last = None
    if prices:
        have_last = datetime.fromisoformat(prices[-1]["time"]).astimezone(AMS)

    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if have_last is not None and have_last.date() >= end.date():
        return prices
    if not token:
        print("[warn] ENTSOE_TOKEN ontbreekt; alleen archiefprijzen", file=sys.stderr)
        return prices

    gap_start = (have_last + timedelta(hours=1)) if have_last else start
    gap_end = end + timedelta(days=1)
    print(f"[info] ENTSO-E aanvulling {gap_start.date()} t/m {gap_end.date()}", file=sys.stderr)

    extra_raw: list[dict] = []
    cursor = gap_start
    while cursor < gap_end:
        chunk_end = min(cursor + timedelta(days=14), gap_end)
        try:
            part = fetch_entsoe(token, cursor.astimezone(timezone.utc),
                                chunk_end.astimezone(timezone.utc))
            extra_raw.extend(part)
            print(f"[info]   {cursor.date()}–{chunk_end.date()}: {len(part)} punten",
                  file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn]   chunk {cursor.date()} mislukt: {exc}", file=sys.stderr)
        cursor = chunk_end
        time.sleep(1)

    extra = aggregate_to_hourly(extra_raw) if extra_raw else []
    merged = {p["time"]: p["price"] for p in prices}
    for p in extra:
        merged[p["time"]] = p["price"]
    out = [{"time": t, "price": v} for t, v in sorted(merged.items())]
    print(f"[info] totaal na aanvulling: {len(out)} uurprijzen", file=sys.stderr)
    return out


# ---------------- weer ----------------

def _parse_weather_payload(data: dict, daily_out: dict, hourly_out: dict) -> None:
    daily = data.get("daily", {}) or {}
    times = daily.get("time", []) or []
    sw = daily.get("shortwave_radiation_sum", []) or []
    wmean = daily.get("wind_speed_10m_mean", []) or []
    wmax = daily.get("wind_speed_10m_max", []) or []
    tmean = daily.get("temperature_2m_mean", []) or []

    for i, day in enumerate(times):
        w10 = None
        if i < len(wmean) and wmean[i] is not None:
            w10 = float(wmean[i])
        elif i < len(wmax) and wmax[i] is not None:
            w10 = 0.7 * float(wmax[i])
        daily_out[day] = {
            # exact dezelfde afleiding als backtest.py vandaag gebruikt
            "shortwave_mj": float(sw[i]) if i < len(sw) and sw[i] is not None else None,
            "wind_ms": w10 * 1.38 if w10 is not None else None,
            "temp_c": float(tmean[i]) if i < len(tmean) and tmean[i] is not None else None,
        }

    hourly = data.get("hourly", {}) or {}
    h_times = hourly.get("time", []) or []
    h_sw = hourly.get("shortwave_radiation", []) or []
    h_w100 = hourly.get("wind_speed_100m", []) or []
    h_w10 = hourly.get("wind_speed_10m", []) or []
    h_t = hourly.get("temperature_2m", []) or []
    for i, t in enumerate(h_times):
        w100 = None
        if i < len(h_w100) and h_w100[i] is not None:
            w100 = float(h_w100[i])
        elif i < len(h_w10) and h_w10[i] is not None:
            w100 = float(h_w10[i]) * 1.38
        hourly_out[t[:13]] = {
            "sw_wh": float(h_sw[i]) if i < len(h_sw) and h_sw[i] is not None else None,
            "wind100": w100,
            "temp_c": float(h_t[i]) if i < len(h_t) and h_t[i] is not None else None,
        }


def collect_weather(start: datetime, end: datetime) -> tuple[dict, dict]:
    """Open-Meteo ERA5-archief per jaar; recente dagen uit de forecast-API (archief loopt ~5d achter)."""
    daily_out: dict[str, dict] = {}
    hourly_out: dict[str, dict] = {}

    common_daily = ",".join([
        "shortwave_radiation_sum", "wind_speed_10m_max",
        "temperature_2m_mean", "wind_speed_10m_mean",
    ])
    common_hourly = ",".join([
        "shortwave_radiation", "wind_speed_100m", "wind_speed_10m", "temperature_2m",
    ])

    year = start.year
    while year <= end.year:
        chunk_start = max(start, datetime(year, 1, 1, tzinfo=AMS)).strftime("%Y-%m-%d")
        chunk_end = min(end, datetime(year, 12, 31, tzinfo=AMS)).strftime("%Y-%m-%d")
        params = {
            "latitude": DEBILT_LAT, "longitude": DEBILT_LON,
            "start_date": chunk_start, "end_date": chunk_end,
            "daily": common_daily, "hourly": common_hourly,
            "wind_speed_unit": "ms", "timezone": "Europe/Amsterdam",
        }
        url = f"{OPEN_METEO_ARCHIVE}?{urllib.parse.urlencode(params)}"
        print(f"[info] weer-archief {chunk_start} t/m {chunk_end}", file=sys.stderr)
        try:
            _parse_weather_payload(_get_json(url), daily_out, hourly_out)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] weer-archief {year} mislukt: {exc}", file=sys.stderr)
        year += 1
        time.sleep(1)

    # Recente dagen: ERA5 loopt ~5 dagen achter. past_days vult het gat.
    try:
        params = {
            "latitude": DEBILT_LAT, "longitude": DEBILT_LON,
            "daily": common_daily, "hourly": common_hourly,
            "past_days": 14, "forecast_days": 1,
            "wind_speed_unit": "ms", "timezone": "Europe/Amsterdam",
        }
        url = f"{OPEN_METEO_FORECAST}?{urllib.parse.urlencode(params)}"
        print("[info] weer-aanvulling via forecast-API (past_days=14)", file=sys.stderr)
        recent_daily: dict = {}
        recent_hourly: dict = {}
        _parse_weather_payload(_get_json(url), recent_daily, recent_hourly)
        for k, v in recent_daily.items():
            daily_out.setdefault(k, v)
        for k, v in recent_hourly.items():
            hourly_out.setdefault(k, v)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] weer-aanvulling mislukt: {exc}", file=sys.stderr)

    print(f"[info] weer: {len(daily_out)} dagen, {len(hourly_out)} uren", file=sys.stderr)
    return daily_out, hourly_out


# ---------------- gas ----------------

def collect_ttf(start: datetime, end: datetime) -> dict:
    out: dict[str, float] = {}
    year = start.year
    while year <= end.year:
        p1 = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
        p2 = int(min(datetime(year + 1, 1, 8, tzinfo=timezone.utc),
                     datetime.now(timezone.utc)).timestamp())
        params = {"period1": p1, "period2": p2, "interval": "1d", "events": "history"}
        url = f"{YAHOO_TTF}?{urllib.parse.urlencode(params)}"
        try:
            data = _get_json(url)
            result = (data.get("chart", {}) or {}).get("result", []) or []
            if result:
                series = result[0]
                stamps = series.get("timestamp", []) or []
                closes = (series.get("indicators", {}).get("quote", [{}])[0]
                          .get("close", []) or [])
                for ts, close in zip(stamps, closes):
                    if close is None:
                        continue
                    d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                    out[d] = float(close)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] TTF {year} mislukt: {exc}", file=sys.stderr)
        year += 1
        time.sleep(1)
    print(f"[info] TTF: {len(out)} handelsdagen", file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--out", default="exp-data/inputs.json.gz")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=AMS)
    if args.end:
        end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=AMS)
    else:
        end = datetime.now(AMS) - timedelta(days=1)

    prices = collect_prices(start, end)
    weather_daily, weather_hourly = collect_weather(start, end)
    ttf = collect_ttf(start - timedelta(days=45), end)

    payload = {
        "meta": {
            "generated": datetime.now(timezone.utc).isoformat(),
            "start": start.strftime("%Y-%m-%d"),
            "end": end.strftime("%Y-%m-%d"),
            "n_prices": len(prices),
            "n_weather_daily": len(weather_daily),
            "n_weather_hourly": len(weather_hourly),
            "n_ttf": len(ttf),
        },
        "prices": prices,
        "weather_daily": weather_daily,
        "weather_hourly": weather_hourly,
        "ttf": ttf,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print(f"[ok] {out_path} geschreven ({out_path.stat().st_size / 1e6:.1f} MB)",
          file=sys.stderr)
    print(json.dumps(payload["meta"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
