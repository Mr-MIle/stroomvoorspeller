#!/usr/bin/env bash
# Genereert public/sitemap.xml dynamisch uit alle .html-bestanden in public/.
#
# - <loc>     = de <link rel="canonical"> van de pagina zelf (altijd in sync
#               met wat de pagina als canonieke URL opgeeft).
# - <lastmod> = datum van de laatste git-commit die het bestand wijzigde
#               (valt terug op vandaag als git-historie ontbreekt).
# - Overgeslagen: pagina's met <meta name="robots" content="noindex…"> en
#   maandpagina's /historisch/YYYY-MM ouder dan 12 maanden. Die blijven
#   bereikbaar via /historisch en de jaarpagina's, maar Google liet ze als
#   "Gevonden, niet geïndexeerd" liggen (GSC-analyse 2 nov 2026).
#
# changefreq/priority zijn weggelaten: Google negeert die velden; <lastmod>
# is het enige verversheidssignaal dat nog meetelt.
#
# Draaien:  bash scripts/generate-sitemap.sh

set -euo pipefail
cd "$(dirname "$0")/.."   # naar repo-root (script staat in scripts/)

OUT="public/sitemap.xml"
TODAY="$(date +%F)"
# Oudste maand (YYYY-MM) die nog in de sitemap mag: 12 maanden terug.
MIN_MONTH="$(date -d "$(date +%Y-%m-01) -12 months" +%Y-%m)"
TMP="$(mktemp)"

{
  echo '<?xml version="1.0" encoding="UTF-8"?>'
  echo '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
  while IFS= read -r f; do
    loc="$(grep -oP '<link rel="canonical" href="\K[^"]+' "$f" | head -1 || true)"
    [ -z "$loc" ] && continue
    grep -q '<meta name="robots" content="noindex' "$f" && continue
    month="$(basename "$f" .html)"
    if [[ "$f" == public/historisch/* && "$month" =~ ^[0-9]{4}-[0-9]{2}$ && "$month" < "$MIN_MONTH" ]]; then
      continue
    fi
    lastmod="$(git log -1 --format=%cs -- "$f" 2>/dev/null || true)"
    [ -z "$lastmod" ] && lastmod="$TODAY"
    printf '  <url>\n    <loc>%s</loc>\n    <lastmod>%s</lastmod>\n  </url>\n' "$loc" "$lastmod"
  done < <(find public -type f -name '*.html' | sort)
  echo '</urlset>'
} > "$TMP"

mv "$TMP" "$OUT"
echo "Sitemap geschreven: $OUT ($(grep -c '<loc>' "$OUT") URL's)"
