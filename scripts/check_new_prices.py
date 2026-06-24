"""
check_new_prices.py — Lichte check: heeft ENTSO-E morgen-prijzen die nog niet in
prices.json staan?

Exit 0  → nieuwe prijzen beschikbaar  → run de volledige pipeline
Exit 1  → prices.json is al up-to-date, of ENTSO-E heeft nog niets → skip

Gebruik:
    ENTSOE_TOKEN=xxx python scripts/check_new_prices.py

Logica:
1. Bepaal "morgen" in Amsterdam-tijd.
2. Als prices.json al ≥24 uurprijzen voor morgen heeft → exit 1.
3. Vraag ENTSO-E om de eerste 2 uur van morgen (minimale API-call).
4. Als ENTSO-E data teruggeeft → exit 0 (pipeline starten).
5. Anders: vraag de achtervang op volgorde — EnergyZero (leverancier-API, vaak eerder),
   dan energy-charts (SMARD). Geen API-key. Heeft één van beide de volledige dag →
   exit 0; fetch_prices.py vult morgen dan via diezelfde volgorde aan.
6. Anders → exit 1 (nog niet gepubliceerd).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

NL_EIC = "10YNL----------L"
DOC_TYPE_DAY_AHEAD = "A44"
ENTSOE_BASE = "https://web-api.tp.entsoe.eu/api"
ENERGY_CHARTS_BASE = "https://api.energy-charts.info/price"
ENERGYZERO_BASE = "https://api.energyzero.nl/v1/energyprices"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = PROJECT_ROOT / "public" / "data" / "prices.json"


def amsterdam_offset() -> timedelta:
    """Simpele DST-benadering voor Amsterdam (geen externe lib nodig)."""
    now_utc = datetime.now(timezone.utc)
    year = now_utc.year
    # Laatste zondag maart 01:00 UTC = begin zomertijd
    march = datetime(year, 3, 31, 1, 0, tzinfo=timezone.utc)
    while march.weekday() != 6:
        march -= timedelta(days=1)
    # Laatste zondag oktober 01:00 UTC = einde zomertijd
    october = datetime(year, 10, 31, 1, 0, tzinfo=timezone.utc)
    while october.weekday() != 6:
        october -= timedelta(days=1)
    return timedelta(hours=2) if march <= now_utc < october else timedelta(hours=1)


def tomorrow_date_ams() -> str:
    """Geeft 'morgen' als YYYY-MM-DD string in Amsterdam-tijd."""
    offset = amsterdam_offset()
    now_ams = datetime.now(timezone.utc) + offset
    return (now_ams + timedelta(days=1)).strftime("%Y-%m-%d")


def prices_json_tomorrow_source(tomorrow: str) -> str | None:
    """
    Geeft de bron ('entsoe' / 'energyzero' / 'energy-charts') waarmee morgen al
    volledig (>=24 uur) in prices.json staat, of None als morgen er (nog) niet
    volledig in staat. Zo kan de caller onderscheid maken tussen "al via ENTSO-E"
    (klaar) en "via achtervang" (mag nog naar ENTSO-E upgraden).
    """
    if not OUTPUT_FILE.exists():
        return None
    try:
        data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        src = data.get("source")
        if src not in ("entsoe", "energyzero", "energy-charts"):
            return None
        count = sum(1 for p in data.get("prices", []) if p.get("time", "")[:10] == tomorrow)
        return src if count >= 24 else None
    except Exception as exc:
        print(f"[warn] Kon prices.json niet lezen: {exc}", file=sys.stderr)
        return None


def entsoe_has_tomorrow(token: str, tomorrow: str) -> bool:
    """
    True als ENTSO-E al day-ahead prijzen heeft gepubliceerd voor morgen.
    Vraagt slechts 2 uur op om de API-belasting minimaal te houden.
    """
    offset = amsterdam_offset()
    tz = timezone(offset)
    tomorrow_dt = datetime.strptime(tomorrow, "%Y-%m-%d").replace(tzinfo=tz)
    start_utc = tomorrow_dt.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(hours=2)

    params = {
        "documentType": DOC_TYPE_DAY_AHEAD,
        "in_Domain": NL_EIC,
        "out_Domain": NL_EIC,
        "periodStart": start_utc.strftime("%Y%m%d%H%M"),
        "periodEnd": end_utc.strftime("%Y%m%d%H%M"),
        "securityToken": token,
    }
    url = f"{ENTSOE_BASE}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "stroomvoorspeller/0.1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
        # Als er een <TimeSeries> in de XML zit, zijn er prijzen
        has_data = "<TimeSeries>" in body
        if has_data:
            print(f"[go] ENTSO-E heeft prijzen voor {tomorrow}.", file=sys.stderr)
        else:
            print(f"[wait] ENTSO-E XML bevat geen TimeSeries voor {tomorrow}.", file=sys.stderr)
        return has_data
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print("[error] ENTSO-E: token ongeldig of verlopen (HTTP 401).", file=sys.stderr)
            return False
        elif exc.code == 400:
            # ENTSO-E geeft HTTP 400 terug als er geen data is voor de gevraagde periode
            print(f"[wait] ENTSO-E HTTP 400 — nog geen data voor {tomorrow}.", file=sys.stderr)
            return False
        elif exc.code in (502, 503, 504):
            # Server tijdelijk onbereikbaar — dit betekent NIET dat er geen data is.
            # Geef True terug zodat de volledige pipeline draait; fetch_prices.py heeft
            # eigen retry-logica (3 pogingen) en kan de data alsnog ophalen.
            print(
                f"[uncertain] ENTSO-E HTTP {exc.code} — server onbereikbaar maar data "
                f"kan er al zijn. Pipeline starten met retries.",
                file=sys.stderr,
            )
            return True
        else:
            print(f"[warn] ENTSO-E HTTP {exc.code}", file=sys.stderr)
            return False
    except Exception as exc:
        # Verbindingsfout of timeout: we weten niet of data beschikbaar is.
        # Behandel net als HTTP 502/503/504 — start de pipeline toch, want
        # fetch_prices.py heeft eigen retry-logica (3 pogingen) en kan de data
        # alsnog ophalen, of bewaart de bestaande prices.json als fallback.
        print(
            f"[uncertain] ENTSO-E verbindingsfout: {exc} — kan niet verifiëren of data "
            f"beschikbaar is. Pipeline starten met retries.",
            file=sys.stderr,
        )
        return True


def energyzero_has_tomorrow(tomorrow: str) -> bool:
    """
    True als EnergyZero (leverancier-API, voedt o.a. ANWB/jeprijs) al een volledige dag
    day-ahead prijzen voor morgen heeft. Geen API-key. Eerste achtervang: vaak iets
    eerder dan ENTSO-E/SMARD. Telt unieke uren.
    """
    offset = amsterdam_offset()
    tz = timezone(offset)
    start_ams = datetime.strptime(tomorrow, "%Y-%m-%d").replace(tzinfo=tz)
    from_utc = start_ams.astimezone(timezone.utc)
    till_utc = (start_ams + timedelta(hours=23, minutes=59, seconds=59)).astimezone(timezone.utc)
    params = {
        "fromDate": from_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "tillDate": till_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z"),
        "interval": "4",
        "usageType": "1",
        "inclBtw": "false",
    }
    url = f"{ENERGYZERO_BASE}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "stroomvoorspeller/0.1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        hours = {
            r.get("readingDate", "")[:13]
            for r in (data.get("Prices") or [])
            if r.get("price") is not None
        }
        has_data = len(hours) >= 24
        if has_data:
            print(f"[go] EnergyZero heeft {len(hours)} uren voor {tomorrow}.", file=sys.stderr)
        else:
            print(
                f"[wait] EnergyZero heeft nog geen volledige dag voor {tomorrow} "
                f"({len(hours)} uren).",
                file=sys.stderr,
            )
        return has_data
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] EnergyZero check mislukt: {exc}", file=sys.stderr)
        return False


def epex_has_tomorrow(tomorrow: str) -> bool:
    """
    True als de EPEX-achtervang (energy-charts.info) al een volledige dag day-ahead
    prijzen voor morgen heeft. Geen API-key nodig. Gebruikt als ENTSO-E nog niets
    heeft: EPEX/leveranciers publiceren doorgaans eerder dan het ENTSO-E Transparency
    Platform, dus dit vangt de uren op dat de markt de prijzen wél heeft maar ENTSO-E
    nog niet. Telt unieke uren (robuust voor zowel PT60M als PT15M).
    """
    params = {"bzn": "NL", "start": tomorrow, "end": tomorrow}
    url = f"{ENERGY_CHARTS_BASE}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "stroomvoorspeller/0.1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        seconds = data.get("unix_seconds", []) or []
        prices = data.get("price", []) or []
        hours = set()
        for ts, pr in zip(seconds, prices):
            if pr is None:
                continue
            hours.add(datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y%m%d%H"))
        has_data = len(hours) >= 24
        if has_data:
            print(f"[go] energy-charts heeft {len(hours)} uren voor {tomorrow}.", file=sys.stderr)
        else:
            print(
                f"[wait] energy-charts heeft nog geen volledige dag voor {tomorrow} "
                f"({len(hours)} uren).",
                file=sys.stderr,
            )
        return has_data
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] energy-charts check mislukt: {exc}", file=sys.stderr)
        return False


def main() -> None:
    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if not token:
        print("[skip] Geen ENTSOE_TOKEN ingesteld — check overgeslagen.", file=sys.stderr)
        sys.exit(1)

    tomorrow = tomorrow_date_ams()
    print(f"[check] Morgen in Amsterdam-tijd = {tomorrow}", file=sys.stderr)

    current_source = prices_json_tomorrow_source(tomorrow)

    # Morgen staat al volledig via ENTSO-E -> klaar, niets te doen.
    if current_source == "entsoe":
        print(f"[skip] prices.json heeft al ENTSO-E-prijzen voor {tomorrow}. Niets te doen.",
              file=sys.stderr)
        sys.exit(1)

    # Morgen staat er, maar via een achtervang. ENTSO-E is leidend: zodra ENTSO-E de
    # data heeft, draaien we opnieuw zodat ENTSO-E de achtervang overschrijft (incl.
    # kwartierdata). Heeft ENTSO-E nog niets, dan laten we de achtervang staan.
    if current_source in ("energyzero", "energy-charts"):
        print(f"[check] Morgen staat in prices.json via achtervang ({current_source}). "
              f"Controleer of ENTSO-E kan upgraden...", file=sys.stderr)
        if entsoe_has_tomorrow(token, tomorrow):
            print("[go] ENTSO-E heeft morgen nu — upgrade van achtervang naar ENTSO-E.",
                  file=sys.stderr)
            sys.exit(0)
        print("[skip] ENTSO-E nog niet beschikbaar; achtervang blijft staan. Niets te doen.",
              file=sys.stderr)
        sys.exit(1)

    print("[check] prices.json mist morgen-prijzen. Controleer ENTSO-E...", file=sys.stderr)

    if entsoe_has_tomorrow(token, tomorrow):
        print("[go] Pipeline starten.", file=sys.stderr)
        sys.exit(0)

    # ENTSO-E heeft nog niets — probeer de achtervang op volgorde: EnergyZero (vaak
    # eerder), dan energy-charts. Heeft een van beide morgen, dan draaien we de pipeline
    # toch: fetch_prices.py vult morgen via diezelfde volgorde aan.
    print("[check] ENTSO-E nog leeg. Controleer achtervang (EnergyZero -> energy-charts)...",
          file=sys.stderr)
    if energyzero_has_tomorrow(tomorrow):
        print("[go] EnergyZero heeft morgen — pipeline starten.", file=sys.stderr)
        sys.exit(0)
    if epex_has_tomorrow(tomorrow):
        print("[go] energy-charts heeft morgen — pipeline starten.", file=sys.stderr)
        sys.exit(0)

    print("[wait] ENTSO-E, EnergyZero en energy-charts hebben nog niets. "
          "Volgende check over 15 minuten.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
