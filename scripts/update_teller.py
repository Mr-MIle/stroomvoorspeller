#!/usr/bin/env python3
"""Teller voor /aanbieders: telt paginaweergaven uit Cloudflare Web Analytics op.

Schrijft public/data/teller.json:
  { "pad": "/aanbieders", "sinds": "YYYY-MM-DD", "totaal": N,
    "bijgewerkt": "YYYY-MM-DD", "per_dag": {"YYYY-MM-DD": n, ...}, "bron": ... }

Cloudflare bewaart de gegevens maar een beperkte tijd. Daarom houdt dit
bestand zelf de dagtotalen bij: elke run haalt de laatste 7 dagen opnieuw op
(de lopende dag is dan nog niet compleet) en telt alles bij elkaar op. De
eerste run haalt zo ver terug als Cloudflare toestaat, in blokken van 30 dagen.

Nodig als GitHub-secrets: CF_API_TOKEN (Account Analytics: Read),
CF_ACCOUNT_ID en CF_SITE_TAG. Ontbreekt er een, dan stopt het script
zonder fout en blijft de teller op de pagina verborgen.
"""
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PAD = "/aanbieders"
PADEN = ["/aanbieders", "/aanbieders/", "/aanbieders.html"]
BESTAND = Path("public/data/teller.json")
ENDPOINT = "https://api.cloudflare.com/client/v4/graphql"
QUERY = """
query ($account: string!, $filter: AccountRumPageloadEventsAdaptiveGroupsFilter_InputObject) {
  viewer {
    accounts(filter: {accountTag: $account}) {
      rumPageloadEventsAdaptiveGroups(limit: 1000, filter: $filter) {
        count
        dimensions { date }
      }
    }
  }
}
"""


def haal_op(token, account, site, van, tot):
    """Paginaweergaven per dag tussen van (incl.) en tot (excl.)."""
    filt = {"AND": [
        {"datetime_geq": f"{van.isoformat()}T00:00:00Z", "datetime_lt": f"{tot.isoformat()}T00:00:00Z"},
        {"siteTag": site},
        {"requestPath_in": PADEN},
    ]}
    body = json.dumps({"query": QUERY, "variables": {"account": account, "filter": filt}}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if data.get("errors"):
        raise RuntimeError(data["errors"][0].get("message", "onbekende fout"))
    groepen = data["data"]["viewer"]["accounts"][0]["rumPageloadEventsAdaptiveGroups"]
    uit = {}
    for g in groepen:
        d = g["dimensions"]["date"]
        uit[d] = uit.get(d, 0) + int(g["count"])
    return uit


def main():
    token, account, site = (os.environ.get(k, "").strip() for k in ("CF_API_TOKEN", "CF_ACCOUNT_ID", "CF_SITE_TAG"))
    if not (token and account and site):
        print("[teller] CF_API_TOKEN, CF_ACCOUNT_ID of CF_SITE_TAG ontbreekt; niets gedaan.")
        return 0

    oud = {}
    if BESTAND.exists():
        oud = json.loads(BESTAND.read_text(encoding="utf-8"))
    per_dag = dict(oud.get("per_dag", {}))
    vandaag = datetime.now(timezone.utc).date()
    morgen = vandaag + timedelta(days=1)

    if per_dag:
        blokken = [(vandaag - timedelta(days=7), morgen)]
    else:
        # Eerste keer: terug in blokken van 30 dagen tot Cloudflare niets meer geeft.
        blokken = [(morgen - timedelta(days=30 * (i + 1)), morgen - timedelta(days=30 * i)) for i in range(12)]

    for van, tot in blokken:
        try:
            nieuw = haal_op(token, account, site, van, tot)
        except Exception as e:  # noqa: BLE001 - bij een oud blok is dit het einde van de bewaartermijn
            print(f"[teller] {van} t/m {tot}: {e}")
            if per_dag:
                break
            continue
        for d in [(van + timedelta(days=i)).isoformat() for i in range((tot - van).days)]:
            if d in nieuw:
                per_dag[d] = nieuw[d]
        print(f"[teller] {van} t/m {tot - timedelta(days=1)}: {sum(nieuw.values())} weergaven")

    per_dag = {d: n for d, n in sorted(per_dag.items()) if n > 0}
    if not per_dag:
        print("[teller] nog geen gegevens")
        return 0
    uit = {
        "pad": PAD,
        "sinds": min(per_dag),
        "totaal": sum(per_dag.values()),
        "bijgewerkt": vandaag.isoformat(),
        "bron": "Cloudflare Web Analytics, paginaweergaven (geen unieke personen)",
        "per_dag": per_dag,
    }
    if {k: v for k, v in uit.items() if k != "bijgewerkt"} == {k: v for k, v in oud.items() if k != "bijgewerkt"}:
        print("[teller] ongewijzigd")
        return 0
    BESTAND.write_text(json.dumps(uit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[teller] totaal {uit['totaal']} sinds {uit['sinds']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
