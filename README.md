# stroomvoorspeller.nl

Eenvoudige Nederlandse website die day-ahead elektriciteitsprijzen toont en (later)
een eigen voorspelling geeft voor de komende 5–7 dagen.

## Hoe werkt het?

```
+---------------------+        +-----------------+        +-----------+
|  GitHub Actions     |  cron  |  fetch_prices.py|  JSON  |  /public  |
|  (elke 3 uur)       +------->+  (Python)        +------>+  data/    |
+---------------------+        +-----------------+        +-----+-----+
                                                                |
                                                                v
                                                      +---------+----------+
                                                      |  Vercel CDN        |
                                                      |  stroomvoorspeller |
                                                      +--------------------+
```

- Data komt van [ENTSO-E Transparency Platform](https://transparency.entsoe.eu).
- De website is volledig statisch (HTML + CSS + JS), gehost door Vercel.
- GitHub Actions draait elke 3 uur het update-script en commit nieuwe data terug.

## Lokaal draaien

```bash
# 1. Sample-data genereren (zonder API-key)
python3 scripts/fetch_prices.py

# 2. Met echte ENTSO-E data
ENTSOE_TOKEN="xxx" python3 scripts/fetch_prices.py

# 3. Lokaal serveren
cd public && python3 -m http.server 8080
# open http://localhost:8080
```

## Vercel deploy

1. Importeer deze repo in Vercel.
2. Framework preset: **Other**.
3. Output directory: `public` (staat al in `vercel.json`).
4. Deploy. Klaar.

## GitHub secret instellen

Settings → Secrets and variables → Actions → New repository secret:

- Naam: `ENTSOE_TOKEN`
- Waarde: jouw ENTSO-E Web API security token

## Mappenstructuur

```
.
├── public/                 # statische site (Vercel serveert dit)
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   └── data/
│       └── prices.json     # automatisch bijgewerkt
├── scripts/
│   └── fetch_prices.py     # haalt prijzen op
├── .github/workflows/
│   └── update-prices.yml   # cron-automatisering
├── vercel.json
└── README.md
```

## Roadmap

- [x] MVP: day-ahead prijzen + grafiek + 'beste momenten'
- [ ] Voorspellingsmodel 5–7 dagen vooruit
- [ ] Pagina met dynamische tariefaanbieders
- [ ] Uitleg-artikelen (zonnepanelen bij negatieve prijzen, slim laden EV, enz.)
- [ ] Niet-agressieve advertenties

## Disclaimer

Geen financieel of energieadvies. Prijzen en voorspellingen kunnen afwijken.
Beslissingen over je energiecontract neem je zelf.

## Licentie

(nog te bepalen — voor nu: alle rechten voorbehouden)
