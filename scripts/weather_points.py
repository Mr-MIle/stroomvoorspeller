"""
weather_points.py — de weerpunten waar model v5 naar kijkt, plus het ophalen ervan.

Waarom meerdere punten: de stroomprijs hangt aan hoeveel wind- en zonnestroom er
in Nederland én Duitsland is. Eén punt in De Bilt op 10 meter hoogte zag dat
slecht (zie 01-documenten/onderzoek-modelverbetering-2026-10-03.md): winderige
dagen werden tot 39 EUR/MWh te hoog voorspeld.

Wind:  windsnelheid op 100 m bij de grote windparken op zee en op land.
Zon:   instraling op vijf punten verspreid over NL en DE.
Temp:  De Bilt en midden-Duitsland (warmtevraag).

Gedeeld door run_forecast.py (productie) en dump_lab_v5.py (backtestdata), zodat
de backtest exact dezelfde invoer ziet als productie.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

# (naam, lat, lon, gewicht in de index)
# Gewichten ruwweg naar opgesteld vermogen per gebied (2026): Duits land-wind is
# het grootst, dan Duitse en Nederlandse zee-wind, dan Nederlandse land-wind.
WIND_POINTS = [
    ("nl_zee_borssele",   51.70, 3.00, 1.0),
    ("nl_zee_hollandse",  52.60, 4.20, 1.5),
    ("nl_zee_gemini",     54.03, 5.95, 1.0),
    ("nl_land_flevoland", 52.50, 5.60, 1.0),
    ("nl_land_groningen", 53.40, 6.80, 0.8),
    ("de_zee_noordzee",   54.30, 6.60, 1.5),
    ("de_land_sh",        54.40, 9.20, 1.5),
    ("de_land_nds",       52.90, 8.00, 2.0),
    ("de_land_bb",        52.50, 13.20, 1.5),
]

SOLAR_POINTS = [
    ("nl_debilt",    52.10, 5.18, 1.0),
    ("nl_brabant",   51.45, 5.48, 1.0),
    ("nl_groningen", 53.20, 6.60, 0.7),
    ("de_nrw",       51.50, 7.50, 1.5),
    ("de_bayern",    48.50, 11.50, 2.0),
]

TEMP_POINTS = [
    ("nl_debilt", 52.10, 5.18, 1.0),
    ("de_midden", 51.00, 10.00, 1.0),
]

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"

# Grove vermogenscurve van een moderne turbine op 100 m (capaciteitsfactor 0..1).
# Inschakelen ~3 m/s, vol vermogen vanaf ~12 m/s, afschakelen boven 25 m/s.
CUT_IN, RATED, CUT_OUT = 3.0, 12.0, 25.0


def capacity_factor(ws: float | None) -> float | None:
    if ws is None:
        return None
    if ws < CUT_IN or ws >= CUT_OUT:
        return 0.0
    if ws >= RATED:
        return 1.0
    return _s_curve((ws - CUT_IN) / (RATED - CUT_IN))


def _s_curve(x: float) -> float:
    """S-curve tussen inschakel- en nominale snelheid (glad, monotoon)."""
    return 3 * x * x - 2 * x * x * x


def _get_json(url: str, tries: int = 4, timeout: int = 90) -> dict | list:
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; stroomvoorspeller/5.0)",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            wait = 3 * (attempt + 1)
            print(f"[warn] open-meteo poging {attempt + 1} mislukt ({exc}); {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"open-meteo ophalen mislukt: {last}")


def _multi(points, base_url: str, params: dict) -> list[dict]:
    """Eén call voor meerdere punten; geeft per punt de 'hourly'-dict terug."""
    q = dict(params)
    q["latitude"] = ",".join(f"{p[1]:.2f}" for p in points)
    q["longitude"] = ",".join(f"{p[2]:.2f}" for p in points)
    q.setdefault("timezone", "Europe/Amsterdam")
    q.setdefault("wind_speed_unit", "ms")
    data = _get_json(f"{base_url}?{urllib.parse.urlencode(q)}")
    if isinstance(data, dict):
        data = [data]
    return [d.get("hourly", {}) or {} for d in data]


def fetch_point_series(base_url: str, variables: dict[str, list], extra: dict) -> dict:
    """
    Haal per puntgroep de gevraagde variabelen op.

    variables: {"wind": ["wind_speed_100m", ...], "solar": [...], "temp": [...]}
    Return: {"time": [...], "wind": {var: {punt: [..]}}, "solar": ..., "temp": ...}
    """
    groups = {"wind": WIND_POINTS, "solar": SOLAR_POINTS, "temp": TEMP_POINTS}
    out: dict = {"time": None}
    for g, varlist in variables.items():
        if not varlist:
            continue
        pts = groups[g]
        hourly_list = _multi(pts, base_url, {**extra, "hourly": ",".join(varlist)})
        gout: dict = {v: {} for v in varlist}
        for p, hourly in zip(pts, hourly_list):
            if out["time"] is None:
                out["time"] = hourly.get("time", [])
            for v in varlist:
                gout[v][p[0]] = hourly.get(v, [])
        out[g] = gout
        time.sleep(0.5)
    return out


def weighted_index(values_by_point: dict[str, list], points, i: int, transform=None):
    """Gewogen gemiddelde over de punten op positie i (None-waarden tellen niet mee)."""
    num = den = 0.0
    for name, _lat, _lon, w in points:
        series = values_by_point.get(name) or []
        if i >= len(series) or series[i] is None:
            continue
        v = float(series[i])
        if transform is not None:
            v = transform(v)
            if v is None:
                continue
        num += w * v
        den += w
    return num / den if den > 0 else None
