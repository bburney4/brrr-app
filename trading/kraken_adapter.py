"""
kraken_adapter.py

Connects risk_manager.py to a live Kraken Futures account via python-kraken-sdk.

risk_manager.py deliberately knows nothing about exchanges. This module is the
translation layer: it reads equity and open positions off the account, converts
them into the plain numbers the risk manager expects, and turns an approved
trade into an entry order with a linked stop.

Verified against python-kraken-sdk 3.3.0 (kraken.futures User/Trade/Market).
Response field names come from the SDK's own documented payloads; run
preflight.py against your account to confirm them before trading real size.

Sizing note, and it matters:

    PF_* (multi-collateral perpetuals) are sized in units of the BASE asset,
    so quantity = notional / price, which is what risk_manager returns.

    PI_*/FI_* (inverse futures) are sized in USD contracts, where that formula
    would be wrong by a factor of the price. This adapter refuses to place
    orders on anything but PF_* for that reason. Existing inverse positions are
    still counted toward your exposure caps, using size directly as notional.
"""

from __future__ import annotations

import math
import os
from typing import Dict, Optional

# Imported lazily so the pure helpers below (and their tests) work without the
# SDK installed; only constructing the adapter actually needs it.
try:
    from kraken.futures import Market, Trade, User

    _SDK_IMPORT_ERROR: Optional[BaseException] = None
except ImportError as exc:  # pragma: no cover - dependency guard
    Market = Trade = User = None  # type: ignore[assignment]
    _SDK_IMPORT_ERROR = exc

from risk_manager import RiskConfig, RiskManager, RiskState


__all__ = [
    "KrakenFuturesAdapter",
    "UnsupportedSymbol",
    "AccountShapeError",
    "build_adapter_from_env",
    "load_config_from_env",
]

# Account equity, most-preferred field first. portfolioValue is the flex
# account's total value including unrealised P&L, which is the number the
# drawdown and daily-loss switches should be measured against.
_EQUITY_FIELDS = ("portfolioValue", "marginEquity", "balanceValue", "collateralValue")

# Size precision on an instrument, in decimal places.
_SIZE_PRECISION_FIELDS = ("contractValueTradePrecision", "volumePrecision", "sizePrecision")


class UnsupportedSymbol(ValueError):
    """Raised when a symbol is not sized in base-asset units."""


class AccountShapeError(RuntimeError):
    """Raised when an API payload does not contain the expected fields."""


# ---------------------------------------------------------------------------
# Pure helpers — unit-tested without touching the network
# ---------------------------------------------------------------------------

def is_flex_perpetual(symbol: str) -> bool:
    """True for multi-collateral perps (PF_*), which are sized in base units."""
    return symbol.upper().startswith("PF_")


def extract_equity(wallets: dict) -> float:
    """Pull total account equity out of a get_wallets() payload."""
    accounts = (wallets or {}).get("accounts") or {}
    flex = accounts.get("flex")
    if not flex:
        raise AccountShapeError(
            "no 'flex' (multi-collateral) account in get_wallets(); found "
            f"{sorted(accounts)}. This bot trades PF_* perps, which settle in "
            "the flex account — transfer collateral to it in the Kraken UI first."
        )
    for name in _EQUITY_FIELDS:
        value = flex.get(name)
        if isinstance(value, (int, float)) and math.isfinite(value):
            return float(value)
    raise AccountShapeError(
        f"none of {_EQUITY_FIELDS} present in the flex account; got {sorted(flex)}"
    )


def extract_positions(open_positions: dict) -> Dict[str, float]:
    """
    Turn a get_open_positions() payload into {SYMBOL: notional_usd}.

    Kraken returns symbols lowercase ('pf_ethusd'); they are upper-cased here so
    they match the symbols in your RiskConfig.correlated_groups.
    """
    positions: Dict[str, float] = {}
    for entry in (open_positions or {}).get("openPositions") or []:
        symbol = str(entry.get("symbol", "")).upper()
        if not symbol:
            continue
        size = abs(float(entry.get("size") or 0.0))
        price = float(entry.get("price") or 0.0)
        # PF_ sizes are base-asset units; inverse contracts are already USD.
        notional = size * price if is_flex_perpetual(symbol) else size
        positions[symbol] = positions.get(symbol, 0.0) + notional
    return positions


def size_step_from_instrument(instrument: dict) -> Optional[float]:
    """Smallest tradable size increment, or None if the payload doesn't say."""
    for name in _SIZE_PRECISION_FIELDS:
        decimals = (instrument or {}).get(name)
        if isinstance(decimals, int) and 0 <= decimals <= 12:
            return 10.0 ** -decimals
    return None


def round_price_toward(price: float, tick: Optional[float], reference: float) -> float:
    """
    Snap a price to the instrument's tick, moving it TOWARD `reference`.

    Stops are rounded toward the entry so the realised loss stays inside the
    modelled risk budget; rounding away would quietly widen every stop by up to
    one tick.
    """
    if not tick or tick <= 0:
        return price
    if price > reference:
        return math.floor(price / tick) * tick
    return math.ceil(price / tick) * tick


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class KrakenFuturesAdapter:
    """Thin, explicit wrapper over the three Kraken Futures calls this bot needs."""

    def __init__(
        self,
        key: str,
        secret: str,
        sandbox: bool = True,
        allow_market_entries: bool = False,
    ) -> None:
        if User is None:  # pragma: no cover - dependency guard
            raise ImportError(
                "python-kraken-sdk is required to talk to Kraken: "
                "pip install 'python-kraken-sdk>=3.3,<4'"
            ) from _SDK_IMPORT_ERROR
        if not key or not secret:
            raise ValueError("API key and secret are required")
        self.sandbox = sandbox
        self.allow_market_entries = allow_market_entries
        self.user = User(key=key, secret=secret, sandbox=sandbox)
        self.trade = Trade(key=key, secret=secret, sandbox=sandbox)
        self.market = Market(sandbox=sandbox)          # public endpoints
        self._instruments: Dict[str, dict] = {}

    # -- account reads ------------------------------------------------------

    def account_equity(self) -> float:
        """Total equity in USD, for RiskManager.update_equity / approve_trade."""
        return extract_equity(self.user.get_wallets())

    def open_positions(self) -> Dict[str, float]:
        """{SYMBOL: notional_usd}, for RiskManager.approve_trade."""
        return extract_positions(self.user.get_open_positions())

    def instrument(self, symbol: str) -> dict:
        """Contract spec for a symbol, fetched once and cached."""
        symbol = symbol.upper()
        if not self._instruments:
            payload = self.market.get_instruments()
            for entry in payload.get("instruments") or []:
                self._instruments[str(entry.get("symbol", "")).upper()] = entry
        if symbol not in self._instruments:
            raise UnsupportedSymbol(f"{symbol} is not a tradable Kraken Futures instrument")
        return self._instruments[symbol]

    def size_step(self, symbol: str) -> Optional[float]:
        """Lot size for the symbol — feed this into RiskConfig.quantity_step."""
        return size_step_from_instrument(self.instrument(symbol))

    def tick_size(self, symbol: str) -> Optional[float]:
        tick = self.instrument(symbol).get("tickSize")
        return float(tick) if isinstance(tick, (int, float)) and tick > 0 else None

    # -- order placement ----------------------------------------------------

    def place_entry_with_stop(
        self,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        stop_price: float,
        order_type: str = "lmt",
        client_id: Optional[str] = None,
    ) -> dict:
        """
        Place the entry and its protective stop.

        The stop goes on as reduceOnly, so it can only ever close a position —
        never open one if the entry does not fill, and never oversize itself if
        the entry only fills partially. It triggers on the mark price rather
        than last, which is harder to wick through.

        Returns {"entry": <sendStatus>, "stop": <sendStatus>}.
        """
        symbol = symbol.upper()
        side = side.lower()
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        if not is_flex_perpetual(symbol):
            raise UnsupportedSymbol(
                f"{symbol} is not a PF_* perpetual. Inverse contracts (PI_/FI_) are "
                "sized in USD, so the base-unit quantity from risk_manager would be "
                "wrong by a factor of the price. Trade the PF_ equivalent instead."
            )
        if quantity <= 0:
            raise ValueError(f"quantity must be positive, got {quantity!r}")
        if order_type == "mkt" and not self.allow_market_entries:
            raise ValueError(
                "market entries are disabled; the fill price can differ from the "
                "entry_price the position was sized against. Pass "
                "allow_market_entries=True if you accept that slippage."
            )

        # Rounding: size down so it cannot breach a cap, stop toward entry so it
        # cannot exceed the risk budget.
        step = self.size_step(symbol)
        if step:
            quantity = math.floor(quantity / step) * step
            if quantity <= 0:
                raise ValueError(f"quantity rounds to zero at the {step} lot step")
        tick = self.tick_size(symbol)
        if tick:
            entry_price = round(entry_price / tick) * tick
            stop_price = round_price_toward(stop_price, tick, entry_price)

        entry_kwargs = dict(orderType=order_type, symbol=symbol, side=side, size=quantity)
        if order_type != "mkt":
            entry_kwargs["limitPrice"] = entry_price
        if client_id:
            entry_kwargs["cliOrdId"] = client_id
        entry = self.trade.create_order(**entry_kwargs)

        stop = self.trade.create_order(
            orderType="stp",
            symbol=symbol,
            side="sell" if side == "buy" else "buy",
            size=quantity,
            stopPrice=stop_price,
            reduceOnly=True,
            triggerSignal="mark",
            **({"cliOrdId": f"{client_id}-stop"} if client_id else {}),
        )
        return {"entry": entry, "stop": stop}

    # -- safety -------------------------------------------------------------

    def arm_dead_mans_switch(self, seconds: int = 60) -> dict:
        """
        Tell Kraken to cancel all open orders if this bot stops checking in
        within `seconds`. Re-arm it on every loop tick; call with 0 to disarm.

        This covers the failure the risk manager cannot: the process dying with
        resting orders on the book.
        """
        return self.trade.dead_mans_switch(timeout=seconds)

    def cancel_all(self, symbol: Optional[str] = None) -> dict:
        return self.trade.cancel_all_orders(symbol=symbol)

    def set_leverage(self, symbol: str, max_leverage: float) -> dict:
        """Cap leverage exchange-side to match RiskConfig.leverage."""
        return self.market.set_leverage_preference(symbol=symbol.upper(), maxLeverage=max_leverage)


# ---------------------------------------------------------------------------
# Environment wiring
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def load_config_from_env() -> RiskConfig:
    """
    Build a RiskConfig from environment variables, so limits can be changed
    without editing code. Every variable is optional; defaults are conservative.

    KRAKEN_RISK_PER_TRADE_PCT, KRAKEN_MAX_POSITION_PCT, KRAKEN_MAX_CORRELATED_PCT,
    KRAKEN_MAX_TOTAL_EXPOSURE_PCT, KRAKEN_DAILY_LOSS_LIMIT_PCT,
    KRAKEN_MAX_DRAWDOWN_PCT, KRAKEN_LEVERAGE, KRAKEN_CORRELATED_MAJORS
    (comma-separated symbols).
    """
    majors = os.environ.get(
        "KRAKEN_CORRELATED_MAJORS", "PF_ETHUSD,PF_SOLUSD,PF_XRPUSD,PF_LINKUSD"
    )
    groups = {"majors": [s.strip().upper() for s in majors.split(",") if s.strip()]}
    return RiskConfig(
        risk_per_trade_pct=_env_float("KRAKEN_RISK_PER_TRADE_PCT", 0.01),
        max_position_pct=_env_float("KRAKEN_MAX_POSITION_PCT", 0.08),
        max_correlated_pct=_env_float("KRAKEN_MAX_CORRELATED_PCT", 0.22),
        max_total_exposure_pct=_env_float("KRAKEN_MAX_TOTAL_EXPOSURE_PCT", 0.60),
        daily_loss_limit_pct=_env_float("KRAKEN_DAILY_LOSS_LIMIT_PCT", 0.04),
        max_drawdown_pct=_env_float("KRAKEN_MAX_DRAWDOWN_PCT", 0.18),
        leverage=_env_float("KRAKEN_LEVERAGE", 2.0),
        correlated_groups=groups,
    )


def build_adapter_from_env() -> KrakenFuturesAdapter:
    """
    Read credentials from the environment. Keys never belong in source control.

        KRAKEN_FUTURES_KEY, KRAKEN_FUTURES_SECRET
        KRAKEN_SANDBOX  ("true" by default — you must opt in to real money)
    """
    key = os.environ.get("KRAKEN_FUTURES_KEY", "")
    secret = os.environ.get("KRAKEN_FUTURES_SECRET", "")
    if not key or not secret:
        raise ValueError(
            "set KRAKEN_FUTURES_KEY and KRAKEN_FUTURES_SECRET. Demo keys come from "
            "demo-futures.kraken.com and are separate from live keys."
        )
    sandbox = os.environ.get("KRAKEN_SANDBOX", "true").strip().lower() not in ("false", "0", "no")
    return KrakenFuturesAdapter(key=key, secret=secret, sandbox=sandbox)


def build_risk_manager(db_path: str = "risk_state.db") -> RiskManager:
    """RiskManager wired to env-configured limits and the persistent state file."""
    return RiskManager(load_config_from_env(), RiskState(db_path=db_path))
