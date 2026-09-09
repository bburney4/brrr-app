"""
kraken_spot_adapter.py

Connects risk_manager.py to a Kraken SPOT account (buying and holding real
coins) via python-kraken-sdk. Use this one unless you are deliberately trading
perpetual futures — see kraken_adapter.py for that.

Verified against python-kraken-sdk 3.3.0 (kraken.spot User/Trade/Market).

Spot differs from futures in ways that matter to risk:

  Equity is cash PLUS coins at market value. A portfolio that is 90% in coins
  can show flat cash while the equity the drawdown switch watches falls hard.

  You cannot spend cash you do not have. The percentage caps are computed off
  equity, so without a cash check they will happily approve a buy bigger than
  your remaining balance. account_equity() and available_cash() are separate
  numbers here, and both belong in approve_trade().

  There is no leverage and no margin. Set RiskConfig(leverage=1.0), which makes
  margin_required equal the cash the trade costs.

  Stops attach to the buy as a Kraken CONDITIONAL CLOSE, so the stop-loss is
  created by the exchange only if the buy actually fills. No reduceOnly flag
  exists on spot, and a separate stop order placed before a fill would try to
  sell coins you do not own yet.

  Positions are keyed by BASE ASSET ("ETH", "SOL", "XBT"), not by pair, because
  a coin is one exposure however you bought it. Write RiskConfig.correlated_
  groups in those terms: {"majors": ["ETH", "SOL", "XRP", "LINK"]}.
  Kraken calls bitcoin XBT, not BTC.
"""

from __future__ import annotations

import math
import os
from typing import Dict, Iterable, List, Optional, Tuple

# Imported lazily so the pure helpers (and their tests) work without the SDK.
try:
    from kraken.spot import Market, Trade, User

    _SDK_IMPORT_ERROR: Optional[BaseException] = None
except ImportError as exc:  # pragma: no cover - dependency guard
    Market = Trade = User = None  # type: ignore[assignment]
    _SDK_IMPORT_ERROR = exc

from risk_manager import RiskConfig, RiskManager, RiskState


__all__ = [
    "KrakenSpotAdapter",
    "PairNotFound",
    "normalize_asset",
    "parse_balances",
    "portfolio_value",
    "holdings_notional",
    "build_spot_adapter_from_env",
    "load_spot_config_from_env",
]

# Fee credits, not money. Counting them as equity would inflate it.
IGNORED_ASSETS = {"KFEE"}

# Assets treated as cash for a USD-quoted bot.
CASH_ASSETS = {"USD", "ZUSD"}


class PairNotFound(LookupError):
    """Raised when an asset has no tradable pair against the quote currency."""


# ---------------------------------------------------------------------------
# Pure helpers — unit-tested without the network
# ---------------------------------------------------------------------------

def normalize_asset(code: str) -> str:
    """
    Turn a Kraken balance key into a plain asset code.

    Kraken mixes legacy four-character codes with modern ones, and suffixes
    staked or Earn balances:

        'XXBT'    -> 'XBT'      (legacy X prefix)
        'ZUSD'    -> 'USD'      (legacy Z prefix)
        'SOL'     -> 'SOL'
        'XXBT.S'  -> 'XBT'      (staked; same exposure, different liquidity)
        'USD.M'   -> 'USD'
    """
    code = (code or "").strip().upper()
    if not code:
        return ""
    base = code.split(".", 1)[0]
    # Legacy codes are exactly four characters behind an X or Z.
    if len(base) == 4 and base[0] in ("X", "Z"):
        base = base[1:]
    return base


def is_illiquid_balance(code: str) -> bool:
    """True for staked/Earn balances, which cannot be sold instantly."""
    return "." in (code or "")


def parse_balances(payload: dict) -> Dict[str, Dict[str, float]]:
    """
    Turn get_balances() into {ASSET: {"total": x, "available": y, "locked": z}}.

    'available' subtracts hold_trade, the part already committed to open orders,
    so a resting buy cannot be counted twice as spendable cash. Staked balances
    are folded into the base asset for exposure, and reported separately as
    'illiquid' so callers can warn about them.
    """
    balances: Dict[str, Dict[str, float]] = {}
    for raw_code, entry in (payload or {}).items():
        if raw_code in IGNORED_ASSETS:
            continue
        asset = normalize_asset(raw_code)
        if not asset or asset in IGNORED_ASSETS:
            continue
        if isinstance(entry, dict):
            total = float(entry.get("balance") or 0.0)
            held = float(entry.get("hold_trade") or 0.0)
        else:  # get_balance() style: a bare number
            total = float(entry or 0.0)
            held = 0.0
        if total == 0.0 and held == 0.0:
            continue
        slot = balances.setdefault(
            asset, {"total": 0.0, "available": 0.0, "locked": 0.0, "illiquid": 0.0}
        )
        slot["total"] += total
        slot["locked"] += held
        slot["available"] += max(0.0, total - held)
        if is_illiquid_balance(raw_code):
            slot["illiquid"] += total
    return balances


def portfolio_value(
    balances: Dict[str, Dict[str, float]],
    prices: Dict[str, float],
    quote: str = "USD",
) -> float:
    """
    Total account equity: cash plus every coin at its market price.

    This is the number the drawdown and daily-loss switches watch. Assets with
    no price are skipped rather than guessed at — better to under-report equity
    (which tightens sizing) than to invent it.
    """
    total = 0.0
    for asset, slot in balances.items():
        amount = slot["total"]
        if not amount:
            continue
        if asset == quote or asset in CASH_ASSETS:
            total += amount
            continue
        price = prices.get(asset)
        if price:
            total += amount * float(price)
    return total


def holdings_notional(
    balances: Dict[str, Dict[str, float]],
    prices: Dict[str, float],
    quote: str = "USD",
    dust_threshold: float = 1.0,
) -> Dict[str, float]:
    """
    {ASSET: value in quote currency} for the coins you hold — the open_positions
    argument to approve_trade. Cash is excluded (it is not exposure), and dust
    below `dust_threshold` is dropped so leftover crumbs from old trades do not
    eat into your position count.
    """
    positions: Dict[str, float] = {}
    for asset, slot in balances.items():
        if asset == quote or asset in CASH_ASSETS:
            continue
        price = prices.get(asset)
        if not price:
            continue
        value = slot["total"] * float(price)
        if value >= dust_threshold:
            positions[asset] = value
    return positions


def available_quote(balances: Dict[str, Dict[str, float]], quote: str = "USD") -> float:
    """Spendable cash — total minus anything already committed to open orders."""
    slot = balances.get(quote) or balances.get(normalize_asset(quote)) or {}
    return float(slot.get("available", 0.0))


def index_pairs(asset_pairs: dict, quote: str = "USD") -> Dict[str, Tuple[str, dict]]:
    """
    Map base asset -> (kraken pair id, pair spec) for pairs quoted in `quote`.

    Kraken pair ids are not guessable ('XXBTZUSD', 'SOLUSD'), so build the index
    from the exchange's own list rather than string-concatenating.
    """
    index: Dict[str, Tuple[str, dict]] = {}
    for pair_id, spec in (asset_pairs or {}).items():
        if not isinstance(spec, dict):
            continue
        base = normalize_asset(spec.get("base", ""))
        pair_quote = normalize_asset(spec.get("quote", ""))
        if not base or pair_quote != normalize_asset(quote):
            continue
        # Prefer the plain spot pair over dark-pool/derived duplicates.
        if base not in index or len(pair_id) < len(index[base][0]):
            index[base] = (pair_id, spec)
    return index


def extract_prices(ticker_payload: dict, pair_to_asset: Dict[str, str]) -> Dict[str, float]:
    """Pull last-trade prices out of get_ticker() and key them by asset."""
    prices: Dict[str, float] = {}
    for pair_id, data in (ticker_payload or {}).items():
        asset = pair_to_asset.get(pair_id)
        if not asset or not isinstance(data, dict):
            continue
        close = data.get("c")
        if isinstance(close, (list, tuple)) and close:
            try:
                prices[asset] = float(close[0])
            except (TypeError, ValueError):
                continue
    return prices


def order_minimums(pair_spec: dict) -> Dict[str, Optional[float]]:
    """Kraken's per-pair minimums and decimal limits."""
    def _float(name):
        value = (pair_spec or {}).get(name)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _int(name):
        value = (pair_spec or {}).get(name)
        return value if isinstance(value, int) else None

    return {
        "ordermin": _float("ordermin"),
        "costmin": _float("costmin"),
        "lot_decimals": _int("lot_decimals"),
        "pair_decimals": _int("pair_decimals"),
    }


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class KrakenSpotAdapter:
    """The Kraken Spot calls this bot needs, and nothing else."""

    def __init__(
        self,
        key: str,
        secret: str,
        quote: str = "USD",
        validate_only: bool = False,
    ) -> None:
        if User is None:  # pragma: no cover - dependency guard
            raise ImportError(
                "python-kraken-sdk is required: pip install 'python-kraken-sdk>=3.3,<4'"
            ) from _SDK_IMPORT_ERROR
        if not key or not secret:
            raise ValueError("API key and secret are required")
        self.quote = normalize_asset(quote)
        # When True every order is sent with validate=True: Kraken checks it and
        # returns what it would do, without putting anything on the book.
        self.validate_only = validate_only
        self.user = User(key=key, secret=secret)
        self.trade = Trade(key=key, secret=secret)
        self.market = Market()
        self._pairs: Dict[str, Tuple[str, dict]] = {}

    # -- reference data -----------------------------------------------------

    def pairs(self) -> Dict[str, Tuple[str, dict]]:
        if not self._pairs:
            self._pairs = index_pairs(self.market.get_asset_pairs(), self.quote)
        return self._pairs

    def pair_for(self, asset: str) -> str:
        """Kraken's pair id for buying `asset` with the quote currency."""
        asset = normalize_asset(asset)
        entry = self.pairs().get(asset)
        if not entry:
            raise PairNotFound(
                f"no {asset}/{self.quote} pair on Kraken Spot "
                f"(bitcoin is 'XBT' here, not 'BTC')"
            )
        return entry[0]

    def pair_spec(self, asset: str) -> dict:
        self.pair_for(asset)  # raises if unknown
        return self.pairs()[normalize_asset(asset)][1]

    def minimums(self, asset: str) -> Dict[str, Optional[float]]:
        """Minimum order size and cost — Kraken rejects anything smaller."""
        return order_minimums(self.pair_spec(asset))

    # -- account reads ------------------------------------------------------

    def balances(self) -> Dict[str, Dict[str, float]]:
        return parse_balances(self.user.get_balances())

    def prices(self, assets: Iterable[str]) -> Dict[str, float]:
        """Last-trade price in the quote currency for each asset."""
        wanted = [normalize_asset(a) for a in assets]
        pair_to_asset: Dict[str, str] = {}
        pair_ids: List[str] = []
        for asset in wanted:
            if not asset or asset == self.quote or asset in CASH_ASSETS:
                continue
            entry = self.pairs().get(asset)
            if not entry:
                continue
            pair_to_asset[entry[0]] = asset
            pair_ids.append(entry[0])
        if not pair_ids:
            return {}
        return extract_prices(self.market.get_ticker(pair=pair_ids), pair_to_asset)

    def snapshot(self) -> Dict[str, object]:
        """
        One round of account reads: equity, holdings and spendable cash.

        Taken together so all three describe the same instant, and so a signal
        costs two private calls instead of six.
        """
        balances = self.balances()
        prices = self.prices(balances.keys())
        return {
            "equity": portfolio_value(balances, prices, self.quote),
            "holdings": holdings_notional(balances, prices, self.quote),
            "cash": available_quote(balances, self.quote),
            "prices": prices,
            "balances": balances,
        }

    def account_equity(self) -> float:
        return float(self.snapshot()["equity"])

    def holdings(self) -> Dict[str, float]:
        return dict(self.snapshot()["holdings"])  # type: ignore[arg-type]

    def available_cash(self) -> float:
        return float(self.snapshot()["cash"])

    # -- orders -------------------------------------------------------------

    def place_buy_with_stop(
        self,
        asset: str,
        quantity: float,
        entry_price: float,
        stop_price: float,
        order_type: str = "limit",
        client_id: Optional[str] = None,
        validate: Optional[bool] = None,
    ) -> dict:
        """
        Buy `asset`, with the stop-loss attached as Kraken's conditional close.

        The stop only comes into existence if the buy fills, which is what you
        want on spot: a standalone stop placed first would be trying to sell
        coins you do not own yet.

        Kraken truncates the volume and price to the pair's allowed decimals
        (truncate=True), and the stop triggers on the index price rather than
        last, which is harder to wick through on a thin book.
        """
        asset = normalize_asset(asset)
        pair = self.pair_for(asset)
        if quantity <= 0:
            raise ValueError(f"quantity must be positive, got {quantity!r}")
        if stop_price >= entry_price:
            raise ValueError(
                f"stop {stop_price} must sit below the buy price {entry_price}"
            )

        mins = self.minimums(asset)
        ordermin, costmin = mins["ordermin"], mins["costmin"]
        if ordermin and quantity < ordermin:
            raise ValueError(
                f"{quantity} {asset} is below Kraken's {ordermin} minimum for {pair}. "
                "Either your risk budget is too small for this coin or the caps "
                "trimmed the trade too far — skip it rather than oversizing."
            )
        cost = quantity * entry_price
        if costmin and cost < costmin:
            raise ValueError(
                f"order cost {cost:.2f} {self.quote} is below the {costmin} minimum for {pair}"
            )

        kwargs = dict(
            ordertype=order_type,
            side="buy",
            pair=pair,
            volume=quantity,
            close_ordertype="stop-loss",
            close_price=stop_price,
            trigger="index",
            truncate=True,
            validate=self.validate_only if validate is None else validate,
        )
        if order_type != "market":
            kwargs["price"] = entry_price
        if client_id:
            kwargs["cl_ord_id"] = client_id
        return self.trade.create_order(**kwargs)

    def sell(self, asset: str, quantity: float, order_type: str = "market",
             price: Optional[float] = None, validate: Optional[bool] = None) -> dict:
        """
        Sell a holding. Deliberately not risk-gated: reducing exposure is always
        allowed, including while the bot is halted.
        """
        kwargs = dict(
            ordertype=order_type,
            side="sell",
            pair=self.pair_for(asset),
            volume=quantity,
            truncate=True,
            validate=self.validate_only if validate is None else validate,
        )
        if order_type != "market" and price is not None:
            kwargs["price"] = price
        return self.trade.create_order(**kwargs)

    def cancel_all(self) -> dict:
        return self.trade.cancel_all_orders()

    def arm_dead_mans_switch(self, seconds: int = 60) -> dict:
        """
        Kraken cancels every open order if the bot stops checking in. Re-arm on
        each loop tick; call with 0 to disarm. Covers the one failure the risk
        manager cannot: the process dying with orders resting on the book.
        """
        return self.trade.cancel_all_orders_after_x(timeout=seconds)

    def fee_tier(self, asset: str = "XBT") -> dict:
        """Your current maker/taker fees — spot fees are a real drag on a small edge."""
        return self.user.get_trade_volume(pair=self.pair_for(asset))


# ---------------------------------------------------------------------------
# Environment wiring
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() not in ("false", "0", "no")


def load_spot_config_from_env() -> RiskConfig:
    """
    RiskConfig for spot. leverage is pinned to 1.0 — there is none — so
    margin_required reports the cash a trade actually costs.

    KRAKEN_RISK_PER_TRADE_PCT, KRAKEN_MAX_POSITION_PCT, KRAKEN_MAX_CORRELATED_PCT,
    KRAKEN_MAX_TOTAL_EXPOSURE_PCT, KRAKEN_DAILY_LOSS_LIMIT_PCT,
    KRAKEN_MAX_DRAWDOWN_PCT, KRAKEN_CORRELATED_MAJORS.
    """
    majors = os.environ.get("KRAKEN_CORRELATED_MAJORS", "ETH,SOL,XRP,LINK")
    groups = {"majors": [normalize_asset(s) for s in majors.split(",") if s.strip()]}
    return RiskConfig(
        risk_per_trade_pct=_env_float("KRAKEN_RISK_PER_TRADE_PCT", 0.01),
        max_position_pct=_env_float("KRAKEN_MAX_POSITION_PCT", 0.15),
        max_correlated_pct=_env_float("KRAKEN_MAX_CORRELATED_PCT", 0.40),
        # Spot's natural risk-off is cash. This is your crypto allocation cap.
        max_total_exposure_pct=_env_float("KRAKEN_MAX_TOTAL_EXPOSURE_PCT", 0.80),
        daily_loss_limit_pct=_env_float("KRAKEN_DAILY_LOSS_LIMIT_PCT", 0.04),
        max_drawdown_pct=_env_float("KRAKEN_MAX_DRAWDOWN_PCT", 0.18),
        leverage=1.0,
        correlated_groups=groups,
    )


def build_spot_adapter_from_env() -> KrakenSpotAdapter:
    """
    Credentials from the environment; never in source control.

        KRAKEN_API_KEY, KRAKEN_API_SECRET   (Kraken Spot keys)
        KRAKEN_QUOTE            default USD
        KRAKEN_VALIDATE_ONLY    default true — orders are checked, not placed
    """
    key = os.environ.get("KRAKEN_API_KEY", "")
    secret = os.environ.get("KRAKEN_API_SECRET", "")
    if not key or not secret:
        raise ValueError(
            "set KRAKEN_API_KEY and KRAKEN_API_SECRET from "
            "https://www.kraken.com/u/security/api"
        )
    return KrakenSpotAdapter(
        key=key,
        secret=secret,
        quote=os.environ.get("KRAKEN_QUOTE", "USD"),
        # Safe by default: you must switch this off deliberately to trade.
        validate_only=_env_bool("KRAKEN_VALIDATE_ONLY", True),
    )


def build_spot_risk_manager(db_path: str = "risk_state.db") -> RiskManager:
    return RiskManager(load_spot_config_from_env(), RiskState(db_path=db_path))
