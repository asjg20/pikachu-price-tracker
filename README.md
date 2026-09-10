# Pikachu Monthly Price Mover Tracker

Finds the Pikachu-named Pokémon cards whose price has moved the most (up or
down) over roughly the past month, using the free [TCGdex](https://tcgdex.dev/)
API (no API key required). Delivered two ways:

- **`Pikachu_Movers.ipynb`** — a notebook you run manually to see a styled table
  and a bar chart of the movers.
- **GitHub Actions** — a scheduled (or on-demand, via a button) workflow that
  regenerates the report and publishes it to GitHub Pages.

The published page is a price board in the style of a stock screener: two tabs
(**Top gainers** / **Top losers**), five rows each showing what the card cost a
month ago, what it costs now, and the change between them — plus a sidebar of
summary stats.

**Every figure on the page is a Cardmarket price in euros.** An earlier version
put a TCGplayer USD price next to a Cardmarket EUR percentage, which made each
row claim something untrue: "Pokémon Rumble #7 — $750.00 — +155.8%" read as
*this $750 card rose 155.8%*, when the $750 and the 155.8% came from different
marketplaces in different currencies. Cardmarket is the only source here with
price history, so it supplies every number, and the three columns reconcile by
eye (€731.03 → €1,869.99 is +155.8%).

## How the ranking works

TCGdex returns pricing from two marketplaces, and they're used very differently:

- **TCGplayer** (`pricing.tcgplayer`) is a point-in-time snapshot with no
  built-in history — no way to know what a card cost a month ago from this
  block alone. It's used **only** as the displayed USD price, read from the
  card's **base printing** (`normal` > `holofoil` > `reverse-holofoil` >
  `1st-edition-holofoil` > `1st-edition` > `unlimited`).

  **`normal` is first on purpose.** The ranking uses Cardmarket's base
  (non-holo) average track, so the displayed price has to be the base
  printing or the two describe different objects. Preferring a holo variant
  caused a real bug: Legendary Collection Pikachu (`lc-86`) has a
  `reverse-holofoil` block whose only listings are $4,999.99 outliers
  (marketPrice $1,574.99) sitting next to a `normal` printing at $6.38 — the
  report showed **$1,574.99** for a card TCGplayer lists at **$6.38**. Cards
  that only exist as holo (most modern ones) still fall through to
  `holofoil` and are unaffected.
- **Cardmarket** (`pricing.cardmarket`) exposes `avg1` / `avg7` / `avg30` —
  rolling trailing averages in **EUR**. These *are* usable as a
  moving-average crossover, the same idea as comparing a short vs. long
  moving average for a stock:

  ```
  pct_change_month = (avg7 - avg30) / avg30 * 100
  ```

  **Why `avg7`, not `avg1`, as specified in the original brief:** `avg1` is a
  single day's worth of sales, so it's dominated by one-sale noise. In a live
  check, one card showed **+182%** on `avg1` vs. only **+4%** on `avg7` for
  the exact same card — one optimistic listing swung the "monthly" number by
  178 points. `avg7` still updates far faster than a real 30-day trend would,
  but it isn't wrecked by a single outlier sale the way `avg1` is. The
  `avg1`-vs-`avg30` figure is still computed and shown as a secondary
  "last 24h" column, in case a very recent spike is itself interesting.

- **Selection**: the board splits gainers and losers into **two tabs of five**
  (`get_gainers_and_losers`), each sorted by plain percent change. Splitting by
  direction is what makes a plain sort correct: percent change is bounded at
  −100% but unbounded upward, so ranking gainers and losers *together* by
  `abs(pct)` structurally favours gainers — a live sample of 169 priced cards
  had 92 fallers vs. 74 risers, yet a combined abs-% top 10 came out 100%
  gainers. Within a single-direction list that bias can't arise.

  `get_top_movers` (used by the notebook) still returns one combined list, and
  for that case it ranks by `abs(ln(avg7/avg30))`, which treats a halving and a
  doubling as equally large. Either way **the displayed percentage is always the
  plain signed value** — only the sort order ever uses the log-ratio.
- **De-duplication**: TCGdex sometimes lists one physical card under several
  ids that map to the same Cardmarket product — `xyp-XY95` and `xyp-XY202` both
  resolve to `idProduct` 289809 and carry byte-identical pricing, so they
  appeared as two identical rows eating two of five slots. Cards are collapsed
  by `cm_product_id`, keeping whichever record is most complete (a USD price and
  artwork beat neither). A typical run goes 157 rankable → 149 after this.
- **Price floor**: cards whose 30-day average is under `MIN_AVG30_EUR`
  (€1.00, in `pikachu_core.py`) are excluded. Below that, a few cents of
  movement produces a triple-digit percentage swing that isn't economically
  meaningful (e.g. a bulk common going from €0.24 to €1.40 reads as +483%).
- **"NEW!" badge**: a card whose *set* released within the last 30 days
  (`NEW_CARD_WINDOW_DAYS` in `pikachu_core.py`) gets a "NEW!" badge in
  the report and notebook. This is looked up lazily — only for whichever
  cards make the final top 10 — via `GET /v2/en/sets/{id}`, which returns a
  `releaseDate`; unlike the per-card pricing lookups, this uses only a
  handful of extra requests since many movers share the same set.
- **Card identity**: every card in the report is a Pikachu, so repeating the
  word in every row carries no information. The **set** is the headline
  instead, with the card number beneath it and only the *distinguishing*
  part of the name kept as a chip (`Ash's Pikachu` → `Ash's`,
  `Pikachu V-UNION` → `V-UNION`, plain `Pikachu` → nothing).
- **Columns**: `A month ago` is the 30-day average, `Price now` is the 7-day
  average, `Change` is the difference. Nothing on the page needs a footnote to
  be believed — the arithmetic is visible in the row.
- **Verify-it-yourself links**: every card name (row or sidebar) links out to
  its own Cardmarket product page, via `?idProduct=<id>` — a documented
  Cardmarket cross-site linking convention (the same one Scryfall uses for
  Magic cards), built from the `idProduct` field already present in the
  pricing response. Cardmarket blocks automated requests outright (403 on
  every path), so this couldn't be confirmed by fetching it — it's built from
  that published format, not a live check. Given the data-quality issues
  below, this exists so no number has to be taken on faith.

### The sidebar stats

**There is no sales-volume or transaction data in this API** — Cardmarket
exposes only `avg`/`low`/`trend`/`avg1`/`avg7`/`avg30` and TCGplayer only
`low`/`mid`/`high`/`market`/`directLow` price points. So "most traded" or
"most transactions" cannot be built here without inventing it. The sidebar
shows what the data genuinely supports instead:

- **Priciest Pikachu** — the highest current Cardmarket price among all tracked cards.
- **Wildest 24h swing** — largest gap between a card's 1-day and 7-day averages.
  This is the one job `avg1` is actually good for: too noisy to rank a monthly
  trend on, but that same sensitivity makes it a decent "something happened to
  this card yesterday" detector.
- **Market pulse** — how many of the tracked cards are rising vs. falling.

### Known limitations (stated plainly, not hidden)

- `avg7`/`avg30` are **rolling** trailing averages, not "the price exactly 7
  or 30 days ago" — this is an approximation of a monthly move, not an exact
  one.
- Prices are **European** (Cardmarket, in EUR) because that is the only source
  in this API with price history. US prices on TCGplayer can differ
  substantially for the same card — the board is not a guide to what something
  sells for in the States.
- Cards missing `cardmarket.avg7` or `cardmarket.avg30` are skipped, not
  treated as 0% — the number of skips is logged on every run so it's
  visible rather than silent (a typical run skips ~50 of ~207 Pikachu cards,
  mostly promos with no Cardmarket listings).
- **Cardmarket's feed carries no condition or grade information at all** —
  every `avg`/`avg1`/`avg7`/`avg30` field is a blend across whatever
  condition sold, so these numbers cannot be assumed to be mint (or any
  specific grade). This is also the likely cause of the next point: a single
  high-value sale — plausibly a graded slab mixed into an otherwise raw-card
  average — can dominate a low-volume card's short window.
- **Some cards' Cardmarket data is simply wrong**, verified against outside
  trackers rather than assumed. Pokémon Rumble Pikachu (`ru1-7`) showed an
  `avg7` of €1,869.99 against a TCGplayer market price around $750 and Troll
  & Toad retail of $169.99; Special Delivery Pikachu (`swshp-SWSH074`) showed
  €1,511.93 against independent trackers' $250–455 range — and against its
  *own* holo Cardmarket track (€377.32), which roughly agrees with them. Both
  are excluded by id via `KNOWN_BAD_CARDS` in `pikachu_core.py`.

  This is a **curated list, not a filter**, because no statistical shortcut
  works here: the ratio between a card's avg1/avg7/avg30 was tested as a
  general "this data looks wrong" signal and rejected — across the full
  ~150-card set it forms one smooth continuum with no gap between the two
  confirmed-bad cards and cards with no evidence of a problem (a verified-bad
  card sat at 2.25x while an unremarkable neighbor sat at 2.21x). So the list
  only ever grows by someone actually checking a number against an outside
  source — it won't auto-catch a new bad card next week, and that's a real
  tradeoff, not an oversight.
- Card art comes from TCGdex's image CDN. A few cards have no artwork on file
  upstream; those rows show a placeholder rather than a broken image. Roughly
  5% of cards serve `/high.png` but **not** `/high.webp`, and a `<picture>`
  fallback does not cover a 404 — so `_resolve_image_url` HEAD-checks WebP once per
  displayed card (cached) and falls back to PNG, rather than shipping broken
  images for that 5%.
- The page backdrop (`docs/assets/backdrop.jpg`, not from TCGdex) is blurred
  and held at low opacity behind a veil (`--backdrop-opacity`,
  `--backdrop-veil`), with panels near-opaque on top so text stays readable.
- TCGdex occasionally returns a transient 5xx. Since the workflow runs
  unattended, requests retry with backoff (`MAX_RETRIES`), and a failed
  set-release lookup degrades to "no NEW badge" instead of failing the run —
  a cosmetic badge is never worth losing the whole report over. Genuine 4xx
  responses are not retried.

## Files

| File | Purpose |
|---|---|
| `pikachu_core.py` | All shared logic: fetching, ranking, HTML rendering. No dependency beyond `requests`. |
| `test_pikachu_core.py` | 54 unit tests (`unittest` + mocked `requests.get` — no real network calls). |
| `Pikachu_Movers.ipynb` | Manual notebook: styled DataFrame + bar chart. |
| `generate_report.py` | Automation entry point: fetch → write `docs/index.html`. |
| `docs/assets/backdrop.jpg` | Page background art (not from TCGdex). Static file, untouched by every regeneration. |
| `.github/workflows/weekly-report.yml` | Runs `generate_report.py` weekly and on-demand. |
| `docs/index.html` | The published report (GitHub Pages serves this). Regenerated by every run. |

## Running the notebook

```bash
pip install -r requirements-notebook.txt
jupyter notebook Pikachu_Movers.ipynb
```

Run all cells. The first code cell fetches live data from TCGdex (~200 API
calls, politely spaced ~0.15s apart, so it takes a couple of minutes).

## Running the tests

```bash
pip install -r requirements.txt
python -m unittest test_pikachu_core.py -v
```

All 54 tests mock `requests.get` — no network access needed, and none of the
real API's rate limits are touched.

## Running the report generator locally

```bash
pip install -r requirements.txt
python generate_report.py
```

Writes `docs/index.html`.

## Setting up the automated (GitHub Pages) version

### 1. Enable GitHub Pages

**Settings → Pages** → under "Build and deployment", set **Source** to
"Deploy from a branch", **Branch** to your repo's default branch (`master`
unless you've renamed it) / `/docs`. After the first workflow run (or after
you push the locally-generated `docs/index.html`), your report will be live
at `https://<username>.github.io/<repo>/`.

### 2. Run it

- **On a schedule**: the workflow runs weekly (Mondays at 13:00 UTC by
  default — edit the `cron` line in `.github/workflows/weekly-report.yml` to
  change it).
- **On demand**: go to the repo's **Actions** tab → "Weekly Pikachu Price
  Movers Report" → **Run workflow** button.

Each run regenerates `docs/index.html` and commits it back to the repo only
if it changed.
