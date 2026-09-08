"""
Core logic for the Pikachu Monthly Price Mover Tracker.

Data source: TCGdex (https://api.tcgdex.net/v2) -- free, no API key required.

Ranking methodology (see README.md for the full rationale):
  - TCGplayer's block in the API is a point-in-time snapshot with no built-in
    history, so it is used only as a display-only USD reference price.
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

import logging
import math
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
# card has every variant). Preferred order for picking a single display
# price: holo-style variants first, then plain, then unlimited.
TCGPLAYER_VARIANT_PRIORITY = [
    "reverse-holofoil",
    "holofoil",
    "1st-edition-holofoil",
    "1st-edition",
    "normal",
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
    """Pick a single display-only USD price from a tcgplayer pricing block."""
    if not tcgplayer:
        return None
    for variant in TCGPLAYER_VARIANT_PRIORITY:
        block = tcgplayer.get(variant)
        if block and block.get("marketPrice") is not None:
            return block["marketPrice"]
    # Fall back to any other variant TCGdex might return that we didn't
    # anticipate, rather than silently reporting no price.
    for key, block in tcgplayer.items():
        if isinstance(block, dict) and block.get("marketPrice") is not None:
            return block["marketPrice"]
    return None


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

    return {
        "id": card_id,
        "name": data.get("name"),
        "set": set_info.get("name"),
        "set_id": set_info.get("id"),
        "local_id": data.get("localId"),
        "pct_change_month": pct_change_month,
        "pct_change_24h": pct_change_24h,
        "usd_display_price": _pick_usd_display_price(pricing.get("tcgplayer")),
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


def render_html_report(movers):
    """Render an HTML table report for a list of mover dicts (as returned by
    get_top_movers). Self-contained (inline CSS, no external assets) so it
    can be used directly as an email body or written to docs/index.html.
    """
    from datetime import datetime, timezone

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows_html = []
    for rank, card in enumerate(movers, start=1):
        is_gainer = card["pct_change_month"] >= 0
        row_class = "gainer" if is_gainer else "dropper"
        arrow = "▲" if is_gainer else "▼"

        pct_24h = card.get("pct_change_24h")
        pct_24h_display = f"{pct_24h:+.1f}%" if pct_24h is not None else "n/a"

        usd_price = card.get("usd_display_price")
        usd_display = f"${usd_price:,.2f}" if usd_price is not None else "n/a"

        set_name = card.get("set") or "Unknown set"
        local_id = card.get("local_id") or ""
        new_badge = '<span class="new-badge">NEW!</span>' if card.get("is_new") else ""

        rows_html.append(
            f"""
            <tr class="{row_class}">
              <td class="rank">{rank}</td>
              <td class="name">{card.get('name', 'Unknown')} {new_badge}</td>
              <td class="set">{set_name} {f'#{local_id}' if local_id else ''}</td>
              <td class="pct-month">{arrow} {card['pct_change_month']:+.1f}%</td>
              <td class="pct-24h">{pct_24h_display}</td>
              <td class="usd">{usd_display}</td>
            </tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Pikachu Monthly Price Movers</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; background: #f7f7f9; color: #222; margin: 0; padding: 24px; }}
  .container {{ max-width: 820px; margin: 0 auto; background: #fff; border-radius: 8px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .subtitle {{ color: #666; font-size: 13px; margin: 0 0 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th {{ text-align: left; padding: 8px 10px; border-bottom: 2px solid #ddd; color: #555; font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em; }}
  td {{ padding: 10px; border-bottom: 1px solid #eee; }}
  tr.gainer .pct-month {{ color: #1a7f37; font-weight: 600; }}
  tr.dropper .pct-month {{ color: #cf222e; font-weight: 600; }}
  td.pct-24h {{ color: #888; }}
  td.rank {{ color: #999; font-variant-numeric: tabular-nums; }}
  td.usd {{ font-variant-numeric: tabular-nums; }}
  .new-badge {{ display: inline-block; background: #0969da; color: #fff; font-size: 10px; font-weight: 700; letter-spacing: 0.03em; padding: 2px 6px; border-radius: 10px; vertical-align: middle; }}
  .caveat {{ margin-top: 20px; font-size: 12px; color: #888; line-height: 1.5; }}
</style>
</head>
<body>
  <div class="container">
    <h1>Pikachu Monthly Price Movers</h1>
    <p class="subtitle">Top {len(movers)} Pikachu-named cards by monthly price move &middot; generated {generated_at}</p>
    <table>
      <thead>
        <tr>
          <th>#</th><th>Card</th><th>Set</th><th>Monthly move</th><th>Last 24h</th><th>USD ref. price</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows_html)}
      </tbody>
    </table>
    <p class="caveat">
      "Monthly move" = (Cardmarket 7-day avg &minus; 30-day avg) / 30-day avg, in EUR &mdash;
      an approximation, not the price exactly 30 days ago. Ranked by log-ratio magnitude so
      gainers and droppers are compared fairly; cards under &euro;{MIN_AVG30_EUR:.2f} (30-day avg) are excluded.
      USD reference price is a live TCGplayer snapshot, shown for context only. A blue
      "NEW!" badge marks cards whose set released within the last {NEW_CARD_WINDOW_DAYS} days.
    </p>
  </div>
</body>
</html>
"""
