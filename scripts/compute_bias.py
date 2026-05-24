"""
compute_bias.py — Berekent MOS bias-correcties per (uur, regime, maand)-cel.

Analyseert prediction_log.json over een rollerend 60-dagenvenster en schrijft
bias_corrections.json voor gebruik door run_forecast.py.

Draait wekelijks via .github/workflows/compute-bias.yml.
Kan ook handmatig worden uitgevoerd: python scripts/compute_bias.py

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
"""

from __future__ import annotations

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
MIN_SAMPLES  = 7       # Minimaal aantal datapunten voor een cel om 'apply' te zetten
MIN_BIAS_EUR = 5.0     # Minimale absolute bias (EUR/MWh) om correctie te activeren

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
    """Bouw de cel-sleutel: '{regime}_h{uur:02d}_m{maand:02d}'."""
    return f"{regime}_h{hour:02d}_m{month:02d}"


# ---------------------------------------------------------------------------
# Hoofdfunctie
# ---------------------------------------------------------------------------

def main() -> int:
    print("[info] compute_bias.py gestart.", file=sys.stderr)

    log = load_prediction_log(PREDICTION_LOG_FILE)

    # Filter: alleen entries met actuals die binnen het window vallen
    window_start = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    window_start_str = window_start.isoformat()

    entries_with_actual = [
        e for e in log
        if e.get("actual") is not None
    ]
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
    entries_in_window: list[dict] = []
    for entry in entries_with_actual:
        t_str = entry.get("target_time", "")
        try:
            t = datetime.fromisoformat(t_str)
            # Zet naar UTC-aware voor vergelijking
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if t >= window_start:
                entries_in_window.append(entry)
        except (ValueError, TypeError):
            continue

    print(f"[info] {len(entries_in_window)} entries binnen het {WINDOW_DAYS}-dagenvenster.",
          file=sys.stderr)

    if not entries_in_window:
        print("[warn] Geen entries binnen het window — bias_corrections.json wordt aangemaakt "
              "met lege corrections.", file=sys.stderr)

    # Groepeer op (regime_bucket, hour_of_day, month_bucket)
    # Analyse 13 mei: verwacht de grootste biases in oversupply x uur 10-16 x mei/april.
    cells: dict[str, list[float]] = defaultdict(list)

    for entry in entries_in_window:
        t_str = entry.get("target_time", "")
        try:
            t = datetime.fromisoformat(t_str)
            hour  = t.hour
            month = t.month
        except (ValueError, TypeError):
            continue

        actual    = entry.get("actual")
        predicted = entry.get("predicted")
        if actual is None or predicted is None:
            continue

        try:
            error = float(actual) - float(predicted)   # positief = model te laag
        except (ValueError, TypeError):
            continue

        regime = infer_regime_bucket(entry)
        key    = build_cell_key(regime, hour, month)
        cells[key].append(error)

    print(f"[info] {len(cells)} unieke cellen gevonden.", file=sys.stderr)

    # Bereken bias per cel
    corrections: dict[str, dict] = {}
    n_apply = 0
    n_skip  = 0

    for key, errors in sorted(cells.items()):
        n    = len(errors)
        bias = round(mean(errors), 2)
        do_apply = (n >= MIN_SAMPLES) and (abs(bias) >= MIN_BIAS_EUR)
        corrections[key] = {
            "bias":  bias,
            "n":     n,
            "apply": do_apply,
        }
        if do_apply:
            n_apply += 1
        else:
            n_skip += 1

    print(f"[info] {n_apply} cellen met apply=true, {n_skip} met apply=false.",
          file=sys.stderr)

    # Log de sterkste biases voor inspectie
    strong = [(k, v["bias"], v["n"]) for k, v in corrections.items() if v["apply"]]
    strong.sort(key=lambda x: abs(x[1]), reverse=True)
    if strong:
        print("[info] Top-5 sterkste correcties (cel, bias, n):", file=sys.stderr)
        for cel, b, n in strong[:5]:
            print(f"  {cel}: {b:+.1f} EUR/MWh (n={n})", file=sys.stderr)

    # Bouw output
    output = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "window_days":   WINDOW_DAYS,
        "min_samples":   MIN_SAMPLES,
        "min_bias_eur":  MIN_BIAS_EUR,
        "n_entries_used": len(entries_in_window),
        "corrections":   corrections,
    }

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
