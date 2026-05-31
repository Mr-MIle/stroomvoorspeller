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
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

# Nederland EIC-code (zie ENTSO-E EIC list)
NL_EIC = "10YNL----------L"

# Day-ahead prices document type
DOC_TYPE_DAY_AHEAD = "A44"

ENTSOE_BASE = "https://web-api.tp.entsoe.eu/api"

# Output locatie t.o.v. project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = PROJECT_ROOT / "public" / "data" / "prices.json"

# Archief-map voor maandelijkse historische prijzen
ARCHIVE_DIR = PROJECT_ROOT / "public" / "data" / "archief"

# Amsterdam tijdzone via IANA zoneinfo (DST automatisch correct)
AMS_TZ = ZoneInfo("Europe/Amsterdam")


def amsterdam_now() -> datetime:
    """Huidige tijd in Amsterdam (DST-correct via zoneinfo)."""
    return datetime.now(AMS_TZ)


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


def fetch_entsoe_with_retry(
    token: str, start_utc: datetime, end_utc: datetime, max_retries: int = 3
) -> list[dict]:
    """Haal day-ahead prijzen op bij ENTSO-E, met retry bij tijdelijke serverfouten.

    Probeert max_retries keer; bij HTTP 502/503/504 wacht de code steeds langer
    (5 s → 15 s → 45 s) voordat hij het opnieuw probeert. Alle andere fouten
    worden direct doorgegooid.
    """
    delays = [5, 15, 45]
    last_exc: Exception = RuntimeError("geen pogingen gedaan")
    for attempt in range(max_retries):
        try:
            return fetch_entsoe(token, start_utc, end_utc)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in (502, 503, 504) and attempt < max_retries - 1:
                wait = delays[attempt]
                print(
                    f"[warn] ENTSO-E HTTP {exc.code} (poging {attempt + 1}/{max_retries}),"
                    f" wacht {wait}s…",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                raise
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                wait = delays[attempt]
                print(
                    f"[warn] ENTSO-E fout (poging {attempt + 1}/{max_retries}): {exc},"
                    f" wacht {wait}s…",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                raise
    raise last_exc


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
    """Converteer UTC tijdstip naar Amsterdam (DST-correct via zoneinfo)."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(AMS_TZ)


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


def generate_sample_prices_15m(now_ams: datetime) -> list[dict]:
    """Genereer realistische 15-minuten sample-data voor vandaag + morgen (192 punten).

    Elke uur-baseline uit generate_sample_prices krijgt vier kwartierwaarden met
    realistische intra-uur variatie (standaarddeviatie ~4 EUR/MWh).
    """
    rng = random.Random(137)  # apart seed zodat kwartierwaarden anders zijn dan uurwaarden
    start = now_ams.replace(hour=0, minute=0, second=0, microsecond=0)
    prices = []
    for hour in range(2 * 24):  # vandaag + morgen = 48 uur
        t_hour = start + timedelta(hours=hour)
        h = t_hour.hour
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
        if t_hour.weekday() >= 5:
            base *= 0.9
        for q in range(4):
            t = t_hour + timedelta(minutes=15 * q)
            intra = rng.gauss(0, 4.0)  # ~4 EUR/MWh std intra-uur variatie
            prices.append({"time": t.isoformat(), "price": round(base + intra, 2)})
    return prices


def archive_prices(prices: list[dict]) -> None:
    """Sla uurprijzen op in maandelijkse archief-bestanden (append-only).

    Elk bestand in public/data/archief/ heeft de naam YYYY-MM.json en bevat
    alle bekende uurprijzen voor die maand. Bestaande uren worden nooit
    overschreven — alleen nieuwe tijdstempels worden toegevoegd. Toekomstige
    uren (morgen) worden ook gearchiveerd; ze krijgen de komende dag al hun
    definitieve day-ahead prijs mee.

    Wordt alleen aangeroepen bij echte ENTSO-E data (niet bij sample-data).
    """
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    # Groepeer binnenkomende prijzen per maand (op basis van de eerste 7 tekens van time)
    by_month: dict[str, list[dict]] = {}
    for p in prices:
        month_key = p["time"][:7]  # "YYYY-MM"
        by_month.setdefault(month_key, []).append(p)

    for month_key, new_entries in by_month.items():
        archive_file = ARCHIVE_DIR / f"{month_key}.json"

        # Laad bestaand archief (als het er is)
        existing_times: set[str] = set()
        existing_prices: list[dict] = []
        if archive_file.exists():
            try:
                existing = json.loads(archive_file.read_text(encoding="utf-8"))
                existing_prices = existing.get("prices", [])
                # Normaliseer de tijdstempel-key voor de lookup (eerste 19 tekens = YYYY-MM-DDTHH:MM:SS)
                existing_times = {p["time"][:19] for p in existing_prices}
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] Kon archief {archive_file.name} niet lezen: {exc}", file=sys.stderr)

        # Voeg alleen nieuwe uren toe (append-only, geen duplicaten)
        added = 0
        for entry in new_entries:
            if entry["time"][:19] not in existing_times:
                existing_prices.append(entry)
                existing_times.add(entry["time"][:19])
                added += 1

        if added == 0 and archive_file.exists():
            continue  # niets nieuws — bestand niet aanraken

        existing_prices.sort(key=lambda x: x["time"])

        payload = {
            "month": month_key,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "prices": existing_prices,
        }
        archive_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(
            f"[ok] Archief {archive_file.name}: {added} nieuwe uren toegevoegd "
            f"({len(existing_prices)} totaal).",
            file=sys.stderr,
        )


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

    # Datum-strings voor vandaag en morgen (Amsterdam), gebruikt als filter voor prices_15m.
    today_date = today_start_ams.strftime("%Y-%m-%d")
    tomorrow_date = (today_start_ams + timedelta(days=1)).strftime("%Y-%m-%d")

    source = "sample"
    prices: list[dict] = []
    prices_15m: list[dict] = []
    has_pt15m = False
    error_msg = None

    if token:
        try:
            raw_prices = fetch_entsoe_with_retry(token, start_utc, end_utc)
            source = "entsoe"
            print(f"[ok] {len(raw_prices)} ruwe prijspunten opgehaald van ENTSO-E.", file=sys.stderr)
            before = len(raw_prices)
            prices = aggregate_to_hourly(raw_prices)
            has_pt15m = before > len(prices)  # True als ENTSO-E PT15M leverde
            if has_pt15m:
                print(f"[ok] Geaggregeerd van {before} PT15M-punten naar {len(prices)} uurpunten.", file=sys.stderr)
                # Sla ruwe kwartierdata op voor vandaag + morgen (voor de frontend-chart).
                prices_15m = [
                    p for p in raw_prices
                    if p["time"][:10] in (today_date, tomorrow_date)
                ]
                print(f"[ok] {len(prices_15m)} PT15M-punten bewaard voor vandaag+morgen.", file=sys.stderr)

                # Aanvullen: voeg uurdata toe voor uren die geen kwartierdata hebben.
                # ENTSO-E publiceert de PT15M dag-ahead soms partieel (bv. alleen de eerste
                # N uur), waardoor de kwartier-grafiek halverwege afkapt. Voor ontbrekende
                # uren pakken we het uurgemiddelde uit `prices` als fallback-punt, zodat de
                # grafiek altijd de volledige dag toont — in die uren met uurresolutie ipv
                # kwartierresolutie. [:13] geeft "YYYY-MM-DDTHH" ongeacht de tijdzone-suffix.
                covered_hours = {p["time"][:13] for p in prices_15m}
                hourly_today_tomorrow = [
                    p for p in prices
                    if p["time"][:10] in (today_date, tomorrow_date)
                ]
                supplemented = 0
                for hp in hourly_today_tomorrow:
                    if hp["time"][:13] not in covered_hours:
                        prices_15m.append(hp)
                        supplemented += 1
                if supplemented:
                    prices_15m.sort(key=lambda p: p["time"])
                    print(
                        f"[ok] {supplemented} ontbrekende uren aangevuld met uurdata in prices_15m.",
                        file=sys.stderr,
                    )
        except Exception as exc:  # noqa: BLE001
            error_msg = f"ENTSO-E fout: {exc}"
            print(f"[warn] {error_msg}", file=sys.stderr)

    # Archiveer alle opgehaalde uurprijzen (alleen bij echte ENTSO-E data).
    # Dit gebeurt ook als prices leeg is na een fout — in dat geval is er niets te archiveren.
    if prices and source == "entsoe":
        try:
            archive_prices(prices)
        except Exception as exc:  # noqa: BLE001
            # Archiveren mag de hoofdflow nooit breken.
            print(f"[warn] Archiveren mislukt (niet kritiek): {exc}", file=sys.stderr)

    if not prices:
        # Probeer de bestaande prices.json te bewaren als die echte ENTSO-E data bevat.
        # Bezoekers krijgen dan verouderde echte data (met stale-banner na >6 uur)
        # in plaats van neppe sample-data. Sample-data is alleen voor de eerste run
        # of als er echt nog geen prices.json bestaat (ontwikkelmodus zonder token).
        preserved = False
        if OUTPUT_FILE.exists():
            try:
                existing = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
                if existing.get("source") == "entsoe":
                    if error_msg:
                        existing["last_error"] = error_msg
                    OUTPUT_FILE.write_text(
                        json.dumps(existing, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    print(
                        "[ok] ENTSO-E onbeschikbaar — bestaande prices.json met echte data "
                        "bewaard en last_error bijgewerkt.",
                        file=sys.stderr,
                    )
                    preserved = True
            except Exception as read_exc:  # noqa: BLE001
                print(f"[warn] Kon bestaande prices.json niet lezen: {read_exc}", file=sys.stderr)

        if preserved:
            return 0  # bestaande data is bewaard, niets meer te schrijven

        # Geen bestaande echte data beschikbaar: genereer sample (eerste run / dev zonder token).
        prices = generate_sample_prices(now_ams)
        prices_15m = generate_sample_prices_15m(now_ams)
        has_pt15m = True  # sample-data heeft altijd PT15M voor dev-gebruik
        source = "sample"
        print(f"[ok] {len(prices)} sample-uurprijzen + {len(prices_15m)} sample-kwartierdata gegenereerd.", file=sys.stderr)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "currency": "EUR",
        "unit": "EUR/MWh",
        "tz": "Europe/Amsterdam",
        "source": source,
        "has_pt15m": has_pt15m,
        "prices": prices,
        "prices_15m": prices_15m,  # Kwartierdata voor vandaag+morgen (leeg als PT60M of fout)
    }
    if error_msg:
        payload["last_error"] = error_msg

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[ok] Geschreven: {OUTPUT_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
