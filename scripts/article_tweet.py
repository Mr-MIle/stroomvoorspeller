"""
article_tweet.py -- Post een X-bericht (@stroomtarief) als een nieuw kennisbank-artikel
verschijnt op stroomvoorspeller.nl.

Gebruik:
    python scripts/article_tweet.py public/kennisbank/terugleverbeperking.html --dry-run
    X_API_KEY=... python scripts/article_tweet.py public/kennisbank/terugleverbeperking.html
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SITE_BASE = "https://stroomvoorspeller.nl"
KENNISBANK_PREFIX = "kennisbank"
MAX_TWEET_CHARS = 280
TWITTER_URL_LENGTH = 23

CATEGORY_RULES = [
    (["zonnepanelen", "saldering", "teruglever", "solar"], "☀️", ["#zonnepanelen", "#dynamischcontract"]),
    (["batterij", "accu", "opslag", "thuisbatterij", "wateraccu"], "🔋", ["#thuisbatterij", "#dynamischcontract"]),
    (["negatief", "negatieve", "p1", "poort", "stroom", "prijs", "epex"], "⚡", ["#stroomprijzen", "#dynamischcontract"]),
]
FALLBACK_EMOJI = "⚡"
FALLBACK_HASHTAGS = ["#stroomprijzen", "#dynamischcontract"]

TITLE_SUFFIXES = [
    " — Stroomvoorspeller.nl",
    " | Stroomvoorspeller.nl",
    " - Stroomvoorspeller.nl",
]


def detect_category(slug):
    slug_lower = slug.lower()
    for keywords, emoji, hashtags in CATEGORY_RULES:
        if any(kw in slug_lower for kw in keywords):
            return emoji, hashtags
    return FALLBACK_EMOJI, FALLBACK_HASHTAGS


def extract_meta(html_path):
    text = html_path.read_text(encoding="utf-8", errors="replace")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    desc_match = re.search(
        r'<meta\s[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']',
        text, re.IGNORECASE | re.DOTALL,
    )
    if not desc_match:
        desc_match = re.search(
            r'<meta\s[^>]*content=["\'](.*?)["\'][^>]*name=["\']description["\']',
            text, re.IGNORECASE | re.DOTALL,
        )
    description = re.sub(r"\s+", " ", desc_match.group(1)).strip() if desc_match else ""
    return title, description


def build_tweet(title, slug, emoji, hashtags):
    url = f"{SITE_BASE}/{KENNISBANK_PREFIX}/{slug}"
    hashtag_str = " ".join(hashtags)
    prefix = f'{emoji} Nieuw artikel: "'
    fixed_chars = len(prefix) + 1 + 1 + TWITTER_URL_LENGTH + 1 + len(hashtag_str)
    title_budget = MAX_TWEET_CHARS - fixed_chars
    if len(title) > title_budget:
        title = title[: title_budget - 1].rstrip() + "…"
    return f'{prefix}{title}"\n{url}\n{hashtag_str}'


def post_tweet(text):
    import tweepy
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_SECRET"],
    )
    response = client.create_tweet(text=text)
    tweet_id = response.data.get("id") if response and response.data else "?"
    print(f"OK -- tweet gepost: id={tweet_id}")


def main():
    parser = argparse.ArgumentParser(description="Post artikel-tweet op @stroomtarief")
    parser.add_argument("html_file", help="Pad naar het nieuwe kennisbank-HTML-bestand")
    parser.add_argument("--dry-run", action="store_true", help="Print tweet zonder te posten")
    args = parser.parse_args()

    html_path = Path(args.html_file)
    if not html_path.exists():
        print(f"FOUT: bestand niet gevonden: {html_path}", file=sys.stderr)
        sys.exit(1)

    slug = html_path.stem
    if slug == "index":
        print("Overzichtspagina overgeslagen -- geen tweet nodig.")
        sys.exit(0)

    title, description = extract_meta(html_path)

    for ts in TITLE_SUFFIXES:
        if title.endswith(ts):
            title = title[: -len(ts)].rstrip()
            break

    if not title:
        print(f"WAARSCHUWING: geen title gevonden in {html_path}", file=sys.stderr)
        sys.exit(1)

    tweet_title = title if len(title) >= 10 else (description[:80] if description else title)
    emoji, hashtags = detect_category(slug)
    text = build_tweet(tweet_title, slug, emoji, hashtags)

    url = f"{SITE_BASE}/{KENNISBANK_PREFIX}/{slug}"
    displayed_chars = len(text) - len(url) + TWITTER_URL_LENGTH

    print("=" * 60)
    print(text)
    print("=" * 60)
    print(f"Tekens (URL als {TWITTER_URL_LENGTH}): {displayed_chars}")

    if args.dry_run:
        print("DRY RUN -- niet gepost.")
        return

    post_tweet(text)


if __name__ == "__main__":
    main()
