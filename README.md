# Pikachu Monthly Price Mover Tracker

Finds the 10 Pikachu-named Pokémon cards whose price has moved the most (up or
down) over roughly the past month, using the free [TCGdex](https://tcgdex.dev/)
API (no API key required). Delivered two ways:

- **`Pikachu_Movers.ipynb`** — a notebook you run manually to see a styled table
  and a bar chart of the movers.
- **GitHub Actions** — a scheduled (or on-demand, via a button) workflow that
  regenerates the report and publishes it to GitHub Pages.

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

- **Selection**: the top 10 are *not* chosen by raw `abs(pct_change_month)`.
  Percent change is bounded at -100% but unbounded upward, so a naive
  absolute-percent ranking structurally favors gainers — a live sample of 169
  priced cards had 92 droppers vs. 74 gainers, yet a raw abs-% top 10 was
  100% gainers. Instead, cards are ranked by `abs(ln(avg7/avg30))`, which
  treats a halving and a doubling as equally large moves. **The displayed
  percentage is always the plain signed value** — only the sort order uses
  the log-ratio.
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

### Known limitations (stated plainly, not hidden)

- `avg7`/`avg30` are **rolling** trailing averages, not "the price exactly 7
  or 30 days ago" — this is an approximation of a monthly move, not an exact
  one.
- The ranking metric is in **EUR** (Cardmarket), while the displayed price is
  in **USD** (TCGplayer) — the two aren't on the same currency or the same
  marketplace, and a card can be popular on one and quiet on the other.
- Cards missing `cardmarket.avg7` or `cardmarket.avg30` are skipped, not
  treated as 0% — the number of skips is logged on every run so it's
  visible rather than silent (a typical run skips ~50 of ~207 Pikachu cards,
  mostly promos with no Cardmarket listings).
- Card art comes from TCGdex's image CDN. A few cards (mostly older promos,
  e.g. Special Delivery Pikachu) have no artwork on file upstream; those
  tiles show a labelled placeholder rather than a broken image.

## Files

| File | Purpose |
|---|---|
| `pikachu_core.py` | All shared logic: fetching, ranking, HTML rendering. No dependency beyond `requests`. |
| `test_pikachu_core.py` | Unit tests (`unittest` + mocked `requests.get` — no real network calls). |
| `Pikachu_Movers.ipynb` | Manual notebook: styled DataFrame + bar chart. |
| `generate_report.py` | Automation entry point: fetch → write `docs/index.html`. |
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

All 19 tests mock `requests.get` — no network access needed, and none of the
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
