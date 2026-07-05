"""
compute_bias.py — Berekent MOS bias-correcties uit prediction_log.json.

Twee modi (--mode, default 'legacy'):

  legacy   Cellen per (regime, uur, maand), vlak gemiddelde over het venster.
           Exact het gedrag van vóór v3.2 — de wekelijkse workflow draait dit
           tot de rolling-modus via backtest-A/B is gevalideerd.

  rolling  Cellen per (regime, uur) — GEEN maand-key. Fouten worden gewogen op
           recentheid (exponentieel, halfwaardetijd --half-life dagen). Lost twee
           structurele problemen van legacy op, zichtbaar in juni/juli 2026:
           1. Maandwissel-reset: op 1 juli vielen alle juni-correcties abrupt weg
              en hadden alle juli-cellen n<7 (apply=false) — precies toen het
              model 60-120 EUR/MWh te hoog zat op de avonduren.
           2. Traagheid: een vlak 60d-gemiddelde leert een regime-omslag pas na
              weken; de EWMA volgt hem in dagen (residu-persistentie).
           Gebruikt de verste voorspelling per doel-uur zoals gelogd; zodra
           run_forecast.py v3.2 'predicted_raw_latest' bijhoudt (meest recente
           run per doel-uur, kortste horizon) heeft die voorrang — dat is de
           beste maat voor de actuele systematische fout van het kale model.

Draait wekelijks via .github/workflows/compute-bias.yml (legacy). Na go-live
van v3.2 draait 'rolling' 3× per dag in update-forecast.yml, ná update_log.py.

Handmatig: python scripts/compute_bias.py [--mode rolling] [--half-life 5]

---
Analyse 13 mei 2026 (188 uurpunten, v1.9):
- Overall MAE: 20.1 EUR/MWh, overall bias: -0.4 EUR/MWh (nagenoeg nul)
- Nachturen (0-5u): bias slechts +2.1 EUR/MWh
- Dagbias geconcentreerd in solar hours: bias uur 10-16u = -12.7 EUR/MWh
  (model onderschat systematisch; MAE uur 12-13 = 35-39 EUR/MWh)
- Oversupply-trigger te agressief: sw_ratio_h 1.4-1.9 leverde positieve actuele
  prijzen (+14 tot +71 EUR/MWh) terwijl model negatieve voorspelde (-40 tot -60).
  Verwacht: grote negatieve bias in oversupply x solar x uur-10-16 cellen.
- Aandachtspunt baseline-window: het 2d-window bij solar-piekuren (oversupply) kan
  te gevoelig zijn voor één atypische dag. Op 6 mei trok het window vroeg-mei lage
  prijzen mee terwijl de actuele prijs EUR 92-110 was (bias -24.7). De bias-correctie
  vangt dit op, maar het window is structureel gevoelig voor uitschieters.
---
Analyse 5 juli 2026 (aanleiding rolling-modus, v3.2):
- Juni: model structureel te laag op avonduren (cel h20_m06: +89.5, h21_m06: +93.7).
- Begin juli: structureel te hoog (h20_m07: -122.7) — klassiek naijlen.
- Live MAE 19-21u: 96-142 EUR/MWh vs 24-30 op middaguren (performance.json 5 juli).
---
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

# ---------------------------------------------------------------------------
# Paden
# ---------------------------------------------------------------------------
PROJECT_ROOT         = Path(__file__).resolve().parent.parent
PREDICTION_LOG_FILE  = PROJECT_ROOT / "03-data" / "prediction_log.json"
BIAS_CORRECTIONS_FILE = PROJECT_ROOT / "03-data" / "bias_corrections.json"

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
WINDOW_DAYS  = 60      # Rollerend venster voor bias-berekening
MIN_SAMPLES  = 7       # legacy: minimaal aantal datapunten voor 'apply'
MIN_BIAS_EUR = 5.0     # Minimale absolute bias (EUR/MWh) om correctie te activeren

# rolling-modus (v3.2) — defaults; overschrijfbaar via CLI voor backtest-tuning
HALF_LIFE_DAYS = 5.0   # halfwaardetijd van het recentheidsgewicht
MIN_N_EFF      = 5.0   # minimale effectieve steekproefomvang (som van gewichten)

# Max correctie die mag worden TOEGEPAST (cap in run_forecast.py, niet hier):
# MAX_CORRECTION = 50.0  # EUR/MWh — documentatie; afdwinging in run_forecast.py


# ---------------------------------------------------------------------------
# Hulpfuncties
# ---------------------------------------------------------------------------

def load_prediction_log(log_file: Path) -> list[dict]:
    """Lees prediction_log.json; retourneer [] bij ontbrekend of corrupt bestand."""
    if not log_file.exists():
        print(f"[warn] {log_file} ontbreekt — geen data om te analyseren.", file=sys.stderr)
        return []
    try:
        raw = log_file.read_bytes().rstrip(b"\x00")
        data = json.loads(raw) if raw else []
        print(f"[info] {len(data)} entries geladen uit prediction_log.", file=sys.stderr)
        return data
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[err] prediction_log.json corrupt: {exc}", file=sys.stderr)
        return []


def infer_regime_bucket(entry: dict) -> str:
    """
    Bepaal regime-bucket op basis van beschikbare velden.

    v2.1-entries: gebruik 'regime' direct als die beschikbaar is.
    v1.9-entries (geen regime/wind_ms/temp_c): gebruik proxy op basis van
    sw_ratio_h en sw_ratio_daily:
      - oversupply: sw_ratio_h >= 1.40 EN uur 8-18
      - scarcity:   sw_ratio_daily < 0.60
      - normaal:    alles overige

    Zie methodologie-voorspellingsmodel.md sectie 4 en bias-correctie-ontwerp.md.
    """
    # v2.1: regime-veld direct aanwezig — gebruik dit bij voorkeur
    regime = entry.get("regime")
    if regime in ("oversupply", "normaal", "scarcity", "schaarste"):
        # Normaliseer 'schaarste' naar 'scarcity' voor consistente sleutels
        if regime == "schaarste":
            return "scarcity"
        return regime

    # v1.9 proxy-bepaling op basis van sw_ratio_h + sw_ratio_daily + uur
    t_str = entry.get("target_time", "")
    try:
        target_dt = datetime.fromisoformat(t_str)
        hour = target_dt.hour
    except (ValueError, TypeError):
        hour = None

    sw_ratio_h     = entry.get("sw_ratio_h")
    sw_ratio_daily = entry.get("sw_ratio_daily")

    if sw_ratio_h is not None and hour is not None and 8 <= hour <= 18 and sw_ratio_h >= 1.40:
        return "oversupply"
    if sw_ratio_daily is not None and sw_ratio_daily < 0.60:
        return "scarcity"
    return "normaal"


def build_cell_key(regime: str, hour: int, month: int) -> str:
    """Bouw de legacy cel-sleutel: '{regime}_h{uur:02d}_m{maand:02d}'."""
    return f"{regime}_h{hour:02d}_m{month:02d}"


def build_rolling_key(regime: str, hour: int) -> str:
    """Bouw de rolling cel-sleutel (v3.2, zonder maand): '{regime}_h{uur:02d}'."""
    return f"{regime}_h{hour:02d}"


def entry_error(entry: dict, prefer_latest: bool) -> float | None:
    """
    Fout = actual − voorspelling (positief = model te laag).

    rolling (prefer_latest=True): gebruik de meest recente kale voorspelling
    voor dit doel-uur als die er is (predicted_raw_latest, v3.2), anders
    predicted_raw, anders predicted. legacy: altijd 'predicted' (het gelogde,
    eventueel al MOS-gecorrigeerde getal) — exact het gedrag van vóór v3.2.
    """
    actual = entry.get("actual")
    if actual is None:
        return None
    if prefer_latest:
        predicted = (entry.get("predicted_raw_latest")
                     if entry.get("predicted_raw_latest") is not None
                     else entry.get("predicted_raw")
                     if entry.get("predicted_raw") is not None
                     else entry.get("predicted"))
    else:
        predicted = entry.get("predicted")
    if predicted is None:
        return None
    try:
        return float(actual) - float(predicted)
    except (ValueError, TypeError):
        return None


def parse_target(entry: dict) -> datetime | None:
    """Parse target_time naar UTC-aware datetime; None bij mislukking."""
    try:
        t = datetime.fromisoformat(entry.get("target_time", ""))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Hoofdfunctie
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="MOS bias-correcties uit prediction_log.json.")
    parser.add_argument("--mode", choices=["legacy", "rolling"], default="legacy",
                        help="legacy = (regime, uur, maand)-cellen, vlak gemiddelde "
                             "(default; huidige live-gedrag). rolling = (regime, uur)-"
                             "cellen met exponentieel recentheidsgewicht (v3.2).")
    parser.add_argument("--half-life", type=float, default=HALF_LIFE_DAYS,
                        help=f"Halfwaardetijd recentheidsgewicht in dagen "
                             f"(rolling, default {HALF_LIFE_DAYS}).")
    parser.add_argument("--min-neff", type=float, default=MIN_N_EFF,
                        help=f"Minimale effectieve n (som gewichten) voor apply "
                             f"(rolling, default {MIN_N_EFF}).")
    parser.add_argument("--window-days", type=int, default=WINDOW_DAYS,
                        help=f"Rollerend datavenster in dagen (default {WINDOW_DAYS}).")
    args = parser.parse_args()

    print(f"[info] compute_bias.py gestart (mode={args.mode}).", file=sys.stderr)

    log = load_prediction_log(PREDICTION_LOG_FILE)
    now_utc = datetime.now(timezone.utc)
    window_start = now_utc - timedelta(days=args.window_days)

    entries_with_actual = [e for e in log if e.get("actual") is not None]
    print(f"[info] {len(entries_with_actual)} entries met actuals (voor window-filter).",
          file=sys.stderr)

    # Controleer v2.1 beschikbaarheid
    v21_with_actual = [
        e for e in entries_with_actual
        if e.get("model_version", "").startswith("2.")
    ]
    if len(v21_with_actual) < 200:
        print(
            f"[warn] Slechts {len(v21_with_actual)} v2.1-entries met actuals beschikbaar "
            f"(doel: >= 200). Plausibility-laag is nog niet valideerbaar. "
            f"Validatie zinvol na ~4 weken v2.1-data.",
            file=sys.stderr,
        )

    # Window-filter: target_time >= window_start
    entries_in_window: list[tuple[dict, datetime]] = []
    for entry in entries_with_actual:
        t = parse_target(entry)
        if t is not None and t >= window_start:
            entries_in_window.append((entry, t))

    print(f"[info] {len(entries_in_window)} entries binnen het {args.window_days}-dagenvenster.",
          file=sys.stderr)

    if not entries_in_window:
        print("[warn] Geen entries binnen het window — bias_corrections.json wordt aangemaakt "
              "met lege corrections.", file=sys.stderr)

    corrections: dict[str, dict] = {}
    n_apply = 0
    n_skip  = 0

    if args.mode == "legacy":
        # ------------------------------------------------------------------
        # LEGACY: (regime, uur, maand)-cellen, vlak gemiddelde
        # ------------------------------------------------------------------
        cells: dict[str, list[float]] = defaultdict(list)
        for entry, t in entries_in_window:
            error = entry_error(entry, prefer_latest=False)
            if error is None:
                continue
            key = build_cell_key(infer_regime_bucket(entry), t.hour, t.month)
            cells[key].append(error)

        print(f"[info] {len(cells)} unieke cellen gevonden.", file=sys.stderr)

        for key, errors in sorted(cells.items()):
            n    = len(errors)
            bias = round(mean(errors), 2)
            do_apply = (n >= MIN_SAMPLES) and (abs(bias) >= MIN_BIAS_EUR)
            corrections[key] = {"bias": bias, "n": n, "apply": do_apply}
            n_apply += 1 if do_apply else 0
            n_skip  += 0 if do_apply else 1

    else:
        # ------------------------------------------------------------------
        # ROLLING (v3.2): (regime, uur)-cellen, exponentieel recentheidsgewicht
        # w = 0.5 ** (leeftijd_dagen / half_life); bias = Σ(w·e) / Σw;
        # n_eff = Σw. apply als n_eff >= min_neff EN |bias| >= MIN_BIAS_EUR.
        # ------------------------------------------------------------------
        acc: dict[str, dict] = defaultdict(lambda: {"we": 0.0, "w": 0.0, "n": 0})
        for entry, t in entries_in_window:
            error = entry_error(entry, prefer_latest=True)
            if error is None:
                continue
            age_days = max(0.0, (now_utc - t).total_seconds() / 86400.0)
            w = 0.5 ** (age_days / args.half_life)
            key = build_rolling_key(infer_regime_bucket(entry), t.hour)
            acc[key]["we"] += w * error
            acc[key]["w"]  += w
            acc[key]["n"]  += 1

        print(f"[info] {len(acc)} unieke rolling-cellen gevonden.", file=sys.stderr)

        for key, a in sorted(acc.items()):
            if a["w"] <= 0.0:
                continue
            bias  = round(a["we"] / a["w"], 2)
            n_eff = round(a["w"], 1)
            do_apply = (n_eff >= args.min_neff) and (abs(bias) >= MIN_BIAS_EUR)
            corrections[key] = {"bias": bias, "n": a["n"], "n_eff": n_eff, "apply": do_apply}
            n_apply += 1 if do_apply else 0
            n_skip  += 0 if do_apply else 1

    print(f"[info] {n_apply} cellen met apply=true, {n_skip} met apply=false.",
          file=sys.stderr)

    # Log de sterkste biases voor inspectie
    strong = [(k, v["bias"], v["n"]) for k, v in corrections.items() if v["apply"]]
    strong.sort(key=lambda x: abs(x[1]), reverse=True)
    if strong:
        print("[info] Top-5 sterkste correcties (cel, bias, n):", file=sys.stderr)
        for cel, b, n in strong[:5]:
            print(f"  {cel}: {b:+.1f} EUR/MWh (n={n})", file=sys.stderr)

    # Bouw output. 'format' laat run_forecast.py zien welk celtype dit is;
    # legacy-bestanden (zonder 'format') blijven gewoon werken.
    output = {
        "generated_at":  now_utc.isoformat(),
        "format":        "rolling_v1" if args.mode == "rolling" else "legacy",
        "window_days":   args.window_days,
        "min_bias_eur":  MIN_BIAS_EUR,
        "n_entries_used": len(entries_in_window),
        "corrections":   corrections,
    }
    if args.mode == "rolling":
        output["half_life_days"] = args.half_life
        output["min_n_eff"]      = args.min_neff
    else:
        output["min_samples"] = MIN_SAMPLES

    # Schrijf via write_bytes (OneDrive null-byte fix)
    BIAS_CORRECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(output, indent=2, ensure_ascii=False)
    BIAS_CORRECTIONS_FILE.write_bytes(content.encode("utf-8"))
    byte_count = BIAS_CORRECTIONS_FILE.stat().st_size
    print(f"[ok] bias_corrections.json geschreven ({byte_count} bytes, "
          f"{len(corrections)} cellen).", file=sys.stderr)

    if byte_count < 200:
        print("[warn] bias_corrections.json is kleiner dan 200 bytes — controleer output.",
              file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
