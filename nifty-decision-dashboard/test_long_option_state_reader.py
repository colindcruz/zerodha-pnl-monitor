"""
Unit tests for long_option_state_reader.py. Real temp JSON files on disk
(the function under test reads a file by path) but no Kite session — pure
read-only parsing against fixtures this test writes itself, using the exact
shape long_option_persistence.py/Position.to_dict() produce.
"""

import json
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from long_option_state_reader import read_open_anchor

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def anchor(trade_state="T1_PROTECTED", t1_executed=True, direction="LONG", entry_price=180.0,
           current_stop=181.0, initial_quantity=10, remaining_quantity=5, realized_pnl=75.0):
    return {
        "instrument": "NIFTY26AUG25000CE", "entry_price": entry_price, "initial_quantity": initial_quantity,
        "current_price": 201.5, "highest_price": 205.0, "remaining_quantity": remaining_quantity,
        "realized_pnl": realized_pnl, "current_stop": current_stop, "trade_state": trade_state,
        "t1_executed": t1_executed, "runner_activated": False, "exit_reason": None,
        "position_id": "NIFTY26AUG25000CE@x", "product": "MIS", "direction": direction, "log": [],
    }


def state_with_book(symbol, cohorts):
    return {"positions": {}, "closed_trades": [], "order_ledger": {},
            "books": {symbol: {"instrument": symbol, "cohorts": cohorts}}}


# ============================================================
print("=== basic read ===")
# ============================================================
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "long_option_state.json"
    path.write_text(json.dumps(state_with_book(
        "NIFTY26AUG25000CE", [{"anchor": anchor(), "followers": []}],
    )))
    result = read_open_anchor(path, "NIFTY26AUG25000CE")
    check("anchor found", result is not None)
    if result:
        check("entry_price preserved", result["entry_price"] == 180.0, str(result))
        check("current_stop preserved", result["current_stop"] == 181.0, str(result))
        check("trade_state preserved as plain string", result["trade_state"] == "T1_PROTECTED", str(result))
        check("t1_hit True when t1_executed", result["t1_hit"] is True)
        check("t1_label is the cosmetic +15 PTS default", result["t1_label"] == "+15 PTS")
        check("t2_hit False (T1_PROTECTED hasn't reached PROFIT_LOCK_25)", result["t2_hit"] is False)
        check("direction preserved", result["direction"] == "LONG")


# ============================================================
print("\n=== T2 reached states ===")
# ============================================================
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "long_option_state.json"
    path.write_text(json.dumps(state_with_book(
        "NIFTY26AUG25000CE", [{"anchor": anchor(trade_state="PROFIT_LOCK_25"), "followers": []}],
    )))
    r = read_open_anchor(path, "NIFTY26AUG25000CE")
    check("PROFIT_LOCK_25 -> t2_hit True", r["t2_hit"] is True)

    path.write_text(json.dumps(state_with_book(
        "NIFTY26AUG25000CE", [{"anchor": anchor(trade_state="RUNNER"), "followers": []}],
    )))
    r2 = read_open_anchor(path, "NIFTY26AUG25000CE")
    check("RUNNER -> t2_hit True", r2["t2_hit"] is True)

    path.write_text(json.dumps(state_with_book(
        "NIFTY26AUG25000CE", [{"anchor": anchor(trade_state="PROFIT_LOCK_20"), "followers": []}],
    )))
    r3 = read_open_anchor(path, "NIFTY26AUG25000CE")
    check("PROFIT_LOCK_20 -> t2_hit False (not yet at +25)", r3["t2_hit"] is False)


# ============================================================
print("\n=== closed anchor -> None (nothing open to show) ===")
# ============================================================
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "long_option_state.json"
    path.write_text(json.dumps(state_with_book(
        "NIFTY26AUG25000CE", [{"anchor": anchor(trade_state="CLOSED"), "followers": []}],
    )))
    check("closed anchor returns None", read_open_anchor(path, "NIFTY26AUG25000CE") is None)


# ============================================================
print("\n=== most recent cohort wins when an instrument has multiple ===")
# ============================================================
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "long_option_state.json"
    old_cohort = {"anchor": anchor(trade_state="CLOSED", entry_price=150.0), "followers": []}
    new_cohort = {"anchor": anchor(trade_state="INITIAL_RISK", entry_price=190.0, t1_executed=False), "followers": []}
    path.write_text(json.dumps(state_with_book("NIFTY26AUG25000CE", [old_cohort, new_cohort])))
    r = read_open_anchor(path, "NIFTY26AUG25000CE")
    check("reads the LAST cohort, not the first", r is not None and r["entry_price"] == 190.0, str(r))


# ============================================================
print("\n=== missing / malformed inputs never raise ===")
# ============================================================
with tempfile.TemporaryDirectory() as d:
    check("missing file -> None", read_open_anchor(Path(d) / "nope.json", "X") is None)

    corrupt = Path(d) / "corrupt.json"
    corrupt.write_text("{not valid json")
    check("corrupt file -> None", read_open_anchor(corrupt, "X") is None)

    empty_books = Path(d) / "empty.json"
    empty_books.write_text(json.dumps({"books": {}}))
    check("no book for symbol -> None", read_open_anchor(empty_books, "NIFTY26AUG25000CE") is None)

    no_cohorts = Path(d) / "no_cohorts.json"
    no_cohorts.write_text(json.dumps(state_with_book("NIFTY26AUG25000CE", [])))
    check("book with zero cohorts -> None", read_open_anchor(no_cohorts, "NIFTY26AUG25000CE") is None)


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
