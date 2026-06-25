#!/usr/bin/env python3
"""
Bereken per dag wat de thuisbatterij + zon doen en schrijf naar
public/data/arbitrage.json. Voedt het dashboard met vier cijfers:
verbruikt, wat het kostte (mét en zonder batterij), hoeveel de batterij
leverde, en wat arbitrage (terugleveren in dure uren) opbracht.

Rekenmodel (met Emile vastgelegd):
  - Verbruik = homeLoadTodayEnergy (hele huis).
  - Kosten ACTUEEL = inkoop x consumentenprijs - teruglevering x kale EPEX.
  - Kosten ZONDER batterij/zon = verbruik x consumentenprijs (alles van net).
  - Besparing = zonder - actueel.
  - Laden uit zon = GRATIS (eur 0), apart gemeld. Laden uit net = import boven
    huisverbruik dat samenvalt met laden.
  - Laden uit net (22-06u) = consumentenprijs (zit ook in inkoop).
  - Ontladen naar huis = vermeden inkoop (waarde = consumentenprijs).
  - Ontladen naar net (valt samen met net-export) = inkomsten tegen kale EPEX
    (min eventuele terugleverkosten SOLIS_FEEDIN_COST, standaard 0).
  consumentenprijs = (epex + opslag + energiebelasting) x btw.

Draait in een GitHub Action. Sleutels uit env SOLIS_KEY_ID / SOLIS_KEY_SECRET.

Gebruik:
  python scripts/fetch_solis_arbitrage.py            # gisteren (NL-tijd)
  python scripts/fetch_solis_arbitrage.py 2026-06-21 # specifieke dag
  python scripts/fetch_solis_arbitrage.py 2026-06-09 2026-06-22 # backfill bereik
"""
import hashlib
import hmac
import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------- configuratie
KEY_ID = os.environ["SOLIS_KEY_ID"].strip()
KEY_SECRET = os.environ["SOLIS_KEY_SECRET"].strip().encode()
BASE = os.environ.get("SOLIS_BASE", "https://www.soliscloud.com:13333").rstrip("/")

SUPPLIER_ID = os.environ.get("SOLIS_SUPPLIER_ID", "frank")     # opslag uit config.json
FEEDIN_COST = float(os.environ.get("SOLIS_FEEDIN_COST", "0"))  # terugleverkosten EUR/kWh

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


ENERGY_CHARTS_URL = "https://api.energy-charts.info/price"


def fetch_history_prices(start: str, end: str) -> dict:
    """Historische uur-EPEX (EUR/kWh) uit energy-charts voor [start, end] (incl.).

    prices.json is rollend (~16 dagen), dus backfill-dagen daarbuiten hebben anders
    geen prijs en worden door process_day overgeslagen (price_coverage < 0.5). Deze
    achtervang vult dat gat. Geen API-key. energy-charts geeft kwartierwaarden in
    EUR/MWh op UTC-tijd; we middelen per uur en zetten om naar Amsterdam-lokale
    uursleutels 'YYYY-MM-DDTHH', zodat ze matchen met load_prices/compute_day.
    """
    params = urllib.parse.urlencode({"bzn": "NL", "start": start, "end": end})
    req = urllib.request.Request(
        f"{ENERGY_CHARTS_URL}?{params}",
        headers={"User-Agent": "stroomvoorspeller/0.1"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    seconds = data.get("unix_seconds", []) or []
    prices = data.get("price", []) or []
    buckets: dict = {}
    for ts, pr in zip(seconds, prices):
        if pr is None:
            continue
        utc = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        local = utc + amsterdam_offset(utc)
        key = local.strftime("%Y-%m-%dT%H")
        buckets.setdefault(key, []).append(pr)
    return {k: (sum(v) / len(v)) / 1000.0 for k, v in buckets.items()}


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

    consumption = import_kwh = export_kwh = 0.0
    charged = discharged = 0.0
    solar_charged = grid_charged = 0.0
    battery_home = arbitrage_kwh = 0.0
    cost_actual = cost_without = 0.0
    battery_home_value = arbitrage_income = grid_charge_cost = 0.0
    solar_charge_value = 0.0
    energy_priced = energy_total = 0.0

    def d(cur, prv, key):
        return max(0.0, cur.get(key, 0.0) - prv.get(key, 0.0))

    prev = None
    for p in pts:
        if prev is not None:
            dch = d(p, prev, "batteryTodayChargeEnergy")
            ddis = d(p, prev, "batteryTodayDischargeEnergy")
            dimp = d(p, prev, "gridPurchasedTodayEnergy")
            dsell = d(p, prev, "gridSellTodayEnergy")
            dload = d(p, prev, "homeLoadTodayEnergy")
            charged += dch
            discharged += ddis
            import_kwh += dimp
            export_kwh += dsell
            consumption += dload
            flow = dch + ddis + dimp + dsell + dload
            if flow:
                hour = int(p["timeStr"][11:13])
                hour_key = p["timeStr"][:13].replace(" ", "T")
                energy_total += flow
                epex = prices.get(hour_key)
                if epex is not None:
                    cons = consumer_price(epex, tar)
                    export_price = epex - FEEDIN_COST
                    # kosten = je echte netto-rekening: inkoop tegen consumentenprijs,
                    # min ALLE teruglevering (batterij + losse zonnepanelen) tegen kale EPEX.
                    cost_actual += dimp * cons - dsell * export_price
                    cost_without += dload * cons
                    # batterij-teruglevering apart, voor de arbitrage-tegel: deel van de
                    # ontlading dat samenvalt met net-export. 's Avonds (geen zon) is net-
                    # export = batterij; begrensd op de ontlading.
                    to_grid = min(ddis, dsell)
                    to_home = ddis - to_grid
                    battery_home += to_home
                    arbitrage_kwh += to_grid
                    battery_home_value += to_home * cons
                    arbitrage_income += to_grid * export_price
                    # laden splitsen: net-stroom boven het huisverbruik die samenvalt
                    # met laden komt uit het net (kosten); de rest uit zon (gratis).
                    gch = min(dch, max(0.0, dimp - dload))
                    grid_charged += gch
                    solar_charged += dch - gch
                    grid_charge_cost += gch * cons
                    solar_charge_value += (dch - gch) * epex
                    energy_priced += flow
        prev = p

    coverage = (energy_priced / energy_total) if energy_total else 0.0
    saving = cost_without - cost_actual
    # Schatting: batterij-aandeel = wat de batterij verdiende door in tijd te schuiven
    # (thuis vermeden inkoop + verkoop terug) min laadkosten (net + marktwaarde zon).
    # Zon-aandeel = de rest van de besparing.
    battery_saving = battery_home_value + arbitrage_income - grid_charge_cost - solar_charge_value
    solar_saving = saving - battery_saving
    grid_data_ok = not (import_kwh == 0 and export_kwh == 0 and (charged or discharged or consumption))

    soc_start = pts[0].get("batteryCapacitySoc")
    soc_end = pts[-1].get("batteryCapacitySoc")

    def r(x, n=2):
        return round(x, n)

    return {
        "date": day,
        # vier tegels
        "consumption_kwh": r(consumption),
        "cost_actual_eur": r(cost_actual),
        "cost_without_eur": r(cost_without),
        "saving_eur": r(saving),
        "battery_home_kwh": r(battery_home),
        "battery_home_value_eur": r(battery_home_value),
        "arbitrage_kwh": r(arbitrage_kwh),
        "arbitrage_income_eur": r(arbitrage_income),
        # zon en laden
        "solar_charged_kwh": r(solar_charged),
        "grid_charged_kwh": r(grid_charged),
        "grid_charge_cost_eur": r(grid_charge_cost),
        "battery_saving_eur": r(battery_saving),
        "solar_saving_eur": r(solar_saving),
        # batterij ruw
        "charged_kwh": r(charged),
        "discharged_kwh": r(discharged),
        "import_kwh": r(import_kwh),
        "export_kwh": r(export_kwh),
        "battery_to_grid_kwh": r(arbitrage_kwh),
        "soc_start_pct": soc_start,
        "soc_end_pct": soc_end,
        # kwaliteit
        "price_coverage": round(coverage, 3),
        "grid_data_ok": grid_data_ok,
        "n_points": len(pts),
    }


def upsert(record: dict, tar: dict):
    if OUT_FILE.exists():
        doc = json.loads(OUT_FILE.read_text())
    else:
        doc = {"days": []}
    days = {x["date"]: x for x in doc.get("days", [])}
    days[record["date"]] = record
    ordered = [days[k] for k in sorted(days)]

    def tot(key):
        return round(sum(x.get(key, 0) or 0 for x in ordered), 2)

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "currency": "EUR",
        "supplier": tar["supplier"],
        "markup_per_kwh": tar["markup"],
        "method": ("Verbruik = homeLoad. Kosten actueel = inkoop x consumentenprijs - "
                   "teruglevering x EPEX. Kosten zonder = verbruik x consumentenprijs. "
                   "Laden uit zon = gratis; laden uit net (import boven huisverbruik) = consumentenprijs. Ontladen "
                   "naar huis = vermeden inkoop; naar net = inkomsten tegen EPEX. Besparing geschat gesplitst in batterij (arbitrage) en zon."),
        "totals": {
            "days": len(ordered),
            "consumption_kwh": tot("consumption_kwh"),
            "cost_actual_eur": tot("cost_actual_eur"),
            "cost_without_eur": tot("cost_without_eur"),
            "saving_eur": tot("saving_eur"),
            "battery_saving_eur": tot("battery_saving_eur"),
            "solar_saving_eur": tot("solar_saving_eur"),
            "battery_home_kwh": tot("battery_home_kwh"),
            "arbitrage_income_eur": tot("arbitrage_income_eur"),
            "solar_charged_kwh": tot("solar_charged_kwh"),
        },
        "days": ordered,
    }
    OUT_FILE.write_text(json.dumps(doc, indent=1, ensure_ascii=False))


def process_day(inv_id, sn, day, prices, tar, tz_hours):
    """Verwerk één dag; schrijft weg en geeft het record terug (of None bij overslaan)."""
    resp = call("/v1/api/inverterDay",
                {"id": inv_id, "sn": sn, "money": "EUR", "time": day, "timeZone": tz_hours})
    points = resp.get("data") or []
    if not points:
        print(f"[{day}] geen meetdata - overgeslagen.", file=sys.stderr)
        return None
    record = compute_day(points, prices, tar, day)
    if record["price_coverage"] < 0.5:
        print(f"[{day}] te weinig uurprijzen ({record['price_coverage']*100:.0f}%) - overgeslagen.", file=sys.stderr)
        return None
    upsert(record, tar)
    print(f"[{day}] verbruik {record['consumption_kwh']} kWh | "
          f"besparing EUR {record['saving_eur']} (batterij {record['battery_saving_eur']} / zon {record['solar_saving_eur']})")
    if not record["grid_data_ok"]:
        print(f"  LET OP {day}: net-tellers op 0 - kosten mogelijk onbetrouwbaar.", file=sys.stderr)
    return record


def main():
    now_utc = datetime.now(timezone.utc)
    tz_hours = int(amsterdam_offset(now_utc).total_seconds() // 3600)
    args = [a.strip() for a in sys.argv[1:] if a.strip()]

    prices = load_prices()
    tar = load_tariff()
    inv_id, sn = get_inverter()

    if len(args) >= 2:
        # Backfill: bereik van start t/m eind (beide inclusief).
        from datetime import date
        start = date.fromisoformat(args[0])
        end = date.fromisoformat(args[1])
        # prices.json dekt alleen de laatste ~16 dagen; haal de rest als historische
        # EPEX uit energy-charts. prices.json wint bij overlap (autoritatieve day-ahead).
        try:
            hist = fetch_history_prices(args[0], args[1])
            prices = {**hist, **prices}
            print(f"energy-charts: {len(hist)} uurprijzen geladen voor backfill.",
                  file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] historische prijzen ophalen mislukt: {exc}", file=sys.stderr)
        cur = start
        done = 0
        while cur <= end:
            if process_day(inv_id, sn, cur.isoformat(), prices, tar, tz_hours):
                done += 1
            cur += timedelta(days=1)
            time.sleep(2)  # rustig aan met de API
        print(f"Backfill klaar: {done} dagen weggeschreven ({start} t/m {end}).")
    else:
        if args:
            day = args[0]
        else:
            ams = now_utc + amsterdam_offset(now_utc)
            day = (ams - timedelta(days=1)).strftime("%Y-%m-%d")
        if not process_day(inv_id, sn, day, prices, tar, tz_hours):
            sys.exit(0)


if __name__ == "__main__":
    main()
