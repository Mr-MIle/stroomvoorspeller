#!/usr/bin/env python3
"""
Bereken de dagelijkse batterij-arbitragewinst en schrijf die naar
public/data/arbitrage.json.

Methode (bevestigd met Emile):
  - Laden en ontladen per ~5-min interval uit de lopende kWh-tellers van de
    Solis-omvormer (batteryTodayChargeEnergy / batteryTodayDischargeEnergy).
  - Elk interval gekoppeld aan de EPEX-uurprijs uit public/data/prices.json.
  - Ontladen = vermeden inkoop tegen consumentenprijs (epex + opslag +
    energiebelasting) x btw  [Frank Energie].
  - Laden overdag = gemiste export uit zon, tegen kale EPEX (Frank betaalt spot).
    Laden 22-06u kan alleen netstroom zijn -> consumentenprijs (vangnet).
  - Markt-cijfer: laden en ontladen tegen kale EPEX.
  - PV en wateraccu lopen buiten deze omvormer om en worden NIET gemeten.

Draait in een GitHub Action. Sleutels uit env SOLIS_KEY_ID / SOLIS_KEY_SECRET.

Gebruik:
  python scripts/fetch_solis_arbitrage.py            # verwerkt gisteren (NL-tijd)
  python scripts/fetch_solis_arbitrage.py 2026-06-21 # verwerkt specifieke dag
"""
import hashlib
import hmac
import base64
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------- configuratie
KEY_ID = os.environ["SOLIS_KEY_ID"].strip()
KEY_SECRET = os.environ["SOLIS_KEY_SECRET"].strip().encode()
BASE = os.environ.get("SOLIS_BASE", "https://www.soliscloud.com:13333").rstrip("/")

SUPPLIER_ID = os.environ.get("SOLIS_SUPPLIER_ID", "frank")  # opslag uit config.json

ROOT = Path(__file__).resolve().parents[1]          # .../02-code
PRICES_FILE = ROOT / "public" / "data" / "prices.json"
CONFIG_FILE = ROOT / "public" / "data" / "config.json"
OUT_FILE = ROOT / "public" / "data" / "arbitrage.json"


# ----------------------------------------------------------------- API-helpers
def _md5_b64(body: bytes) -> str:
    return base64.b64encode(hashlib.md5(body).digest()).decode()


def call(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    content_md5 = _md5_b64(body)
    content_type = "application/json"
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    string_to_sign = f"POST\n{content_md5}\n{content_type}\n{date}\n{path}"
    sign = base64.b64encode(
        hmac.new(KEY_SECRET, string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    headers = {
        "Content-MD5": content_md5,
        "Content-Type": content_type,
        "Date": date,
        "Authorization": f"API {KEY_ID}:{sign}",
    }
    req = urllib.request.Request(BASE + path, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# ------------------------------------------------------------------ tijdhulpen
def amsterdam_offset(now_utc: datetime) -> timedelta:
    """Zomertijd (+2) of wintertijd (+1) voor Europe/Amsterdam, zonder tzdata."""
    year = now_utc.year
    march = datetime(year, 3, 31, 1, 0, tzinfo=timezone.utc)
    while march.weekday() != 6:
        march -= timedelta(days=1)
    october = datetime(year, 10, 31, 1, 0, tzinfo=timezone.utc)
    while october.weekday() != 6:
        october -= timedelta(days=1)
    return timedelta(hours=2) if march <= now_utc < october else timedelta(hours=1)


# ----------------------------------------------------------------- kernlogica
def get_inverter():
    """Eerste station -> eerste omvormer (id, sn)."""
    st = call("/v1/api/userStationList", {"pageNo": 1, "pageSize": 100})
    recs = st["data"]["page"]["records"]
    if not recs:
        raise RuntimeError("Geen stations gevonden onder dit account.")
    sid = str(recs[0]["id"])
    inv = call("/v1/api/inverterList", {"pageNo": 1, "pageSize": 100, "stationId": sid})
    irecs = inv["data"]["page"]["records"]
    if not irecs:
        raise RuntimeError(f"Geen omvormer gevonden voor station {sid}.")
    return str(irecs[0]["id"]), irecs[0].get("sn")


def load_prices() -> dict:
    """ISO-uur ('YYYY-MM-DDTHH') -> EPEX-prijs in EUR/kWh."""
    d = json.loads(PRICES_FILE.read_text())
    out = {}
    for e in d.get("prices", []):
        t = e.get("time", "")
        if t and e.get("price") is not None:
            out[t[:13]] = e["price"] / 1000.0   # EUR/MWh -> EUR/kWh
    return out


def load_tariff():
    cfg = json.loads(CONFIG_FILE.read_text())
    t = cfg["taxes"]
    markup = None
    supplier_name = SUPPLIER_ID
    for s in cfg.get("suppliers", []):
        if s.get("id") == SUPPLIER_ID:
            markup = s.get("markup_per_kwh")
            supplier_name = s.get("name")
            break
    if markup is None:
        raise RuntimeError(f"Aanbieder '{SUPPLIER_ID}' niet in config.json gevonden.")
    return {
        "supplier": supplier_name,
        "markup": markup,
        "energiebelasting": t.get("energiebelasting_per_kwh", 0.0),
        "btw": t.get("btw_factor", 1.0),
    }


def consumer_price(epex_kwh: float, tar: dict) -> float:
    """Consumentenprijs EUR/kWh = (epex + opslag + energiebelasting) * btw."""
    return (epex_kwh + tar["markup"] + tar["energiebelasting"]) * tar["btw"]


def compute_day(points: list, prices: dict, tar: dict, day: str) -> dict:
    """Reken intervallen door; geeft het dag-record terug."""
    pts = sorted(points, key=lambda p: int(p["dataTimestamp"]))

    charged = discharged = 0.0
    solar_charge = grid_charge = 0.0
    cost_mkt = val_mkt = cost_incl = val_incl = 0.0
    energy_priced = energy_total = 0.0

    prev = None
    for p in pts:
        if prev is not None:
            dch = max(0.0, p["batteryTodayChargeEnergy"] - prev["batteryTodayChargeEnergy"])
            ddis = max(0.0, p["batteryTodayDischargeEnergy"] - prev["batteryTodayDischargeEnergy"])
            if dch or ddis:
                hour = int(p["timeStr"][11:13])
                hour_key = p["timeStr"][:13].replace(" ", "T")  # 'YYYY-MM-DDTHH'
                charged += dch
                discharged += ddis
                energy_total += dch + ddis
                epex = prices.get(hour_key)
                if epex is not None:
                    cons = consumer_price(epex, tar)
                    # ontladen = vermeden inkoop tegen consumentenprijs
                    val_mkt += ddis * epex
                    val_incl += ddis * cons
                    # laden: overdag uit zon (gemiste export = EPEX);
                    # 22-06u kan alleen netstroom zijn -> consumentenprijs.
                    cost_mkt += dch * epex
                    is_night = hour < 6 or hour >= 22
                    if is_night:
                        grid_charge += dch
                        cost_incl += dch * cons
                    else:
                        solar_charge += dch
                        cost_incl += dch * epex
                    energy_priced += dch + ddis
        prev = p

    coverage = (energy_priced / energy_total) if energy_total else 0.0

    # "Gratis" verbruik: deel van de ontlading dat uit zon geladen was en in
    # (dure) uren is gebruikt i.p.v. ingekocht. Zon-aandeel van de lading
    # toegepast op de ontlading.
    solar_fraction = (solar_charge / charged) if charged else 0.0
    gratis_kwh = discharged * solar_fraction
    gratis_besparing = val_incl * solar_fraction

    soc_start = pts[0].get("batteryCapacitySoc")
    soc_end = pts[-1].get("batteryCapacitySoc")
    last = pts[-1]

    def r(x, n=3):
        return round(x, n)

    return {
        "date": day,
        "charged_kwh": r(charged, 2),
        "discharged_kwh": r(discharged, 2),
        "solar_charge_kwh": r(solar_charge, 2),
        "grid_charge_kwh": r(grid_charge, 2),
        "gratis_kwh": r(gratis_kwh, 2),
        "gratis_besparing_eur": r(gratis_besparing, 2),
        "profit_market_eur": r(val_mkt - cost_mkt),
        "profit_incl_eur": r(val_incl - cost_incl),
        "charge_cost_market_eur": r(cost_mkt),
        "discharge_value_market_eur": r(val_mkt),
        "charge_cost_incl_eur": r(cost_incl),
        "discharge_value_incl_eur": r(val_incl),
        "avg_charge_ct_incl": r(cost_incl / charged * 100, 2) if charged else None,
        "avg_discharge_ct_incl": r(val_incl / discharged * 100, 2) if discharged else None,
        "soc_start_pct": soc_start,
        "soc_end_pct": soc_end,
        "grid_import_kwh": last.get("gridPurchasedTodayEnergy"),
        "grid_export_kwh": last.get("gridSellTodayEnergy"),
        "price_coverage": r(coverage, 3),
        "n_points": len(pts),
    }


def upsert(record: dict, tar: dict):
    if OUT_FILE.exists():
        doc = json.loads(OUT_FILE.read_text())
    else:
        doc = {"days": []}
    days = {d["date"]: d for d in doc.get("days", [])}
    days[record["date"]] = record
    ordered = [days[k] for k in sorted(days)]

    tot_mkt = sum(d["profit_market_eur"] for d in ordered)
    tot_incl = sum(d["profit_incl_eur"] for d in ordered)
    tot_gratis = sum(d.get("gratis_kwh", 0) for d in ordered)

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "currency": "EUR",
        "supplier": tar["supplier"],
        "markup_per_kwh": tar["markup"],
        "method": ("Ontladen = vermeden inkoop tegen consumentenprijs "
                   "(epex + opslag + energiebelasting) x btw. Laden overdag = gemiste "
                   "export tegen EPEX (zon); laden 22-06u = netstroom tegen consumentenprijs. "
                   "Markt = EPEX voor laden en ontladen. PV en wateraccu niet gemeten."),
        "totals": {
            "days": len(ordered),
            "profit_market_eur": round(tot_mkt, 2),
            "profit_incl_eur": round(tot_incl, 2),
            "gratis_kwh": round(tot_gratis, 2),
        },
        "days": ordered,
    }
    OUT_FILE.write_text(json.dumps(doc, indent=1, ensure_ascii=False))


def main():
    now_utc = datetime.now(timezone.utc)
    if len(sys.argv) > 1 and sys.argv[1].strip():
        day = sys.argv[1].strip()
    else:
        ams = now_utc + amsterdam_offset(now_utc)
        day = (ams - timedelta(days=1)).strftime("%Y-%m-%d")

    tz_hours = int(amsterdam_offset(now_utc).total_seconds() // 3600)
    inv_id, sn = get_inverter()
    resp = call("/v1/api/inverterDay",
                {"id": inv_id, "sn": sn, "money": "EUR", "time": day, "timeZone": tz_hours})
    points = resp.get("data") or []
    if not points:
        print(f"Geen meetdata voor {day} - niets weggeschreven.", file=sys.stderr)
        sys.exit(0)

    prices = load_prices()
    tar = load_tariff()
    record = compute_day(points, prices, tar, day)
    upsert(record, tar)

    print(f"[{day}] geladen {record['charged_kwh']} kWh / ontladen {record['discharged_kwh']} kWh")
    print(f"  winst markt:          EUR {record['profit_market_eur']}")
    print(f"  winst incl.belasting: EUR {record['profit_incl_eur']}")
    print(f"  gratis verbruikt:     {record['gratis_kwh']} kWh (EUR {record['gratis_besparing_eur']})")
    print(f"  prijsdekking: {record['price_coverage']*100:.0f}%  ({record['n_points']} meetpunten)")
    if record["price_coverage"] < 0.95:
        print("  LET OP: niet alle uren hadden een prijs in prices.json.", file=sys.stderr)


if __name__ == "__main__":
    main()
