# Installing on Kraken Spot

Setup for a bot that buys and holds real crypto: keys, install, verification,
and the wiring. Written against **python-kraken-sdk 3.3.0** (`kraken.spot` —
`User`, `Trade`, `Market`).

Kraken Spot has no demo exchange, so safety comes from a different place:
**every order is validate-only until you deliberately switch that off.** Kraken
checks the order and reports what it would do without putting anything on the
book.

---

## 1. API keys

Create them at **https://www.kraken.com/u/security/api** (these are Spot keys —
Futures keys are separate and won't work here).

Permissions to tick:

- **Query Funds** — read balances. Required.
- **Query Open Orders & Trades** / **Query Closed Orders & Trades** — required.
- **Create & Modify Orders** — required to trade.
- **Cancel/Close Orders** — required for the dead man's switch.
- **Withdraw Funds — leave OFF.** Nothing here withdraws. A key that cannot move
  money off the exchange is a far smaller problem if it leaks.

Add an IP allowlist if you run from a fixed address.

## 2. Install

```bash
cd trading
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # python-kraken-sdk>=3.3,<4
```

## 3. Configure

```bash
export KRAKEN_API_KEY="your-key"
export KRAKEN_API_SECRET="your-secret"
export KRAKEN_QUOTE=USD
export KRAKEN_VALIDATE_ONLY=true    # orders are checked, never placed. Keep this on to start.
```

Risk limits, with spot-appropriate defaults:

```bash
export KRAKEN_RISK_PER_TRADE_PCT=0.01      # 1% of portfolio risked to the stop
export KRAKEN_MAX_POSITION_PCT=0.15        # 15% of portfolio in any one coin
export KRAKEN_MAX_CORRELATED_PCT=0.40      # 40% across a correlated group
export KRAKEN_MAX_TOTAL_EXPOSURE_PCT=0.80  # 80% in crypto, 20% cash floor
export KRAKEN_DAILY_LOSS_LIMIT_PCT=0.04    # stop buying at -4% on the day
export KRAKEN_MAX_DRAWDOWN_PCT=0.18        # stop buying at -18% from peak
export KRAKEN_CORRELATED_MAJORS="ETH,SOL,XRP,LINK"
```

Put them in a `.env` you source, or a systemd `EnvironmentFile` with `chmod 600`.
Never in git.

**Assets are base asset codes, not pairs:** `SOL`, `ETH`, `LINK` — and **`XBT`,
not `BTC`**, which is Kraken's name for bitcoin. A coin is one exposure however
you bought it.

## 4. Preflight

```bash
python3 preflight_spot.py --asset SOL
python3 preflight_spot.py --asset SOL --check-write   # validate-only order, nothing placed
```

It prints your equity, spendable cash, every holding with its portfolio share,
the pair's minimums, and a dry-run trade decision. Check three things:

- **Equity vs cash.** Equity is cash + coins. If you're 90% invested, the caps
  compute off a much bigger number than you can actually spend — which is why
  the cash check exists.
- **Staked/Earn balances** are flagged. They count toward exposure but cannot be
  sold instantly, so a stop cannot protect them.
- **`ordermin`**. If your risk budget sizes a trade below it, that coin is too
  expensive for your account size right now. Skip it — don't oversize to clear
  the minimum.

## 5. Wire it into your bot

Copy `example_spot.py`. The core is:

```python
snap = adapter.snapshot()          # equity, holdings, cash from one instant

decision = risk.approve_trade(
    account_equity=snap["equity"],    # cash + coins at market
    entry_price=entry, stop_price=stop, symbol="SOL",
    open_positions=snap["holdings"],  # {ASSET: USD value}
    available_cash=snap["cash"],      # you cannot spend what you do not have
)
if decision["approved"]:
    s = decision["sizing"]
    adapter.place_buy_with_stop("SOL", s["quantity"], entry, stop)
```

`place_buy_with_stop` sends the buy with the stop-loss attached as Kraken's
**conditional close**, so the exchange creates the stop only if the buy fills.
That ordering matters on spot: a standalone stop placed first would be trying to
sell coins you don't own yet.

Once at startup, and on each loop tick:

```python
adapter.arm_dead_mans_switch(60)   # Kraken cancels resting orders if the bot dies
```

## 6. First real orders

```bash
export KRAKEN_VALIDATE_ONLY=false   # the only switch that places real orders
export KRAKEN_RISK_PER_TRADE_PCT=0.0025   # 0.25% while you learn
```

Watch for all of this at least once before sizing up:

- [ ] A buy filling at roughly the size the log predicted
- [ ] The stop-loss visible in the Kraken UI **after** the buy fills
- [ ] A trade skipped by a cap — read `decision["reason"]`, don't just loosen it
- [ ] A stop triggering, with the loss landing near `risk_amount` plus fees
- [ ] A halt firing (drop the daily limit to force one) and `manual_resume()` clearing it
- [ ] Killing the process mid-halt and confirming it wakes up still halted

Back up `risk_state.db`. It holds your equity peak and halt state; losing it
resets the drawdown baseline and silently hands the bot a fresh 18% to lose.

## 7. Schedule the kill-switch check

`approve_trade()` only evaluates the switches when a signal fires. On spot you
can hold through a 20% drawdown without a single signal, so run this hourly:

```cron
0 * * * * cd /path/to/trading && . .venv/bin/activate && . ./.env && python3 equity_check.py >> equity.log 2>&1
```

It defaults to the spot account and exits `2` when halted, so cron emails you.

## 8. Spot-specific things that will bite you

- **Fees are a real drag.** Kraken spot taker fees start around 0.25–0.26% and
  fall with volume. A round trip is roughly half a percent of notional — on a
  1%-risk trade that is a meaningful slice of the edge. `adapter.fee_tier()`
  prints your actual tier. Factor it into whether a setup is worth taking.
- **A halt stops buying, not holding.** The kill-switch blocks new entries; it
  does not sell what you own. That's deliberate — force-selling a portfolio on a
  drawdown trigger is a decision for you, not a cron job. Your stops are the
  automatic exit.
- **Equity includes unrealised P&L**, so an open loser can trip the daily limit
  before you sell it. That's the number your drawdown is actually running at.
- **Kraken calls bitcoin XBT.** Write `XBT` in `KRAKEN_CORRELATED_MAJORS` and
  everywhere else, or the group cap silently never matches.
- **Staked and Earn balances** (`XXBT.S`, `USD.M`) are folded into the base asset
  for exposure but can't be sold on demand. If most of a holding is staked, your
  stop cannot protect it.
- **The daily baseline resets at 00:00 UTC** (8pm ET). A bad afternoon that halts
  you can hand you a fresh budget four hours later. Say the word and I'll make
  the boundary configurable.
- **Minimums.** Kraken rejects orders below `ordermin` volume or `costmin` value.
  The adapter refuses them up front rather than letting the exchange error out.
- **Dust.** Holdings under $1 are ignored, so crumbs from old trades don't eat a
  position slot.

## 9. On the risk defaults

They're starting points, not advice — pick them from your own tolerance:

- `risk_per_trade 1%` means a stop-out costs 1% of the portfolio. Ten in a row is
  a 10% drawdown, which happens more often than people plan for.
- `max_total_exposure 80%` keeps a 20% cash floor, so a halt still leaves you
  able to act.
- `max_correlated 40%` matters more in crypto than anywhere else: in a real
  drawdown, alt correlations converge on 1 and "five positions" becomes one bet.

---

## Files

| File | Role |
|---|---|
| `risk_manager.py` | Sizing, caps, kill-switches. No exchange dependency. |
| `kraken_spot_adapter.py` | Kraken Spot ↔ risk manager translation. |
| `preflight_spot.py` | Verify account, permissions, minimums. No live orders. |
| `example_spot.py` | Wiring to copy from. |
| `equity_check.py` | Hourly kill-switch check for cron/systemd. |
| `test_*.py` | 88 tests, no network: `python3 -m unittest -v` |
