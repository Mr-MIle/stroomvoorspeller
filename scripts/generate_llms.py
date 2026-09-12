#!/usr/bin/env python3
"""
Schrijft public/llms.txt: een korte wegwijzer voor AI-assistenten.

Waarom: llms.txt is de conventie waarmee een site vertelt wat er te halen valt en
waar. Deze site is er bij uitstek geschikt voor, want de data staat open en de
methodologie is publiek. Een handgeschreven bestand loopt achter zodra er een
artikel bijkomt, dus de kennisbank-lijst komt uit de bestanden zelf.

Draait in generate-sitemap.yml, bij elke push naar main.

Gebruik:
    python scripts/generate_llms.py
"""

import html
import re
import sys
from pathlib import Path

BASIS = "https://stroomvoorspeller.nl"
ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"
UIT = PUBLIC / "llms.txt"

# Pagina's die geen inhoudelijke bestemming zijn.
OVERSLAAN = {"404.html", "uit/joulo.html", "embed/index.html"}


def titel_en_beschrijving(pad):
    h = pad.read_text(encoding="utf-8", errors="replace")
    t = re.search(r"<title>(.*?)</title>", h, re.S)
    d = re.search(
        r'<meta[^>]*?name=["\']description["\'][^>]*?content=(["\'])(.*?)\1', h, re.S
    )
    titel = html.unescape(t.group(1)).strip() if t else pad.stem
    # Alles na een pipe of em-dash is merknaam, niet de kern van de pagina.
    titel = re.split(r"\s+[|—]\s+", titel)[0].strip()
    beschrijving = html.unescape(d.group(2)).strip() if d else ""
    return titel, beschrijving


def url_van(pad):
    rel = pad.relative_to(PUBLIC).as_posix()
    rel = re.sub(r"(^|/)index\.html$", r"\1", rel)
    rel = re.sub(r"\.html$", "", rel)
    return f"{BASIS}/{rel}".rstrip("/") if rel else BASIS


def eerste_zin(tekst, maxlen=140):
    if not tekst:
        return ""
    zin = re.split(r"(?<=[.!?])\s", tekst)[0].strip()
    if len(zin) > maxlen:
        zin = zin[: maxlen - 1].rsplit(" ", 1)[0] + "…"
    return zin


def regel(pad):
    titel, beschrijving = titel_en_beschrijving(pad)
    kort = eerste_zin(beschrijving)
    return f"- [{titel}]({url_van(pad)})" + (f": {kort}" if kort else "")


def main():
    kennisbank = sorted(
        p for p in (PUBLIC / "kennisbank").glob("*.html") if p.name != "index.html"
    )

    delen = [
        "# Stroomvoorspeller.nl",
        "",
        "> Onafhankelijke Nederlandse day-ahead stroomprijzen (EPEX Spot via ENTSO-E)",
        "> en een eigen voorspelling 2 tot 7 dagen vooruit, elke dag getoetst aan de",
        "> werkelijke prijzen. Gemaakt voor huishoudens met een dynamisch energiecontract.",
        "",
        "Alle data is gratis en vrij te gebruiken, met bronvermelding naar",
        f"{BASIS}. De JSON-endpoints staan open (CORS: *) en hoeven niet",
        "gescraped te worden.",
        "",
        "## Hoe de prijs is opgebouwd",
        "",
        "All-in consumentenprijs = (kale EPEX-prijs + inkoopvergoeding van de leverancier",
        "+ energiebelasting) x 1,21 btw. Bedragen op de site staan in ct/kWh inclusief",
        "belasting, tenzij anders vermeld. Prijzen gelden voor de NL bidding zone en",
        "worden elke drie uur ververst; de prijzen voor morgen komen rond 14:00",
        "Nederlandse tijd beschikbaar.",
        "",
        "## Data",
        "",
        f"- [Prijzen per uur en per kwartier (JSON)]({BASIS}/data/prices.json): day-ahead prijzen in EUR/MWh, inclusief PT15M-kwartierdata.",
        f"- [Voorspelling 7 dagen vooruit (JSON)]({BASIS}/data/forecast.json): voorspelde prijs per uur met onzekerheidsband en de factoren erachter.",
        f"- [Home Assistant-feed (JSON)]({BASIS}/data/ha.json): kant-en-klaar per uur, zowel kale marktprijs als all-in prijs in EUR/kWh.",
        f"- [JSON Schema van ha.json]({BASIS}/data/ha.schema.json): formele beschrijving van bovenstaande feed.",
        f"- [Gemeten nauwkeurigheid (JSON)]({BASIS}/data/performance.json): MAE, trefzekerheid per horizon en de volledige foutverdeling.",
        f"- [Records (JSON)]({BASIS}/data/records.json): uiterste waarden en negatieve uren per jaar uit het volledige archief.",
        f"- [Aanbieders en tarieven (JSON)]({BASIS}/data/config.json): opslag en vaste kosten per leverancier, plus de belastingtarieven.",
        f"- [RSS-feed]({BASIS}/data/feed.xml): de uurprijzen van de afgelopen zeven dagen.",
        "",
        "## Prijzen en voorspelling",
        "",
        f"- [Stroomprijs nu, morgen en komende week]({BASIS}/): actuele prijs per uur, de goedkoopste vensters en de voorspelling.",
        f"- [Stroomprijs morgen]({BASIS}/morgen): alle 24 uren van morgen, met duiding waarom de prijs is zoals hij is.",
        f"- [Historische stroomprijzen]({BASIS}/historisch): terugkijken per dag, met archief vanaf 2015.",
        f"- [Records]({BASIS}/records): laagste en hoogste uurprijs ooit, goedkoopste en duurste dag, negatieve uren per jaar sinds 2015.",
        f"- [Hoe het voorspellingsmodel werkt]({BASIS}/over/voorspelling): de factoren, hun gewicht en de grenzen van het model.",
        f"- [Gemeten nauwkeurigheid]({BASIS}/over/performance): elke voorspelling achteraf getoetst aan de werkelijke ENTSO-E-prijzen.",
        "",
        "## Vergelijken en rekenen",
        "",
        f"- [Dynamische energieaanbieders vergeleken]({BASIS}/aanbieders): jaarkosten, opslag en vaste kosten per leverancier.",
        f"- [Thuisbatterij berekenen]({BASIS}/batterij-berekenen): opbrengst en terugverdientijd op echte uurprijzen.",
        f"- [Opbrengst van een echte thuisbatterij]({BASIS}/batterij): dag voor dag, uit een eigen installatie.",
        f"- [ERE-certificaten vergelijken]({BASIS}/ere-vergelijken): tien inboekers op netto opbrengst bij thuisladen.",
        "",
        "## Koppelingen",
        "",
        f"- [Integraties]({BASIS}/integraties): overzicht van widget, Home Assistant en open data.",
        f"- [Home Assistant]({BASIS}/home-assistant): RESTful sensor, template-sensoren en automatiseringen.",
        "",
        "## Kennisbank",
        "",
    ]
    delen += [regel(p) for p in kennisbank]
    delen += [
        "",
        "## Over de bron",
        "",
        "Prijzen komen van ENTSO-E Transparency (NL bidding zone), met energy-charts.info",
        "als achtervang wanneer ENTSO-E achterloopt. De voorspelling is een eigen model;",
        "de uitkomsten worden dagelijks gemeten en ongefilterd gepubliceerd, inclusief de",
        "uren waarin het misging. Bij een nieuwe modelversie begint die meting opnieuw.",
        "",
        "De site verkoopt geen energie en is niet gelieerd aan een leverancier.",
        "",
    ]

    tekst = "\n".join(delen)
    oud = UIT.read_text(encoding="utf-8") if UIT.exists() else None
    if oud == tekst:
        print("[generate_llms] llms.txt ongewijzigd")
        return 0
    UIT.write_text(tekst, encoding="utf-8")
    print(f"[generate_llms] llms.txt geschreven ({len(kennisbank)} kennisbank-artikelen, {len(tekst)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
