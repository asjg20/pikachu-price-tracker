"""
Unit tests for pikachu_core. All network calls are mocked via
unittest.mock.patch on pikachu_core.requests.get -- no real HTTP happens here.
"""

import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

import pikachu_core


def _response(json_data, status_ok=True):
    """Build a fake requests.Response-like object."""
    resp = MagicMock()
    resp.json.return_value = json_data
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = Exception("HTTP error")
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
    def test_usd_display_price_prefers_reverse_holofoil_over_others(self, mock_get):
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


class RenderHtmlReportTests(unittest.TestCase):
    @staticmethod
    def _movers():
        return [
            {"name": "Pikachu", "variant_label": "", "set": "Set A", "local_id": "1",
             "image_url": "https://assets.tcgdex.net/en/x/y/1/high.webp",
             "pct_change_month": 42.0, "pct_change_24h": 10.0,
             "usd_display_price": 9.99, "usd_price_variant": "normal",
             "avg7_eur": 14.2, "avg30_eur": 10.0, "is_new": False},
            {"name": "Ash's Pikachu", "variant_label": "Ash's", "set": "Set B", "local_id": "2",
             "image_url": None,
             "pct_change_month": -30.0, "pct_change_24h": None,
             "usd_display_price": None, "usd_price_variant": None,
             "avg7_eur": 7.0, "avg30_eur": 10.0, "is_new": True},
        ]

    def test_gainers_and_droppers_are_visually_distinguished(self):
        report = pikachu_core.render_html_report(self._movers())
        self.assertIn('class="tile up"', report)
        self.assertIn('class="tile down"', report)
        self.assertIn("+42.0%", report)
        self.assertIn("-30.0%", report)
        self.assertIn("NEW!", report)

    def test_card_art_is_rendered_with_graceful_fallback(self):
        report = pikachu_core.render_html_report(self._movers())
        self.assertIn('src="https://assets.tcgdex.net/en/x/y/1/high.webp"', report)
        self.assertIn("art-missing", report)  # second card has no image

    def test_set_is_the_headline_and_redundant_pikachu_is_dropped(self):
        report = pikachu_core.render_html_report(self._movers())
        self.assertIn("<h2 class=\"set\">Set A</h2>", report)
        # "Ash's" survives as the distinguishing chip; bare "Pikachu" does not
        # appear as a card heading anywhere.
        self.assertIn(">Ash&#x27;s<", report)
        self.assertNotIn("<h2 class=\"set\">Pikachu</h2>", report)

    def test_price_column_is_labelled_price_not_usd_ref(self):
        report = pikachu_core.render_html_report(self._movers())
        self.assertIn("<dt>Price</dt>", report)
        self.assertNotIn("USD ref", report)

    def test_title_is_natural(self):
        report = pikachu_core.render_html_report(self._movers())
        self.assertIn("<title>Pikachu Card Prices</title>", report)

    def test_handles_empty_mover_list_without_crashing(self):
        report = pikachu_core.render_html_report([])
        self.assertIn("Pikachu Card Prices", report)


if __name__ == "__main__":
    unittest.main()
