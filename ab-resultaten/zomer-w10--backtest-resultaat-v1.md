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
