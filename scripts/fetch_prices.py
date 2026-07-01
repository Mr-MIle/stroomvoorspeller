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

# Config met belasting/opslag — gebruikt voor de 30-daagse referentieprijs (#63)
CONFIG_FILE = PROJECT_ROOT / "public" / "data" / "config.json"

# Map met de scripts zelf, voor sibling-imports (load_archive). Zelfde patroon als
# run_forecast.py / backtest.py.
_SCRIPT_DIR = Path(__file__).resolve().parent

# Amsterdam tijdzone via IANA zoneinfo (DST automatisch correct)
AMS_TZ = ZoneInfo("Europe/Amsterdam")

# Hoeveel dagen terug (vanaf vandaag) we bij elke run controleren op gaten, naast
# vandaag+morgen zelf. Vangt incidenten zoals 1 juli 2026 op: ENTSO-E's parser sloeg
# toen 30 juni + 1 juli stil over (TimeSeries zonder punten, zie parse_entsoe_xml)
# terwijl morgen gewoon compleet was — de oude check keek alléén naar morgen, dus dit
# gat werd nooit hersteld. 3 dagen geeft ruim marge boven dat 2-daagse gat, zonder de
# achtervang-APIs (die vooral recente data hebben) voor oude historie te belasten.
RECENT_CHECK_DAYS = 3


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


ENERGY_CHARTS_BASE = "https://api.energy-charts.info/price"


def fetch_epex(start_date: str, end_date: str) -> list[dict]:
    """Achtervang-bron: day-ahead prijzen via energy-charts.info (EPEX/SMARD-data).

    Geen API-key nodig. `start_date`/`end_date` zijn 'YYYY-MM-DD' (Amsterdam-dagen,
    inclusief). Gebruikt als ENTSO-E nog geen (volledige) morgen-prijzen heeft:
    leveranciers en EPEX publiceren doorgaans eerder dan het ENTSO-E Transparency
    Platform. Retourneert [{time: ISO Amsterdam, price: EUR/MWh}], gesorteerd.
    """
    params = {"bzn": "NL", "start": start_date, "end": end_date}
    url = f"{ENERGY_CHARTS_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "stroomvoorspeller/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    seconds = data.get("unix_seconds", []) or []
    prices = data.get("price", []) or []
    out: list[dict] = []
    for ts, pr in zip(seconds, prices):
        if pr is None:
            continue
        dt_utc = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        out.append({"time": utc_to_amsterdam(dt_utc).isoformat(), "price": round(float(pr), 2)})
    out.sort(key=lambda x: x["time"])
    return out


ENERGYZERO_BASE = "https://api.energyzero.nl/v1/energyprices"


def fetch_energyzero(start_date: str, end_date: str) -> list[dict]:
    """Achtervang-bron 1: day-ahead prijzen via EnergyZero (leverancier-API die o.a.
    ANWB Energie/jeprijs voedt). Geen API-key. Vaak iets eerder bijgewerkt dan
    ENTSO-E/SMARD. `start_date`/`end_date` = 'YYYY-MM-DD' (Amsterdam-dagen, inclusief).

    EnergyZero rapporteert EUR/kWh exclusief btw; we rekenen om naar EUR/MWh (×1000)
    zoals de rest van de pipeline. Retour: [{time: ISO Amsterdam, price: EUR/MWh}].
    """
    from_utc = datetime.fromisoformat(start_date).replace(tzinfo=AMS_TZ).astimezone(timezone.utc)
    till_utc = (
        datetime.fromisoformat(end_date)
        .replace(hour=23, minute=59, second=59, tzinfo=AMS_TZ)
        .astimezone(timezone.utc)
    )
    params = {
        "fromDate": from_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "tillDate": till_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z"),
        "interval": "4",      # uurprijzen
        "usageType": "1",     # elektriciteit
        "inclBtw": "false",   # kale marktprijs
    }
    url = f"{ENERGYZERO_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "stroomvoorspeller/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    out: list[dict] = []
    for row in data.get("Prices", []) or []:
        rd = row.get("readingDate")
        pr = row.get("price")
        if rd is None or pr is None:
            continue
        dt_utc = datetime.fromisoformat(rd.replace("Z", "+00:00"))
        out.append({"time": utc_to_amsterdam(dt_utc).isoformat(), "price": round(float(pr) * 1000.0, 2)})
    out.sort(key=lambda x: x["time"])
    return out


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
        end_el = period.find(f"{ns}timeInterval/{ns}end")
        period_start_utc = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        resolution = period.find(f"{ns}resolution").text  # bv. PT60M

        if resolution.endswith("M"):
            res_minutes = int(resolution.replace("PT", "").replace("M", ""))
        elif resolution.endswith("H"):
            res_minutes = int(resolution.replace("PT", "").replace("H", "")) * 60
        else:
            res_minutes = 60

        # ENTSO-E gebruikt curveType A03: opeenvolgende punten met dezelfde prijs
        # worden weggelaten. Een ontbrekende positie betekent dus "zelfde prijs als
        # het vorige gegeven punt", niet "geen data". We lezen eerst alle gegeven
        # punten in en vullen daarna de gaten op (forward-fill). Zonder dit levert
        # een vlakke dag 23 i.p.v. 24 uur op, en faalt de >=24-uur morgen-check.
        given: dict[int, float] = {}
        for point in period.findall(f"{ns}Point"):
            position = int(point.find(f"{ns}position").text)
            given[position] = float(point.find(f"{ns}price.amount").text)
        if not given:
            continue

        # Aantal verwachte posities: uit het tijdsinterval als dat er is, anders het
        # hoogste gegeven positienummer. Het interval staat in UTC, dus DST (23/25 uur)
        # blijft automatisch correct.
        n_positions = max(given)
        if end_el is not None and end_el.text:
            period_end_utc = datetime.fromisoformat(end_el.text.replace("Z", "+00:00"))
            span_minutes = (period_end_utc - period_start_utc).total_seconds() / 60
            n_positions = max(n_positions, int(round(span_minutes / res_minutes)))

        last_price: float | None = None
        for position in range(1, n_positions + 1):
            if position in given:
                last_price = given[position]
            if last_price is None:
                continue  # nog geen prijs gezien (zou niet mogen als positie 1 bestaat)
            point_utc = period_start_utc + timedelta(minutes=res_minutes * (position - 1))
            point_ams = utc_to_amsterdam(point_utc)
            results.append({"time": point_ams.isoformat(), "price": round(last_price, 2)})

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


def find_missing_days(prices: list[dict], start_date: str, end_date: str) -> list[str]:
    """Geeft de dagen (YYYY-MM-DD) in [start_date, end_date] die geen 24 volledige
    uurprijzen hebben in `prices`. Dient om gaten te vinden die de ENTSO-E-parser
    stilzwijgend kan laten vallen (parse_entsoe_xml: 'if not given: continue', geen
    foutmelding) — niet alleen in morgen, maar in elke dag van de gecheckte periode.
    """
    d0 = datetime.strptime(start_date, "%Y-%m-%d").date()
    d1 = datetime.strptime(end_date, "%Y-%m-%d").date()
    counts: dict[str, int] = {}
    for p in prices:
        day = p["time"][:10]
        counts[day] = counts.get(day, 0) + 1
    missing = []
    d = d0
    while d <= d1:
        key = d.isoformat()
        if counts.get(key, 0) < 24:
            missing.append(key)
        d += timedelta(days=1)
    return missing


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


def compute_avg_30d(now_ams: datetime, prices: list[dict]) -> tuple[float | None, str | None]:
    """Bereken het 30-daags gemiddelde van de consumentenprijs (ct/kWh incl. btw).

    Voor de referentielijn 'wat is normaal' (#63). Combineert het historische
    archief met de uurprijzen die in deze run zijn opgehaald, zodat er ook zonder
    volledig archief een zinvol getal ontstaat. De toekomst (vandaag + morgen)
    telt bewust niet mee: dit is een achterwaarts gemiddelde t/m gisteren.

    Retourneert (None, None) bij te weinig data of een leesfout — het veld wordt
    dan weggelaten en de frontend toont simpelweg geen referentielijn.
    """
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] avg_30d: config niet leesbaar ({exc})", file=sys.stderr)
        return None, None

    taxes = cfg.get("taxes", {})
    eb_per_kwh = float(taxes.get("energiebelasting_per_kwh", 0.0))
    btw = float(taxes.get("btw_factor", 1.21))
    # Gemiddelde leverancieropslag uit de 'average'-aanbieder.
    markup = 0.0
    for s in cfg.get("suppliers", []):
        if s.get("id") == "average":
            markup = float(s.get("markup_per_kwh", 0.0))
            break

    end_ams = now_ams.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ams = end_ams - timedelta(days=30)

    # Dedup per uur ("YYYY-MM-DDTHH"); in-memory data overschrijft archief.
    hourly: dict[str, float] = {}

    # 1) Archief (historische uurprijzen EUR/MWh).
    try:
        if str(_SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(_SCRIPT_DIR))
        from load_archive import load_range  # noqa: E402
        for p in load_range(start_ams, end_ams):
            hourly[p["time"][:13]] = float(p["price"])
    except Exception as exc:  # noqa: BLE001
        print(f"[info] avg_30d: archief niet bruikbaar ({exc}); val terug op run-data.",
              file=sys.stderr)

    # 2) In-memory historie uit deze run (vult/overschrijft recente uren; geen toekomst).
    for p in prices:
        try:
            dt = datetime.fromisoformat(p["time"])
        except (ValueError, KeyError):
            continue
        dt = dt.astimezone(AMS_TZ) if dt.tzinfo else dt.replace(tzinfo=AMS_TZ)
        if start_ams <= dt < end_ams:
            hourly[p["time"][:13]] = float(p["price"])

    vals = list(hourly.values())
    if len(vals) < 48:  # minder dan twee dagen data: niet zinvol als 'normaal'
        print(f"[info] avg_30d: te weinig data ({len(vals)} uur) — veld overgeslagen.",
              file=sys.stderr)
        return None, None

    avg_eur_mwh = sum(vals) / len(vals)
    # EUR/MWh -> ct/kWh kale beursprijs: /10. Opslag + energiebelasting in ct, dan btw.
    avg_incl_ct = (avg_eur_mwh / 10.0 + markup * 100.0 + eb_per_kwh * 100.0) * btw
    window = f"{start_ams:%Y-%m-%d} – {end_ams:%Y-%m-%d}"
    print(f"[ok] avg_30d: {round(avg_incl_ct, 1)} ct/kWh over {len(vals)} uur "
          f"({window}).", file=sys.stderr)
    return round(avg_incl_ct, 1), window


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
    fallback_source: str | None = None

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

    # ---- Achtervang als ENTSO-E (deels) ontbreekt ----
    # Volgorde: EnergyZero (leverancier-API, vaak eerder) → energy-charts (SMARD).
    # Beide geven dezelfde SDAC-uitslag via een andere pijplijn; op een dag waarop de
    # markt zélf laat publiceert hebben ze allebei nog niets.
    fallback_sources = (("energyzero", fetch_energyzero), ("energy-charts", fetch_epex))

    # Scenario a: ENTSO-E leverde data, maar er zitten gaten in de opgehaalde periode.
    # Vroeger checkten we hier alléén morgen — maar op 1 juli 2026 sloeg de parser
    # 30 juni + 1 juli (vandaag) stil over terwijl morgen gewoon compleet was, en
    # dat gat werd nooit hersteld (de site toonde daardoor alleen "morgen" in de
    # grafiek). Daarom nu: check elke dag van vandaag-RECENT_CHECK_DAYS t/m morgen,
    # en vul elk gat aan via de achtervang — belangrijk: eerst naar uur aggregeren
    # (energy-charts levert kwartierdata) vóór het mergen, anders komen er meerdere
    # punten per uur in de uur-reeks.
    if source == "entsoe":
        recent_start_date = (today_start_ams - timedelta(days=RECENT_CHECK_DAYS)).strftime("%Y-%m-%d")
        missing_days = find_missing_days(prices, recent_start_date, tomorrow_date)
        if missing_days:
            print(
                f"[info] ENTSO-E-data onvolledig voor: {', '.join(missing_days)} — "
                f"achtervang bevragen.", file=sys.stderr,
            )
            for name, fn in fallback_sources:
                if not missing_days:
                    break
                gap_start, gap_end = missing_days[0], missing_days[-1]
                try:
                    raw = fn(gap_start, gap_end)
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] achtervang {name} mislukt: {exc}", file=sys.stderr)
                    continue
                hourly = aggregate_to_hourly(raw)
                have_hours = {p["time"][:13] for p in prices}
                added = [p for p in hourly if p["time"][:13] not in have_hours]
                if added:
                    prices.extend(added)
                    prices.sort(key=lambda x: x["time"])
                    fallback_source = f"{fallback_source}+{name}" if fallback_source else name
                    print(
                        f"[ok] Achtervang {name}: {len(added)} uren aangevuld "
                        f"({gap_start}..{gap_end}).", file=sys.stderr,
                    )
                else:
                    print(f"[wait] Achtervang {name} had niets nieuws voor {gap_start}..{gap_end}.", file=sys.stderr)
                missing_days = find_missing_days(prices, recent_start_date, tomorrow_date)
            if missing_days:
                # Alle achtervang geprobeerd en er blijft een gat over — dit mag niet
                # stil verdwijnen zoals op 1 juli. Vlag het in last_error zodat het
                # zichtbaar wordt in payload.last_error i.p.v. dat de site zwijgend
                # een lege dag toont.
                warn = f"gaten in prijsdata na achtervang: {', '.join(missing_days)}"
                print(f"[warn] {warn}", file=sys.stderr)
                error_msg = f"{error_msg}; {warn}" if error_msg else warn

    # Scenario b: ENTSO-E leverde helemaal niets, maar er was wél een token (dus
    # productie, geen dev). Haal de hele periode — inclusief historie voor de forecast —
    # bij de eerste achtervang die werkt, vóór we oude data bewaren of sample genereren.
    if not prices and token:
        for name, fn in fallback_sources:
            try:
                raw = fn(history_start_ams.strftime("%Y-%m-%d"), tomorrow_date)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] volledige achtervang {name} mislukt: {exc}", file=sys.stderr)
                continue
            hourly = aggregate_to_hourly(raw)
            if hourly:
                prices = hourly
                source = name
                fallback_source = name
                if error_msg:
                    error_msg = f"{error_msg}; achtervang {name} gebruikt"
                print(f"[ok] ENTSO-E leeg — {len(prices)} uurprijzen via achtervang {name}.", file=sys.stderr)
                break

    # Archiveer alle opgehaalde uurprijzen (alleen bij echte day-ahead data).
    # Dit gebeurt ook als prices leeg is na een fout — in dat geval is er niets te archiveren.
    if prices and source in ("entsoe", "energyzero", "energy-charts"):
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
        "fallback_source": fallback_source,
        "has_pt15m": has_pt15m,
        "prices": prices,
        "prices_15m": prices_15m,  # Kwartierdata voor vandaag+morgen (leeg als PT60M of fout)
    }

    # Referentielijn 'wat is normaal' (#63): 30-daags gemiddelde consumentenprijs.
    # Alleen bij echte day-ahead data — over sample-data is het gemiddelde betekenisloos.
    if source in ("entsoe", "energyzero", "energy-charts"):
        try:
            avg_30d, avg_window = compute_avg_30d(now_ams, prices)
            if avg_30d is not None:
                payload["avg_30d_inclusive_ct"] = avg_30d
                payload["avg_30d_window"] = avg_window
        except Exception as exc:  # noqa: BLE001
            # Mag de hoofdflow nooit breken.
            print(f"[warn] avg_30d berekenen mislukt (niet kritiek): {exc}", file=sys.stderr)

    if error_msg:
        payload["last_error"] = error_msg

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[ok] Geschreven: {OUTPUT_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
