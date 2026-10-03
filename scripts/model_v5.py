"""
model_v5.py — rekenkern van voorspellingsmodel v5 (alleen standaardbibliotheek).

Opbouw van één voorspeld uur:

    basis      = 0,5 x v4-baseline (28 dagen, zelfde uur)
               + 0,5 x profiel van de 5 meest vergelijkbare dagen (zon en wind)
                   uit de laatste 21 dagen
    correctie  = lineair model per uur van de dag op het verwachte weer:
                   wind NL+DE (100 m, 9 punten), zon (5 punten), temperatuur,
                   zaterdag/zondag, gasprijsverandering
    voorspelling = basis + a(L) x correctie

L = aantal dagen tussen de laatst bekende prijsdag en de doeldag. a(L) loopt af van
1,0 (dag 1-2) naar 0,6 (dag 6+): verder vooruit is de weersverwachting minder
zeker, dus de correctie telt minder.

De gewichten van het lineaire model komen uit public/data/model_v5.json. Dat bestand
schrijft refit_model_v5.py (wekelijks, GitHub Actions) op alle data sinds februari
2024, met het weer zoals het op het moment van voorspellen verwacht werd.

Backtest juli 2024 - sept 2026 (01-documenten/ab-model-v5-uitkomst.md): MAE 25,2
(v4-baseline) -> 21,1 EUR/MWh; in het live-venster van v4 (20 aug - 1 okt 2026)
34,1 -> 28,1.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Optional

from forecast import dagtype, is_crossborder_feestdag
from weather_points import (WIND_POINTS, SOLAR_POINTS, TEMP_POINTS, capacity_factor)

MODEL_VERSION = "5.0"

REF_DAYS = 28            # referentievenster weer (zelfde lengte als de lange mediaan)
LONG_DAYS = 28           # lange mediaan van de v4-baseline
ANALOG_N = 5
ANALOG_BACK = 21
ANALOG_MIX = 0.5
RIDGE_LAMBDA = 30.0
ALPHA_BY_LEAD = {1: 1.0, 2: 1.0, 3: 0.9, 4: 0.8, 5: 0.7, 6: 0.6}
BAND_SCALE_BY_LEAD = {1: 0.80, 2: 0.85, 3: 0.90, 4: 0.95, 5: 1.0, 6: 1.0}
BAND_ABS = 17.0
BAND_REL = 0.25
DELTA_CLIP = 250.0       # EUR/MWh — vangnet tegen een kapot invoerbestand

FEATURES = [
    "wind_uur",        # windindex dit uur - gemiddelde laatste 28 dagen
    "wind_dag",        # windindex daggemiddelde - idem
    "zon_uur",         # instraling dit uur - gemiddelde zelfde uur laatste 28 dagen (W/m2)
    "temp_dag",        # daggemiddelde temperatuur - gemiddelde laatste 28 dagen
    "zaterdag",
    "zondag_feestdag",
    "gas",             # TTF nu / gemiddelde 30 dagen - 1
    "wind_uur_kw",     # kwadraat van wind_uur (wind werkt niet lineair)
    "stookgraad",      # max(0, 15 - temp) - zelfde voor de referentie
]
GROUPS = [
    ("wind", [0, 1, 7]),
    ("zon", [2]),
    ("temperatuur", [3, 8]),
    ("dagtype", [4, 5]),
    ("gas", [6]),
]


# ---------------------------------------------------------------------------
# Hulpjes
# ---------------------------------------------------------------------------

def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2


def _pstd(xs):
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def is_weekendish(d: date) -> bool:
    return dagtype(_dt(d)) != "werkdag"


def _dt(d: date):
    from datetime import datetime
    return datetime(d.year, d.month, d.day, 12)


# ---------------------------------------------------------------------------
# Prijzen per dag
# ---------------------------------------------------------------------------

def prices_by_day(history: list[dict]) -> dict:
    """{date: [24 prijzen]} uit [{time, price}]; ontbrekende uren (zomertijd) gevuld."""
    from datetime import datetime
    out: dict = {}
    for e in history:
        try:
            t = datetime.fromisoformat(e["time"])
            v = float(e["price"])
        except (KeyError, ValueError, TypeError):
            continue
        row = out.setdefault(t.date(), [None] * 24)
        if row[t.hour] is None:
            row[t.hour] = v
    for d, row in out.items():
        known = [v for v in row if v is not None]
        if len(known) < 20:          # onvolledige dag: niet bijvullen, valt hieronder af
            continue
        for h in range(24):
            if row[h] is None:
                row[h] = row[h - 1] if h > 0 and row[h - 1] is not None else sum(known) / len(known)
    return {d: r for d, r in out.items() if all(v is not None for v in r)}


def baseline_v4(P: dict, k: date, target: date) -> Optional[list]:
    """
    v4-niveauschatter met historie t/m dag k (zelfde rekenregels als
    forecast._v4_parts, maar per dag in plaats van per uur — 24x sneller, en
    getest op gelijke uitkomst).
    """
    tt = dagtype(_dt(target))
    wk = tt in ("weekend", "feestdag")
    short_days = 14 if wk else 7

    def ok_day(d):
        return d in P and not (tt == "werkdag" and is_crossborder_feestdag(_dt(d)))

    short = [k - timedelta(j) for j in range(short_days)]
    short = [d for d in short if ok_day(d) and dagtype(_dt(d)) == tt]
    long_ = [k - timedelta(j) for j in range(LONG_DAYS)]
    long_ = [d for d in long_ if ok_day(d) and (dagtype(_dt(d)) in ("weekend", "feestdag")) == wk]

    def daymean(n):
        vals = [v for j in range(n) for v in P.get(k - timedelta(j), [])]
        return sum(vals) / len(vals) if vals else None

    rec, lon = daymean(7), daymean(28)
    ratio = 1.0
    if rec is not None and lon is not None and abs(lon) > 5:
        ratio = min(2.0, max(0.5, rec / lon))
    out = []
    for h in range(24):
        ms = _median([P[d][h] for d in short]) if short else None
        ml = _median([P[d][h] for d in long_]) if long_ else None
        if ms is None and ml is None:
            return None
        base = ml if ms is None else ms if ml is None else 0.25 * ms + 0.75 * ml
        out.append(base * ratio ** 0.25)
    return out


# ---------------------------------------------------------------------------
# Weer: indices per dag
# ---------------------------------------------------------------------------

def daily_indices(series: dict, var_wind: str, var_solar: str, var_temp: str) -> dict:
    """
    series = uitvoer van weather_points.fetch_point_series.
    Return {date: {"w": [24], "s": [24], "t": [24], "ws": [24]}} (alleen complete dagen).
    w = windindex (capaciteitsfactor 0..1), ws = gemiddelde windsnelheid op 100 m (m/s),
    s = instraling (W/m2), t = temperatuur (graden C); alles gewogen over de punten.
    """
    from datetime import datetime
    times = series.get("time") or []
    groups = [("w", "wind", var_wind, WIND_POINTS, capacity_factor),
              ("s", "solar", var_solar, SOLAR_POINTS, None),
              ("t", "temp", var_temp, TEMP_POINTS, None),
              ("ws", "wind", var_wind, WIND_POINTS, None)]
    out: dict = {}
    seen = set()
    for i, ts in enumerate(times):
        t = datetime.fromisoformat(ts)
        key = (t.date(), t.hour)
        if key in seen:
            continue
        seen.add(key)
        rec = out.setdefault(t.date(), {k_: [None] * 24 for k_ in ("w", "s", "t", "ws")})
        for short, g, var, pts, tf in groups:
            by_pt = (series.get(g) or {}).get(var) or {}
            num = den = 0.0
            for name, _la, _lo, wgt in pts:
                arr = by_pt.get(name) or []
                if i >= len(arr) or arr[i] is None:
                    continue
                v = float(arr[i])
                if tf is not None:
                    v = tf(v)
                num += wgt * v
                den += wgt
            rec[short][t.hour] = num / den if den > 0 else None
    for d, rec in out.items():           # zomertijd-gat dichten
        for key in ("w", "s", "t", "ws"):
            row = rec[key]
            for h in range(24):
                if row[h] is None and h > 0 and row[h - 1] is not None:
                    row[h] = row[h - 1]
    return {d: r for d, r in out.items()
            if all(v is not None for key in ("w", "s", "t", "ws") for v in r[key])}


def reference(obs: dict, k: date) -> Optional[dict]:
    """Gemiddeld weer over de 28 dagen t/m k (waargenomen = verwachting met lead 0)."""
    ds = [k - timedelta(j) for j in range(REF_DAYS)]
    ds = [d for d in ds if d in obs]
    if len(ds) < REF_DAYS // 2:
        return None
    w = [v for d in ds for v in obs[d]["w"]]
    t = [v for d in ds for v in obs[d]["t"]]
    ws = [v for d in ds for v in obs[d].get("ws", [])]
    s = [sum(obs[d]["s"][h] for d in ds) / len(ds) for h in range(24)]
    return {"w": sum(w) / len(w), "s": s, "t": sum(t) / len(t),
            "ws": sum(ws) / len(ws) if ws else None}


def analog_profile(P: dict, obs: dict, k: date, target: date,
                   s_mean: float, w_mean: float) -> Optional[list]:
    """Mediaanprofiel van de 5 dagen (laatste 21 t/m k) die qua zon en wind het meest lijken."""
    wk = is_weekendish(target)
    cand = [k - timedelta(j) for j in range(ANALOG_BACK - 1, -1, -1)]
    cand = [d for d in cand if d in P and d in obs and is_weekendish(d) == wk]
    if not cand:
        return None
    sc = [sum(obs[d]["s"]) / 24 for d in cand]
    wc = [sum(obs[d]["w"]) / 24 for d in cand]
    ss = max(_pstd(sc), 1.0)
    sw = max(_pstd(wc), 0.05)
    dist = [abs(a - s_mean) / ss + abs(b - w_mean) / sw for a, b in zip(sc, wc)]
    order = sorted(range(len(cand)), key=lambda i: dist[i])[:ANALOG_N]
    sel = [cand[i] for i in order]
    return [_median([P[d][h] for d in sel]) for h in range(24)]


# ---------------------------------------------------------------------------
# Kenmerken en model
# ---------------------------------------------------------------------------

def hour_features(target: date, fc: dict, ref: dict, gas: float) -> list:
    """24 kenmerkvectoren (zie FEATURES) voor een doeldag."""
    w_mean = sum(fc["w"]) / 24
    t_mean = sum(fc["t"]) / 24
    wd = target.weekday()
    sat = 1.0 if wd == 5 else 0.0
    sun = 1.0 if (wd == 6 or dagtype(_dt(target)) == "feestdag") else 0.0
    hdd = max(0.0, 15 - t_mean) - max(0.0, 15 - ref["t"])
    rows = []
    for h in range(24):
        dw = fc["w"][h] - ref["w"]
        rows.append([dw, w_mean - ref["w"], fc["s"][h] - ref["s"][h], t_mean - ref["t"],
                     sat, sun, gas, dw * dw, hdd])
    return rows


def ridge_fit(X: list, y: list, lam: float = RIDGE_LAMBDA) -> dict:
    """Ridge op gestandaardiseerde kenmerken, intercept onbestraft. Pure Python."""
    n, p = len(X), len(X[0])
    mu = [sum(r[j] for r in X) / n for j in range(p)]
    sd = []
    for j in range(p):
        v = math.sqrt(sum((r[j] - mu[j]) ** 2 for r in X) / n)
        sd.append(v if v > 1e-9 else 1.0)
    q = p + 1
    M = [[0.0] * q for _ in range(q)]
    b = [0.0] * q
    for r, yy in zip(X, y):
        z = [(r[j] - mu[j]) / sd[j] for j in range(p)] + [1.0]
        for a in range(q):
            za = z[a]
            b[a] += za * yy
            row = M[a]
            for c in range(q):
                row[c] += za * z[c]
    for a in range(p):
        M[a][a] += lam
    # Gauss-eliminatie met pivotering
    A = [M[i][:] + [b[i]] for i in range(q)]
    for c in range(q):
        piv = max(range(c, q), key=lambda r_: abs(A[r_][c]))
        A[c], A[piv] = A[piv], A[c]
        for r_ in range(q):
            if r_ != c and A[r_][c] != 0:
                f = A[r_][c] / A[c][c]
                for cc in range(c, q + 1):
                    A[r_][cc] -= f * A[c][cc]
    beta = [A[i][q] / A[i][i] for i in range(q)]
    return {"mu": mu, "sd": sd, "beta": beta[:p], "intercept": beta[p]}


def apply_hour(model_h: dict, x: list) -> tuple[float, list]:
    """Correctie in EUR/MWh plus bijdrage per kenmerk (som + intercept = correctie)."""
    contrib = [bj * (xj - m) / s for bj, xj, m, s in
               zip(model_h["beta"], x, model_h["mu"], model_h["sd"])]
    return model_h["intercept"] + sum(contrib), contrib


def lead_alpha(L: int) -> float:
    return ALPHA_BY_LEAD.get(L, ALPHA_BY_LEAD[6] if L > 6 else 1.0)


def band_half(pred: float, L: int) -> float:
    scale = BAND_SCALE_BY_LEAD.get(L, 1.0)
    return scale * (BAND_ABS + BAND_REL * abs(pred))


def forecast_day(P: dict, obs: dict, fc_day: dict, k: date, target: date, gas: float,
                 coefs: Optional[dict]) -> Optional[dict]:
    """
    Voorspelling voor één doeldag. P = prijzen t/m k, obs = waargenomen weerindices,
    fc_day = verwacht weer voor de doeldag {"w","s","t"}, coefs = model_v5.json.
    Return dict met per uur: base, b4, analog, delta, pred, contrib-groepen.
    """
    L = (target - k).days
    b4 = baseline_v4(P, k, target)
    ref = reference(obs, k)
    if b4 is None or ref is None:
        return None
    s_mean = sum(fc_day["s"]) / 24
    w_mean = sum(fc_day["w"]) / 24
    an = analog_profile(P, obs, k, target, s_mean, w_mean)
    base = [(1 - ANALOG_MIX) * b + ANALOG_MIX * a for b, a in zip(b4, an)] if an else list(b4)
    X = hour_features(target, fc_day, ref, gas)
    alpha = lead_alpha(L)
    hours = []
    for h in range(24):
        delta, contrib, groups = 0.0, [0.0] * len(FEATURES), {}
        if coefs:
            delta, contrib = apply_hour(coefs["hours"][h], X[h])
            delta = max(-DELTA_CLIP, min(DELTA_CLIP, delta))
        pred = base[h] + alpha * delta
        for gname, idx in GROUPS:
            groups[gname] = alpha * sum(contrib[j] for j in idx)
        groups["vast"] = alpha * (coefs["hours"][h]["intercept"] if coefs else 0.0)
        hours.append({"hour": h, "b4": b4[h], "analog": an[h] if an else None,
                      "base": base[h], "delta": alpha * delta, "pred": pred,
                      "groups": groups, "x": X[h], "lead": L})
    return {"lead": L, "alpha": alpha, "ref": ref, "hours": hours, "gas": gas,
            "target": target, "w_mean": w_mean, "s_mean": s_mean,
            "t_mean": sum(fc_day["t"]) / 24,
            "ws_mean": (sum(fc_day["ws"]) / 24) if fc_day.get("ws") else None,
            "s_hour": list(fc_day["s"])}


# ---------------------------------------------------------------------------
# Uitleg per uur (factor-paneel op de site, duiding op /morgen)
# ---------------------------------------------------------------------------

def _fmt(x: float, nd: int = 1) -> str:
    return f"{x:.{nd}f}".replace(".", ",")


def explain_hour(day: dict, h: int) -> list[dict]:
    """
    Factorlijst voor één uur. 'eur' = bijdrage in EUR/MWh ten opzichte van de
    v4-baseline; 'points' = dezelfde bijdrage afgerond in ct/kWh (voor de
    bestaande tekstgeneratoren). Som van alle 'eur' + baseline = voorspelling.
    """
    hr = day["hours"][h]
    ref = day["ref"]
    g = hr["groups"]
    wd = day["target"].weekday()
    ws = day.get("ws_mean")
    ref_ws = ref.get("ws")
    dw = day["w_mean"] - ref["w"]
    if dw < -0.08:
        wlabel = "minder wind dan normaal"
    elif dw > 0.08:
        wlabel = "meer wind dan normaal"
    else:
        wlabel = "ongeveer normale wind"
    wtxt = (f"{wlabel} ({_fmt(ws)} m/s op 100 m boven NL en DE, normaal {_fmt(ref_ws)})"
            if ws is not None and ref_ws is not None else wlabel)
    s_h = day["s_hour"][h]
    if ref["s"][h] < 5 and s_h < 5:
        ztxt = "geen zon op dit uur"
    else:
        ztxt = f"instraling {round(s_h)} W/m² (normaal {round(ref['s'][h])} W/m² op dit uur)"
    ttxt = f"{_fmt(day['t_mean'])} °C gemiddeld (normaal {_fmt(ref['t'])} °C)"
    if wd == 5:
        dtxt = "zaterdag"
    elif wd == 6 or dagtype(_dt(day["target"])) == "feestdag":
        dtxt = "zondag of feestdag"
    else:
        dtxt = "werkdag"
    gtxt = f"gasprijs {'+' if day['gas'] >= 0 else ''}{round(day['gas'] * 100)}% t.o.v. de laatste 30 dagen"
    analog = hr["base"] - hr["b4"]
    items = [
        ("vergelijkbare dagen", analog,
         "gemiddelde van de 5 dagen uit de laatste 3 weken met het meest vergelijkbare weer"),
        ("wind", g["wind"], wtxt),
        ("zon", g["zon"], ztxt),
        ("temperatuur", g["temperatuur"], ttxt),
        ("dagtype", g["dagtype"], dtxt),
        ("gas", g["gas"], gtxt),
        ("vaste correctie", g["vast"], "gemiddelde afwijking van de basisprijs in de training"),
    ]
    return [{"name": n, "eur": round(v, 2), "points": int(round(v / 10.0)), "reason": r}
            for n, v, r in items]
