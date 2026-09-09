"""
Unit tests for risk_manager.py — no network, no exchange, no external deps.

Run from this directory with:  python3 -m unittest -v
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from risk_manager import HaltReason, RiskConfig, RiskManager, RiskState


class FakeClock:
    """Controllable UTC clock so day-rollover logic is testable."""

    def __init__(self, start: datetime = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance_days(self, days: int = 1) -> None:
        self.now += timedelta(days=days)


def build(config: RiskConfig = None, clock: FakeClock = None, db_path: str = ":memory:"):
    config = config or RiskConfig(
        risk_per_trade_pct=0.01,
        max_position_pct=0.50,
        max_correlated_pct=0.50,
        max_total_exposure_pct=1.0,
        leverage=2.0,
    )
    return RiskManager(config, RiskState(db_path=db_path), clock=clock or FakeClock())


class TestSizing(unittest.TestCase):
    def test_size_comes_from_stop_distance(self):
        risk = build()
        decision = risk.approve_trade(10_000, entry_price=100.0, stop_price=95.0,
                                      symbol="PF_ETHUSD", open_positions={})
        self.assertTrue(decision["approved"])
        # $100 risked / $5 stop distance = 20 units
        self.assertAlmostEqual(decision["sizing"]["quantity"], 20.0)
        self.assertAlmostEqual(decision["sizing"]["notional"], 2000.0)
        self.assertAlmostEqual(decision["sizing"]["risk_amount"], 100.0)
        self.assertEqual(decision["sizing"]["capped_by"], "risk_per_trade")
        self.assertFalse(decision["sizing"]["trimmed"])

    def test_wider_stop_gives_smaller_position_same_dollar_risk(self):
        risk = build()
        tight = risk.approve_trade(10_000, 100.0, 98.0, "PF_ETHUSD", {})["sizing"]
        wide = risk.approve_trade(10_000, 100.0, 90.0, "PF_ETHUSD", {})["sizing"]
        self.assertGreater(tight["quantity"], wide["quantity"])
        self.assertAlmostEqual(tight["risk_amount"], wide["risk_amount"])
        self.assertAlmostEqual(tight["risk_amount"], 100.0)

    def test_margin_uses_configured_leverage(self):
        risk = build()
        sizing = risk.approve_trade(10_000, 100.0, 95.0, "PF_ETHUSD", {})["sizing"]
        self.assertAlmostEqual(sizing["margin_required"], sizing["notional"] / 2.0)

    def test_quantity_rounds_down_to_lot_step(self):
        cfg = RiskConfig(risk_per_trade_pct=0.01, max_position_pct=0.5,
                         max_total_exposure_pct=1.0, quantity_step=0.5)
        risk = build(cfg)
        sizing = risk.approve_trade(10_000, 100.0, 97.0, "PF_ETHUSD", {})["sizing"]
        # 33.33 units risk-sized, floored to the 0.5 lot step
        self.assertAlmostEqual(sizing["quantity"], 33.0)
        self.assertLessEqual(sizing["risk_amount"], 100.0)


class TestExposureCaps(unittest.TestCase):
    def test_per_symbol_cap_trims_size(self):
        cfg = RiskConfig(risk_per_trade_pct=0.015, max_position_pct=0.08,
                         max_correlated_pct=0.50, max_total_exposure_pct=1.0)
        risk = build(cfg)
        sizing = risk.approve_trade(10_000, 180.0, 174.0, "PF_SOLUSD", {})["sizing"]
        self.assertAlmostEqual(sizing["notional"], 800.0)          # 8% of equity
        self.assertEqual(sizing["capped_by"], "max_position")
        self.assertTrue(sizing["trimmed"])

    def test_per_symbol_cap_counts_the_existing_position(self):
        cfg = RiskConfig(risk_per_trade_pct=0.015, max_position_pct=0.08,
                         max_correlated_pct=0.50, max_total_exposure_pct=1.0)
        risk = build(cfg)
        sizing = risk.approve_trade(10_000, 180.0, 174.0, "PF_SOLUSD",
                                    {"PF_SOLUSD": 500.0})["sizing"]
        self.assertAlmostEqual(sizing["notional"], 300.0)          # 800 cap - 500 open

    def test_correlated_group_cap_rejects_when_group_is_full(self):
        cfg = RiskConfig(max_correlated_pct=0.22,
                         correlated_groups={"majors": ["PF_ETHUSD", "PF_SOLUSD", "PF_LINKUSD"]})
        risk = build(cfg)
        decision = risk.approve_trade(10_000, 20.0, 19.0, "PF_LINKUSD",
                                      {"PF_ETHUSD": 1800.0, "PF_SOLUSD": 500.0})
        self.assertFalse(decision["approved"])
        self.assertIn("majors", decision["reason"])
        self.assertIsNone(decision["sizing"])

    def test_tightest_group_wins_when_symbol_is_in_two_groups(self):
        cfg = RiskConfig(risk_per_trade_pct=0.05, max_position_pct=0.5,
                         max_correlated_pct=0.20, max_total_exposure_pct=1.0,
                         correlated_groups={"majors": ["PF_ETHUSD", "PF_SOLUSD"],
                                            "l1s": ["PF_SOLUSD", "PF_AVAXUSD"]})
        risk = build(cfg)
        sizing = risk.approve_trade(10_000, 100.0, 90.0, "PF_SOLUSD",
                                    {"PF_ETHUSD": 500.0, "PF_AVAXUSD": 1500.0})["sizing"]
        # l1s has 500 of headroom vs majors' 1500
        self.assertAlmostEqual(sizing["notional"], 500.0)
        self.assertEqual(sizing["capped_by"], "correlated:l1s")

    def test_total_exposure_cap(self):
        cfg = RiskConfig(risk_per_trade_pct=0.05, max_position_pct=0.9,
                         max_correlated_pct=0.9, max_total_exposure_pct=0.60)
        risk = build(cfg)
        sizing = risk.approve_trade(10_000, 100.0, 90.0, "PF_ETHUSD",
                                    {"PF_XBTUSD": 5500.0})["sizing"]
        self.assertAlmostEqual(sizing["notional"], 500.0)
        self.assertEqual(sizing["capped_by"], "max_total_exposure")

    def test_short_notionals_count_toward_exposure(self):
        cfg = RiskConfig(risk_per_trade_pct=0.05, max_position_pct=0.9,
                         max_correlated_pct=0.9, max_total_exposure_pct=0.60)
        risk = build(cfg)
        decision = risk.approve_trade(10_000, 100.0, 90.0, "PF_ETHUSD",
                                      {"PF_XBTUSD": -6000.0})
        self.assertFalse(decision["approved"])
        self.assertIn("total exposure", decision["reason"])

    def test_max_open_positions(self):
        cfg = RiskConfig(max_open_positions=2, max_total_exposure_pct=1.0)
        risk = build(cfg)
        decision = risk.approve_trade(10_000, 100.0, 90.0, "PF_ETHUSD",
                                      {"PF_XBTUSD": 100.0, "PF_SOLUSD": 100.0})
        self.assertFalse(decision["approved"])
        self.assertIn("max 2", decision["reason"])

    def test_adding_to_an_open_symbol_is_not_a_new_slot(self):
        cfg = RiskConfig(max_open_positions=2, max_position_pct=0.5,
                         max_correlated_pct=0.9, max_total_exposure_pct=1.0)
        risk = build(cfg)
        decision = risk.approve_trade(10_000, 100.0, 90.0, "PF_SOLUSD",
                                      {"PF_XBTUSD": 100.0, "PF_SOLUSD": 100.0})
        self.assertTrue(decision["approved"])

    def test_dust_after_trimming_is_rejected(self):
        cfg = RiskConfig(max_position_pct=0.5, max_correlated_pct=0.9,
                         max_total_exposure_pct=1.0, min_notional=10.0)
        risk = build(cfg)
        decision = risk.approve_trade(10_000, 100.0, 90.0, "PF_ETHUSD",
                                      {"PF_ETHUSD": 4995.0})
        self.assertFalse(decision["approved"])
        self.assertIn("minimum", decision["reason"])


class TestInputValidation(unittest.TestCase):
    def test_stop_inside_the_noise_band_is_rejected(self):
        risk = build()
        decision = risk.approve_trade(10_000, 100.0, 99.99, "PF_ETHUSD", {})
        self.assertFalse(decision["approved"])
        self.assertIn("minimum", decision["reason"])

    def test_non_positive_prices_are_rejected(self):
        risk = build()
        self.assertFalse(risk.approve_trade(10_000, 0.0, 95.0, "PF_ETHUSD", {})["approved"])
        self.assertFalse(risk.approve_trade(10_000, 100.0, -5.0, "PF_ETHUSD", {})["approved"])

    def test_short_side_stop_above_entry_sizes_the_same(self):
        risk = build()
        long_side = risk.approve_trade(10_000, 100.0, 95.0, "PF_ETHUSD", {})["sizing"]
        short_side = risk.approve_trade(10_000, 100.0, 105.0, "PF_ETHUSD", {})["sizing"]
        self.assertAlmostEqual(long_side["quantity"], short_side["quantity"])

    def test_config_rejects_nonsense_limits(self):
        with self.assertRaises(ValueError):
            RiskConfig(risk_per_trade_pct=0)
        with self.assertRaises(ValueError):
            RiskConfig(max_drawdown_pct=1.5)
        with self.assertRaises(ValueError):
            RiskConfig(leverage=0)

    def test_state_rejects_unknown_fields(self):
        state = RiskState(db_path=":memory:")
        with self.assertRaises(ValueError):
            state.save(nonsense=1)


class TestKillSwitches(unittest.TestCase):
    def test_daily_loss_limit_halts(self):
        cfg = RiskConfig(daily_loss_limit_pct=0.04, max_drawdown_pct=0.18)
        risk = build(cfg)
        self.assertIsNone(risk.update_equity(10_000))
        self.assertIsNone(risk.update_equity(9_650))          # -3.5%, still trading
        self.assertEqual(risk.update_equity(9_500), HaltReason.DAILY_LOSS_LIMIT)  # -5%
        self.assertTrue(risk.is_halted())

    def test_drawdown_halts_on_a_later_day(self):
        clock = FakeClock()
        cfg = RiskConfig(daily_loss_limit_pct=0.04, max_drawdown_pct=0.18)
        risk = build(cfg, clock)
        risk.update_equity(10_000)
        clock.advance_days()
        self.assertEqual(risk.update_equity(8_100), HaltReason.MAX_DRAWDOWN)

    def test_daily_baseline_resets_each_day(self):
        clock = FakeClock()
        risk = build(RiskConfig(daily_loss_limit_pct=0.04, max_drawdown_pct=0.50), clock)
        risk.update_equity(10_000)
        clock.advance_days()
        self.assertIsNone(risk.update_equity(9_000))          # new day's open
        self.assertEqual(risk.state.day_start_equity, 9_000)
        self.assertIsNone(risk.update_equity(8_700))          # -3.3% of today's open
        self.assertEqual(risk.update_equity(8_600), HaltReason.DAILY_LOSS_LIMIT)

    def test_peak_ratchets_up_only(self):
        risk = build()
        risk.update_equity(10_000)
        risk.update_equity(12_000)
        risk.update_equity(11_000)
        self.assertEqual(risk.state.peak_equity, 12_000)

    def test_wipeout_halts(self):
        risk = build()
        risk.update_equity(10_000)
        self.assertEqual(risk.update_equity(0.0), HaltReason.EQUITY_WIPEOUT)

    def test_halted_manager_rejects_trades(self):
        risk = build()
        risk.manual_halt("maintenance window")
        decision = risk.approve_trade(10_000, 100.0, 95.0, "PF_ETHUSD", {})
        self.assertFalse(decision["approved"])
        self.assertIn("halted", decision["reason"])

    def test_manual_resume_clears_the_halt(self):
        risk = build()
        risk.update_equity(10_000)
        risk.update_equity(9_000)
        self.assertTrue(risk.is_halted())
        risk.manual_resume("reviewed")
        self.assertFalse(risk.is_halted())
        self.assertTrue(risk.approve_trade(9_000, 100.0, 95.0, "PF_ETHUSD", {})["approved"])

    def test_resume_reanchors_the_drawdown_peak_when_still_in_breach(self):
        clock = FakeClock()
        risk = build(RiskConfig(daily_loss_limit_pct=0.04, max_drawdown_pct=0.18), clock)
        risk.update_equity(10_000)
        clock.advance_days()
        self.assertEqual(risk.update_equity(8_100), HaltReason.MAX_DRAWDOWN)
        risk.manual_resume("accepted the loss, restarting from here")
        self.assertEqual(risk.state.peak_equity, 8_100)
        self.assertIsNone(risk.update_equity(8_100))          # does not re-halt
        detail = risk.state.events()[0]["detail"]
        self.assertIn("re-anchored", detail)                  # visible in the audit log

    def test_resume_without_reanchor_re_halts_on_the_next_update(self):
        risk = build(RiskConfig(daily_loss_limit_pct=0.04))
        risk.update_equity(10_000)
        risk.update_equity(9_000)
        risk.manual_resume("keeping the original baseline", reanchor=False)
        self.assertFalse(risk.is_halted())
        self.assertEqual(risk.update_equity(9_000), HaltReason.DAILY_LOSS_LIMIT)

    def test_equity_recovery_alone_does_not_clear_a_halt(self):
        risk = build()
        risk.update_equity(10_000)
        risk.update_equity(9_000)
        self.assertEqual(risk.update_equity(10_500), HaltReason.DAILY_LOSS_LIMIT)
        self.assertTrue(risk.is_halted())

    def test_halt_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "risk.db")
            cfg = RiskConfig(daily_loss_limit_pct=0.04)
            first = build(cfg, db_path=db)
            first.update_equity(10_000)
            first.update_equity(9_000)
            self.assertTrue(first.is_halted())
            first.state.close()

            # Process dies, comes back up against the same file.
            restarted = build(cfg, db_path=db)
            self.assertTrue(restarted.is_halted())
            self.assertEqual(restarted.halt_reason(), HaltReason.DAILY_LOSS_LIMIT)
            self.assertEqual(restarted.state.peak_equity, 10_000)
            restarted.state.close()

    def test_halts_are_written_to_the_audit_log(self):
        risk = build()
        risk.update_equity(10_000)
        risk.update_equity(9_000)
        risk.manual_resume("reviewed")
        events = [row["event"] for row in risk.state.events()]
        self.assertIn("halt:daily_loss_limit", events)
        self.assertIn("resume", events)


class TestStatus(unittest.TestCase):
    def test_status_reports_drawdown_and_daily_pnl(self):
        clock = FakeClock()
        risk = build(RiskConfig(daily_loss_limit_pct=0.10, max_drawdown_pct=0.50), clock)
        risk.update_equity(10_000)
        clock.advance_days()
        risk.update_equity(9_500)
        status = risk.status()
        self.assertAlmostEqual(status["drawdown_pct"], 0.05)
        self.assertAlmostEqual(status["daily_pnl_pct"], 0.0)   # 9,500 is today's open
        risk.update_equity(9_405)
        self.assertAlmostEqual(risk.status()["daily_pnl_pct"], -0.01)
        self.assertFalse(risk.status()["halted"])


if __name__ == "__main__":
    unittest.main()
