"""
load_archive.py — Leeshulp voor het historische prijsarchief (public/data/archief/).

Het archief bestaat uit maandbestanden YYYY-MM.json met uurprijzen in EUR/MWh
(Amsterdam-lokale ISO-tijden), geschreven door fetch_prices.archive_prices() en
backfill_archive.py. Deze module geeft forecast-/backtest-code toegang tot dat
archief zonder kennis van de bestandsindeling.

Belangrijkste functies:
    load_range(start, end)              -> uurprijzen tussen twee datums (incl./excl.)
    load_same_period(target, years, w)  -> "zelfde kalenderweek, vorige jaren" baseline

Alle functies geven lijsten van {"time": iso-string, "price": float} terug,
gesorteerd op tijd en ontdubbeld. Ontbrekende maanden/uren worden stil overgeslagen.

Geen externe dependencies; alleen de standaardlibrary.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

AMS_TZ = ZoneInfo("Europe/Amsterdam")

# Standaard archief-locatie (zelfde als fetch_prices.ARCHIVE_DIR).
_DEFAULT_ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "public" / "data" / "archief"


def _to_ams(dt: datetime) -> datetime:
    """Zorg dat dt een Amsterdam-aware datetime is."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=AMS_TZ)
    return dt.astimezone(AMS_TZ)


def _month_keys(start_ams: datetime, end_ams: datetime) -> list[str]:
    """Lijst van 'YYYY-MM' maand-keys die het bereik [start, end] raken (inclusief)."""
    keys: list[str] = []
    cur = start_ams.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = end_ams.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur <= last:
        keys.append(f"{cur:%Y-%m}")
        cur = (cur.replace(day=28) + timedelta(days=7)).replace(day=1)
    return keys


def _read_month(archive_dir: Path, month_key: str) -> list[dict]:
    """Lees één maandbestand; lege lijst als het niet bestaat of corrupt is."""
    f = archive_dir / f"{month_key}.json"
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data.get("prices", [])
    except Exception as exc:  # noqa: BLE001
        print(f"[load_archive] kon {f.name} niet lezen: {exc}", file=sys.stderr)
        return []


def load_range(
    start,
    end,
    *,
    archive_dir: Path | str | None = None,
    inclusive_end: bool = False,
) -> list[dict]:
    """Geef alle uurprijzen tussen start en end uit het archief.

    start, end : datetime of 'YYYY-MM-DD'-string (Amsterdam). end is standaard
                 exclusief; zet inclusive_end=True om de einddag mee te nemen.
    Resultaat  : gesorteerde, ontdubbelde lijst van {"time", "price"}.
    """
    adir = Path(archive_dir) if archive_dir else _DEFAULT_ARCHIVE_DIR
    if isinstance(start, str):
        start = datetime.strptime(start, "%Y-%m-%d")
    if isinstance(end, str):
        end = datetime.strptime(end, "%Y-%m-%d")
    start_ams = _to_ams(start)
    end_ams = _to_ams(end)
    if inclusive_end:
        end_ams = end_ams + timedelta(days=1)

    seen: set[str] = set()
    out: list[dict] = []
    for key in _month_keys(start_ams, end_ams):
        for p in _read_month(adir, key):
            t = p.get("time", "")
            try:
                dt = datetime.fromisoformat(t)
            except ValueError:
                continue
            if start_ams <= dt < end_ams and t[:19] not in seen:
                seen.add(t[:19])
                out.append({"time": t, "price": float(p["price"])})
    out.sort(key=lambda x: x["time"])
    return out


def load_same_period(
    target,
    *,
    years_back: int = 3,
    window_days: int = 7,
    archive_dir: Path | str | None = None,
) -> list[dict]:
    """Verzamel uurprijzen rond dezelfde kalenderdatum in de voorgaande N jaren.

    Voor target-datum D pakt deze functie, voor elk van de vorige `years_back`
    jaren, het venster [D - window_days, D + window_days] (zelfde maand/dag, ander
    jaar). Handig als seizoens-baseline: hoe gedroegen de prijzen zich rond deze
    tijd van het jaar in eerdere jaren?

    Let op: het lopende jaar zelf wordt NIET meegenomen (dat is je korte venster
    in forecast.py al). Schrikkeldag 29-2 valt terug op 28-2 in niet-schrikkeljaren.
    """
    if isinstance(target, str):
        target = datetime.strptime(target[:10], "%Y-%m-%d")
    target = _to_ams(target)

    out: list[dict] = []
    seen: set[str] = set()
    for k in range(1, years_back + 1):
        year = target.year - k
        try:
            anchor = target.replace(year=year)
        except ValueError:
            # 29 feb in een niet-schrikkeljaar -> 28 feb
            anchor = target.replace(year=year, day=28)
        start = anchor - timedelta(days=window_days)
        end = anchor + timedelta(days=window_days + 1)  # exclusief -> +1 dag
        for p in load_range(start, end, archive_dir=archive_dir):
            if p["time"][:19] not in seen:
                seen.add(p["time"][:19])
                out.append(p)
    out.sort(key=lambda x: x["time"])
    return out


def archive_coverage(archive_dir: Path | str | None = None) -> dict:
    """Snelle dekkingscheck: aantal maanden, eerste/laatste maand, totaal uren."""
    adir = Path(archive_dir) if archive_dir else _DEFAULT_ARCHIVE_DIR
    if not adir.exists():
        return {"months": 0, "first": None, "last": None, "hours": 0}
    files = sorted(adir.glob("*.json"))
    total = 0
    for f in files:
        try:
            total += len(json.loads(f.read_text(encoding="utf-8")).get("prices", []))
        except Exception:  # noqa: BLE001
            pass
    return {
        "months": len(files),
        "first": files[0].stem if files else None,
        "last": files[-1].stem if files else None,
        "hours": total,
    }


if __name__ == "__main__":
    # Kleine zelftest / dekkingsrapport bij direct aanroepen.
    cov = archive_coverage()
    print(f"Archiefdekking: {cov['months']} maanden "
          f"({cov['first']} t/m {cov['last']}), {cov['hours']} uurprijzen.")
