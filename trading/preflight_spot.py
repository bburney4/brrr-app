#!/usr/bin/env python3
"""
preflight_spot.py

Run before the bot places its first spot order. Verifies against your real
account: credentials work, balances parse, equity and spendable cash come out
right, the pair you want exists with its minimums, and a dry-run trade decision
looks sane.

Places NO live orders. With --check-write it sends one order with Kraken's
validate flag set, which asks the exchange to check the order and report back
without putting anything on the book.

    python3 preflight_spot.py
    python3 preflight_spot.py --asset SOL
    python3 preflight_spot.py --asset SOL --check-write
"""

from __future__ import annotations

import argparse
import sys

from kraken_spot_adapter import (
    build_spot_adapter_from_env,
    load_spot_config_from_env,
)
from risk_manager import RiskManager, RiskState


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the Kraken Spot wiring.")
    parser.add_argument("--asset", default="XBT", help="base asset, e.g. SOL (bitcoin is XBT)")
    parser.add_argument("--check-write", action="store_true",
                        help="send one validate-only order to confirm trade permissions")
    parser.add_argument("--db", default="risk_state.db", help="risk state file")
    args = parser.parse_args()
    asset = args.asset.upper()

    adapter = build_spot_adapter_from_env()
    print("=" * 70)
    print(f"Kraken SPOT — quote currency {adapter.quote}")
    print(f"Order mode : {'VALIDATE ONLY (nothing reaches the book)' if adapter.validate_only else '*** LIVE ORDERS ***'}")
    print("=" * 70)

    # 1. Balances, equity, cash --------------------------------------------
    snap = adapter.snapshot()
    equity, cash, holdings = snap["equity"], snap["cash"], snap["holdings"]
    print(f"\n[1/5] Portfolio")
    print(f"      equity (cash + coins) : ${equity:,.2f}")
    print(f"      spendable cash        : ${cash:,.2f}")
    print(f"      invested              : ${equity - cash:,.2f} "
          f"({(equity - cash) / equity if equity else 0:.1%} of portfolio)")
    if equity <= 0:
        print("      Equity is zero — fund the account before trading.")

    # 2. Holdings ----------------------------------------------------------
    print(f"\n[2/5] Holdings: {len(holdings)}")
    for name, value in sorted(holdings.items(), key=lambda kv: -kv[1]):
        share = value / equity if equity else 0
        illiquid = snap["balances"].get(name, {}).get("illiquid", 0.0)
        flag = "  (staked/Earn — not instantly sellable)" if illiquid else ""
        print(f"      {name:<8} ${value:>12,.2f}  {share:>6.1%}{flag}")
    if not holdings:
        print("      (all cash)")

    # 3. Pair spec ---------------------------------------------------------
    print(f"\n[3/5] Pair for {asset}")
    pair = adapter.pair_for(asset)
    mins = adapter.minimums(asset)
    print(f"      kraken pair : {pair}")
    print(f"      ordermin    : {mins['ordermin']}  (smallest volume Kraken accepts)")
    print(f"      costmin     : {mins['costmin']}   (smallest order value)")
    print(f"      decimals    : lot={mins['lot_decimals']} price={mins['pair_decimals']}")

    # 4. Risk state --------------------------------------------------------
    config = load_spot_config_from_env()
    risk = RiskManager(config, RiskState(db_path=args.db))
    print(f"\n[4/5] Risk state ({args.db})")
    for key, value in risk.status().items():
        print(f"      {key:<18} {value}")
    if risk.is_halted():
        print("      >>> HALTED — every buy is rejected until manual_resume()")

    # 5. Dry-run decision --------------------------------------------------
    print(f"\n[5/5] Dry-run decision for {asset}")
    price = adapter.prices([asset]).get(asset)
    if not price:
        print(f"      could not price {asset}")
        return 1
    stop = price * 0.92                       # hypothetical 8% stop
    decision = risk.approve_trade(
        account_equity=equity, entry_price=price, stop_price=stop,
        symbol=asset, open_positions=holdings, available_cash=cash,
    )
    print(f"      last ${price:,.2f}, hypothetical 8% stop ${stop:,.2f}")
    print(f"      approved: {decision['approved']}")
    print(f"      reason  : {decision['reason']}")
    if decision["sizing"]:
        s = decision["sizing"]
        print(f"      buy     : {s['quantity']} {asset} costing ${s['notional']:,.2f}, "
              f"risking ${s['risk_amount']:,.2f} to the stop")
        if mins["ordermin"] and s["quantity"] < mins["ordermin"]:
            print(f"      WARNING: below Kraken's {mins['ordermin']} minimum — this trade "
                  "would be rejected. Your risk budget may be too small for this coin.")
    print("      NOTE: a dry run. No order was placed.")

    # Optional write check --------------------------------------------------
    if args.check_write:
        print("\n[+]   Verifying trade permissions (validate-only order)")
        if not decision["sizing"]:
            print("      skipped — no approved size to validate with")
        else:
            result = adapter.place_buy_with_stop(
                asset, decision["sizing"]["quantity"], price, stop, validate=True
            )
            print(f"      Kraken accepted the order shape: {result}")
            print("      OK — the key can trade. Nothing was placed.")
    else:
        print("\n[+]   Skipped the write check. Re-run with --check-write to confirm "
              "the key can actually place orders.")

    print("\nPreflight complete.")
    if not adapter.validate_only:
        print("KRAKEN_VALIDATE_ONLY is off — this bot will place REAL orders.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"\nPREFLIGHT FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
