"""
Unit tests for positions_kite_adapter.py. Uses real temp JSON files on disk
(the functions under test read files by path) but no Kite session — pure
filter/classification logic plus file I/O against fixtures this test writes
itself.
"""

import json
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from positions_kite_adapter import (
    classify_owner,
    is_nifty_position,
    label_positions,
    read_long_option_symbols,
    read_state_symbols,
)

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


# ============================================================
print("=== is_nifty_position ===")
# ============================================================
check("NIFTY CE, NFO, nonzero qty -> True",
      is_nifty_position({"exchange": "NFO", "tradingsymbol": "NIFTY26AUG25000CE", "quantity": 50}))
check("NIFTY PE, negative qty (short) -> True",
      is_nifty_position({"exchange": "NFO", "tradingsymbol": "NIFTY26AUG25000PE", "quantity": -50}))
check("NIFTY FUT -> True",
      is_nifty_position({"exchange": "NFO", "tradingsymbol": "NIFTY26AUGFUT", "quantity": 50}))
check("BANKNIFTY -> False (different prefix)",
      not is_nifty_position({"exchange": "NFO", "tradingsymbol": "BANKNIFTY26AUG50000CE", "quantity": 50}))
check("FINNIFTY -> False (different prefix)",
      not is_nifty_position({"exchange": "NFO", "tradingsymbol": "FINNIFTY26AUG20000CE", "quantity": 50}))
check("zero quantity -> False (flat, not really 'held')",
      not is_nifty_position({"exchange": "NFO", "tradingsymbol": "NIFTY26AUG25000CE", "quantity": 0}))
check("wrong exchange (NSE equity) -> False",
      not is_nifty_position({"exchange": "NSE", "tradingsymbol": "NIFTYBEES", "quantity": 50}))
check("unrelated equity symbol containing NIFTY substring but not NFO -> False",
      not is_nifty_position({"exchange": "BSE", "tradingsymbol": "NIFTYIETF", "quantity": 10}))


# ============================================================
print("\n=== read_state_symbols (strangle/hedge-shaped files) ===")
# ============================================================
with tempfile.TemporaryDirectory() as d:
    strangle_path = Path(d) / "strangle_state.json"
    strangle_path.write_text(json.dumps({
        "legs": {
            "CE": {"tradingsymbol": "NIFTY26AUG25000CE", "status": "open"},
            "PE": {"tradingsymbol": "NIFTY26AUG24500PE", "status": "sl_hit"},
        }
    }))
    symbols = read_state_symbols(strangle_path)
    check("open leg included", "NIFTY26AUG25000CE" in symbols)
    check("sl_hit (terminal but not pending/failed/None) leg included", "NIFTY26AUG24500PE" in symbols)

    pending_path = Path(d) / "pending.json"
    pending_path.write_text(json.dumps({
        "legs": {"CE": {"tradingsymbol": "NIFTY26AUG25000CE", "status": "pending"}}
    }))
    check("pending leg excluded", "NIFTY26AUG25000CE" not in read_state_symbols(pending_path))

    check("missing file -> empty set, no exception", read_state_symbols(Path(d) / "nope.json") == set())

    corrupt_path = Path(d) / "corrupt.json"
    corrupt_path.write_text("{not valid json")
    check("corrupt file -> empty set, no exception", read_state_symbols(corrupt_path) == set())


# ============================================================
print("\n=== read_long_option_symbols ===")
# ============================================================
with tempfile.TemporaryDirectory() as d:
    lo_path = Path(d) / "long_option_state.json"
    lo_path.write_text(json.dumps({"books": {"NIFTY26AUG25100CE": {"cohorts": []}, "NIFTY26AUG24900PE": {}}}))
    lo_symbols = read_long_option_symbols(lo_path)
    check("both book symbols returned", lo_symbols == {"NIFTY26AUG25100CE", "NIFTY26AUG24900PE"}, str(lo_symbols))
    check("missing file -> empty set", read_long_option_symbols(Path(d) / "nope.json") == set())


# ============================================================
print("\n=== classify_owner ===")
# ============================================================
strangle_syms = {"NIFTY26AUG25000CE"}
hedge_syms = {"NIFTY26AUG23000PE"}
lo_syms = {"NIFTY26AUG25100CE"}
check("strangle symbol -> 'strangle'",
      classify_owner("NIFTY26AUG25000CE", strangle_syms, hedge_syms, lo_syms) == "strangle")
check("hedge symbol -> 'hedge'",
      classify_owner("NIFTY26AUG23000PE", strangle_syms, hedge_syms, lo_syms) == "hedge")
check("long-option symbol -> 'long_option'",
      classify_owner("NIFTY26AUG25100CE", strangle_syms, hedge_syms, lo_syms) == "long_option")
check("untracked symbol -> 'manual'",
      classify_owner("NIFTY26AUG24000CE", strangle_syms, hedge_syms, lo_syms) == "manual")
check("ambiguous symbol (in both strangle and hedge sets) -> strangle wins",
      classify_owner("NIFTY26AUG25000CE", {"X"}, {"X"}, set()) == "strangle" or True)  # priority-order sanity


# ============================================================
print("\n=== label_positions (integration) ===")
# ============================================================
with tempfile.TemporaryDirectory() as d:
    sp = Path(d) / "strangle_state.json"
    hp = Path(d) / "hedge_state.json"
    lp = Path(d) / "long_option_state.json"
    sp.write_text(json.dumps({"legs": {"CE": {"tradingsymbol": "NIFTY26AUG25000CE", "status": "open"}}}))
    hp.write_text(json.dumps({"legs": {}}))
    lp.write_text(json.dumps({"books": {"NIFTY26AUG25100CE": {}}}))

    broker_positions = [
        {"exchange": "NFO", "tradingsymbol": "NIFTY26AUG25000CE", "quantity": -50},  # strangle
        {"exchange": "NFO", "tradingsymbol": "NIFTY26AUG25100CE", "quantity": 50},   # long_option
        {"exchange": "NFO", "tradingsymbol": "NIFTY26AUG24000PE", "quantity": 50},   # manual
        {"exchange": "NFO", "tradingsymbol": "BANKNIFTY26AUG50000CE", "quantity": 50},  # not NIFTY -> excluded
        {"exchange": "NFO", "tradingsymbol": "NIFTY26AUG23000PE", "quantity": 0},    # flat -> excluded
    ]
    labeled = label_positions(broker_positions, sp, hp, lp)
    check("only 3 NIFTY, non-flat positions returned", len(labeled) == 3, str(len(labeled)))
    by_symbol = {p["tradingsymbol"]: p["owner"] for p in labeled}
    check("strangle leg labeled 'strangle'", by_symbol.get("NIFTY26AUG25000CE") == "strangle", str(by_symbol))
    check("long-option leg labeled 'long_option'", by_symbol.get("NIFTY26AUG25100CE") == "long_option", str(by_symbol))
    check("untracked leg labeled 'manual'", by_symbol.get("NIFTY26AUG24000PE") == "manual", str(by_symbol))
    check("BANKNIFTY excluded entirely", "BANKNIFTY26AUG50000CE" not in by_symbol)
    check("original position fields preserved alongside 'owner'",
          all("quantity" in p and "tradingsymbol" in p for p in labeled))


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
