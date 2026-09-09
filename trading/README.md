# Kraken Futures Risk Manager

Position sizing, exposure caps and kill-switches for a Kraken Futures perpetuals bot.

**Setting it up against a real account: [INSTALL_SPOT.md](INSTALL_SPOT.md)**
(spot crypto — the normal case). For perpetual futures, [INSTALL.md](INSTALL.md).

| File | Role |
|---|---|
| `risk_manager.py` | The risk engine. Standard library only, no exchange dependency. |
| `kraken_spot_adapter.py` | **Kraken Spot** integration — buying and holding coins. |
| `preflight_spot.py` | Verifies your spot account end to end. Places no orders. |
| `example_spot.py` | Spot wiring to copy from. |
| `equity_check.py` | Hourly kill-switch check for cron/systemd (spot or futures). |
| `kraken_adapter.py`, `preflight.py`, `example_usage.py` | The same, for perpetual futures. |
| `test_*.py` | 88 unit tests, no network. |

```bash
cd trading
python3 example_usage.py      # offline demo, no credentials needed
python3 -m unittest -v        # tests
```

## Why it is shaped this way

The module takes numbers in (equity, entry, stop, open positions) and returns a decision
out. Nothing in it talks to an exchange, so the rules that decide how much you can lose are
testable offline and reusable across strategies. Your bot keeps the API calls; this keeps
the arithmetic.

### 1. Size off the stop, not off a fixed notional

Quantity is `(equity x risk_per_trade_pct) / |entry - stop|`. A wide-stop setup gets a small
position and a tight-stop setup a large one — both lose the same dollars when wrong. Fixed
notional sizing does the opposite: it makes your worst setups your biggest losses.

### 2. Cap exposure three ways

| Cap | Field | Guards against |
|---|---|---|
| Per symbol | `max_position_pct` | One name blowing up the account |
| Per correlated group | `max_correlated_pct` | Four alt perps that are really one long-beta bet |
| All positions | `max_total_exposure_pct`, `max_open_positions` | Gross leverage creeping up unnoticed |

Caps are **reduce-to-fit**: a trade that would breach a cap is trimmed to the headroom that
exists and still taken. It is rejected only when there is no usable headroom, or when what
is left is below `min_notional`. `sizing["capped_by"]` names the binding constraint, so your
logs show *why* a position was smaller than the signal asked for.

A symbol in several groups gets the tightest headroom of them all.

### 3. Halt, and stay halted

- `daily_loss_limit_pct` — equity drop from the day's opening equity (UTC boundary).
- `max_drawdown_pct` — equity drop from the all-time peak.
- Equity at or below zero halts unconditionally.

The daily limit is checked first because it is the tighter, faster-moving switch.

State lives in SQLite, so a bot that dies mid-drawdown **wakes up still halted** — a
kill-switch a crash-loop can clear is not a kill-switch. Only `manual_resume()` clears it.

Resuming re-anchors the baselines still in breach (today's open, and the drawdown peak if
equity is still past the limit). Without that, resume is a no-op: the loss has not gone
anywhere, so the next equity update would halt again on the breach you just reviewed. Every
re-anchor is written to the `risk_events` audit table, so resuming repeatedly through a
losing run leaves a trail instead of being silent. Pass `reanchor=False` to stay halted
until equity actually recovers.

## Wiring it into the bot

```python
config = RiskConfig(
    risk_per_trade_pct=0.015,
    max_position_pct=0.08,
    max_correlated_pct=0.22,
    daily_loss_limit_pct=0.04,
    max_drawdown_pct=0.18,
    leverage=2.0,
    correlated_groups={"majors": ["PF_ETHUSD", "PF_SOLUSD", "PF_XRPUSD", "PF_LINKUSD"]},
)
risk = RiskManager(config, RiskState(db_path="risk_state.db"))

decision = risk.approve_trade(
    account_equity=equity,        # from client.get_wallets()
    entry_price=entry,
    stop_price=stop,
    symbol="PF_SOLUSD",
    open_positions=positions,     # {symbol: notional_usd} from client.get_open_positions()
)
if decision["approved"]:
    qty = decision["sizing"]["quantity"]
    ...                            # client.send_order(...) + the linked stop
else:
    log.info("skipped %s: %s", symbol, decision["reason"])
```

Two calls matter:

- `approve_trade(...)` on every signal. It refreshes equity and evaluates the kill-switches
  before sizing, so a halt is caught even if you never call `update_equity` yourself.
- `update_equity(...)` on a timer (hourly is plenty). Between signals is exactly when a
  drawdown breach would otherwise go unnoticed.

`risk.status()` returns a snapshot — equity, peak, drawdown, daily P&L, halt state — for a
log line or a dashboard tile.

## Operating notes

- **Send the stop with the entry.** This module sizes on the assumption that the stop
  exists on the exchange. A mental stop makes `risk_per_trade_pct` fiction.
- `open_positions` takes notional in USD, sign-insensitive: a short's exposure counts the
  same as a long's. Shorts do not offset longs here, which is deliberate — with perps you
  are usually paying to hold both, not hedged.
- `leverage` is used to report `margin_required`. It does not increase position size;
  the caps do the constraining. Set the exchange's own leverage no higher than this.
- Fees and funding are not modelled. On a bot that holds perps overnight, funding is a real
  drag — track it against realised P&L separately.
- `min_stop_distance_pct` rejects stops sitting inside the noise band, where a normal wick
  stops you out and the risk-based size is enormous.
- Set `quantity_step` to the contract's lot size. Rounding is always **down**, so rounding
  can never push a position past a cap.
- The defaults in `RiskConfig` are conservative starting points, not advice. Pick limits
  from your own drawdown tolerance and backtest them before running size.
