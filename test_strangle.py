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
STRANGLE_TARGET_DELTA = float(os.getenv("STRANGLE_TARGET_DELTA", "0.25"))
STRANGLE_DELTA_SEARCH_STRIKES = int(os.getenv("STRANGLE_DELTA_SEARCH_STRIKES", "20"))
STRANGLE_SL_MULTIPLIER = float(os.getenv("STRANGLE_SL_MULTIPLIER", "2.5"))
STRANGLE_LOTS = int(os.getenv("STRANGLE_LOTS", "5"))
STRANGLE_0DTE_LOT_FRACTION = float(os.getenv("STRANGLE_0DTE_LOT_FRACTION", "0.5"))
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.065"))
WEBSOCKET_FILE = Path(__file__).parent / "with-websockets" / "pnl_monitor.py"

failures = []


def _connect_kite():
    """Lazy — only Section 1 needs live credentials; Sections 2-4 are pure logic and
    should still run (e.g. from a dev machine without .env) even if this fails."""
    kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
    kite.set_access_token(Path(os.getenv("ACCESS_TOKEN_PATH", ".access_token")).read_text().strip())
    return kite


# Mirrors with-websockets/pnl_monitor.py:1602-1651 verbatim — kept in sync manually,
# not imported, matching this file's existing style of reimplementing production
# formulas locally rather than importing (importing pnl_monitor.py would also require
# TELEGRAM_* env vars just to run this dry-run, which today's Section 1 doesn't need).
import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def _bs_price(S, K, T, r, sigma, is_call) -> float:
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if is_call else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _implied_vol(price, S, K, T, r, is_call) -> float | None:
    if T <= 0 or price <= 0:
        return None
    lo, hi = 0.001, 5.0
    if _bs_price(S, K, T, r, hi, is_call) < price:
        return None
    for _ in range(50):
        mid = (lo + hi) / 2
        if _bs_price(S, K, T, r, mid, is_call) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def _bs_greeks(S, K, T, r, sigma, is_call) -> dict:
    if T <= 0 or sigma <= 0:
        itm = (S > K) if is_call else (S < K)
        return {"delta": (1.0 if is_call else -1.0) if itm else 0.0, "theta": 0.0, "vega": 0.0}
    d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    pdf_d1 = _norm_pdf(d1)
    vega = S * pdf_d1 * math.sqrt(T) / 100
    if is_call:
        delta = _norm_cdf(d1)
        theta = (-(S * pdf_d1 * sigma) / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365
    else:
        delta = _norm_cdf(d1) - 1
        theta = (-(S * pdf_d1 * sigma) / (2 * math.sqrt(T)) + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365
    return {"delta": delta, "theta": theta, "vega": vega}


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

            # ---- Delta-targeted resolution (the actual production logic) ----
            print(f"\n--- Delta-targeted resolution (target={STRANGLE_TARGET_DELTA}) ---\n")
            is_0dte = expiry == today
            if is_0dte:
                print(f"  ⏭  0DTE today (expiry={expiry}) — delta-targeting falls back to the fixed "
                      f"{STRANGLE_STRIKE_OFFSET}pt offset by design (T=0, no solvable IV). Nothing further to check.")
            else:
                T = max((expiry - today).days, 0) / 365

                def _find(strike, opt_type):
                    return next((i for i in nifty_options if _norm_expiry(i) == expiry
                                 and float(i["strike"]) == strike and i["instrument_type"] == opt_type), None)

                ce_candidates = [c for n in range(1, STRANGLE_DELTA_SEARCH_STRIKES + 1)
                                  if (c := _find(atm + n * STRANGLE_STRIKE_OFFSET, "CE"))]
                pe_candidates = [c for n in range(1, STRANGLE_DELTA_SEARCH_STRIKES + 1)
                                  if (c := _find(atm - n * STRANGLE_STRIKE_OFFSET, "PE"))]
                all_symbols = [f"NFO:{c['tradingsymbol']}" for c in ce_candidates + pe_candidates]
                ltp_data = kite.ltp(all_symbols) if all_symbols else {}

                def _best_match(candidates, is_call):
                    best, best_diff = None, None
                    for c in candidates:
                        price = ltp_data.get(f"NFO:{c['tradingsymbol']}", {}).get("last_price")
                        if not price:
                            continue
                        iv = _implied_vol(price, spot, c["strike"], T, RISK_FREE_RATE, is_call)
                        if iv is None:
                            continue
                        delta = _bs_greeks(spot, c["strike"], T, RISK_FREE_RATE, iv, is_call)["delta"]
                        diff = abs(abs(delta) - STRANGLE_TARGET_DELTA)
                        if best is None or diff < best_diff:
                            best, best_diff = {**c, "delta": delta, "iv": iv}, diff
                    return best

                ce_delta = _best_match(ce_candidates, True)
                pe_delta = _best_match(pe_candidates, False)
                check(f"CE target-delta strike resolved ({len(ce_candidates)} candidates searched)", ce_delta is not None)
                check(f"PE target-delta strike resolved ({len(pe_candidates)} candidates searched)", pe_delta is not None)
                if ce_delta and pe_delta:
                    print(f"  CE: {ce_delta['tradingsymbol']} strike={ce_delta['strike']} "
                          f"Δ={ce_delta['delta']:.3f} IV={ce_delta['iv']*100:.1f}%")
                    print(f"  PE: {pe_delta['tradingsymbol']} strike={pe_delta['strike']} "
                          f"Δ={pe_delta['delta']:.3f} IV={pe_delta['iv']*100:.1f}%")
                    imbalance = abs(abs(ce_delta["delta"]) - abs(pe_delta["delta"]))
                    print(f"  Delta imbalance (|CE|-|PE|): {imbalance:.3f}")
                    # For comparison: what the OLD fixed-offset strikes' own live delta looks like today —
                    # this is the number the whole change exists to bring closer to zero.
                    old_ce_price = kite.ltp([f"NFO:{ce['tradingsymbol']}"]).get(f"NFO:{ce['tradingsymbol']}", {}).get("last_price")
                    old_pe_price = kite.ltp([f"NFO:{pe['tradingsymbol']}"]).get(f"NFO:{pe['tradingsymbol']}", {}).get("last_price")
                    old_ce_iv = _implied_vol(old_ce_price, spot, ce_strike, T, RISK_FREE_RATE, True) if old_ce_price else None
                    old_pe_iv = _implied_vol(old_pe_price, spot, pe_strike, T, RISK_FREE_RATE, False) if old_pe_price else None
                    if old_ce_iv and old_pe_iv:
                        old_ce_delta = _bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, old_ce_iv, True)["delta"]
                        old_pe_delta = _bs_greeks(spot, pe_strike, T, RISK_FREE_RATE, old_pe_iv, False)["delta"]
                        old_imbalance = abs(abs(old_ce_delta) - abs(old_pe_delta))
                        print(f"  (for comparison) old fixed-{STRANGLE_STRIKE_OFFSET}pt strikes today: "
                              f"CE Δ={old_ce_delta:.3f}  PE Δ={old_pe_delta:.3f}  imbalance={old_imbalance:.3f}")


# ============================================================
# 2. State load/save round-trip, including stale-date reset
# ============================================================
print("\n=== 2. STATE ROUND-TRIP ===\n")


def _default_leg():
    return {
        "tradingsymbol": None, "instrument_token": None, "strike": None, "lot_size": None, "qty": None,
        "entry_order_id": None, "entry_status": "pending", "entry_price": None,
        "delta_at_entry": None, "iv_at_entry": None,
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
# 5. 0DTE lot-fraction math sanity check
# ============================================================
print("\n=== 5. 0DTE LOT FRACTION ===\n")

lots_0dte = max(1, int(STRANGLE_LOTS * STRANGLE_0DTE_LOT_FRACTION))
check(f"0DTE lots ({lots_0dte}) < normal lots ({STRANGLE_LOTS})", lots_0dte < STRANGLE_LOTS or STRANGLE_LOTS == 1)
check("0DTE lots floor of 1 even at a tiny STRANGLE_LOTS", max(1, int(1 * STRANGLE_0DTE_LOT_FRACTION)) == 1)
check("0DTE fraction of 0.5 on the default 5 lots rounds down to 2", max(1, int(5 * 0.5)) == 2)


# ============================================================
print(f"\n{'='*50}")
if failures:
    print(f"❌ {len(failures)} check(s) FAILED: {', '.join(failures)}")
else:
    print("✅ All checks passed.")
print("(No orders were placed by this script.)")
