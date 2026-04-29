"""
fetch_prices.py — Haalt day-ahead elektriciteitsprijzen op van ENTSO-E.

Gebruik:
    ENTSOE_TOKEN=xxx python scripts/fetch_prices.py

Zonder ENTSOE_TOKEN: genereert realistische sample-data zodat de frontend
ook zonder live API-key getest kan worden.

Output: public/data/prices.json, met de structuur:
    {
        "generated_at": ISO-timestamp UTC,
        "currency": "EUR",
        "unit": "EUR/MWh",
        "tz": "Europe/Amsterdam",
        "source": "entsoe" | "sample",
        "prices": [
            {"time": "2026-04-27T00:00:00+02:00", "price": 42.31},
            ...
        ]
    }

Prijzen zijn in EUR per MWh (zoals ENTSO-E rapporteert). De frontend rekent
om naar consumenten-eurocenten per kWh inclusief schattingsopslag.

Sinds v1.6 (2026-04-29): we halen 14 dagen historie + 2 dagen toekomst op.
De extra historie is nodig voor run_forecast.py om voldoende baseline-data
te hebben (werkdag 7d, weekend 14d, feestdag 7d). De frontend filtert op
"vandaag + morgen" voor weergave, dus de extra historie schaadt niet.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Nederland EIC-code (zie ENTSO-E EIC list)
NL_EIC = "10YNL----------L"

# Day-ahead prices document type
DOC_TYPE_DAY_AHEAD = "A44"

ENTSOE_BASE = "https://web-api.tp.entsoe.eu/api"

# Output locatie t.o.v. project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = PROJECT_ROOT / "public" / "data" / "prices.json"

# Amsterdam tijdzone (statisch want we draaien in UTC op CI)
AMS_OFFSET_WINTER = timedelta(hours=1)
AMS_OFFSET_SUMMER = timedelta(hours=2)


def amsterdam_now() -> datetime:
    """Huidige tijd in Amsterdam, simpele DST-benadering."""
    now_utc = datetime.now(timezone.utc)
    # DST: laatste zondag maart 01:00 UTC tot laatste zondag oktober 01:00 UTC
    year = now_utc.year
    march = datetime(year, 3, 31, 1, 0, tzinfo=timezone.utc)
    while march.weekday() != 6:
        march -= timedelta(days=1)
    october = datetime(year, 10, 31, 1, 0, tzinfo=timezone.utc)
    while october.weekday() != 6:
        october -= timedelta(days=1)
    is_dst = march <= now_utc < october
    offset = AMS_OFFSET_SUMMER if is_dst else AMS_OFFSET_WINTER
    return (now_utc + offset).replace(tzinfo=timezone(offset))


def entsoe_period(dt: datetime) -> str:
    """Format als YYYYMMDDHHMM in UTC, zoals ENTSO-E vereist."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%d%H%M")


def fetch_entsoe(token: str, start_utc: datetime, end_utc: datetime) -> list[dict]:
    """Haal day-ahead prijzen op bij ENTSO-E voor de gegeven UTC-periode."""
    params = {
        "documentType": DOC_TYPE_DAY_AHEAD,
        "in_Domain": NL_EIC,
        "out_Domain": NL_EIC,
        "periodStart": entsoe_period(start_utc),
        "periodEnd": entsoe_period(end_utc),
        "securityToken": token,
    }
    url = f"{ENTSOE_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "stroomvoorspeller/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")

    return parse_entsoe_xml(body, start_utc)


def parse_entsoe_xml(xml_text: str, default_start_utc: datetime) -> list[dict]:
    """Parseer ENTSO-E day-ahead prices XML naar een lijst van prijzen.

    Resultaat is een lijst met dicts {time: ISO-string Amsterdam, price: float}.
    """
    cleaned = xml_text
    tree = ET.fromstring(cleaned)
    ns = ""
    if tree.tag.startswith("{"):
        ns = tree.tag.split("}", 1)[0] + "}"

    results: list[dict] = []
    for ts in tree.findall(f"{ns}TimeSeries"):
        period = ts.find(f"{ns}Period")
        if period is None:
            continue
        start_text = period.find(f"{ns}timeInterval/{ns}start").text
        period_start_utc = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        resolution = period.find(f"{ns}resolution").text  # bv. PT60M

        if resolution.endswith("M"):
            res_minutes = int(resolution.replace("PT", "").replace("M", ""))
        elif resolution.endswith("H"):
            res_minutes = int(resolution.replace("PT", "").replace("H", "")) * 60
        else:
            res_minutes = 60

        for point in period.findall(f"{ns}Point"):
            position = int(point.find(f"{ns}position").text)
            price_text = point.find(f"{ns}price.amount").text
            price = float(price_text)
            point_utc = period_start_utc + timedelta(minutes=res_minutes * (position - 1))
            point_ams = utc_to_amsterdam(point_utc)
            results.append({"time": point_ams.isoformat(), "price": round(price, 2)})

    results.sort(key=lambda x: x["time"])
    return results


def utc_to_amsterdam(dt_utc: datetime) -> datetime:
    """Converteer UTC tijdstip naar Amsterdam met simpele DST-benadering."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    dt_utc = dt_utc.astimezone(timezone.utc)
    year = dt_utc.year
    march = datetime(year, 3, 31, 1, 0, tzinfo=timezone.utc)
    while march.weekday() != 6:
        march -= timedelta(days=1)
    october = datetime(year, 10, 31, 1, 0, tzinfo=timezone.utc)
    while october.weekday() != 6:
        october -= timedelta(days=1)
    offset = AMS_OFFSET_SUMMER if march <= dt_utc < october else AMS_OFFSET_WINTER
    return (dt_utc + offset).replace(tzinfo=timezone(offset))


def aggregate_to_hourly(prices: list[dict]) -> list[dict]:
    """Aggregeer sub-uurlijke prijzen (bv. ENTSO-E PT15M kwartieren) naar uurgemiddelden.

    EPEX/ENTSO-E publiceren voor NL day-ahead sinds 2025 in 15-minuten-resolutie.
    Voor de site willen we uurgemiddelden tonen zodat de grafiek niet over-druk wordt.
    Punten met dezelfde (jaar, maand, dag, uur) in Amsterdam-lokale tijd worden samen
    genomen als simpel rekenkundig gemiddelde. Als er al exact één punt per uur is,
    is deze functie idempotent.
    """
    if not prices:
        return prices
    buckets: dict[str, list[float]] = {}
    times: dict[str, str] = {}  # bucket_key -> iso-tijd op vol uur
    for p in prices:
        dt = datetime.fromisoformat(p["time"])
        bucket_dt = dt.replace(minute=0, second=0, microsecond=0)
        key = bucket_dt.isoformat()
        buckets.setdefault(key, []).append(float(p["price"]))
        times.setdefault(key, key)
    out = []
    for key in sorted(buckets.keys()):
        vals = buckets[key]
        avg = sum(vals) / len(vals)
        out.append({"time": times[key], "price": round(avg, 2)})
    return out


def generate_sample_prices(now_ams: datetime) -> list[dict]:
    """Genereer realistische sample-data voor 16 dagen (14d historie + 2d toekomst)."""
    rng = random.Random(42)
    start = now_ams.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=14)
    prices = []
    for hour in range(16 * 24):
        t = start + timedelta(hours=hour)
        h = t.hour
        if 0 <= h <= 5:
            base = 35 + rng.uniform(-8, 8)
        elif 6 <= h <= 8:
            base = 95 + rng.uniform(-15, 15)
        elif 9 <= h <= 14:
            base = 30 + rng.uniform(-40, 25)
        elif 15 <= h <= 16:
            base = 65 + rng.uniform(-15, 15)
        elif 17 <= h <= 20:
            base = 130 + rng.uniform(-20, 30)
        else:
            base = 70 + rng.uniform(-15, 15)
        if t.weekday() >= 5:
            base *= 0.9
        prices.append({"time": t.isoformat(), "price": round(base, 2)})
    return prices


def main() -> int:
    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    now_ams = amsterdam_now()

    # Vraag data op voor "vandaag - 14 dagen" tot "overmorgen 00:00" Amsterdam.
    # 14 dagen historie zodat run_forecast.py voldoende baseline-data heeft voor
    # werkdag (7d), weekend (14d) en feestdag (7d). De frontend filtert op
    # "vandaag + morgen" voor weergave, dus de extra historie schaadt niet.
    today_start_ams = now_ams.replace(hour=0, minute=0, second=0, microsecond=0)
    history_start_ams = today_start_ams - timedelta(days=14)
    end_ams = today_start_ams + timedelta(days=2)
    start_utc = history_start_ams.astimezone(timezone.utc)
    end_utc = end_ams.astimezone(timezone.utc)

    source = "sample"
    prices: list[dict] = []
    error_msg = None

    if token:
        try:
            prices = fetch_entsoe(token, start_utc, end_utc)
            source = "entsoe"
            print(f"[ok] {len(prices)} ruwe prijspunten opgehaald van ENTSO-E.", file=sys.stderr)
            before = len(prices)
            prices = aggregate_to_hourly(prices)
            if len(prices) != before:
                print(f"[ok] Geaggregeerd van {before} sub-uurpunten naar {len(prices)} uurpunten.", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            error_msg = f"ENTSO-E fout: {exc}"
            print(f"[warn] {error_msg} - terugval naar sample-data.", file=sys.stderr)

    if not prices:
        prices = generate_sample_prices(now_ams)
        source = "sample"
        print(f"[ok] {len(prices)} sample-prijzen gegenereerd.", file=sys.stderr)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "currency": "EUR",
        "unit": "EUR/MWh",
        "tz": "Europe/Amsterdam",
        "source": source,
        "prices": prices,
    }
    if error_msg:
        payload["last_error"] = error_msg

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[ok] Geschreven: {OUTPUT_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
