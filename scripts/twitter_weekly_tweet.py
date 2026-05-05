#!/usr/bin/env python3
"""
Stroomvoorspeller – Weekly Insight Tweet
=========================================
Haalt de energie-weekdata op, genereert een weekoverzicht-tweet met
voorspelde richting voor volgende week, en vraagt goedkeuring vóór posten.

Vereisten:
  pip install tweepy python-dotenv requests

Zet je credentials in .env (zie .env.example).
Run idealiter elke zondag.
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tweepy
from dotenv import load_dotenv

load_dotenv()

# ── Configuratie ──────────────────────────────────────────────────────────────

BEARER_TOKEN        = os.getenv("TWITTER_BEARER_TOKEN")
CONSUMER_KEY        = os.getenv("TWITTER_CONSUMER_KEY")
CONSUMER_SECRET     = os.getenv("TWITTER_CONSUMER_SECRET")
ACCESS_TOKEN        = os.getenv("TWITTER_ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

SITE_URL = "https://stroomvoorspeller.nl"

# ENTSO-E Transparency Platform – gratis API voor Europese stroomprijzen
# Vraag een token aan op: https://transparency.entsoe.eu
ENTSOE_TOKEN = os.getenv("ENTSOE_TOKEN")

# EAN / bidding zone voor Nederland
ENTSOE_AREA = "10YNL----------L"   # NL bidding zone


# ── ENTSO-E helper ────────────────────────────────────────────────────────────

def fetch_day_ahead_prices(date_from: datetime, date_to: datetime) -> list[float]:
    """
    Haalt dag-voor-dag prijzen op uit ENTSO-E Transparency Platform.
    Retourneert lijst van prijzen in €/MWh; leeg bij fout.
    """
    if not ENTSOE_TOKEN:
        print("⚠️   ENTSOE_TOKEN niet ingesteld — genereer prijs-inschatting op basis van template.")
        return []

    url = "https://web-api.tp.entsoe.eu/api"
    params = {
        "securityToken": ENTSOE_TOKEN,
        "documentType": "A44",
        "in_Domain":  ENTSOE_AREA,
        "out_Domain": ENTSOE_AREA,
        "periodStart": date_from.strftime("%Y%m%d%H%M"),
        "periodEnd":   date_to.strftime("%Y%m%d%H%M"),
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        # Simpele XML-parse: extraheer alle <price.amount> waarden
        import re
        prices = [float(p) for p in re.findall(r"<price\.amount>([\d.]+)</price\.amount>", r.text)]
        return prices
    except Exception as e:
        print(f"⚠️   ENTSO-E fout: {e}")
        return []


# ── Tweet generatie ───────────────────────────────────────────────────────────

def analyse_week(prices: list[float]) -> dict:
    """Bereken statistieken over een week met prijzen."""
    if not prices:
        return {}
    avg   = sum(prices) / len(prices)
    low   = min(prices)
    high  = max(prices)
    return {"avg": avg, "low": low, "high": high, "n": len(prices)}


def richting_label(deze_week_avg: float, vorige_week_avg: float) -> tuple[str, str]:
    """Geeft (label, emoji) terug op basis van prijsverschil."""
    verschil_pct = ((deze_week_avg - vorige_week_avg) / vorige_week_avg) * 100
    if verschil_pct > 10:
        return "flink duurder", "📈"
    elif verschil_pct > 3:
        return "iets duurder", "↗️"
    elif verschil_pct < -10:
        return "flink goedkoper", "📉"
    elif verschil_pct < -3:
        return "iets goedkoper", "↘️"
    else:
        return "stabiel", "➡️"


def format_prijs(mwh_eur: float) -> str:
    """Zet €/MWh om naar ct/kWh."""
    ct = mwh_eur / 10
    return f"{ct:.1f} ct/kWh"


def genereer_tweet(
    week_stats: dict,
    vorige_stats: dict,
    week_label: str,
    volgende_week_label: str,
) -> str:
    """Stelt de weekoverzicht-tweet samen."""

    if week_stats and vorige_stats:
        label, emoji = richting_label(week_stats["avg"], vorige_stats["avg"])
        gem_nu   = format_prijs(week_stats["avg"])
        gem_voor = format_prijs(vorige_stats["avg"])

        tweet = (
            f"⚡ Energie weekoverzicht NL – {week_label}\n"
            f"\n"
            f"Gem. stroomprijs: {gem_nu} (vorige week: {gem_voor})\n"
            f"Laagste uur: {format_prijs(week_stats['low'])}  |  Hoogste: {format_prijs(week_stats['high'])}\n"
            f"\n"
            f"Verwachting {volgende_week_label}: {label} {emoji}\n"
            f"\n"
            f"Dagelijkse voorspellingen → {SITE_URL}\n"
            f"#stroomprijs #energie #NL"
        )
    else:
        # Fallback zonder API-data (template-gebaseerd)
        tweet = (
            f"⚡ Energie weekoverzicht NL – {week_label}\n"
            f"\n"
            f"Hoe staan de stroomprijzen er deze week voor?\n"
            f"Dagelijkse voorspellingen en het weekoverzicht: {SITE_URL}\n"
            f"\n"
            f"#stroomprijs #energie #NL #dynamischcontract"
        )

    return tweet


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("⚡ Stroomvoorspeller Weekly Insight Tweet")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # Valideer Twitter credentials
    missing = [k for k, v in {
        "BEARER_TOKEN": BEARER_TOKEN,
        "CONSUMER_KEY": CONSUMER_KEY,
        "CONSUMER_SECRET": CONSUMER_SECRET,
        "ACCESS_TOKEN": ACCESS_TOKEN,
        "ACCESS_TOKEN_SECRET": ACCESS_TOKEN_SECRET,
    }.items() if not v]
    if missing:
        print(f"❌  Ontbrekende .env variabelen: {', '.join(missing)}")
        return

    client = tweepy.Client(
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_TOKEN_SECRET,
    )

    # Bepaal datumbereiken
    nu          = datetime.now(timezone.utc)
    maandag     = nu - timedelta(days=nu.weekday())           # begin deze week
    vorige_ma   = maandag - timedelta(weeks=1)                 # begin vorige week
    week_label       = f"week {maandag.isocalendar()[1]}"
    volgende_label   = f"week {(maandag + timedelta(weeks=1)).isocalendar()[1]}"

    # Haal prijsdata op
    print("📡  Prijsdata ophalen van ENTSO-E…")
    deze_week_prijzen  = fetch_day_ahead_prices(maandag, nu)
    vorige_week_prijzen = fetch_day_ahead_prices(vorige_ma, maandag)

    week_stats   = analyse_week(deze_week_prijzen)
    vorige_stats = analyse_week(vorige_week_prijzen)

    if week_stats:
        print(f"    Gem. deze week:   {format_prijs(week_stats['avg'])}")
    if vorige_stats:
        print(f"    Gem. vorige week: {format_prijs(vorige_stats['avg'])}")

    # Genereer tweet
    tweet_tekst = genereer_tweet(week_stats, vorige_stats, week_label, volgende_label)

    print(f"\n{'─'*62}")
    print("📝  Voorgestelde weekly tweet:\n")
    print(tweet_tekst)
    print(f"{'─'*62}")
    print(f"    Tekens: {len(tweet_tekst)} / 280")

    if len(tweet_tekst) > 280:
        print("⚠️   Tweet is te lang! Pas hem aan voor je post.")

    print("\n[j] Plaatsen   [e] Bewerken   [q] Annuleren")

    while True:
        keuze = input("→ ").strip().lower()
        if keuze == "j":
            break
        elif keuze == "e":
            print("Plak je nieuwe tweet tekst hieronder (sluit af met een lege regel):")
            regels = []
            while True:
                regel = input()
                if regel == "" and regels:
                    break
                regels.append(regel)
            tweet_tekst = "\n".join(regels)
            print(f"\nNieuwe tekst ({len(tweet_tekst)} tekens):")
            print(tweet_tekst)
            print("\n[j] Plaatsen   [e] Opnieuw bewerken   [q] Annuleren")
        elif keuze == "q":
            print("❌  Geannuleerd.")
            return
        else:
            print("Kies j / e / q")

    # Posten
    try:
        response = client.create_tweet(text=tweet_tekst)
        tweet_id = response.data["id"]
        print(f"\n✅  Tweet geplaatst!")
        print(f"    🔗 https://twitter.com/i/web/status/{tweet_id}")
    except tweepy.Forbidden as e:
        print(f"❌  Verboden: {e}")
    except tweepy.TooManyRequests:
        print("❌  Rate limit bereikt. Probeer later opnieuw.")
    except Exception as e:
        print(f"❌  Fout: {e}")


if __name__ == "__main__":
    main()
