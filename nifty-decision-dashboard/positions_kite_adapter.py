"""
Kite-specific position discovery + owner-system labeling for the Position
Management panel. Per the user's explicit call: this panel tracks EVERY
NIFTY-related position in the account — including the ones already owned and
managed by the existing automated strangle/hedge system
(with-websockets/pnl_monitor.py) and the long-option trade-management engine
(long_option_live.py et al) — purely for visibility. The Position Management
Engine's own health verdict for a position may disagree with what the system
that actually owns it is doing; that's expected and accepted, not a bug to
paper over (see the plan's Context section).

Mirrors long_option_kite_adapter.py's pure-filter-function pattern and
read_excluded_symbols' state-file-reading approach, but as this service's
OWN small copy rather than an import — this is its own directory/venv/
deployment, and importing across service directories would break that
isolation (the same reasoning live-dashboard/server.py's _read_access_token
docstring gives for not sharing that function either).
"""

from __future__ import annotations

import json
from pathlib import Path


def is_nifty_position(position: dict) -> bool:
    """True for any NFO NIFTY (not BANKNIFTY/FINNIFTY — those don't share the
    "NIFTY" tradingsymbol prefix) option or future with a non-zero quantity."""
    symbol = position.get("tradingsymbol", "")
    if position.get("exchange") != "NFO" or not symbol.startswith("NIFTY"):
        return False
    if int(position.get("quantity", 0) or 0) == 0:
        return False
    return symbol.endswith("CE") or symbol.endswith("PE") or symbol.endswith("FUT")


def read_state_symbols(state_path) -> set:
    """Symbols with a non-terminal-non-pending leg status in a strangle- or
    hedge-shaped state file ({"legs": {"CE": {...}, "PE": {...}}}) — mirrors
    long_option_kite_adapter.py's read_excluded_symbols exactly. Missing/
    corrupt files are treated as "nothing there," never a startup failure."""
    symbols = set()
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, json.JSONDecodeError):
        return symbols
    for leg in state.get("legs", {}).values():
        symbol = leg.get("tradingsymbol")
        status = leg.get("status")
        if symbol and status not in (None, "pending", "failed"):
            symbols.add(symbol)
    return symbols


def read_long_option_symbols(state_path) -> set:
    """Every tradingsymbol the long-option engine's books have ever tracked
    ({"books": {"NIFTY...CE": {...}, ...}}) — a book stays in state even once
    fully flat (see long_option_persistence.py), so this can include symbols
    with no CURRENT broker position; callers only ever consult this against
    positions that ARE currently held, so that's harmless."""
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return set(state.get("books", {}).keys())


def classify_owner(symbol: str, strangle_symbols: set, hedge_symbols: set, long_option_symbols: set) -> str:
    """One label per symbol: "strangle" | "hedge" | "long_option" | "manual".
    Checked in this order because a symbol should realistically only ever
    belong to one system at a time — if it somehow matches more than one
    (stale state file, a manual trade that happens to reuse a symbol another
    system just exited), strangle/hedge take priority since those are the
    automated systems already placing real orders on this account."""
    if symbol in strangle_symbols:
        return "strangle"
    if symbol in hedge_symbols:
        return "hedge"
    if symbol in long_option_symbols:
        return "long_option"
    return "manual"


def label_positions(broker_positions: list, strangle_state_path, hedge_state_path, long_option_state_path) -> list:
    """broker_positions: kite.positions()['net']-shaped list. Returns only
    the NIFTY-related ones, each augmented with an "owner" label."""
    strangle_symbols = read_state_symbols(strangle_state_path)
    hedge_symbols = read_state_symbols(hedge_state_path)
    long_option_symbols = read_long_option_symbols(long_option_state_path)

    out = []
    for p in broker_positions:
        if not is_nifty_position(p):
            continue
        symbol = p["tradingsymbol"]
        out.append({**p, "owner": classify_owner(symbol, strangle_symbols, hedge_symbols, long_option_symbols)})
    return out
