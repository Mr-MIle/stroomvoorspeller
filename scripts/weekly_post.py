"""
weekly_post.py — Plaatst elke zondag een X-post (@stroomtarief) met het
energie-weekoverzicht voor Nederland + onze voorspelling voor de richting
van volgende week.

Gebruik:
    # Lokaal/CI testen — tekst genereren, GEEN post:
    python scripts/weekly_post.py --dry-run

    # Productie:
    X_API_KEY=... X_API_SECRET=... X_ACCESS_TOKEN=... X_ACCESS_SECRET=... \
        python scripts/weekly_post.py

Data:
    Haalt prijzen op uit public/data/prices.json (dezelfde bron als daily_post.py).
    Berekent gemiddelden voor de afgelopen week (ma–zo) en vergelijkt met
    de week ervoor. De richting-hint voor volgende week komt uit forecast.json
    (het eigen voorspellingsmodel) — zo geeft de tweet ook aandacht aan de
    voorspelling van stroomvoorspeller.nl.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── Paden ─────────────────────────────────────────────────────────────────────

PROJECT_ROOT   = Path(__file__).resolve().parent.parent
PRICES_FILE    = PROJECT_ROOT / "public" / "data" / "prices.json"
FORECAST_FILE  = PROJECT_ROOT / "public" / "data" / "forecast.json"
CONFIG_FILE    = PROJECT_ROOT / "public" / "data" / "config.json"

BRAND = "stroomvoorspeller.nl"


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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


def consumer_price_ct(eur_mwh: float, config: dict, supplier_id: str = "average") -> float:
    """All-in consumentenprijs in ct/kWh incl. btw."""
    suppliers = config.get("suppliers", [])
    s = next((x for x in suppliers if x["id"] == supplier_id), suppliers[0] if suppliers else {})
    markup = float(s.get("markup_per_kwh", 0))
    t = config.get("taxes", {})
    eb = float(t.get("energiebelasting_per_kwh", 0))
    btw = float(t.get("btw_factor", 1))
    return ((eur_mwh / 1000.0) + markup + eb) * btw * 100.0


def prices_for_week(prices: list, monday: date, config: dict) -> list[float]:
    """Geeft ct/kWh-waarden terug voor de week die begint op `monday`."""
    week_dates = {monday + timedelta(days=i) for i in range(7)}
    return [
        consumer_price_ct(float(p["price"]), config)
        for p in prices
        if datetime.fromisoformat(p["time"]).date() in week_dates
    ]


def forecast_avg_for_week(forecasts: list, monday: date, config: dict) -> float | None:
    """
    Berekent de gemiddelde voorspelde ct/kWh voor de week die begint op `monday`,
    op basis van het `predicted` veld in forecast.json (EUR/MWh).
    Geeft None terug als er te weinig voorspellingen zijn (< 24 uur).
    """
    week_dates = {monday + timedelta(days=i) for i in range(7)}
    vals = [
        consumer_price_ct(float(f["predicted"]), config)
        for f in forecasts
        if datetime.fromisoformat(f["time"]).date() in week_dates
    ]
    return sum(vals) / len(vals) if len(vals) >= 24 else None


def week_stats(vals: list[float]) -> dict | None:
    if len(vals) < 24:   # te weinig data — week (nog) niet volledig
        return None
    return {
        "avg":  sum(vals) / len(vals),
        "low":  min(vals),
        "high": max(vals),
        "n":    len(vals),
    }


def richting(deze_avg: float, vorige_avg: float) -> tuple[str, str]:
    """(label, emoji) op basis van procentueel verschil."""
    pct = (deze_avg - vorige_avg) / vorige_avg * 100
    if   pct >  12: return "flink duurder",    "📈"
    elif pct >   4: return "iets duurder",     "↗️"
    elif pct <  -12: return "flink goedkoper", "📉"
    elif pct <   -4: return "iets goedkoper",  "↘️"
    else:            return "stabiel",          "➡️"


# ── Tweet samenstellen ────────────────────────────────────────────────────────

def compose_tweet(
    deze_stats: dict,
    vorige_stats: dict | None,
    forecast_avg_volgende: float | None,
    week_nr: int,
) -> str:
    avg  = deze_stats["avg"]
    low  = deze_stats["low"]
    high = deze_stats["high"]

    # Vergelijking met vorige week
    if vorige_stats:
        pct = (avg - vorige_stats["avg"]) / vorige_stats["avg"] * 100
        sign = "+" if pct >= 0 else ""
        vergelijking = f" ({sign}{pct:.0f}% t.o.v. vorige week)"
    else:
        vergelijking = ""

    # Richting volgende week — altijd op basis van ons eigen voorspellingsmodel
    if forecast_avg_volgende is not None:
        vw_label, vw_emoji = richting(forecast_avg_volgende, avg)
        volgende_regel = f"Onze voorspelling week {week_nr + 1}: {vw_label} {vw_emoji}"
    else:
        volgende_regel = f"Voorspelling volgende week → {BRAND}"

    return (
        f"⚡ Energie weekoverzicht NL – week {week_nr}\n"
        f"\n"
        f"Gem. stroomprijs: {avg:.1f} ct/kWh{vergelijking}\n"
        f"Laagste uur: {low:.1f} ct  |  Hoogste: {high:.1f} ct\n"
        f"\n"
        f"{volgende_regel}\n"
        f"\n"
        f"→ {BRAND}\n"
        f"#stroomprijzen #dynamischcontract"
    )


# ── Posten ────────────────────────────────────────────────────────────────────

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


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Genereer tekst maar post NIET.")
    args = p.parse_args()

    config  = load_json(CONFIG_FILE)
    payload = load_json(PRICES_FILE)
    prices  = payload.get("prices", [])
    if not prices:
        print("ERROR: prices.json bevat geen 'prices'", file=sys.stderr)
        sys.exit(1)

    # Laad forecast.json voor de richting-hint volgende week
    forecasts: list = []
    if FORECAST_FILE.exists():
        fc_payload = load_json(FORECAST_FILE)
        forecasts  = fc_payload.get("forecasts", [])
        print(f"Forecast geladen: {len(forecasts)} uur ({fc_payload.get('model_version', '?')})")
    else:
        print("WAARSCHUWING: forecast.json niet gevonden — geen richting-hint.", file=sys.stderr)

    now     = get_now_local()
    today   = now.date()

    # Bepaal maandag van deze week (zondag = einde van de week die we rapporteren)
    days_since_monday = today.weekday()   # ma=0 … zo=6
    deze_maandag     = today - timedelta(days=days_since_monday)
    vorige_maandag   = deze_maandag - timedelta(weeks=1)
    volgende_maandag = deze_maandag + timedelta(weeks=1)

    week_nr = deze_maandag.isocalendar()[1]

    # Statistieken afgelopen twee weken (uit prices.json)
    deze_vals  = prices_for_week(prices, deze_maandag,  config)
    vorige_vals = prices_for_week(prices, vorige_maandag, config)

    deze_s  = week_stats(deze_vals)
    vorige_s = week_stats(vorige_vals)

    # Richting volgende week: uit eigen voorspellingsmodel (forecast.json)
    forecast_avg_volgende = forecast_avg_for_week(forecasts, volgende_maandag, config)
    if forecast_avg_volgende is not None:
        print(f"Voorspelling volgende week: gem. {forecast_avg_volgende:.1f} ct/kWh")
    else:
        print("Onvoldoende forecast-data voor volgende week — geen richting-hint.")

    if not deze_s:
        print(
            f"SKIP: te weinig prijsdata voor week {week_nr} "
            f"(gevonden: {len(deze_vals)} uur, minimaal 24 nodig).",
            file=sys.stderr,
        )
        sys.exit(0)

    text = compose_tweet(deze_s, vorige_s, forecast_avg_volgende, week_nr)

    print(f"=== Tweet ({len(text)} chars) ===")
    print(text)
    print("=== /tweet ===\n")

    if len(text) > 280:
        print("WAARSCHUWING: tweet is langer dan 280 tekens!", file=sys.stderr)

    if args.dry_run:
        print("DRY RUN — niet gepost.")
        return

    post_tweet(text)


if __name__ == "__main__":
    main()
