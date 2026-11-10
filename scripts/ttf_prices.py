#!/usr/bin/env python3
"""
ttf_prices.py — TTF-gasprijs per dag (EUR/MWh), voor voorspelling, refit en lab.

Sinds ongeveer 1 nov 2026 geeft Yahoo voor TTF=F met interval=1d nog maar één
koers terug (andere tickers niet). Met interval=1h komt de reeks wel binnen, tot
730 dagen terug. Daarom: dagkoersen uit de 1d-reeks waar die er is, aangevuld
met de laatste uurkoers van elke dag (UTC-datum).

Omdat Yahoo met uurdata hooguit 730 dagen teruggaat, houden we een cache bij in
data/ttf_daily.json. De wekelijkse refit vult die aan (update_cache=True); de
voorspelling leest hem alleen als terugval. Zo blijft de historie groeien, ook als
Yahoo hem kwijtraakt.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "ttf_daily.json"
URL = "https://query1.finance.yahoo.com/v8/finance/chart/TTF=F"
UA = {"User-Agent": "Mozilla/5.0 (compatible; stroomvoorspeller/0.1)",
      "Accept": "application/json"}
MAX_HOURLY_DAYS = 730   # Yahoo: uurdata hooguit range=730d


def _chart(params: dict) -> list[tuple[int, float]]:
    url = f"{URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    res = (data.get("chart") or {}).get("result") or []
    if not res:
        return []
    ts = res[0].get("timestamp") or []
    closes = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    return [(t, float(c)) for t, c in zip(ts, closes) if c is not None]


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def fetch_daily(days_back: int) -> dict:
    """{YYYY-MM-DD: slotkoers} van Yahoo, zo volledig als het nu lukt. {} bij fouten."""
    end = datetime.now(timezone.utc)
    out: dict = {}

    # Uurdata: laatste koers per dag. Met range=<n>d i.p.v. period1/period2: dan telt
    # Yahoo handelsdagen en komt 730d tot ruim 2 jaar terug (period1 kapt eerder af).
    hb = min(days_back, MAX_HOURLY_DAYS)
    try:
        for t, c in _chart({"range": f"{hb}d", "interval": "1h"}):
            out[_day(t)] = c           # later uur overschrijft eerder uur
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Yahoo TTF (1h) fout: {exc}", file=sys.stderr)

    # Dagdata: officiële slotkoers waar Yahoo die (weer) levert, wint van de uurkoers.
    try:
        for t, c in _chart({"period1": int((end - timedelta(days=days_back)).timestamp()),
                            "period2": int(end.timestamp()), "interval": "1d",
                            "events": "history"}):
            out[_day(t)] = c
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Yahoo TTF (1d) fout: {exc}", file=sys.stderr)
    return out


def load_cache() -> dict:
    try:
        return {k: float(v) for k, v in json.loads(CACHE.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def get_ttf(days_back: int, update_cache: bool = False) -> dict:
    """Cache + verse Yahoo-koersen (vers wint), vanaf vandaag - days_back."""
    fresh = fetch_daily(days_back)
    merged = {**load_cache(), **fresh}
    if update_cache and fresh:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(dict(sorted(merged.items())), indent=0),
                         encoding="utf-8")
    start = (date.today() - timedelta(days=days_back)).isoformat()
    out = {k: v for k, v in sorted(merged.items()) if k >= start}
    print(f"[info] TTF: {len(out)} dagen ({len(fresh)} vers van Yahoo)"
          + (f", t/m {max(out)}" if out else ""), file=sys.stderr)
    return out


def usable(ttf: dict, min_days: int, max_age_days: int = 7) -> bool:
    """Genoeg dagen én recent genoeg om op te schatten of te voorspellen."""
    if len(ttf) < min_days:
        return False
    return (date.today() - date.fromisoformat(max(ttf))).days <= max_age_days


if __name__ == "__main__":
    s = get_ttf(int(sys.argv[1]) if len(sys.argv) > 1 else 35,
                update_cache="--update-cache" in sys.argv)
    for k in list(s)[-10:]:
        print(k, round(s[k], 2))
