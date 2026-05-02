"""
alert_post.py — Post een alert-tweet op @stroomtarief bij extreme stroomprijzen.

Twee alerttypen (beide gebaseerd op all-in consumentenprijs NA belasting):
  - "negative": >= 2 komende uren vandaag met all-in prijs < 0 ct/kWh
  - "peak":     >= 1 komend uur vandaag met all-in prijs > 38 ct/kWh

Deduplicatie: per dag per type slechts een alert. Status wordt bijgehouden in
public/data/tweet_state.json (publiek maar niet-sensitief).

Gebruik:
    # Testen -- print tweets maar post NIET:
    python scripts/alert_post.py --dry-run

    # Productie:
    X_API_KEY=... X_API_SECRET=... X_ACCESS_TOKEN=... X_ACCESS_SECRET=... \\
        python scripts/alert_post.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- Paden ----
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRICES_FILE  = PROJECT_ROOT / "public" / "data" / "prices.json"
CONFIG_FILE  = PROJECT_ROOT / "public" / "data" / "config.json"
STATE_FILE   = PROJECT_ROOT / "public" / "data" / "tweet_state.json"

BRAND = "stroomvoorspeller.nl"

# ---- Drempels (all-in consumentenprijs incl. btw, in ct/kWh) ----
# Negatief: all-in prijs < 0 ct/kWh -- stroom kost letterlijk niets na belasting
NEGATIVE_THRESHOLD_CT = 0.0
# Piek: all-in prijs > 38 ct/kWh -- equivalent van EPEX ~200 EUR/MWh
# na gemiddelde opslag (2,1 ct) + energiebelasting + 21% btw
PEAK_THRESHOLD_CT = 38.0

# Minimaal aantal uur vereist voor een alert
MIN_NEGATIVE_HOURS = 2
MIN_PEAK_HOURS     = 1

# Max tweet-lengte (X-limiet)
MAX_CHARS = 280


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_now_local() -> datetime:
    """Huidige tijd in Europe/Amsterdam, naive datetime."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Amsterdam")).replace(tzinfo=None)
    except Exception:
        utc = datetime.now(timezone.utc).replace(tzinfo=None)
        y = utc.year
        m_last = datetime(y, 3, 31)
        while m_last.weekday() != 6:
            m_last -= timedelta(days=1)
        o_last = datetime(y, 10, 31)
        while o_last.weekday() != 6:
            o_last -= timedelta(days=1)
        is_summer = m_last <= utc < o_last
        return utc + timedelta(hours=2 if is_summer else 1)


def consumer_price_ct(eur_mwh: float, config: dict) -> float:
    """All-in consumentenprijs in ct/kWh incl. btw, gemiddelde leverancier."""
    suppliers = config.get("suppliers", [])
    s = next((x for x in suppliers if x["id"] == "average"), suppliers[0])
    markup = float(s.get("markup_per_kwh", 0))
    t   = config.get("taxes", {})
    eb  = float(t.get("energiebelasting_per_kwh", 0))
    btw = float(t.get("btw_factor", 1))
    return ((eur_mwh / 1000.0) + markup + eb) * btw * 100.0


def fmt_ct(ct: float) -> str:
    """Formateer ct-waarde in NL-notatie, bijv. '3,2' of '-12,5'."""
    return f"{ct:.1f}".replace(".", ",")


def already_alerted(state: dict, date_str: str, alert_type: str) -> bool:
    return alert_type in state.get("alerts", {}).get(date_str, [])


def mark_alerted(state: dict, date_str: str, alert_type: str) -> dict:
    alerts = state.setdefault("alerts", {})
    day_list = alerts.setdefault(date_str, [])
    if alert_type not in day_list:
        day_list.append(alert_type)
    # Bewaar alleen de laatste 7 dagen om het bestand klein te houden
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    state["alerts"] = {k: v for k, v in alerts.items() if k >= cutoff}
    return state


def compose_negative_tweet(neg_rows: list) -> str:
    """Bouw alert-tweet voor uren met negatieve all-in prijs (na belasting)."""
    hours   = sorted(r["hour"] for r in neg_rows)
    start_h = hours[0]
    end_h   = hours[-1] + 1  # '14:00' = t/m het 13:00-uur
    min_ct  = min(r["ct"] for r in neg_rows)

    text = (
        "Stroom is negatief geprijsd -- ook na belasting.\n\n"
        + f"{start_h:02d}:00-{end_h:02d}:00 | Laagste all-in: {fmt_ct(min_ct)} ct/kWh\n\n"
        + "Nu aanzetten: wasmachine, vaatwasser, EV of batterij\n\n"
        + f"-> {BRAND}\n\n"
        + "#negatieveprijs #zonnepanelen #EVladen #dynamischcontract #stroomprijzen"
    )
    return text


def compose_peak_tweet(peak_rows: list, all_today: list) -> str:
    """Bouw alert-tweet voor uren met hoge all-in prijs (na belasting)."""
    hours   = sorted(r["hour"] for r in peak_rows)
    start_h = hours[0]
    end_h   = hours[-1] + 1
    max_ct  = max(r["ct"] for r in peak_rows)

    # Goedkoopste uur vandaag buiten de piekuren, op basis van all-in
    non_peak = [r for r in all_today if r["hour"] not in hours]
    if non_peak:
        cheapest   = min(non_peak, key=lambda r: r["ct"])
        cheap_line = (
            f"Goedkoopste uur vandaag: {cheapest['hour']:02d}:00 "
            f"({fmt_ct(cheapest['ct'])} ct)\n"
        )
    else:
        cheap_line = ""

    text = (
        "Stroom is extreem duur -- ook na belasting.\n\n"
        + f"{start_h:02d}:00-{end_h:02d}:00 | Piek all-in: {fmt_ct(max_ct)} ct/kWh\n\n"
        + f"{cheap_line}"
        + "Zet zware apparaten uit of verschuif ze.\n\n"
        + f"-> {BRAND}\n\n"
        + "#energieprijzen #stroomprijzen #dynamischcontract #energietips #slimstroom"
    )
    return text


def validate_length(text: str, label: str) -> bool:
    """Waarschuw als tweet te lang is. Geeft True terug als OK."""
    n = len(text)
    if n > MAX_CHARS:
        print(f"WAARSCHUWING: {label} tweet is {n} tekens (max {MAX_CHARS})!", file=sys.stderr)
        return False
    print(f"  Lengte {label}: {n}/{MAX_CHARS} tekens -- OK")
    return True


def post_tweet(text: str) -> None:
    import tweepy

    creds = {
        "consumer_key":        os.environ["X_API_KEY"],
        "consumer_secret":     os.environ["X_API_SECRET"],
        "access_token":        os.environ["X_ACCESS_TOKEN"],
        "access_token_secret": os.environ["X_ACCESS_SECRET"],
    }
    client = tweepy.Client(**creds)
    response = client.create_tweet(text=text)
    tweet_id = response.data.get("id") if response and response.data else "?"
    print(f"OK tweet gepost: id={tweet_id}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Post alert-tweet bij extreme stroomprijzen.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print tweets maar post NIET en schrijf state NIET.")
    args = ap.parse_args()

    # ---- Data laden ----
    config  = load_json(CONFIG_FILE)
    payload = load_json(PRICES_FILE)
    prices  = payload.get("prices", [])
    if not prices:
        print("ERROR: prices.json bevat geen 'prices'.", file=sys.stderr)
        sys.exit(1)

    now       = get_now_local()
    today_str = now.strftime("%Y-%m-%d")

    # Alle prijzen van vandaag, inclusief berekende all-in consumentenprijs
    today_rows = []
    for p in prices:
        t = datetime.fromisoformat(p["time"])
        if t.date().isoformat() == today_str:
            epex = float(p["price"])
            today_rows.append({
                "hour": t.hour,
                "epex": epex,
                "ct":   consumer_price_ct(epex, config),
            })

    if not today_rows:
        print("SKIP: geen prijzen voor vandaag in prices.json.", file=sys.stderr)
        sys.exit(0)

    # Alleen komende uren (inclusief huidig uur)
    upcoming = [r for r in today_rows if r["hour"] >= now.hour]

    state         = load_json(STATE_FILE)
    state_changed = False

    # ---- Check 1: Negatieve all-in prijs ----
    neg_rows = [r for r in upcoming if r["ct"] < NEGATIVE_THRESHOLD_CT]
    if len(neg_rows) >= MIN_NEGATIVE_HOURS:
        if already_alerted(state, today_str, "negative"):
            print(f"SKIP negatief-alert: al gepost vandaag ({today_str}).")
        else:
            tweet = compose_negative_tweet(neg_rows)
            print(f"\n=== Alert NEGATIEF ===\n{tweet}\n=====================")
            validate_length(tweet, "negatief")
            if not args.dry_run:
                post_tweet(tweet)
                state = mark_alerted(state, today_str, "negative")
                state_changed = True
            else:
                print("DRY RUN -- niet gepost.")
    else:
        print(
            f"Geen negatief-alert: {len(neg_rows)} uur met all-in prijs < 0 ct "
            f"(minimum {MIN_NEGATIVE_HOURS} vereist)."
        )

    # ---- Check 2: Hoge all-in prijs ----
    peak_rows = [r for r in upcoming if r["ct"] > PEAK_THRESHOLD_CT]
    if len(peak_rows) >= MIN_PEAK_HOURS:
        if already_alerted(state, today_str, "peak"):
            print(f"SKIP piek-alert: al gepost vandaag ({today_str}).")
        else:
            tweet = compose_peak_tweet(peak_rows, today_rows)
            print(f"\n=== Alert PIEK ===\n{tweet}\n==================")
            validate_length(tweet, "piek")
            if not args.dry_run:
                post_tweet(tweet)
                state = mark_alerted(state, today_str, "peak")
                state_changed = True
            else:
                print("DRY RUN -- niet gepost.")
    else:
        print(
            f"Geen piek-alert: {len(peak_rows)} uur met all-in prijs > {PEAK_THRESHOLD_CT} ct "
            f"(minimum {MIN_PEAK_HOURS} vereist)."
        )

    # ---- State opslaan ----
    if state_changed:
        save_json(STATE_FILE, state)
        print(f"\nState bijgewerkt: {STATE_FILE}")


if __name__ == "__main__":
    main()
