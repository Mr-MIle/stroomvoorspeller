"""
backfill_archive.py — Vul het historische prijsarchief met ENTSO-E data.

Gebruik:
    ENTSOE_TOKEN=xxx python scripts/backfill_archive.py --from 2026-04-01 --to 2026-05-21

Werking:
  - Haalt ENTSO-E day-ahead prijzen op per maandchunk (max ~32 dagen per API-call).
  - Schrijft resultaat naar public/data/archief/YYYY-MM.json via dezelfde
    archive_prices()-logica als fetch_prices.py — append-only, geen duplicaten.
  - Bestaande uren in het archief worden nooit overschreven.
  - Als --to niet opgegeven is, wordt gisteren gebruikt.

Normaal gebruik: eenmalig uitvoeren via de backfill-workflow in GitHub Actions.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Importeer gedeelde functies uit fetch_prices.py (zit in dezelfde map)
sys.path.insert(0, str(Path(__file__).parent))
from fetch_prices import (
    amsterdam_now,
    fetch_entsoe_with_retry,
    aggregate_to_hourly,
    archive_prices,
    utc_to_amsterdam,
    AMS_OFFSET_WINTER,
    AMS_OFFSET_SUMMER,
)


def parse_date(date_str: str) -> datetime:
    """Parseer YYYY-MM-DD naar een Amsterdam-middernacht datetime."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    # Bepaal Amsterdam-offset voor die datum
    year = dt.year
    march = datetime(year, 3, 31, 1, 0, tzinfo=timezone.utc)
    while march.weekday() != 6:
        march -= timedelta(days=1)
    october = datetime(year, 10, 31, 1, 0, tzinfo=timezone.utc)
    while october.weekday() != 6:
        october -= timedelta(days=1)
    # Schatting: is 00:00 lokaal zomer- of wintertijd?
    approx_utc = dt.replace(tzinfo=timezone.utc)
    is_dst = march <= approx_utc < october
    offset = AMS_OFFSET_SUMMER if is_dst else AMS_OFFSET_WINTER
    return dt.replace(tzinfo=timezone(offset))


def month_chunks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Splits het datumbereik in maandchunks.

    Elke chunk loopt van de eerste dag van de maand (of start) tot de eerste dag
    van de volgende maand (of end). ENTSO-E accepteert maximaal ~1 jaar per request,
    maar maandchunks zijn netjes en vermijden time-out risico's.
    """
    chunks = []
    current = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while current < end:
        # Eerste dag van volgende maand
        if current.month == 12:
            next_month = current.replace(year=current.year + 1, month=1)
        else:
            next_month = current.replace(month=current.month + 1)

        chunk_start = max(current, start)
        chunk_end = min(next_month, end)
        chunks.append((chunk_start, chunk_end))
        current = next_month
    return chunks


def main() -> int:
    import os

    parser = argparse.ArgumentParser(description="Backfill historisch prijsarchief vanuit ENTSO-E.")
    parser.add_argument(
        "--from", dest="from_date", required=True,
        help="Startdatum (inclusief), formaat YYYY-MM-DD. Bijv. 2026-04-01"
    )
    parser.add_argument(
        "--to", dest="to_date", default=None,
        help="Einddatum (exclusief, dus tot middernacht van deze datum). "
             "Standaard: gisteren 23:59 (= tot en met gisteren volledig)."
    )
    args = parser.parse_args()

    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if not token:
        print("[fout] ENTSOE_TOKEN niet ingesteld. Zet de omgevingsvariabele en probeer opnieuw.", file=sys.stderr)
        return 1

    now_ams = amsterdam_now()

    start = parse_date(args.from_date)

    if args.to_date:
        # Einddatum is exclusief: we halen t/m de dag vóór op
        end_raw = parse_date(args.to_date)
        # Verschuif naar middernacht van de dag ná to_date zodat to_date zelf volledig meegenomen wordt
        end = end_raw + timedelta(days=1)
    else:
        # Standaard: t/m gisteren volledig (vandaag loopt al via de reguliere fetch)
        yesterday = now_ams.replace(hour=0, minute=0, second=0, microsecond=0)
        end = yesterday

    if start >= end:
        print(f"[fout] Startdatum {start.date()} ligt niet vóór einddatum {end.date()}.", file=sys.stderr)
        return 1

    print(f"[info] Backfill van {start.date()} t/m {(end - timedelta(days=1)).date()}", file=sys.stderr)

    chunks = month_chunks(start, end)
    print(f"[info] {len(chunks)} maandchunk(s) te verwerken: "
          f"{', '.join(f'{s.strftime('%Y-%m')}' for s, _ in chunks)}", file=sys.stderr)

    total_archived = 0
    errors = 0

    for chunk_start, chunk_end in chunks:
        label = chunk_start.strftime("%Y-%m")
        print(f"\n[chunk] {label}: {chunk_start.date()} → {(chunk_end - timedelta(seconds=1)).date()}", file=sys.stderr)

        start_utc = chunk_start.astimezone(timezone.utc)
        end_utc = chunk_end.astimezone(timezone.utc)

        try:
            raw = fetch_entsoe_with_retry(token, start_utc, end_utc)
            print(f"[ok]    {len(raw)} ruwe punten opgehaald.", file=sys.stderr)

            hourly = aggregate_to_hourly(raw)
            print(f"[ok]    Geaggregeerd naar {len(hourly)} uurpunten.", file=sys.stderr)

            archive_prices(hourly)
            total_archived += len(hourly)

        except Exception as exc:  # noqa: BLE001
            print(f"[warn]  Chunk {label} mislukt: {exc}", file=sys.stderr)
            errors += 1

    print(f"\n[klaar] {total_archived} uurprijzen gearchiveerd. {errors} chunk(s) mislukt.", file=sys.stderr)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
