"""
example_spot.py

Wiring the risk manager into a Kraken SPOT bot. This is the file to copy from
if you are buying and holding coins rather than trading perpetuals.

Run it with credentials set to see your own account's numbers; with
KRAKEN_VALIDATE_ONLY left at its default, no order can reach the book.
"""

from kraken_spot_adapter import build_spot_adapter_from_env, build_spot_risk_manager

adapter = build_spot_adapter_from_env()
risk = build_spot_risk_manager(db_path="risk_state.db")


def on_signal(asset: str, entry_price: float, stop_price: float) -> None:
    """
    Call this wherever your strategy decides to buy.

    asset is a base asset code — "SOL", "ETH", "XBT" (Kraken's name for
    bitcoin) — not a pair.
    """
    snap = adapter.snapshot()          # equity, holdings and cash from one instant

    decision = risk.approve_trade(
        account_equity=snap["equity"],      # cash + coins at market
        entry_price=entry_price,
        stop_price=stop_price,
        symbol=asset,
        open_positions=snap["holdings"],    # {ASSET: value in USD}
        available_cash=snap["cash"],        # you cannot spend what you do not have
    )

    if not decision["approved"]:
        print(f"[SKIP] {asset}: {decision['reason']}")
        return

    s = decision["sizing"]
    print(f"[BUY]  {s['quantity']} {asset} @ ${entry_price:,.2f} "
          f"= ${s['notional']:,.2f}, stop ${stop_price:,.2f}, "
          f"risking ${s['risk_amount']:,.2f} (capped by {s['capped_by']})")

    adapter.place_buy_with_stop(
        asset=asset,
        quantity=s["quantity"],
        entry_price=entry_price,
        stop_price=stop_price,
    )


def on_exit_signal(asset: str) -> None:
    """
    Selling is never risk-gated: reducing exposure is allowed even while the
    bot is halted. The stop attached to the buy handles the involuntary case.
    """
    held = adapter.balances().get(asset, {}).get("available", 0.0)
    if held > 0:
        adapter.sell(asset, held)
        print(f"[SELL] {held} {asset}")


def hourly_check() -> None:
    """Kill-switch check between signals. See equity_check.py for the cron version."""
    halt = risk.update_equity(adapter.account_equity())
    if halt is not None:
        print(f"[HALT] {halt.value} — no new buys until manual_resume()")


if __name__ == "__main__":
    snap = adapter.snapshot()
    print(f"Equity ${snap['equity']:,.2f} | cash ${snap['cash']:,.2f} | "
          f"holdings {len(snap['holdings'])}")
    print(f"Risk status: {risk.status()}")
    print("\nImport on_signal() from your strategy, or run preflight_spot.py first.")
