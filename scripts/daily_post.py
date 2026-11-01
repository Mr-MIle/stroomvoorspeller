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
(bv. als de cron per ongeluk vóór 14:00 CET draait, of ENTSO-E down was).
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
# Markeert voor welke morgen-datum al een tweet is gepost. Voorkomt dat een tweede
# run (bv. omdat prices.json opnieuw is overschreven) nogmaals tweet. De workflow
# committeert dit bestand terug naar de repo, zodat de markering blijft bestaan.
TWEET_STATE_FILE = PROJECT_ROOT / "public" / "data" / "tweet_state.json"

# ---- Branding & X ----
BRAND = "stroomvoorspeller.nl"
HANDLE = "@stroomtarief"

# ---- Image ----
IMG_W, IMG_H = 1200, 675
# Huisstijl (logo G, nov 2026): inkt, zon, mint = goedkoop, koraal = duur.
# Zie public/brand/ voor het logo zelf.
COLOR_INKT       = (29, 43, 83)      # #1D2B53 merkbalk, tekst, prijslijn
COLOR_ZON        = (255, 200, 61)    # #FFC83D bliksemlijn in het teken
COLOR_MINT       = (43, 196, 138)    # #2BC48A groene stip, goedkoopste uur
COLOR_KORAAL     = (255, 107, 91)    # #FF6B5B duurste uur
COLOR_TLD        = (138, 147, 176)   # #8A93B0 ".nl" in het woordmerk
COLOR_HANDLE     = (163, 170, 196)   # handle op de inktbalk
COLOR_TEXT       = COLOR_INKT
COLOR_TEXT_SOFT  = (94, 103, 135)    # #5E6787
# Donkerdere tinten van mint/koraal voor cijfers op licht vlak (leesbaarheid)
COLOR_CHEAP      = (29, 140, 99)     # #1D8C63
COLOR_PRICEY     = (214, 69, 54)     # #D64536
COLOR_CARD_BG    = (247, 245, 239)
COLOR_CARD_BORDER = (228, 225, 216)

# Lettertypen: Manrope uit scripts/fonts (OFL), DejaVu als terugval.
FONTS_DIR = Path(__file__).resolve().parent / "fonts"
FONT_PATHS_BOLD = [
    str(FONTS_DIR / "Manrope-ExtraBold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
]
FONT_PATHS_REGULAR = [
    str(FONTS_DIR / "Manrope-Medium.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "DejaVuSans.ttf",
]

# Logo-teken in een 64x64-raster (gelijk aan public/brand/mark.svg)
MARK_LINE = [(11, 43), (24, 23), (32, 36), (43, 16)]
MARK_LINE_W = 6
MARK_DOTS = [(46.2, 24.8, 2.1), (48.8, 32.2, 2.1)]
MARK_GO = (52, 42, 5)

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
        f"Gem: {fmt_ct(avg)} ct/kWh\n"
        f"Goedkoopst: {cheap['hour']:02d}u → {fmt_ct(cheap['ct'])} ct\n"
        f"Duurst: {dear['hour']:02d}u → {fmt_ct(dear['ct'])} ct\n"
        f"{insight}\n\n"
        f"→ stroomvoorspeller.nl/morgen\n\n"
        f"{tags}"
    )


def fmt_ct(ct: float) -> str:
    """Formateer ct-waarde in NL-notatie, bijv. '3,2' of '-12,5'."""
    return f"{ct:.1f}".replace(".", ",")


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
    import matplotlib.ticker
    from matplotlib import font_manager
    from matplotlib.figure import Figure

    rc = {"font.family": "DejaVu Sans"}
    manrope = FONTS_DIR / "Manrope-Medium.ttf"
    if manrope.exists():
        font_manager.fontManager.addfont(str(manrope))
        rc = {"font.family": font_manager.FontProperties(fname=str(manrope)).get_name(),
              "font.weight": "medium"}

    rows = summary["rows"]
    hours = [r["hour"] for r in rows]
    cents = [r["ct"] for r in rows]
    cheap_h, cheap_c = summary["cheapest"]["hour"], summary["cheapest"]["ct"]
    dear_h, dear_c = summary["dearest"]["hour"], summary["dearest"]["ct"]

    with matplotlib.rc_context(rc):
        fig = Figure(figsize=(11, 3.6), dpi=100, facecolor="white")
        ax = fig.add_subplot(111)
        ax.plot(hours, cents, color="#1D2B53", linewidth=2.8, zorder=3,
                solid_capstyle="round", solid_joinstyle="round")
        y_low = min(cents) - 2
        ax.fill_between(hours, cents, y_low, color="#1D2B53", alpha=0.06, zorder=2)
        ax.scatter([cheap_h], [cheap_c], color="#2BC48A", s=150, zorder=5, edgecolor="white", linewidth=2)
        ax.scatter([dear_h], [dear_c], color="#FF6B5B", s=150, zorder=5, edgecolor="white", linewidth=2)
        ax.axhline(y=summary["avg_ct"], color="#8A93B0", linestyle=(0, (2, 4)), linewidth=1.4, zorder=1)
        ax.text(23.3, summary["avg_ct"], f" gem {fmt_ct(summary['avg_ct'])}", color="#5E6787",
                fontsize=10, va="center", ha="left")

        ax.set_xlim(-0.5, 23.5)
        ax.set_xticks([0, 6, 12, 18, 23])
        ax.set_xticklabels(["00:00", "06:00", "12:00", "18:00", "23:00"])
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(
            lambda v, _: f"{v:.0f}" if v == int(v) else f"{v:.1f}".replace(".", ",")))
        ax.tick_params(colors="#5E6787", labelsize=11, length=0)
        ax.set_ylabel("ct/kWh incl. btw", color="#5E6787", fontsize=11)
        ax.grid(True, axis="y", color="#E4E1D8", linewidth=0.8)
        for name, s in ax.spines.items():
            s.set_visible(name == "bottom")
            s.set_color("#E4E1D8")
        fig.tight_layout(pad=2)

        buf = BytesIO()
        fig.savefig(buf, format="png", facecolor="white", dpi=100, bbox_inches="tight")
    return buf.getvalue()


def _draw_mark(size: int, bg=None):
    """Teken het logo (lijn op donker) als RGBA-plaatje, 4x supersampled."""
    from PIL import Image, ImageDraw

    ss = 4
    k = size * ss / 64
    im = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if bg is not None:
        d.rounded_rectangle([(0, 0), (size * ss - 1, size * ss - 1)], radius=int(16 * k), fill=bg)
    pts = [(x * k, y * k) for x, y in MARK_LINE]
    w = int(MARK_LINE_W * k)
    d.line(pts, fill=COLOR_ZON, width=w, joint="curve")
    for x, y in pts[:1] + pts[-1:]:
        d.ellipse([(x - w / 2, y - w / 2), (x + w / 2, y + w / 2)], fill=COLOR_ZON)
    for x, y, r in MARK_DOTS:
        d.ellipse([((x - r) * k, (y - r) * k), ((x + r) * k, (y + r) * k)], fill=COLOR_ZON)
    x, y, r = MARK_GO
    d.ellipse([((x - r) * k, (y - r) * k), ((x + r) * k, (y + r) * k)], fill=COLOR_MINT)
    return im.resize((size, size), Image.LANCZOS)


def generate_image(summary: dict, output_path: Path) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (IMG_W, IMG_H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    f_brand = _try_font(FONT_PATHS_BOLD, 32)
    f_handle = _try_font(FONT_PATHS_REGULAR, 21)
    f_title = _try_font(FONT_PATHS_BOLD, 30)
    f_card_label = _try_font(FONT_PATHS_REGULAR, 18)
    f_card_value = _try_font(FONT_PATHS_BOLD, 32)
    f_footer = _try_font(FONT_PATHS_REGULAR, 16)

    # Merkbalk: inkt, met het teken (lijn op donker) en het woordmerk
    bar_h = 76
    draw.rectangle([(0, 0), (IMG_W, bar_h)], fill=COLOR_INKT)
    mark = _draw_mark(52)
    img.paste(mark, (34, (bar_h - 52) // 2), mask=mark)
    name, tld = BRAND.split(".", 1)
    x_name = 96
    draw.text((x_name, bar_h // 2), name, fill=(255, 255, 255), font=f_brand, anchor="lm")
    name_w = draw.textlength(name, font=f_brand)
    draw.text((x_name + name_w, bar_h // 2), "." + tld, fill=COLOR_TLD, font=f_brand, anchor="lm")
    handle_w = draw.textlength(HANDLE, font=f_handle)
    draw.text((IMG_W - 40 - handle_w, bar_h // 2), HANDLE, fill=COLOR_HANDLE, font=f_handle, anchor="lm")

    # Title
    d = summary["date"]
    title = f"Stroomprijzen morgen, {NL_DAYS_LONG[d.weekday()]} {d.day} {NL_MONTHS[d.month]}"
    draw.text((40, 98), title, fill=COLOR_TEXT, font=f_title)

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
        ("Gemiddelde", f"{fmt_ct(summary['avg_ct'])} ct", COLOR_TEXT, None),
        (f"Goedkoopst {summary['cheapest']['hour']:02d}:00",
         f"{fmt_ct(summary['cheapest']['ct'])} ct", COLOR_CHEAP, COLOR_MINT),
        (f"Duurst {summary['dearest']['hour']:02d}:00",
         f"{fmt_ct(summary['dearest']['ct'])} ct", COLOR_PRICEY, COLOR_KORAAL),
    ]
    for i, (label, value, color, dot) in enumerate(cards):
        x = 40 + i * (card_w + 20)
        draw.rounded_rectangle([(x, cards_y), (x + card_w, cards_y + card_h)], radius=14,
                               fill=COLOR_CARD_BG, outline=COLOR_CARD_BORDER, width=1)
        lx = x + 20
        if dot is not None:
            draw.ellipse([(lx, cards_y + 23), (lx + 12, cards_y + 35)], fill=dot)
            lx += 20
        draw.text((lx, cards_y + 16), label, fill=COLOR_TEXT_SOFT, font=f_card_label)
        draw.text((x + 20, cards_y + 48), value, fill=color, font=f_card_value)

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


def already_posted(target_date: str) -> bool:
    """True als er voor deze morgen-datum al een tweet is gepost (zie TWEET_STATE_FILE)."""
    try:
        state = json.loads(TWEET_STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] tweet_state.json onleesbaar ({exc}) — behandel als niet-gepost.",
              file=sys.stderr)
        return False
    return state.get("last_posted_date") == target_date


def mark_posted(target_date: str) -> None:
    """Leg vast dat voor deze morgen-datum een tweet is gepost."""
    TWEET_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TWEET_STATE_FILE.write_text(
        json.dumps(
            {"last_posted_date": target_date, "posted_at": datetime.now(timezone.utc).isoformat()},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"[ok] tweet_state.json bijgewerkt: last_posted_date={target_date}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Genereer image + tekst maar post NIET (lokaal testen).")
    p.add_argument("--output", default="/tmp/daily_post.jpg",
                   help="Output JPEG-pad. Default: /tmp/daily_post.jpg")
    p.add_argument("--target-date", default=None,
                   help="Override 'morgen'-datum (YYYY-MM-DD). Voor test of inhalen.")
    p.add_argument("--force", action="store_true",
                   help="Negeer de tweet-markering en post toch (handmatig opnieuw posten).")
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

    # Harde dedup: post hooguit één keer per morgen-datum, ook als prices.json
    # meerdere keren wordt overschreven of de workflow vaker draait. --force omzeilt dit.
    target_date_str = window[0]["time"][:10]
    if not args.dry_run and not args.force and already_posted(target_date_str):
        print(f"SKIP: al getweet voor {target_date_str} (tweet_state.json). Niets gepost.",
              file=sys.stderr)
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
    # Pas ná een geslaagde post markeren. Faalt post_tweet (gooit exception), dan
    # blijft de markering uit en probeert de volgende run het opnieuw.
    mark_posted(target_date_str)


if __name__ == "__main__":
    main()
