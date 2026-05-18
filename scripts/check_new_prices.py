"""
check_new_prices.py — Lichte check: heeft ENTSO-E morgen-prijzen die nog niet in
prices.json staan?

Exit 0  → nieuwe prijzen beschikbaar  → run de volledige pipeline
Exit 1  → prices.json is al up-to-date, of ENTSO-E heeft nog niets → skip

Gebruik:
    ENTSOE_TOKEN=xxx python scripts/check_new_prices.py

Logica:
1. Bepaal "morgen" in Amsterdam-tijd.
2. Als prices.json al ≥24 uurprijzen van source=entsoe heeft voor morgen → exit 1.
3. Vraag ENTSO-E om de eerste 2 uur van morgen (minimale API-call).
4. Als ENTSO-E data teruggeeft → exit 0 (pipeline starten).
5. Anders → exit 1 (nog niet gepubliceerd).
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


def prices_json_has_tomorrow(tomorrow: str) -> bool:
    """
    True als prices.json al ≥24 uurprijzen van source=entsoe heeft voor morgen.
    Dan is er niets te doen.
    """
    if not OUTPUT_FILE.exists():
        return False
    try:
        data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        if data.get("source") != "entsoe":
            return False
        count = sum(1 for p in data.get("prices", []) if p.get("time", "")[:10] == tomorrow)
        return count >= 24
    except Exception as exc:
        print(f"[warn] Kon prices.json niet lezen: {exc}", file=sys.stderr)
        return False


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
        with urllib.request.urlopen(req, timeout=15) as resp:
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
        elif exc.code == 400:
            # ENTSO-E geeft HTTP 400 terug als er geen data is voor de gevraagde periode
            print(f"[wait] ENTSO-E HTTP 400 — nog geen data voor {tomorrow}.", file=sys.stderr)
        else:
            print(f"[warn] ENTSO-E HTTP {exc.code}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"[warn] ENTSO-E verbindingsfout: {exc}", file=sys.stderr)
        return False


def main() -> None:
    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if not token:
        print("[skip] Geen ENTSOE_TOKEN ingesteld — check overgeslagen.", file=sys.stderr)
        sys.exit(1)

    tomorrow = tomorrow_date_ams()
    print(f"[check] Morgen in Amsterdam-tijd = {tomorrow}", file=sys.stderr)

    if prices_json_has_tomorrow(tomorrow):
        print(
            f"[skip] prices.json heeft al ≥24 uurprijzen voor {tomorrow}. Niets te doen.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[check] prices.json mist morgen-prijzen. Controleer ENTSO-E…", file=sys.stderr)

    if entsoe_has_tomorrow(token, tomorrow):
        print("[go] Pipeline starten.", file=sys.stderr)
        sys.exit(0)
    else:
        print("[wait] ENTSO-E heeft nog niets. Volgende check over 15 minuten.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
