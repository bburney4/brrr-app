"""
Tests for the Kraken Spot adapter's pure logic. No network, and the SDK does
not need to be installed.

Fixtures use the response shapes documented by python-kraken-sdk 3.3.0,
including Kraken's legacy asset codes.
"""

import unittest

from kraken_spot_adapter import (
    KrakenSpotAdapter,
    PairNotFound,
    available_quote,
    extract_prices,
    holdings_notional,
    index_pairs,
    normalize_asset,
    order_minimums,
    parse_balances,
    portfolio_value,
)

BALANCES = {
    "ZUSD": {"balance": "2500.0000", "hold_trade": "500.0000"},
    "XXBT": {"balance": "0.1000000000", "hold_trade": "0.0000000000"},
    "SOL": {"balance": "10.0000000000", "hold_trade": "0.0000000000"},
    "KFEE": {"balance": "1407.73", "hold_trade": "0.00"},
}
PRICES = {"XBT": 60_000.0, "SOL": 180.0}


class TestAssetCodes(unittest.TestCase):
    def test_legacy_prefixes_are_stripped(self):
        self.assertEqual(normalize_asset("XXBT"), "XBT")
        self.assertEqual(normalize_asset("ZUSD"), "USD")
        self.assertEqual(normalize_asset("XETH"), "ETH")

    def test_modern_codes_pass_through(self):
        for code in ("SOL", "LINK", "USDT", "USDC"):
            self.assertEqual(normalize_asset(code), code)

    def test_staked_and_earn_suffixes_fold_into_the_base_asset(self):
        self.assertEqual(normalize_asset("XXBT.S"), "XBT")
        self.assertEqual(normalize_asset("USD.M"), "USD")
        self.assertEqual(normalize_asset("ETH.F"), "ETH")

    def test_blank_input(self):
        self.assertEqual(normalize_asset(""), "")
        self.assertEqual(normalize_asset(None), "")


class TestBalances(unittest.TestCase):
    def test_fee_credits_are_not_money(self):
        # KFEE is a fee rebate balance; counting it would inflate equity.
        self.assertNotIn("KFEE", parse_balances(BALANCES))

    def test_available_subtracts_funds_held_by_open_orders(self):
        parsed = parse_balances(BALANCES)
        self.assertEqual(parsed["USD"]["total"], 2500.0)
        self.assertEqual(parsed["USD"]["available"], 2000.0)
        self.assertEqual(parsed["USD"]["locked"], 500.0)

    def test_staked_balance_merges_and_is_flagged(self):
        parsed = parse_balances({
            "XXBT": {"balance": "0.5", "hold_trade": "0"},
            "XXBT.S": {"balance": "1.5", "hold_trade": "0"},
        })
        self.assertEqual(parsed["XBT"]["total"], 2.0)
        self.assertEqual(parsed["XBT"]["illiquid"], 1.5)

    def test_zero_balances_are_dropped(self):
        self.assertEqual(parse_balances({"XXLM": {"balance": "0.0", "hold_trade": "0.0"}}), {})

    def test_bare_number_payloads(self):
        self.assertEqual(parse_balances({"ZUSD": "100.0"})["USD"]["available"], 100.0)


class TestEquityAndHoldings(unittest.TestCase):
    def test_equity_is_cash_plus_coins_at_market(self):
        # 2500 cash + 0.1 XBT @ 60k + 10 SOL @ 180 = 10,300
        equity = portfolio_value(parse_balances(BALANCES), PRICES)
        self.assertAlmostEqual(equity, 10_300.0)

    def test_unpriced_assets_are_skipped_not_guessed(self):
        balances = parse_balances({"WEIRD": {"balance": "5", "hold_trade": "0"}})
        self.assertEqual(portfolio_value(balances, {}), 0.0)

    def test_holdings_exclude_cash(self):
        holdings = holdings_notional(parse_balances(BALANCES), PRICES)
        self.assertEqual(holdings, {"XBT": 6000.0, "SOL": 1800.0})
        self.assertNotIn("USD", holdings)

    def test_dust_is_dropped(self):
        balances = parse_balances({"SOL": {"balance": "0.001", "hold_trade": "0"}})
        self.assertEqual(holdings_notional(balances, PRICES), {})

    def test_spendable_cash_excludes_funds_on_the_book(self):
        self.assertEqual(available_quote(parse_balances(BALANCES)), 2000.0)

    def test_equity_and_cash_are_different_numbers(self):
        # The whole reason approve_trade needs available_cash on spot.
        parsed = parse_balances(BALANCES)
        self.assertGreater(portfolio_value(parsed, PRICES), available_quote(parsed) * 5)


class TestPairIndex(unittest.TestCase):
    ASSET_PAIRS = {
        "XXBTZUSD": {"base": "XXBT", "quote": "ZUSD", "altname": "XBTUSD",
                     "ordermin": "0.0001", "costmin": "0.5",
                     "lot_decimals": 8, "pair_decimals": 1},
        "SOLUSD": {"base": "SOL", "quote": "ZUSD", "altname": "SOLUSD",
                   "ordermin": "0.05", "costmin": "0.5"},
        "XXBTZEUR": {"base": "XXBT", "quote": "ZEUR", "altname": "XBTEUR"},
    }

    def test_pairs_are_indexed_by_base_asset(self):
        index = index_pairs(self.ASSET_PAIRS, "USD")
        self.assertEqual(index["XBT"][0], "XXBTZUSD")
        self.assertEqual(index["SOL"][0], "SOLUSD")

    def test_other_quote_currencies_are_ignored(self):
        index = index_pairs(self.ASSET_PAIRS, "USD")
        self.assertNotIn("XXBTZEUR", [pair for pair, _ in index.values()])

    def test_euro_quoted_index(self):
        self.assertEqual(index_pairs(self.ASSET_PAIRS, "EUR")["XBT"][0], "XXBTZEUR")

    def test_minimums_are_parsed_from_strings(self):
        mins = order_minimums(self.ASSET_PAIRS["XXBTZUSD"])
        self.assertEqual(mins["ordermin"], 0.0001)
        self.assertEqual(mins["costmin"], 0.5)
        self.assertEqual(mins["lot_decimals"], 8)

    def test_missing_minimums_are_none(self):
        self.assertIsNone(order_minimums({})["ordermin"])


class TestPrices(unittest.TestCase):
    def test_last_trade_price_is_read_from_the_c_field(self):
        payload = {"XXBTZUSD": {"c": ["60000.5", "0.01"], "b": ["59999.0", "1", "1"]}}
        self.assertEqual(extract_prices(payload, {"XXBTZUSD": "XBT"}), {"XBT": 60000.5})

    def test_unknown_pairs_and_bad_values_are_skipped(self):
        payload = {"OTHER": {"c": ["1.0", "1"]}, "XXBTZUSD": {"c": ["nope", "1"]}}
        self.assertEqual(extract_prices(payload, {"XXBTZUSD": "XBT"}), {})


class FakeTrade:
    def __init__(self):
        self.calls = []

    def create_order(self, **kwargs):
        self.calls.append(kwargs)
        return {"error": [], "result": {"txid": ["OABC-123"]}}


def make_adapter(validate_only=False):
    adapter = KrakenSpotAdapter.__new__(KrakenSpotAdapter)
    adapter.quote = "USD"
    adapter.validate_only = validate_only
    adapter.trade = FakeTrade()
    adapter._pairs = {
        "SOL": ("SOLUSD", {"base": "SOL", "quote": "ZUSD",
                           "ordermin": "0.05", "costmin": "0.5"}),
        "XBT": ("XXBTZUSD", {"base": "XXBT", "quote": "ZUSD",
                             "ordermin": "0.0001", "costmin": "0.5"}),
    }
    return adapter


class TestOrders(unittest.TestCase):
    def test_stop_rides_along_as_a_conditional_close(self):
        adapter = make_adapter()
        adapter.place_buy_with_stop("SOL", 5.0, 180.0, 174.0)
        call = adapter.trade.calls[0]
        self.assertEqual(call["pair"], "SOLUSD")
        self.assertEqual(call["side"], "buy")
        self.assertEqual(call["volume"], 5.0)
        self.assertEqual(call["price"], 180.0)
        # The stop is created by Kraken only if the buy fills.
        self.assertEqual(call["close_ordertype"], "stop-loss")
        self.assertEqual(call["close_price"], 174.0)
        self.assertEqual(call["trigger"], "index")
        self.assertTrue(call["truncate"])

    def test_validate_only_never_reaches_the_book(self):
        adapter = make_adapter(validate_only=True)
        adapter.place_buy_with_stop("SOL", 5.0, 180.0, 174.0)
        self.assertTrue(adapter.trade.calls[0]["validate"])

    def test_stop_above_entry_is_refused(self):
        adapter = make_adapter()
        with self.assertRaises(ValueError):
            adapter.place_buy_with_stop("SOL", 5.0, 180.0, 185.0)
        self.assertEqual(adapter.trade.calls, [])

    def test_below_kraken_minimum_volume_is_refused(self):
        adapter = make_adapter()
        with self.assertRaises(ValueError) as ctx:
            adapter.place_buy_with_stop("SOL", 0.01, 180.0, 174.0)
        self.assertIn("minimum", str(ctx.exception))

    def test_below_minimum_cost_is_refused(self):
        adapter = make_adapter()
        adapter._pairs["SOL"][1]["costmin"] = "100"
        with self.assertRaises(ValueError):
            adapter.place_buy_with_stop("SOL", 0.1, 180.0, 174.0)

    def test_unknown_asset_says_xbt_not_btc(self):
        adapter = make_adapter()
        with self.assertRaises(PairNotFound) as ctx:
            adapter.place_buy_with_stop("BTC", 1.0, 60000.0, 58000.0)
        self.assertIn("XBT", str(ctx.exception))

    def test_selling_is_not_risk_gated(self):
        adapter = make_adapter()
        adapter.sell("SOL", 5.0)
        call = adapter.trade.calls[0]
        self.assertEqual(call["side"], "sell")
        self.assertEqual(call["ordertype"], "market")


if __name__ == "__main__":
    unittest.main()
