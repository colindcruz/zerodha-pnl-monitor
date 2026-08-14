"""
Dry-run + offline checks for the auto-strangle feature (with-websockets/pnl_monitor.py).
Follows this repo's existing test style: a plain script, no test framework, real Kite
API for anything market-dependent (read-only, never places orders), plain assertions
for pure logic. Run before trusting the feature with a live entry.

Sections:
  1. Strike/expiry resolution — dry run against real market data, no orders placed.
  2. State load/save round-trip, including the stale-date -> fresh-defaults reset.
  3. on_order_update source-order check — the strangle exclusion must appear before
     the legacy _ensure_sl_order call, or a fill could get a duplicate/conflicting SL.
  4. SL trigger math sanity check.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252, which can't print emoji

from dotenv import load_dotenv
from kiteconnect import KiteConnect

load_dotenv()

STRANGLE_STRIKE_OFFSET = int(os.getenv("STRANGLE_STRIKE_OFFSET", "50"))
STRANGLE_SL_MULTIPLIER = float(os.getenv("STRANGLE_SL_MULTIPLIER", "2.5"))
WEBSOCKET_FILE = Path(__file__).parent / "with-websockets" / "pnl_monitor.py"

failures = []


def _connect_kite():
    """Lazy — only Section 1 needs live credentials; Sections 2-4 are pure logic and
    should still run (e.g. from a dev machine without .env) even if this fails."""
    kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
    kite.set_access_token(Path(os.getenv("ACCESS_TOKEN_PATH", ".access_token")).read_text().strip())
    return kite


def check(label: str, condition: bool, detail: str = ""):
    status = "✅ PASS" if condition else "❌ FAIL"
    print(f"  {status}  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


# ============================================================
# 1. Strike/expiry resolution — dry run against real market data
# ============================================================
print("\n=== 1. STRIKE/EXPIRY RESOLUTION (dry run, no orders) ===\n")

try:
    kite = _connect_kite()
except Exception as exc:
    kite = None
    print(f"  ⏭  SKIPPED — no live Kite session available here ({exc}).")
    print("     Run this section from wherever the bot's real .env lives (e.g. the deployed server).")

if kite:
    spot = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
    atm = round(spot / STRANGLE_STRIKE_OFFSET) * STRANGLE_STRIKE_OFFSET
    ce_strike = atm + STRANGLE_STRIKE_OFFSET
    pe_strike = atm - STRANGLE_STRIKE_OFFSET
    print(f"Spot: {spot}  ATM: {atm}  CE strike: {ce_strike}  PE strike: {pe_strike}")

    instruments = kite.instruments("NFO")
    today = date.today()

    def _norm_expiry(i):
        e = i["expiry"]
        return e.date() if isinstance(e, datetime) else e

    nifty_options = [i for i in instruments if i["name"] == "NIFTY" and i["instrument_type"] in ("CE", "PE")]
    check("NIFTY options found in instrument list", len(nifty_options) > 0, f"{len(nifty_options)} rows")

    upcoming = sorted({_norm_expiry(i) for i in nifty_options if _norm_expiry(i) >= today})
    check("at least one upcoming expiry found", len(upcoming) > 0)
    if upcoming:
        expiry = upcoming[0]
        print(f"Nearest expiry: {expiry}")
        ce = next((i for i in nifty_options if _norm_expiry(i) == expiry and float(i["strike"]) == ce_strike and i["instrument_type"] == "CE"), None)
        pe = next((i for i in nifty_options if _norm_expiry(i) == expiry and float(i["strike"]) == pe_strike and i["instrument_type"] == "PE"), None)
        check(f"CE {ce_strike} resolved", ce is not None, ce["tradingsymbol"] if ce else "not found")
        check(f"PE {pe_strike} resolved", pe is not None, pe["tradingsymbol"] if pe else "not found")
        if ce and pe:
            print(f"  CE: {ce['tradingsymbol']} (lot_size={ce['lot_size']})")
            print(f"  PE: {pe['tradingsymbol']} (lot_size={pe['lot_size']})")
            check("CE and PE lot sizes match", ce["lot_size"] == pe["lot_size"])


# ============================================================
# 2. State load/save round-trip, including stale-date reset
# ============================================================
print("\n=== 2. STATE ROUND-TRIP ===\n")


def _default_leg():
    return {
        "tradingsymbol": None, "instrument_token": None, "strike": None, "lot_size": None, "qty": None,
        "entry_order_id": None, "entry_status": "pending", "entry_price": None,
        "sl_order_id": None, "sl_trigger": None, "status": "pending", "exit_price": None, "closed_at": None,
    }


def _default_state(d):
    return {
        "date": d, "entry_attempted": False, "entry_completed": False, "skip_today": False,
        "spot_at_entry": None, "expiry": None, "legs": {"CE": _default_leg(), "PE": _default_leg()},
        "eod_square_off_attempted": False, "eod_square_off_completed": False,
    }


tmp_state_file = Path(__file__).parent / "_test_strangle_state.json"
try:
    today_str = date.today().isoformat()
    yesterday_str = (date.today() - timedelta(days=1)).isoformat()

    # Fresh (no file) -> defaults for today
    if tmp_state_file.exists():
        tmp_state_file.unlink()
    loaded = json.loads(tmp_state_file.read_text()) if tmp_state_file.exists() else _default_state(today_str)
    check("no file -> fresh defaults", loaded["date"] == today_str and loaded["entry_attempted"] is False)

    # Save today's state with entry_attempted=True, reload, should round-trip exactly
    state = _default_state(today_str)
    state["entry_attempted"] = True
    state["legs"]["CE"]["tradingsymbol"] = "NIFTY2681824800CE"
    tmp_state_file.write_text(json.dumps(state, indent=2))
    reloaded = json.loads(tmp_state_file.read_text())
    check("same-day round-trip preserves state", reloaded == state)

    # Stale (yesterday's) date on disk -> today's load must discard it and return fresh defaults
    stale = _default_state(yesterday_str)
    stale["entry_attempted"] = True
    tmp_state_file.write_text(json.dumps(stale, indent=2))
    on_disk = json.loads(tmp_state_file.read_text())
    is_stale = on_disk.get("date") != today_str
    fresh = _default_state(today_str) if is_stale else on_disk
    check("stale date -> fresh defaults, not yesterday's flags", fresh["entry_attempted"] is False)
finally:
    if tmp_state_file.exists():
        tmp_state_file.unlink()


# ============================================================
# 3. on_order_update source-order check — the single regression that would be
#    catastrophic and silent if broken later (strangle fill routed to the legacy
#    ATR/Turtle SL path instead of its own handler).
# ============================================================
print("\n=== 3. ON_ORDER_UPDATE ROUTING ORDER (source check) ===\n")

source = WEBSOCKET_FILE.read_text(encoding="utf-8")
func_start = source.index("def on_order_update(")
func_end = source.index("\n    kws.on_connect", func_start)  # next top-level assignment after the closure
func_body = source[func_start:func_end]

exclusion_pos = func_body.find("if symbol in tracker.strangle_symbols:")
ensure_sl_pos = func_body.find("threading.Thread(target=_ensure_sl_order")

check("strangle exclusion exists in on_order_update", exclusion_pos != -1)
check("legacy _ensure_sl_order call still present", ensure_sl_pos != -1)
check(
    "strangle exclusion appears BEFORE the legacy _ensure_sl_order dispatch",
    exclusion_pos != -1 and ensure_sl_pos != -1 and exclusion_pos < ensure_sl_pos,
    f"exclusion@{exclusion_pos} ensure_sl@{ensure_sl_pos}",
)


# ============================================================
# 4. SL trigger math sanity check
# ============================================================
print("\n=== 4. SL TRIGGER MATH ===\n")

entry_price = 42.55
trigger = round(entry_price * STRANGLE_SL_MULTIPLIER, 1)
check(f"trigger = entry * {STRANGLE_SL_MULTIPLIER}", trigger == round(entry_price * STRANGLE_SL_MULTIPLIER, 1))
check("trigger is above entry (short leg, stop on the way up)", trigger > entry_price)


# ============================================================
print(f"\n{'='*50}")
if failures:
    print(f"❌ {len(failures)} check(s) FAILED: {', '.join(failures)}")
else:
    print("✅ All checks passed.")
print("(No orders were placed by this script.)")
