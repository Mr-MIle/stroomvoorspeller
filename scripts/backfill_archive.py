"""
backfill_archive.py — Eenmalige/incrementele historische backfill van ENTSO-E
day-ahead prijzen naar public/data/archief/ (maandbestanden YYYY-MM.json).

Hergebruikt de fetch-, parse-, aggregatie- en archiveer-logica uit fetch_prices.py,
zodat het archiefformaat exact gelijk blijft aan wat de reguliere cron schrijft.

CLI (afgestemd op .github/workflows/backfill-archive.yml):
    --from YYYY-MM-DD   Startdatum (inclusief). Verplicht in de praktijk.
    --to   YYYY-MM-DD   Einddatum (inclusief). Leeg/weggelaten = t/m gisteren.
    --force             Herhaal ook volledige maanden die al gearchiveerd zijn.
    --dry-run           Toon alleen het plan; haal niets op.
    --sleep N           Pauze (s) tussen requests (default 1.0).

(YYYY-MM wordt ook geaccepteerd en geinterpreteerd als de 1e van die maand.)

Gebruik via GitHub Actions: open Actions -> "Backfill historisch prijsarchief" ->
Run workflow -> vul start/eind in. De workflow gebruikt de ENTSOE_TOKEN-secret en
commit de nieuwe archiefbestanden zelf terug. Lokaal draaien kan ook:
    ENTSOE_TOKEN=xxx python scripts/backfill_archive.py --from 2015-01-01

Eigenschappen:
- HERVATBAAR: volledige maanden die al een archiefbestand hebben worden
  overgeslagen (tenzij --force). Rand-maanden (gedeeltelijk gevraagd) worden wel
  opnieuw opgehaald, maar archive_prices() is append-only met dedup, dus dat
  voegt alleen ontbrekende uren toe.
- VEILIG: raakt prices.json of het live-model NIET aan; schrijft alleen in archief/.
- BELEEFD: korte pauze tussen requests; chunkt per maand (ruim binnen de
  ENTSO-E 1-jaar-per-request-limiet).

NB: NL day-ahead data op ENTSO-E gaat ongeveer terug tot 2015. Maanden zonder
data leveren 0 punten en worden overgeslagen met een waarschuwing.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Zorg dat we fetch_prices.py kunnen importeren, ongeacht vanwaar het script start.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from fetch_prices import (  # noqa: E402  (import na sys.path-aanpassing)
    AMS_TZ,
    ARCHIVE_DIR,
    aggregate_to_hourly,
    archive_prices,
    fetch_entsoe_with_retry,
)


def parse_date_arg(value: str) -> datetime:
    """Parse 'YYYY-MM-DD' (of 'YYYY-MM') naar Amsterdam-datetime op 00:00 van die dag."""
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=AMS_TZ)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Ongeldige datum '{value}', verwacht YYYY-MM-DD (bv. 2015-01-01) of YYYY-MM."
    )


def month_floor(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month(dt: datetime) -> datetime:
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(month=dt.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def iter_month_chunks(from_ams: datetime, end_excl_ams: datetime):
    """Yield (chunk_start, chunk_end_excl, is_full_month) per maand tussen
    from_ams (inclusief) en end_excl_ams (exclusief), geklemd op de gevraagde range."""
    cur = month_floor(from_ams)
    while cur < end_excl_ams:
        m_start = cur
        m_end = next_month(cur)
        chunk_start = max(m_start, from_ams)
        chunk_end = min(m_end, end_excl_ams)
        is_full = (chunk_start == m_start) and (chunk_end == m_end)
        if chunk_start < chunk_end:
            yield chunk_start, chunk_end, is_full
        cur = m_end


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill ENTSO-E day-ahead prijzen naar archief/.")
    ap.add_argument("--from", dest="from_date", type=parse_date_arg,
                    default=parse_date_arg("2015-01-01"),
                    help="Startdatum (inclusief), YYYY-MM-DD. Default 2015-01-01.")
    ap.add_argument("--to", dest="to_date", type=parse_date_arg, default=None,
                    help="Einddatum (inclusief), YYYY-MM-DD. Leeg = t/m gisteren.")
    ap.add_argument("--force", action="store_true",
                    help="Herhaal ook volledige maanden die al gearchiveerd zijn.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Toon alleen welke maanden opgehaald zouden worden; haal niets op.")
    ap.add_argument("--sleep", type=float, default=1.0,
                    help="Pauze (seconden) tussen requests. Default 1.0.")
    args = ap.parse_args()

    now_ams = datetime.now(AMS_TZ)
    today_start = now_ams.replace(hour=0, minute=0, second=0, microsecond=0)

    from_ams = args.from_date
    # Exclusieve bovengrens: gevraagde einddag + 1 (inclusief eind), of vandaag 00:00 (t/m gisteren).
    if args.to_date is not None:
        end_excl = args.to_date + timedelta(days=1)
    else:
        end_excl = today_start  # t/m gisteren

    # Nooit verder dan vandaag 00:00 ophalen (morgen vult de gewone cron).
    if end_excl > today_start:
        end_excl = today_start

    if from_ams >= end_excl:
        print("[fout] Lege range: from {0:%Y-%m-%d} ligt op/na eind-exclusief {1:%Y-%m-%d}.".format(
            from_ams, end_excl), file=sys.stderr)
        return 2

    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if not token and not args.dry_run:
        print("[fout] Geen ENTSOE_TOKEN gevonden. Zet 'm in je omgeving, of gebruik --dry-run.",
              file=sys.stderr)
        return 2

    chunks = list(iter_month_chunks(from_ams, end_excl))
    last_day = end_excl - timedelta(days=1)
    print("[plan] {0} maand-chunks van {1:%Y-%m-%d} t/m {2:%Y-%m-%d} ({3}).".format(
        len(chunks), from_ams, last_day, "DRY-RUN" if args.dry_run else "live"), file=sys.stderr)

    total_added = 0
    fetched = skipped = empty = failed = 0

    for chunk_start, chunk_end, is_full in chunks:
        month_key = "{0:%Y-%m}".format(chunk_start)
        archive_file = ARCHIVE_DIR / (month_key + ".json")

        # Snelle resume: alleen volledige, al-gearchiveerde maanden overslaan.
        if is_full and archive_file.exists() and not args.force:
            skipped += 1
            print("[skip] {0} - volledige maand al gearchiveerd.".format(month_key), file=sys.stderr)
            continue

        if args.force and is_full and archive_file.exists():
            archive_file.unlink()  # schoon herschrijven bij --force op volledige maand

        start_utc = chunk_start.astimezone(timezone.utc)
        end_utc = chunk_end.astimezone(timezone.utc)

        if args.dry_run:
            tag = "volledig" if is_full else "deel"
            print("[zou ophalen] {0} ({1}): {2:%Y-%m-%d %H:%M}Z -> {3:%Y-%m-%d %H:%M}Z".format(
                month_key, tag, start_utc, end_utc), file=sys.stderr)
            continue

        try:
            raw = fetch_entsoe_with_retry(token, start_utc, end_utc)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("[fout] {0}: ophalen mislukt: {1}".format(month_key, exc), file=sys.stderr)
            time.sleep(args.sleep)
            continue

        if not raw:
            empty += 1
            print("[leeg] {0}: ENTSO-E gaf geen data (waarschijnlijk voor beschikbaarheid).".format(
                month_key), file=sys.stderr)
            time.sleep(args.sleep)
            continue

        hourly = aggregate_to_hourly(raw)
        archive_prices(hourly)  # groepeert per maand, append-only met dedup
        fetched += 1
        total_added += len(hourly)
        print("[ok] {0}: {1} ruwe punten -> {2} uurprijzen verwerkt.".format(
            month_key, len(raw), len(hourly)), file=sys.stderr)
        time.sleep(args.sleep)

    print("", file=sys.stderr)
    print("[klaar] opgehaald: {0} | overgeslagen: {1} | leeg: {2} | mislukt: {3} | "
          "~{4} uurprijzen verwerkt.".format(fetched, skipped, empty, failed, total_added),
          file=sys.stderr)
    if failed:
        print("[tip] Draai opnieuw om mislukte maanden alsnog op te halen "
              "(volledige maanden worden overgeslagen).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
