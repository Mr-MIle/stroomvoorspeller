"""
generate_ha.py — Genereert public/data/ha.json voor stroomvoorspeller.nl.

Eén kant-en-klaar JSON-bestand dat Home Assistant (en andere
huisautomatisering) rechtstreeks kan uitlezen via de ingebouwde RESTful
sensor. Geen custom integratie nodig.

Het bestand bevat:
  - vandaag + morgen: volledige uurlijst, goedkoopste/duurste uur,
    daggemiddelde en aantal negatieve uren;
  - een meerdaagse modelvoorspelling (indicatief, mét onzekerheid).

Twee prijssoorten per uur, beide in EUR/kWh:
  - market : kale EPEX day-ahead marktprijs (excl. belasting en opslag);
  - all_in : indicatieve consumentenprijs
             = (market + gemiddelde leverancieropslag + energiebelasting) × btw.

Gebruik:
    python scripts/generate_ha.py

Vereist: public/data/prices.json  (fetch_prices.py)
Optioneel: public/data/forecast.json (run_forecast.py),
           public/data/config.json  (belasting + gemiddelde opslag)
Output:  public/data/ha.json

Testhaak: zet HA_REFDATE=YYYY-MM-DD om "vandaag" te forceren (voor het
genereren van een voorbeeldbestand tegen historische data).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRICES_FILE   = PROJECT_ROOT / "public" / "data" / "prices.json"
FORECAST_FILE = PROJECT_ROOT / "public" / "data" / "forecast.json"
CONFIG_FILE   = PROJECT_ROOT / "public" / "data" / "config.json"
OUTPUT_FILE   = PROJECT_ROOT / "public" / "data" / "ha.json"

SITE_URL   = "https://stroomvoorspeller.nl"
DOCS_URL   = f"{SITE_URL}/home-assistant"
SCHEMA_VER = 1

# Fallback-belasting als config.json ontbreekt (tarief 2026).
DEFAULT_ENERGY_TAX = 0.0916   # EUR/kWh, excl. btw
DEFAULT_VAT        = 1.21
DEFAULT_MARKUP     = 0.0178   # EUR/kWh, gemiddelde leverancieropslag excl. btw


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def amsterdam_now() -> datetime:
    """Huidige tijd in Europe/Amsterdam, zonder externe tz-library."""
    now_utc = datetime.now(timezone.utc)
    year = now_utc.year
    # Laatste zondag van maart 01:00 UTC -> laatste zondag van oktober 01:00 UTC = zomertijd.
    march = datetime(year, 3, 31, 1, 0, tzinfo=timezone.utc)
    while march.weekday() != 6:
        march -= timedelta(days=1)
    october = datetime(year, 10, 31, 1, 0, tzinfo=timezone.utc)
    while october.weekday() != 6:
        october -= timedelta(days=1)
    offset = timedelta(hours=2) if march <= now_utc < october else timedelta(hours=1)
    return now_utc + offset


def get_tax_params(config: dict) -> dict:
    """Haal belasting + gemiddelde opslag uit config.json, met fallbacks."""
    taxes = config.get("taxes", {})
    energy_tax = taxes.get("energiebelasting_per_kwh", DEFAULT_ENERGY_TAX)
    vat = taxes.get("btw_factor", DEFAULT_VAT)
    markup = DEFAULT_MARKUP
    for sup in config.get("suppliers", []):
        if sup.get("id") == "average":
            markup = sup.get("markup_per_kwh", DEFAULT_MARKUP)
            break
    return {
        "energy_tax_per_kwh": round(energy_tax, 5),
        "vat_factor": vat,
        "average_markup_per_kwh": round(markup, 5),
        "year": taxes.get("year", 2026),
    }


def market_kwh(eur_mwh: float) -> float:
    return round(eur_mwh / 1000.0, 5)


def all_in_kwh(eur_mwh: float, tax: dict) -> float:
    base = eur_mwh / 1000.0 + tax["average_markup_per_kwh"] + tax["energy_tax_per_kwh"]
    return round(base * tax["vat_factor"], 5)


def hourly_only(prices: list[dict]) -> list[dict]:
    """Filter op hele uren (negeer eventuele PT15M-kwartierwaarden)."""
    return [p for p in prices if p.get("time", "")[14:16] == "00"]


def build_day(date: str, day_prices: list[dict], tax: dict) -> dict | None:
    """Bouw het vandaag/morgen-object voor één datum, of None als er niets is."""
    day_prices = sorted(day_prices, key=lambda p: p["time"])
    if not day_prices:
        return None

    hours = []
    for p in day_prices:
        hours.append({
            "start": p["time"],
            "market": market_kwh(p["price"]),
            "all_in": all_in_kwh(p["price"], tax),
        })

    # all_in is een strikt stijgende functie van market, dus het goedkoopste/
    # duurste uur is identiek voor beide prijssoorten. Elk uur bevat zowel
    # 'market' als 'all_in', zodat de gebruiker zelf kan kiezen waar hij op stuurt.
    cheapest = min(hours, key=lambda h: h["market"])
    priciest = max(hours, key=lambda h: h["market"])
    avg_market = round(sum(h["market"] for h in hours) / len(hours), 5)
    avg_all_in = round(sum(h["all_in"] for h in hours) / len(hours), 5)
    negative_hours_market = sum(1 for h in hours if h["market"] < 0)
    negative_hours_all_in = sum(1 for h in hours if h["all_in"] < 0)

    return {
        "date": date,
        "complete": len(hours) >= 23,        # 23 i.v.m. DST-dag met 23 uur
        "hours_count": len(hours),
        "average_market": avg_market,
        "average_all_in": avg_all_in,
        "cheapest": cheapest,
        "most_expensive": priciest,
        # Negatieve uren voor beide prijssoorten. 'negative_hours' = markt
        # (de kale beurs onder nul: 'gratis stroom'-signaal). 'negative_hours_all_in'
        # = consumentenprijs onder nul (zeldzamer; pas bij flink negatieve markt).
        "negative_hours": negative_hours_market,
        "negative_hours_market": negative_hours_market,
        "negative_hours_all_in": negative_hours_all_in,
        "hours": hours,
    }


def build_forecast(forecast: dict, today: str, tax: dict) -> dict:
    """Bouw het meerdaagse voorspelling-object (per dag samengevat)."""
    rows = forecast.get("forecasts", [])
    if not rows:
        return {"available": False}

    by_date: dict[str, list[dict]] = {}
    for r in rows:
        d = r.get("time", "")[:10]
        if d and d > today:            # alleen toekomst t.o.v. vandaag
            by_date.setdefault(d, []).append(r)

    days = []
    for d in sorted(by_date.keys()):
        rs = by_date[d]
        # 'predicted', 'lower', 'upper' staan in EUR/MWh -> EUR/kWh.
        avg_pred = sum(r.get("predicted", 0.0) for r in rs) / len(rs)
        avg_low  = sum(r.get("lower", r.get("predicted", 0.0)) for r in rs) / len(rs)
        avg_high = sum(r.get("upper", r.get("predicted", 0.0)) for r in rs) / len(rs)
        # Ruwe kans op negatieve uren: aandeel uren waarvan de onzekerheidsband
        # tot onder nul reikt (lower < 0).
        neg_share = sum(1 for r in rs if r.get("lower", 0.0) < 0) / len(rs)
        days.append({
            "date": d,
            "days_ahead": (datetime.fromisoformat(d) - datetime.fromisoformat(today)).days,
            "average_market_estimate": round(avg_pred / 1000.0, 5),
            "lower": round(avg_low / 1000.0, 5),
            "upper": round(avg_high / 1000.0, 5),
            "negative_probability": round(neg_share, 2),
        })

    return {
        "available": True,
        "model_version": str(forecast.get("model_version", "")),
        "disclaimer": (
            "Modelvoorspelling met onzekerheid — géén day-ahead feit. "
            "Gebruik dit niet voor automatiseringen die op exacte prijzen rekenen; "
            "gebruik daarvoor 'today' en (na ~14:00) 'tomorrow'."
        ),
        "days": days,
    }


def main() -> int:
    if not PRICES_FILE.exists():
        print(f"[error] {PRICES_FILE} niet gevonden.", flush=True)
        return 1

    prices_payload = load_json(PRICES_FILE)
    prices = hourly_only(prices_payload.get("prices", []))
    if not prices:
        print("[warn] geen uurprijzen gevonden, ha.json niet geschreven.", flush=True)
        return 0

    config = load_json(CONFIG_FILE) if CONFIG_FILE.exists() else {}
    tax = get_tax_params(config)

    ref = os.environ.get("HA_REFDATE")
    today = ref if ref else amsterdam_now().strftime("%Y-%m-%d")
    tomorrow = (datetime.fromisoformat(today) + timedelta(days=1)).strftime("%Y-%m-%d")

    by_date: dict[str, list[dict]] = {}
    for p in prices:
        by_date.setdefault(p["time"][:10], []).append(p)

    today_block = build_day(today, by_date.get(today, []), tax)
    tomorrow_block = build_day(tomorrow, by_date.get(tomorrow, []), tax)

    forecast_block = {"available": False}
    if FORECAST_FILE.exists():
        try:
            forecast_block = build_forecast(load_json(FORECAST_FILE), today, tax)
        except Exception as e:   # voorspelling mag nooit de hele build breken
            print(f"[warn] forecast overgeslagen: {e}", flush=True)

    out = {
        "schema_version": SCHEMA_VER,
        "generated": amsterdam_now().replace(microsecond=0).isoformat(),
        "source": "stroomvoorspeller.nl",
        "docs": DOCS_URL,
        "license": "CC-BY 4.0",
        "price_unit": "EUR/kWh",
        "timezone": "Europe/Amsterdam",
        "price_types": {
            "market": "Kale EPEX day-ahead marktprijs in EUR/kWh, excl. belasting en leverancieropslag.",
            "all_in": ("Indicatieve consumentenprijs in EUR/kWh incl. btw: "
                       "(market + gemiddelde leverancieropslag + energiebelasting) x btw. "
                       "Voor je exacte prijs: vervang de gemiddelde opslag door die van je eigen leverancier."),
        },
        "tax": tax,
        "today": today_block if today_block else {"date": today, "available": False},
        "tomorrow": tomorrow_block if tomorrow_block else {"date": tomorrow, "available": False},
        "forecast": forecast_block,
    }
    # 'available'-vlag expliciet meegeven op today/tomorrow voor de consument.
    if today_block:
        out["today"]["available"] = True
    if tomorrow_block:
        out["tomorrow"]["available"] = True

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")

    t_ok = "ja" if today_block else "nee"
    m_ok = "ja" if tomorrow_block else "nee"
    print(f"[ok] {OUTPUT_FILE} geschreven (vandaag={t_ok}, morgen={m_ok}, "
          f"voorspelling={'ja' if forecast_block.get('available') else 'nee'}).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
