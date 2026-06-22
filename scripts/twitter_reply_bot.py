#!/usr/bin/env python3
"""
Stroomvoorspeller – Twitter Reply Bot
======================================
Semi-automatisch: vindt energie-tweets, genereert replies, vraagt goedkeuring,
dan pas posten. Veilig voor je account.

Vereisten:
  pip install tweepy python-dotenv

Zet je credentials in .env (zie .env.example).
"""

import os
import json
import time
import random
from datetime import datetime
from pathlib import Path

import tweepy
from dotenv import load_dotenv

load_dotenv()

# ── Configuratie ──────────────────────────────────────────────────────────────

BEARER_TOKEN        = os.getenv("TWITTER_BEARER_TOKEN")
CONSUMER_KEY        = os.getenv("TWITTER_CONSUMER_KEY")
CONSUMER_SECRET     = os.getenv("TWITTER_CONSUMER_SECRET")
ACCESS_TOKEN        = os.getenv("TWITTER_ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

SITE_URL      = "https://stroomvoorspeller.nl"
REPLIED_FILE  = "replied_tweets.json"
MIN_FOLLOWERS = 500   # filter kleine accounts weg
MAX_PER_RUN   = 20    # maximaal te reviewen tweets per run

# ── Zoekqueries per categorie ─────────────────────────────────────────────────

SEARCH_QUERIES: dict[str, list[str]] = {
    "energieprijzen": [
        'stroomprijs lang:nl -is:retweet -is:reply',
        'energieprijs morgen lang:nl -is:retweet -is:reply',
        'dag-voor-dag stroom lang:nl -is:retweet -is:reply',
        'dynamisch energiecontract lang:nl -is:retweet -is:reply',
        'APX prijs stroom lang:nl -is:retweet -is:reply',
    ],
    "leveranciers": [
        '"Frank Energie" lang:nl -is:retweet -is:reply',
        'Tibber stroom lang:nl -is:retweet -is:reply',
        '"ANWB Energie" dynamisch lang:nl -is:retweet -is:reply',
        'dynamische energieleverancier lang:nl -is:retweet -is:reply',
    ],
    "nieuws": [
        'stroomprijzen stijgen lang:nl -is:retweet -is:reply',
        'stroomprijzen dalen lang:nl -is:retweet -is:reply',
        'energiemarkt Nederland lang:nl -is:retweet -is:reply',
        'elektriciteitsprijs lang:nl -is:retweet -is:reply',
    ],
}

# ── Reply templates per categorie ─────────────────────────────────────────────
# {url} wordt vervangen door SITE_URL

REPLY_TEMPLATES: dict[str, list[str]] = {
    "energieprijzen": [
        "Benieuwd wat de stroomprijs morgen doet? We maken dagelijkse voorspellingen → {url} ⚡",
        "Handig: uur-voor-uur stroomprijs voorspellingen voor morgen staan op {url}",
        "Voor actuele en verwachte stroomprijzen in NL: {url} — dag-voor-dag inzicht 🔌",
        "Wil je weten wanneer stroom morgen het goedkoopst is? → {url}",
    ],
    "leveranciers": [
        "Met een dynamisch contract is timing alles. Morgens tarieven voorspeld op {url} ⚡",
        "Slim verbruiken op goedkope uren? Bekijk de dagprijs voorspellingen op {url} 🔌",
        "Voor wie dynamisch afrekent: uur-voor-uur stroomprijs voorspellingen → {url}",
        "Handig bij dynamisch contract: dagelijkse stroomprijs outlook op {url}",
    ],
    "nieuws": [
        "Meer context: dagelijkse stroomprijs voorspellingen voor NL op {url} ⚡",
        "Wil je dagprijzen en de verwachte richting zien? → {url}",
        "Voor context bij dit nieuws: {url} — Nederlandse stroomprijs voorspellingen per dag 🔌",
        "Bijhouden wat stroom kost (en gaat kosten)? {url} doet dagelijkse NL voorspellingen",
    ],
}

# ── Hulpfuncties ──────────────────────────────────────────────────────────────

def load_replied() -> set:
    if Path(REPLIED_FILE).exists():
        with open(REPLIED_FILE) as f:
            return set(json.load(f))
    return set()


def save_replied(replied: set) -> None:
    with open(REPLIED_FILE, "w") as f:
        json.dump(sorted(replied), f, indent=2)


def generate_reply(category: str) -> str:
    templates = REPLY_TEMPLATES.get(category, REPLY_TEMPLATES["nieuws"])
    return random.choice(templates).format(url=SITE_URL)


def search_tweets(client: tweepy.Client, query: str, max_results: int = 10):
    try:
        response = client.search_recent_tweets(
            query=query,
            max_results=max_results,
            tweet_fields=["author_id", "created_at", "public_metrics", "text"],
            expansions=["author_id"],
            user_fields=["username", "public_metrics"],
        )
        return response
    except tweepy.TooManyRequests:
        print("⏳  Rate limit bereikt — 60 seconden wachten...")
        time.sleep(60)
        return None
    except Exception as e:
        print(f"❌  Zoekfout: {e}")
        return None


def show_tweet(tweet, users_by_id: dict) -> None:
    author   = users_by_id.get(str(tweet.author_id), {})
    username  = author.get("username", "onbekend")
    followers = author.get("public_metrics", {}).get("followers_count", 0)
    likes     = (tweet.public_metrics or {}).get("like_count", 0)
    retweets  = (tweet.public_metrics or {}).get("retweet_count", 0)

    print(f"\n{'─'*62}")
    print(f"👤  @{username}  ({followers:,} volgers)")
    preview = tweet.text[:220] + ("…" if len(tweet.text) > 220 else "")
    print(f"💬  {preview}")
    print(f"    ❤️  {likes}  🔁 {retweets}")
    print(f"    🔗 https://twitter.com/i/web/status/{tweet.id}")


# ── Interactieve review queue ─────────────────────────────────────────────────

def review_queue(
    tweets_with_cats: list,
    users_by_id: dict,
    replied: set,
) -> list[tuple[str, str]]:
    """Toon elke tweet, vraag goedkeuring. Retourneert lijst van (tweet_id, reply_text)."""
    approved = []
    total    = len(tweets_with_cats)

    for i, (tweet, category) in enumerate(tweets_with_cats, 1):
        if str(tweet.id) in replied:
            continue

        reply_text = generate_reply(category)
        show_tweet(tweet, users_by_id)
        print(f"\n📝  Reply [{i}/{total}]:")
        print(f"    {reply_text}")
        print("\n    [j] Plaatsen   [e] Bewerken   [s] Overslaan   [q] Stoppen")

        while True:
            keuze = input("    → ").strip().lower()
            if keuze == "j":
                approved.append((str(tweet.id), reply_text))
                print("    ✅ Goedgekeurd")
                break
            elif keuze == "e":
                nieuwe_tekst = input("    Nieuwe reply: ").strip()
                if nieuwe_tekst:
                    approved.append((str(tweet.id), nieuwe_tekst))
                    print("    ✅ Aangepaste reply goedgekeurd")
                break
            elif keuze == "s":
                replied.add(str(tweet.id))   # ook skip opslaan zodat hij niet opnieuw verschijnt
                print("    ⏭️  Overgeslagen")
                break
            elif keuze == "q":
                print("\n🛑  Gestopt met reviewen.")
                return approved
            else:
                print("    Kies j / e / s / q")

    return approved


# ── Posten ────────────────────────────────────────────────────────────────────

def post_replies(
    client: tweepy.Client,
    approved: list[tuple[str, str]],
    replied: set,
) -> int:
    posted = 0
    for tweet_id, reply_text in approved:
        try:
            client.create_tweet(text=reply_text, in_reply_to_tweet_id=tweet_id)
            replied.add(tweet_id)
            save_replied(replied)
            posted += 1
            print(f"✅  Reply geplaatst ({posted}/{len(approved)})")
        except tweepy.Forbidden as e:
            print(f"❌  Verboden: {e}")
        except tweepy.TooManyRequests:
            print("⏳  Rate limit — 5 minuten wachten...")
            time.sleep(300)
        except Exception as e:
            print(f"❌  Fout bij posten: {e}")

        if posted < len(approved):
            wacht = random.randint(45, 120)
            print(f"⏳  Wachten {wacht}s voor volgende reply…")
            time.sleep(wacht)

    return posted


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("⚡ Stroomvoorspeller Twitter Reply Bot")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # Valideer credentials
    missing = [k for k, v in {
        "BEARER_TOKEN": BEARER_TOKEN,
        "CONSUMER_KEY": CONSUMER_KEY,
        "CONSUMER_SECRET": CONSUMER_SECRET,
        "ACCESS_TOKEN": ACCESS_TOKEN,
        "ACCESS_TOKEN_SECRET": ACCESS_TOKEN_SECRET,
    }.items() if not v]
    if missing:
        print(f"❌  Ontbrekende .env variabelen: {', '.join(missing)}")
        print("   Zie .env.example voor instructies.")
        return

    client_search = tweepy.Client(bearer_token=BEARER_TOKEN)
    client_post   = tweepy.Client(
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_TOKEN_SECRET,
    )

    replied      = load_replied()
    users_by_id: dict = {}
    all_tweets:  list = []

    print(f"📋  {len(replied)} tweets al eerder behandeld\n")

    # ── Zoeken ────────────────────────────────────────────────────────────────
    for category, queries in SEARCH_QUERIES.items():
        print(f"🔍  Zoeken: {category}…")
        for query in queries:
            response = search_tweets(client_search, query, max_results=10)
            if not response or not response.data:
                time.sleep(1)
                continue

            if response.includes and "users" in response.includes:
                for user in response.includes["users"]:
                    users_by_id[str(user.id)] = {
                        "username": user.username,
                        "public_metrics": user.public_metrics,
                    }

            for tweet in response.data:
                if str(tweet.id) in replied:
                    continue
                author    = users_by_id.get(str(tweet.author_id), {})
                followers = (author.get("public_metrics") or {}).get("followers_count", 0)
                if followers >= MIN_FOLLOWERS:
                    all_tweets.append((tweet, category))

            time.sleep(2)   # API pacing

    # Dedupliceer + sorteer op likes
    seen: set = set()
    unique: list = []
    for t, c in all_tweets:
        if str(t.id) not in seen:
            seen.add(str(t.id))
            unique.append((t, c))

    unique.sort(
        key=lambda x: (x[0].public_metrics or {}).get("like_count", 0),
        reverse=True,
    )
    unique = unique[:MAX_PER_RUN]

    print(f"\n📬  {len(unique)} nieuwe tweets gevonden (≥{MIN_FOLLOWERS} volgers)\n")
    if not unique:
        print("Geen nieuwe tweets gevonden. Probeer later opnieuw.")
        return

    # ── Review ────────────────────────────────────────────────────────────────
    approved = review_queue(unique, users_by_id, replied)

    if not approved:
        save_replied(replied)   # sla ook de skips op
        print("\n👋  Geen replies goedgekeurd. Tot de volgende run!")
        return

    # ── Posten ────────────────────────────────────────────────────────────────
    print(f"\n📤  {len(approved)} replies worden geplaatst…")
    posted = post_replies(client_post, approved, replied)
    save_replied(replied)
    print(f"\n✅  Klaar! {posted}/{len(approved)} replies geplaatst.")


if __name__ == "__main__":
    main()
