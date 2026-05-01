"""
daily_post.py — Plaatst dagelijks een X-post (@stroomtarief) met de prijzen
van morgen + een zelfgegenereerde 1200×675 JPEG.

Gebruik:
    # Lokaal/CI testen — image + tekst, GEEN post:
    python scripts/daily_post.py --dry-run

    # Productie:
    X_API_KEY=... X_API_SECRET=... X_ACCESS_TOKEN=... X_ACCESS_SECRET=... \
        python scripts/daily_post.py

Skips automatisch als de morgen-prijzen nog niet in prices.json staan
(bv. als de cron per ongeluk vóór 13:00 CET draait, of ENTSO-E down was).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

# ---- Paden ----
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRICES_FILE = PROJECT_ROOT / "public" / "data" / "prices.json"
CONFIG_FILE = PROJECT_ROOT / "public" / "data" / "config.json"

# ---- Branding & X ----
BRAND = "stroomvoorspeller.nl"
HANDLE = "@stroomtarief"

# ---- Image ----
IMG_W, IMG_H = 1200, 675
COLOR_BRAND = (15, 108, 189)
COLOR_TEXT = (33, 41, 56)
COLOR_TEXT_SOFT = (96, 110, 128)
COLOR_CHEAP = (47, 158, 68)
COLOR_PRICEY = (201, 42, 42)
COLOR_CARD_BG = (247, 250, 252)
COLOR_CARD_BORDER = (222, 228, 235)

# Lettertype-paden — eerste hit wint. DejaVu is pre-installed op Ubuntu CI.
FONT_PATHS_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]
FONT_PATHS_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
]

NL_DAYS_LONG = {0: "maandag", 1: "dinsdag", 2: "woensdag", 3: "donderdag",
                4: "vrijdag", 5: "zaterdag", 6: "zondag"}
NL_DAYS_SHORT = {0: "ma", 1: "di", 2: "wo", 3: "do", 4: "vr", 5: "za", 6: "zo"}
NL_MONTHS = {1: "januari", 2: "februari", 3: "maart", 4: "april", 5: "mei",
             6: "juni", 7: "juli", 8: "augustus", 9: "september",
             10: "oktober", 11: "november", 12: "december"}
NL_MONTHS_SHORT = {1: "jan", 2: "feb", 3: "mrt", 4: "apr", 5: "mei", 6: "jun",
                   7: "jul", 8: "aug", 9: "sep", 10: "okt", 11: "nov", 12: "dec"}


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_now_local() -> datetime:
    """Huidige tijd in Europe/Amsterdam, naive datetime."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Amsterdam")).replace(tzinfo=None)
    except Exception:
        # Fallback voor systemen zonder tzdata: ruwe DST-benadering
        utc = datetime.now(timezone.utc).replace(tzinfo=None)
        # NL is UTC+1 (winter) of UTC+2 (zomer). DST = laatste zo maart t/m laatste zo oktober.
        y = utc.year
        m_last = datetime(y, 3, 31)
        while m_last.weekday() != 6:
            m_last -= timedelta(days=1)
        o_last = datetime(y, 10, 31)
        while o_last.weekday() != 6:
            o_last -= timedelta(days=1)
        is_summer = m_last <= utc < o_last
        return utc + timedelta(hours=2 if is_summer else 1)


def get_tomorrow_window(prices: list, now_local: datetime) -> list | None:
    """Return de prijzen voor 'morgen' in NL-datum, of None bij onvolledige data."""
    target = now_local.date() + timedelta(days=1)
    selected = [p for p in prices if datetime.fromisoformat(p["time"]).date() == target]
    if len(selected) < 20:
        return None
    return selected


def consumer_price_ct(eur_mwh: float, config: dict, supplier_id: str = "average") -> float:
    """All-in consumentenprijs in ct/kWh incl. btw, met de 'average' leverancier."""
    suppliers = config.get("suppliers", [])
    s = next((x for x in suppliers if x["id"] == supplier_id), suppliers[0])
    markup = float(s.get("markup_per_kwh", 0))
    t = config.get("taxes", {})
    eb = float(t.get("energiebelasting_per_kwh", 0))
    btw = float(t.get("btw_factor", 1))
    return ((eur_mwh / 1000.0) + markup + eb) * btw * 100.0


def compute_summary(window: list, config: dict) -> dict:
    rows = []
    for p in window:
        t = datetime.fromisoformat(p["time"])
        rows.append({
            "time": t,
            "hour": t.hour,
            "epex": float(p["price"]),
            "ct": consumer_price_ct(float(p["price"]), config),
        })
    avg = sum(r["ct"] for r in rows) / len(rows)
    return {
        "rows": rows,
        "date": rows[0]["time"].date(),
        "avg_ct": avg,
        "cheapest": min(rows, key=lambda r: r["ct"]),
        "dearest": max(rows, key=lambda r: r["ct"]),
        "n_negative_epex": sum(1 for r in rows if r["epex"] < 0),
    }


def compose_tweet(summary: dict) -> str:
    d = summary["date"]
    date_str = f"{NL_DAYS_SHORT[d.weekday()]} {d.day} {NL_MONTHS_SHORT[d.month]}"
    avg, cheap, dear = summary["avg_ct"], summary["cheapest"], summary["dearest"]
    n_neg = summary["n_negative_epex"]

    if n_neg >= 2:
        insight = f"🌞 {n_neg}u met negatieve EPEX-prijs — perfect voor wasmachine, EV laden"
        tags = "#stroomprijzen #zonnepanelen #EVrijden"
    elif avg < 10:
        insight = "💚 Lekker goedkope dag morgen"
        tags = "#stroomprijzen #dynamischcontract #energieprijzen"
    elif avg > 25:
        insight = f"💸 Plan zware apparaten op {cheap['hour']:02d}:00"
        tags = "#stroomprijzen #energieprijzen #dynamischcontract"
    else:
        spread_pct = max(0, round((dear["ct"] - cheap["ct"]) / max(dear["ct"], 1) * 100))
        insight = f"📊 Spreiding {spread_pct}% tussen goedkoopste en duurste uur — schakelen scheelt"
        tags = "#stroomprijzen #dynamischcontract #energieprijzen"

    return (
        f"⚡ Stroomprijs morgen ({date_str})\n\n"
        f"Gem: {avg:.1f} ct/kWh\n"
        f"Goedkoopst: {cheap['hour']:02d}u → {cheap['ct']:.1f} ct\n"
        f"Duurst: {dear['hour']:02d}u → {dear['ct']:.1f} ct\n"
        f"{insight}\n\n"
        f"→ {BRAND}\n\n"
        f"{tags}"
    )


def _try_font(paths: list, size: int):
    from PIL import ImageFont
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _render_chart(summary: dict) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    rows = summary["rows"]
    hours = [r["hour"] for r in rows]
    cents = [r["ct"] for r in rows]
    cheap_h, cheap_c = summary["cheapest"]["hour"], summary["cheapest"]["ct"]
    dear_h, dear_c = summary["dearest"]["hour"], summary["dearest"]["ct"]

    fig = Figure(figsize=(11, 3.6), dpi=100, facecolor="white")
    ax = fig.add_subplot(111)
    ax.plot(hours, cents, color="#0f6cbd", linewidth=2.5, zorder=3)
    y_low = min(cents) - 2
    ax.fill_between(hours, cents, y_low, color="#0f6cbd", alpha=0.10, zorder=2)
    ax.scatter([cheap_h], [cheap_c], color="#2f9e44", s=110, zorder=5, edgecolor="white", linewidth=1.5)
    ax.scatter([dear_h], [dear_c], color="#c92a2a", s=110, zorder=5, edgecolor="white", linewidth=1.5)
    ax.axhline(y=summary["avg_ct"], color="#7c8a99", linestyle="--", linewidth=1.2, alpha=0.7, zorder=1)
    ax.text(23.3, summary["avg_ct"], f" gem {summary['avg_ct']:.1f}", color="#7c8a99",
            fontsize=10, va="center", ha="left")

    ax.set_xlim(-0.5, 23.5)
    ax.set_xticks([0, 6, 12, 18, 23])
    ax.set_xticklabels(["00:00", "06:00", "12:00", "18:00", "23:00"])
    ax.tick_params(colors="#7c8a99", labelsize=11)
    ax.set_ylabel("ct/kWh incl. btw", color="#7c8a99", fontsize=11)
    ax.grid(True, alpha=0.18)
    for s in ax.spines.values():
        s.set_color("#dee4eb")
    fig.tight_layout(pad=2)

    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor="white", dpi=100, bbox_inches="tight")
    return buf.getvalue()


def generate_image(summary: dict, output_path: Path) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (IMG_W, IMG_H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    f_brand = _try_font(FONT_PATHS_BOLD, 34)
    f_handle = _try_font(FONT_PATHS_REGULAR, 22)
    f_title = _try_font(FONT_PATHS_BOLD, 30)
    f_card_label = _try_font(FONT_PATHS_REGULAR, 18)
    f_card_value = _try_font(FONT_PATHS_BOLD, 30)
    f_footer = _try_font(FONT_PATHS_REGULAR, 16)

    # Top brand bar
    draw.rectangle([(0, 0), (IMG_W, 70)], fill=COLOR_BRAND)
    draw.text((40, 18), BRAND, fill="white", font=f_brand)
    # rechts uitlijnen handle: meten
    handle_bbox = draw.textbbox((0, 0), HANDLE, font=f_handle)
    handle_w = handle_bbox[2] - handle_bbox[0]
    draw.text((IMG_W - 40 - handle_w, 24), HANDLE, fill="white", font=f_handle)

    # Title
    d = summary["date"]
    title = f"Stroomprijzen morgen — {NL_DAYS_LONG[d.weekday()]} {d.day} {NL_MONTHS[d.month]}"
    draw.text((40, 95), title, fill=COLOR_TEXT, font=f_title)

    # Chart paste
    chart_png = _render_chart(summary)
    chart_img = Image.open(BytesIO(chart_png))
    chart_max_w, chart_max_h = IMG_W - 80, 350
    chart_img.thumbnail((chart_max_w, chart_max_h), Image.LANCZOS)
    chart_x = (IMG_W - chart_img.width) // 2
    chart_y = 150
    if chart_img.mode == "RGBA":
        img.paste(chart_img, (chart_x, chart_y), mask=chart_img.split()[3])
    else:
        img.paste(chart_img, (chart_x, chart_y))

    # Stat cards
    cards_y = IMG_H - 165
    card_h = 100
    card_w = (IMG_W - 80 - 40) // 3  # 40px tussen kaarten
    cards = [
        ("GEMIDDELDE", f"{summary['avg_ct']:.1f} ct", COLOR_TEXT),
        (f"GOEDKOOPST {summary['cheapest']['hour']:02d}:00",
         f"{summary['cheapest']['ct']:.1f} ct", COLOR_CHEAP),
        (f"DUURST {summary['dearest']['hour']:02d}:00",
         f"{summary['dearest']['ct']:.1f} ct", COLOR_PRICEY),
    ]
    for i, (label, value, color) in enumerate(cards):
        x = 40 + i * (card_w + 20)
        draw.rectangle([(x, cards_y), (x + card_w, cards_y + card_h)],
                       fill=COLOR_CARD_BG, outline=COLOR_CARD_BORDER, width=1)
        draw.text((x + 18, cards_y + 16), label, fill=COLOR_TEXT_SOFT, font=f_card_label)
        draw.text((x + 18, cards_y + 50), value, fill=color, font=f_card_value)

    # Footer
    footer = "Day-ahead EPEX + energiebelasting + 21% btw, gem. leverancier. Bron: ENTSO-E."
    draw.text((40, IMG_H - 38), footer, fill=COLOR_TEXT_SOFT, font=f_footer)

    img.save(str(output_path), "JPEG", quality=88, optimize=True)


def post_tweet(text: str, image_path: Path) -> None:
    import tweepy

    creds = {
        "consumer_key": os.environ["X_API_KEY"],
        "consumer_secret": os.environ["X_API_SECRET"],
        "access_token": os.environ["X_ACCESS_TOKEN"],
        "access_token_secret": os.environ["X_ACCESS_SECRET"],
    }
    # v1.1 nog steeds nodig voor media-upload (tweepy/X-API quirk anno 2025)
    auth = tweepy.OAuth1UserHandler(
        creds["consumer_key"], creds["consumer_secret"],
        creds["access_token"], creds["access_token_secret"],
    )
    api_v1 = tweepy.API(auth)
    media = api_v1.media_upload(filename=str(image_path))

    client = tweepy.Client(**creds)
    response = client.create_tweet(text=text, media_ids=[media.media_id])
    tweet_id = response.data.get("id") if response and response.data else "?"
    print(f"OK tweet gepost: id={tweet_id}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Genereer image + tekst maar post NIET (lokaal testen).")
    p.add_argument("--output", default="/tmp/daily_post.jpg",
                   help="Output JPEG-pad. Default: /tmp/daily_post.jpg")
    p.add_argument("--target-date", default=None,
                   help="Override 'morgen'-datum (YYYY-MM-DD). Voor test of inhalen.")
    args = p.parse_args()

    config = load_json(CONFIG_FILE)
    payload = load_json(PRICES_FILE)
    prices = payload.get("prices", [])
    if not prices:
        print("ERROR: prices.json bevat geen 'prices'", file=sys.stderr)
        sys.exit(1)

    now_local = get_now_local()
    if args.target_date:
        from datetime import date as _date
        target = _date.fromisoformat(args.target_date)
        window = [p for p in prices if datetime.fromisoformat(p["time"]).date() == target]
        if len(window) < 20:
            print(f"SKIP: target-date {target} heeft maar {len(window)} prijzen.", file=sys.stderr)
            sys.exit(0)
    else:
        window = get_tomorrow_window(prices, now_local)
    if not window:
        print("SKIP: morgen-prijzen nog niet in prices.json. Niets gepost.", file=sys.stderr)
        sys.exit(0)

    summary = compute_summary(window, config)
    text = compose_tweet(summary)

    print(f"=== Tweet ({len(text)} chars) ===")
    print(text)
    print("=== /tweet ===\n")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    generate_image(summary, out)
    print(f"Image: {out} ({out.stat().st_size:,} bytes)")

    if args.dry_run:
        print("DRY RUN — niet gepost.")
        return

    post_tweet(text, out)


if __name__ == "__main__":
    main()
