#!/usr/bin/env python3
"""
equity_check.py

Standalone kill-switch check, meant for cron or a systemd timer. Reads equity
from Kraken, feeds it to the risk manager, and exits non-zero if the bot is
halted — so cron emails you, or your monitoring notices.

Defaults to the SPOT account. Set KRAKEN_MARKET=futures for the perps account.

Run it hourly. Between signals is exactly when a drawdown breach would
otherwise go unnoticed: approve_trade() only checks when a signal fires, and a
quiet market can be the one bleeding your account.

    python3 equity_check.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import os


def _load_market():
    """Spot unless explicitly told otherwise."""
    market = os.environ.get("KRAKEN_MARKET", "spot").strip().lower()
    if market == "futures":
        from kraken_adapter import build_adapter_from_env, build_risk_manager

        return "futures", build_adapter_from_env(), build_risk_manager()
    from kraken_spot_adapter import build_spot_adapter_from_env, build_spot_risk_manager

    return "spot", build_spot_adapter_from_env(), build_spot_risk_manager()


def main() -> int:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    market, adapter, risk = _load_market()

    equity = adapter.account_equity()
    halt = risk.update_equity(equity)
    status = risk.status()

    print(f"{stamp} [{market}] equity=${equity:,.2f} peak=${status['peak_equity'] or 0:,.2f} "
          f"dd={status['drawdown_pct']:.2%} day={status['daily_pnl_pct']:+.2%} "
          f"halted={status['halted']}")

    if halt is not None:
        print(f"{stamp} HALTED ({halt.value}): {status['note']}", file=sys.stderr)
        print("New trades are rejected until a human calls manual_resume().", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"equity_check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
