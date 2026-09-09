"""
Tests for the Kraken adapter's pure logic — payload parsing, sizing units and
rounding. No network, and the Kraken SDK does not need to be installed.

Fixtures use the response shapes documented by python-kraken-sdk 3.3.0.
"""

import unittest

from kraken_adapter import (
    AccountShapeError,
    KrakenFuturesAdapter,
    UnsupportedSymbol,
    extract_equity,
    extract_positions,
    is_flex_perpetual,
    round_price_toward,
    size_step_from_instrument,
)

WALLETS = {
    "result": "success",
    "accounts": {
        "cash": {"balances": {"usd": 5000.0}, "type": "cashAccount"},
        "flex": {
            "initialMargin": 400.0,
            "balanceValue": 10_000.0,
            "portfolioValue": 10_250.0,
            "collateralValue": 10_000.0,
            "pnl": 250.0,
            "availableMargin": 9_600.0,
            "marginEquity": 10_250.0,
            "type": "multiCollateralMarginAccount",
        },
    },
}


class TestEquity(unittest.TestCase):
    def test_reads_portfolio_value_from_the_flex_account(self):
        # portfolioValue includes unrealised P&L — the number the drawdown
        # switch must watch, not the flat balance.
        self.assertEqual(extract_equity(WALLETS), 10_250.0)

    def test_falls_back_when_portfolio_value_is_absent(self):
        payload = {"accounts": {"flex": {"marginEquity": 9_000.0}}}
        self.assertEqual(extract_equity(payload), 9_000.0)

    def test_missing_flex_account_explains_itself(self):
        payload = {"accounts": {"cash": {"balances": {"usd": 100.0}}}}
        with self.assertRaises(AccountShapeError) as ctx:
            extract_equity(payload)
        self.assertIn("flex", str(ctx.exception))

    def test_flex_account_without_any_known_field(self):
        with self.assertRaises(AccountShapeError):
            extract_equity({"accounts": {"flex": {"type": "multiCollateralMarginAccount"}}})


class TestPositions(unittest.TestCase):
    def test_perp_notional_is_size_times_price(self):
        payload = {"openPositions": [
            {"side": "long", "symbol": "pf_ethusd", "size": 2.5, "price": 3000.0},
        ]}
        self.assertEqual(extract_positions(payload), {"PF_ETHUSD": 7500.0})

    def test_symbols_are_upper_cased_to_match_config(self):
        # Kraken returns 'pf_solusd'; correlated_groups are written 'PF_SOLUSD'.
        payload = {"openPositions": [{"symbol": "pf_solusd", "size": 1, "price": 180.0}]}
        self.assertIn("PF_SOLUSD", extract_positions(payload))

    def test_inverse_contracts_are_already_usd(self):
        # PI_ size is USD contracts; multiplying by price would inflate exposure
        # by ~27,000x and silently disable the caps.
        payload = {"openPositions": [
            {"side": "short", "symbol": "pi_xbtusd", "size": 8000, "price": 27523.75},
        ]}
        self.assertEqual(extract_positions(payload), {"PI_XBTUSD": 8000.0})

    def test_shorts_count_as_exposure(self):
        payload = {"openPositions": [
            {"side": "short", "symbol": "pf_ethusd", "size": -2.0, "price": 3000.0},
        ]}
        self.assertEqual(extract_positions(payload), {"PF_ETHUSD": 6000.0})

    def test_two_entries_for_one_symbol_are_summed(self):
        payload = {"openPositions": [
            {"symbol": "pf_ethusd", "size": 1.0, "price": 3000.0},
            {"symbol": "pf_ethusd", "size": 0.5, "price": 3000.0},
        ]}
        self.assertEqual(extract_positions(payload), {"PF_ETHUSD": 4500.0})

    def test_empty_and_missing_payloads(self):
        self.assertEqual(extract_positions({"openPositions": []}), {})
        self.assertEqual(extract_positions({}), {})


class TestInstrumentSpecs(unittest.TestCase):
    def test_precision_becomes_a_lot_step(self):
        self.assertEqual(size_step_from_instrument({"contractValueTradePrecision": 4}), 0.0001)
        self.assertEqual(size_step_from_instrument({"contractValueTradePrecision": 0}), 1.0)

    def test_unknown_precision_returns_none(self):
        self.assertIsNone(size_step_from_instrument({"symbol": "PF_ETHUSD"}))
        self.assertIsNone(size_step_from_instrument({"contractValueTradePrecision": "4"}))

    def test_flex_perpetual_detection(self):
        self.assertTrue(is_flex_perpetual("PF_ETHUSD"))
        self.assertTrue(is_flex_perpetual("pf_ethusd"))
        self.assertFalse(is_flex_perpetual("PI_XBTUSD"))
        self.assertFalse(is_flex_perpetual("FI_ETHUSD_240329"))


class TestPriceRounding(unittest.TestCase):
    def test_long_stop_rounds_up_toward_entry(self):
        # A stop below entry rounded down would widen the loss past the budget.
        self.assertAlmostEqual(round_price_toward(2899.97, 0.05, 3000.0), 2900.0)

    def test_short_stop_rounds_down_toward_entry(self):
        self.assertAlmostEqual(round_price_toward(3100.03, 0.05, 3000.0), 3100.0)

    def test_no_tick_size_leaves_the_price_alone(self):
        self.assertEqual(round_price_toward(2899.97, None, 3000.0), 2899.97)
        self.assertEqual(round_price_toward(2899.97, 0, 3000.0), 2899.97)


class FakeTrade:
    """Records create_order calls instead of sending them."""

    def __init__(self):
        self.calls = []

    def create_order(self, **kwargs):
        self.calls.append(kwargs)
        return {"result": "success", "sendStatus": {"status": "placed"}}


def make_adapter(instrument=None, allow_market_entries=False):
    """An adapter with the network parts replaced — no SDK, no credentials."""
    adapter = KrakenFuturesAdapter.__new__(KrakenFuturesAdapter)
    adapter.sandbox = True
    adapter.allow_market_entries = allow_market_entries
    adapter.trade = FakeTrade()
    adapter._instruments = {
        "PF_ETHUSD": instrument or {"symbol": "PF_ETHUSD",
                                    "contractValueTradePrecision": 4,
                                    "tickSize": 0.05},
        "PI_XBTUSD": {"symbol": "PI_XBTUSD", "tickSize": 0.5},
    }
    return adapter


class TestOrderPlacement(unittest.TestCase):
    def test_entry_and_stop_are_both_placed(self):
        adapter = make_adapter()
        adapter.place_entry_with_stop("PF_ETHUSD", "buy", 1.5, 3000.0, 2900.0)
        entry, stop = adapter.trade.calls
        self.assertEqual(entry["orderType"], "lmt")
        self.assertEqual(entry["side"], "buy")
        self.assertEqual(entry["limitPrice"], 3000.0)
        self.assertEqual(stop["orderType"], "stp")
        self.assertEqual(stop["side"], "sell")          # closes the long
        self.assertTrue(stop["reduceOnly"])             # can never open a position
        self.assertEqual(stop["triggerSignal"], "mark")  # harder to wick through
        self.assertEqual(stop["size"], entry["size"])

    def test_short_entry_flips_the_stop_side(self):
        adapter = make_adapter()
        adapter.place_entry_with_stop("PF_ETHUSD", "sell", 1.0, 3000.0, 3100.0)
        self.assertEqual(adapter.trade.calls[1]["side"], "buy")

    def test_quantity_is_floored_to_the_lot_step(self):
        adapter = make_adapter()
        adapter.place_entry_with_stop("PF_ETHUSD", "buy", 1.23456789, 3000.0, 2900.0)
        self.assertAlmostEqual(adapter.trade.calls[0]["size"], 1.2345)

    def test_stop_is_rounded_toward_entry_not_away(self):
        adapter = make_adapter()
        adapter.place_entry_with_stop("PF_ETHUSD", "buy", 1.0, 3000.0, 2899.97)
        self.assertAlmostEqual(adapter.trade.calls[1]["stopPrice"], 2900.0)

    def test_inverse_contracts_are_refused(self):
        adapter = make_adapter()
        with self.assertRaises(UnsupportedSymbol) as ctx:
            adapter.place_entry_with_stop("PI_XBTUSD", "buy", 1.0, 27000.0, 26000.0)
        self.assertIn("sized in USD", str(ctx.exception))
        self.assertEqual(adapter.trade.calls, [])

    def test_market_entries_are_off_by_default(self):
        adapter = make_adapter()
        with self.assertRaises(ValueError):
            adapter.place_entry_with_stop("PF_ETHUSD", "buy", 1.0, 3000.0, 2900.0,
                                          order_type="mkt")
        adapter = make_adapter(allow_market_entries=True)
        adapter.place_entry_with_stop("PF_ETHUSD", "buy", 1.0, 3000.0, 2900.0,
                                      order_type="mkt")
        self.assertNotIn("limitPrice", adapter.trade.calls[0])

    def test_bad_inputs_place_nothing(self):
        adapter = make_adapter()
        for kwargs in ({"side": "long"}, {"quantity": 0.0}, {"quantity": -1.0}):
            args = {"symbol": "PF_ETHUSD", "side": "buy", "quantity": 1.0,
                    "entry_price": 3000.0, "stop_price": 2900.0, **kwargs}
            with self.assertRaises(ValueError):
                adapter.place_entry_with_stop(**args)
        self.assertEqual(adapter.trade.calls, [])

    def test_quantity_below_one_lot_is_refused(self):
        adapter = make_adapter()
        with self.assertRaises(ValueError):
            adapter.place_entry_with_stop("PF_ETHUSD", "buy", 0.00001, 3000.0, 2900.0)


if __name__ == "__main__":
    unittest.main()
