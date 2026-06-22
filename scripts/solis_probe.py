#!/usr/bin/env python3
"""
Solis API probe — eenmalig, om de ruwe responsstructuur te leren kennen.

Draait in een GitHub Action (waar internet wél naar soliscloud.com:13333 mag).
Leest de sleutels uit env-vars SOLIS_KEY_ID en SOLIS_KEY_SECRET (GitHub Secrets).
Schrijft alles ruw naar solis_probe.json -> wordt als workflow-artifact bewaard.

GEEN geheimen in dit bestand. Niets committen naar de repo.
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

KEY_ID = os.environ["SOLIS_KEY_ID"].strip()
KEY_SECRET = os.environ["SOLIS_KEY_SECRET"].strip().encode()
BASE = os.environ.get("SOLIS_BASE", "https://www.soliscloud.com:13333").rstrip("/")


def _md5_b64(body: bytes) -> str:
    return base64.b64encode(hashlib.md5(body).digest()).decode()


def call(path: str, payload: dict):
    """Ondertekende POST volgens het SolisCloud HMAC-SHA1 schema."""
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
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"status": r.status, "json": json.loads(r.read().decode())}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": e.read().decode()[:1000]}
    except Exception as e:  # noqa: BLE001
        return {"status": "ERR", "error": str(e)}


def main():
    # Gisteren in NL-tijd (CEST = UTC+2 in de zomer); voor de probe ruim genoeg.
    yesterday = (datetime.now(timezone.utc) + timedelta(hours=2) - timedelta(days=1)).strftime("%Y-%m-%d")
    out = {"probed_at": datetime.now(timezone.utc).isoformat(), "day": yesterday, "calls": {}}

    # 1) Stations onder dit account
    stations = call("/v1/api/userStationList", {"pageNo": 1, "pageSize": 100})
    out["calls"]["userStationList"] = stations

    station_ids = []
    try:
        recs = stations["json"]["data"]["page"]["records"]
        station_ids = [str(r.get("id")) for r in recs if r.get("id") is not None]
    except Exception:  # noqa: BLE001
        pass

    # 2) Omvormers (per station, of globaal als er geen station-id is)
    inverters = []  # (id, sn)
    if station_ids:
        for sid in station_ids:
            inv = call("/v1/api/inverterList", {"pageNo": 1, "pageSize": 100, "stationId": sid})
            out["calls"][f"inverterList[station={sid}]"] = inv
            try:
                for r in inv["json"]["data"]["page"]["records"]:
                    inverters.append((str(r.get("id")), r.get("sn")))
            except Exception:  # noqa: BLE001
                pass
    else:
        inv = call("/v1/api/inverterList", {"pageNo": 1, "pageSize": 100})
        out["calls"]["inverterList"] = inv
        try:
            for r in inv["json"]["data"]["page"]["records"]:
                inverters.append((str(r.get("id")), r.get("sn")))
        except Exception:  # noqa: BLE001
            pass

    # 3) Detail + intraday-dagdata per omvormer
    for inv_id, sn in inverters:
        out["calls"][f"inverterDetail[{sn}]"] = call("/v1/api/inverterDetail", {"id": inv_id, "sn": sn})
        out["calls"][f"inverterDay[{sn}]"] = call(
            "/v1/api/inverterDay",
            {"id": inv_id, "sn": sn, "money": "EUR", "time": yesterday, "timeZone": 2},
        )

    with open("solis_probe.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)

    # Korte samenvatting in de log (geen geheimen)
    print(f"Stations: {station_ids}")
    print(f"Omvormers: {[sn for _, sn in inverters]}")
    print("Statussen:", {k: v.get('status') for k, v in out['calls'].items()})
    print("-> solis_probe.json geschreven")
    if not inverters:
        print("LET OP: geen omvormers gevonden. Controleer of de API-toegang volledig actief is.", file=sys.stderr)


if __name__ == "__main__":
    main()
