"""
risk_manager.py

Position sizing, exposure caps and kill-switches for a Kraken Futures
perpetuals bot.

The module is deliberately free of exchange dependencies: it takes numbers in
(account equity, entry, stop, open positions) and returns a decision out, so it
can be unit-tested without touching the API and reused by any strategy.

Three jobs:

1. Size every trade off a fixed fraction of equity risked to the stop, not off
   a fixed notional. Stop distance drives quantity, so a wide-stop setup gets a
   small position and a tight-stop setup gets a large one, both losing the same
   dollars if wrong.
2. Cap exposure per symbol, per correlated group, and in aggregate. Perps make
   it trivial to hold four positions that are really one bet on beta; the group
   caps stop that from happening by accident.
3. Halt trading on a daily-loss or peak-to-trough drawdown breach and stay
   halted across process restarts (state lives in SQLite) until a human calls
   manual_resume(). A kill-switch that a crash-loop can clear is not a
   kill-switch.

Sizing is reduce-to-fit: if the risk-based quantity would breach a cap, the
trade is trimmed to the headroom that exists rather than rejected, and rejected
only when there is no usable headroom left.

All dates are UTC, matching the exchange's own daily boundary.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional


__all__ = ["HaltReason", "RiskConfig", "RiskState", "RiskManager"]


class HaltReason(Enum):
    """Why the bot stopped taking new trades."""

    DAILY_LOSS_LIMIT = "daily_loss_limit"
    MAX_DRAWDOWN = "max_drawdown"
    EQUITY_WIPEOUT = "equity_wipeout"
    MANUAL = "manual"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _floor_to(value: float, step: Optional[float], decimals: int) -> float:
    """Round a quantity down so rounding never grows a position past a cap."""
    if step:
        return math.floor(value / step) * step
    factor = 10 ** decimals
    return math.floor(value * factor) / factor


def _money(value: float) -> float:
    return round(value + 0.0, 2)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskConfig:
    """Risk limits. Every *_pct field is a fraction of account equity (0.02 = 2%)."""

    # Sizing
    risk_per_trade_pct: float = 0.01      # equity risked between entry and stop
    leverage: float = 2.0                 # used to report margin required

    # Exposure caps (notional, as a fraction of equity)
    max_position_pct: float = 0.08        # per symbol
    max_correlated_pct: float = 0.22      # per correlated group
    max_total_exposure_pct: float = 0.60  # all open positions combined
    max_open_positions: int = 5

    # Kill-switches
    daily_loss_limit_pct: float = 0.04    # equity drop from the day's open
    max_drawdown_pct: float = 0.18        # equity drop from the all-time peak

    # Symbols that tend to move together, e.g.
    # {"majors": ["PF_ETHUSD", "PF_SOLUSD"]}. A symbol may appear in several
    # groups; the tightest headroom wins.
    correlated_groups: Dict[str, List[str]] = field(default_factory=dict)

    # Order hygiene
    min_stop_distance_pct: float = 0.001  # reject stops inside the noise band
    min_notional: float = 10.0            # skip dust after the caps trim size
    quantity_step: Optional[float] = None  # exchange lot size, if any
    quantity_decimals: int = 8

    def __post_init__(self) -> None:
        fractions = {
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_position_pct": self.max_position_pct,
            "max_correlated_pct": self.max_correlated_pct,
            "max_total_exposure_pct": self.max_total_exposure_pct,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "min_stop_distance_pct": self.min_stop_distance_pct,
        }
        for name, value in fractions.items():
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be a fraction in (0, 1], got {value!r}")
        if self.leverage <= 0:
            raise ValueError(f"leverage must be positive, got {self.leverage!r}")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be at least 1")
        if self.daily_loss_limit_pct >= self.max_drawdown_pct:
            # Not fatal, but it means the drawdown switch can never fire first.
            pass
        if self.quantity_step is not None and self.quantity_step <= 0:
            raise ValueError("quantity_step must be positive when set")

    def groups_for(self, symbol: str) -> List[str]:
        """Names of the correlated groups this symbol belongs to."""
        return [name for name, members in self.correlated_groups.items() if symbol in members]


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

class RiskState:
    """
    SQLite-backed risk state: equity peak, the day's opening equity, and the
    halt flag. Survives restarts, so a bot that dies mid-drawdown wakes up
    still halted.

    Pass db_path=":memory:" for tests.
    """

    _COLUMNS = (
        "peak_equity",
        "last_equity",
        "day_start_equity",
        "day_start_date",
        "halted",
        "halt_reason",
        "halted_at",
        "note",
        "updated_at",
    )

    def __init__(self, db_path: str = "risk_state.db") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

        self.peak_equity: Optional[float] = None
        self.last_equity: Optional[float] = None
        self.day_start_equity: Optional[float] = None
        self.day_start_date: Optional[str] = None
        self.halted: bool = False
        self.halt_reason: Optional[str] = None
        self.halted_at: Optional[str] = None
        self.note: Optional[str] = None
        self.updated_at: Optional[str] = None
        self.load()

    # -- schema / io --------------------------------------------------------

    def _create_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS risk_state (
                    id               INTEGER PRIMARY KEY CHECK (id = 1),
                    peak_equity      REAL,
                    last_equity      REAL,
                    day_start_equity REAL,
                    day_start_date   TEXT,
                    halted           INTEGER NOT NULL DEFAULT 0,
                    halt_reason      TEXT,
                    halted_at        TEXT,
                    note             TEXT,
                    updated_at       TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS risk_events (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts      TEXT NOT NULL,
                    event   TEXT NOT NULL,
                    detail  TEXT
                )
                """
            )
            self._conn.execute("INSERT OR IGNORE INTO risk_state (id) VALUES (1)")

    def load(self) -> "RiskState":
        """Re-read state from disk into this object's attributes."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM risk_state WHERE id = 1"
            ).fetchone()
            if row is None:
                return self
            for column in self._COLUMNS:
                value = row[column]
                if column == "halted":
                    value = bool(value)
                setattr(self, column, value)
        return self

    def save(self, **updates) -> "RiskState":
        """
        Apply keyword updates to the in-memory attributes and flush everything
        to disk. Called with no arguments it just persists current values.
        """
        unknown = set(updates) - set(self._COLUMNS)
        if unknown:
            raise ValueError(f"unknown risk state field(s): {sorted(unknown)}")
        with self._lock:
            for key, value in updates.items():
                setattr(self, key, value)
            self.updated_at = _utcnow().isoformat()
            self._conn.execute(
                """
                UPDATE risk_state SET
                    peak_equity = ?, last_equity = ?, day_start_equity = ?,
                    day_start_date = ?, halted = ?, halt_reason = ?,
                    halted_at = ?, note = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    self.peak_equity,
                    self.last_equity,
                    self.day_start_equity,
                    self.day_start_date,
                    int(bool(self.halted)),
                    self.halt_reason,
                    self.halted_at,
                    self.note,
                    self.updated_at,
                ),
            )
            self._conn.commit()
        return self

    def record_event(self, event: str, detail: str = "") -> None:
        """Append to the audit log — the record you want during a post-mortem."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO risk_events (ts, event, detail) VALUES (?, ?, ?)",
                (_utcnow().isoformat(), event, detail),
            )

    def events(self, limit: int = 50) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT ts, event, detail FROM risk_events ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            )

    def snapshot(self) -> Dict[str, object]:
        return {column: getattr(self, column) for column in self._COLUMNS}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RiskState({self.snapshot()})"


# ---------------------------------------------------------------------------
# Risk manager
# ---------------------------------------------------------------------------

class RiskManager:
    """Applies a RiskConfig to live account numbers and returns trade decisions."""

    def __init__(
        self,
        config: RiskConfig,
        state: Optional[RiskState] = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.config = config
        self.state = state if state is not None else RiskState()
        self._clock = clock

    # -- equity tracking / kill-switches ------------------------------------

    def update_equity(self, account_equity: float) -> Optional[HaltReason]:
        """
        Record the latest equity and evaluate the kill-switches.

        Returns the HaltReason if the bot is halted, or None if it is clear to
        trade. Call it on every signal *and* on a timer, so a breach between
        signals is still caught.
        """
        if account_equity is None or not math.isfinite(account_equity):
            raise ValueError(f"account_equity must be a finite number, got {account_equity!r}")

        cfg, state = self.config, self.state
        today = self._clock().strftime("%Y-%m-%d")

        # New UTC day: today's loss limit is measured from this morning's equity,
        # so the baseline resets before any check runs.
        if state.day_start_date != today:
            state.save(day_start_date=today, day_start_equity=account_equity)

        if state.peak_equity is None or account_equity > state.peak_equity:
            state.peak_equity = account_equity
        if state.day_start_equity is None:
            state.day_start_equity = account_equity
        state.save(last_equity=account_equity)

        if state.halted:
            return self.halt_reason()

        if account_equity <= 0:
            return self._halt(HaltReason.EQUITY_WIPEOUT, "account equity is zero or negative")

        daily_loss_pct = 0.0
        if state.day_start_equity and state.day_start_equity > 0:
            daily_loss_pct = (state.day_start_equity - account_equity) / state.day_start_equity

        drawdown_pct = 0.0
        if state.peak_equity and state.peak_equity > 0:
            drawdown_pct = (state.peak_equity - account_equity) / state.peak_equity

        # Daily limit first: it is the tighter, faster-moving switch.
        if daily_loss_pct >= cfg.daily_loss_limit_pct:
            return self._halt(
                HaltReason.DAILY_LOSS_LIMIT,
                f"down {daily_loss_pct:.2%} today (limit {cfg.daily_loss_limit_pct:.2%}); "
                f"equity {_money(account_equity)} vs day open {_money(state.day_start_equity)}",
            )

        if drawdown_pct >= cfg.max_drawdown_pct:
            return self._halt(
                HaltReason.MAX_DRAWDOWN,
                f"down {drawdown_pct:.2%} from peak (limit {cfg.max_drawdown_pct:.2%}); "
                f"equity {_money(account_equity)} vs peak {_money(state.peak_equity)}",
            )

        return None

    def _halt(self, reason: HaltReason, detail: str) -> HaltReason:
        self.state.save(
            halted=True,
            halt_reason=reason.value,
            halted_at=self._clock().isoformat(),
            note=detail,
        )
        self.state.record_event(f"halt:{reason.value}", detail)
        return reason

    def manual_halt(self, note: str = "") -> HaltReason:
        """Operator kill-switch — same effect as an automatic breach."""
        return self._halt(HaltReason.MANUAL, note or "halted manually")

    def manual_resume(self, note: str = "", reanchor: bool = True) -> None:
        """
        Clear the halt. Deliberately manual: a human should have looked at the
        cause before the bot is allowed to size another trade.

        Resuming also re-anchors the baselines that are still in breach — the
        day's opening equity, and the drawdown peak if equity is still past the
        limit — because the loss that tripped the switch has not gone anywhere,
        and the very next equity update would otherwise halt the bot again on
        the breach you just reviewed. The re-anchor is logged, so repeatedly
        resuming through a losing run is visible in the audit trail rather than
        silent. Pass reanchor=False to keep the original baselines and stay
        halted until equity genuinely recovers.
        """
        state, cfg = self.state, self.config
        previous = state.halt_reason
        equity = state.last_equity

        updates = {
            "halted": False,
            "halt_reason": None,
            "halted_at": None,
            "note": note or None,
        }
        reanchored = []
        if reanchor and equity is not None and equity > 0:
            updates["day_start_date"] = self._clock().strftime("%Y-%m-%d")
            updates["day_start_equity"] = equity
            reanchored.append(f"day open -> {_money(equity)}")
            if state.peak_equity and (
                (state.peak_equity - equity) / state.peak_equity >= cfg.max_drawdown_pct
            ):
                updates["peak_equity"] = equity
                reanchored.append(f"peak {_money(state.peak_equity)} -> {_money(equity)}")

        state.save(**updates)
        detail = f"cleared {previous or 'halt'}: {note}".strip()
        if reanchored:
            detail += f" [re-anchored {', '.join(reanchored)}]"
        state.record_event("resume", detail)

    def is_halted(self) -> bool:
        return bool(self.state.halted)

    def halt_reason(self) -> Optional[HaltReason]:
        if not self.state.halted or not self.state.halt_reason:
            return None
        try:
            return HaltReason(self.state.halt_reason)
        except ValueError:  # pragma: no cover - state written by an older version
            return HaltReason.MANUAL

    # -- sizing / approval --------------------------------------------------

    def approve_trade(
        self,
        account_equity: float,
        entry_price: float,
        stop_price: float,
        symbol: str,
        open_positions: Optional[Dict[str, float]] = None,
        available_cash: Optional[float] = None,
    ) -> Dict[str, object]:
        """
        Decide whether to take a trade and at what size.

        open_positions maps symbol -> current notional in USD (absolute value;
        a short's notional counts the same as a long's for exposure purposes).

        available_cash caps the trade at spendable balance. On spot this is not
        optional in practice: equity includes coins you already hold, so the
        percentage caps alone will happily approve a buy larger than the cash
        you have left. Leave it None on margin or futures, where buying power
        is not the same as cash.

        Returns:
            {"approved": bool, "reason": str, "sizing": {...} | None, "risk": {...}}
        """
        cfg = self.config
        open_positions = {k: abs(float(v)) for k, v in (open_positions or {}).items()}

        halt = self.update_equity(account_equity)
        if halt is not None:
            return self._reject(
                f"trading halted ({halt.value}): {self.state.note or 'see risk log'}"
            )

        # --- input sanity ---------------------------------------------------
        if account_equity <= 0:
            return self._reject("account equity is zero or negative")
        for name, price in (("entry_price", entry_price), ("stop_price", stop_price)):
            if price is None or not math.isfinite(price) or price <= 0:
                return self._reject(f"{name} must be a positive number, got {price!r}")

        stop_distance = abs(entry_price - stop_price)
        stop_distance_pct = stop_distance / entry_price
        if stop_distance_pct < cfg.min_stop_distance_pct:
            return self._reject(
                f"stop is {stop_distance_pct:.3%} from entry, inside the "
                f"{cfg.min_stop_distance_pct:.3%} minimum"
            )

        # --- risk-based size, before caps -----------------------------------
        risk_amount = account_equity * cfg.risk_per_trade_pct
        raw_notional = (risk_amount / stop_distance) * entry_price

        # --- exposure caps ---------------------------------------------------
        existing_symbol = open_positions.get(symbol, 0.0)
        total_open = sum(open_positions.values())

        if symbol not in open_positions and len(open_positions) >= cfg.max_open_positions:
            return self._reject(
                f"{len(open_positions)} positions already open "
                f"(max {cfg.max_open_positions})"
            )

        limits = [
            (
                "risk_per_trade",
                raw_notional,
                f"risk budget {_money(risk_amount)} over a {stop_distance_pct:.2%} stop",
            ),
            (
                "max_position",
                account_equity * cfg.max_position_pct - existing_symbol,
                f"{symbol} exposure cap {_money(account_equity * cfg.max_position_pct)} "
                f"with {_money(existing_symbol)} already open",
            ),
            (
                "max_total_exposure",
                account_equity * cfg.max_total_exposure_pct - total_open,
                f"total exposure cap {_money(account_equity * cfg.max_total_exposure_pct)} "
                f"with {_money(total_open)} already open",
            ),
        ]

        if available_cash is not None:
            limits.append(
                (
                    "available_cash",
                    float(available_cash),
                    f"spendable balance {_money(available_cash)}",
                )
            )

        for group in cfg.groups_for(symbol):
            members = cfg.correlated_groups[group]
            group_open = sum(open_positions.get(member, 0.0) for member in members)
            group_cap = account_equity * cfg.max_correlated_pct
            limits.append(
                (
                    f"correlated:{group}",
                    group_cap - group_open,
                    f"correlated group '{group}' cap {_money(group_cap)} "
                    f"with {_money(group_open)} already open",
                )
            )

        binding_name, allowed_notional, binding_detail = min(limits, key=lambda item: item[1])

        if allowed_notional <= 0:
            return self._reject(f"no headroom: {binding_detail}")

        quantity = _floor_to(
            allowed_notional / entry_price, cfg.quantity_step, cfg.quantity_decimals
        )
        notional = quantity * entry_price
        if quantity <= 0 or notional < cfg.min_notional:
            return self._reject(
                f"size trimmed to {_money(notional)} notional, below the "
                f"{_money(cfg.min_notional)} minimum: {binding_detail}"
            )

        risk_at_size = quantity * stop_distance
        return {
            "approved": True,
            "reason": f"within limits (sized by {binding_name}: {binding_detail})",
            "sizing": {
                "symbol": symbol,
                "quantity": quantity,
                "notional": _money(notional),
                "margin_required": _money(notional / cfg.leverage),
                "risk_amount": _money(risk_at_size),
                "risk_pct_of_equity": round(risk_at_size / account_equity, 6),
                "stop_distance": stop_distance,
                "stop_distance_pct": round(stop_distance_pct, 6),
                "capped_by": binding_name,
                "trimmed": binding_name != "risk_per_trade",
            },
            "risk": self.status(),
        }

    def _reject(self, reason: str) -> Dict[str, object]:
        return {"approved": False, "reason": reason, "sizing": None, "risk": self.status()}

    # -- reporting ----------------------------------------------------------

    def status(self) -> Dict[str, object]:
        """Snapshot for logging or a dashboard tile."""
        state = self.state
        equity = state.last_equity
        drawdown_pct = 0.0
        if state.peak_equity and equity is not None:
            drawdown_pct = max(0.0, (state.peak_equity - equity) / state.peak_equity)
        daily_pnl_pct = 0.0
        if state.day_start_equity and equity is not None:
            daily_pnl_pct = (equity - state.day_start_equity) / state.day_start_equity
        return {
            "equity": equity,
            "peak_equity": state.peak_equity,
            "day_start_equity": state.day_start_equity,
            "day_start_date": state.day_start_date,
            "drawdown_pct": round(drawdown_pct, 6),
            "daily_pnl_pct": round(daily_pnl_pct, 6),
            "halted": bool(state.halted),
            "halt_reason": state.halt_reason,
            "note": state.note,
        }
