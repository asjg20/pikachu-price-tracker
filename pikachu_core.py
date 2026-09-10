"""
Core logic for the Pikachu Monthly Price Mover Tracker.

Data source: TCGdex (https://api.tcgdex.net/v2) -- free, no API key required.

Ranking methodology (see README.md for the full rationale):
  - Everything the report DISPLAYS is Cardmarket, in EUR. Showing a TCGplayer
    USD price beside a Cardmarket EUR percentage made each row assert
    something false -- "this $750 card rose 155.8%" when the $750 and the
    155.8% came from different marketplaces in different currencies. One
    source, one currency, and the columns reconcile by eye.
  - TCGplayer's block is still parsed (it is a point-in-time snapshot with no
    history, so it could never drive the ranking anyway); its price is used
    only as a tie-breaker when de-duplicating cards that share a Cardmarket
    product. It is read from the card's BASE printing, see
    TCGPLAYER_VARIANT_PRIORITY.
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
  - Some cards' Cardmarket feed is simply wrong -- e.g. an avg7 several times
    what TCGplayer and independent trackers show for the same card, likely a
    single outlier sale (possibly a graded slab; Cardmarket carries no
    condition/grade field) dominating a thin week of trades. There is no
    reliable way to detect this generally: the ratio between a card's avg1,
    avg7 and avg30 was tried as a bad-data signal and rejected -- across the
    full card set it forms one smooth continuum with no gap between
    confirmed-bad cards and legitimate large moves (a verified-bad card can
    sit at 2.25x while a card with no evidence of a problem sits at 2.21x).
    Known-bad cards are excluded by id via KNOWN_BAD_CARDS, each entry
    documented with the evidence that got it added -- a small, honest,
    manually-curated list rather than a heuristic that doesn't actually work.
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

# Card image URLs verified to exist (see _resolve_image_url), keyed by base URL.
_image_url_cache = {}

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

# Cards whose Cardmarket pricing (via TCGdex) has been checked against outside
# trackers and found unreliable -- not "this card is volatile," but "this
# specific number does not hold up against reality." Excluded before any
# other logic runs.
#
# There is no statistical shortcut for this list: the ratio between a card's
# 1-day, 7-day and 30-day averages was tested as a general "unreliable data"
# detector and rejected -- across the full card set it forms one smooth
# continuum with no gap between confirmed-bad cards and legitimate large
# moves. Cards below were flagged by hand, cross-checked against independent
# trackers, and belong here only because someone actually looked.
#
# To add one: verify the number against TCGplayer, PokeScope, or another
# independent tracker first -- a big move alone is not evidence of bad data.
KNOWN_BAD_CARDS = {
    "ru1-7": (
        "Pokemon Rumble Pikachu -- avg7 EUR1,869.99 vs TCGplayer market price "
        "~$750 (~EUR700) and Troll & Toad retail $169.99. avg1 spikes to "
        "EUR4,100 in a single day, consistent with one outlier sale (possibly "
        "a graded slab -- Cardmarket's feed carries no condition/grade field) "
        "dominating a thin week of trades."
    ),
    "swshp-SWSH074": (
        "Special Delivery Pikachu -- avg7 EUR1,511.93 vs independent trackers "
        "(PokeScope $454.55, CardRake $263.60) and even this card's OWN holo "
        "Cardmarket track (avg7 EUR377.32, which roughly matches reality). "
        "The non-holo track used throughout this report is the contaminated "
        "one here specifically -- not a rule that generalizes to other cards."
    ),
}

# Page backdrop: a Pokemon Center summer-campaign illustration (not a card,
# not from TCGdex). Committed at docs/assets/backdrop.jpg since it has no
# public URL of its own -- referenced relative to docs/index.html so it
# works both locally (file://) and on GitHub Pages without changes.
BACKDROP_IMAGE_URL = "assets/backdrop.jpg"

# TCGdex occasionally returns a transient 5xx. The weekly workflow runs
# unattended, so a single blip must not take down the whole report.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5

logger = logging.getLogger(__name__)


def _get_json(url, params=None):
    """GET a TCGdex URL and return parsed JSON, retrying transient failures.

    Retries on connection errors, timeouts and 5xx responses with a linear
    backoff. A 404 (or any other 4xx) is not retried -- it's a real answer.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=30)
            if response.status_code >= 500:
                raise requests.exceptions.HTTPError(
                    f"{response.status_code} server error", response=response
                )
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.HTTPError) as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status is not None and status < 500:
                raise  # a genuine 4xx -- retrying won't help
            last_error = error
            if attempt < MAX_RETRIES:
                logger.warning(
                    "%s failed (attempt %d/%d): %s -- retrying",
                    url, attempt, MAX_RETRIES, error,
                )
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise last_error


def fetch_all_pikachu_card_ids():
    """Return the list of card ids for every card whose name contains 'pikachu'.

    TCGdex's `name` filter is a substring match by default, so this also
    picks up "Pikachu V", "Pikachu VMAX", "Pikachu ex", etc. Expect 100+
    results -- that's correct, we need all of them to find the top movers.
    """
    cards = _get_json(f"{BASE_URL}/cards", params={"name": "pikachu"})
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

    Returns None (and logs why) if the card is unrankable: known-bad pricing
    data, no cardmarket pricing at all, missing avg7/avg30, or below the
    price floor.
    """
    if card_id in KNOWN_BAD_CARDS:
        logger.info("skipping %s: known bad Cardmarket data -- %s", card_id, KNOWN_BAD_CARDS[card_id])
        return None

    data = _get_json(f"{BASE_URL}/cards/{card_id}")

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
    # suffix is required. Some cards (mostly old promos) have no image at all,
    # and some have PNG but not WebP -- see _resolve_image_url.
    image_base = data.get("image")

    return {
        "id": card_id,
        "name": data.get("name"),
        "variant_label": variant_label(data.get("name")),
        "set": set_info.get("name"),
        "set_id": set_info.get("id"),
        "local_id": data.get("localId"),
        "image_base": image_base,
        "image_url": None,  # filled in by _resolve_image_url for shown cards
        "pct_change_month": pct_change_month,
        "pct_change_24h": pct_change_24h,
        "change_eur": avg7 - avg30,
        "usd_display_price": usd_price,
        "usd_price_variant": usd_variant,
        "cm_product_id": cardmarket.get("idProduct"),
        "avg1_eur": avg1,
        "avg30_eur": avg30,
        "avg7_eur": avg7,
    }


def fetch_set_release_date(set_id):
    """Return a set's release date ("YYYY-MM-DD") or None, caching per set id
    so repeated movers from the same set only cost one request."""
    if set_id in _set_release_date_cache:
        return _set_release_date_cache[set_id]

    release_date = _get_json(f"{BASE_URL}/sets/{set_id}").get("releaseDate")

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


def fetch_all_rankable_cards():
    """Fetch pricing for every Pikachu-named card.

    Returns (rankable_cards, skipped_count). This is the expensive call --
    one request per card -- so callers that need several views of the data
    should call it once and slice the result rather than re-fetching.
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

    deduped = _dedupe_by_product(rankable)
    logger.info(
        "fetched %d cards: %d rankable (%d after de-duplication), %d skipped",
        len(card_ids), len(rankable), len(deduped), skipped,
    )
    return deduped, skipped


def _dedupe_by_product(cards):
    """Collapse cards that share one Cardmarket product into a single entry.

    TCGdex sometimes lists the same physical card under several ids (e.g.
    xyp-XY95 and xyp-XY202 both map to Cardmarket idProduct 289809), and
    they carry byte-identical pricing. Left alone they show up as duplicate
    rows and eat slots in a top-5. Where duplicates exist we keep the most
    complete record -- one with a USD price and artwork beats one without.
    """
    best_by_product = {}
    passthrough = []

    for card in cards:
        product_id = card.get("cm_product_id")
        if product_id is None:
            passthrough.append(card)
            continue
        completeness = (
            card.get("usd_display_price") is not None,
            card.get("image_url") is not None,
        )
        existing = best_by_product.get(product_id)
        if existing is None or completeness > existing[0]:
            best_by_product[product_id] = (completeness, card)

    return passthrough + [card for _, card in best_by_product.values()]


def _resolve_image_url(image_base):
    """Pick an image URL that actually exists for this card.

    Most cards serve /high.webp, but a minority (~5%) only have /high.png --
    requesting webp for those returns 404 and the browser shows a broken
    image, since <picture> fallbacks only cover format support, not misses.
    One HEAD request per *displayed* card settles it, and the answer is
    cached. Falls back to PNG if the check itself fails.
    """
    if not image_base:
        return None
    if image_base in _image_url_cache:
        return _image_url_cache[image_base]

    webp = f"{image_base}/high.webp"
    resolved = f"{image_base}/high.png"
    try:
        if requests.head(webp, timeout=15).status_code == 200:
            resolved = webp
    except requests.exceptions.RequestException as error:
        logger.warning("image check failed for %s, using PNG: %s", image_base, error)

    _image_url_cache[image_base] = resolved
    return resolved


def _enrich_for_display(cards):
    """Fill in the extras only the displayed cards need: the "NEW!" badge and
    a verified image URL.

    Done for a handful of selected cards rather than every rankable one --
    that would multiply the API calls for information only the shown rows
    ever use. Results are cached per set id / image.
    """
    for card in cards:
        set_id = card.get("set_id")
        release_date = None
        if set_id:
            try:
                release_date = fetch_set_release_date(set_id)
            except requests.exceptions.RequestException as error:
                # A cosmetic badge is never worth failing the whole report for.
                logger.warning("could not read release date for set %s: %s", set_id, error)
        card["release_date"] = release_date
        card["is_new"] = _is_recently_released(release_date)
        card["image_url"] = _resolve_image_url(card.get("image_base"))
        time.sleep(REQUEST_DELAY_SECONDS)
    return cards


def get_top_movers(n=10):
    """Return the n biggest movers, gainers and droppers mixed together.

    "Biggest" means largest log-ratio move (see _rank_score); each returned
    card keeps its own signed pct_change_month. Used by the notebook; the
    HTML report uses get_gainers_and_losers instead, which splits the two
    directions into separate lists.
    """
    rankable, _ = fetch_all_rankable_cards()
    top = sorted(rankable, key=_rank_score, reverse=True)[:n]
    return _enrich_for_display(top)


def _wildest_24h_swing(rankable):
    """The card whose 1-day average has moved furthest from its 7-day average.

    This is the one job avg1 is actually good for: it's too noisy to rank a
    monthly trend on, but that same sensitivity makes it a decent detector of
    "something happened to this card in the last day".
    """
    candidates = [
        c for c in rankable
        if c.get("avg1_eur") is not None and c.get("avg7_eur")
    ]
    if not candidates:
        return None
    card = max(candidates, key=lambda c: abs(c["avg1_eur"] - c["avg7_eur"]) / c["avg7_eur"])
    swing = (card["avg1_eur"] - card["avg7_eur"]) / card["avg7_eur"] * 100
    return {"card": card, "swing_pct": swing}


def get_gainers_and_losers(n=5):
    """Return the top n gainers and top n losers, plus summary stats.

    Splitting the two directions into separate lists removes the need for the
    log-ratio trick used by get_top_movers: percent change is bounded at
    -100% but unbounded upward, which skews a *combined* ranking toward
    gainers, but within a single-direction list a plain percentage sort is
    exactly right.
    """
    rankable, skipped = fetch_all_rankable_cards()

    by_move = sorted(rankable, key=lambda c: c["pct_change_month"], reverse=True)
    gainers = [c for c in by_move if c["pct_change_month"] > 0][:n]
    losers = [c for c in reversed(by_move) if c["pct_change_month"] < 0][:n]

    priciest = max(rankable, key=lambda c: c["avg7_eur"]) if rankable else None
    wildest = _wildest_24h_swing(rankable)

    # The sidebar features are shown too, so they need art and badges as much
    # as the table rows do. De-duplicate by id -- the priciest card is often
    # also a top gainer, and enriching it twice would just cost extra requests.
    shown = list(gainers) + list(losers)
    for card in (priciest, wildest["card"] if wildest else None):
        if card is not None and not any(c is card for c in shown):
            shown.append(card)
    _enrich_for_display(shown)

    return {
        "gainers": gainers,
        "losers": losers,
        "stats": {
            "tracked": len(rankable),
            "skipped": skipped,
            "gainer_count": sum(1 for c in rankable if c["pct_change_month"] > 0),
            "loser_count": sum(1 for c in rankable if c["pct_change_month"] < 0),
            "priciest": priciest,
            "wildest_24h": wildest,
        },
    }


def _cardmarket_url(card):
    """Link to this card's own Cardmarket product page, or None.

    URL shape (game name + bare idProduct as a query param) is Cardmarket's
    own documented cross-site linking convention -- the same one Scryfall
    uses to link out to Cardmarket for Magic cards. Cardmarket blocks
    automated requests outright (403 on every path, including this one), so
    this could not be verified by fetching it; it's confirmed via that
    published convention and the idProduct already on hand for de-duplication,
    not by a live check.
    """
    product_id = card.get("cm_product_id")
    if not product_id:
        return None
    return f"https://www.cardmarket.com/en/Pokemon/Products?idProduct={product_id}"


def _fmt_pct(value):
    return f"{value:+.1f}%" if value is not None else "—"


def _pikachu_face_svg():
    """Inline Pikachu face for the title.

    Drawn rather than pulled from an emoji font -- Unicode has no Pikachu, and
    an image would need a network round trip just to render the heading. Each
    ear is a rotated ellipse used as a clip, with a black band filled across
    its top to make the tip, which keeps the tips following the ear angle.
    """
    return (
        '<svg class="face" viewBox="0 0 100 100" role="img" aria-label="Pikachu">'
        "<defs>"
        '<clipPath id="ear-l"><ellipse cx="31" cy="26" rx="8.5" ry="24" '
        'transform="rotate(-30 31 26)"/></clipPath>'
        '<clipPath id="ear-r"><ellipse cx="69" cy="26" rx="8.5" ry="24" '
        'transform="rotate(30 69 26)"/></clipPath>'
        "</defs>"
        '<g clip-path="url(#ear-l)">'
        '<rect x="0" y="0" width="100" height="100" fill="#F7D02C"/>'
        '<rect x="0" y="0" width="100" height="15" fill="#3B3B3B"/></g>'
        '<g clip-path="url(#ear-r)">'
        '<rect x="0" y="0" width="100" height="100" fill="#F7D02C"/>'
        '<rect x="0" y="0" width="100" height="15" fill="#3B3B3B"/></g>'
        '<ellipse cx="50" cy="62" rx="31" ry="28" fill="#F7D02C"/>'
        '<circle cx="24" cy="70" r="7.5" fill="#E3350D"/>'
        '<circle cx="76" cy="70" r="7.5" fill="#E3350D"/>'
        '<ellipse cx="39" cy="55" rx="5.6" ry="6" fill="#2B2B2B"/>'
        '<ellipse cx="61" cy="55" rx="5.6" ry="6" fill="#2B2B2B"/>'
        '<circle cx="40.8" cy="52.6" r="1.9" fill="#fff"/>'
        '<circle cx="62.8" cy="52.6" r="1.9" fill="#fff"/>'
        '<path d="M46 66 Q50 70 54 66" stroke="#2B2B2B" stroke-width="2.6" '
        'fill="none" stroke-linecap="round"/>'
        "</svg>"
    )


def _fmt_eur(value):
    return f"€{value:,.2f}" if value is not None else "—"


def _thumb_html(card):
    image_url = card.get("image_url")
    if image_url:
        alt = html.escape(f"{card.get('name') or 'Pikachu'} — {card.get('set') or ''}")
        return f'<img class="thumb" src="{html.escape(image_url)}" loading="lazy" alt="{alt}">'
    # TCGdex has no artwork on file for some promos. At this size a bare glyph
    # reads as a broken image, so say what happened.
    return (
        '<span class="thumb thumb-empty">'
        '<span class="thumb-bolt" aria-hidden="true">⚡</span>'
        '<span class="thumb-label">No art<br>on file</span>'
        "</span>"
    )


def _row_html(rank, card):
    direction = "up" if card["pct_change_month"] >= 0 else "down"
    arrow = "▲" if direction == "up" else "▼"

    set_name = html.escape(card.get("set") or "Unknown set")
    local_id = html.escape(str(card.get("local_id") or ""))
    variant = html.escape(card.get("variant_label") or "")
    variant_html = f'<span class="chip">{variant}</span>' if variant else ""
    new_html = '<span class="chip chip-new">NEW</span>' if card.get("is_new") else ""

    word = "up" if direction == "up" else "down"
    tooltip_text = (
        f"{card.get('name')} — was €{card['avg30_eur']:,.2f} a month ago, "
        f"now €{card['avg7_eur']:,.2f}: {word} {abs(card['pct_change_month']):.1f}%"
    )

    cardmarket_url = _cardmarket_url(card)
    if cardmarket_url:
        tooltip_text += " — click to check this price on Cardmarket"
        set_name_html = (
            f'<a class="set-link" href="{html.escape(cardmarket_url)}" '
            f'target="_blank" rel="noopener noreferrer">{set_name}'
            f'<span class="out-icon" aria-hidden="true">↗</span></a>'
        )
    else:
        set_name_html = set_name
    tooltip = html.escape(tooltip_text)

    return f"""
        <tr class="{direction}" title="{tooltip}">
          <td class="c-rank">{rank}</td>
          <td class="c-card">
            <span class="cell">
              {_thumb_html(card)}
              <span class="card-id">
                <span class="card-set">{set_name_html}</span>
                <span class="card-sub">{f'#{local_id}' if local_id else ''} {variant_html} {new_html}</span>
              </span>
            </span>
          </td>
          <td class="c-num c-was">{_fmt_eur(card.get('avg30_eur'))}</td>
          <td class="c-num c-now">{_fmt_eur(card.get('avg7_eur'))}</td>
          <td class="c-num c-pct"><span class="pill {direction}">{arrow} {abs(card['pct_change_month']):.1f}%</span></td>
        </tr>"""


def _table_html(cards, empty_message):
    if not cards:
        return f'<p class="empty">{empty_message}</p>'
    rows = "".join(_row_html(rank, card) for rank, card in enumerate(cards, start=1))
    return f"""
      <table class="board">
        <thead>
          <tr>
            <th class="c-rank">#</th>
            <th class="c-card">Card</th>
            <th class="c-num">A month ago</th>
            <th class="c-num">Price now</th>
            <th class="c-num">Change</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>"""


def _feature_name_html(card):
    """A sidebar feature's card name, linked to Cardmarket when possible --
    same treatment as a table row's set name, see _cardmarket_url."""
    set_name = html.escape(card.get("set") or "")
    cardmarket_url = _cardmarket_url(card)
    if not cardmarket_url:
        return set_name
    return (
        f'<a class="set-link" href="{html.escape(cardmarket_url)}" '
        f'target="_blank" rel="noopener noreferrer">{set_name}'
        f'<span class="out-icon" aria-hidden="true">↗</span></a>'
    )


def _aside_html(stats):
    priciest = stats.get("priciest")
    wildest = stats.get("wildest_24h")

    if priciest:
        priciest_block = f"""
        <div class="feature">
          {_thumb_html(priciest)}
          <div class="feature-text">
            <span class="feature-value">{_fmt_eur(priciest.get('avg7_eur'))}</span>
            <span class="feature-name">{_feature_name_html(priciest)}</span>
            <span class="feature-sub">#{html.escape(str(priciest.get('local_id') or ''))}
              {html.escape(priciest.get('variant_label') or '')}</span>
          </div>
        </div>"""
    else:
        priciest_block = '<p class="empty">No priced cards.</p>'

    if wildest:
        card = wildest["card"]
        swing_dir = "up" if wildest["swing_pct"] >= 0 else "down"
        swing_word = "jumped" if swing_dir == "up" else "dropped"
        wildest_block = f"""
        <div class="feature">
          {_thumb_html(card)}
          <div class="feature-text">
            <span class="feature-value {swing_dir}">{abs(wildest['swing_pct']):.0f}%</span>
            <span class="feature-name">{_feature_name_html(card)}</span>
            <span class="feature-sub">{swing_word} in a day</span>
          </div>
        </div>"""
    else:
        wildest_block = '<p class="empty">No 1-day data.</p>'

    tracked = stats.get("tracked", 0)
    gainers = stats.get("gainer_count", 0)
    losers = stats.get("loser_count", 0)
    gain_share = (gainers / tracked * 100) if tracked else 0

    return f"""
      <aside class="side">
        <section class="panel panel-hero">
          <h2>Priciest Pikachu</h2>
          {priciest_block}
          <p class="panel-note">The most expensive of the {tracked} cards tracked.</p>
        </section>

        <section class="panel panel-hero">
          <h2>Wildest 24h swing</h2>
          {wildest_block}
          <p class="panel-note">The sharpest one-day move — a single sale can cause it.</p>
        </section>

        <section class="panel">
          <h2>Market pulse</h2>
          <div class="pulse">
            <div class="pulse-bar">
              <span class="pulse-up" style="width:{gain_share:.1f}%"></span>
            </div>
            <div class="pulse-legend">
              <span><b class="up">{gainers}</b> rising</span>
              <span><b class="down">{losers}</b> falling</span>
            </div>
          </div>
          <p class="panel-note">Across {tracked} Pikachu cards with usable Cardmarket pricing
          ({stats.get('skipped', 0)} skipped for missing data or a sub-€1 average).</p>
        </section>
      </aside>"""


def render_html_report(data):
    """Render the Pikachu price board as a self-contained HTML page.

    `data` is the dict returned by get_gainers_and_losers(): gainers, losers
    and summary stats. Written straight to docs/index.html for GitHub Pages.
    """
    gainers = data["gainers"]
    losers = data["losers"]
    stats = data.get("stats", {})

    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    top_gain = gainers[0]["pct_change_month"] if gainers else None
    top_loss = losers[0]["pct_change_month"] if losers else None

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pikachu Card Prices</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    color-scheme: light;
    --plane: #f4f4f2;
    --surface: #fcfcfb;
    --ink: #0b0b0b;
    --ink-2: #52514e;
    --muted: #898781;
    --hairline: rgba(11,11,11,0.10);
    --row-hover: rgba(11,11,11,0.035);
    --up: #006300;
    --up-mark: #0ca30c;
    --up-wash: rgba(12,163,12,0.12);
    --down: #c22a2a;
    --down-mark: #d03b3b;
    --down-wash: rgba(208,59,59,0.12);
    --accent: #f6c945;
    --accent-ink: #3a2f00;
    --chip: rgba(137,135,129,0.16);
    --shadow: 0 1px 2px rgba(11,11,11,0.05), 0 6px 20px rgba(11,11,11,0.06);
    /* Panels sit on the card-art backdrop, so they need to be near-opaque to
       stay readable while still letting a little of it through. */
    --panel: rgba(252,252,251,0.90);
    --backdrop-opacity: 0.30;
    --backdrop-veil: rgba(244,244,242,0.55);
    /* Deeper amber on the light plane -- the bright yellow used in dark mode
       is close to invisible on a near-white background. */
    --title-grad: linear-gradient(96deg, #c98500 4%, #d2691a 52%, #c2410c 96%);
    --title-shadow: none;
    /* One size for every piece of card art on the page -- table rows and
       sidebar features alike. Card aspect ratio is 245:342. */
    --art-w: 96px;
    --art-h: 134px;
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
      --row-hover: rgba(255,255,255,0.045);
      --up: #23c723;
      --up-mark: #0ca30c;
      --up-wash: rgba(12,163,12,0.16);
      --down: #ef7676;
      --down-mark: #d03b3b;
      --down-wash: rgba(208,59,59,0.18);
      --chip: rgba(195,194,183,0.14);
      --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 6px 20px rgba(0,0,0,0.35);
      --panel: rgba(26,26,25,0.88);
      --backdrop-opacity: 0.22;
      --backdrop-veil: rgba(13,13,13,0.55);
      --title-grad: linear-gradient(96deg, #f6c945 6%, #ffb020 46%, #ff8a3d 92%);
      --title-shadow: drop-shadow(0 2px 5px rgba(0,0,0,0.35));
    }}
  }}

  * {{ box-sizing: border-box; }}
  html, body {{ height: 100%; }}
  body {{
    margin: 0; background: var(--plane); color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    font-size: 14px; line-height: 1.4;
  }}

  /* Card art backdrop: fixed, blurred and dialled well back, with a veil on
     top, so it reads as texture behind the data rather than competing with
     it. Two layers instead of one so the blur can't wash out the veil. */
  body::before, body::after {{
    content: ""; position: fixed; inset: -40px; z-index: -2; pointer-events: none;
  }}
  body::before {{
    background: url("{BACKDROP_IMAGE_URL}") center 35% / cover no-repeat;
    opacity: var(--backdrop-opacity);
    filter: blur(3px) saturate(1.15);
  }}
  body::after {{ background: var(--backdrop-veil); z-index: -1; }}
  .wrap {{
    max-width: 1240px; min-height: 100%; margin: 0 auto;
    padding: 18px 22px 14px; display: flex; flex-direction: column; gap: 12px;
  }}

  /* ---------- header ---------- */
  .head {{ display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 16px 28px; }}
  .title-block {{ flex: 1 1 460px; min-width: 0; }}
  .head h1 {{
    margin: 0;
    font-family: Fredoka, "Trebuchet MS", system-ui, sans-serif;
    font-weight: 700; font-size: clamp(30px, 3.4vw, 44px);
    letter-spacing: -0.015em; line-height: 1.05;
    background: var(--title-grad);
    -webkit-background-clip: text; background-clip: text; color: transparent;
    -webkit-text-fill-color: transparent;
    filter: var(--title-shadow);
  }}
  /* The description runs the full width of the title block rather than
     wrapping early in a narrow column. */
  .head p {{ margin: 6px 0 0; color: var(--ink-2); font-size: 14.5px; max-width: none; }}
  /* Sized in em so the face tracks the responsive title, and nudged onto the
     text baseline. It sits inside the gradient-clipped h1, so the fill has to
     be reset or the artwork inherits transparent text fill. */
  .face {{
    height: 1.05em; width: 1.05em; vertical-align: -0.17em;
    -webkit-text-fill-color: initial;
    filter: drop-shadow(0 2px 6px rgba(246,201,69,0.45));
  }}
  .head-stats {{ display: flex; gap: 8px; align-items: stretch; }}
  .kpi {{
    background: var(--panel); border: 1px solid var(--hairline); border-radius: 10px;
    backdrop-filter: blur(6px);
    padding: 8px 13px; min-width: 96px; box-shadow: var(--shadow);
  }}
  .kpi span {{ display: block; font-size: 10px; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); }}
  .kpi b {{ font-size: 18px; font-weight: 680; font-variant-numeric: tabular-nums; }}
  .kpi.stamp b {{ font-size: 12.5px; font-weight: 560; color: var(--ink-2); }}

  /* ---------- layout ---------- */
  .main {{ display: grid; grid-template-columns: minmax(0,1fr) 340px; gap: 16px; flex: 1; align-items: start; }}
  .board-card {{
    background: var(--panel); border: 1px solid var(--hairline); border-radius: 12px;
    backdrop-filter: blur(6px);
    box-shadow: var(--shadow); overflow: hidden; height: 100%;
    display: flex; flex-direction: column;
  }}

  /* ---------- tabs (CSS-only) ---------- */
  .tabin {{ position: absolute; opacity: 0; pointer-events: none; }}
  .tabs {{ display: flex; gap: 4px; padding: 10px 12px 0; border-bottom: 1px solid var(--hairline); }}
  .tabs label {{
    padding: 8px 14px; border-radius: 8px 8px 0 0; cursor: pointer;
    font-size: 13px; font-weight: 620; color: var(--muted);
    border: 1px solid transparent; border-bottom: none; margin-bottom: -1px;
  }}
  .tabs label:hover {{ color: var(--ink); background: var(--row-hover); }}
  .panel-body {{ display: none; flex: 1; min-height: 0; }}
  #t-gain:checked ~ .tabs label[for="t-gain"],
  #t-lose:checked ~ .tabs label[for="t-lose"] {{
    color: var(--ink); background: var(--panel);
    border-color: var(--hairline); border-bottom: 1px solid transparent;
  }}
  #t-gain:checked ~ .body-gain, #t-lose:checked ~ .body-lose {{ display: block; }}
  .tabin:focus-visible ~ .tabs label[for="t-gain"],
  .tabin:focus-visible ~ .tabs label[for="t-lose"] {{ outline: 2px solid var(--accent); outline-offset: -2px; }}

  /* ---------- table ---------- */
  /* height:100% lets the five rows share out the leftover vertical space, so
     the board fills the page instead of leaving a gap under a short list. */
  .board {{ width: 100%; height: 100%; border-collapse: collapse; }}
  .board th {{
    text-align: left; padding: 9px 16px; font-size: 10px; font-weight: 620;
    letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted);
    border-bottom: 1px solid var(--hairline); white-space: nowrap;
    height: 34px;
  }}
  .board td {{ padding: 7px 16px; border-bottom: 1px solid var(--hairline); vertical-align: middle; }}
  .board tbody tr {{ height: calc(var(--art-h) + 14px); }}
  .board tbody tr:last-child td {{ border-bottom: none; }}
  .board tbody tr:hover {{ background: var(--row-hover); }}
  .c-num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .c-rank {{ width: 30px; color: var(--muted); font-variant-numeric: tabular-nums; font-size: 12px; }}

  .c-card {{ min-width: 0; }}
  /* the flex lives on an inner span, not the td -- a flex td drops out of
     table layout and the row borders stop lining up */
  .c-card .cell {{ display: flex; align-items: center; gap: 12px; }}
  .thumb {{
    width: var(--art-w); height: var(--art-h); object-fit: contain;
    border-radius: 6px; background: var(--chip); flex: none;
    box-shadow: 0 2px 6px rgba(0,0,0,0.22);
  }}
  .thumb-empty {{
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 5px; box-shadow: none; border: 1px dashed var(--hairline);
    color: var(--muted); text-align: center;
  }}
  .thumb-bolt {{ font-size: 24px; opacity: .5; }}
  .thumb-label {{ font-size: 11px; line-height: 1.25; }}
  .card-id {{ display: flex; flex-direction: column; min-width: 0; }}
  .card-set {{ font-weight: 620; font-size: 15px; letter-spacing: -0.01em; }}
  /* A card name that links out to its own Cardmarket page, so any price
     here is one click from independent verification. Inherits the row's ink
     rather than looking like a generic blue link -- the underline and arrow
     are what mark it clickable. */
  .set-link {{ color: inherit; text-decoration: none; border-bottom: 1px dotted var(--muted); }}
  .set-link:hover {{ border-bottom-style: solid; }}
  .out-icon {{ font-size: 0.75em; margin-left: 3px; color: var(--muted); }}
  .card-sub {{ font-size: 11.5px; color: var(--muted); display: flex; align-items: center; gap: 5px; margin-top: 1px; font-variant-numeric: tabular-nums; }}
  .chip {{ background: var(--chip); color: var(--ink-2); padding: 0 6px; border-radius: 999px; font-size: 10.5px; font-weight: 600; }}
  .chip-new {{ background: var(--accent); color: var(--accent-ink); font-weight: 800; letter-spacing: 0.03em; }}

  /* "A month ago" is deliberately quieter than "Price now" -- the eye should
     land on today's price, with the old one as context beside it. */
  .c-was {{ color: var(--muted); font-size: 14px; font-weight: 500; }}
  .c-now {{ font-weight: 680; font-size: 16.5px; }}
  .pill {{
    display: inline-block; padding: 5px 11px; border-radius: 7px;
    font-weight: 700; font-size: 15px; font-variant-numeric: tabular-nums;
  }}
  .pill.up {{ background: var(--up-wash); color: var(--up); }}
  .pill.down {{ background: var(--down-wash); color: var(--down); }}

  /* ---------- sidebar ---------- */
  .side {{ display: flex; flex-direction: column; gap: 12px; }}
  .panel {{
    background: var(--panel); border: 1px solid var(--hairline); border-radius: 12px;
    padding: 13px 14px; box-shadow: var(--shadow); backdrop-filter: blur(6px);
  }}
  .panel h2 {{ margin: 0 0 10px; font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }}
  .panel-hero {{ padding: 16px 18px 15px; }}
  .panel-hero h2 {{ font-size: 11.5px; margin-bottom: 12px; }}
  .feature {{ display: flex; gap: 16px; align-items: center; }}
  .feature .thumb {{ border-radius: 7px; box-shadow: 0 2px 6px rgba(0,0,0,0.28); }}
  .feature-text {{ display: flex; flex-direction: column; min-width: 0; }}
  .feature-value {{
    font-family: Fredoka, "Trebuchet MS", system-ui, sans-serif;
    font-size: 34px; font-weight: 600; letter-spacing: -0.02em; line-height: 1.05;
    font-variant-numeric: tabular-nums;
  }}
  .feature-value.up {{ color: var(--up); }}
  .feature-value.down {{ color: var(--down); }}
  .feature-name {{ font-size: 15px; font-weight: 650; margin-top: 6px; line-height: 1.25; }}
  .feature-sub {{ font-size: 12.5px; color: var(--muted); margin-top: 2px; }}
  .panel-note {{ margin: 10px 0 0; font-size: 10.5px; line-height: 1.45; color: var(--muted); }}

  .pulse-bar {{ height: 7px; border-radius: 999px; background: var(--down-mark); overflow: hidden; }}
  .pulse-up {{ display: block; height: 100%; background: var(--up-mark); }}
  .pulse-legend {{ display: flex; justify-content: space-between; margin-top: 7px; font-size: 12px; color: var(--ink-2); }}
  .pulse-legend b {{ font-variant-numeric: tabular-nums; }}
  b.up {{ color: var(--up); }} b.down {{ color: var(--down); }}

  .empty {{ color: var(--muted); font-size: 12.5px; margin: 4px 0; }}

  /* ---------- footer ---------- */
  .foot {{ color: var(--muted); font-size: 10.5px; line-height: 1.5; margin: 0; }}
  .foot a {{ color: inherit; }}

  @media (max-width: 900px) {{
    .main {{ grid-template-columns: 1fr; }}
    .side {{ flex-direction: row; flex-wrap: wrap; }}
    .side .panel {{ flex: 1 1 220px; }}
    .side .panel-hero {{ flex: 1 1 260px; }}
  }}
  @media (max-width: 560px) {{
    .c-was, .board th:nth-child(3) {{ display: none; }}
    .head h1 {{ font-size: 22px; }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <header class="head">
      <div>
        <h1>{_pikachu_face_svg()} Pikachu Card Prices</h1>
        <p>The Pikachu cards that went up and down the most in the last month. All prices in euros, from Cardmarket.</p>
      </div>
      <div class="head-stats">
        <div class="kpi"><span>Biggest gain</span><b class="up">{_fmt_pct(top_gain)}</b></div>
        <div class="kpi"><span>Biggest drop</span><b class="down">{_fmt_pct(top_loss)}</b></div>
        <div class="kpi stamp"><span>Last updated</span><b>{generated_at}</b></div>
      </div>
    </header>

    <div class="main">
      <div class="board-card">
        <input class="tabin" type="radio" name="board" id="t-gain" checked>
        <input class="tabin" type="radio" name="board" id="t-lose">
        <div class="tabs">
          <label for="t-gain">Top gainers</label>
          <label for="t-lose">Top losers</label>
        </div>
        <div class="panel-body body-gain">{_table_html(gainers, "No cards gained this period.")}</div>
        <div class="panel-body body-lose">{_table_html(losers, "No cards fell this period.")}</div>
      </div>

      {_aside_html(stats)}
    </div>

    <footer class="foot">
      Every figure is a Cardmarket selling price in euros, so the three columns always agree:
      <strong>a month ago</strong> is the 30-day average, <strong>price now</strong> is the 7-day average, and
      <strong>change</strong> is the difference between them. These are rolling averages, not condition-filtered --
      a low-volume card's average can still be skewed by a handful of sales. Card names link out to their own
      Cardmarket page, so any number here is one click from a second opinion. Cards averaging under €1.00, missing
      Cardmarket averages, or individually verified as unreliable are left out. Data from
      <a href="https://tcgdex.dev/">TCGdex</a>.
    </footer>
  </div>
</body>
</html>
"""
