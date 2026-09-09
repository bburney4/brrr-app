"""
example_usage.py

Shows how to wire risk_manager.py into your Kraken Futures perpetuals bot.
Replace the marked sections with real python-kraken-sdk calls.
"""

from risk_manager import RiskConfig, RiskState, RiskManager

# ---------------------------------------------------------------------------
# 1. One-time setup (do this once when the bot starts)
# ---------------------------------------------------------------------------

config = RiskConfig(
    max_position_pct=0.08,
    max_correlated_pct=0.22,
    risk_per_trade_pct=0.015,
    daily_loss_limit_pct=0.04,
    max_drawdown_pct=0.18,
    leverage=2.0,
    correlated_groups={"majors": ["PF_ETHUSD", "PF_SOLUSD", "PF_XRPUSD", "PF_LINKUSD"]},
)

state = RiskState(db_path="risk_state.db")   # persists across restarts
risk = RiskManager(config, state)


# ---------------------------------------------------------------------------
# 2. Each loop tick / signal check (this is what runs repeatedly)
# ---------------------------------------------------------------------------

def get_account_equity_from_kraken() -> float:
    """
    Replace with: client.get_wallets() or the futures account balance call
    from python-kraken-sdk. Should return total account equity in USD.
    """
    return 10_000.00  # placeholder for demo


def get_open_positions_from_kraken() -> dict:
    """
    Replace with: client.get_open_positions() from python-kraken-sdk,
    reshaped into {symbol: notional_value}.
    """
    return {"PF_ETHUSD": 1200.00}  # placeholder for demo


def send_order_to_kraken(symbol: str, quantity: float, side: str, entry_price: float,
                          stop_price: float):
    """
    Replace with the actual python-kraken-sdk order call, e.g.
    client.send_order(orderType="lmt", symbol=symbol, side=side,
                       size=quantity, limitPrice=entry_price, ...)
    plus a linked stop order at stop_price.
    """
    print(f"[ORDER SENT] {side} {quantity} {symbol} @ {entry_price}, stop @ {stop_price}")


def on_signal(symbol: str, side: str, entry_price: float, stop_price: float):
    """Call this from wherever your strategy currently decides to enter a trade."""
    equity = get_account_equity_from_kraken()
    open_positions = get_open_positions_from_kraken()

    decision = risk.approve_trade(
        account_equity=equity,
        entry_price=entry_price,
        stop_price=stop_price,
        symbol=symbol,
        open_positions=open_positions,
    )

    if not decision["approved"]:
        print(f"[TRADE SKIPPED] {symbol}: {decision['reason']}")
        return

    sizing = decision["sizing"]
    print(f"[TRADE APPROVED] {symbol} qty={sizing['quantity']} "
          f"notional=${sizing['notional']} margin=${sizing['margin_required']}")
    send_order_to_kraken(symbol, sizing["quantity"], side, entry_price, stop_price)


# ---------------------------------------------------------------------------
# 3. Standalone equity check (run this on its own schedule too, e.g. hourly,
#    so a drawdown/daily-loss breach is caught even between signals)
# ---------------------------------------------------------------------------

def periodic_equity_check():
    equity = get_account_equity_from_kraken()
    halt = risk.update_equity(equity)
    if halt is not None:
        print(f"[HALT TRIGGERED] {halt.value} — bot will reject new trades until manual_resume()")


# ---------------------------------------------------------------------------
# Demo run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("--- Demo 1: approved trade within limits ---")
    on_signal("PF_SOLUSD", "buy", entry_price=180.0, stop_price=174.0)

    print("\n--- Demo 2: trade rejected — correlated-group exposure cap ---")
    # Simulate existing majors exposure already near the 22% combined cap
    def get_open_positions_large():
        return {"PF_ETHUSD": 3800.0, "PF_SOLUSD": 300.0, "PF_XRPUSD": 200.0}
    globals()["get_open_positions_from_kraken"] = get_open_positions_large
    on_signal("PF_LINKUSD", "buy", entry_price=20.0, stop_price=19.0)

    print("\n--- Demo 3: drawdown kill-switch (separate risk instance, own state) ---")
    print("    (uses a fresh day so the tighter same-day loss limit doesn't fire first)")
    risk2 = RiskManager(config, RiskState(db_path="risk_state_demo3.db"))
    risk2.update_equity(10_000.0)                   # day 1: sets peak + day-start baseline
    risk2.state.save(day_start_date="2020-01-01")   # force a new "day" for the next call
    halt = risk2.update_equity(8_100.0)             # new day: the daily-loss baseline resets
                                                    # to 8,100, but that is 19% off the peak
    print("Halt reason:", halt)
    print("Is halted:", risk2.is_halted())

    print("\n--- Demo 4: trade attempt on risk2 while it's halted ---")
    decision = risk2.approve_trade(
        account_equity=8_100.0, entry_price=3000.0, stop_price=2900.0,
        symbol="PF_ETHUSD", open_positions={},
    )
    print("Approved:", decision["approved"], "-", decision.get("reason", ""))

    print("\n--- Demo 5: manual resume clears the halt ---")
    risk2.manual_resume("Reviewed cause, resuming after reconfiguring stop distances")
    print("Is halted:", risk2.is_halted())
