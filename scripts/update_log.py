"""
update_log.py — Vul 'actual' prijzen in de prediction_log.json.

Draait na fetch_prices.py in de GitHub Actions workflow.
Zoekt entries in prediction_log.json waar actual == null en het uur
al is verstreken (prijs gepubliceerd in prices.json).

Gebruik: python update_log.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRICES_FILE = PROJECT_ROOT / "public" / "data" / "prices.json"
PREDICTION_LOG_FILE = PROJECT_ROOT / "03-data" / "prediction_log.json"


def main() -> int:
    if not PRICES_FILE.exists():
        print(f"[err] {PRICES_FILE} ontbreekt.", file=sys.stderr)
        return 1
    if not PREDICTION_LOG_FILE.exists():
        print("[info] prediction_log.json nog niet aanwezig; niks te doen.", file=sys.stderr)
        return 0

    # Lees prices.json
    prices_raw = PRICES_FILE.read_bytes().rstrip(b"\x00")
    prices_payload = json.loads(prices_raw)
    prices_list = prices_payload.get("prices", [])

    # Bouw lookup: ISO-string (tz-naief, uur-afgerond) -> prijs
    prices_by_iso: dict[str, float] = {}
    for entry in prices_list:
        t_str = entry.get("time", "")
        try:
            t_norm = datetime.fromisoformat(t_str).replace(
                minute=0, second=0, microsecond=0, tzinfo=None
            ).isoformat()
            prices_by_iso[t_norm] = float(entry["price"])
        except (ValueError, KeyError):
            continue
    print(f"[info] {len(prices_by_iso)} prijzen beschikbaar.", file=sys.stderr)

    # Lees prediction log
    log_raw = PREDICTION_LOG_FILE.read_bytes().rstrip(b"\x00")
    log: list[dict] = json.loads(log_raw) if log_raw else []
    print(f"[info] {len(log)} log-entries geladen.", file=sys.stderr)

    now_utc = datetime.now(timezone.utc)
    updated = 0
    for entry in log:
        if entry.get("actual") is not None:
            continue
        t_str = entry.get("target_time", "")
        try:
            target_dt = datetime.fromisoformat(t_str)
        except ValueError:
            continue
        # Alleen invullen als het uur verstreken is (prijs is known)
        if target_dt.replace(tzinfo=timezone.utc if target_dt.tzinfo is None else target_dt.tzinfo) > now_utc:
            continue
        # Normaliseer: tz-naief, uur-afgerond
        t_norm = target_dt.replace(
            minute=0, second=0, microsecond=0, tzinfo=None
        ).isoformat()
        actual = prices_by_iso.get(t_norm)
        if actual is not None:
            entry["actual"] = actual
            updated += 1

    if updated == 0:
        print("[info] Geen nieuwe actuals gevonden.", file=sys.stderr)
        return 0

    PREDICTION_LOG_FILE.write_bytes(
        json.dumps(log, indent=2, ensure_ascii=False).encode("utf-8")
    )
    total_with_actual = sum(1 for e in log if e.get("actual") is not None)
    print(f"[ok] {updated} entries aangevuld; "
          f"{total_with_actual}/{len(log)} entries hebben nu actual.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
