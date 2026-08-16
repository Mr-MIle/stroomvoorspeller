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
