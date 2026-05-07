"""
event_plausibility.py — EVENT_PLAUSIBILITY_LAYER voor stroomvoorspeller.nl v2.1

Post-processing module die ná de price-prediction draait.
WIJZIGT DE VOORSPELDE PRIJS NIET — evalueert alleen hoe realistisch
de voorspelde marktsituatie is op basis van historische analogen.

Positie in pipeline:
    forecast_hour → v2.0 prediction → compute_event_plausibility() → final output

Vragen die deze module beantwoordt:
    "Hoe vaak hebben we historisch situaties gezien die lijken op dit voorspelde uur?"
    "Is een extreme prijs (zoals −18 EUR/MWh) realistisch gegeven de weerssituatie?"

De plausibility-score verandert de voorspelling NIET. Hij communiceert hoe zeldzaam
de voorspelde situatie historisch is, zodat de gebruiker de onzekerheid beter kan
inschatten. Extreme prijzen blijven mogelijk maar worden gemarkeerd als ze historisch
zeldzaam zijn.

Ontwerpbeslissingen:
- Geen machine learning: volledig deterministisch en herleidbaar.
- Geen deling door nul: overal expliciete fallbacks.
- Snel genoeg voor uurlijkse forecasts: lineaire scan van ≤90 × 24 = 2160 entries.
- Caching-vriendelijk: de analog search is een pure functie zonder side-effects.
- Kleine datasets veilig: score = 0 bij N = 0; fallback op ruwe P_negative bij
  geen actuals.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Drempelwaarden voor analogie-zoekopdracht (Step 2)
# ---------------------------------------------------------------------------

SOLAR_RATIO_TOLERANCE = 0.15   # ±15% verschil in solar_ratio
WIND_SPEED_TOLERANCE  = 2.0    # ±2 m/s verschil in windsnelheid (op 100m)
TEMP_TOLERANCE        = 3.0    # ±3°C verschil in dagtemperatuur
MONTH_TOLERANCE       = 1      # ±1 maand (seizoenspariteit)

# ---------------------------------------------------------------------------
# Drempel negatieve prijs reality-check (Step 4)
# ---------------------------------------------------------------------------

NEGATIVE_PRICE_THRESHOLD = -5.0   # EUR/MWh; onder deze grens = extreme negatieve prijs

# ---------------------------------------------------------------------------
# Plausibility score normalisatie (Step 3)
# ---------------------------------------------------------------------------

# N = PLAUSIBILITY_N_MAX analogen → score ≈ 1.0
# Keuze 50: bij 90 dagen × 1 soortgelijk uur/dag ≈ 90 punten max; 50 is
# het streefpunt voor "goed gevulde dataset". Aanpasbaar zodra meer history
# beschikbaar is.
PLAUSIBILITY_N_MAX = 50

# ---------------------------------------------------------------------------
# Label-drempelwaarden (Step 5)
# ---------------------------------------------------------------------------

PLAUSIBILITY_HIGH   = 0.7   # score > 0.7  → "HIGH"
PLAUSIBILITY_NORMAL = 0.4   # score > 0.4  → "NORMAL"
PLAUSIBILITY_LOW    = 0.2   # score > 0.2  → "LOW"
                             # score ≤ 0.2  → "VERY_RARE_EVENT"


# ---------------------------------------------------------------------------
# Hulpfuncties
# ---------------------------------------------------------------------------

def _day_type_from_dt(dt: datetime) -> str:
    """
    Categoriseer dag als 'werkdag', 'zaterdag', of 'zondag_feestdag'.

    We gebruiken drie categorieën in plaats van de zeven weekdagen om de
    analog search niet te restrictief te maken. Werkdagen (ma–vr) vertonen
    een vergelijkbaar vraagpatroon; zaterdag en zondag zijn structureel anders.

    NL feestdagen worden hier NIET apart afgesplitst — dat zou de steekproef
    te klein maken. De plausibility-score neemt toch mee dat zulke uren zeldzaam
    zijn (lage N).
    """
    wd = dt.weekday()   # 0=maandag … 6=zondag
    if wd == 6:
        return "zondag_feestdag"
    if wd == 5:
        return "zaterdag"
    return "werkdag"


def _parse_iso(iso_str: str) -> Optional[datetime]:
    """Parseer ISO-8601-string naar datetime; retourneer None bij fout."""
    try:
        return datetime.fromisoformat(iso_str)
    except (ValueError, TypeError, AttributeError):
        return None


def _month_distance(m1: int, m2: int) -> int:
    """
    Afstand tussen twee maandnummers op een cirkel (1–12).

    Voorbeeld: afstand(1, 12) = 1, niet 11.
    """
    diff = abs(m1 - m2)
    return min(diff, 12 - diff)


# ---------------------------------------------------------------------------
# Step 2 — Analogie-zoekopdracht
# ---------------------------------------------------------------------------

def find_analog_hours(
    forecast_hour: dict,
    historical_data: list[dict],
) -> list[dict]:
    """
    Zoek historische uren die qua weersomstandigheden en dagtype lijken op
    het voorspelde uur.

    Een historisch uur telt als analoog als ALLE vijf condities gelden:
        1. |solar_ratio_verschil| ≤ 0.15
        2. |wind_ms_verschil|    ≤ 2.0 m/s
        3. |temp_c_verschil|     ≤ 3.0 °C
        4. zelfde day_type-categorie (werkdag / zaterdag / zondag_feestdag)
        5. maandverschil         ≤ 1 maand

    De zoekopdracht is deterministisch: dezelfde inputs → dezelfde output.

    Parameters
    ----------
    forecast_hour : dict
        Minimaal vereiste velden:
            target_time  : ISO-string  (voor dag-type en maand)
            solar_ratio  : float       (of sw_ratio_h als fallback)
            wind_ms      : float
            temp_c       : float
    historical_data : list[dict]
        Entries uit prediction_log.json. Entries zonder wind_ms of temp_c
        worden automatisch overgeslagen (achterwaartse compatibiliteit met
        log-entries geschreven vóór v2.1).

    Returns
    -------
    list[dict]
        Subset van historical_data die overeenkomen met de criteria.
        Volgorde is gelijk aan de invoer (deterministisch).
    """
    # Extraheer forecast-eigenschappen; 'solar_ratio' is de primaire naam,
    # 'sw_ratio_h' is de interne naam in prediction_log entries (achterwaarts).
    fc_solar = (
        forecast_hour.get("solar_ratio")
        or forecast_hour.get("sw_ratio_h")
        or 0.0
    )
    fc_wind  = forecast_hour.get("wind_ms", 0.0)
    fc_temp  = forecast_hour.get("temp_c", 10.0)
    fc_dt    = _parse_iso(forecast_hour.get("target_time", ""))

    if fc_dt is None:
        # Geen bruikbare tijdstempel → geen analogen mogelijk
        return []

    fc_month  = fc_dt.month
    fc_dtype  = _day_type_from_dt(fc_dt)

    analogs: list[dict] = []
    for entry in historical_data:
        h_solar = entry.get("sw_ratio_h")    # naam in prediction_log
        h_wind  = entry.get("wind_ms")
        h_temp  = entry.get("temp_c")
        h_time  = entry.get("target_time", "")

        # Sla over als verplichte velden ontbreken (pre-v2.1 log-entries)
        if h_solar is None or h_wind is None or h_temp is None:
            continue

        h_dt = _parse_iso(h_time)
        if h_dt is None:
            continue

        # Conditie 1: solar_ratio
        if abs(float(h_solar) - float(fc_solar)) > SOLAR_RATIO_TOLERANCE:
            continue

        # Conditie 2: wind
        if abs(float(h_wind) - float(fc_wind)) > WIND_SPEED_TOLERANCE:
            continue

        # Conditie 3: temperatuur
        if abs(float(h_temp) - float(fc_temp)) > TEMP_TOLERANCE:
            continue

        # Conditie 4: dagtype-categorie
        if _day_type_from_dt(h_dt) != fc_dtype:
            continue

        # Conditie 5: seizoen (maandcirkel)
        if _month_distance(h_dt.month, fc_month) > MONTH_TOLERANCE:
            continue

        analogs.append(entry)

    return analogs


# ---------------------------------------------------------------------------
# Step 3 — Plausibility score
# ---------------------------------------------------------------------------

def compute_plausibility_score(n_analogs: int) -> float:
    """
    Bereken plausibility score op basis van het aantal analoge uren.

    Formule:
        score = min(1.0, log(N + 1) / log(PLAUSIBILITY_N_MAX + 1))

    Eigenschappen:
        N = 0  → score = 0.000  (situatie nooit eerder gezien)
        N = 1  → score ≈ 0.176
        N = 7  → score ≈ 0.520
        N = 20 → score ≈ 0.775
        N = 50 → score ≈ 1.000  (referentie-normalisatie)

    Keuze voor logaritmische schaal: de informatiewaarde van de eerste paar
    analogen is groter dan die van de vijftigste. Lineaire schaling zou de
    score onnodig laag houden voor veel-voorkomende situaties.
    """
    if n_analogs <= 0:
        return 0.0
    return min(1.0, math.log(n_analogs + 1) / math.log(PLAUSIBILITY_N_MAX + 1))


# ---------------------------------------------------------------------------
# Step 4 — Extreme event reality check
# ---------------------------------------------------------------------------

def compute_realistic_negative_probability(
    p_negative: float,
    analog_hours: list[dict],
) -> float:
    """
    Bereken de realistische kans op negatieve prijs door het model-P_negative
    te combineren met de historische frequentie bij vergelijkbare uren.

    Formule:
        realistic_P = P_negative × (# analogen met actual < 0) / N_actuals

    Rationale:
        Het model kan P_negative hoog inschatten puur op basis van weersfactoren.
        Als historisch gelijkaardige uren zelden werkelijk negatief waren, is
        die hoge kans minder geloofwaardig. Deze correctie damt één historisch
        extreem event in (bijv. −413 EUR/MWh op 26 april) zonder hem volledig
        te negeren.

    Alleen entries met ingevuld 'actual'-veld (≠ None) tellen mee.
    Als er nog geen actuals zijn (eerste weken na livegang) wordt de ruwe
    P_negative teruggegeven als conservatieve schatting.

    Parameters
    ----------
    p_negative : float
        Model-output kans op negatieve prijs (0.0 – 1.0).
    analog_hours : list[dict]
        Historische analogen gevonden door find_analog_hours().

    Returns
    -------
    float
        Gecorrigeerde kans op negatieve prijs (0.0 – 1.0), afgerond op 4 decimalen.
    """
    actuals = [
        h["actual"]
        for h in analog_hours
        if h.get("actual") is not None
    ]
    n_actuals = len(actuals)

    if n_actuals == 0:
        # Geen verificatie-data beschikbaar: gebruik ruwe model-kans (conservatief)
        return round(float(p_negative), 4)

    negative_count = sum(1 for a in actuals if a < 0)
    historical_negative_rate = negative_count / n_actuals
    realistic = float(p_negative) * historical_negative_rate
    return round(realistic, 4)


# ---------------------------------------------------------------------------
# Step 5 — Label
# ---------------------------------------------------------------------------

def plausibility_label(score: float) -> str:
    """
    Vertaal plausibility score naar een leesbaar label voor UI en logging.

    Labels zijn bewust in het Engels om consistent te zijn met de regime-
    labels in forecast.py (REGIME_NORMAL, REGIME_OVERSUPPLY, etc.).
    """
    if score > PLAUSIBILITY_HIGH:
        return "HIGH"
    if score > PLAUSIBILITY_NORMAL:
        return "NORMAL"
    if score > PLAUSIBILITY_LOW:
        return "LOW"
    return "VERY_RARE_EVENT"


# ---------------------------------------------------------------------------
# Hoofdfunctie (Step 1 t/m 6)
# ---------------------------------------------------------------------------

def compute_event_plausibility(
    forecast_hour: dict,
    historical_data: list[dict],
) -> dict:
    """
    Bereken de event plausibility voor één voorspeld uur.

    Dit is de integratiepunt-functie van de EVENT_PLAUSIBILITY_LAYER.
    Wordt aangeroepen ná forecast_one() in run_forecast.py.

    WIJZIGT DE VOORSPELDE PRIJS NIET.

    Parameters
    ----------
    forecast_hour : dict
        Eén verrijkt forecast-dict uit run_forecast.py met minimaal:
            target_time           : ISO-string van het voorspelde uur
            predicted             : voorspelde prijs (EUR/MWh)
            solar_ratio           : uurspecifieke zonratio (sw_ratio_h)
            wind_ms               : windsnelheid op 100m (m/s)
            temp_c                : dagtemperatuur (°C)
            P_negative            : kans op negatieve prijs (0.0–1.0), optioneel

    historical_data : list[dict]
        Entries uit prediction_log.json (maximaal 90 dagen, ~2160 entries).
        Entries zonder wind_ms of temp_c (pre-v2.1) worden automatisch
        overgeslagen in de analog search.

    Returns
    -------
    dict
        Uitbreiding van de forecast output:
            event_plausibility_score          : float [0.0 – 1.0]
            event_plausibility_label          : str
            analog_sample_size                : int
            realistic_negative_probability    : float  (alleen als predicted < −5 EUR/MWh)

    Voorbeeld output:
        {
            "event_plausibility_score": 0.31,
            "event_plausibility_label": "LOW",
            "analog_sample_size": 7,
            "realistic_negative_probability": 0.08
        }
    """
    # Step 2: zoek analoge uren
    analogs = find_analog_hours(forecast_hour, historical_data)
    n = len(analogs)

    # Step 3: plausibility score
    score = compute_plausibility_score(n)

    # Step 5: label
    label = plausibility_label(score)

    result: dict = {
        "event_plausibility_score": round(score, 4),
        "event_plausibility_label": label,
        "analog_sample_size": n,
    }

    # Step 4: reality check alleen bij extreme negatieve voorspellingen
    predicted_price = float(forecast_hour.get("predicted", 0.0))
    p_negative = float(
        forecast_hour.get("P_negative")
        or forecast_hour.get("extreme_event_prob")
        or 0.0
    )

    if predicted_price < NEGATIVE_PRICE_THRESHOLD:
        rnp = compute_realistic_negative_probability(p_negative, analogs)
        result["realistic_negative_probability"] = rnp

    return result


# ---------------------------------------------------------------------------
# Unit tests (draai met: python event_plausibility.py)
# ---------------------------------------------------------------------------

def _run_tests() -> None:
    """Eenvoudige sanity checks voor alle kernfuncties."""
    import json

    print("--- event_plausibility.py unit tests ---\n")

    # ---- Test 1: compute_plausibility_score ----
    assert compute_plausibility_score(0)  == 0.0, "N=0 moet 0.0 geven"
    score_7  = compute_plausibility_score(7)
    score_50 = compute_plausibility_score(50)
    score_99 = compute_plausibility_score(99)
    assert 0.4 < score_7  < 0.7,  f"N=7 moet tussen 0.4 en 0.7 liggen, kreeg {score_7:.3f}"
    assert 0.9 < score_50 <= 1.0, f"N=50 moet ≈1.0 zijn, kreeg {score_50:.3f}"
    assert score_99 == 1.0,       f"N=99 moet begrensd op 1.0, kreeg {score_99:.3f}"
    print(f"[ok] compute_plausibility_score: N=0→{compute_plausibility_score(0):.3f}, "
          f"N=7→{score_7:.3f}, N=50→{score_50:.3f}, N=99→{score_99:.3f}")

    # ---- Test 2: plausibility_label ----
    assert plausibility_label(0.9) == "HIGH"
    assert plausibility_label(0.6) == "NORMAL"
    assert plausibility_label(0.3) == "LOW"
    assert plausibility_label(0.1) == "VERY_RARE_EVENT"
    assert plausibility_label(0.0) == "VERY_RARE_EVENT"
    print("[ok] plausibility_label: alle categorieën correct")

    # ---- Test 3: find_analog_hours — happy path ----
    fc_hour = {
        "target_time": "2026-05-15T13:00:00",  # vrijdag, mei
        "solar_ratio": 1.6,
        "wind_ms": 7.0,
        "temp_c": 15.0,
        "predicted": -18.0,
        "P_negative": 0.63,
    }
    history = [
        # Match: alles binnen tolerantie
        {"target_time": "2026-05-08T13:00:00", "sw_ratio_h": 1.65, "wind_ms": 7.5,
         "temp_c": 14.0, "actual": -22.0, "predicted": -15.0},
        # Match: maandgrens (april = 1 maand van mei)
        {"target_time": "2026-04-17T13:00:00", "sw_ratio_h": 1.55, "wind_ms": 6.5,
         "temp_c": 15.5, "actual": 5.0, "predicted": 3.0},
        # Geen match: solar te ver weg
        {"target_time": "2026-05-03T13:00:00", "sw_ratio_h": 0.4, "wind_ms": 7.0,
         "temp_c": 15.0, "actual": 30.0, "predicted": 28.0},
        # Geen match: weekend (zaterdag) terwijl forecast = vrijdag (werkdag)
        {"target_time": "2026-05-09T13:00:00", "sw_ratio_h": 1.58, "wind_ms": 7.2,
         "temp_c": 14.5, "actual": -10.0, "predicted": -8.0},
        # Geen match: maand te ver (februari)
        {"target_time": "2026-02-13T13:00:00", "sw_ratio_h": 1.61, "wind_ms": 7.0,
         "temp_c": 14.8, "actual": 50.0, "predicted": 48.0},
        # Geen match: ontbrekende wind_ms (pre-v2.1 entry)
        {"target_time": "2026-05-07T13:00:00", "sw_ratio_h": 1.62, "temp_c": 14.9,
         "actual": -5.0, "predicted": -3.0},
    ]
    analogs = find_analog_hours(fc_hour, history)
    assert len(analogs) == 2, f"Verwachtte 2 analogen, kreeg {len(analogs)}: {analogs}"
    print(f"[ok] find_analog_hours: {len(analogs)} analogen gevonden (verwacht 2)")

    # ---- Test 4: realistic_negative_probability ----
    p_neg = 0.63
    rnp_no_actuals = compute_realistic_negative_probability(p_neg, [])
    assert rnp_no_actuals == round(p_neg, 4), \
        f"Geen analogen → fallback op P_negative={p_neg}, kreeg {rnp_no_actuals}"

    analogs_with_actuals = [
        {"actual": -22.0},   # negatief
        {"actual": 5.0},     # positief
    ]
    rnp = compute_realistic_negative_probability(p_neg, analogs_with_actuals)
    expected = round(p_neg * 0.5, 4)   # 1/2 analogen zijn negatief
    assert rnp == expected, f"Verwachtte {expected}, kreeg {rnp}"
    print(f"[ok] compute_realistic_negative_probability: "
          f"geen actuals→{rnp_no_actuals:.4f}, 1/2 negatief→{rnp:.4f}")

    # ---- Test 5: compute_event_plausibility — volledig ----
    result = compute_event_plausibility(fc_hour, history)
    assert "event_plausibility_score"  in result
    assert "event_plausibility_label"  in result
    assert "analog_sample_size"        in result
    assert "realistic_negative_probability" in result,  \
        "predicted < -5 → realistic_negative_probability moet aanwezig zijn"
    assert result["analog_sample_size"] == 2
    assert result["event_plausibility_label"] in ("HIGH", "NORMAL", "LOW", "VERY_RARE_EVENT")
    print(f"[ok] compute_event_plausibility:")
    print(f"     {json.dumps(result, indent=5)}")

    # ---- Test 6: geen divisie door nul bij N=0 ----
    result_empty = compute_event_plausibility(fc_hour, [])
    assert result_empty["analog_sample_size"] == 0
    assert result_empty["event_plausibility_score"] == 0.0
    assert result_empty["event_plausibility_label"] == "VERY_RARE_EVENT"
    assert result_empty["realistic_negative_probability"] == round(0.63, 4)
    print(f"[ok] geen divisie door nul bij lege historische dataset")

    # ---- Test 7: positieve prijs → geen realistic_negative_probability ----
    fc_positive = {**fc_hour, "predicted": 45.0}
    result_pos = compute_event_plausibility(fc_positive, history)
    assert "realistic_negative_probability" not in result_pos, \
        "predicted > -5 → realistic_negative_probability mag niet aanwezig zijn"
    print("[ok] geen realistic_negative_probability bij positieve prijs")

    # ---- Test 8: _month_distance cirkel ----
    assert _month_distance(1, 12) == 1,   "jan–dec afstand moet 1 zijn"
    assert _month_distance(6, 8)  == 2,   "jun–aug afstand moet 2 zijn"
    assert _month_distance(1, 7)  == 6,   "jan–jul afstand moet 6 zijn"
    print("[ok] _month_distance cirkelberekening correct")

    print("\n[ok] Alle tests geslaagd.")


if __name__ == "__main__":
    _run_tests()
