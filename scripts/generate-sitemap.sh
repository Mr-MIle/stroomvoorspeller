#!/usr/bin/env bash
# Genereert public/sitemap.xml dynamisch uit alle .html-bestanden in public/.
#
# - <loc>     = de <link rel="canonical"> van de pagina zelf (altijd in sync
#               met wat de pagina als canonieke URL opgeeft).
# - <lastmod> = datum van de laatste git-commit die het bestand wijzigde
#               (valt terug op vandaag als git-historie ontbreekt).
#
# changefreq/priority zijn weggelaten: Google negeert die velden; <lastmod>
# is het enige verversheidssignaal dat nog meetelt.
#
# Draaien:  bash scripts/generate-sitemap.sh

set -euo pipefail
cd "$(dirname "$0")/.."   # naar repo-root (script staat in scripts/)

OUT="public/sitemap.xml"
TODAY="$(date +%F)"
TMP="$(mktemp)"

{
  echo '<?xml version="1.0" encoding="UTF-8"?>'
  echo '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
  while IFS= read -r f; do
    loc="$(grep -oP '<link rel="canonical" href="\K[^"]+' "$f" | head -1 || true)"
    [ -z "$loc" ] && continue
    lastmod="$(git log -1 --format=%cs -- "$f" 2>/dev/null || true)"
    [ -z "$lastmod" ] && lastmod="$TODAY"
    printf '  <url>\n    <loc>%s</loc>\n    <lastmod>%s</lastmod>\n  </url>\n' "$loc" "$lastmod"
  done < <(find public -type f -name '*.html' | sort)
  echo '</urlset>'
} > "$TMP"

mv "$TMP" "$OUT"
echo "Sitemap geschreven: $OUT ($(grep -c '<loc>' "$OUT") URL's)"
