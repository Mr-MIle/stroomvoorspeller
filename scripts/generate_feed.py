"""
generate_feed.py — Genereert public/data/feed.xml voor stroomvoorspeller.nl.

RSS 2.0 feed met één item per dag: goedkoopste en duurste uur van morgen,
daggemiddelde, en een handig overzicht van alle uurprijzen.

Gebruik:
    python scripts/generate_feed.py

Vereist: public/data/prices.json (gegenereerd door fetch_prices.py)
Output:  public/data/feed.xml

De feed toont consumentenprijzen als indicatie (kale EPEX + standaard
energiebelasting + 21% btw, zonder leverancieropslag). Voor exacte prijzen
incl. opslag: zie stroomvoorspeller.nl.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import formatdate
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRICES_FILE  = PROJECT_ROOT / "public" / "data" / "prices.json"
CONFIG_FILE  = PROJECT_ROOT / "public" / "data" / "config.json"
OUTPUT_FILE  = PROJECT_ROOT / "public" / "data" / "feed.xml"

SITE_URL  = "https://stroomvoorspeller.nl"
FEED_URL  = f"{SITE_URL}/data/feed.xml"
MAX_ITEMS = 7  # maximaal 7 dagen terugkijken in de feed


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def mwh_to_cents_inclusive(eur_mwh: float, taxes: dict) -> float:
    """Reken EUR/MWh om naar ct/kWh incl. energiebelasting + btw (geen leverancieropslag)."""
    eur_kwh = eur_mwh / 1000.0
    eb = taxes.get("energiebelasting_per_kwh", 0.0916)
    btw = taxes.get("btw_factor", 1.21)
    return (eur_kwh + eb) * btw * 100  # ct/kWh


def fmt_ct(cents: float) -> str:
    return f"{cents:.1f}".replace(".", ",")


def group_by_date(prices: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for p in prices:
        date = p["time"][:10]
        groups.setdefault(date, []).append(p)
    return groups


def build_item(date: str, day_prices: list[dict], taxes: dict) -> ET.Element:
    """Bouw één RSS <item> voor een dag."""
    inclusive = [(p, mwh_to_cents_inclusive(p["price"], taxes)) for p in day_prices]
    cheapest  = min(inclusive, key=lambda x: x[1])
    priciest  = max(inclusive, key=lambda x: x[1])
    avg_cents = sum(c for _, c in inclusive) / len(inclusive)
    neg_hours = [p for p, c in inclusive if c < 0]

    # Titel
    cheap_time = cheapest[0]["time"][11:16]
    cheap_ct   = fmt_ct(cheapest[1])
    dt_obj     = datetime.fromisoformat(date)
    day_nl     = ["maandag","dinsdag","woensdag","donderdag","vrijdag","zaterdag","zondag"][dt_obj.weekday()]
    day_fmt    = f"{day_nl} {dt_obj.day} {['jan','feb','mrt','apr','mei','jun','jul','aug','sep','okt','nov','dec'][dt_obj.month-1]}"
    title      = f"Stroom {day_fmt}: goedkoopst om {cheap_time} ({cheap_ct} ct/kWh)"

    # Body: tekst-overzicht
    lines = [
        f"Stroomprijs overzicht voor {day_fmt}",
        "",
        f"⚡ Goedkoopste uur: {cheap_time}  →  {cheap_ct} ct/kWh (incl. eb + btw)",
        f"🔴 Duurste uur:     {priciest[0]['time'][11:16]}  →  {fmt_ct(priciest[1])} ct/kWh",
        f"📊 Daggemiddelde:   {fmt_ct(avg_cents)} ct/kWh",
    ]
    if neg_hours:
        neg_list = ", ".join(p["time"][11:16] for p in neg_hours)
        lines.append(f"🎉 Negatieve uren ({len(neg_hours)}): {neg_list} — stroom is dan gratis of levert geld op!")
    lines += [
        "",
        "Uuroverzicht (EPEX + standaard belasting, excl. leverancieropslag):",
    ]
    for p, c in inclusive:
        marker = " ← goedkoopste" if p is cheapest[0] else (" ← duurste" if p is priciest[0] else "")
        flag = " 🎉" if c < 0 else ""
        lines.append(f"  {p['time'][11:16]}  {fmt_ct(c):>6} ct/kWh{flag}{marker}")
    lines += [
        "",
        "Exacte prijs incl. jouw leverancieropslag: stroomvoorspeller.nl",
        "Bron: ENTSO-E day-ahead prijzen (EPEX Spot NL bidding zone).",
    ]
    description = "\n".join(lines)

    # pubDate: 00:00 local time van de dag
    pub_dt  = datetime.fromisoformat(day_prices[0]["time"])
    pub_rfc = formatdate(pub_dt.timestamp(), usegmt=True)

    item = ET.Element("item")
    ET.SubElement(item, "title").text       = title
    ET.SubElement(item, "link").text        = SITE_URL
    desc_el = ET.SubElement(item, "description")
    desc_el.text = description
    ET.SubElement(item, "pubDate").text     = pub_rfc
    ET.SubElement(item, "guid", isPermaLink="false").text = f"{SITE_URL}/dag/{date}"
    return item


def main() -> int:
    if not PRICES_FILE.exists():
        print(f"[error] {PRICES_FILE} niet gevonden.", flush=True)
        return 1

    payload = load_json(PRICES_FILE)
    prices  = payload.get("prices", [])
    if not prices:
        print("[warn] prices leeg, feed niet gegenereerd.", flush=True)
        return 0

    taxes = {}
    if CONFIG_FILE.exists():
        cfg   = load_json(CONFIG_FILE)
        taxes = cfg.get("taxes", {})

    # Groepeer per datum, sorteer aflopend, pak laatste MAX_ITEMS dagen
    by_date  = group_by_date(prices)
    dates    = sorted(by_date.keys(), reverse=True)[:MAX_ITEMS]

    # RSS root
    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:atom", "http://www.w3.org/2005/Atom")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text          = "Stroomvoorspeller.nl — day-ahead prijzen"
    ET.SubElement(channel, "link").text           = SITE_URL
    ET.SubElement(channel, "description").text    = (
        "Dagelijkse day-ahead stroomprijzen voor Nederland (EPEX Spot), "
        "met goedkoopste en duurste uur. Indicatieprijs incl. energiebelasting + btw, "
        "excl. leverancieropslag. Bron: ENTSO-E."
    )
    ET.SubElement(channel, "language").text       = "nl-NL"
    ET.SubElement(channel, "lastBuildDate").text  = formatdate(usegmt=True)
    ET.SubElement(channel, "ttl").text            = "180"  # 3 uur cache
    atom_link = ET.SubElement(channel, "atom:link")
    atom_link.set("href", FEED_URL)
    atom_link.set("rel",  "self")
    atom_link.set("type", "application/rss+xml")

    for date in dates:
        item = build_item(date, sorted(by_date[date], key=lambda p: p["time"]), taxes)
        channel.append(item)

    # Schrijf met declaratie + mooie inspringing
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="unicode", xml_declaration=False)
    print(f"[ok] Feed geschreven: {OUTPUT_FILE} ({len(dates)} items)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
