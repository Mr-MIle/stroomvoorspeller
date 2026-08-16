# A/B niveauverschuiving — run 31974106160

Commit: e0ef78b843efe903ff458ad08139b38f2fc9075b

## winter-control--backtest-resultaat-v1.md

# Backtest-resultaat v1 - voorspellingsmodel

**Datum**: 2026-08-16 21:38
**Periode**: 2025-12-31 t/m 2026-02-28
**Databron**: Archief + Open-Meteo Archive + Yahoo Finance TTF=F
**Datapunten**: 5760
**Instellingen**: `--days 60 --horizons 1,3,5,7 --source archive --end-date 2026-02-28 --bias-mode off --output-dir out`

Dit rapport is automatisch gegenereerd door `02-code/scripts/backtest.py`.
Het evalueert het 6-puntenmodel uit `forecast.py` retrospectief.

## Beslissingscriteria (uit methodologie sectie 8.4)

- MAE op 1-3 dagen vooruit moet beter zijn dan de naieve baseline (alleen 7d-gemiddelde, geen factoren). Anders dragen de factoren niets bij.
- Bias dicht bij nul. Structurele afwijking duidt op verkeerde drempels.
- Hit-rate goedkoop/duur > 65% = klaar voor live; < 55% = terug naar tekentafel.

## Samenvatting per horizon

| Horizon | n | MAE (EUR/MWh) | Bias | Naieve MAE | Verbetering | Goedkoop hit-rate | Duur hit-rate | Richting-hit |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1d | 1440 | 18.79 | +7.12 | 16.81 | -11.8% | 26% (160) | 63% (248) | 44% |
| 3d | 1440 | 19.76 | +5.12 | 18.30 | -8.0% | 27% (140) | 57% (259) | 45% |
| 5d | 1440 | 22.20 | +3.93 | 20.35 | -9.1% | 30% (148) | 52% (284) | 44% |
| 7d | 1440 | 21.85 | +3.43 | 19.73 | -10.7% | 26% (155) | 55% (276) | 43% |

**Lezen**: "Verbetering" is de relatieve daling van MAE t.o.v. de naieve baseline (alleen 7d-gemiddelde, geen factoren). Positief = factoren helpen. "Richting-hit" = % van uren waar het model de juiste *richting* van afwijking t.o.v. baseline aangaf (niet de magnitude).

## Categorisatie-drempels (uit config.json)

- Goedkoop: < EUR 72.42/MWh
- Normaal: EUR 72.42 - EUR 122.0/MWh
- Duur: > EUR 122.0/MWh

## Conclusie

- **Factoren verslechteren** op horizon 1d: MAE is 11.8% hoger dan naieve baseline. Drempels of gewichten herzien.
- **Bias** op 1d (+7.12 EUR/MWh) wijst op systematische over-/onderschatting.
- **Hit-rate** goedkoop/duur < 55% op 1d - terug naar tekentafel volgens criterium.
- **Schaalverloop** MAE 7d/1d = 1.16x - verwacht is een factor 1,5-2,5.

## Gebruiksgerichte metrics (v1.7)

### Rank accuracy (Spearman ρ)

Gemiddelde Spearman ρ over 240 dag×horizon-combinaties: **0.783**
→ Model sorteert uren redelijk goed van goedkoop naar duur.

### Cheap-hour hit rate (goedkoopste 25% uren)

Van de werkelijk goedkoopste 6 uren per dag zit **68%** ook in de voorspelde goedkoopste 6 uren (n=1440 slots).
→ Bruikbaar voor slimme laadadvies-toepassingen.

### Negatieve prijs detectie

TP=0, FP=0, FN=12
Geen negatieve prijsuren in testperiode (of model voorspelt nooit negatief).

## Regime-uitslag (v1.7)

| Regime | n | MAE (EUR/MWh) | Bias |
|:---|---:|---:|---:|
| Normaal | 4837 | 21.51 | +5.93 |
| Oversupply (hernieuwbaar) | 539 | 17.18 | -3.31 |
| Schaarste / Dunkelflaute | 384 | 14.70 | +3.44 |


## MAE per uurblok

| Uurblok | n | MAE (EUR/MWh) | Bias |
|:---|---:|---:|---:|
| 00-05u nacht | 1440 | 14.05 | +0.12 |
| 06-08u ochtend | 720 | 22.77 | +6.36 |
| 09-16u midden | 1920 | 23.83 | +8.00 |
| 17-18u vooravond | 480 | 33.33 | +5.93 |
| 19-21u avondpiek | 720 | 20.01 | +6.28 |
| 22-23u laat | 480 | 12.84 | +1.60 |

## Caveats

1. **Perfect-foresight weather**: deze backtest gebruikt de werkelijke gemeten weersgegevens op de targetdag, niet de voorspelde weersdata van het moment van forecast. Dit overschat de modelkwaliteit licht. In productie introduceert weervoorspellingsfout extra variantie. Voor de 7-dagen horizon kan dat substantieel zijn.
2. **Een locatie per weervariabele**: De Bilt voor zon en temperatuur, idem voor wind. De methodologie noemt drie windlocaties; voor een latere iteratie kan dat verfijnen.
3. **Seizoennorm zonneproductie**: hardgecodeerde 12-maand-tabel voor De Bilt klimatologie; ruwe interpolatie tussen maandgemiddelden.
4. **TTF**: een ticker (Yahoo TTF=F front-month), close-to-close. Weekend/feestdagen vullen we vooruit met laatst-bekende koers.
5. **Sample-modus**: bij gebruik van `--sample` is de evaluatie zelf-circulair (synthetische prijzen vs. dezelfde structuur) en zegt alleen iets over de mechanica, niets over voorspelkracht.

## Voorstellen op basis van metrics

- Bias structureel positief: voorspellingen zitten te hoog. Overweeg POINT_WEIGHT van 4% naar 3% te verlagen, of de positieve drempels van factoren strenger te maken.

---

*Ruwe datapunten: zie `03-data/backtest-results.json` (of de map opgegeven met --output-dir).*

## winter-w05--backtest-resultaat-v1.md

# Backtest-resultaat v1 - voorspellingsmodel

**Datum**: 2026-08-16 21:39
**Periode**: 2025-12-31 t/m 2026-02-28
**Databron**: Archief + Open-Meteo Archive + Yahoo Finance TTF=F + niveauverschuiving (w=0.5, ratio=3.0, gap=40.0)
**Datapunten**: 5760
**Instellingen**: `--days 60 --horizons 1,3,5,7 --source archive --end-date 2026-02-28 --bias-mode off --level-shift --level-shift-weight 0.5 --output-dir out`

Dit rapport is automatisch gegenereerd door `02-code/scripts/backtest.py`.
Het evalueert het 6-puntenmodel uit `forecast.py` retrospectief.

## Beslissingscriteria (uit methodologie sectie 8.4)

- MAE op 1-3 dagen vooruit moet beter zijn dan de naieve baseline (alleen 7d-gemiddelde, geen factoren). Anders dragen de factoren niets bij.
- Bias dicht bij nul. Structurele afwijking duidt op verkeerde drempels.
- Hit-rate goedkoop/duur > 65% = klaar voor live; < 55% = terug naar tekentafel.

## Samenvatting per horizon

| Horizon | n | MAE (EUR/MWh) | Bias | Naieve MAE | Verbetering | Goedkoop hit-rate | Duur hit-rate | Richting-hit |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1d | 1440 | 18.85 | +6.78 | 16.86 | -11.8% | 26% (160) | 63% (248) | 44% |
| 3d | 1440 | 19.95 | +4.84 | 18.48 | -7.9% | 29% (140) | 57% (259) | 45% |
| 5d | 1440 | 22.23 | +3.64 | 20.43 | -8.8% | 33% (148) | 52% (284) | 45% |
| 7d | 1440 | 21.76 | +3.20 | 19.70 | -10.5% | 29% (155) | 55% (276) | 44% |

**Lezen**: "Verbetering" is de relatieve daling van MAE t.o.v. de naieve baseline (alleen 7d-gemiddelde, geen factoren). Positief = factoren helpen. "Richting-hit" = % van uren waar het model de juiste *richting* van afwijking t.o.v. baseline aangaf (niet de magnitude).

## Categorisatie-drempels (uit config.json)

- Goedkoop: < EUR 72.42/MWh
- Normaal: EUR 72.42 - EUR 122.0/MWh
- Duur: > EUR 122.0/MWh

## Conclusie

- **Factoren verslechteren** op horizon 1d: MAE is 11.8% hoger dan naieve baseline. Drempels of gewichten herzien.
- **Bias** op 1d (+6.78 EUR/MWh) wijst op systematische over-/onderschatting.
- **Hit-rate** goedkoop/duur < 55% op 1d - terug naar tekentafel volgens criterium.
- **Schaalverloop** MAE 7d/1d = 1.15x - verwacht is een factor 1,5-2,5.

## Gebruiksgerichte metrics (v1.7)

### Rank accuracy (Spearman ρ)

Gemiddelde Spearman ρ over 240 dag×horizon-combinaties: **0.785**
→ Model sorteert uren redelijk goed van goedkoop naar duur.

### Cheap-hour hit rate (goedkoopste 25% uren)

Van de werkelijk goedkoopste 6 uren per dag zit **68%** ook in de voorspelde goedkoopste 6 uren (n=1440 slots).
→ Bruikbaar voor slimme laadadvies-toepassingen.

### Negatieve prijs detectie

TP=0, FP=0, FN=12
Geen negatieve prijsuren in testperiode (of model voorspelt nooit negatief).

## Regime-uitslag (v1.7)

| Regime | n | MAE (EUR/MWh) | Bias |
|:---|---:|---:|---:|
| Normaal | 4837 | 21.58 | +5.64 |
| Oversupply (hernieuwbaar) | 539 | 16.99 | -3.69 |
| Schaarste / Dunkelflaute | 384 | 14.70 | +3.44 |


## MAE per uurblok

| Uurblok | n | MAE (EUR/MWh) | Bias |
|:---|---:|---:|---:|
| 00-05u nacht | 1440 | 14.41 | -0.64 |
| 06-08u ochtend | 720 | 22.77 | +6.36 |
| 09-16u midden | 1920 | 23.70 | +7.71 |
| 17-18u vooravond | 480 | 33.33 | +5.93 |
| 19-21u avondpiek | 720 | 20.01 | +6.28 |
| 22-23u laat | 480 | 12.84 | +1.60 |

## Caveats

1. **Perfect-foresight weather**: deze backtest gebruikt de werkelijke gemeten weersgegevens op de targetdag, niet de voorspelde weersdata van het moment van forecast. Dit overschat de modelkwaliteit licht. In productie introduceert weervoorspellingsfout extra variantie. Voor de 7-dagen horizon kan dat substantieel zijn.
2. **Een locatie per weervariabele**: De Bilt voor zon en temperatuur, idem voor wind. De methodologie noemt drie windlocaties; voor een latere iteratie kan dat verfijnen.
3. **Seizoennorm zonneproductie**: hardgecodeerde 12-maand-tabel voor De Bilt klimatologie; ruwe interpolatie tussen maandgemiddelden.
4. **TTF**: een ticker (Yahoo TTF=F front-month), close-to-close. Weekend/feestdagen vullen we vooruit met laatst-bekende koers.
5. **Sample-modus**: bij gebruik van `--sample` is de evaluatie zelf-circulair (synthetische prijzen vs. dezelfde structuur) en zegt alleen iets over de mechanica, niets over voorspelkracht.

## Voorstellen op basis van metrics

- Bias structureel positief: voorspellingen zitten te hoog. Overweeg POINT_WEIGHT van 4% naar 3% te verlagen, of de positieve drempels van factoren strenger te maken.

---

*Ruwe datapunten: zie `03-data/backtest-results.json` (of de map opgegeven met --output-dir).*

## winter-w10--backtest-resultaat-v1.md

# Backtest-resultaat v1 - voorspellingsmodel

**Datum**: 2026-08-16 21:38
**Periode**: 2025-12-31 t/m 2026-02-28
**Databron**: Archief + Open-Meteo Archive + Yahoo Finance TTF=F + niveauverschuiving (w=1.0, ratio=3.0, gap=40.0)
**Datapunten**: 5760
**Instellingen**: `--days 60 --horizons 1,3,5,7 --source archive --end-date 2026-02-28 --bias-mode off --level-shift --level-shift-weight 1.0 --output-dir out`

Dit rapport is automatisch gegenereerd door `02-code/scripts/backtest.py`.
Het evalueert het 6-puntenmodel uit `forecast.py` retrospectief.

## Beslissingscriteria (uit methodologie sectie 8.4)

- MAE op 1-3 dagen vooruit moet beter zijn dan de naieve baseline (alleen 7d-gemiddelde, geen factoren). Anders dragen de factoren niets bij.
- Bias dicht bij nul. Structurele afwijking duidt op verkeerde drempels.
- Hit-rate goedkoop/duur > 65% = klaar voor live; < 55% = terug naar tekentafel.

## Samenvatting per horizon

| Horizon | n | MAE (EUR/MWh) | Bias | Naieve MAE | Verbetering | Goedkoop hit-rate | Duur hit-rate | Richting-hit |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1d | 1440 | 18.94 | +6.45 | 16.94 | -11.8% | 26% (160) | 63% (248) | 43% |
| 3d | 1440 | 20.16 | +4.56 | 18.68 | -7.9% | 29% (140) | 57% (259) | 45% |
| 5d | 1440 | 22.50 | +3.35 | 20.70 | -8.7% | 33% (148) | 52% (284) | 45% |
| 7d | 1440 | 21.95 | +2.97 | 19.88 | -10.4% | 29% (155) | 55% (276) | 44% |

**Lezen**: "Verbetering" is de relatieve daling van MAE t.o.v. de naieve baseline (alleen 7d-gemiddelde, geen factoren). Positief = factoren helpen. "Richting-hit" = % van uren waar het model de juiste *richting* van afwijking t.o.v. baseline aangaf (niet de magnitude).

## Categorisatie-drempels (uit config.json)

- Goedkoop: < EUR 72.42/MWh
- Normaal: EUR 72.42 - EUR 122.0/MWh
- Duur: > EUR 122.0/MWh

## Conclusie

- **Factoren verslechteren** op horizon 1d: MAE is 11.8% hoger dan naieve baseline. Drempels of gewichten herzien.
- **Bias** op 1d (+6.45 EUR/MWh) wijst op systematische over-/onderschatting.
- **Hit-rate** goedkoop/duur < 55% op 1d - terug naar tekentafel volgens criterium.
- **Schaalverloop** MAE 7d/1d = 1.16x - verwacht is een factor 1,5-2,5.

## Gebruiksgerichte metrics (v1.7)

### Rank accuracy (Spearman ρ)

Gemiddelde Spearman ρ over 240 dag×horizon-combinaties: **0.785**
→ Model sorteert uren redelijk goed van goedkoop naar duur.

### Cheap-hour hit rate (goedkoopste 25% uren)

Van de werkelijk goedkoopste 6 uren per dag zit **68%** ook in de voorspelde goedkoopste 6 uren (n=1440 slots).
→ Bruikbaar voor slimme laadadvies-toepassingen.

### Negatieve prijs detectie

TP=0, FP=0, FN=12
Geen negatieve prijsuren in testperiode (of model voorspelt nooit negatief).

## Regime-uitslag (v1.7)

| Regime | n | MAE (EUR/MWh) | Bias |
|:---|---:|---:|---:|
| Normaal | 4837 | 21.80 | +5.34 |
| Oversupply (hernieuwbaar) | 539 | 17.07 | -4.07 |
| Schaarste / Dunkelflaute | 384 | 14.70 | +3.44 |


## MAE per uurblok

| Uurblok | n | MAE (EUR/MWh) | Bias |
|:---|---:|---:|---:|
| 00-05u nacht | 1440 | 15.17 | -1.40 |
| 06-08u ochtend | 720 | 22.77 | +6.36 |
| 09-16u midden | 1920 | 23.70 | +7.43 |
| 17-18u vooravond | 480 | 33.33 | +5.93 |
| 19-21u avondpiek | 720 | 20.01 | +6.28 |
| 22-23u laat | 480 | 12.84 | +1.60 |

## Caveats

1. **Perfect-foresight weather**: deze backtest gebruikt de werkelijke gemeten weersgegevens op de targetdag, niet de voorspelde weersdata van het moment van forecast. Dit overschat de modelkwaliteit licht. In productie introduceert weervoorspellingsfout extra variantie. Voor de 7-dagen horizon kan dat substantieel zijn.
2. **Een locatie per weervariabele**: De Bilt voor zon en temperatuur, idem voor wind. De methodologie noemt drie windlocaties; voor een latere iteratie kan dat verfijnen.
3. **Seizoennorm zonneproductie**: hardgecodeerde 12-maand-tabel voor De Bilt klimatologie; ruwe interpolatie tussen maandgemiddelden.
4. **TTF**: een ticker (Yahoo TTF=F front-month), close-to-close. Weekend/feestdagen vullen we vooruit met laatst-bekende koers.
5. **Sample-modus**: bij gebruik van `--sample` is de evaluatie zelf-circulair (synthetische prijzen vs. dezelfde structuur) en zegt alleen iets over de mechanica, niets over voorspelkracht.

## Voorstellen op basis van metrics

- Bias structureel positief: voorspellingen zitten te hoog. Overweeg POINT_WEIGHT van 4% naar 3% te verlagen, of de positieve drempels van factoren strenger te maken.

---

*Ruwe datapunten: zie `03-data/backtest-results.json` (of de map opgegeven met --output-dir).*

## zomer-control--backtest-resultaat-v1.md

# Backtest-resultaat v1 - voorspellingsmodel

**Datum**: 2026-08-16 21:39
**Periode**: 2026-06-18 t/m 2026-08-16
**Databron**: ENTSO-E + Open-Meteo Archive + Yahoo Finance TTF=F
**Datapunten**: 5280
**Instellingen**: `--days 60 --horizons 1,3,5,7 --source entsoe --end-date 2026-08-16 --bias-mode off --output-dir out`

Dit rapport is automatisch gegenereerd door `02-code/scripts/backtest.py`.
Het evalueert het 6-puntenmodel uit `forecast.py` retrospectief.

## Beslissingscriteria (uit methodologie sectie 8.4)

- MAE op 1-3 dagen vooruit moet beter zijn dan de naieve baseline (alleen 7d-gemiddelde, geen factoren). Anders dragen de factoren niets bij.
- Bias dicht bij nul. Structurele afwijking duidt op verkeerde drempels.
- Hit-rate goedkoop/duur > 65% = klaar voor live; < 55% = terug naar tekentafel.

## Samenvatting per horizon

| Horizon | n | MAE (EUR/MWh) | Bias | Naieve MAE | Verbetering | Goedkoop hit-rate | Duur hit-rate | Richting-hit |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1d | 1392 | 27.05 | -8.90 | 25.57 | -5.8% | 84% (346) | 81% (755) | 45% |
| 3d | 1344 | 29.67 | -9.24 | 27.62 | -7.4% | 78% (333) | 78% (738) | 42% |
| 5d | 1296 | 30.26 | -7.29 | 28.86 | -4.9% | 78% (320) | 80% (720) | 43% |
| 7d | 1248 | 28.98 | -6.53 | 28.01 | -3.5% | 79% (312) | 79% (689) | 46% |

**Lezen**: "Verbetering" is de relatieve daling van MAE t.o.v. de naieve baseline (alleen 7d-gemiddelde, geen factoren). Positief = factoren helpen. "Richting-hit" = % van uren waar het model de juiste *richting* van afwijking t.o.v. baseline aangaf (niet de magnitude).

## Categorisatie-drempels (uit config.json)

- Goedkoop: < EUR 72.42/MWh
- Normaal: EUR 72.42 - EUR 122.0/MWh
- Duur: > EUR 122.0/MWh

## Conclusie

- **Factoren verslechteren** op horizon 1d: MAE is 5.8% hoger dan naieve baseline. Drempels of gewichten herzien.
- **Bias** op 1d (-8.90 EUR/MWh) wijst op systematische over-/onderschatting.
- **Hit-rate** goedkoop/duur is op 1d >=65% - klaar voor live overweging.
- **Schaalverloop** MAE 7d/1d = 1.07x - verwacht is een factor 1,5-2,5.

## Gebruiksgerichte metrics (v1.7)

### Rank accuracy (Spearman ρ)

Gemiddelde Spearman ρ over 220 dag×horizon-combinaties: **0.947**
→ Model sorteert uren redelijk goed van goedkoop naar duur.

### Cheap-hour hit rate (goedkoopste 25% uren)

Van de werkelijk goedkoopste 6 uren per dag zit **93%** ook in de voorspelde goedkoopste 6 uren (n=1320 slots).
→ Bruikbaar voor slimme laadadvies-toepassingen.

### Negatieve prijs detectie

TP=203, FP=155, FN=174
Precision: **57%** | Recall: **54%**

## Regime-uitslag (v1.7)

| Regime | n | MAE (EUR/MWh) | Bias |
|:---|---:|---:|---:|
| Normaal | 4125 | 29.87 | -8.44 |
| Oversupply (hernieuwbaar) | 1155 | 25.72 | -6.57 |


## MAE per uurblok

| Uurblok | n | MAE (EUR/MWh) | Bias |
|:---|---:|---:|---:|
| 00-05u nacht | 1320 | 18.66 | -11.46 |
| 06-08u ochtend | 660 | 22.11 | -3.08 |
| 09-16u midden | 1760 | 29.19 | -3.71 |
| 17-18u vooravond | 440 | 36.94 | -8.52 |
| 19-21u avondpiek | 660 | 53.73 | -14.33 |
| 22-23u laat | 440 | 24.13 | -12.53 |

## Caveats

1. **Perfect-foresight weather**: deze backtest gebruikt de werkelijke gemeten weersgegevens op de targetdag, niet de voorspelde weersdata van het moment van forecast. Dit overschat de modelkwaliteit licht. In productie introduceert weervoorspellingsfout extra variantie. Voor de 7-dagen horizon kan dat substantieel zijn.
2. **Een locatie per weervariabele**: De Bilt voor zon en temperatuur, idem voor wind. De methodologie noemt drie windlocaties; voor een latere iteratie kan dat verfijnen.
3. **Seizoennorm zonneproductie**: hardgecodeerde 12-maand-tabel voor De Bilt klimatologie; ruwe interpolatie tussen maandgemiddelden.
4. **TTF**: een ticker (Yahoo TTF=F front-month), close-to-close. Weekend/feestdagen vullen we vooruit met laatst-bekende koers.
5. **Sample-modus**: bij gebruik van `--sample` is de evaluatie zelf-circulair (synthetische prijzen vs. dezelfde structuur) en zegt alleen iets over de mechanica, niets over voorspelkracht.

## Voorstellen op basis van metrics

- Bias structureel negatief: voorspellingen zitten te laag. Overweeg POINT_WEIGHT iets te verhogen of negatieve drempels strenger te maken.

---

*Ruwe datapunten: zie `03-data/backtest-results.json` (of de map opgegeven met --output-dir).*

## zomer-w05--backtest-resultaat-v1.md

# Backtest-resultaat v1 - voorspellingsmodel

**Datum**: 2026-08-16 21:39
**Periode**: 2026-06-18 t/m 2026-08-16
**Databron**: ENTSO-E + Open-Meteo Archive + Yahoo Finance TTF=F + niveauverschuiving (w=0.5, ratio=3.0, gap=40.0)
**Datapunten**: 5280
**Instellingen**: `--days 60 --horizons 1,3,5,7 --source entsoe --end-date 2026-08-16 --bias-mode off --level-shift --level-shift-weight 0.5 --output-dir out`

Dit rapport is automatisch gegenereerd door `02-code/scripts/backtest.py`.
Het evalueert het 6-puntenmodel uit `forecast.py` retrospectief.

## Beslissingscriteria (uit methodologie sectie 8.4)

- MAE op 1-3 dagen vooruit moet beter zijn dan de naieve baseline (alleen 7d-gemiddelde, geen factoren). Anders dragen de factoren niets bij.
- Bias dicht bij nul. Structurele afwijking duidt op verkeerde drempels.
- Hit-rate goedkoop/duur > 65% = klaar voor live; < 55% = terug naar tekentafel.

## Samenvatting per horizon

| Horizon | n | MAE (EUR/MWh) | Bias | Naieve MAE | Verbetering | Goedkoop hit-rate | Duur hit-rate | Richting-hit |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1d | 1392 | 26.58 | -8.85 | 25.13 | -5.8% | 86% (346) | 81% (755) | 45% |
| 3d | 1344 | 29.38 | -9.32 | 27.35 | -7.4% | 80% (333) | 78% (738) | 42% |
| 5d | 1296 | 29.93 | -7.10 | 28.45 | -5.2% | 80% (320) | 80% (720) | 42% |
| 7d | 1248 | 28.96 | -5.89 | 28.02 | -3.4% | 79% (312) | 79% (689) | 46% |

**Lezen**: "Verbetering" is de relatieve daling van MAE t.o.v. de naieve baseline (alleen 7d-gemiddelde, geen factoren). Positief = factoren helpen. "Richting-hit" = % van uren waar het model de juiste *richting* van afwijking t.o.v. baseline aangaf (niet de magnitude).

## Categorisatie-drempels (uit config.json)

- Goedkoop: < EUR 72.42/MWh
- Normaal: EUR 72.42 - EUR 122.0/MWh
- Duur: > EUR 122.0/MWh

## Conclusie

- **Factoren verslechteren** op horizon 1d: MAE is 5.8% hoger dan naieve baseline. Drempels of gewichten herzien.
- **Bias** op 1d (-8.85 EUR/MWh) wijst op systematische over-/onderschatting.
- **Hit-rate** goedkoop/duur is op 1d >=65% - klaar voor live overweging.
- **Schaalverloop** MAE 7d/1d = 1.09x - verwacht is een factor 1,5-2,5.

## Gebruiksgerichte metrics (v1.7)

### Rank accuracy (Spearman ρ)

Gemiddelde Spearman ρ over 220 dag×horizon-combinaties: **0.948**
→ Model sorteert uren redelijk goed van goedkoop naar duur.

### Cheap-hour hit rate (goedkoopste 25% uren)

Van de werkelijk goedkoopste 6 uren per dag zit **93%** ook in de voorspelde goedkoopste 6 uren (n=1320 slots).
→ Bruikbaar voor slimme laadadvies-toepassingen.

### Negatieve prijs detectie

TP=203, FP=150, FN=174
Precision: **57%** | Recall: **54%**

## Regime-uitslag (v1.7)

| Regime | n | MAE (EUR/MWh) | Bias |
|:---|---:|---:|---:|
| Normaal | 4125 | 29.50 | -8.14 |
| Oversupply (hernieuwbaar) | 1155 | 25.74 | -6.78 |


## MAE per uurblok

| Uurblok | n | MAE (EUR/MWh) | Bias |
|:---|---:|---:|---:|
| 00-05u nacht | 1320 | 18.61 | -11.41 |
| 06-08u ochtend | 660 | 21.50 | -2.50 |
| 09-16u midden | 1760 | 28.66 | -3.44 |
| 17-18u vooravond | 440 | 36.46 | -8.54 |
| 19-21u avondpiek | 660 | 53.89 | -14.17 |
| 22-23u laat | 440 | 24.13 | -12.53 |

## Caveats

1. **Perfect-foresight weather**: deze backtest gebruikt de werkelijke gemeten weersgegevens op de targetdag, niet de voorspelde weersdata van het moment van forecast. Dit overschat de modelkwaliteit licht. In productie introduceert weervoorspellingsfout extra variantie. Voor de 7-dagen horizon kan dat substantieel zijn.
2. **Een locatie per weervariabele**: De Bilt voor zon en temperatuur, idem voor wind. De methodologie noemt drie windlocaties; voor een latere iteratie kan dat verfijnen.
3. **Seizoennorm zonneproductie**: hardgecodeerde 12-maand-tabel voor De Bilt klimatologie; ruwe interpolatie tussen maandgemiddelden.
4. **TTF**: een ticker (Yahoo TTF=F front-month), close-to-close. Weekend/feestdagen vullen we vooruit met laatst-bekende koers.
5. **Sample-modus**: bij gebruik van `--sample` is de evaluatie zelf-circulair (synthetische prijzen vs. dezelfde structuur) en zegt alleen iets over de mechanica, niets over voorspelkracht.

## Voorstellen op basis van metrics

- Bias structureel negatief: voorspellingen zitten te laag. Overweeg POINT_WEIGHT iets te verhogen of negatieve drempels strenger te maken.

---

*Ruwe datapunten: zie `03-data/backtest-results.json` (of de map opgegeven met --output-dir).*

## zomer-w10--backtest-resultaat-v1.md

# Backtest-resultaat v1 - voorspellingsmodel

**Datum**: 2026-08-16 21:39
**Periode**: 2026-06-18 t/m 2026-08-16
**Databron**: ENTSO-E + Open-Meteo Archive + Yahoo Finance TTF=F + niveauverschuiving (w=1.0, ratio=3.0, gap=40.0)
**Datapunten**: 5280
**Instellingen**: `--days 60 --horizons 1,3,5,7 --source entsoe --end-date 2026-08-16 --bias-mode off --level-shift --level-shift-weight 1.0 --output-dir out`

Dit rapport is automatisch gegenereerd door `02-code/scripts/backtest.py`.
Het evalueert het 6-puntenmodel uit `forecast.py` retrospectief.

## Beslissingscriteria (uit methodologie sectie 8.4)

- MAE op 1-3 dagen vooruit moet beter zijn dan de naieve baseline (alleen 7d-gemiddelde, geen factoren). Anders dragen de factoren niets bij.
- Bias dicht bij nul. Structurele afwijking duidt op verkeerde drempels.
- Hit-rate goedkoop/duur > 65% = klaar voor live; < 55% = terug naar tekentafel.

## Samenvatting per horizon

| Horizon | n | MAE (EUR/MWh) | Bias | Naieve MAE | Verbetering | Goedkoop hit-rate | Duur hit-rate | Richting-hit |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1d | 1392 | 26.76 | -8.79 | 25.33 | -5.6% | 84% (346) | 81% (755) | 46% |
| 3d | 1344 | 29.71 | -9.40 | 27.71 | -7.2% | 76% (333) | 78% (738) | 42% |
| 5d | 1296 | 30.21 | -6.92 | 28.73 | -5.2% | 76% (320) | 79% (720) | 42% |
| 7d | 1248 | 29.34 | -5.26 | 28.41 | -3.3% | 76% (312) | 79% (689) | 45% |

**Lezen**: "Verbetering" is de relatieve daling van MAE t.o.v. de naieve baseline (alleen 7d-gemiddelde, geen factoren). Positief = factoren helpen. "Richting-hit" = % van uren waar het model de juiste *richting* van afwijking t.o.v. baseline aangaf (niet de magnitude).

## Categorisatie-drempels (uit config.json)

- Goedkoop: < EUR 72.42/MWh
- Normaal: EUR 72.42 - EUR 122.0/MWh
- Duur: > EUR 122.0/MWh

## Conclusie

- **Factoren verslechteren** op horizon 1d: MAE is 5.6% hoger dan naieve baseline. Drempels of gewichten herzien.
- **Bias** op 1d (-8.79 EUR/MWh) wijst op systematische over-/onderschatting.
- **Hit-rate** goedkoop/duur is op 1d >=65% - klaar voor live overweging.
- **Schaalverloop** MAE 7d/1d = 1.10x - verwacht is een factor 1,5-2,5.

## Gebruiksgerichte metrics (v1.7)

### Rank accuracy (Spearman ρ)

Gemiddelde Spearman ρ over 220 dag×horizon-combinaties: **0.944**
→ Model sorteert uren redelijk goed van goedkoop naar duur.

### Cheap-hour hit rate (goedkoopste 25% uren)

Van de werkelijk goedkoopste 6 uren per dag zit **92%** ook in de voorspelde goedkoopste 6 uren (n=1320 slots).
→ Bruikbaar voor slimme laadadvies-toepassingen.

### Negatieve prijs detectie

TP=206, FP=186, FN=171
Precision: **53%** | Recall: **55%**

## Regime-uitslag (v1.7)

| Regime | n | MAE (EUR/MWh) | Bias |
|:---|---:|---:|---:|
| Normaal | 4125 | 29.59 | -7.84 |
| Oversupply (hernieuwbaar) | 1155 | 26.72 | -6.99 |


## MAE per uurblok

| Uurblok | n | MAE (EUR/MWh) | Bias |
|:---|---:|---:|---:|
| 00-05u nacht | 1320 | 18.56 | -11.37 |
| 06-08u ochtend | 660 | 21.06 | -1.91 |
| 09-16u midden | 1760 | 29.53 | -3.18 |
| 17-18u vooravond | 440 | 36.74 | -8.57 |
| 19-21u avondpiek | 660 | 54.21 | -14.01 |
| 22-23u laat | 440 | 24.13 | -12.53 |

## Caveats

1. **Perfect-foresight weather**: deze backtest gebruikt de werkelijke gemeten weersgegevens op de targetdag, niet de voorspelde weersdata van het moment van forecast. Dit overschat de modelkwaliteit licht. In productie introduceert weervoorspellingsfout extra variantie. Voor de 7-dagen horizon kan dat substantieel zijn.
2. **Een locatie per weervariabele**: De Bilt voor zon en temperatuur, idem voor wind. De methodologie noemt drie windlocaties; voor een latere iteratie kan dat verfijnen.
3. **Seizoennorm zonneproductie**: hardgecodeerde 12-maand-tabel voor De Bilt klimatologie; ruwe interpolatie tussen maandgemiddelden.
4. **TTF**: een ticker (Yahoo TTF=F front-month), close-to-close. Weekend/feestdagen vullen we vooruit met laatst-bekende koers.
5. **Sample-modus**: bij gebruik van `--sample` is de evaluatie zelf-circulair (synthetische prijzen vs. dezelfde structuur) en zegt alleen iets over de mechanica, niets over voorspelkracht.

## Voorstellen op basis van metrics

- Bias structureel negatief: voorspellingen zitten te laag. Overweeg POINT_WEIGHT iets te verhogen of negatieve drempels strenger te maken.

---

*Ruwe datapunten: zie `03-data/backtest-results.json` (of de map opgegeven met --output-dir).*

