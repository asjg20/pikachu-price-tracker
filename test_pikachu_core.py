"""
Unit tests for pikachu_core. All network calls are mocked via
unittest.mock.patch on pikachu_core.requests.get -- no real HTTP happens here.
"""

import datetime as dt
import unittest
import requests
from unittest.mock import MagicMock, patch

import pikachu_core


def _response(json_data, status_code=200):
    """Build a fake requests.Response-like object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code < 400:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"{status_code}", response=resp
        )
    return resp


def _card_detail(name="Pikachu", set_id="swsh4", set_name="Vivid Voltage",
                  local_id="44", cardmarket=None, tcgplayer=None):
    pricing = {}
    if cardmarket is not None:
        pricing["cardmarket"] = cardmarket
    if tcgplayer is not None:
        pricing["tcgplayer"] = tcgplayer
    return {
        "id": f"{set_id}-{local_id}",
        "name": name,
        "localId": local_id,
        "set": {"id": set_id, "name": set_name},
        "pricing": pricing,
    }


class FetchAllPikachuCardIdsTests(unittest.TestCase):
    @patch("pikachu_core.requests.get")
    def test_returns_card_ids_from_list_endpoint(self, mock_get):
        mock_get.return_value = _response([
            {"id": "swsh4-44", "localId": "44", "name": "Pikachu VMAX"},
            {"id": "basep-1", "localId": "1", "name": "Pikachu"},
        ])
        ids = pikachu_core.fetch_all_pikachu_card_ids()
        self.assertEqual(ids, ["swsh4-44", "basep-1"])
        mock_get.assert_called_once()
        self.assertIn("name", mock_get.call_args.kwargs.get("params", {}))


class FetchCardPricingTests(unittest.TestCase):
    def setUp(self):
        pikachu_core._set_release_date_cache.clear()

    @patch("pikachu_core.requests.get")
    def test_pct_change_month_math_is_correct(self, mock_get):
        mock_get.return_value = _response(_card_detail(
            cardmarket={"avg1": 9.95, "avg7": 9.97, "avg30": 9.35},
        ))
        card = pikachu_core.fetch_card_pricing("swsh4-44")
        self.assertIsNotNone(card)
        expected_pct_month = (9.97 - 9.35) / 9.35 * 100
        expected_pct_24h = (9.95 - 9.35) / 9.35 * 100
        self.assertAlmostEqual(card["pct_change_month"], expected_pct_month)
        self.assertAlmostEqual(card["pct_change_24h"], expected_pct_24h)

    @patch("pikachu_core.requests.get")
    def test_card_missing_cardmarket_block_is_excluded(self, mock_get):
        mock_get.return_value = _response(_card_detail(cardmarket=None))
        card = pikachu_core.fetch_card_pricing("basep-1")
        self.assertIsNone(card)

    @patch("pikachu_core.requests.get")
    def test_card_missing_avg30_is_excluded_not_crashed_on(self, mock_get):
        mock_get.return_value = _response(_card_detail(
            cardmarket={"avg1": 5.0, "avg7": 5.0, "avg30": None},
        ))
        card = pikachu_core.fetch_card_pricing("swsh4-44")
        self.assertIsNone(card)

    @patch("pikachu_core.requests.get")
    def test_card_missing_avg7_is_excluded_not_crashed_on(self, mock_get):
        mock_get.return_value = _response(_card_detail(
            cardmarket={"avg1": 5.0, "avg7": None, "avg30": 5.0},
        ))
        card = pikachu_core.fetch_card_pricing("swsh4-44")
        self.assertIsNone(card)

    @patch("pikachu_core.requests.get")
    def test_card_below_price_floor_is_excluded(self, mock_get):
        mock_get.return_value = _response(_card_detail(
            cardmarket={"avg1": 1.40, "avg7": 0.30, "avg30": 0.24},
        ))
        card = pikachu_core.fetch_card_pricing("basep-1")
        self.assertIsNone(card)

    @patch("pikachu_core.requests.get")
    def test_card_at_or_above_price_floor_is_included(self, mock_get):
        mock_get.return_value = _response(_card_detail(
            cardmarket={"avg1": 1.10, "avg7": 1.05, "avg30": 1.00},
        ))
        card = pikachu_core.fetch_card_pricing("basep-1")
        self.assertIsNotNone(card)

    @patch("pikachu_core.requests.get")
    def test_usd_display_price_prefers_base_printing_over_holo_variants(self, mock_get):
        mock_get.return_value = _response(_card_detail(
            cardmarket={"avg1": 10, "avg7": 10, "avg30": 10},
            tcgplayer={
                "unit": "USD",
                "normal": {"marketPrice": 1.0},
                "holofoil": {"marketPrice": 5.0},
                "reverse-holofoil": {"marketPrice": 8.0},
            },
        ))
        card = pikachu_core.fetch_card_pricing("swsh4-44")
        self.assertEqual(card["usd_display_price"], 1.0)
        self.assertEqual(card["usd_price_variant"], "normal")

    def test_regression_holo_outlier_does_not_override_base_printing(self):
        # Legendary Collection Pikachu (lc-86) really does return a
        # reverse-holofoil block whose only listings are $4,999.99 outliers
        # alongside a normal printing at $6.38. The report must show $6.38 --
        # the base printing, matching the Cardmarket track we rank on.
        price, variant = pikachu_core._pick_usd_display_price({
            "unit": "USD",
            "normal": {"lowPrice": 2.25, "midPrice": 4.81, "marketPrice": 6.38},
            "reverse-holofoil": {"lowPrice": 4999.99, "midPrice": 4999.99,
                                 "marketPrice": 1574.99},
        })
        self.assertEqual(price, 6.38)
        self.assertEqual(variant, "normal")

    @patch("pikachu_core.requests.get")
    def test_usd_display_price_falls_back_when_base_printing_absent(self, mock_get):
        # A holo-only card (no "normal" block) still gets a price.
        mock_get.return_value = _response(_card_detail(
            cardmarket={"avg1": 10, "avg7": 10, "avg30": 10},
            tcgplayer={"unit": "USD", "holofoil": {"marketPrice": 12.92}},
        ))
        card = pikachu_core.fetch_card_pricing("swsh4-44")
        self.assertEqual(card["usd_display_price"], 12.92)
        self.assertEqual(card["usd_price_variant"], "holofoil")

    @patch("pikachu_core.requests.get")
    def test_usd_display_price_is_none_when_tcgplayer_absent(self, mock_get):
        mock_get.return_value = _response(_card_detail(
            cardmarket={"avg1": 10, "avg7": 10, "avg30": 10},
            tcgplayer=None,
        ))
        card = pikachu_core.fetch_card_pricing("swsh4-44")
        self.assertIsNone(card["usd_display_price"])
        self.assertIsNone(card["usd_price_variant"])


class VariantLabelTests(unittest.TestCase):
    def test_plain_pikachu_has_no_variant_label(self):
        self.assertEqual(pikachu_core.variant_label("Pikachu"), "")

    def test_distinguishing_words_are_kept(self):
        cases = {
            "Pikachu V-UNION": "V-UNION",
            "Ash's Pikachu": "Ash's",
            "Special Delivery Pikachu": "Special Delivery",
            "Flying Pikachu": "Flying",
            "Pikachu VMAX": "VMAX",
            "Pikachu on the Ball": "on the Ball",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(pikachu_core.variant_label(name), expected)

    def test_handles_missing_name(self):
        self.assertEqual(pikachu_core.variant_label(None), "")


class IsRecentlyReleasedTests(unittest.TestCase):
    def test_within_window_is_new(self):
        today = dt.date(2026, 9, 8)
        released = "2026-08-20"  # 19 days ago
        self.assertTrue(pikachu_core._is_recently_released(released, today=today))

    def test_exactly_at_window_boundary_is_new(self):
        today = dt.date(2026, 9, 8)
        released = "2026-08-09"  # exactly 30 days ago
        self.assertTrue(pikachu_core._is_recently_released(released, today=today, window_days=30))

    def test_outside_window_is_not_new(self):
        today = dt.date(2026, 9, 8)
        released = "2026-01-01"
        self.assertFalse(pikachu_core._is_recently_released(released, today=today))

    def test_missing_release_date_is_not_new(self):
        self.assertFalse(pikachu_core._is_recently_released(None))

    def test_unparseable_release_date_is_not_new(self):
        self.assertFalse(pikachu_core._is_recently_released("not-a-date"))


class GetTopMoversTests(unittest.TestCase):
    def setUp(self):
        pikachu_core._set_release_date_cache.clear()

    @patch("pikachu_core.time.sleep", return_value=None)
    @patch("pikachu_core.requests.get")
    def test_top_n_selected_by_absolute_log_ratio_sign_preserved(self, mock_get, mock_sleep):
        # 4 cards: two big movers (one up, one down), two small movers.
        # Log-ratio must pick the two big ones regardless of sign, and each
        # returned card must keep its own signed pct_change_month.
        list_response = _response([
            {"id": "big-up", "localId": "1", "name": "Big Up"},
            {"id": "big-down", "localId": "2", "name": "Big Down"},
            {"id": "small-up", "localId": "3", "name": "Small Up"},
            {"id": "small-down", "localId": "4", "name": "Small Down"},
        ])
        detail_responses = {
            "big-up": _card_detail(name="Big Up", set_id="setA", local_id="1",
                                    cardmarket={"avg1": 20, "avg7": 20, "avg30": 10}),  # +100%
            "big-down": _card_detail(name="Big Down", set_id="setA", local_id="2",
                                      cardmarket={"avg1": 5, "avg7": 5, "avg30": 10}),  # -50%
            "small-up": _card_detail(name="Small Up", set_id="setA", local_id="3",
                                      cardmarket={"avg1": 10.5, "avg7": 10.5, "avg30": 10}),  # +5%
            "small-down": _card_detail(name="Small Down", set_id="setA", local_id="4",
                                        cardmarket={"avg1": 9.5, "avg7": 9.5, "avg30": 10}),  # -5%
        }
        set_response = _response({"releaseDate": "2020-01-01"})

        def side_effect(url, *args, **kwargs):
            if url.endswith("/cards"):
                return list_response
            if "/sets/" in url:
                return set_response
            card_id = url.rsplit("/", 1)[-1]
            return _response(detail_responses[card_id])

        mock_get.side_effect = side_effect

        top = pikachu_core.get_top_movers(n=2)

        self.assertEqual(len(top), 2)
        names = {card["name"] for card in top}
        self.assertEqual(names, {"Big Up", "Big Down"})

        by_name = {card["name"]: card for card in top}
        self.assertAlmostEqual(by_name["Big Up"]["pct_change_month"], 100.0)
        self.assertAlmostEqual(by_name["Big Down"]["pct_change_month"], -50.0)

    @patch("pikachu_core.time.sleep", return_value=None)
    @patch("pikachu_core.requests.get")
    def test_skipped_cards_are_counted_not_crashed_on(self, mock_get, mock_sleep):
        list_response = _response([
            {"id": "good", "localId": "1", "name": "Good"},
            {"id": "no-pricing", "localId": "2", "name": "No Pricing"},
        ])
        detail_responses = {
            "good": _card_detail(name="Good", set_id="setA", local_id="1",
                                  cardmarket={"avg1": 12, "avg7": 12, "avg30": 10}),
            "no-pricing": _card_detail(name="No Pricing", set_id="setA", local_id="2",
                                        cardmarket=None),
        }
        set_response = _response({"releaseDate": "2020-01-01"})

        def side_effect(url, *args, **kwargs):
            if url.endswith("/cards"):
                return list_response
            if "/sets/" in url:
                return set_response
            card_id = url.rsplit("/", 1)[-1]
            return _response(detail_responses[card_id])

        mock_get.side_effect = side_effect

        top = pikachu_core.get_top_movers(n=10)
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["name"], "Good")

    @patch("pikachu_core.time.sleep", return_value=None)
    @patch("pikachu_core.requests.get")
    def test_new_badge_set_for_recently_released_set(self, mock_get, mock_sleep):
        list_response = _response([
            {"id": "fresh", "localId": "1", "name": "Fresh Pikachu"},
        ])
        detail = _card_detail(name="Fresh Pikachu", set_id="brand-new-set", local_id="1",
                               cardmarket={"avg1": 20, "avg7": 20, "avg30": 10})
        recent_release = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=5)).isoformat()
        set_response = _response({"releaseDate": recent_release})

        def side_effect(url, *args, **kwargs):
            if url.endswith("/cards"):
                return list_response
            if "/sets/" in url:
                return set_response
            return _response(detail)

        mock_get.side_effect = side_effect

        top = pikachu_core.get_top_movers(n=1)
        self.assertTrue(top[0]["is_new"])
        self.assertEqual(top[0]["release_date"], recent_release)


class GainersAndLosersTests(unittest.TestCase):
    def setUp(self):
        pikachu_core._set_release_date_cache.clear()

    @staticmethod
    def _wire(mock_get, cards):
        """cards: list of (id, name, avg1, avg7, avg30)."""
        list_response = _response([
            {"id": cid, "localId": "1", "name": name} for cid, name, *_ in cards
        ])
        details = {
            cid: _card_detail(name=name, set_id=f"set-{cid}", local_id="1",
                              cardmarket={"avg1": a1, "avg7": a7, "avg30": a30},
                              tcgplayer={"normal": {"marketPrice": a30}})
            for cid, name, a1, a7, a30 in cards
        }
        set_response = _response({"releaseDate": "2020-01-01"})

        def side_effect(url, *args, **kwargs):
            if url.endswith("/cards"):
                return list_response
            if "/sets/" in url:
                return set_response
            return _response(details[url.rsplit("/", 1)[-1]])

        mock_get.side_effect = side_effect

    @patch("pikachu_core.time.sleep", return_value=None)
    @patch("pikachu_core.requests.get")
    def test_splits_directions_and_sorts_each_by_percent(self, mock_get, mock_sleep):
        self._wire(mock_get, [
            ("big-up", "Pikachu A", 20, 20, 10),      # +100%
            ("small-up", "Pikachu B", 11, 11, 10),    # +10%
            ("small-down", "Pikachu C", 9, 9, 10),    # -10%
            ("big-down", "Pikachu D", 5, 5, 10),      # -50%
        ])
        data = pikachu_core.get_gainers_and_losers(n=2)

        self.assertEqual([c["id"] for c in data["gainers"]], ["big-up", "small-up"])
        self.assertEqual([c["id"] for c in data["losers"]], ["big-down", "small-down"])
        # Signs are preserved on each side.
        self.assertTrue(all(c["pct_change_month"] > 0 for c in data["gainers"]))
        self.assertTrue(all(c["pct_change_month"] < 0 for c in data["losers"]))

    @patch("pikachu_core.time.sleep", return_value=None)
    @patch("pikachu_core.requests.get")
    def test_flat_cards_appear_in_neither_list(self, mock_get, mock_sleep):
        self._wire(mock_get, [
            ("up", "Pikachu A", 12, 12, 10),
            ("flat", "Pikachu B", 10, 10, 10),
        ])
        data = pikachu_core.get_gainers_and_losers(n=5)
        self.assertEqual([c["id"] for c in data["gainers"]], ["up"])
        self.assertEqual(data["losers"], [])

    @patch("pikachu_core.time.sleep", return_value=None)
    @patch("pikachu_core.requests.get")
    def test_stats_report_counts_and_priciest_card(self, mock_get, mock_sleep):
        self._wire(mock_get, [
            ("up", "Pikachu A", 12, 12, 10),
            ("down", "Pikachu B", 5, 5, 100),   # priciest by avg30 -> marketPrice 100
        ])
        stats = pikachu_core.get_gainers_and_losers(n=5)["stats"]
        self.assertEqual(stats["tracked"], 2)
        self.assertEqual(stats["gainer_count"], 1)
        self.assertEqual(stats["loser_count"], 1)
        self.assertEqual(stats["priciest"]["id"], "down")

    @patch("pikachu_core.time.sleep", return_value=None)
    @patch("pikachu_core.requests.get")
    def test_wildest_24h_swing_picks_largest_one_day_gap(self, mock_get, mock_sleep):
        self._wire(mock_get, [
            ("calm", "Pikachu A", 10.1, 10, 10),
            ("wild", "Pikachu B", 30, 10, 10),   # 1-day avg triple the weekly
        ])
        wildest = pikachu_core.get_gainers_and_losers(n=5)["stats"]["wildest_24h"]
        self.assertEqual(wildest["card"]["id"], "wild")
        self.assertAlmostEqual(wildest["swing_pct"], 200.0)


class RenderHtmlReportTests(unittest.TestCase):
    @staticmethod
    def _card(**overrides):
        card = {
            "id": "x-1", "name": "Pikachu", "variant_label": "", "set": "Set A",
            "local_id": "1", "image_url": "https://assets.tcgdex.net/en/x/y/1/high.webp",
            "pct_change_month": 42.0, "pct_change_24h": 10.0, "change_eur": 4.2,
            "usd_display_price": 9.99, "usd_price_variant": "normal",
            "avg1_eur": 15.0, "avg7_eur": 14.2, "avg30_eur": 10.0, "is_new": False,
        }
        card.update(overrides)
        return card

    def _data(self):
        gainer = self._card()
        loser = self._card(
            id="y-2", name="Ash's Pikachu", variant_label="Ash's", set="Set B",
            local_id="2", image_url=None, pct_change_month=-30.0,
            pct_change_24h=None, change_eur=-3.0, usd_display_price=None,
            usd_price_variant=None, avg1_eur=None, avg7_eur=7.0, is_new=True,
        )
        return {
            "gainers": [gainer],
            "losers": [loser],
            "stats": {
                "tracked": 157, "skipped": 50, "gainer_count": 74, "loser_count": 83,
                "priciest": self._card(set="Rich Set", usd_display_price=4100.0),
                "wildest_24h": {"card": self._card(set="Wild Set"), "swing_pct": 212.0},
            },
        }

    def test_renders_both_tabs_with_their_rows(self):
        report = pikachu_core.render_html_report(self._data())
        self.assertIn("Top gainers", report)
        self.assertIn("Top losers", report)
        self.assertIn("+42.0%", report)
        self.assertIn("-30.0%", report)

    def test_gainers_and_losers_are_distinguished_beyond_colour(self):
        report = pikachu_core.render_html_report(self._data())
        self.assertIn('<tr class="up"', report)
        self.assertIn('<tr class="down"', report)
        self.assertIn("▲", report)
        self.assertIn("▼", report)

    def test_header_carries_title_kpis_and_timestamp(self):
        report = pikachu_core.render_html_report(self._data())
        self.assertIn("<title>Pikachu Card Prices</title>", report)
        self.assertIn("Biggest gain", report)
        self.assertIn("Biggest drop", report)
        self.assertIn("Last updated", report)

    def test_sidebar_shows_priciest_and_wildest_swing(self):
        report = pikachu_core.render_html_report(self._data())
        self.assertIn("Priciest Pikachu", report)
        self.assertIn("$4,100.00", report)
        self.assertIn("Wildest 24h swing", report)
        self.assertIn("+212%", report)
        self.assertIn("Market pulse", report)

    def test_price_column_is_plain_price_and_set_is_the_headline(self):
        report = pikachu_core.render_html_report(self._data())
        self.assertIn(">Price<", report)
        self.assertNotIn("USD ref", report)
        self.assertIn('class="card-set">Set A<', report)
        self.assertIn(">Ash&#x27;s<", report)

    def test_sparkline_handles_missing_one_day_average(self):
        # The loser fixture has avg1_eur=None: two points, not a crash.
        report = pikachu_core.render_html_report(self._data())
        self.assertEqual(report.count("<svg class=\"spark\""), 2)

    def test_empty_lists_render_without_crashing(self):
        report = pikachu_core.render_html_report({
            "gainers": [], "losers": [],
            "stats": {"tracked": 0, "skipped": 0, "gainer_count": 0,
                      "loser_count": 0, "priciest": None, "wildest_24h": None},
        })
        self.assertIn("Pikachu Card Prices", report)
        self.assertIn("No cards gained this period.", report)


class TransientFailureTests(unittest.TestCase):
    """The weekly workflow runs unattended, so a blip must not kill the run."""

    def setUp(self):
        pikachu_core._set_release_date_cache.clear()

    @staticmethod
    def _error_response(status):
        resp = MagicMock()
        resp.status_code = status
        http_error = requests.exceptions.HTTPError(f"{status}", response=resp)
        resp.raise_for_status.side_effect = http_error
        return resp

    @patch("pikachu_core.time.sleep", return_value=None)
    @patch("pikachu_core.requests.get")
    def test_retries_a_transient_5xx_then_succeeds(self, mock_get, mock_sleep):
        ok = _response({"releaseDate": "2020-01-01"})
        mock_get.side_effect = [self._error_response(503), ok]

        result = pikachu_core._get_json("https://example.test/sets/x")

        self.assertEqual(result, {"releaseDate": "2020-01-01"})
        self.assertEqual(mock_get.call_count, 2)

    @patch("pikachu_core.time.sleep", return_value=None)
    @patch("pikachu_core.requests.get")
    def test_gives_up_after_max_retries(self, mock_get, mock_sleep):
        mock_get.side_effect = [self._error_response(503)] * pikachu_core.MAX_RETRIES

        with self.assertRaises(requests.exceptions.HTTPError):
            pikachu_core._get_json("https://example.test/sets/x")

        self.assertEqual(mock_get.call_count, pikachu_core.MAX_RETRIES)

    @patch("pikachu_core.time.sleep", return_value=None)
    @patch("pikachu_core.requests.get")
    def test_does_not_retry_a_404(self, mock_get, mock_sleep):
        mock_get.side_effect = [self._error_response(404)]

        with self.assertRaises(requests.exceptions.HTTPError):
            pikachu_core._get_json("https://example.test/cards/nope")

        self.assertEqual(mock_get.call_count, 1)

    @patch("pikachu_core.time.sleep", return_value=None)
    @patch("pikachu_core.requests.get")
    def test_failed_release_lookup_does_not_sink_the_report(self, mock_get, mock_sleep):
        # Every /sets/ call fails; the card must still come back, unbadged.
        def side_effect(url, *args, **kwargs):
            if "/sets/" in url:
                raise requests.exceptions.ConnectionError("boom")
            raise AssertionError(f"unexpected call to {url}")

        mock_get.side_effect = side_effect

        cards = pikachu_core._add_new_badges([{"set_id": "ru1", "name": "Pikachu"}])

        self.assertIs(cards[0]["is_new"], False)
        self.assertIsNone(cards[0]["release_date"])


class DedupeByProductTests(unittest.TestCase):
    """TCGdex lists some physical cards under several ids that share one
    Cardmarket product, producing identical duplicate rows."""

    def test_duplicate_products_collapse_to_one_entry(self):
        cards = [
            {"id": "xyp-XY95", "cm_product_id": 289809,
             "usd_display_price": None, "image_url": "img"},
            {"id": "xyp-XY202", "cm_product_id": 289809,
             "usd_display_price": None, "image_url": "img"},
        ]
        result = pikachu_core._dedupe_by_product(cards)
        self.assertEqual(len(result), 1)

    def test_keeps_the_more_complete_record(self):
        sparse = {"id": "a", "cm_product_id": 1, "usd_display_price": None, "image_url": None}
        rich = {"id": "b", "cm_product_id": 1, "usd_display_price": 9.99, "image_url": "img"}
        for order in ([sparse, rich], [rich, sparse]):
            with self.subTest(order=[c["id"] for c in order]):
                result = pikachu_core._dedupe_by_product(order)
                self.assertEqual([c["id"] for c in result], ["b"])

    def test_distinct_products_are_all_kept(self):
        cards = [
            {"id": "a", "cm_product_id": 1, "usd_display_price": 1, "image_url": None},
            {"id": "b", "cm_product_id": 2, "usd_display_price": 2, "image_url": None},
        ]
        self.assertEqual(len(pikachu_core._dedupe_by_product(cards)), 2)

    def test_cards_without_a_product_id_are_never_merged(self):
        cards = [
            {"id": "a", "cm_product_id": None, "usd_display_price": None, "image_url": None},
            {"id": "b", "cm_product_id": None, "usd_display_price": None, "image_url": None},
        ]
        self.assertEqual(len(pikachu_core._dedupe_by_product(cards)), 2)


if __name__ == "__main__":
    unittest.main()
