"""
forecast.py — 6-puntensysteem voor Nederlandse day-ahead prijzen.

Implementeert het model uit 01-documenten/methodologie-voorspellingsmodel.md.
Wordt gebruikt door zowel de live forecast als door backtest.py.

Alle bedragen zijn in EUR/MWh tenzij anders vermeld.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
import math


# Officiële NL vrije dagen 2025-2027.
# Let op: Bevrijdingsdag (5 mei) is alleen vrij in lustrum-jaren (2020, 2025, 2030).
# 2026 en 2027 zijn GEEN lustrum, dus niet opgenomen.
# 2027: Pasen = 28 maart (berekend via Gregoriaanse methode).
NL_FEESTDAGEN = {
    # 2025
    "2025-01-01", "2025-04-18", "2025-04-20", "2025-04-21", "2025-04-27",
    "2025-05-05",  # Bevrijdingsdag 2025 — 80e lustrum ✓
    "2025-05-29", "2025-06-08", "2025-06-09",
    "2025-12-25", "2025-12-26",
    # 2026 — Pasen = 5 april
    "2026-01-01", "2026-04-03", "2026-04-05", "2026-04-06", "2026-04-27",
    "2026-05-14", "2026-05-24", "2026-05-25",  # Hemelvaart, Pinksteren
    "2026-12-25", "2026-12-26",
    # 2027 — Pasen = 28 maart
    "2027-01-01", "2027-03-26", "2027-03-28", "2027-03-29", "2027-04-27",
    "2027-05-06", "2027-05-16", "2027-05-17",  # Hemelvaart, Pinksteren
    "2027-12-25", "2027-12-26",
}

# Dagen waarop DE+BE (en vaak FR) vrij zijn maar NL NIET.
# v1.7: gebruikt om baseline-besmetting te voorkomen — als een historische
# werkdag toevallig een EU-feestdag was (bijv. 1 mei), zijn de prijzen van
# die dag structureel afwijkend (buurland-overschot drukt de prijs) en
# mogen ze niet meewegen in de baseline van een gewone werkdag.
CROSSBORDER_FEESTDAGEN = {
    "2026-05-01",  # Dag van de Arbeid (DE+BE+FR vrij, NL open)
    "2027-05-01",  # Dag van de Arbeid
}

# Gewicht per punt (zie methodologie sectie 3.2).
# v1.0-1.2: 0.04. v1.3 (2026-04-29): gehalveerd naar 0.02 nadat backtest v1
# een systematische over-voorspelling toonde (bias +8 EUR/MWh op 1d, oplopend
# naar +19 op 7d) en MAE iets boven de naïeve baseline lag.
# v1.10 (2026-05-04): verder verlaagd naar 0.015 na backtest op echte ENTSO-E data
# (60 dagen, mrt-mei 2026): bias +7.5 EUR/MWh op 1d. Puntenverdeling structureel
# positief (gem +1.03), wat samen met POINT_WEIGHT 0.02 de opwaartse bias verklaart.
# Zie 01-documenten/backtest-resultaat-v1.md.
# v2.0 (2026-05-06): verdubbeld naar 0.030 na analyse prediction_log 6 mei.
# MAE 32.4 EUR/MWh; dynamische range bij 0.015 onvoldoende om grote
# baseline-afwijkingen te overbruggen (max ±21% bij 14 punten → nu ±42%).
# Verdere kalibratie gepland op 18 mei zodra meer data beschikbaar is.
POINT_WEIGHT = 0.030

# Welke factoren tellen mee in de som. Default: alle 7. Via deze set is het
# mogelijk individuele factoren uit te schakelen voor experimenten zonder de
# code zelf te wijzigen.
#
# Backtest v4 (v1.5) testte een simpel model met {"zon", "wind"} alleen — dat
# liet richting-hit zakken van 51% naar 42% op 1d (onder random). Conclusie:
# de gecombineerde factoren capteren wél subtiele richtingsignalen die individueel
# weinig lijken bij te dragen, en het volledige model blijft de productiekeuze.
#
# v1.8: "vorige_dag" toegevoegd — zie factor_vorige_dag() hieronder.
# v1.10: "nonlinear" toegevoegd — zie nonlinear_correction() hieronder.
ENABLED_FACTORS = {"zon", "wind", "temperatuur", "gas", "dagtype", "uurpatroon", "vorige_dag", "nonlinear"}

# v1.6: zondag-boost voor weersfactoren.
# Backtest v3 toonde een hardnekkige bias van +27 EUR/MWh op zondag-uren die niet
# door de v1.4 weekend-baseline-fix werd opgelost. Op zondag is de basale stroomvraag
# lager (geen industrie, weinig commercieel) dus dezelfde MWh aan zon- en
# windproductie drukt de prijs sterker. Een zonnige+winderige zondag laat prijzen
# diep zakken; een bewolkte+windstille zondag piekt de prijs juist. v1.6 versterkt
# alleen op zondag de zon- en wind-factoren met deze multiplier; andere dagen
# ongewijzigd. Andere factoren (temperatuur, gas, dagtype, uurpatroon) blijven 1x.
ZONDAG_BOOST = 2

# ---- Marktregimes (v1.7 sectie 5) ----
# Het model detecteert eerst het regime, dat bepaalt welke aanvullende
# correcties van toepassing zijn (o.a. niet-lineaire oversupply-factor).
REGIME_NORMAL     = "normaal"       # Normaal Evenwicht
REGIME_OVERSUPPLY = "oversupply"    # Hernieuwbare Oversupply
REGIME_SCARCITY   = "schaarste"     # Schaarste / Dunkelflaute (winter)
REGIME_SCARCITY_SUMMER = "zomerschaarste"  # v3.2 (#71): windstille hitte, avondramp
REGIME_TRANSITION = "transitie"     # Transitie / Volatiliteit (toekomstig)


@dataclass
class FactorScore:
    """Score van één factor met uitleg voor logging/UI."""
    name: str
    points: int
    reason: str


@dataclass
class Forecast:
    """Resultaat van één uurvoorspelling."""
    target_iso: str
    baseline: float            # EUR/MWh
    factors: list[FactorScore]
    total_points: int
    predicted: float           # EUR/MWh
    uncertainty_pct: float     # 0..1
    days_ahead: int
    regime: str = REGIME_NORMAL          # v1.7: gedetecteerd marktregime
    extreme_event_prob: float = 0.0      # v1.7: kans op negatieve prijs (0..1)
    band_half: Optional[float] = None    # v4: halve bandbreedte in EUR/MWh

    # v4: de band wordt als absolute halfbreedte bijgehouden zodra die bekend is.
    # De oude vorm (predicted x (1 +/- pct)) klapt om bij een negatieve voorspelling:
    # bij predicted = -20 en pct = 1,2 kwam de ondergrens boven de bovengrens uit.
    @property
    def lower(self) -> float:
        if self.band_half is not None:
            return self.predicted - self.band_half
        return self.predicted * (1 - self.uncertainty_pct)

    @property
    def upper(self) -> float:
        if self.band_half is not None:
            return self.predicted + self.band_half
        return self.predicted * (1 + self.uncertainty_pct)


# ---- Hulpfuncties ----

def is_feestdag(dt: datetime) -> bool:
    return dt.strftime("%Y-%m-%d") in NL_FEESTDAGEN

def is_crossborder_feestdag(dt: datetime) -> bool:
    return dt.strftime("%Y-%m-%d") in CROSSBORDER_FEESTDAGEN

def dagtype(dt: datetime) -> str:
    """werkdag | weekend | feestdag — voor baseline-grouping."""
    if is_feestdag(dt):
        return "feestdag"
    wd = dt.weekday()
    return "weekend" if wd >= 5 else "werkdag"


def is_zomer(dt: datetime) -> bool:
    return 4 <= dt.month <= 9


# ---- Baseline (sectie 3.1) ----

def compute_baseline(
    target_dt: datetime,
    history: list[dict],
    regime: str = "",
) -> Optional[float]:
    """
    Robuuste baseline-prijs voor hetzelfde uur en hetzelfde dagtype.

    Window-keuze:
    - werkdag/feestdag normaal:           laatste 7 dagen  (~5 werkdag-punten).
    - weekend normaal:                    laatste 14 dagen (v1.4, geeft 2 punten/uur).
    - werkdag/feestdag oversupply:        laatste 4 dagen  (v1.11).
    - weekend oversupply:                 laatste 7 dagen  (v1.11).
    - werkdag/feestdag oversupply 9-17h:  laatste 2 dagen  (v1.12, zie hieronder).

    v1.11: korter baseline-window bij REGIME_OVERSUPPLY.
    Backtest (mrt-mei 2026) toonde een oversupply-bias van +19 EUR/MWh ondanks
    sterkere factoren en niet-lineaire correctie. Oorzaak: de 7d-baseline loopt
    1-2 weken achter op een structurele prijsdaling door toenemende zon. Tijdens
    een aanhoudend oversupply-regime (meerdere zonnige dagen op rij) reflecteert
    een 4-daags window de actuele markt veel beter dan 7 dagen. Fallback naar
    7 dagen als <2 datapunten beschikbaar zijn.

    v1.12: solar-piekuren (9-17h) krijgen een nog korter 2-daags window.
    De prijzen tijdens solar-piek veranderen het snelst: een patroon van toenemende
    zonnepanelen in mrt-mei duwt de middagprijzen structureel elke week lager. Een
    2-daags window pikt dit sneller op dan 4 dagen. Fallback naar 7d als er minder
    dan 2 matches zijn.

    v1.7: cross-border feestdagen worden uitgesloten van de werkdag-baseline.
    v1.9: mediaan in plaats van gemiddelde (robuuster tegen uitschieters).

    history: lijst van {time: ISO-string, price: float in EUR/MWh}
    regime:  REGIME_OVERSUPPLY verkort het window; andere waarden gebruiken standaard.
    Return:  baseline in EUR/MWh, of None als er geen data is.
    """
    if BASELINE_MODE == "v4":
        return compute_baseline_v4(target_dt, history)

    target_hour = target_dt.hour
    target_type = dagtype(target_dt)

    # Window-keuze: oversupply gebruikt kortere windows om sneller te adaptieren.
    # v1.12: solar-piekuren (9-17h) krijgen extra-kort 2-daags window.
    if regime == REGIME_OVERSUPPLY:
        if target_type == "weekend":
            window_days, fallback_days = 7, 14
        elif 9 <= target_hour <= 17:
            window_days, fallback_days = 2, 7   # v1.12: solar-piek extra kort
        else:
            window_days, fallback_days = 4, 7   # v1.11: overige oversupply-uren
    else:
        window_days = 14 if target_type == "weekend" else 7
        fallback_days = 14

    cutoff_start = target_dt - timedelta(days=window_days)
    cutoff_end = target_dt

    def _collect(from_dt):
        matches = []
        for entry in history:
            t = datetime.fromisoformat(entry["time"])
            if t < from_dt or t >= cutoff_end:
                continue
            if t.hour != target_hour:
                continue
            if dagtype(t) != target_type:
                continue
            # v1.7: werkdag-baseline mag geen cross-border feestdagen bevatten.
            if target_type == "werkdag" and is_crossborder_feestdag(t):
                continue
            matches.append((t, entry["price"]))
        return matches

    matches = _collect(cutoff_start)

    # Fallback: te weinig datapunten — verleng window
    if len(matches) < 2 and window_days < fallback_days:
        matches = _collect(target_dt - timedelta(days=fallback_days))

    # Fallback feestdag zonder recente feestdag-history (v2.2).
    # Feestdagen zoals Hemelvaart, Pinksteren en Koningsdag liggen ver uit
    # elkaar (soms >5 weken). Als het 14-dagenvenster geen enkel feestdaguur
    # bevat, valt de baseline terug op weekendprijzen: economisch vergelijkbaar
    # (lage industrievraag, vergelijkbaar consumptiepatroon) en betrouwbaarder
    # dan helemaal niets. factor_dagtype geeft al -2 punten, net als zondag,
    # dus de predictie corrigeert daarna nog voor het feestdagkarakter.
    if not matches and target_type == "feestdag":
        feestdag_fallback_days = 14
        fallback_cutoff = target_dt - timedelta(days=feestdag_fallback_days)
        for entry in history:
            t = datetime.fromisoformat(entry["time"])
            if t < fallback_cutoff or t >= cutoff_end:
                continue
            if t.hour != target_hour:
                continue
            if dagtype(t) != "weekend":
                continue
            matches.append((t, entry["price"]))

    if not matches:
        return None

    med = _median([p for _, p in matches])

    # v3.3 (optie 1): niveauverschuiving-detectie. Default UIT.
    if ENABLE_LEVEL_SHIFT:
        shifted = detect_level_shift(matches, history)
        if shifted is not None:
            w = LEVEL_SHIFT_WEIGHT
            return (1.0 - w) * med + w * shifted

    return med


# ================= v4: niveauschatter (backlog #75) =================
# WAAROM. De baseline was de mediaan van hetzelfde uur en hetzelfde dagtype over
# 7 dagen (14 in het weekend). Dat zijn ~5 datapunten, en op horizon 5-7 vallen
# de meeste daarvan buiten de bekende historie, waardoor het venster stilletjes
# terugvalt op nog minder punten. Een backtest over 2021-2026 (alle uren, alle
# horizonten) laat zien dat een langer venster op de werkdag/weekend-groep
# stelselmatig nauwkeuriger is: 28 dagen scheelt 8-10% MAE, in de zomer van 2026
# zelfs 15%. De korte mediaan blijft voor een kwart meewegen zodat een verse
# dagtype-eigenaardigheid niet helemaal verdwijnt, en een gedempte trendfactor
# (gemiddelde van 7 dagen gedeeld door dat van 28 dagen, tot de macht 0,25)
# vangt op dat een lang venster in een stijgende of dalende markt achterloopt.
#
# Deze route is bewust anders dan de niveauverschuiving-detectie van v3.3: die
# probeerde één verse waarneming te laten winnen van de mediaan en kon een
# eendaagse uitschieter niet van een echte verschuiving onderscheiden. Hier
# verandert niet de gevoeligheid voor één punt, maar de steekproefgrootte.

BASELINE_MODE = "legacy"      # "legacy" | "v4"  (run_forecast zet dit)
V4_SHORT_WEIGHT = 0.25        # gewicht van de korte mediaan (het oude gedrag)
V4_LONG_DAYS = 28             # lang venster, werkdag/weekend-groep
V4_TREND_POWER = 0.25         # demping van de trendfactor
V4_TREND_SHORT = 7
V4_TREND_LONG = 28
V4_TREND_CLIP = (0.5, 2.0)    # trendfactor nooit verder dan halvering/verdubbeling


def _v4_parts(target_dt: datetime, history: list[dict]):
    """Korte mediaan, lange mediaan en trendfactor uit de bekende historie."""
    if not history:
        return None, None, 1.0

    parsed = [(datetime.fromisoformat(e["time"]), e["price"]) for e in history]
    # Anker = het laatste bekende uur VOOR het doel-uur. Door hier al op het
    # doel-uur af te kappen kan een aanroeper die per ongeluk latere prijzen
    # meestuurt de vensters niet verschuiven: prijzen op of na het doel-uur
    # veranderen de uitkomst niet (zie self-test onderaan).
    earlier = [t for t, _ in parsed if t < target_dt]
    if not earlier:
        return None, None, 1.0
    end = max(earlier) + timedelta(hours=1)
    target_hour = target_dt.hour
    ttype = dagtype(target_dt)
    t_weekendish = ttype in ("weekend", "feestdag")

    short_days = 14 if t_weekendish else 7
    lo_short = end - timedelta(days=short_days)
    lo_long = end - timedelta(days=V4_LONG_DAYS)

    short_vals: list[float] = []
    long_vals: list[float] = []
    for t, price in parsed:
        if t >= end or t.hour != target_hour:
            continue
        if ttype == "werkdag" and is_crossborder_feestdag(t):
            continue
        if t >= lo_short and dagtype(t) == ttype:
            short_vals.append(price)
        if t >= lo_long and (dagtype(t) in ("weekend", "feestdag")) == t_weekendish:
            long_vals.append(price)

    def _daymean(days_back: int):
        lo = end - timedelta(days=days_back)
        vals = [p for t, p in parsed if lo <= t < end]
        return sum(vals) / len(vals) if vals else None

    recent, longer = _daymean(V4_TREND_SHORT), _daymean(V4_TREND_LONG)
    ratio = 1.0
    if recent is not None and longer is not None and abs(longer) > 5:
        ratio = min(V4_TREND_CLIP[1], max(V4_TREND_CLIP[0], recent / longer))

    ms = _median(short_vals) if short_vals else None
    ml = _median(long_vals) if long_vals else None
    return ms, ml, ratio


def compute_baseline_v4(target_dt: datetime, history: list[dict]) -> Optional[float]:
    """Niveauschatter v4: gewogen korte + lange mediaan, met gedempte trendfactor."""
    ms, ml, ratio = _v4_parts(target_dt, history)
    if ms is None and ml is None:
        return None
    if ms is None:
        base = ml
    elif ml is None:
        base = ms
    else:
        base = V4_SHORT_WEIGHT * ms + (1.0 - V4_SHORT_WEIGHT) * ml
    return base * (ratio ** V4_TREND_POWER)


# ---- v4: bodem onder de niet-lineaire oversupply-correctie ----
# De correctie is kwadratisch en had geen ondergrens. Bij een uurlijkse zonratio
# van 3 of hoger (komt voor rond zonsopgang en na zware bewolking) levert dat
# tientallen minpunten op, en met POINT_WEIGHT 0,03 kantelt de voorspelling dan
# door nul heen. In de backtest 2021-2026 is dit veruit de grootste bron van
# fouten in het oversupply-regime (MAE 136 tegen 39 voor de kale baseline).
NONLINEAR_FLOOR: Optional[float] = None   # bv. -3.0; None = ongewijzigd gedrag


# ---- v4: bandbreedte ----
# De relatieve band (10% + 2%/dag + 1%/punt) dekte 36-53% van de werkelijke
# prijzen, terwijl bezoekers hem als "hier ligt de prijs" lezen. Een band van
# BAND_ABS + BAND_REL x |voorspelling| dekt op dezelfde data 80%.
UNCERTAINTY_MODE = "legacy"   # "legacy" | "v4"
BAND_ABS = 17.0               # EUR/MWh  (80%-dekking gemeten op de replay mei-aug 2026)
BAND_REL = 0.25


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


# ---- Niveauverschuiving-detectie (v3.3, optie 1) ----
#
# PROBLEEM. De baseline is de mediaan van ~5 werkdagen op hetzelfde uur. Die
# mediaan is per constructie ongevoelig voor de nieuwste dag: één nieuw punt
# schuift de mediaan van positie 3 naar positie 3. Bij een echte
# niveauverschuiving (markt springt van ~5 naar ~150 EUR/MWh) blijft de
# baseline dus wekenlang op het oude niveau hangen.
#
# Voorbeeld 18 aug 2026, uur 14 (werkdagen 11/12/13/14/17 aug):
#   [-1.94, -4.53, 5.00, 24.00, 141.27] -> mediaan 5.00
# Terwijl 17 aug (de enige dag die het nieuwe niveau kent) op 141.27 zat.
# De factoren kunnen dat niet repareren: die zijn multiplicatief (+3% per punt),
# dus +10 punten op 5.00 levert 6.50 op terwijl het gat 135 EUR/MWh is.
#
# AANPAK. Detecteer of de nieuwste waarneming een niveausprong is in plaats van
# ruis, en schuif de baseline dan (deels) naar die waarneming.
#
# Drie tests moeten alledrie slagen, anders gebeurt er niets:
#   1. VERSHEID  — de nieuwste match moet echt de verste informatie zijn, niet
#      een oude uitschieter ergens in het venster. Max LEVEL_SHIFT_MAX_AGE_DAYS
#      oud t.o.v. het einde van de bekende history. (3.5 dagen zodat een
#      vrijdag-sprong nog meetelt voor een maandag-target.)
#   2. RELATIEF  — hi / max(lo, FLOOR) >= LEVEL_SHIFT_RATIO, met hi/lo = de
#      hoogste/laagste van {nieuwste, mediaan-van-de-rest}. Deze vorm is
#      SYMMETRISCH: een sprong 1.5 -> 141 en een val 100 -> 5 scoren allebei.
#      Een simpele (nieuw - mediaan)/mediaan zou dat niet doen, want een val is
#      begrensd door nul en haalt nooit factor 3.
#      De FLOOR voorkomt deling door een mediaan rond nul (zonnedagen!).
#   3. ABSOLUUT  — |hi - lo| >= LEVEL_SHIFT_MIN_GAP EUR/MWh. Zonder deze test
#      vuurt de relatieve test op ruis: 0.5 -> 2.0 is ook "factor 4".
#
# RISICO dat de A/B moet meten: dit is precies de robuustheid die de mediaan
# bood. Was de sprong een eendaagse uitschieter (storing, veiling-incident) en
# valt de markt terug, dan schiet het model nu de andere kant op. Daarom een
# gewicht tussen 0 en 1 in plaats van hard vervangen, en daarom draait dit
# achter een vlag met een gewichtsknop voor de backtest.
#
# INTERACTIE. compute_baseline wordt ook aangeroepen voor prior_baseline
# (factor vorige_dag). Met de verschuiving aan wordt prior_ratio ~1.0 in plaats
# van 32x, dus factor_vorige_dag stopt met dubbeltellen. Dat is gewenst: het
# signaal hoort nu in het niveau te zitten, niet in een gecapte factor.

ENABLE_LEVEL_SHIFT       = False  # vlag (default UIT; backtest zet hem aan)
LEVEL_SHIFT_WEIGHT       = 1.0    # 0..1 — hoe ver de baseline naar de sprong schuift
LEVEL_SHIFT_RATIO        = 3.0    # relatieve drempel (hi / max(lo, FLOOR))
LEVEL_SHIFT_MIN_GAP      = 40.0   # EUR/MWh — absolute drempel
LEVEL_SHIFT_FLOOR        = 5.0    # EUR/MWh — vloer onder de noemer
LEVEL_SHIFT_MAX_AGE_DAYS = 3.5    # versheidseis t.o.v. einde history
LEVEL_SHIFT_MIN_MATCHES  = 3      # minder punten -> mediaan is toch al zwak


def detect_level_shift(
    matches: list[tuple],
    history: list[dict],
) -> Optional[float]:
    """
    Bepaal of de nieuwste match een niveauverschuiving is.

    matches: lijst van (datetime, prijs) voor hetzelfde uur en dagtype.
    history: volledige prijsgeschiedenis (voor de versheidstest).

    Return: de prijs waar de baseline naartoe mag schuiven, of None.
    """
    if len(matches) < LEVEL_SHIFT_MIN_MATCHES:
        return None

    ordered = sorted(matches, key=lambda m: m[0])
    newest_dt, newest = ordered[-1]
    rest = [p for _, p in ordered[:-1]]
    if not rest:
        return None

    # 1. Versheid: is dit echt de laatste informatie die we hebben?
    if history:
        history_end = max(datetime.fromisoformat(e["time"]) for e in history)
        age_days = (history_end - newest_dt).total_seconds() / 86400.0
        if age_days > LEVEL_SHIFT_MAX_AGE_DAYS:
            return None

    med_rest = _median(rest)
    hi, lo = max(newest, med_rest), min(newest, med_rest)

    # Twee negatieve/nul niveaus: geen zinnige ratio, en de absolute test
    # zou hier toch al zelden vuren.
    if hi <= 0:
        return None

    # 2. Relatief (symmetrisch) en 3. absoluut.
    if hi / max(lo, LEVEL_SHIFT_FLOOR) < LEVEL_SHIFT_RATIO:
        return None
    if (hi - lo) < LEVEL_SHIFT_MIN_GAP:
        return None

    return newest


# ---- Factor 1: Zonproductie ----

def factor_zon(shortwave_ratio: float) -> FactorScore:
    """
    shortwave_ratio: voorspelde dagelijkse straling / seizoengemiddelde.

    v1.10: extra trap voor solar_ratio > 2.0 toegevoegd. Backtest (mrt-mei 2026)
    toonde dat de max -3 bij ratio > 1.5 grofweg gelijk bleef voor extreem zonnige
    dagen (ratio 1.6-2.5+), terwijl de werkelijke prijsdaling daar veel sterker was.
    """
    if shortwave_ratio < 0.50:
        pts, reason = +3, f"bewolkt ({shortwave_ratio*100:.0f}% van seizoen)"
    elif shortwave_ratio < 0.80:
        pts, reason = +1, f"iets minder zon ({shortwave_ratio*100:.0f}%)"
    elif shortwave_ratio <= 1.20:
        pts, reason = 0, f"normaal ({shortwave_ratio*100:.0f}%)"
    elif shortwave_ratio <= 1.50:
        pts, reason = -1, f"zonnig ({shortwave_ratio*100:.0f}%)"
    elif shortwave_ratio <= 2.00:
        pts, reason = -3, f"heel zonnig ({shortwave_ratio*100:.0f}%)"
    else:
        pts, reason = -5, f"extreem zonnig ({shortwave_ratio*100:.0f}%)"
    return FactorScore("zon", pts, reason)


# ---- Factor 2: Windproductie ----

def factor_wind(wind_ms: float) -> FactorScore:
    """wind_ms: gemiddelde windsnelheid op 100m hoogte (m/s)."""
    if wind_ms < 4:
        pts, reason = +3, f"windstil ({wind_ms:.1f} m/s)"
    elif wind_ms < 8:
        pts, reason = +1, f"zwakke wind ({wind_ms:.1f} m/s)"
    elif wind_ms < 12:
        pts, reason = 0, f"normaal ({wind_ms:.1f} m/s)"
    elif wind_ms < 16:
        pts, reason = -2, f"stevige wind ({wind_ms:.1f} m/s)"
    else:
        pts, reason = -3, f"storm ({wind_ms:.1f} m/s)"
    return FactorScore("wind", pts, reason)


# ---- Factor 3: Temperatuur ----
# v1.3: drempels herzien. Voorheen was alleen koud/vorst positief; mild en
# warm gaven 0 of +1 zodat de factor structureel niet-negatief was. Dat droeg
# bij aan de bias in backtest v1. Nu: lekker weer (18-26 °C) geeft -1 (lagere
# ruimtevraag, mensen buiten, zon op piek), warm (>26 °C) is 0 (lichte
# airco-koeling balanceert overige effecten).

def factor_temperatuur(temp_c: float) -> FactorScore:
    if temp_c < 0:
        pts, reason = +2, f"vorst ({temp_c:.1f}°C)"
    elif temp_c < 10:
        pts, reason = +1, f"koud ({temp_c:.1f}°C)"
    elif temp_c < 18:
        pts, reason = 0, f"mild ({temp_c:.1f}°C)"
    elif temp_c <= 26:
        pts, reason = -1, f"lekker ({temp_c:.1f}°C)"
    else:
        pts, reason = 0, f"warm ({temp_c:.1f}°C)"
    return FactorScore("temperatuur", pts, reason)


# ---- Factor 4: Gasprijs (TTF) ----

def factor_gas(ttf_ratio: float) -> FactorScore:
    """ttf_ratio: huidige TTF / 30-dagen gemiddelde TTF."""
    if ttf_ratio < 0.70:
        pts, reason = -2, f"gas goedkoop ({ttf_ratio*100:.0f}% van 30d gem.)"
    elif ttf_ratio < 0.90:
        pts, reason = -1, f"gas iets goedkoper ({ttf_ratio*100:.0f}%)"
    elif ttf_ratio <= 1.10:
        pts, reason = 0, f"normaal ({ttf_ratio*100:.0f}%)"
    elif ttf_ratio <= 1.30:
        pts, reason = +1, f"gas iets duurder ({ttf_ratio*100:.0f}%)"
    else:
        pts, reason = +2, f"gas duur ({ttf_ratio*100:.0f}%)"
    return FactorScore("gas", pts, reason)


# ---- Factor 5: Type dag ----
# v1.3: werkdag-bonus van +1 naar 0. De baseline filtert al op dagtype, dus
# een expliciete +1 voor werkdagen telde dubbel — een belangrijke oorzaak
# van de bias in backtest v1. Weekend en feestdag houden hun negatieve
# gewicht omdat de baseline-window voor zaterdag/zondag/feestdag mager is
# (1-2 datapunten in 7 dagen) en daar de factor nog corrigerende waarde heeft.
#
# v1.7: ook cross-border feestdagen (EU-feestdag, NL open) krijgen -2 punten.
# Op die dagen is de effectieve marktprijs structureel lager door verminderde
# buurland-vraag én exportoverschot dat op het NL net drukt.

def factor_dagtype(dt: datetime) -> FactorScore:
    if is_feestdag(dt):
        return FactorScore("dagtype", -2, "NL feestdag")
    if is_crossborder_feestdag(dt):
        return FactorScore("dagtype", -2, "EU-feestdag (NL open, buurlanden vrij)")
    wd = dt.weekday()
    if wd == 6:
        return FactorScore("dagtype", -2, "zondag")
    if wd == 5:
        return FactorScore("dagtype", -1, "zaterdag")
    return FactorScore("dagtype", 0, "werkdag")


# ---- Factor 6: Uurpatroon ----
# v1.3: ochtendspits winter van +2 naar +1. De NL-markt heeft in 2026 een
# minder scherpe ochtendpiek dan vroeger — warmtepompen draaien al de hele
# nacht door, EV's laden 's nachts, en zonsopgang verlicht de ochtend al
# vroeg in de zomer. De avondspits 17-20 uur blijft wel scherp.
#
# v1.10: zomerpatroon 15-18h herzien. Backtest (mrt-mei 2026) toonde een
# bias van +23 tot +33 EUR/MWh op 14:00-18:00 uur. Zonne-energie in het
# voorjaar/zomer drukt de prijs ook in de namiddag en vroege avond —
# de traditionele "avondspits" schuift in de zomer op naar 19-20h.
# Nieuw zomer: 15-16h: 0→-1 (zonneplateau),  17-18h: +1→0 (geen avondspits meer).

def factor_uurpatroon(dt: datetime) -> FactorScore:
    h = dt.hour
    zomer = is_zomer(dt)
    season = "zomer" if zomer else "winter"
    if 0 <= h <= 5:
        return FactorScore("uurpatroon", -2, f"{season}, nacht ({h}:00)")
    if 6 <= h <= 8:
        return FactorScore("uurpatroon", +1, f"{season}, ochtendspits ({h}:00)")
    if 9 <= h <= 14:
        pts = -1 if zomer else 0
        return FactorScore("uurpatroon", pts, f"{season}, midden van de dag ({h}:00)")
    if 15 <= h <= 16:
        pts = -1 if zomer else 0
        return FactorScore("uurpatroon", pts, f"{season}, namiddag ({h}:00)")
    if 17 <= h <= 18:
        pts = 0 if zomer else +2
        return FactorScore("uurpatroon", pts, f"{season}, vroege avond ({h}:00)")
    if 19 <= h <= 20:
        pts = +1 if zomer else +2
        return FactorScore("uurpatroon", pts, f"{season}, avondspits ({h}:00)")
    return FactorScore("uurpatroon", -1, f"{season}, late avond ({h}:00)")


# ---- Factor 7: Vorige dag (v1.8) ----
# Dagelijkse day-ahead prijzen vertonen sterke autocorrelatie: een dag met hoge
# prijzen wordt vaak gevolgd door een dag met relatief hoge prijzen (aanhoudend
# weerregime, gasprijsniveau, marktomstandigheden veranderen niet van dag op dag).
#
# Deze factor benut de bekende D+1-prijzen (gepubliceerd ~14:00) als signaal voor
# D+2, het eerste te voorspellen uur. Voor D+3 en verder ontbreken de vorige-dag-
# prijzen en geeft de factor 0 (neutraal).
#
# Werkwijze per te voorspellen uur H op dag D+2:
#   1. Zoek de werkelijke prijs van D+1 op datzelfde uur H  → prior_price
#   2. Bereken de historische baseline voor dat uur op D+1  → prior_baseline
#      (= gemiddelde van dezelfde dag/uur-combinatie in de voorgaande 7-14 dagen,
#       identiek aan hoe compute_baseline() normaal werkt)
#   3. ratio = prior_price / prior_baseline
#   4. Sla de ratio om in ±1 of ±2 punten
#
# Rationale voor de drempelkeuze (zelfde stijl als factor_gas):
#   ratio < 0.70 → de voorgaande dag was structureel goedkoop      → -2
#   ratio < 0.90 → iets goedkoper                                  → -1
#   ratio ≤ 1.10 → normaal                                         →  0
#   ratio ≤ 1.30 → iets duurder                                    → +1
#   ratio > 1.30 → structureel duur                                → +2
#
# Effect op de voorspelling (POINT_WEIGHT = 0.02):
#   max +2 punten → +4% op baseline  (bv. 30 EUR/MWh → 31.20 EUR/MWh)
#   max -2 punten → -4% op baseline
# Dit is bewust conservatief: de autocorrelatie is sterk maar niet volledig,
# en het model mag de historische baseline niet te ver overrulen.

def factor_vorige_dag(prior_ratio: Optional[float]) -> FactorScore:
    """
    prior_ratio: (prijs voorgaande dag uur H) / (baseline voorgaande dag uur H).
    None als de voorgaande-dag-prijs niet beschikbaar is (D+3 en verder).
    """
    if prior_ratio is None:
        return FactorScore("vorige_dag", 0, "niet beschikbaar (>1 dag vooruit)")
    if prior_ratio < 0.70:
        pts, reason = -2, f"vorige dag goedkoop ({prior_ratio:.2f}× baseline)"
    elif prior_ratio < 0.90:
        pts, reason = -1, f"vorige dag iets goedkoper ({prior_ratio:.2f}×)"
    elif prior_ratio <= 1.10:
        pts, reason = 0, f"vorige dag normaal ({prior_ratio:.2f}×)"
    elif prior_ratio <= 1.30:
        pts, reason = +1, f"vorige dag duurder ({prior_ratio:.2f}×)"
    else:
        pts, reason = +2, f"vorige dag duur ({prior_ratio:.2f}×)"
    return FactorScore("vorige_dag", pts, reason)


# ---- Factor 9: Seizoen (v3.0, experimenteel, standaard UIT) ----
# Niveaucorrectie op basis van het prijspatroon in dezelfde kalenderperiode van
# voorgaande jaren. Vult een gat dat de MOS bias-correctie niet kan dichten: MOS
# corrigeert alleen maanden die het live-model al heeft gezien, terwijl deze factor
# uit jaren archief leert voor ELKE maand. seasonal_history wordt aangeleverd door
# de orchestrator (run_forecast/backtest) via load_archive.load_same_period().
#
# Telt alleen mee als "seizoen" in ENABLED_FACTORS staat (default: niet). Zo blijft
# de live-voorspelling ongewijzigd tot een backtest de waarde bevestigt.

def seasonal_baseline(target_dt: datetime, seasonal_history: list[dict]) -> Optional[float]:
    """Mediaan-prijs voor hetzelfde uur en dagtype uit historische seizoensdata.

    seasonal_history: prijzen rond dezelfde kalenderperiode in voorgaande jaren.
    Filtert op gelijk uur + dagtype (werkdag/weekend/feestdag) en neemt de mediaan.
    None als er geen match is.
    """
    target_hour = target_dt.hour
    target_type = dagtype(target_dt)
    matches: list[float] = []
    for entry in seasonal_history:
        try:
            t = datetime.fromisoformat(entry["time"])
        except (ValueError, KeyError, TypeError):
            continue
        if t.hour != target_hour:
            continue
        if dagtype(t) != target_type:
            continue
        matches.append(entry["price"])
    if not matches:
        return None
    srt = sorted(matches)
    n = len(srt)
    mid = n // 2
    return srt[mid] if n % 2 else (srt[mid - 1] + srt[mid]) / 2


def factor_seizoen(seasonal_ratio: Optional[float]) -> FactorScore:
    """Niveaucorrectie uit het seizoenspatroon van voorgaande jaren.

    seasonal_ratio = (seizoens-mediaan voor dit uur/dagtype) / (recente baseline).
    > 1 → deze tijd van het jaar is historisch duurder dan het recente venster
    suggereert (duw omhoog); < 1 → goedkoper (duw omlaag). Bewust begrensd op ±2
    punten (±6% bij POINT_WEIGHT 0.03), net als factor_gas en factor_vorige_dag.
    """
    if seasonal_ratio is None:
        return FactorScore("seizoen", 0, "geen seizoensdata")
    if seasonal_ratio < 0.80:
        pts, reason = -2, f"seizoen historisch goedkoper ({seasonal_ratio:.2f}x baseline)"
    elif seasonal_ratio < 0.93:
        pts, reason = -1, f"seizoen iets goedkoper ({seasonal_ratio:.2f}x)"
    elif seasonal_ratio <= 1.07:
        pts, reason = 0, f"seizoen normaal ({seasonal_ratio:.2f}x)"
    elif seasonal_ratio <= 1.25:
        pts, reason = +1, f"seizoen iets duurder ({seasonal_ratio:.2f}x)"
    else:
        pts, reason = +2, f"seizoen historisch duurder ({seasonal_ratio:.2f}x)"
    return FactorScore("seizoen", pts, reason)


# ---- Regime detectie (v1.7 sectie 5) ----

def detect_regime(solar_ratio: float, wind_ms: float, temp_c: float, dt: datetime) -> str:
    """
    Detecteer marktregime voor een uur op basis van zon, wind en temperatuur.

    Regime 3 (Schaarste/Dunkelflaute): alle drie drempels gelijktijdig overschreden.
      solar < 60% EN wind < 5 m/s EN temp < 8°C — gasprijs bepaalt de markt.
    Regime 2 (Oversupply): sterke hernieuwbare productie + lage vraag.
      - Zon-trigger: solar > 140% EN uur 8-18  (v1.12: daglichturen; v1.13:
        verkleind tot 8-18h omdat uren 19-20h juist HOGE avondprijzen hebben
        door ramp-up vraag na zonsondergang — bias was −15 tot −22 EUR/MWh).
      - Wind-trigger: wind > 14 m/s AND (weekend/feestdag/warm) 24/7  (v1.12:
        drempel verhoogd van 12→14 m/s om fout-positieven te verminderen).
    Regime 1 (Normaal): alles overig.
    Regime 4 (Transitie): vereist Δ-weersverwachting als input — nog niet geïmplementeerd.

    v1.12: backtest (mrt-mei 2026): zon-trigger beperkt tot 7-20h, winddrempel 14 m/s.
    v1.13: zon-trigger verder ingeperkt naar 8-18h. Backtest toonde −15 tot −22 EUR/MWh
    bias op uren 19-20h (hoge avondprijzen, kortere baseline trok voorspelling te laag).
    """
    is_low_demand = dt.weekday() >= 5 or is_feestdag(dt) or temp_c > 10.0

    # Schaarste: lage zon + windstil + koud
    if solar_ratio < 0.60 and wind_ms < 5.0 and temp_c < 8.0:
        return REGIME_SCARCITY

    # v3.2 (#71): zomerse schaarste — windstille hitte tijdens de avondramp.
    # De 8°C-ondergrens hierboven maakt de winter-detectie blind voor hittegolven
    # (backtest juni 2026: pieken tot 578 EUR/MWh op 20-21u, voorspelling 78-122
    # te laag). Trigger: zomermaand + avondramp-uur + windstil + heet. Vóór de
    # oversupply-checks, zodat een zonnige hittedag om 18u niet als oversupply
    # wordt gelabeld terwijl de ramp al begint. Achter een aparte flag (default
    # UIT) omdat een regime-wissel ook labels in log/UI raakt; run_forecast en
    # backtest zetten hem expliciet aan (zelfde uitrolpad als v3.0/v3.1).
    if (ENABLE_SUMMER_SCARCITY_REGIME
            and dt.month in SUMMER_SCARCITY_MONTHS
            and SUMMER_SCARCITY_HOURS[0] <= dt.hour <= SUMMER_SCARCITY_HOURS[1]
            and wind_ms < SUMMER_SCARCITY_WIND_MAX
            and temp_c > SUMMER_SCARCITY_TEMP_MIN):
        return REGIME_SCARCITY_SUMMER

    # Oversupply zon: alleen tijdens daglichturen (zon heeft 's nachts geen effect)
    if solar_ratio > 1.40 and 8 <= dt.hour <= 18 and is_low_demand:  # v1.13: 8-18h
        return REGIME_OVERSUPPLY

    # Oversupply wind: geldt 24/7, maar hogere drempel (14 m/s) om fout-positieven
    # te beperken — wind bij 12-13 m/s verhoogt weliswaar productie maar duwt
    # nacht-prijzen in NL zelden negatief.
    if wind_ms > 14.0 and is_low_demand:
        return REGIME_OVERSUPPLY

    return REGIME_NORMAL


# ---- Factor 8: Niet-lineaire oversupply correctie (v1.7 sectie 8) ----
# De lineaire factoren zon (-3 max) en wind (-3 max) onderschatten extreme events.
# Bij solar_ratio = 2.0 of wind = 20 m/s drukt de markt de prijs exponentieel omlaag.
# Deze correctie is ALLEEN actief in REGIME_OVERSUPPLY; in andere regimes 0.
#
# Formule (kwadratisch):
#   solar_penalty = -(solar_ratio - 1.3)² × 14   [punten] (only if > 1.3)
#   wind_penalty  = -(wind_ms - 16)² × 0.25       [punten] (only if > 16 m/s)
#
# Effect op de voorspelling (POINT_WEIGHT = 0.015):
#   solar_ratio = 1.4 → solar_penalty = -(0.1)² × 14 = -0.14 → 0 pt
#   solar_ratio = 1.5 → solar_penalty = -(0.2)² × 14 = -0.56 → -1 pt → -1.5% baseline
#   solar_ratio = 1.8 → solar_penalty = -(0.5)² × 14 = -3.5  → -4 pt → -6% baseline
#   solar_ratio = 2.0 → solar_penalty = -(0.7)² × 14 = -6.86 → -7 pt → -10% baseline
#   solar_ratio = 2.5 → solar_penalty = -(1.2)² × 14 = -20.2 → -20 pt → -30% baseline
#   wind_ms = 20     → wind_penalty  = -(4)² × 0.25   = -4.0  → -4 pt → -6% baseline
#
# v1.10: multiplier zon 8→14 na backtest die toonde dat oversupply-bias +18.8 EUR/MWh
# was; de eerdere correctie was te klein om extreem zonnige voorjaarsdagen te vangen.
# v1.12: drempel zon 1.5→1.3 zodat de correctie al actief is voor typische oversupply
# (solar_ratio 1.4-1.8). Backtest toonde dat correctie bij drempel 1.5 pas kickte bij
# solar_ratio > 1.5, terwijl de trigger al 1.4 is. Nu actief voor vrijwel alle
# oversupply-uren.

def nonlinear_correction(solar_ratio: float, wind_ms: float, regime: str) -> FactorScore:
    """Niet-lineaire correctie voor extreme oversupply (v1.7 sectie 8, v1.10, v1.12)."""
    if regime != REGIME_OVERSUPPLY:
        return FactorScore("nonlinear", 0, "n.v.t.")

    solar_extra = -(max(0.0, solar_ratio - 1.3) ** 2) * 14.0  # v1.12: drempel 1.5→1.3
    wind_extra  = -(max(0.0, wind_ms - 16.0) ** 2) * 0.25
    total_float = solar_extra + wind_extra
    if NONLINEAR_FLOOR is not None:
        total_float = max(total_float, NONLINEAR_FLOOR)
    pts = round(total_float)

    parts = []
    if solar_extra < -0.05:
        parts.append(f"zon {solar_extra:.1f}p")
    if wind_extra < -0.05:
        parts.append(f"wind {wind_extra:.1f}p")
    reason = "oversupply niet-lineair: " + (", ".join(parts) if parts else "grensgeval")
    return FactorScore("nonlinear", pts, reason)


# ---- Factor 10: Schaarste-amplifier (v3.1, experimenteel, standaard UIT) ----
# Spiegelbeeld van de niet-lineaire oversupply-correctie (factor 8), maar OMHOOG.
# Achtergrond: backtest winter 2024/25 toonde dat het model tijdens Dunkelflaute
# (REGIME_SCARCITY: weinig zon + windstil + koud) de prijspieken structureel met
# ~-43 tot -48 EUR/MWh ONDERschat. De lineaire factoren zon (+3 max) en wind (+3 max)
# vangen de exponentiele opwaartse prijsdruk niet: bij windstil + koud zet gas de
# marginale prijs en schiet die niet-lineair omhoog. De seizoensfactor v3.0 loste dit
# niet op (die trekt het niveau juist omlaag).
#
# Deze correctie is STRIKT beperkt tot REGIME_SCARCITY; in elk ander regime geeft hij
# 0 punten. Daardoor kan het dominante normaal-regime (>89% van de uren) per definitie
# niet verslechteren. De factor staat bovendien standaard NIET in ENABLED_FACTORS, dus
# de live-voorspelling verandert pas nadat een backtest de waarde bevestigt en
# run_forecast hem expliciet aanzet (zelfde uitrolpad als de seizoensfactor v3.0).
#
# Severity (kwadratisch, mirror van factor 8) uit de drie regime-drempels:
#   wind_term  = (5.0 - wind_ms)^2  * K_WIND    [windstil is de bepalende driver]
#   cold_term  = (8.0 - temp_c)^2   * K_COLD    [koude verhoogt verwarmingsvraag]
#   solar_term = (0.60 - solar)^2   * K_SOLAR   [donker, kleinste bijdrage]
# Gas-hefboom: severity *= (1 + max(0, ttf_ratio - 1) * K_GAS) — duur gas versterkt
# de piek omdat gas in Dunkelflaute de marginale prijs zet.
# Plafond SCARCITY_MAX_POINTS voorkomt runaway bij extreme kou.
#
# Voorbeeld diepe Dunkelflaute (solar 0.30, wind 2 m/s, temp -2C, ttf 1.20):
#   wind  = (3.0)^2 * 0.9  = 8.1
#   cold  = (10.0)^2 * 0.04 = 4.0
#   solar = (0.30)^2 * 6.0 = 0.54
#   severity = 12.64; gas_mult = 1 + 0.20*1.0 = 1.20 -> 15.2 -> +15 punten
#   bij baseline ~90 EUR/MWh: +90 * 15 * 0.03 = +40.5 EUR/MWh (dicht bij de ~45 gap)
# Voorbeeld milde schaarste (solar 0.50, wind 4 m/s, temp 6C, ttf 1.00):
#   wind 0.9 + cold 0.16 + solar 0.06 = 1.12; gas_mult 1.0 -> +1 punt (klein, gewenst)
#
# De multipliers zijn tunables; backtest stelt ze bij via SCARCITY_SCALE (globale knop).
SCARCITY_SCALE      = 1.5    # globale schaal (backtest A/B-knop: --scarcity-scale).
                             # v3.1: 1.5 gekozen na archief-backtest winter 24/25 + 23/24:
                             # MAE-minimum in de zware winter (schaarste-MAE 65->59, bias
                             # -52->-24), normaal-regime ongewijzigd, bias nooit positief.
SCARCITY_K_WIND     = 0.9    # windstil:  (5.0 - wind_ms)^2  * K
SCARCITY_K_COLD     = 0.04   # koud:      (8.0 - temp_c)^2   * K
SCARCITY_K_SOLAR    = 6.0    # donker:    (0.60 - solar)^2   * K
SCARCITY_K_GAS      = 1.0    # gas-hefboom: severity *= (1 + max(0, ttf-1) * K)
SCARCITY_MAX_POINTS = 18     # veiligheidsplafond op de bijdrage in punten


def scarcity_correction(
    solar_ratio: float,
    wind_ms: float,
    temp_c: float,
    ttf_ratio: float,
    regime: str,
) -> FactorScore:
    """Niet-lineaire opwaartse correctie voor Dunkelflaute (v3.1). Mirror van factor 8."""
    if regime != REGIME_SCARCITY:
        return FactorScore("scarcity", 0, "n.v.t.")

    wind_term  = (max(0.0, 5.0 - wind_ms) ** 2) * SCARCITY_K_WIND
    cold_term  = (max(0.0, 8.0 - temp_c) ** 2) * SCARCITY_K_COLD
    solar_term = (max(0.0, 0.60 - solar_ratio) ** 2) * SCARCITY_K_SOLAR
    severity   = wind_term + cold_term + solar_term
    gas_mult   = 1.0 + max(0.0, ttf_ratio - 1.0) * SCARCITY_K_GAS
    total_float = severity * gas_mult * SCARCITY_SCALE
    pts = min(round(total_float), SCARCITY_MAX_POINTS)

    parts = []
    if wind_term > 0.05:
        parts.append(f"windstil +{wind_term:.1f}p")
    if cold_term > 0.05:
        parts.append(f"koud +{cold_term:.1f}p")
    if solar_term > 0.05:
        parts.append(f"donker +{solar_term:.1f}p")
    if gas_mult > 1.001:
        parts.append(f"×{gas_mult:.2f} gas")
    reason = "schaarste-amplifier: " + (", ".join(parts) if parts else "grensgeval")
    return FactorScore("scarcity", pts, reason)


# ---- Factor 11: Zomer-schaarste-amplifier (v3.2, #71, experimenteel, standaard UIT) ----
# Zomer-equivalent van de winter-Dunkelflaute-amplifier (factor 10): zelfde ziekte,
# ander seizoen. Achtergrond: de nauwkeurigheids-check over juni 2026 toonde dat het
# model zomerse avondpieken structureel te laag inschat (1 juni piek 321 EUR/MWh,
# 23-24 juni piek 578 EUR/MWh op 20-21u; voorspelling 78-122 EUR/MWh te laag, steeds
# dezelfde kant op). Oorzaak: het winter-schaarste-regime eist temp < 8°C en de
# temperatuurfactor geeft > 26°C bewust 0 punten — er is geen mechanisme dat
# "windstil + heet + avondramp = duur" herkent. De rolling-MOS A/B (5 juli 2026)
# bevestigde dat een fouten-volger dit niet oplost (tekenconsistentie 49%): de piek
# is event-gedreven en moet uit de weer-condities van de doeldag zelf komen.
#
# STRIKT gated op REGIME_SCARCITY_SUMMER (dat zelf achter een flag zit, default UIT);
# in elk ander regime 0 punten -> het normaal-regime kan per definitie niet
# verslechteren. Uitrolpad identiek aan v3.0/v3.1: pas live na backtest-A/B.
#
# Severity (kwadratisch, mirror van factor 10) uit de regime-drempels:
#   wind_term = (WIND_MAX - wind_ms)^2 * K_WIND   [windstil is de bepalende driver]
#   heat_term = (temp_c - TEMP_MIN)^2  * K_HEAT   [hitte verhoogt (airco-)vraag]
# Ramp-gewicht per uur: de piek zit op 20-21u (live MAE 142/121), de schouders
# ervoor en erna wegen minder mee. Gas-hefboom als bij winter: bij weinig wind en
# geen zon zet gas de marginale avondprijs.
#
# Afstelling v2 (5 juli 2026, na Z0/Z1-backtest-diagnose op juni 2026):
#   - TEMP_MIN 23 -> 20: de piekdagen 1 en 23 juni (gat +115 resp. +430 EUR/MWh
#     op 20u) hadden daggemiddelden in de band 18-23 °C en triggerden NIET;
#     de heat_term blijft bij 20-23 °C vrijwel nul, dus dit verruimt vooral
#     de trigger — de kracht blijft uit de windterm komen.
#   - K_WIND 0.9 -> 1.5: bij de wél getriggerde juni-uren was de gemiddelde
#     correctie +11,4 EUR/MWh tegen een gemiddeld gat van +96,6 (0 van 120 uren
#     bereikte het plafond). De windterm was te vlak voor wind 2,5-4 m/s.
#   Z5-run (koele zomer 2025) bewaakt dat de ruimere trigger niet te los staat.
#
# Voorbeeld stevige hittegolf-avond (wind 3 m/s, temp 26C, 20u, ttf 1.0):
#   wind = (2.0)^2 * 1.5 = 6.0;  heat = (6.0)^2 * 0.10 = 3.6
#   severity = 9.6 * ramp 1.0 = 9.6 -> +10 punten -> +30% op de baseline
# Randgeval (wind 3.5, temp 22, 20u): 3.4 + 0.4 = 3.8 -> +4 punten (mild, gewenst)
# Extreem (wind 2, temp 27, 20u): 13.5 + 4.9 = 18.4 -> plafond +18 -> +54%.
# Bewust conservatief: een 578-piek wordt zo niet geraakt, wel gesignaleerd —
# het doel is het risico op tijd zien, niet de piek millimeter-precies voorspellen.
ENABLE_SUMMER_SCARCITY_REGIME = False  # regime-flag (default UIT; zie detect_regime)
SUMMER_SCARCITY_MONTHS   = (5, 6, 7, 8, 9)  # mei t/m september
SUMMER_SCARCITY_HOURS    = (18, 22)  # avondramp (inclusief grenzen)
SUMMER_SCARCITY_WIND_MAX = 5.0       # m/s — zelfde windstil-drempel als winter
SUMMER_SCARCITY_TEMP_MIN = 20.0      # °C daggemiddelde (v2: was 23, zie boven)
SUMMER_SCARCITY_SCALE    = 1.0       # globale schaal (backtest A/B-knop)
SUMMER_K_WIND            = 1.5       # windstil: (WIND_MAX - wind)^2 * K (v2: was 0.9)
SUMMER_K_HEAT            = 0.10      # heet:     (temp - TEMP_MIN)^2 * K
SUMMER_K_GAS             = 1.0       # gas-hefboom: severity *= (1 + max(0, ttf-1) * K)
SUMMER_RAMP_WEIGHT       = {18: 0.5, 19: 0.8, 20: 1.0, 21: 1.0, 22: 0.7}
SUMMER_SCARCITY_MAX_POINTS = 18      # veiligheidsplafond, zelfde als winter


def summer_scarcity_correction(
    wind_ms: float,
    temp_c: float,
    ttf_ratio: float,
    regime: str,
    hour: int,
) -> FactorScore:
    """Niet-lineaire opwaartse correctie voor zomerse avondschaarste (v3.2, #71)."""
    if regime != REGIME_SCARCITY_SUMMER:
        return FactorScore("zomerschaarste", 0, "n.v.t.")

    wind_term = (max(0.0, SUMMER_SCARCITY_WIND_MAX - wind_ms) ** 2) * SUMMER_K_WIND
    heat_term = (max(0.0, temp_c - SUMMER_SCARCITY_TEMP_MIN) ** 2) * SUMMER_K_HEAT
    ramp      = SUMMER_RAMP_WEIGHT.get(hour, 0.0)
    severity  = (wind_term + heat_term) * ramp
    gas_mult  = 1.0 + max(0.0, ttf_ratio - 1.0) * SUMMER_K_GAS
    total_float = severity * gas_mult * SUMMER_SCARCITY_SCALE
    pts = min(round(total_float), SUMMER_SCARCITY_MAX_POINTS)

    parts = []
    if wind_term > 0.05:
        parts.append(f"windstil +{wind_term:.1f}p")
    if heat_term > 0.05:
        parts.append(f"hitte +{heat_term:.1f}p")
    if ramp != 1.0:
        parts.append(f"ramp ×{ramp:.1f}")
    if gas_mult > 1.001:
        parts.append(f"×{gas_mult:.2f} gas")
    reason = "zomerschaarste-amplifier: " + (", ".join(parts) if parts else "grensgeval")
    return FactorScore("zomerschaarste", pts, reason)


# ---- Extreme event probabiliteit (v1.7 sectie 9) ----

def calc_extreme_event_prob(solar_ratio: float, wind_ms: float, regime: str) -> float:
    """
    Kans op negatieve EPEX-prijs (0..0.95) bij extreme oversupply (v1.7 sectie 9).

    Logistische functie op severity = max(solar_ratio/1.4, wind_ms/12).
    Drempel: severity 1.2 → P ≈ 50%  (solar ≈ 1.68 of wind ≈ 14.4 m/s).
    Alleen zinvol in REGIME_OVERSUPPLY; anders 0.0.
    """
    if regime != REGIME_OVERSUPPLY:
        return 0.0
    severity = max(
        solar_ratio / 1.4 if solar_ratio > 1.4 else 0.0,
        wind_ms / 12.0 if wind_ms > 12.0 else 0.0,
    )
    if severity <= 0.0:
        return 0.0
    x = 2.5 * (severity - 1.2)
    p = 1.0 / (1.0 + math.exp(-x))
    return round(min(p, 0.95), 3)


# ---- Onzekerheidsband ----

def uncertainty(days_ahead: int, abs_points: int) -> float:
    return 0.10 + 0.02 * days_ahead + 0.01 * abs_points


# ---- Hoofdfunctie: één forecast ----

def forecast_one(
    target_dt: datetime,
    history: list[dict],
    shortwave_ratio: float,
    wind_ms: float,
    temp_c: float,
    ttf_ratio: float,
    days_ahead: int,
    prior_day_price: Optional[float] = None,
    seasonal_history: Optional[list[dict]] = None,
) -> Optional[Forecast]:
    """
    Voorspel de prijs voor een specifiek toekomstig uur.

    prior_day_price: bekende day-ahead prijs van de voorgaande dag op hetzelfde
        uur (EUR/MWh). Alleen beschikbaar voor D+2 (eerste voorspeldag), waarbij
        de D+1-prijzen al gepubliceerd zijn. Geef None door voor D+3 en verder.

    Return: Forecast object, of None als baseline niet bepaald kon worden.
    """
    # v1.7: regime detectie — vóór baseline zodat window-keuze regime-bewust is
    regime = detect_regime(shortwave_ratio, wind_ms, temp_c, target_dt)

    # v1.11: geef regime door aan baseline zodat oversupply kortere window gebruikt
    baseline = compute_baseline(target_dt, history, regime=regime)
    if baseline is None:
        return None

    # Factor 7: vorige dag — normaliseer de prior_price op zijn eigen baseline.
    # Gebruik standaard window (geen regime-override) voor de prior_baseline:
    # de vorige dag was een andere dag met mogelijk ander regime.
    prior_ratio: Optional[float] = None
    if prior_day_price is not None:
        prior_dt = target_dt - timedelta(days=1)
        prior_baseline = compute_baseline(prior_dt, history)
        if prior_baseline and prior_baseline != 0:
            prior_ratio = prior_day_price / prior_baseline

    # v2.0: uurpatroon-blokkering bij sterk bewolkt uur (sw_ratio_h < 0.30).
    # De uurpatroon-factor gaat ervan uit dat middag goedkoop is door zon
    # (zomer: -1 punt voor 9-16h). Op een bewolkte dag is die aanname onjuist
    # en onderdrukt hij het correcte signaal van factor_zon. Als het uurlijkse
    # zonratio < 0.30 is (minder dan 30% van normaal), wordt uurpatroon op 0
    # gezet zodat factor_zon ongehinderd kan corrigeren.
    _uurpatroon = factor_uurpatroon(target_dt)
    if shortwave_ratio < 0.30 and _uurpatroon.points != 0:
        _uurpatroon = FactorScore(
            "uurpatroon", 0,
            f"geblokkeerd (bewolkt: sw_h={shortwave_ratio:.2f}<0.30)"
        )

    # v3.0: seizoensratio uit voorgaande jaren (alleen als de orchestrator
    # seasonal_history meegeeft). Telt pas mee als "seizoen" in ENABLED_FACTORS staat.
    seasonal_ratio: Optional[float] = None
    if seasonal_history:
        s_anchor = seasonal_baseline(target_dt, seasonal_history)
        if s_anchor is not None and baseline:
            seasonal_ratio = s_anchor / baseline

    factors = [
        factor_zon(shortwave_ratio),
        factor_wind(wind_ms),
        factor_temperatuur(temp_c),
        factor_gas(ttf_ratio),
        factor_dagtype(target_dt),
        _uurpatroon,
        factor_vorige_dag(prior_ratio),
        nonlinear_correction(shortwave_ratio, wind_ms, regime),  # v1.7
        scarcity_correction(shortwave_ratio, wind_ms, temp_c, ttf_ratio, regime),  # v3.1 (default uit)
        summer_scarcity_correction(wind_ms, temp_c, ttf_ratio, regime,
                                   target_dt.hour),              # v3.2 #71 (default uit)
        factor_seizoen(seasonal_ratio),                          # v3.0 (default uit)
    ]

    # v1.6: zondag-boost. Op zondag tellen zon en wind ZWAARDER (×ZONDAG_BOOST)
    # omdat de basisvraag laag is en weersinvloed de prijs sterker beweegt.
    # We vervangen de FactorScore-objects zodat de boost zichtbaar blijft in
    # de uitleg (×N erbij in `reason`-string).
    if target_dt.weekday() == 6:  # zondag
        boosted = []
        for f in factors:
            if f.name in ("zon", "wind"):
                boosted.append(FactorScore(
                    name=f.name,
                    points=f.points * ZONDAG_BOOST,
                    reason=f"{f.reason} ×{ZONDAG_BOOST} (zondag)",
                ))
            else:
                boosted.append(f)
        factors = boosted

    # Alleen ENABLED_FACTORS tellen mee in totaal-score; andere factoren
    # blijven voor transparantie zichtbaar in `factors`-lijst maar dragen niet bij.
    total = sum(f.points for f in factors if f.name in ENABLED_FACTORS)
    predicted = baseline * (1 + total * POINT_WEIGHT)
    unc = uncertainty(days_ahead, abs(total))
    band_half = None
    if UNCERTAINTY_MODE == "v4":
        band_half = BAND_ABS + BAND_REL * abs(predicted)
        unc = band_half / abs(predicted) if abs(predicted) > 1e-6 else 1.0
    ep = calc_extreme_event_prob(shortwave_ratio, wind_ms, regime)  # v1.7

    return Forecast(
        target_iso=target_dt.isoformat(),
        baseline=round(baseline, 2),
        factors=factors,
        total_points=total,
        predicted=round(predicted, 2),
        uncertainty_pct=round(unc, 4),
        days_ahead=days_ahead,
        regime=regime,
        extreme_event_prob=ep,
        band_half=band_half,
    )


# ---- Self-test (eenvoudige sanity check) ----
# Verwacht resultaat voor v2.0-model (werkdag-casus, donderdag 19:00 winter):
#   factor zon (45% van seizoen): +3  [sw_ratio_h=0.45 > 0.30 → uurpatroon NIET geblokkeerd]
#   factor wind (6 m/s zwakke wind): +1
#   factor temperatuur (8°C, koud): +1
#   factor gas (105% van 30d gem.): 0
#   factor dagtype (donderdag werkdag): 0
#   factor uurpatroon (19:00 winter avondspits): +2  [19h = avondspits winter]
#   factor nonlinear: 0  [geen oversupply regime]
#   Totaal: +7.  Baseline mediaan 26.0 EUR/MWh.
#   Voorspelling: 26.0 × (1 + 7 × 0.030) = 26.0 × 1.21 = 31.46 EUR/MWh.
#   Onzekerheid op 4d, |7| punten: 0.10 + 0.02×4 + 0.01×7 = 0.25 (±25%).

if __name__ == "__main__":
    target = datetime(2025, 12, 11, 19, 0)
    base_dates = [
        datetime(2025, 12, 4, 19, 0),   # do
        datetime(2025, 12, 5, 19, 0),   # vr
        datetime(2025, 12, 8, 19, 0),   # ma
        datetime(2025, 12, 9, 19, 0),   # di
        datetime(2025, 12, 10, 19, 0),  # wo
    ]
    base_prices = [24.0, 26.0, 25.0, 26.0, 26.0]  # mediaan = 26.0 (gesorteerd: 24,25,26,26,26)
    history = [{"time": d.isoformat(), "price": p} for d, p in zip(base_dates, base_prices)]

    f = forecast_one(
        target_dt=target,
        history=history,
        shortwave_ratio=0.45,
        wind_ms=6.0,
        temp_c=8.0,
        ttf_ratio=1.05,
        days_ahead=4,
    )
    assert f is not None, "Forecast moest lukken"
    print(f"Target: {f.target_iso}")
    print(f"Baseline: {f.baseline} EUR/MWh")
    for fs in f.factors:
        print(f"  Factor {fs.name:13s}: {fs.points:+d}  ({fs.reason})")
    print(f"Totaal punten: {f.total_points:+d}")
    print(f"Voorspelling: {f.predicted} EUR/MWh")
    print(f"Onzekerheid: +/-{f.uncertainty_pct*100:.0f}%  (band {f.lower:.2f} - {f.upper:.2f})")

    # v2.0: baseline = mediaan [24,25,26,26,26] = 26.0
    # Voorspelling: 26.0 * (1 + 7 * 0.030) = 26.0 * 1.21 = 31.46
    assert abs(f.baseline - 26.0) < 0.01, f"Verwachtte baseline 26.0 (mediaan), kreeg {f.baseline}"
    assert f.total_points == 7, f"Verwachtte 7 punten, kreeg {f.total_points}"
    assert abs(f.predicted - 31.46) < 0.1, f"Verwachtte ~31.46, kreeg {f.predicted}"
    assert abs(f.uncertainty_pct - 0.25) < 0.001, f"Verwachtte +/-25%, kreeg {f.uncertainty_pct}"

    # Test v1.7: factor_dagtype 1 mei
    mei1 = datetime(2026, 5, 1, 13, 0)
    score = factor_dagtype(mei1)
    assert score.points == -2, f"Verwachtte -2 voor 1 mei, kreeg {score.points}"
    print(f"\n[ok] factor_dagtype 1 mei: {score.points} ({score.reason})")

    # Test v1.7: baseline sluit 1 mei uit
    # (uur 14:00 zodat target en history-uren matchen — anders geen baseline-match)
    target_vr = datetime(2026, 5, 8, 14, 0)
    history_met_mei1 = [
        {"time": "2026-04-27T14:00:00", "price": 50.0},
        {"time": "2026-04-28T14:00:00", "price": 50.0},
        {"time": "2026-04-29T14:00:00", "price": 50.0},
        {"time": "2026-04-30T14:00:00", "price": 50.0},
        {"time": "2026-05-01T14:00:00", "price": -300.0},  # EU-feestdag: moet worden uitgesloten
    ]
    baseline_vr = compute_baseline(target_vr, history_met_mei1)
    assert baseline_vr is not None
    assert abs(baseline_vr - 50.0) < 0.01, f"Verwachtte 50.0 (1 mei uitgesloten), kreeg {baseline_vr}"
    print(f"[ok] baseline vrijdag (1 mei uitgesloten, mediaan): {baseline_vr} EUR/MWh")

    # Test v1.8/v1.9: factor_vorige_dag
    assert factor_vorige_dag(None).points == 0
    assert factor_vorige_dag(1.40).points == +2
    assert factor_vorige_dag(0.65).points == -2
    assert factor_vorige_dag(1.00).points == 0

    # Integratie: prior 40 EUR/MWh, baseline mediaan 26.0 -> ratio ~1.54 -> +2 punten
    # total = 7+2 = 9; predicted = 26.0 * (1 + 9*0.030) = 26.0 * 1.27 = 33.02
    f2 = forecast_one(
        target_dt=target,
        history=history,
        shortwave_ratio=0.45,
        wind_ms=6.0,
        temp_c=8.0,
        ttf_ratio=1.05,
        days_ahead=2,
        prior_day_price=40.0,
    )
    assert f2 is not None
    vd = next(x for x in f2.factors if x.name == "vorige_dag")
    assert vd.points == +2, f"Verwachtte +2, kreeg {vd.points}"
    assert f2.total_points == 9, f"Verwachtte 9 punten, kreeg {f2.total_points}"
    assert abs(f2.predicted - 33.02) < 0.1, f"Verwachtte ~33.02, kreeg {f2.predicted}"
    print("[ok] factor_vorige_dag: alle gevallen ok")

    # Test v1.7/v1.10: regime detectie
    # Donderdag werkdag winter
    thu_winter = datetime(2025, 12, 11, 14, 0)
    assert detect_regime(1.0, 7.0, 5.0, thu_winter) == REGIME_NORMAL, "Verwachtte normaal"
    # Oversupply: zonnige zaterdag (lage vraag, hoge zon)
    sat_sunny = datetime(2025, 6, 14, 13, 0)  # zaterdag
    assert detect_regime(1.6, 8.0, 18.0, sat_sunny) == REGIME_OVERSUPPLY, "Verwachtte oversupply"
    # Dunkelflaute: donker + windstil + koud
    assert detect_regime(0.4, 3.0, 2.0, thu_winter) == REGIME_SCARCITY, "Verwachtte schaarste"
    print("[ok] detect_regime: alle gevallen ok")

    # Test v1.10/v1.12: nonlinear_correction
    # v1.12: drempel 1.5→1.3, dus correctie begint eerder
    nl_normal = nonlinear_correction(1.0, 8.0, REGIME_NORMAL)
    assert nl_normal.points == 0, f"Verwachtte 0 bij normaal regime, kreeg {nl_normal.points}"
    nl_oversupply_mild = nonlinear_correction(1.5, 10.0, REGIME_OVERSUPPLY)
    assert nl_oversupply_mild.points == -1, f"Verwachtte -1 bij solar=1.5 (drempel 1.3), kreeg {nl_oversupply_mild.points}"
    nl_oversupply_extreme = nonlinear_correction(2.0, 8.0, REGIME_OVERSUPPLY)
    # -(2.0-1.3)^2 * 14 = -(0.7)^2 * 14 = -6.86 -> round(-6.86) = -7
    assert nl_oversupply_extreme.points == -7, f"Verwachtte -7 bij solar=2.0 (drempel 1.3), kreeg {nl_oversupply_extreme.points}"
    print("[ok] nonlinear_correction: alle gevallen ok")

    # Test v1.12: detect_regime uur-restrictie zon
    sat_noon = datetime(2025, 6, 14, 13, 0)    # zaterdag 13u -> oversupply
    sat_night = datetime(2025, 6, 14, 2, 0)     # zaterdag 02u -> normaal (zon niet actief)
    sat_night_wind = datetime(2025, 6, 14, 2, 0)
    assert detect_regime(1.6, 8.0, 18.0, sat_noon) == REGIME_OVERSUPPLY
    assert detect_regime(1.6, 8.0, 18.0, sat_night) == REGIME_NORMAL, "Nacht+zon moet NORMAAL zijn"
    assert detect_regime(0.0, 15.0, 18.0, sat_night_wind) == REGIME_OVERSUPPLY, "Wind>14 's nachts = oversupply"
    assert detect_regime(0.0, 12.0, 18.0, sat_night_wind) == REGIME_NORMAL, "Wind 12 m/s < drempel 14"
    print("[ok] detect_regime v1.12: uur-restrictie zon + winddrempel 14 m/s")

    # Test v1.12: compute_baseline 2d solar-piekuur
    # Vrijdag 9 jan, werkdag, 12u; history: ma-do met dalende prijs
    target_12u = datetime(2026, 1, 9, 12, 0)
    history_dalend = [
        {"time": datetime(2026, 1, d, 12, 0).isoformat(), "price": float(100 - (d-5)*10)}
        for d in range(5, 9)  # ma=100, di=90, wo=80, do=70
    ]
    b_os_2d = compute_baseline(target_12u, history_dalend, regime=REGIME_OVERSUPPLY)
    b_nm_7d = compute_baseline(target_12u, history_dalend)
    # OS 2d: vr 9 - 2d = wo 7. Matches: wo 7 (80), do 8 (70) -> mediaan 75
    # Normal 7d: ma t/m do: 100, 90, 80, 70 -> mediaan 85
    assert abs(b_os_2d - 75.0) < 0.01, f"Baseline oversupply 12u (2d): verwacht 75.0, kreeg {b_os_2d}"
    assert abs(b_nm_7d - 85.0) < 0.01, f"Baseline normaal 12u (7d): verwacht 85.0, kreeg {b_nm_7d}"
    print("[ok] compute_baseline v1.12: 2d solar-piek window vs 7d normaal")

    # Test v1.10: calc_extreme_event_prob
    assert calc_extreme_event_prob(1.0, 8.0, REGIME_NORMAL) == 0.0
    ep_mild = calc_extreme_event_prob(1.6, 8.0, REGIME_OVERSUPPLY)
    assert 0.3 < ep_mild < 0.6, f"Verwachtte ~0.46, kreeg {ep_mild}"
    ep_extreme = calc_extreme_event_prob(2.0, 8.0, REGIME_OVERSUPPLY)
    assert ep_extreme > 0.55, f"Verwachtte >0.55, kreeg {ep_extreme}"
    print(f"[ok] calc_extreme_event_prob: mild={ep_mild:.2f}, extreme={ep_extreme:.2f}")

    # Integratie: regime + nonlinear in forecast_one op zonnige zomerzondag
    zomerzondag = datetime(2026, 7, 5, 12, 0)  # zondag
    history_zomer = [
        {"time": f"2026-06-2{i}T12:00:00", "price": 20.0} for i in range(1, 6)
    ] + [
        {"time": "2026-06-27T12:00:00", "price": 15.0},
        {"time": "2026-06-28T12:00:00", "price": 15.0},
    ]
    f_oversupply = forecast_one(
        target_dt=zomerzondag,
        history=history_zomer,
        shortwave_ratio=1.8,
        wind_ms=8.0,
        temp_c=18.0,
        ttf_ratio=1.0,
        days_ahead=1,
    )
    assert f_oversupply is not None
    assert f_oversupply.regime == REGIME_OVERSUPPLY, f"Verwachtte oversupply, kreeg {f_oversupply.regime}"
    assert f_oversupply.extreme_event_prob > 0.3, f"Verwachtte EP > 0.3, kreeg {f_oversupply.extreme_event_prob}"
    print(f"[ok] forecast_one oversupply: regime={f_oversupply.regime}, EP={f_oversupply.extreme_event_prob:.2f}")

    # Test v3.1: scarcity_correction (mirror van factor 8, omhoog)
    # Buiten REGIME_SCARCITY altijd 0:
    assert scarcity_correction(0.3, 2.0, -2.0, 1.2, REGIME_NORMAL).points == 0
    assert scarcity_correction(0.3, 2.0, -2.0, 1.2, REGIME_OVERSUPPLY).points == 0
    assert scarcity_correction(1.0, 8.0, 12.0, 1.0, REGIME_SCARCITY).points == 0, "geen severity -> 0"
    # De twee rekenvoorbeelden hieronder zijn uitgeschreven voor SCARCITY_SCALE=1.0.
    # De live default staat sinds v3.1 op 1.5, dus pin de schaal voor deze asserts —
    # anders test je de schaal in plaats van de formule. Deze pin repareert een test
    # die sinds de 1.5-uitrol faalde en daarmee de rest van de suite blokkeerde.
    _scale_backup = SCARCITY_SCALE
    globals()["SCARCITY_SCALE"] = 1.0
    try:
        # Diepe Dunkelflaute: wind=(3)^2*0.9=8.1, cold=(10)^2*0.04=4.0, solar=(0.3)^2*6=0.54
        # severity=12.64; gas_mult=1+0.2=1.2 -> 15.17 -> +15
        deep = scarcity_correction(0.30, 2.0, -2.0, 1.20, REGIME_SCARCITY)
        assert deep.points == 15, f"Verwachtte +15 in diepe Dunkelflaute, kreeg {deep.points}"
        assert deep.points > 0, "schaarste moet OMHOOG corrigeren"
        # Milde schaarste aan de regime-rand: wind=(1)^2*0.9=0.9, cold=(2)^2*0.04=0.16,
        # solar=(0.1)^2*6=0.06 -> 1.12; gas_mult 1.0 -> +1
        mild = scarcity_correction(0.50, 4.0, 6.0, 1.00, REGIME_SCARCITY)
        assert mild.points == 1, f"Verwachtte +1 bij milde schaarste, kreeg {mild.points}"
    finally:
        globals()["SCARCITY_SCALE"] = _scale_backup
    # Plafond: extreme kou mag niet exploderen
    capped = scarcity_correction(0.0, 0.0, -20.0, 2.0, REGIME_SCARCITY)
    assert capped.points == SCARCITY_MAX_POINTS, f"Verwachtte plafond {SCARCITY_MAX_POINTS}, kreeg {capped.points}"
    # SCARCITY_SCALE-knop schaalt lineair (backtest A/B): scale 0 -> 0 punten
    _saved_scale = SCARCITY_SCALE
    globals()["SCARCITY_SCALE"] = 0.0
    assert scarcity_correction(0.30, 2.0, -2.0, 1.20, REGIME_SCARCITY).points == 0, "scale 0 -> 0"
    globals()["SCARCITY_SCALE"] = _saved_scale
    print(f"[ok] scarcity_correction: gated op schaarste, diep={deep.points:+d}, mild={mild.points:+d}, plafond={capped.points:+d}")

    # Integratie: scarcity telt alleen mee als 'scarcity' in ENABLED_FACTORS staat.
    # Default staat hij UIT, dus een Dunkelflaute-forecast mag NIET extra omhoog.
    dunkel_dt = datetime(2025, 12, 11, 18, 0)  # do winter avond
    dunkel_hist = [
        {"time": datetime(2025, 12, d, 18, 0).isoformat(), "price": 90.0}
        for d in (4, 5, 8, 9, 10)
    ]
    # Ook hier de schaal pinnen op 1.0: het verwachte puntenaantal (15) hoort bij
    # de uitgeschreven formule, niet bij de live SCARCITY_SCALE.
    _scale_backup2 = SCARCITY_SCALE
    globals()["SCARCITY_SCALE"] = 1.0
    try:
        f_off = forecast_one(dunkel_dt, dunkel_hist, shortwave_ratio=0.3, wind_ms=2.0,
                             temp_c=-2.0, ttf_ratio=1.2, days_ahead=2)
        assert f_off is not None and f_off.regime == REGIME_SCARCITY
        sc_off = next(x for x in f_off.factors if x.name == "scarcity")
        assert sc_off.points == 15, "factor zichtbaar in uitleg, ook als uit"
        # 'scarcity' staat NIET in ENABLED_FACTORS -> draagt niet bij aan total
        points_zonder = f_off.total_points
        ENABLED_FACTORS.add("scarcity")
        try:
            f_on = forecast_one(dunkel_dt, dunkel_hist, shortwave_ratio=0.3, wind_ms=2.0,
                                temp_c=-2.0, ttf_ratio=1.2, days_ahead=2)
            assert f_on is not None
            assert f_on.total_points == points_zonder + 15, (
                f"Met scarcity AAN moet total +15 hoger zijn: {f_on.total_points} vs {points_zonder}")
            assert f_on.predicted > f_off.predicted, "scarcity AAN moet voorspelling omhoog duwen"
        finally:
            ENABLED_FACTORS.discard("scarcity")
    finally:
        globals()["SCARCITY_SCALE"] = _scale_backup2
    print(f"[ok] forecast_one schaarste: uit={points_zonder:+d}p ({f_off.predicted}), "
          f"aan=+{15}p toggle werkt, default UIT bevestigd")

    # ---- v3.2 (#71): zomer-schaarste-regime + amplifier ----
    hitte_dt = datetime(2026, 6, 23, 20, 0)  # di hittegolf-avond, 20u
    # Regime-flag default UIT: hete windstille avond blijft 'normaal'
    assert detect_regime(1.2, 2.0, 27.0, hitte_dt) == REGIME_NORMAL, \
        "flag uit -> normaal verwacht"
    globals()["ENABLE_SUMMER_SCARCITY_REGIME"] = True
    try:
        assert detect_regime(1.2, 2.0, 27.0, hitte_dt) == REGIME_SCARCITY_SUMMER, \
            "flag aan + hitte + windstil + 20u -> zomerschaarste verwacht"
        # Buiten de ramp-uren of bij wind/koelte: geen zomerschaarste
        assert detect_regime(1.2, 2.0, 27.0, datetime(2026, 6, 23, 13, 0)) != \
            REGIME_SCARCITY_SUMMER, "13u valt buiten de avondramp"
        assert detect_regime(1.2, 8.0, 27.0, hitte_dt) == REGIME_NORMAL, "te veel wind"
        assert detect_regime(1.2, 2.0, 18.0, hitte_dt) == REGIME_NORMAL, "niet heet genoeg"
        assert detect_regime(1.2, 2.0, 27.0, datetime(2026, 2, 10, 20, 0)) != \
            REGIME_SCARCITY_SUMMER, "februari is geen zomermaand"
        # Amplifier: gated, voorbeeldwaarde en plafond (afstelling v2: K_WIND 1.5, TEMP_MIN 20)
        z_uit = summer_scarcity_correction(3.0, 26.0, 1.0, REGIME_NORMAL, 20)
        assert z_uit.points == 0, "buiten zomerschaarste-regime altijd 0"
        z_voor = summer_scarcity_correction(3.0, 26.0, 1.0, REGIME_SCARCITY_SUMMER, 20)
        assert z_voor.points == 10, f"voorbeeld hittegolf-avond: +10 verwacht, kreeg {z_voor.points}"
        z_cap = summer_scarcity_correction(2.0, 27.0, 1.0, REGIME_SCARCITY_SUMMER, 20)
        assert z_cap.points == SUMMER_SCARCITY_MAX_POINTS, \
            f"extreem geval moet plafond {SUMMER_SCARCITY_MAX_POINTS} raken, kreeg {z_cap.points}"
        # Randgeval blijft mild: wind 3.5, temp 22 -> +4
        z_mild = summer_scarcity_correction(3.5, 22.0, 1.0, REGIME_SCARCITY_SUMMER, 20)
        assert z_mild.points == 4, f"randgeval: +4 verwacht, kreeg {z_mild.points}"
        # Ramp-gewicht: 18u weegt half t.o.v. 20u
        z_rand = summer_scarcity_correction(3.0, 26.0, 1.0, REGIME_SCARCITY_SUMMER, 18)
        assert z_rand.points == 5, f"18u (ramp 0.5): +5 verwacht, kreeg {z_rand.points}"
        # Gas-hefboom: ttf 1.2 tilt het voorbeeld naar +12
        z_gas = summer_scarcity_correction(3.0, 26.0, 1.2, REGIME_SCARCITY_SUMMER, 20)
        assert z_gas.points == 12, f"gas-hefboom: +12 verwacht, kreeg {z_gas.points}"
        # Schaal-knop lineair: scale 0 -> 0 punten
        _saved_zscale = SUMMER_SCARCITY_SCALE
        globals()["SUMMER_SCARCITY_SCALE"] = 0.0
        assert summer_scarcity_correction(3.0, 26.0, 1.0, REGIME_SCARCITY_SUMMER, 20).points == 0
        globals()["SUMMER_SCARCITY_SCALE"] = _saved_zscale
        # Integratie: telt alleen mee als 'zomerschaarste' in ENABLED_FACTORS staat
        hitte_hist = [
            {"time": datetime(2026, 6, d, 20, 0).isoformat(), "price": 110.0}
            for d in (16, 17, 18, 19, 22)
        ]
        f_z_uit = forecast_one(hitte_dt, hitte_hist, shortwave_ratio=1.2, wind_ms=3.0,
                               temp_c=26.0, ttf_ratio=1.0, days_ahead=2)
        assert f_z_uit is not None and f_z_uit.regime == REGIME_SCARCITY_SUMMER
        z_pts = next(x for x in f_z_uit.factors if x.name == "zomerschaarste").points
        assert z_pts == 10, "factor zichtbaar in uitleg, ook als uit"
        pts_zonder_z = f_z_uit.total_points
        ENABLED_FACTORS.add("zomerschaarste")
        try:
            f_z_aan = forecast_one(hitte_dt, hitte_hist, shortwave_ratio=1.2, wind_ms=3.0,
                                   temp_c=26.0, ttf_ratio=1.0, days_ahead=2)
            assert f_z_aan is not None
            assert f_z_aan.total_points == pts_zonder_z + z_pts, \
                f"met zomerschaarste AAN moet total +{z_pts}: {f_z_aan.total_points} vs {pts_zonder_z}"
            assert f_z_aan.predicted > f_z_uit.predicted, "AAN moet voorspelling omhoog duwen"
        finally:
            ENABLED_FACTORS.discard("zomerschaarste")
    finally:
        globals()["ENABLE_SUMMER_SCARCITY_REGIME"] = False
    print(f"[ok] zomerschaarste (v3.2 #71): regime-flag, gating, +{z_voor.points}p voorbeeld, "
          f"plafond {z_cap.points}, ramp-gewicht en toggle werken; default UIT bevestigd")

    # ---- v3.3 (optie 1): niveauverschuiving-detectie ----
    # Echte casus: doel di 18 aug 2026 14:00. Werkdagen op uur 14 in het venster:
    # 11 aug -1.94, 12 aug -4.53, 13 aug 5.00, 14 aug 24.00, 17 aug 141.27.
    # Mediaan = 5.00 terwijl de markt op 141 zit.
    ls_dt = datetime(2026, 8, 18, 14, 0)
    ls_hist = [
        {"time": datetime(2026, 8, d, 14, 0).isoformat(), "price": p}
        for d, p in ((11, -1.94), (12, -4.53), (13, 5.00), (14, 24.00), (17, 141.27))
    ]
    assert abs(compute_baseline(ls_dt, ls_hist) - 5.00) < 0.01, "default UIT moet de mediaan geven"

    _ls_backup = (ENABLE_LEVEL_SHIFT, LEVEL_SHIFT_WEIGHT)
    try:
        globals()["ENABLE_LEVEL_SHIFT"] = True
        globals()["LEVEL_SHIFT_WEIGHT"] = 1.0
        b_vol = compute_baseline(ls_dt, ls_hist)
        assert abs(b_vol - 141.27) < 0.01, f"gewicht 1.0 -> volledig naar de sprong, kreeg {b_vol}"
        globals()["LEVEL_SHIFT_WEIGHT"] = 0.5
        b_half = compute_baseline(ls_dt, ls_hist)
        assert abs(b_half - (0.5 * 5.00 + 0.5 * 141.27)) < 0.01, f"gewicht 0.5, kreeg {b_half}"

        # Symmetrie: dezelfde sprong omlaag moet ook vuren.
        drop_hist = [
            {"time": datetime(2026, 8, d, 14, 0).isoformat(), "price": p}
            for d, p in ((11, 100.0), (12, 105.0), (13, 98.0), (14, 102.0), (17, 5.0))
        ]
        globals()["LEVEL_SHIFT_WEIGHT"] = 1.0
        b_drop = compute_baseline(ls_dt, drop_hist)
        assert abs(b_drop - 5.0) < 0.01, f"val moet ook vuren, kreeg {b_drop}"

        # Ruis mag NIET vuren: relatief groot, absoluut klein (0.5 -> 3.0).
        ruis_hist = [
            {"time": datetime(2026, 8, d, 14, 0).isoformat(), "price": p}
            for d, p in ((11, 0.4), (12, 0.6), (13, 0.5), (14, 0.5), (17, 3.0))
        ]
        assert abs(compute_baseline(ls_dt, ruis_hist) - 0.5) < 0.01, "kleine absolute sprong: niet vuren"

        # Absoluut groot, relatief klein (150 -> 200) mag ook niet vuren.
        vlak_hist = [
            {"time": datetime(2026, 8, d, 14, 0).isoformat(), "price": p}
            for d, p in ((11, 150.0), (12, 155.0), (13, 148.0), (14, 152.0), (17, 200.0))
        ]
        assert abs(compute_baseline(ls_dt, vlak_hist) - 152.0) < 0.01, "relatief kleine sprong: niet vuren"

        # Versheidstest: een sprong die 5 dagen oud is, telt niet meer.
        oud_dt = datetime(2026, 8, 24, 14, 0)
        oud_hist = ls_hist + [
            {"time": datetime(2026, 8, d, 14, 0).isoformat(), "price": p}
            for d, p in ((18, 4.0), (19, 3.0), (20, 6.0), (21, 5.0))
        ]
        b_oud = compute_baseline(oud_dt, oud_hist)
        assert b_oud is not None and b_oud < 10.0, f"oude sprong mag niet meer schuiven, kreeg {b_oud}"
        print("[ok] niveauverschuiving (v3.3 optie 1): vuurt op sprong, symmetrisch, negeert "
              "ruis/vlakke stijging/oude sprong, default UIT bevestigd")
    finally:
        globals()["ENABLE_LEVEL_SHIFT"], globals()["LEVEL_SHIFT_WEIGHT"] = _ls_backup


    # ---- v4: niveauschatter, bodem en band ----
    _v4_start = datetime(2026, 7, 1, 0, 0)

    def _mk_hist(late_price):
        rows = []
        for _d in range(28):
            for _h in range(24):
                _t = _v4_start + timedelta(days=_d, hours=_h)
                rows.append({"time": _t.isoformat(),
                             "price": 50.0 if _d < 21 else late_price})
        return rows

    _target = _v4_start + timedelta(days=29, hours=12)
    _prev_mode = BASELINE_MODE
    BASELINE_MODE = "v4"
    _vlak = compute_baseline(_target, _mk_hist(50.0))
    _sprong = compute_baseline(_target, _mk_hist(150.0))
    BASELINE_MODE = "legacy"
    _legacy_sprong = compute_baseline(_target, _mk_hist(150.0))
    BASELINE_MODE = _prev_mode

    assert _vlak is not None and abs(_vlak - 50.0) < 0.01, f"vlakke reeks moet 50 geven: {_vlak}"
    assert 50.0 < _sprong < 150.0, f"v4 hoort tussen oud en nieuw niveau te liggen: {_sprong}"
    assert _sprong > 55.0, f"trendfactor doet te weinig: {_sprong}"
    assert _legacy_sprong is not None and _sprong < _legacy_sprong, \
        "v4 hoort trager te reageren dan het 7/14-daagse venster"
    # geen look-ahead: prijzen op of na het doel-uur mogen de uitkomst niet raken
    BASELINE_MODE = "v4"
    _na = [{"time": (_target + timedelta(hours=_k)).isoformat(), "price": 900.0}
           for _k in range(0, 72)]
    _zonder = compute_baseline(_target, _mk_hist(150.0))
    _met = compute_baseline(_target, _mk_hist(150.0) + _na)
    BASELINE_MODE = _prev_mode
    assert abs(_zonder - _met) < 1e-9, f"look-ahead: {_zonder} vs {_met}"
    print(f"[ok] v4-baseline: vlak {_vlak:.1f}; na een niveausprong {_sprong:.1f} "
          f"(legacy {_legacy_sprong:.1f}) — trager maar niet blind; "
          f"prijzen na het doel-uur veranderen niets")

    _prev_floor = NONLINEAR_FLOOR
    NONLINEAR_FLOOR = None
    _diep = nonlinear_correction(3.0, 5.0, REGIME_OVERSUPPLY)
    NONLINEAR_FLOOR = -3.0
    _begrensd = nonlinear_correction(3.0, 5.0, REGIME_OVERSUPPLY)
    NONLINEAR_FLOOR = _prev_floor
    assert _diep.points <= -40, f"verwachtte een diepe correctie, kreeg {_diep.points}"
    assert _begrensd.points == -3, f"bodem werkt niet: {_begrensd.points}"
    assert nonlinear_correction(1.0, 5.0, REGIME_NORMAL).points == 0
    print(f"[ok] bodem nonlinear: {_diep.points}p -> {_begrensd.points}p, normaal-regime blijft 0")

    _prev_unc = UNCERTAINTY_MODE
    UNCERTAINTY_MODE = "v4"
    _f_band = forecast_one(
        target_dt=target, history=history, shortwave_ratio=0.45, wind_ms=6.0,
        temp_c=8.0, ttf_ratio=1.05, days_ahead=4)
    UNCERTAINTY_MODE = _prev_unc
    _half = _f_band.upper - _f_band.predicted
    _verwacht = BAND_ABS + BAND_REL * abs(_f_band.predicted)
    assert abs(_half - _verwacht) < 0.5, f"band klopt niet: {_half} vs {_verwacht}"
    assert BASELINE_MODE == "legacy" and NONLINEAR_FLOOR is None and UNCERTAINTY_MODE == "legacy", \
        "v4-schakelaars moeten standaard uit staan"
    print(f"[ok] v4-band: +-{_half:.1f} EUR/MWh bij voorspelling {_f_band.predicted:.1f}; "
          f"alle v4-schakelaars default UIT bevestigd")

    print("\n[ok] Self-test geslaagd; v3.1 schaarste-amplifier + v3.2 zomerschaarste + "
          "v3.3 niveauverschuiving (alle drie default uit).")
