# Installing on Kraken

**Trading spot crypto? Read [INSTALL_SPOT.md](INSTALL_SPOT.md) instead — that is
the normal case.** This file covers Kraken *Futures* perpetuals, which is a
different account, different keys, and leverage you probably do not want.

---

# Installing on Kraken Futures (perpetuals)

End-to-end setup: account, keys, dependencies, verification, and the wiring that
replaces the placeholder functions in `example_usage.py`.

Written against **python-kraken-sdk 3.3.0** (`kraken.futures` — `User`, `Trade`,
`Market`). Method names and permission requirements below are taken from that
version's own API surface and documentation.

**Do steps 1–7 on the demo exchange first.** Step 8 is the only one that risks
real money, and it should be boring by the time you get there.

---

## 1. Accounts

| | Where | Notes |
|---|---|---|
| Demo | https://demo-futures.kraken.com | Separate signup from your live account, funded with play money. Its API keys work *only* there. |
| Live | https://futures.kraken.com | Futures availability and product access depend on your jurisdiction — confirm your account is enabled for the PF_ perpetuals you plan to trade. |

Futures collateral sits in the **flex (multi-collateral) account**. If you have
funds on Kraken spot, transfer them to the futures wallet — the bot reads equity
from the flex account and will tell you plainly if it's empty.

## 2. API keys

Create the key inside the environment you're targeting (demo keys on the demo
site, live keys on the live site). Futures keys are separate from Kraken spot keys.

Permissions:

- **General API — Read Only** — enough for `get_wallets`, `get_open_positions`,
  and everything `preflight.py` does without `--check-write`.
- **General API — Full Access** — required by `create_order`. The bot needs this
  to trade.
- **Withdrawal — leave OFF.** Nothing here withdraws. A key that can't move
  funds off the exchange is a much smaller problem if it leaks.

If you run from a fixed IP (a VPS), add an IP allowlist to the key.

## 3. Install

```bash
cd trading
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # python-kraken-sdk>=3.3,<4
```

## 4. Credentials

Environment variables only — never in source, never in git.

```bash
export KRAKEN_FUTURES_KEY="your-key"
export KRAKEN_FUTURES_SECRET="your-secret"
export KRAKEN_SANDBOX=true          # demo. Must be explicitly false to reach live money.
```

Risk limits are env-configurable too, so you can tighten them without editing code
(defaults in parentheses):

```bash
export KRAKEN_RISK_PER_TRADE_PCT=0.01      # 1% of equity risked per trade
export KRAKEN_MAX_POSITION_PCT=0.08        # 8% notional cap per symbol
export KRAKEN_MAX_CORRELATED_PCT=0.22      # 22% across a correlated group
export KRAKEN_MAX_TOTAL_EXPOSURE_PCT=0.60  # 60% gross
export KRAKEN_DAILY_LOSS_LIMIT_PCT=0.04    # halt at -4% on the day
export KRAKEN_MAX_DRAWDOWN_PCT=0.18        # halt at -18% from peak
export KRAKEN_LEVERAGE=2.0
export KRAKEN_CORRELATED_MAJORS="PF_ETHUSD,PF_SOLUSD,PF_XRPUSD,PF_LINKUSD"
```

Put them in a `.env` you source, or a systemd `EnvironmentFile` with `chmod 600`.

## 5. Preflight

```bash
python3 preflight.py --symbol PF_SOLUSD
python3 preflight.py --symbol PF_SOLUSD --check-write   # confirms order permissions
```

It places no orders. `--check-write` arms Kraken's dead man's switch for a moment
and disarms it, which proves the key has Full Access without touching the book.

It prints your equity, parsed positions, the instrument's **lot step** and tick
size, current risk state, and a dry-run trade decision. Two things to act on:

- The **lot step** it prints goes into `RiskConfig(quantity_step=...)`. Without
  it, sizes are rounded to 8 decimals and the exchange may reject them.
- If it says a precision field wasn't recognised, it dumps the raw instrument
  payload. Send me that and I'll wire the field in.

## 6. Wire it into your bot

`example_usage.py` has three placeholder functions. Replace them:

```python
from kraken_adapter import build_adapter_from_env, load_config_from_env
from risk_manager import RiskManager, RiskState

adapter = build_adapter_from_env()
config  = load_config_from_env()
risk    = RiskManager(config, RiskState(db_path="risk_state.db"))

def on_signal(symbol, side, entry_price, stop_price):
    decision = risk.approve_trade(
        account_equity=adapter.account_equity(),      # was get_account_equity_from_kraken
        entry_price=entry_price,
        stop_price=stop_price,
        symbol=symbol,
        open_positions=adapter.open_positions(),      # was get_open_positions_from_kraken
    )
    if not decision["approved"]:
        log.info("skipped %s: %s", symbol, decision["reason"])
        return

    s = decision["sizing"]
    adapter.place_entry_with_stop(                    # was send_order_to_kraken
        symbol=symbol, side=side, quantity=s["quantity"],
        entry_price=entry_price, stop_price=stop_price,
    )
    log.info("entered %s %s @ %s, stop %s, risking $%s",
             side, s["quantity"], entry_price, stop_price, s["risk_amount"])
```

`place_entry_with_stop` sends a limit entry and a **reduceOnly** stop triggered on
the mark price. ReduceOnly matters: if the entry never fills, the stop can't open
a position by itself, and if the entry fills partially, the stop can't oversize.

Two things worth doing once at startup:

```python
adapter.set_leverage("PF_SOLUSD", 2.0)   # cap leverage exchange-side to match config
adapter.arm_dead_mans_switch(60)         # re-arm each loop; Kraken cancels orders if you die
```

The dead man's switch covers the one failure the risk manager can't: the process
dying with resting orders on the book.

## 7. Demo run

Trade the demo for long enough to see all of this happen at least once:

- [ ] A trade approved, sized as expected, with the stop visible in the Kraken UI
- [ ] A trade **skipped** by a cap — check `decision["reason"]` names the right one
- [ ] A stop actually triggering, and the loss landing near `risk_amount`
- [ ] `equity_check.py` running on a schedule and logging equity
- [ ] A halt firing (drop `KRAKEN_DAILY_LOSS_LIMIT_PCT` to something tiny to force one),
      the bot refusing trades, and `manual_resume()` clearing it
- [ ] Killing the process mid-halt and confirming it wakes up still halted

If a skipped trade surprises you, that's the caps doing their job — read the
reason before you loosen anything.

## 8. Going live

```bash
export KRAKEN_SANDBOX=false        # the only switch that points at real money
export KRAKEN_FUTURES_KEY=...      # LIVE keys — demo keys will not authenticate
export KRAKEN_FUTURES_SECRET=...
python3 preflight.py --symbol PF_SOLUSD    # it will print *** LIVE — REAL MONEY ***
```

Start at `KRAKEN_RISK_PER_TRADE_PCT=0.0025` (0.25%) for the first few weeks. The
difference between demo and live is fills, funding and your own behaviour, none
of which the demo teaches you. Size up once live results match what the demo
predicted, not before.

Back up `risk_state.db`. It holds your equity peak and halt state; losing it
resets the drawdown baseline and silently gives the bot a fresh 18% to lose.

## 9. Schedule the equity check

`approve_trade()` only evaluates the kill-switches when a signal fires. A quiet
market can bleed you for hours without one. Run this hourly:

**cron**

```cron
0 * * * * cd /path/to/trading && . .venv/bin/activate && . ./.env && python3 equity_check.py >> equity.log 2>&1
```

**systemd** (`kraken-equity-check.service` + `.timer`)

```ini
[Unit]
Description=Kraken futures equity / kill-switch check

[Service]
Type=oneshot
WorkingDirectory=/path/to/trading
EnvironmentFile=/path/to/trading/.env
ExecStart=/path/to/trading/.venv/bin/python equity_check.py
```

```ini
[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

It exits `2` when halted, so cron emails you and systemd marks the unit failed.

## 10. Things that will bite you

- **PF_ vs PI_.** PF_ perps are sized in base units (ETH); PI_/FI_ inverse
  contracts are sized in USD. The adapter refuses non-PF_ orders because the
  base-unit quantity would be wrong by a factor of the price. Existing inverse
  positions still count toward your caps, at size-as-notional.
- **Kraken returns symbols lowercase** (`pf_ethusd`). The adapter upper-cases
  them; if you match symbols anywhere else in your bot, do the same or your
  correlated groups silently stop matching.
- **Funding is not modelled.** On perps held overnight it's a real drag on a
  1%-per-trade edge. Track it against realised P&L separately.
- **Equity includes unrealised P&L** (`portfolioValue`), so an open loser can
  trip the daily limit before you close it. That's intended — it's the number
  your drawdown is actually running at.
- **The daily baseline resets at 00:00 UTC** (8pm ET), mid-session for US hours.
  A bad afternoon that halts you can hand you a fresh budget four hours later.
  Say the word and I'll make the boundary configurable.
- **Rate limits.** Each `approve_trade` makes two private calls (wallets +
  positions). At a few signals a minute that's fine; polling every second is not.
  Cache equity between signals if your loop is tight.
- **Demo data is not live data.** Fills there are optimistic. Treat demo results
  as a test of your plumbing, not of your edge.

---

## Files

| File | Role |
|---|---|
| `risk_manager.py` | Sizing, caps, kill-switches. No exchange dependency. |
| `kraken_adapter.py` | Kraken Futures ↔ risk manager translation. |
| `preflight.py` | Verify account, permissions and payload shapes. No orders. |
| `equity_check.py` | Hourly kill-switch check for cron/systemd. |
| `example_usage.py` | Wiring example with a runnable offline demo. |
| `test_*.py` | 88 tests, no network: `python3 -m unittest -v` |
