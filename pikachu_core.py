"""
Core logic for the Pikachu Monthly Price Mover Tracker.

Data source: TCGdex (https://api.tcgdex.net/v2) -- free, no API key required.

Ranking methodology (see README.md for the full rationale):
  - TCGplayer's block in the API is a point-in-time snapshot with no built-in
    history, so it is used only as a display-only USD price. It is read from
    the card's BASE printing (see TCGPLAYER_VARIANT_PRIORITY) to match the
    Cardmarket track the ranking uses.
  - Cardmarket's avg1 / avg7 / avg30 fields ARE rolling trailing averages
    (in EUR), so they're used as a moving-average crossover, similar to how
    you'd compare short vs long moving averages for a stock:
        pct_change_month = (avg7 - avg30) / avg30 * 100
    avg7 (not avg1) is used as the "recent" side of the crossover because
    avg1 is a single day of sales and is dominated by one-sale noise -- a
    single optimistic listing can swing avg1 by hundreds of percent without
    the card actually having moved over the month. avg1-vs-avg30 is still
    computed and exposed as pct_change_24h, a secondary "last 24h" signal.
  - Cards are selected for the top-N by abs(ln(avg7/avg30)) rather than raw
    abs(pct_change_month). Percent change is bounded at -100% but unbounded
    upward, so ranking by raw absolute percent structurally favors gainers
    over droppers of equal "real" magnitude (e.g. a halving is -50% but the
    reversing double is +100%). The log-ratio treats a halving and a
    doubling as equally large moves. The *displayed* value is always the
    plain signed percentage -- only the sort order uses the log-ratio.
  - Cards below MIN_AVG30_EUR are excluded: at that price a small absolute
    move produces a huge, meaningless percentage swing (bulk commons).
  - This is an approximation, not "price exactly 30 days ago," and it's in
    EUR (Cardmarket) even though the display reference price is in USD
    (TCGplayer) -- both are stated plainly in the README as known
    limitations, not hidden.
"""

import html
import logging
import math
import re
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.tcgdex.net/v2/en"

# Cards with a 30-day average below this (EUR, Cardmarket) are excluded from
# ranking -- penny commons otherwise produce huge, meaningless % swings.
MIN_AVG30_EUR = 1.0

# Polite delay between the ~100+ per-card detail requests.
REQUEST_DELAY_SECONDS = 0.15

# A mover whose set released this many days ago (or fewer) gets a "NEW!"
# badge in the report/notebook.
NEW_CARD_WINDOW_DAYS = 30

# Set release dates are fetched lazily (only for cards that make the top-N,
# not all 150+ rankable cards) and cached per process run since many movers
# can share a set.
_set_release_date_cache = {}

# TCGplayer nests pricing under variant keys that vary per card (not every
# card has every variant). "normal" comes FIRST deliberately: we rank on
# Cardmarket's base (non-holo) avg track, so the displayed price has to be
# the base printing too, or the two disagree. Preferring a holo variant here
# produced a real bug -- Legendary Collection Pikachu (lc-86) has a
# reverse-holofoil block whose only listings are $4,999.99 outliers
# (marketPrice $1,574.99) next to a normal printing at $6.38, and the report
# showed $1,574.99 for a card TCGplayer lists at $6.38.
TCGPLAYER_VARIANT_PRIORITY = [
    "normal",
    "holofoil",
    "reverse-holofoil",
    "1st-edition-holofoil",
    "1st-edition",
    "unlimited",
]

logger = logging.getLogger(__name__)


def fetch_all_pikachu_card_ids():
    """Return the list of card ids for every card whose name contains 'pikachu'.

    TCGdex's `name` filter is a substring match by default, so this also
    picks up "Pikachu V", "Pikachu VMAX", "Pikachu ex", etc. Expect 100+
    results -- that's correct, we need all of them to find the top movers.
    """
    response = requests.get(f"{BASE_URL}/cards", params={"name": "pikachu"}, timeout=30)
    response.raise_for_status()
    cards = response.json()
    return [card["id"] for card in cards]


def _pick_usd_display_price(tcgplayer):
    """Pick a single display USD price from a tcgplayer pricing block.

    Returns (price, variant_key), or (None, None) if the card has no usable
    TCGplayer price. The variant is returned too so the report can say which
    printing the price refers to instead of showing a bare number.
    """
    if not tcgplayer:
        return None, None
    for variant in TCGPLAYER_VARIANT_PRIORITY:
        block = tcgplayer.get(variant)
        if block and block.get("marketPrice") is not None:
            return block["marketPrice"], variant
    # Fall back to any other variant TCGdex might return that we didn't
    # anticipate, rather than silently reporting no price.
    for key, block in tcgplayer.items():
        if isinstance(block, dict) and block.get("marketPrice") is not None:
            return block["marketPrice"], key
    return None, None


def variant_label(name):
    """Strip the redundant "Pikachu" from a card name, leaving what actually
    distinguishes it ("Pikachu V-UNION" -> "V-UNION", "Ash's Pikachu" ->
    "Ash's", plain "Pikachu" -> ""). Every card in this report is a Pikachu,
    so repeating the word in every row carries no information.
    """
    if not name:
        return ""
    remainder = re.sub(r"pikachu", " ", name, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", remainder).strip(" -–—")


def fetch_card_pricing(card_id):
    """Fetch one card's detail and compute its ranking/display fields.

    Returns None (and logs why) if the card is unrankable: no cardmarket
    pricing at all, missing avg7/avg30, or below the price floor.
    """
    response = requests.get(f"{BASE_URL}/cards/{card_id}", timeout=30)
    response.raise_for_status()
    data = response.json()

    pricing = data.get("pricing") or {}
    cardmarket = pricing.get("cardmarket")
    if not cardmarket:
        logger.info("skipping %s: no cardmarket pricing", card_id)
        return None

    avg1 = cardmarket.get("avg1")
    avg7 = cardmarket.get("avg7")
    avg30 = cardmarket.get("avg30")

    if avg7 is None or avg30 is None:
        logger.info("skipping %s: missing avg7/avg30", card_id)
        return None
    if not avg30 or avg30 < MIN_AVG30_EUR:
        logger.info("skipping %s: avg30 %.2f below floor of %.2f", card_id, avg30 or 0.0, MIN_AVG30_EUR)
        return None

    pct_change_month = (avg7 - avg30) / avg30 * 100
    pct_change_24h = (avg1 - avg30) / avg30 * 100 if avg1 is not None else None

    set_info = data.get("set") or {}
    usd_price, usd_variant = _pick_usd_display_price(pricing.get("tcgplayer"))

    # TCGdex returns a base image URL with no extension; a quality + format
    # suffix is required. Some cards (mostly old promos) have no image.
    image_base = data.get("image")

    return {
        "id": card_id,
        "name": data.get("name"),
        "variant_label": variant_label(data.get("name")),
        "set": set_info.get("name"),
        "set_id": set_info.get("id"),
        "local_id": data.get("localId"),
        "image_url": f"{image_base}/high.webp" if image_base else None,
        "pct_change_month": pct_change_month,
        "pct_change_24h": pct_change_24h,
        "usd_display_price": usd_price,
        "usd_price_variant": usd_variant,
        "avg30_eur": avg30,
        "avg7_eur": avg7,
    }


def fetch_set_release_date(set_id):
    """Return a set's release date ("YYYY-MM-DD") or None, caching per set id
    so repeated movers from the same set only cost one request."""
    if set_id in _set_release_date_cache:
        return _set_release_date_cache[set_id]

    response = requests.get(f"{BASE_URL}/sets/{set_id}", timeout=30)
    response.raise_for_status()
    release_date = response.json().get("releaseDate")

    _set_release_date_cache[set_id] = release_date
    return release_date


def _is_recently_released(release_date_str, window_days=NEW_CARD_WINDOW_DAYS, today=None):
    """True if release_date_str ("YYYY-MM-DD") is within window_days of today
    (today injectable for tests). False for missing/unparseable dates."""
    if not release_date_str:
        return False
    try:
        released = datetime.strptime(release_date_str, "%Y-%m-%d").date()
    except ValueError:
        return False
    today = today or datetime.now(timezone.utc).date()
    return 0 <= (today - released).days <= window_days


def _rank_score(card):
    """Log-ratio magnitude used only for sorting -- see module docstring."""
    avg7 = card["avg7_eur"]
    avg30 = card["avg30_eur"]
    if avg7 <= 0 or avg30 <= 0:
        return 0.0
    return abs(math.log(avg7 / avg30))


def get_top_movers(n=10):
    """Fetch pricing for every Pikachu-named card and return the top n movers.

    "Top" means largest log-ratio move (see _rank_score); each returned card
    keeps its own signed pct_change_month so gainers and droppers are both
    represented and distinguishable.
    """
    card_ids = fetch_all_pikachu_card_ids()

    rankable = []
    skipped = 0
    for index, card_id in enumerate(card_ids):
        card = fetch_card_pricing(card_id)
        if card is None:
            skipped += 1
        else:
            rankable.append(card)
        if index < len(card_ids) - 1:
            time.sleep(REQUEST_DELAY_SECONDS)

    logger.info(
        "fetched %d cards: %d rankable, %d skipped",
        len(card_ids), len(rankable), skipped,
    )

    top = sorted(rankable, key=_rank_score, reverse=True)[:n]

    # Enrich only the final top-N with release-date/"NEW!" info -- fetching
    # this for all 150+ rankable cards would roughly double the API calls
    # for no benefit, since only the top-N ever get shown.
    for card in top:
        set_id = card.get("set_id")
        release_date = fetch_set_release_date(set_id) if set_id else None
        card["release_date"] = release_date
        card["is_new"] = _is_recently_released(release_date)
        time.sleep(REQUEST_DELAY_SECONDS)

    return top


def _fmt_pct(value):
    return f"{value:+.1f}%" if value is not None else "n/a"


def _fmt_usd(value):
    return f"${value:,.2f}" if value is not None else "n/a"


def _card_tile_html(rank, card, max_abs_pct):
    """Render one mover as a card tile: image, set identity, move, price."""
    is_gainer = card["pct_change_month"] >= 0
    direction = "up" if is_gainer else "down"
    arrow = "▲" if is_gainer else "▼"

    set_name = html.escape(card.get("set") or "Unknown set")
    local_id = html.escape(str(card.get("local_id") or ""))
    variant = html.escape(card.get("variant_label") or "")
    variant_html = f'<span class="variant">{variant}</span>' if variant else ""
    new_html = '<span class="badge-new">NEW!</span>' if card.get("is_new") else ""
    number_html = f"#{local_id}" if local_id else ""

    image_url = card.get("image_url")
    if image_url:
        alt = html.escape(f"{card.get('name') or 'Pikachu card'} - {card.get('set') or ''}")
        art = (
            f'<img class="art" src="{html.escape(image_url)}" loading="lazy" alt="{alt}">'
        )
    else:
        # TCGdex genuinely has no artwork for some promos (e.g. swshp-SWSH074).
        # Say so, rather than showing something that reads as a broken image.
        art = (
            '<div class="art art-missing">'
            '<span class="art-missing-bolt" aria-hidden="true">⚡</span>'
            '<span class="art-missing-label">No artwork<br>on file</span>'
            "</div>"
        )

    # Diverging magnitude bar on a scale shared by every tile, so bar lengths
    # are comparable across the grid. Grows right from centre for a gain,
    # left for a drop.
    width_pct = (abs(card["pct_change_month"]) / max_abs_pct * 50) if max_abs_pct else 0
    side = "left:50%;" if is_gainer else "right:50%;"
    bar_style = f"width:{width_pct:.2f}%;{side}"

    price_variant = card.get("usd_price_variant")
    price_note = (
        f"{price_variant.replace('-', ' ')} printing" if price_variant
        else "no TCGplayer listing"
    )
    tooltip = html.escape(
        f"Cardmarket 30-day avg EUR {card['avg30_eur']:.2f} -> 7-day avg EUR {card['avg7_eur']:.2f}"
        f" | price shown is the {price_note}"
    )

    return f"""
      <article class="tile {direction}" title="{tooltip}">
        <div class="tile-rank">{rank}</div>
        <div class="art-frame">{art}</div>
        <div class="tile-body">
          <div class="identity">
            <h2 class="set">{set_name}</h2>
            <div class="meta">{number_html} {variant_html} {new_html}</div>
          </div>
          <div class="move">
            <span class="pct">{arrow} {card['pct_change_month']:+.1f}%</span>
            <span class="window">30-day move</span>
          </div>
          <div class="bar-track" role="presentation">
            <span class="bar-zero"></span>
            <span class="bar-fill" style="{bar_style}"></span>
          </div>
          <dl class="stats">
            <div><dt>Price</dt><dd class="price">{_fmt_usd(card.get('usd_display_price'))}</dd></div>
            <div><dt>Last 24h</dt><dd class="pct24">{_fmt_pct(card.get('pct_change_24h'))}</dd></div>
          </dl>
        </div>
      </article>
"""


def render_html_report(movers):
    """Render the Pikachu card price report as a self-contained HTML page
    (inline CSS, no external assets beyond the card art) suitable for writing
    straight to docs/index.html for GitHub Pages.
    """
    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    max_abs_pct = max((abs(c["pct_change_month"]) for c in movers), default=0)
    gainers = [c for c in movers if c["pct_change_month"] >= 0]
    droppers = [c for c in movers if c["pct_change_month"] < 0]
    top_gain = max((c["pct_change_month"] for c in gainers), default=None)
    top_drop = min((c["pct_change_month"] for c in droppers), default=None)

    tiles = "".join(
        _card_tile_html(rank, card, max_abs_pct)
        for rank, card in enumerate(movers, start=1)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pikachu Card Prices</title>
<style>
  :root {{
    color-scheme: light;
    --plane: #f9f9f7;
    --surface: #fcfcfb;
    --ink: #0b0b0b;
    --ink-2: #52514e;
    --muted: #898781;
    --hairline: rgba(11,11,11,0.10);
    --up: #006300;
    --up-mark: #0ca30c;
    --down: #d03b3b;
    --down-mark: #d03b3b;
    --accent: #f6c945;
    --accent-ink: #3a2f00;
    --shadow: 0 1px 2px rgba(11,11,11,0.06), 0 8px 24px rgba(11,11,11,0.06);
    --hover-shadow: 0 6px 12px rgba(11,11,11,0.10), 0 18px 40px rgba(11,11,11,0.10);
    --chip: rgba(137,135,129,0.16);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --plane: #0d0d0d;
      --surface: #1a1a19;
      --ink: #ffffff;
      --ink-2: #c3c2b7;
      --muted: #898781;
      --hairline: rgba(255,255,255,0.10);
      --up: #0ca30c;
      --up-mark: #0ca30c;
      --down: #e66767;
      --down-mark: #d03b3b;
      --shadow: 0 1px 2px rgba(0,0,0,0.40), 0 8px 24px rgba(0,0,0,0.35);
      --hover-shadow: 0 6px 12px rgba(0,0,0,0.45), 0 18px 40px rgba(0,0,0,0.40);
      --chip: rgba(195,194,183,0.14);
    }}
  }}

  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--plane);
    color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    line-height: 1.45;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 40px 20px 64px; }}

  /* ---- header ---- */
  .hero {{
    display: flex; flex-wrap: wrap; gap: 20px;
    align-items: flex-end; justify-content: space-between;
    padding-bottom: 24px; margin-bottom: 28px;
    border-bottom: 1px solid var(--hairline);
  }}
  .title-block h1 {{
    margin: 0; font-size: clamp(28px, 5vw, 42px); letter-spacing: -0.02em; line-height: 1.1;
  }}
  .spark {{
    display: inline-block; background: var(--accent); color: var(--accent-ink);
    font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
    padding: 4px 9px; border-radius: 999px; margin-bottom: 10px;
  }}
  .title-block p {{ margin: 8px 0 0; color: var(--ink-2); font-size: 14px; max-width: 46ch; }}
  .stamp {{ color: var(--muted); font-size: 12px; }}

  .summary {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 0; }}
  .stat {{
    background: var(--surface); border: 1px solid var(--hairline); border-radius: 12px;
    padding: 10px 14px; min-width: 104px; box-shadow: var(--shadow);
  }}
  .stat dt {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; margin: 0 0 2px; }}
  .stat dd {{ margin: 0; font-size: 19px; font-weight: 650; }}
  .stat dd.up {{ color: var(--up); }}
  .stat dd.down {{ color: var(--down); }}

  /* ---- grid ---- */
  .grid {{
    display: grid; gap: 16px;
    grid-template-columns: repeat(auto-fill, minmax(232px, 1fr));
  }}
  .tile {{
    position: relative; display: flex; flex-direction: column;
    background: var(--surface); border: 1px solid var(--hairline);
    border-radius: 14px; overflow: hidden; box-shadow: var(--shadow);
    transition: transform .15s ease, box-shadow .15s ease;
  }}
  .tile:hover {{ transform: translateY(-3px); box-shadow: var(--hover-shadow); }}
  @media (prefers-reduced-motion: reduce) {{
    .tile {{ transition: none; }}
    .tile:hover {{ transform: none; }}
  }}
  .tile-rank {{
    position: absolute; top: 10px; left: 10px; z-index: 2;
    width: 26px; height: 26px; border-radius: 50%;
    background: var(--ink); color: var(--surface);
    font-size: 13px; font-weight: 700;
    display: grid; place-items: center;
    font-variant-numeric: tabular-nums;
  }}
  .art-frame {{
    padding: 18px 18px 6px; display: grid; place-items: center;
    background: linear-gradient(160deg, rgba(246,201,69,0.16), transparent 62%);
  }}
  .art {{
    width: 100%; max-width: 168px; border-radius: 8px; display: block;
    aspect-ratio: 245/342; object-fit: contain;
  }}
  .art-missing {{
    width: 100%; max-width: 168px; aspect-ratio: 245/342; border-radius: 8px;
    border: 1px dashed var(--hairline); color: var(--muted);
    display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px;
    background: var(--chip); text-align: center;
  }}
  .art-missing-bolt {{ font-size: 26px; opacity: .5; }}
  .art-missing-label {{ font-size: 11px; line-height: 1.3; }}
  .tile-body {{ padding: 12px 16px 16px; display: flex; flex-direction: column; gap: 10px; flex: 1; }}

  .set {{ margin: 0; font-size: 15px; font-weight: 620; letter-spacing: -0.01em; line-height: 1.25; }}
  .meta {{
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 3px;
    font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums;
  }}
  .variant {{
    background: var(--chip); color: var(--ink-2);
    padding: 1px 7px; border-radius: 999px; font-size: 11px; font-weight: 600;
  }}
  .badge-new {{
    background: var(--accent); color: var(--accent-ink);
    padding: 1px 7px; border-radius: 999px; font-size: 10px; font-weight: 800; letter-spacing: 0.04em;
  }}

  .move {{ display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }}
  .pct {{ font-size: 24px; font-weight: 700; letter-spacing: -0.02em; }}
  .window {{ font-size: 11px; color: var(--muted); }}
  .tile.up .pct {{ color: var(--up); }}
  .tile.down .pct {{ color: var(--down); }}

  .bar-track {{ position: relative; height: 6px; background: var(--chip); border-radius: 999px; }}
  .bar-zero {{ position: absolute; left: 50%; top: -2px; bottom: -2px; width: 1px; background: var(--hairline); }}
  .bar-fill {{ position: absolute; top: 0; bottom: 0; border-radius: 999px; }}
  .tile.up .bar-fill {{ background: var(--up-mark); }}
  .tile.down .bar-fill {{ background: var(--down-mark); }}

  .stats {{
    display: flex; gap: 18px; margin: auto 0 0; padding-top: 10px;
    border-top: 1px solid var(--hairline);
  }}
  .stats div {{ display: flex; flex-direction: column; }}
  .stats dt {{ font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 1px; }}
  .stats dd {{ margin: 0; font-size: 15px; font-weight: 620; font-variant-numeric: tabular-nums; }}
  .stats .pct24 {{ color: var(--ink-2); font-size: 13px; font-weight: 550; }}

  /* ---- footer ---- */
  .notes {{
    margin-top: 32px; padding-top: 20px; border-top: 1px solid var(--hairline);
    color: var(--muted); font-size: 12.5px; line-height: 1.6; max-width: 78ch;
  }}
  .notes strong {{ color: var(--ink-2); }}
  .notes p {{ margin: 0 0 8px; }}
  .notes a {{ color: inherit; }}
</style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <div class="title-block">
        <span class="spark">⚡ Top 10 movers</span>
        <h1>Pikachu Card Prices</h1>
        <p>The Pikachu cards that moved the most over the past month, ranked by the size of the move &mdash; gains and drops together.</p>
      </div>
      <div>
        <dl class="summary">
          <div class="stat"><dt>Biggest gain</dt><dd class="up">{_fmt_pct(top_gain)}</dd></div>
          <div class="stat"><dt>Biggest drop</dt><dd class="down">{_fmt_pct(top_drop)}</dd></div>
          <div class="stat"><dt>Gainers</dt><dd>{len(gainers)}<span style="font-size:13px;color:var(--muted)"> / {len(movers)}</span></dd></div>
        </dl>
        <p class="stamp" style="margin:10px 0 0">Updated {generated_at}</p>
      </div>
    </header>

    <main class="grid">{tiles}</main>

    <footer class="notes">
      <p><strong>How the move is measured.</strong> Cardmarket publishes rolling trailing averages in EUR. The 30-day move is
      (7-day average &minus; 30-day average) &divide; 30-day average &mdash; a moving-average crossover, not the price exactly
      30 days ago. &ldquo;Last 24h&rdquo; is the same comparison against the 1-day average, which is a single day of sales and
      much noisier.</p>
      <p><strong>How the top 10 is chosen.</strong> By the size of the move on a log scale, so a halving and a doubling count
      equally. A plain percentage ranking would be almost all gainers, because a drop can never exceed &minus;100% while a gain
      has no ceiling. Cards averaging under &euro;1.00 are left out &mdash; a few cents of movement on a bulk common reads as a
      triple-digit swing.</p>
      <p><strong>Price</strong> is the TCGplayer market price in USD for the card&rsquo;s base printing, shown as a snapshot for
      scale &mdash; the ranking is not based on it, and it is a different marketplace and currency from the EUR figures above.
      Cards with no Cardmarket 7- or 30-day average are skipped rather than counted as flat. A <strong>NEW!</strong> badge marks
      a set released in the last {NEW_CARD_WINDOW_DAYS} days. Data from <a href="https://tcgdex.dev/">TCGdex</a>.</p>
    </footer>
  </div>
</body>
</html>
"""
