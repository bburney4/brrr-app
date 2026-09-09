#!/usr/bin/env python3
"""
preflight.py

Run this before the bot ever places an order. It verifies, against your actual
account, everything the bot assumes: credentials work, the key has the right
permissions, the flex account exists and reports equity, positions parse, and
the instrument you want to trade has the precision fields the adapter reads.

It places NO orders. With --check-write it arms Kraken's dead man's switch for
one second and immediately disarms it, which proves the key has Full Access
without putting anything on the book.

    python3 preflight.py                      # read-only checks
    python3 preflight.py --symbol PF_SOLUSD   # also dry-run a trade decision
    python3 preflight.py --check-write        # also verify order permissions
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from kraken_adapter import (
    build_adapter_from_env,
    load_config_from_env,
    size_step_from_instrument,
)
from risk_manager import RiskManager, RiskState


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the Kraken Futures wiring.")
    parser.add_argument("--symbol", default="PF_ETHUSD", help="instrument to inspect")
    parser.add_argument("--check-write", action="store_true",
                        help="verify order permissions via the dead man's switch")
    parser.add_argument("--db", default="risk_state.db", help="risk state file")
    args = parser.parse_args()

    print("=" * 68)
    adapter = build_adapter_from_env()
    env = "DEMO (demo-futures.kraken.com)" if adapter.sandbox else "*** LIVE — REAL MONEY ***"
    print(f"Environment : {env}")
    print(f"Key         : ...{os.environ.get('KRAKEN_FUTURES_KEY','')[-6:]}")
    print("=" * 68)

    # 1. Credentials + equity ------------------------------------------------
    equity = adapter.account_equity()
    print(f"\n[1/5] Account equity: ${equity:,.2f}")
    if equity <= 0:
        print("      Equity is zero — fund the flex (multi-collateral) account first.")

    # 2. Open positions ------------------------------------------------------
    positions = adapter.open_positions()
    print(f"\n[2/5] Open positions: {len(positions)}")
    for symbol, notional in sorted(positions.items()):
        print(f"      {symbol:<14} ${notional:>12,.2f} notional")
    if not positions:
        print("      (none)")

    # 3. Instrument spec -----------------------------------------------------
    print(f"\n[3/5] Instrument {args.symbol}")
    instrument = adapter.instrument(args.symbol)
    step = size_step_from_instrument(instrument)
    tick = adapter.tick_size(args.symbol)
    print(f"      lot step  : {step}   <- set RiskConfig.quantity_step to this")
    print(f"      tick size : {tick}")
    interesting = {k: v for k, v in instrument.items()
                   if k in ("symbol", "type", "underlying", "tickSize", "contractSize",
                            "contractValueTradePrecision", "isin", "tradeable",
                            "maxPositionSize", "openingDate")}
    print("      spec      : " + json.dumps(interesting, default=str))
    if step is None:
        print("      NOTE: no precision field recognised. Full payload below —")
        print("      tell me the field name and I'll wire it in.")
        print("      " + json.dumps(instrument, default=str)[:600])

    # 4. Risk state ----------------------------------------------------------
    config = load_config_from_env()
    risk = RiskManager(config, RiskState(db_path=args.db))
    print(f"\n[4/5] Risk state ({args.db})")
    for key, value in risk.status().items():
        print(f"      {key:<18} {value}")
    if risk.is_halted():
        print("      >>> HALTED — the bot will reject every trade until manual_resume()")

    # 5. Dry-run a decision --------------------------------------------------
    print(f"\n[5/5] Dry-run decision for {args.symbol}")
    ticker_price = None
    try:
        tickers = adapter.market.get_tickers().get("tickers", [])
        for entry in tickers:
            if str(entry.get("symbol", "")).upper() == args.symbol.upper():
                ticker_price = float(entry.get("markPrice") or entry.get("last") or 0) or None
                break
    except Exception as exc:  # pragma: no cover - informational only
        print(f"      (could not fetch mark price: {exc})")

    if ticker_price:
        stop = ticker_price * 0.97          # hypothetical 3% stop
        decision = risk.approve_trade(
            account_equity=equity, entry_price=ticker_price, stop_price=stop,
            symbol=args.symbol.upper(), open_positions=positions,
        )
        print(f"      mark ${ticker_price:,.2f}, hypothetical 3% stop ${stop:,.2f}")
        print(f"      approved: {decision['approved']}")
        print(f"      reason  : {decision['reason']}")
        if decision["sizing"]:
            s = decision["sizing"]
            print(f"      size    : {s['quantity']} @ ${s['notional']:,.2f} notional, "
                  f"${s['margin_required']:,.2f} margin, risking ${s['risk_amount']:,.2f}")
        print("      NOTE: a dry run only — no order was placed.")

    # Optional write check ---------------------------------------------------
    if args.check_write:
        print("\n[+]   Verifying order permissions (dead man's switch, no orders)")
        adapter.arm_dead_mans_switch(60)
        adapter.arm_dead_mans_switch(0)
        print("      OK — the key has Full Access and can place orders.")
    else:
        print("\n[+]   Skipped the write check. Re-run with --check-write to confirm "
              "the key can actually place orders.")

    print("\nPreflight complete.")
    if not adapter.sandbox:
        print("You are pointed at LIVE. Confirm every number above before trading.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"\nPREFLIGHT FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
