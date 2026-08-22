"""
Read-only enrichment: pulls the long-option engine's own tracked stop/T1/T2
state for symbols this dashboard has already labeled owner="long_option"
(see positions_kite_adapter.py), purely for display in the Open Position
panel.

Reads long_option_state.json's plain JSON structure DIRECTLY — not via
long_option_persistence.py's Position.from_dict/PositionBook.from_dict, and
not by importing long_option_trade_engine.py's enum classes. This is
deliberately a separate, independent service (own directory/venv/
deployment; see server.py's module docstring), so it reads the same
on-disk convention long_option_persistence.py writes without importing that
module: trade_state/exit_reason are already persisted as plain strings
(Position.to_dict()'s own convention), so a read-only display needs no enum
reconstruction at all — just known dict keys.

T1/T2 point labels below are a COSMETIC approximation of the long-option
engine's real ladder (see long_option_trade_engine.py's EngineConfig
defaults: target1_points=15, profit_lock2_trigger=25) — T1 = the 50%-exit
at +15, "T2" = reaching PROFIT_LOCK_25 (+25) or beyond (RUNNER). If that
engine's config is ever tuned away from its defaults, these labels will
silently drift out of sync; that's a known, accepted limitation (flagged,
not solved) since the two services intentionally don't share config.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_T1_LABEL = "+15 PTS"
_T2_LABEL = "+25 PTS"
_T2_REACHED_STATES = {"PROFIT_LOCK_25", "RUNNER"}
_CLOSED_STATE = "CLOSED"


def read_open_anchor(long_option_state_path, symbol: str) -> Optional[dict]:
    """The current open cohort's anchor tranche for `symbol`, or None if the
    engine has no book for this symbol, no cohorts, or its most recent
    cohort's anchor is already closed. Followers (scale-in tranches) are
    intentionally not surfaced here — the anchor is what drives the stop
    ladder that matters for the panel; a full multi-tranche breakdown is
    out of scope for a glanceable display."""
    try:
        state = json.loads(Path(long_option_state_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None

    book = state.get("books", {}).get(symbol)
    if not book or not book.get("cohorts"):
        return None

    anchor = book["cohorts"][-1].get("anchor")
    if not anchor or anchor.get("trade_state") == _CLOSED_STATE:
        return None

    trade_state = anchor.get("trade_state")
    return {
        "instrument": anchor.get("instrument"),
        "direction": anchor.get("direction", "LONG"),
        "entry_price": anchor.get("entry_price"),
        "current_stop": anchor.get("current_stop"),
        "trade_state": trade_state,
        "initial_quantity": anchor.get("initial_quantity"),
        "remaining_quantity": anchor.get("remaining_quantity"),
        "realized_pnl": anchor.get("realized_pnl"),
        "t1_label": _T1_LABEL,
        "t1_hit": bool(anchor.get("t1_executed")),
        "t2_label": _T2_LABEL,
        "t2_hit": trade_state in _T2_REACHED_STATES,
    }
