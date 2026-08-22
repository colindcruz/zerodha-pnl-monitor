"""
Two append-only JSONL logs, per the plan's Logging design:

  nifty_decision_tick_log.jsonl  — one line per recompute tick, the FULL raw
    state of every engine (not a summary — this is what later threshold
    tuning/backtesting will replay against, so nothing here is allowed to be
    lossy).

  nifty_decision_trade_log.jsonl — one line per detected trade lifecycle
    event (ENTRY_DETECTED / EXIT_DETECTED — this service places no orders
    itself, so "detected" means "noticed via the positions poll", not
    "placed"), carrying MFE/MAE tracked since entry and the tick-log state
    at the moment of entry.

Deliberately its own module, not long_option_logging.py's — that module's
format_log_entry is a human-readable text formatter for Telegram-style
alerts, and doesn't fit a machine-replayable JSONL record.

Every engine result here is a plain dataclass; dataclasses.asdict() already
recurses through nested dataclasses/lists/dicts, and every enum in this
service subclasses str (e.g. `class TrendDirection(str, Enum)`), so a member
IS already a str as far as json.dumps' isinstance checks are concerned —
no enum-specific handling is needed. Only `datetime` needs a custom
default().
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=_json_default) + "\n")


def _maybe_asdict(obj):
    return asdict(obj) if obj is not None and is_dataclass(obj) else obj


def log_tick(path: Path, ts: datetime, tick_seq: int, spot: Optional[float], trend=None, entry=None,
             location=None, decision=None, positions=None, key_levels=None, position_health=None) -> dict:
    record = {
        "ts": ts.isoformat(), "tick_seq": tick_seq, "spot": spot,
        "trend": _maybe_asdict(trend), "entry": _maybe_asdict(entry),
        "location": _maybe_asdict(location), "decision": _maybe_asdict(decision),
        "positions": positions or [], "key_levels": _maybe_asdict(key_levels),
        "position_health": _maybe_asdict(position_health),
    }
    append_jsonl(path, record)
    return record


# ============================================================
# Trade lifecycle
# ============================================================

@dataclass
class TrackedTrade:
    """In-memory only — one per currently-open detected trade. Not
    persisted to disk on its own; the JSONL event log IS the durable
    record, this struct just holds the running MFE/MAE between events."""

    trade_id: str
    symbol: str
    direction: str          # "LONG" | "SHORT"
    entry_price: float
    entry_ts: datetime
    mfe: float = 0.0         # max favorable excursion so far, in points (>= 0)
    mae: float = 0.0         # max adverse excursion so far, in points (>= 0)
    dashboard_state_at_entry: Optional[dict] = None


def update_excursion(trade: TrackedTrade, price: float) -> None:
    sign = 1 if trade.direction == "LONG" else -1
    move = sign * (price - trade.entry_price)
    if move > trade.mfe:
        trade.mfe = move
    if -move > trade.mae:
        trade.mae = -move


def log_trade_event(path: Path, event: str, trade: TrackedTrade, price: float, ts: datetime,
                     extra: dict = None) -> dict:
    """event: "ENTRY_DETECTED" | "EXIT_DETECTED". `extra` carries anything
    event-specific (e.g. an exit's realized outcome) without forcing every
    event type through the same fixed schema."""
    record = {
        "event": event, "trade_id": trade.trade_id, "symbol": trade.symbol,
        "direction": trade.direction, "entry_price": trade.entry_price,
        "entry_ts": trade.entry_ts.isoformat(), "price": price, "ts": ts.isoformat(),
        "mfe": trade.mfe, "mae": trade.mae,
        "dashboard_state_at_entry": trade.dashboard_state_at_entry,
    }
    if extra:
        record.update(extra)
    append_jsonl(path, record)
    return record
