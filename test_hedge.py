"""
Dry-run + offline checks for the weekly NIFTY hedge feature (with-websockets/pnl_monitor.py).
Follows this repo's existing test style (see test_strangle.py): a plain script, no test
framework, real Kite API for anything market-dependent (read-only, never places orders),
plain assertions for pure logic. Run before trusting the feature with a live entry.

Sections:
  1. Strike/expiry resolution — dry run against real market data, no orders placed.
  2. State load/save round-trip, including the expiry-based (not date-based) staleness
     reset — the key difference from the strangle's own date-keyed state.
  3. on_order_update source-order check — the hedge exclusion must appear before the
     legacy _ensure_sl_order call, or a fill could get an unwanted SL placed against it.
  4. Entry limit-price direction sanity check — a hedge BUY should price above LTP
     (aggressive fill), opposite direction from the strangle's SELL entry.
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

HEDGE_STRIKE_OFFSET = int(os.getenv("HEDGE_STRIKE_OFFSET", "1000"))
STRANGLE_STRIKE_OFFSET = int(os.getenv("STRANGLE_STRIKE_OFFSET", "50"))
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
# 0. Sanity: hedge is meaningfully farther OTM than the strangle's own legs
# ============================================================
check(
    "HEDGE_STRIKE_OFFSET is wider than STRANGLE_STRIKE_OFFSET",
    HEDGE_STRIKE_OFFSET > STRANGLE_STRIKE_OFFSET,
    f"hedge={HEDGE_STRIKE_OFFSET} strangle={STRANGLE_STRIKE_OFFSET}",
)


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
    ce_strike = atm + HEDGE_STRIKE_OFFSET
    pe_strike = atm - HEDGE_STRIKE_OFFSET
    print(f"Spot: {spot}  ATM: {atm}  Hedge CE strike: {ce_strike}  Hedge PE strike: {pe_strike}")

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
        print(f"Nearest expiry (same one the strangle would use today): {expiry}")
        ce = next((i for i in nifty_options if _norm_expiry(i) == expiry and float(i["strike"]) == ce_strike and i["instrument_type"] == "CE"), None)
        pe = next((i for i in nifty_options if _norm_expiry(i) == expiry and float(i["strike"]) == pe_strike and i["instrument_type"] == "PE"), None)
        check(f"hedge CE {ce_strike} resolved", ce is not None, ce["tradingsymbol"] if ce else "not found")
        check(f"hedge PE {pe_strike} resolved", pe is not None, pe["tradingsymbol"] if pe else "not found")
        if ce and pe:
            print(f"  CE: {ce['tradingsymbol']} (lot_size={ce['lot_size']})")
            print(f"  PE: {pe['tradingsymbol']} (lot_size={pe['lot_size']})")
            check("CE and PE lot sizes match", ce["lot_size"] == pe["lot_size"])
            if kite:
                try:
                    ce_ltp = kite.ltp([f"NFO:{ce['tradingsymbol']}"])[f"NFO:{ce['tradingsymbol']}"]["last_price"]
                    pe_ltp = kite.ltp([f"NFO:{pe['tradingsymbol']}"])[f"NFO:{pe['tradingsymbol']}"]["last_price"]
                    print(f"  CE premium: Rs {ce_ltp}  PE premium: Rs {pe_ltp}  (compare against your target — HEDGE_STRIKE_OFFSET is untuned, see MANUAL.md)")
                except Exception as exc:
                    print(f"  Could not fetch live premiums: {exc}")


# ============================================================
# 2. State load/save round-trip, including expiry-based staleness
# ============================================================
print("\n=== 2. STATE ROUND-TRIP (expiry-keyed, NOT date-keyed) ===\n")


def _default_hedge_leg():
    return {
        "tradingsymbol": None, "instrument_token": None, "strike": None, "lot_size": None, "qty": None,
        "entry_order_id": None, "entry_status": "pending", "entry_price": None,
        "status": "pending", "exit_price": None, "closed_at": None,
    }


def _default_hedge_state(week_key):
    return {
        "week_key": week_key, "entry_attempted": False, "entry_completed": False, "skip_week": False,
        "spot_at_entry": None, "expiry": None, "legs": {"CE": _default_hedge_leg(), "PE": _default_hedge_leg()},
    }


def _week_key(d):
    return (d - timedelta(days=d.weekday())).isoformat()


tmp_state_file = Path(__file__).parent / "_test_hedge_state.json"
try:
    today = date.today()
    today_str = today.isoformat()
    tomorrow_str = (today + timedelta(days=1)).isoformat()
    yesterday_str = (today - timedelta(days=1)).isoformat()

    # Fresh (no file) -> defaults for the current week
    if tmp_state_file.exists():
        tmp_state_file.unlink()
    loaded = _default_hedge_state(_week_key(today))
    check("no file -> fresh defaults", loaded["entry_attempted"] is False and loaded["expiry"] is None)

    # Held expiry is TOMORROW (still active) -> a restart on a later day this week must
    # still recognize it, unlike the strangle's date-keyed state which would discard it.
    state = _default_hedge_state(_week_key(today))
    state["entry_attempted"] = True
    state["entry_completed"] = True
    state["expiry"] = tomorrow_str
    state["legs"]["CE"]["tradingsymbol"] = "NIFTY2681825800CE"
    tmp_state_file.write_text(json.dumps(state, indent=2))
    on_disk = json.loads(tmp_state_file.read_text())
    expiry = datetime.strptime(on_disk["expiry"], "%Y-%m-%d").date()
    still_active = today <= expiry
    check("expiry in the future -> state is KEPT (not treated as stale just because it's a new day)", still_active)
    check("kept state preserves entry_completed", still_active and on_disk["entry_completed"] is True)

    # Held expiry is YESTERDAY (already passed/cash-settled) -> must reset to fresh
    # defaults for a new entry attempt, this being the ONLY staleness trigger (not date).
    stale = _default_hedge_state(_week_key(today - timedelta(days=7)))
    stale["entry_attempted"] = True
    stale["entry_completed"] = True
    stale["expiry"] = yesterday_str
    tmp_state_file.write_text(json.dumps(stale, indent=2))
    on_disk = json.loads(tmp_state_file.read_text())
    expiry = datetime.strptime(on_disk["expiry"], "%Y-%m-%d").date()
    expired = today > expiry
    fresh = _default_hedge_state(_week_key(today)) if expired else on_disk
    check("expiry in the past -> fresh defaults, not last week's flags", fresh["entry_attempted"] is False)
finally:
    if tmp_state_file.exists():
        tmp_state_file.unlink()


# ============================================================
# 3. on_order_update source-order check — the hedge exclusion must appear before the
#    legacy _ensure_sl_order dispatch (same regression class as test_strangle.py's own
#    check), so a hedge fill never gets an unwanted SL placed against it.
# ============================================================
print("\n=== 3. ON_ORDER_UPDATE ROUTING ORDER (source check) ===\n")

source = WEBSOCKET_FILE.read_text(encoding="utf-8")
func_start = source.index("def on_order_update(")
func_end = source.index("\n    kws.on_connect", func_start)
func_body = source[func_start:func_end]

strangle_pos = func_body.find("if symbol in tracker.strangle_symbols:")
hedge_pos = func_body.find("if symbol in tracker.hedge_symbols:")
ensure_sl_pos = func_body.find("threading.Thread(target=_ensure_sl_order")

check("hedge exclusion exists in on_order_update", hedge_pos != -1)
check("legacy _ensure_sl_order call still present", ensure_sl_pos != -1)
check(
    "hedge exclusion appears BEFORE the legacy _ensure_sl_order dispatch",
    hedge_pos != -1 and ensure_sl_pos != -1 and hedge_pos < ensure_sl_pos,
    f"hedge@{hedge_pos} ensure_sl@{ensure_sl_pos}",
)
check(
    "hedge exclusion appears AFTER the strangle exclusion (grouping, not correctness)",
    strangle_pos != -1 and hedge_pos != -1 and strangle_pos < hedge_pos,
    f"strangle@{strangle_pos} hedge@{hedge_pos}",
)

# Every exit_non_hedge_positions call site must account for the hedge — the 3:06pm final
# safety net is the one deliberate exception that excludes ONLY the hedge (not the
# strangle, which it's meant to catch as a backstop).
exit_calls = [line for line in source.splitlines() if "exit_non_hedge_positions(kite" in line and "def " not in line]
check("at least one exit_non_hedge_positions call site found", len(exit_calls) > 0, f"{len(exit_calls)} found")
missing_hedge = [line.strip() for line in exit_calls if "hedge_symbols" not in line]
check(
    "every exit_non_hedge_positions call site references hedge_symbols",
    len(missing_hedge) == 0,
    f"{len(missing_hedge)} call site(s) missing it: {missing_hedge}",
)


# ============================================================
# 4. Entry limit-price direction sanity check
# ============================================================
print("\n=== 4. ENTRY LIMIT-PRICE DIRECTION ===\n")

ltp = 3.50
buffer_pct = float(os.getenv("HEDGE_ENTRY_BUFFER_PCT", "0.01"))
buffer = max(1.0, round(ltp * buffer_pct, 1))
buy_limit = round(ltp + buffer, 1)
check("hedge BUY limit prices ABOVE ltp (aggressive fill)", buy_limit > ltp, f"ltp={ltp} limit={buy_limit}")


# ============================================================
print(f"\n{'='*50}")
if failures:
    print(f"❌ {len(failures)} check(s) FAILED: {', '.join(failures)}")
else:
    print("✅ All checks passed.")
print("(No orders were placed by this script.)")
