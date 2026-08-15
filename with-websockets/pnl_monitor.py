"""
Real-time P&L Monitor for Zerodha Kite positions
==================================================

Alert logic:
- Sends a full P&L update every time total P&L crosses a MILESTONE_STEP boundary
  (e.g. every Rs 5000: ...-10k, -5k, 0, 5k, 10k ... 35k, 40k).
- Once P&L reaches TRAIL_ACTIVATION_THRESHOLD, milestone alerts stop and the
  trailing profit-lock takes over.

Secrets are loaded from a .env file (see .env.example).
Run generate_token.py each morning to refresh the daily access token.
"""

import json
import math
import os
import time
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from kiteconnect import KiteConnect, KiteTicker

load_dotenv()

# ============================================================
# CONFIG — all values come from .env
# ============================================================

API_KEY             = os.environ["KITE_API_KEY"]
ACCESS_TOKEN_PATH   = Path(os.getenv("ACCESS_TOKEN_PATH", ".access_token"))

TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]

NTFY_TOPIC          = os.getenv("NTFY_TOPIC", "")  # e.g. colin-pnl-xyz123

MILESTONE_STEP              = float(os.getenv("MILESTONE_STEP", "5000"))

# ---- Green day floor ----
GREEN_DAY_ACTIVATION        = float(os.getenv("GREEN_DAY_ACTIVATION", "20000"))   # arm when P&L hits this
GREEN_DAY_FLOOR             = float(os.getenv("GREEN_DAY_FLOOR", "5000"))          # exit if P&L drops to this

# ---- Trailing profit-lock ----
TRAIL_ACTIVATION_THRESHOLD  = float(os.getenv("TRAIL_ACTIVATION_THRESHOLD", "40000"))

# Tiered drawdown: (min_peak, drawdown_amount) — sorted highest first
TRAIL_TIERS = [
    (100_000, 15_000),  # above ₹1L   → ₹15k drawdown
    ( 80_000, 12_000),  # ₹80k–₹1L   → ₹12k drawdown
    ( 60_000, 10_000),  # ₹60k–₹80k  → ₹10k drawdown
    ( 40_000,  8_000),  # ₹40k–₹60k  → ₹8k drawdown
]

def _trail_drawdown(peak: float) -> float:
    for threshold, drawdown in TRAIL_TIERS:
        if peak >= threshold:
            return drawdown
    return 8_000  # fallback
EXIT_ALERT_REPEAT_SECONDS   = int(os.getenv("EXIT_ALERT_REPEAT_SECONDS", "15"))
TRAIL_CHECK_INTERVAL        = int(os.getenv("TRAIL_CHECK_INTERVAL", "1"))
HEARTBEAT_INTERVAL_SECONDS  = int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "3600"))

# ---- Auto-exit on trailing breach ----
AUTO_EXIT               = os.getenv("AUTO_EXIT", "true").lower() == "true"
PAUSE_FILE              = Path("pause_auto_exit")  # touch this file to temporarily disable auto-exit
MUTE_FILE               = Path("mute_notifications")  # touch this file to silence all notifications

def auto_exit_enabled() -> bool:
    """Returns False if the pause file exists — allows disabling auto-exit without restart."""
    if PAUSE_FILE.exists():
        return False
    return AUTO_EXIT

# ---- Protective SL-order placement ----
PAUSE_SL_FILE = Path("pause_sl_orders")  # touch this file to temporarily stop placing/resizing SL orders

def sl_orders_enabled() -> bool:
    """Returns False if the pause file exists — lets you stop SL-order placement without restart."""
    return not PAUSE_SL_FILE.exists()

HEDGE_PRICE_THRESHOLD   = float(os.getenv("HEDGE_PRICE_THRESHOLD", "5.0"))  # positions with LTP below this are kept
EXIT_BUFFER_SECONDS     = int(os.getenv("EXIT_BUFFER_SECONDS", "30"))  # wait this long below floor before exiting

# ---- NIFTY short strangle (auto) ----
STRANGLE_ENABLED             = os.getenv("STRANGLE_ENABLED", "true").lower() == "true"
STRANGLE_ENTRY_TIME          = (9, 23)   # HH, MM IST — entry window opens
STRANGLE_ENTRY_CUTOFF        = (9, 35)   # entry window closes; give up + alert if not done by then
STRANGLE_EXIT_TIME           = (15, 0)   # HH, MM IST — square off any legs still open
STRANGLE_STRIKE_OFFSET       = int(os.getenv("STRANGLE_STRIKE_OFFSET", "50"))    # 1-strike-OTM on NIFTY's 50-pt strikes
STRANGLE_SL_MULTIPLIER       = float(os.getenv("STRANGLE_SL_MULTIPLIER", "2.0"))  # regime-dependent (backtested 2.0-3.0x, 2.0 had the best combined return/drawdown across both tested years) — tune via /set, not a fixed truth
STRANGLE_LOTS                = int(os.getenv("STRANGLE_LOTS", "5"))
STRANGLE_PRODUCT             = "NRML"
STRANGLE_ENTRY_BUFFER_PCT    = float(os.getenv("STRANGLE_ENTRY_BUFFER_PCT", "0.01"))
STRANGLE_ENTRY_RETRY_SECONDS = int(os.getenv("STRANGLE_ENTRY_RETRY_SECONDS", "20"))
STRANGLE_ENTRY_MAX_RETRIES   = int(os.getenv("STRANGLE_ENTRY_MAX_RETRIES", "3"))

PAUSE_STRANGLE_FILE = Path("pause_strangle")  # touch this file to stop future days' auto-entry

def strangle_entries_enabled() -> bool:
    """Returns False if the pause file exists — lets you stop strangle auto-entry without restart."""
    return STRANGLE_ENABLED and not PAUSE_STRANGLE_FILE.exists()

STRANGLE_STATE_FILE = Path(os.getenv("STRANGLE_STATE_FILE", "strangle_state.json"))

# ---- Profit target exit ----
PROFIT_TARGET           = float(os.getenv("PROFIT_TARGET", "80000"))   # exit all non-hedge positions when P&L hits this

# ---- Loss warnings + hard limit ----
LOSS_WARNING_1          = float(os.getenv("LOSS_WARNING_1", "-20000"))  # warning alert
LOSS_WARNING_2          = float(os.getenv("LOSS_WARNING_2", "-30000"))  # cut position size alert
LOSS_LIMIT              = float(os.getenv("LOSS_LIMIT", "-40000"))      # hard shutdown — exit all

# ---- Post-exit cool-off ----
# After a loss-limit, profit-target, or trail-activation auto-exit, block manual trading
# for this long: any new/added position gets auto-squared-off instead of protected with an SL.
COOLOFF_MINUTES         = int(os.getenv("COOLOFF_MINUTES", "15"))

# ---- Position size limit ----
MAX_POSITION_QTY        = int(os.getenv("MAX_POSITION_QTY", "1950"))    # max qty per individual non-hedge position

# ---- Greeks (for /greeks) ----
RISK_FREE_RATE           = float(os.getenv("RISK_FREE_RATE", "0.065"))  # used for Black-Scholes IV/Greeks

INDEX_SPOT_SYMBOLS = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MIDCAP SELECT",
    "SENSEX": "BSE:SENSEX",
    "BANKEX": "BSE:BANKEX",
}

LOG_FILE = os.getenv("LOG_FILE", "pnl_monitor.log")
HISTORY_FILE = Path(os.getenv("HISTORY_FILE", "pnl_history.json"))

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN  = (9, 15)
MARKET_CLOSE = (15, 40)

# NSE Equity/Derivatives-segment trading holidays. Verified against two independent
# sources (cleartax.in, groww.in) cross-checked against each other on 2026-08-15 —
# not fetched from NSE's own site, so re-verify before relying on it for a new year.
# Needs a manual update every year; nothing here fetches this automatically.
# Excludes the special Sunday 2026-11-08 Muhurat Trading session (Diwali Laxmi Pujan) —
# that's an extra half-hour evening session, not a normal trading day, and isn't
# relevant to this bot's 9:23am strangle entry / 3pm exit window.
NSE_HOLIDAYS = {
    "2026-01-15",  # Maharashtra municipal elections
    "2026-01-26",  # Republic Day
    "2026-03-03",  # Holi
    "2026-03-26",  # Shri Ram Navami
    "2026-03-31",  # Shri Mahavir Jayanti
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-28",  # Bakri Id
    "2026-06-26",  # Muharram
    "2026-09-14",  # Ganesh Chaturthi
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-11-10",  # Diwali - Balipratipada
    "2026-11-24",  # Prakash Gurpurb Sri Guru Nanak Dev
    "2026-12-25",  # Christmas
}

# ============================================================

def _read_access_token() -> str:
    if ACCESS_TOKEN_PATH.exists():
        token = ACCESS_TOKEN_PATH.read_text().strip()
        if token:
            return token
    token = os.getenv("KITE_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("No access token found. Run generate_token.py first.")
    return token


def is_nse_holiday(d: date) -> bool:
    return d.isoformat() in NSE_HOLIDAYS


def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5 or is_nse_holiday(now.date()):
        return False
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t <= MARKET_CLOSE


# ---- Strangle state (date-keyed, survives restarts — see reconcile_strangle_state_on_startup) ----

def _default_leg() -> dict:
    return {
        "tradingsymbol": None, "instrument_token": None, "strike": None,
        "lot_size": None, "qty": None,
        "entry_order_id": None, "entry_status": "pending",  # pending|order_placed|filled|failed
        "entry_price": None,
        "sl_order_id": None, "sl_trigger": None,
        "status": "pending",  # pending|open|sl_hit|closed_eod|closed_manual|closed_other|failed
        "exit_price": None, "closed_at": None,
    }


def _default_strangle_state(today: str) -> dict:
    return {
        "date": today, "entry_attempted": False, "entry_completed": False, "skip_today": False,
        "spot_at_entry": None, "expiry": None,
        "legs": {"CE": _default_leg(), "PE": _default_leg()},
        "eod_square_off_attempted": False, "eod_square_off_completed": False,
    }


def load_strangle_state() -> dict:
    """Load today's strangle state; a stale (yesterday's) date is discarded in favor of fresh defaults."""
    today = date.today().isoformat()
    if STRANGLE_STATE_FILE.exists():
        try:
            state = json.loads(STRANGLE_STATE_FILE.read_text())
            if state.get("date") == today:
                return state
        except (json.JSONDecodeError, KeyError):
            pass
    return _default_strangle_state(today)


def save_strangle_state(state: dict) -> None:
    STRANGLE_STATE_FILE.write_text(json.dumps(state, indent=2))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("pnl_monitor")

# ---- Notification helpers ----

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        if not resp.ok:
            log.error(f"Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        log.error(f"Telegram send error: {e}")


def send_ntfy(title: str, body: str, priority: str = "default"):
    if not NTFY_TOPIC:
        return
    from urllib.parse import quote
    try:
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": quote(title), "Priority": priority},
            timeout=10,
        )
        if not resp.ok:
            log.error(f"ntfy send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        log.error(f"ntfy send error: {e}")


def notify(title: str, body: str, priority: str = "default"):
    """Send to both Telegram and ntfy. Silenced if mute file exists."""
    if MUTE_FILE.exists():
        return
    send_telegram(f"{title}\n{body}" if title else body)
    send_ntfy(title, body, priority)


# ---- Auto-exit helper ----

FREEZE_QTY_LIMIT = 1800  # NSE exchange freeze quantity per order for F&O
ATR_PERIOD = 14
ATR_INTERVAL = "5minute"

# ---- VIX-adaptive ATR multiplier (manual-trade SL only — the strangle has its own,
# separate premium-multiple SL and is never touched by this) ----
# multiplier = ATR_BASE_MULTIPLIER + (current_VIX - ATR_REFERENCE_VIX) * ATR_VIX_SENSITIVITY,
# clamped to [ATR_MIN_MULTIPLIER, ATR_MAX_MULTIPLIER]. Wider stops when VIX is elevated,
# tighter when calm, instead of the old fixed 2x. NOTE: these defaults are a
# reasoning-based starting point, not backtested against real fills — validate against
# historical data before trusting this with meaningfully larger size.
ATR_BASE_MULTIPLIER  = float(os.getenv("ATR_BASE_MULTIPLIER", "1.5"))
ATR_REFERENCE_VIX    = float(os.getenv("ATR_REFERENCE_VIX", "15"))
ATR_VIX_SENSITIVITY  = float(os.getenv("ATR_VIX_SENSITIVITY", "0.1"))
ATR_MIN_MULTIPLIER   = float(os.getenv("ATR_MIN_MULTIPLIER", "1.5"))
ATR_MAX_MULTIPLIER   = float(os.getenv("ATR_MAX_MULTIPLIER", "4.0"))
INDIA_VIX_SYMBOL     = "NSE:INDIA VIX"


def compute_vix_atr_multiplier(current_vix: float) -> float:
    """Pure function — see the module comment above for the formula and its caveat."""
    multiplier = ATR_BASE_MULTIPLIER + (current_vix - ATR_REFERENCE_VIX) * ATR_VIX_SENSITIVITY
    return max(ATR_MIN_MULTIPLIER, min(ATR_MAX_MULTIPLIER, multiplier))


def _fetch_india_vix(kite: KiteConnect) -> float | None:
    try:
        return kite.ltp([INDIA_VIX_SYMBOL])[INDIA_VIX_SYMBOL]["last_price"]
    except Exception as exc:
        log.warning("Could not fetch India VIX: %s", exc)
        return None


def _fetch_atr(kite: KiteConnect, instrument_token: int, symbol: str) -> float | None:
    """Fetch 5-min historical data and compute 14-period ATR for a symbol."""
    from datetime import timedelta
    try:
        now_ist = datetime.now(IST)
        from_dt = now_ist - timedelta(days=2)  # 2 days ensures enough bars even at market open
        records = kite.historical_data(
            instrument_token,
            from_date=from_dt.replace(tzinfo=None),
            to_date=now_ist.replace(tzinfo=None),
            interval=ATR_INTERVAL,
        )
        if len(records) < ATR_PERIOD + 1:
            log.warning("Not enough bars for ATR on %s (%d bars)", symbol, len(records))
            return None
        trs = []
        for i in range(1, len(records)):
            h, l, pc = records[i]["high"], records[i]["low"], records[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return round(sum(trs[-ATR_PERIOD:]) / ATR_PERIOD, 2)
    except Exception as exc:
        log.warning("ATR fetch failed for %s: %s", symbol, exc)
        return None

def _place_sl_order(kite: KiteConnect, pos: dict, atr: float, multiplier: float) -> tuple[float, float, list]:
    """
    Place SL-Limit order at `multiplier` x ATR from the stop anchor (Turtle-style: the
    most recent entry price when pyramiding into a winner, not the blended average cost —
    so the stop ratchets toward each new add instead of loosening back to the average).
    Falls back to average_price if the caller doesn't supply a stop_anchor (e.g. the
    REST-poll fallback path, where anchor == average cost anyway since it only ever fires
    on a single fill). `multiplier` is normally VIX-adaptive — see compute_vix_atr_multiplier.
    """
    symbol  = pos["tradingsymbol"]
    qty     = pos["quantity"]
    anchor  = pos.get("stop_anchor", pos["average_price"])
    sl_dist = multiplier * atr

    if qty > 0:  # LONG — sell SL below the anchor
        tx            = kite.TRANSACTION_TYPE_SELL
        trigger       = round(anchor - sl_dist, 1)
        buffer        = min(10.0, max(1.0, round(trigger * 0.01, 1)))
        limit_price   = round(trigger - buffer, 1)
    else:        # SHORT — buy SL above the anchor
        tx            = kite.TRANSACTION_TYPE_BUY
        trigger       = round(anchor + sl_dist, 1)
        buffer        = min(10.0, max(1.0, round(trigger * 0.01, 1)))
        limit_price   = round(trigger + buffer, 1)

    lot_sizes = _get_lot_sizes(kite, [symbol])
    lot_size  = lot_sizes.get(symbol, 1)
    max_chunk = (FREEZE_QTY_LIMIT // lot_size) * lot_size if lot_size > 1 else FREEZE_QTY_LIMIT
    remaining = abs(qty)
    order_ids = []

    while remaining > 0:
        chunk = min(remaining, max_chunk)
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=pos["exchange"],
            tradingsymbol=symbol,
            transaction_type=tx,
            quantity=chunk,
            product=pos["product"],
            order_type=kite.ORDER_TYPE_SL,
            price=limit_price,
            trigger_price=trigger,
        )
        log.info("SL order placed: %s %s qty=%d trigger=%.1f limit=%.1f order_id=%s",
                 tx, symbol, chunk, trigger, limit_price, order_id)
        order_ids.append(order_id)
        remaining -= chunk

    return trigger, limit_price, order_ids


def _ensure_sl_order(kite: KiteConnect, tracker: "PositionTracker", token: int, pos: dict) -> None:
    """
    Resizes protection to match a position's current state: cancels any resting SL for
    this symbol, then places a fresh one sized to the current quantity, anchored at
    pos["stop_anchor"] (Turtle-style — the latest add's price when pyramiding, not the
    blended average cost, so the stop ratchets toward each new entry rather than loosening
    back to the average). Called on every fill that leaves a nonzero position — a fresh
    open, adding to an existing position, or partially reducing one — so the SL never sits
    stale at the wrong size (which for a reduction could otherwise oversell/overbuy past
    the actual holding once triggered).

    Shared by both the real-time (on_order_update) and periodic (REST refresh) detection
    paths. Safe to call from any thread — does its own locking and makes blocking network
    calls, so callers on the websocket thread should dispatch it via a background thread.
    """
    symbol = pos["tradingsymbol"]

    if not sl_orders_enabled():
        log.info("SL-order placement paused — skipping %s.", symbol)
        return

    price = pos.get("last_price") or pos.get("average_price", 0)
    if price < HEDGE_PRICE_THRESHOLD:
        return  # hedge leg — never auto-protected, same exclusion auto-exit uses

    with tracker.sl_order_lock:
        try:
            n_cancelled = _cancel_open_orders(kite, [symbol])
            if n_cancelled:
                log.info("Cancelled %d prior order(s) for %s before resizing SL.", n_cancelled, symbol)
        except Exception as exc:
            log.warning("Could not cancel prior orders for %s: %s", symbol, exc)

        direction = "LONG" if pos["quantity"] > 0 else "SHORT"
        atr = _fetch_atr(kite, token, symbol)
        atr_line = f"5-min ATR (14): Rs {atr}" if atr else "ATR unavailable"

        current_vix = _fetch_india_vix(kite)
        if current_vix is None:
            log.warning("India VIX unavailable for %s — falling back to base multiplier %.2f.", symbol, ATR_BASE_MULTIPLIER)
        multiplier = compute_vix_atr_multiplier(current_vix) if current_vix is not None else ATR_BASE_MULTIPLIER

        sl_line = ""
        if atr:
            sl_dist = multiplier * atr
            log.info(
                "SL sizing for %s: VIX=%s multiplier=%.2f atr=Rs %.2f sl_dist=Rs %.2f",
                symbol, f"{current_vix:.2f}" if current_vix is not None else "unavailable", multiplier, atr, sl_dist,
            )
            try:
                trigger, limit_price, order_ids = _place_sl_order(kite, pos, atr, multiplier)
                vix_line = f"VIX {current_vix:.2f} -> {multiplier:.2f}x ATR" if current_vix is not None else f"VIX unavailable -> {multiplier:.2f}x ATR (fallback)"
                sl_line = f"SL order placed: trigger Rs {trigger} | limit Rs {limit_price} ({vix_line})"
            except Exception as exc:
                log.error("SL order failed for %s: %s", symbol, exc)
                sl_line = f"SL order FAILED: {exc}"

    anchor = pos.get("stop_anchor", pos["average_price"])
    notify(
        f"📐 Position update: {symbol}",
        f"{direction} {abs(pos['quantity'])} qty @ avg Rs {pos['average_price']:.2f} "
        f"(SL anchor Rs {anchor:.2f})\n{atr_line}\n{sl_line}".strip(),
    )


def _get_lot_sizes(kite: KiteConnect, symbols: list[str]) -> dict[str, int]:
    """Fetch lot sizes for a list of NFO tradingsymbols."""
    try:
        instruments = kite.instruments("NFO")
        return {i["tradingsymbol"]: int(i["lot_size"]) for i in instruments if i["tradingsymbol"] in symbols}
    except Exception as exc:
        log.warning("Could not fetch lot sizes: %s — defaulting to 1", exc)
        return {}


def _resolve_strangle_legs(kite: KiteConnect) -> dict | None:
    """
    Resolves today's 1-strike-OTM NIFTY strangle: current spot, ATM strike, and the
    exact CE/PE tradingsymbols for the nearest expiry. Uses "earliest expiry >= today
    across all NIFTY option rows" rather than a weekday assumption (NSE's weekly-expiry
    weekday has changed before and could again) — a monthly contract's expiry is always
    itself the final weekly expiry of that month, so this is correct regardless of
    whatever cadence is in effect. Returns None (and logs why) if spot or either strike
    can't be resolved — never guesses a nearby strike.
    """
    try:
        spot = kite.ltp([INDEX_SPOT_SYMBOLS["NIFTY"]])[INDEX_SPOT_SYMBOLS["NIFTY"]]["last_price"]
    except Exception as exc:
        log.error("Could not fetch NIFTY spot for strangle resolution: %s", exc)
        return None

    atm = round(spot / STRANGLE_STRIKE_OFFSET) * STRANGLE_STRIKE_OFFSET
    ce_strike = atm + STRANGLE_STRIKE_OFFSET
    pe_strike = atm - STRANGLE_STRIKE_OFFSET

    try:
        instruments = kite.instruments("NFO")
    except Exception as exc:
        log.error("Could not fetch NFO instruments for strangle resolution: %s", exc)
        return None

    today = date.today()

    def _norm_expiry(i):
        e = i["expiry"]
        return e.date() if isinstance(e, datetime) else e

    nifty_options = [i for i in instruments if i["name"] == "NIFTY" and i["instrument_type"] in ("CE", "PE")]
    upcoming_expiries = sorted({_norm_expiry(i) for i in nifty_options if _norm_expiry(i) >= today})
    if not upcoming_expiries:
        log.error("No upcoming NIFTY option expiry found — cannot resolve strangle.")
        return None
    expiry = upcoming_expiries[0]

    def _find(strike, opt_type):
        for i in nifty_options:
            if _norm_expiry(i) == expiry and float(i["strike"]) == strike and i["instrument_type"] == opt_type:
                return {
                    "tradingsymbol": i["tradingsymbol"],
                    "instrument_token": i["instrument_token"],
                    "strike": float(i["strike"]),
                    "lot_size": int(i["lot_size"]),
                }
        return None

    ce = _find(ce_strike, "CE")
    pe = _find(pe_strike, "PE")
    if not ce or not pe:
        log.error("Could not resolve strangle legs for expiry %s: CE %s found=%s, PE %s found=%s",
                   expiry, ce_strike, bool(ce), pe_strike, bool(pe))
        return None

    return {"expiry": expiry.isoformat(), "spot": spot, "CE": ce, "PE": pe}


def _place_strangle_leg_order(kite: KiteConnect, leg: dict, transaction_type: str, buffer_pct: float) -> list:
    """
    Aggressive LIMIT order (LTP +/- buffer_pct) for one strangle leg — entry (SELL) or
    exit (BUY), chunked around FREEZE_QTY_LIMIT the same way _place_sl_order/
    exit_non_hedge_positions do. Returns the list of order_id(s) placed.

    Always uses leg["qty"] (fixed once at entry resolution), never the live
    STRANGLE_LOTS global — a mid-day /set strangle_lots must not resize an order for a
    leg that's already open (or already has a quantity committed to today's entry),
    only affect quantity going forward on the *next* day's entry.
    """
    symbol = leg["tradingsymbol"]
    lot_size = leg["lot_size"]
    qty = leg["qty"]

    ltp_data = kite.ltp([f"NFO:{symbol}"])
    ltp = ltp_data[f"NFO:{symbol}"]["last_price"]
    buffer = max(1.0, round(ltp * buffer_pct, 1))
    limit_price = round(ltp - buffer if transaction_type == kite.TRANSACTION_TYPE_SELL else ltp + buffer, 1)

    max_chunk = (FREEZE_QTY_LIMIT // lot_size) * lot_size if lot_size > 1 else FREEZE_QTY_LIMIT
    remaining = qty
    order_ids = []
    while remaining > 0:
        chunk = min(remaining, max_chunk)
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange="NFO",
            tradingsymbol=symbol,
            transaction_type=transaction_type,
            quantity=chunk,
            product=STRANGLE_PRODUCT,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=limit_price,
        )
        log.info("Strangle leg order: %s %s qty=%d limit=%.1f order_id=%s", transaction_type, symbol, chunk, limit_price, order_id)
        order_ids.append(order_id)
        remaining -= chunk
    return order_ids


def _place_strangle_sl_order(kite: KiteConnect, leg: dict, trigger: float) -> list:
    """
    SL-LIMIT BUY to close a short strangle leg at trigger = entry_price * STRANGLE_SL_MULTIPLIER.
    Uses leg["qty"] (fixed at entry), not the live STRANGLE_LOTS — see _place_strangle_leg_order.
    """
    symbol = leg["tradingsymbol"]
    lot_size = leg["lot_size"]
    qty = leg["qty"]

    buffer = min(10.0, max(1.0, round(trigger * 0.01, 1)))
    limit_price = round(trigger + buffer, 1)  # buying to close a short, so limit sits above trigger

    max_chunk = (FREEZE_QTY_LIMIT // lot_size) * lot_size if lot_size > 1 else FREEZE_QTY_LIMIT
    remaining = qty
    order_ids = []
    while remaining > 0:
        chunk = min(remaining, max_chunk)
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange="NFO",
            tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=chunk,
            product=STRANGLE_PRODUCT,
            order_type=kite.ORDER_TYPE_SL,
            price=limit_price,
            trigger_price=round(trigger, 1),
        )
        log.info("Strangle SL order: BUY %s qty=%d trigger=%.1f limit=%.1f order_id=%s", symbol, chunk, trigger, limit_price, order_id)
        order_ids.append(order_id)
        remaining -= chunk
    return order_ids


def enter_strangle(kite: KiteConnect, tracker: "PositionTracker") -> None:
    """
    Orchestrates the 9:20am strangle entry: resolves strikes, places SELL LIMIT orders
    for both legs, retries any unfilled leg with a widening buffer, and gives up with
    an urgent alert if the entry window closes before both legs fill. Deliberately never
    auto-corrects an asymmetric fill (one leg filled, one not) and never escalates to a
    MARKET order — both are explicit product decisions, not oversights.
    """
    with tracker.strangle_lock:
        state = tracker.strangle_state
        if state.get("entry_completed"):
            log.info("Strangle entry already completed today — skipping.")
            return

    legs = _resolve_strangle_legs(kite)
    if legs is None:
        notify("🚨 Strangle entry FAILED", "Could not resolve strikes/expiry — no strangle entered today. Check logs.", priority="urgent")
        return

    with tracker.strangle_lock:
        state["spot_at_entry"] = legs["spot"]
        state["expiry"] = legs["expiry"]
        for leg_key in ("CE", "PE"):
            info = legs[leg_key]
            leg = state["legs"][leg_key]
            leg["tradingsymbol"] = info["tradingsymbol"]
            leg["instrument_token"] = info["instrument_token"]
            leg["strike"] = info["strike"]
            leg["lot_size"] = info["lot_size"]
            leg["qty"] = info["lot_size"] * STRANGLE_LOTS
            tracker.strangle_symbols.add(info["tradingsymbol"])
        save_strangle_state(state)

    log.info("Strangle legs resolved: spot=%.2f expiry=%s CE=%s PE=%s",
              legs["spot"], legs["expiry"], legs["CE"]["tradingsymbol"], legs["PE"]["tradingsymbol"])

    for leg_key in ("CE", "PE"):
        leg = state["legs"][leg_key]
        try:
            order_ids = _place_strangle_leg_order(kite, leg, kite.TRANSACTION_TYPE_SELL, STRANGLE_ENTRY_BUFFER_PCT)
            with tracker.strangle_lock:
                leg["entry_order_id"] = order_ids[0] if len(order_ids) == 1 else order_ids
                leg["entry_status"] = "order_placed"
                save_strangle_state(state)
        except Exception as exc:
            log.error("Failed to place strangle %s entry order: %s", leg_key, exc)
            with tracker.strangle_lock:
                leg["entry_status"] = "failed"
                save_strangle_state(state)
            notify(f"🚨 Strangle {leg_key} entry order FAILED", f"{leg['tradingsymbol']}: {exc}", priority="urgent")

    # Only report legs whose order actually went in — a per-leg failure above (e.g.
    # insufficient margin) must not be papered over by an unconditional "SOLD CE & PE".
    placed = [k for k in ("CE", "PE") if state["legs"][k]["entry_status"] == "order_placed"]
    if placed:
        symbols_line = " & ".join(
            f"{state['legs'][k]['tradingsymbol']} x{state['legs'][k]['qty']}" for k in placed
        )
        notify(
            "🦅 Strangle entry orders placed",
            f"SOLD {symbols_line}\n"
            f"Spot: Rs {legs['spot']:.2f} | Expiry: {legs['expiry']}\nAwaiting fill confirmation...",
        )

    # Retry loop: widen the limit buffer for any leg still unfilled, up to STRANGLE_ENTRY_MAX_RETRIES.
    for attempt in range(1, STRANGLE_ENTRY_MAX_RETRIES + 1):
        time.sleep(STRANGLE_ENTRY_RETRY_SECONDS)
        now_ist = datetime.now(IST)
        cutoff = now_ist.replace(hour=STRANGLE_ENTRY_CUTOFF[0], minute=STRANGLE_ENTRY_CUTOFF[1], second=0, microsecond=0)
        if now_ist >= cutoff:
            break
        with tracker.strangle_lock:
            unfilled = [k for k, l in state["legs"].items() if l["entry_status"] == "order_placed"]
        if not unfilled:
            break
        for leg_key in unfilled:
            leg = state["legs"][leg_key]
            try:
                _cancel_open_orders(kite, [leg["tradingsymbol"]])
                order_ids = _place_strangle_leg_order(
                    kite, leg, kite.TRANSACTION_TYPE_SELL, STRANGLE_ENTRY_BUFFER_PCT * (attempt + 1)
                )
                with tracker.strangle_lock:
                    leg["entry_order_id"] = order_ids[0] if len(order_ids) == 1 else order_ids
                    save_strangle_state(state)
                log.info("Strangle %s entry retried (attempt %d), wider buffer.", leg_key, attempt)
            except Exception as exc:
                log.error("Strangle %s entry retry failed: %s", leg_key, exc)

    # Give up on anything still unfilled once retries/window are exhausted.
    with tracker.strangle_lock:
        still_unfilled = [k for k, l in state["legs"].items() if l["entry_status"] != "filled"]
        if still_unfilled:
            for leg_key in still_unfilled:
                if state["legs"][leg_key]["entry_status"] != "filled":
                    state["legs"][leg_key]["entry_status"] = "failed"
            save_strangle_state(state)
    if still_unfilled:
        filled = [k for k in ("CE", "PE") if k not in still_unfilled]
        notify(
            "🚨 Strangle entry INCOMPLETE",
            f"Gave up after {STRANGLE_ENTRY_MAX_RETRIES} retries. "
            f"Filled: {', '.join(filled) if filled else 'none'} | Unfilled: {', '.join(still_unfilled)}\n"
            f"No MARKET fallback, no auto-correction of the other leg — check /strangle_status and decide manually.",
            priority="urgent",
        )


def _on_strangle_leg_filled(kite: KiteConnect, tracker: "PositionTracker", data: dict) -> None:
    """
    Dedicated fill handler for strangle-owned symbols, dispatched from on_order_update
    instead of the legacy ATR/Turtle path (see the exclusion branch there). SELL fill =
    entry (places the premium-multiple SL using the real fill price, not an estimate);
    BUY fill = exit — SL hit, EOD square-off, manual close, or swept up by a global
    stop mechanism (loss limit/trailing/profit target), labeled by matching order_id.

    Note: for a chunked entry (STRANGLE_LOTS large enough to exceed FREEZE_QTY_LIMIT),
    only the first chunk's fill sets entry_price/places the SL — matching this file's
    existing acceptance of not specially handling partial/multi-chunk fills elsewhere.
    Irrelevant at the default STRANGLE_LOTS=1 (no chunking occurs).
    """
    symbol = data.get("tradingsymbol")
    txn = data.get("transaction_type")
    order_id = data.get("order_id")
    try:
        avg_price = float(data.get("average_price") or 0)
    except (TypeError, ValueError):
        avg_price = 0.0

    with tracker.strangle_lock:
        state = tracker.strangle_state
        leg_key = next((k for k, l in state["legs"].items() if l.get("tradingsymbol") == symbol), None)
        if leg_key is None:
            log.warning("Strangle fill for unrecognized symbol %s — ignoring.", symbol)
            return
        leg = state["legs"][leg_key]

        if txn == "SELL" and leg["entry_status"] != "filled":
            leg["entry_price"] = avg_price
            leg["entry_status"] = "filled"
            leg["status"] = "open"
            trigger = round(avg_price * STRANGLE_SL_MULTIPLIER, 1)
            try:
                sl_order_ids = _place_strangle_sl_order(kite, leg, trigger)
                leg["sl_order_id"] = sl_order_ids[0] if len(sl_order_ids) == 1 else sl_order_ids
                leg["sl_trigger"] = trigger
                notify(
                    f"✅ Strangle {leg_key} filled",
                    f"{symbol} SOLD x{leg['qty']} @ Rs {avg_price:.2f} | SL trigger Rs {trigger:.2f}",
                )
            except Exception as exc:
                log.error("Strangle %s SL placement FAILED: %s", leg_key, exc)
                notify(
                    f"🚨 Strangle {leg_key} SL PLACEMENT FAILED",
                    f"{symbol} filled @ Rs {avg_price:.2f} but the SL order failed: {exc}\nNAKED POSITION — act manually.",
                    priority="urgent",
                )
            if all(l["entry_status"] == "filled" for l in state["legs"].values()):
                state["entry_completed"] = True

        elif txn == "BUY" and leg["status"] == "open":
            leg["exit_price"] = avg_price
            leg["closed_at"] = datetime.now(IST).isoformat()
            sl_ids = leg.get("sl_order_id")
            sl_ids = sl_ids if isinstance(sl_ids, list) else [sl_ids]
            if order_id in sl_ids:
                leg["status"] = "sl_hit"
                notify(
                    f"🛑 Strangle {leg_key} SL HIT",
                    f"{symbol} bought back @ Rs {avg_price:.2f} (trigger was Rs {leg['sl_trigger']:.2f})",
                    priority="high",
                )
            elif order_id in tracker.strangle_manual_exit_order_ids:
                leg["status"] = "closed_manual"
                notify(f"✅ Strangle {leg_key} closed (manual)", f"{symbol} bought back @ Rs {avg_price:.2f}")
            elif order_id in tracker.strangle_eod_exit_order_ids:
                leg["status"] = "closed_eod"
                notify(f"🕒 Strangle {leg_key} squared off (3pm)", f"{symbol} bought back @ Rs {avg_price:.2f}")
            else:
                leg["status"] = "closed_other"
                notify(
                    f"⚠️ Strangle {leg_key} closed via unrecognized order",
                    f"{symbol} bought back @ Rs {avg_price:.2f} — likely swept up by a global stop "
                    "(loss limit/trailing/profit target).",
                    priority="high",
                )
        save_strangle_state(state)


def square_off_strangle(kite: KiteConnect, tracker: "PositionTracker", reason: str) -> tuple[list, list]:
    """
    Closes any strangle leg still actually open. reason is "eod" or "manual" — used only
    to label the placed exit order so _on_strangle_leg_filled can attribute the fill
    correctly. Verifies against live kite.positions() rather than trusting persisted
    status alone, since a very recent SL hit might not have reached the fill handler yet.
    """
    with tracker.strangle_lock:
        state = tracker.strangle_state
        legs_snapshot = {k: dict(v) for k, v in state["legs"].items()}

    try:
        live = {p["tradingsymbol"]: p for p in kite.positions().get("net", []) if p["quantity"] != 0}
    except Exception as exc:
        log.error("square_off_strangle: could not fetch live positions: %s", exc)
        notify("🚨 Strangle square-off FAILED", f"Could not fetch live positions: {exc}", priority="urgent")
        return [], []

    closed, skipped = [], []
    for leg_key, leg in legs_snapshot.items():
        symbol = leg.get("tradingsymbol")
        if not symbol:
            continue
        if leg["status"] != "open":
            skipped.append(symbol)
            continue
        pos = live.get(symbol)
        if not pos:
            log.warning("Strangle leg %s marked open but flat in live positions — leaving to fill handler.", symbol)
            skipped.append(symbol)
            continue
        try:
            _cancel_open_orders(kite, [symbol])  # pulls the resting SL first, avoids a double-exit race
            order_ids = _place_strangle_leg_order(kite, leg, kite.TRANSACTION_TYPE_BUY, STRANGLE_ENTRY_BUFFER_PCT)
            target_set = tracker.strangle_eod_exit_order_ids if reason == "eod" else tracker.strangle_manual_exit_order_ids
            target_set.update(order_ids)
            closed.append(symbol)
        except Exception as exc:
            log.error("Failed to square off strangle leg %s: %s", symbol, exc)
            notify(f"🚨 Strangle {leg_key} square-off FAILED", f"{symbol}: {exc}", priority="urgent")

    if reason == "eod":
        with tracker.strangle_lock:
            state["eod_square_off_completed"] = True
            save_strangle_state(state)

    lines = []
    if closed:
        lines.append(f"Closed: {', '.join(closed)}")
    if skipped:
        lines.append(f"Skipped (already closed, or flat): {', '.join(skipped)}")
    notify(
        "🕒 Strangle squared off (3pm)" if reason == "eod" else "✅ Strangle closed (manual)",
        "\n".join(lines) if lines else "Nothing to close.",
    )
    return closed, skipped


def reconcile_strangle_state_on_startup(kite: KiteConnect, tracker: "PositionTracker") -> None:
    """
    Runs once at process start, right after PositionTracker construction. Systemd
    auto-restarts this bot on crash, so a mid-day restart is a real scenario, not a
    hypothetical — this never trusts strangle_state.json blindly, always cross-checks
    against kite.positions()/kite.orders() before deciding whether today's entry is
    pending, in-flight, or complete. This is the highest financial-risk function in the
    whole feature; ambiguous states are alerted, never silently guessed or retried.
    """
    with tracker.strangle_lock:
        state = tracker.strangle_state
        for leg in state["legs"].values():
            if leg.get("tradingsymbol"):
                tracker.strangle_symbols.add(leg["tradingsymbol"])
        has_legs = any(l.get("tradingsymbol") for l in state["legs"].values())

    if not has_legs:
        log.info("Strangle reconciliation: no legs resolved yet today, nothing to reconcile.")
        return

    try:
        live_positions = {p["tradingsymbol"]: p for p in kite.positions().get("net", []) if p["quantity"] != 0}
    except Exception as exc:
        log.error("Strangle reconciliation: could not fetch live positions: %s", exc)
        notify("🚨 Strangle reconciliation FAILED", f"Could not fetch live positions on restart: {exc}. Check /strangle_status manually.", priority="urgent")
        return

    try:
        live_orders = kite.orders()
    except Exception as exc:
        log.warning("Strangle reconciliation: could not fetch orders: %s", exc)
        live_orders = []

    changed = False
    with tracker.strangle_lock:
        for leg_key, leg in state["legs"].items():
            symbol = leg.get("tradingsymbol")
            if not symbol:
                continue
            live_pos = live_positions.get(symbol)

            if live_pos and leg["status"] in ("pending", "open"):
                # Genuinely open — trust it, backfill anything the crash interrupted.
                if leg["entry_status"] != "filled":
                    leg["entry_price"] = live_pos["average_price"]
                    leg["entry_status"] = "filled"
                    leg["status"] = "open"
                    changed = True
                resting_sl = next(
                    (o for o in live_orders if o.get("tradingsymbol") == symbol
                     and o.get("status") in ("OPEN", "TRIGGER PENDING") and o.get("transaction_type") == "BUY"),
                    None,
                )
                if resting_sl:
                    if leg.get("sl_order_id") != resting_sl["order_id"]:
                        leg["sl_order_id"] = resting_sl["order_id"]
                        leg["sl_trigger"] = float(resting_sl.get("trigger_price") or 0) or leg.get("sl_trigger")
                        changed = True
                    notify(
                        f"🔄 Strangle {leg_key} reconciled",
                        f"{symbol} open @ Rs {leg['entry_price']:.2f}, SL confirmed resting at Rs {leg.get('sl_trigger')}.",
                        priority="high",
                    )
                else:
                    # NAKED leg — no resting stop found. Place one immediately.
                    trigger = round(leg["entry_price"] * STRANGLE_SL_MULTIPLIER, 1)
                    try:
                        sl_order_ids = _place_strangle_sl_order(kite, leg, trigger)
                        leg["sl_order_id"] = sl_order_ids[0] if len(sl_order_ids) == 1 else sl_order_ids
                        leg["sl_trigger"] = trigger
                        changed = True
                        notify(
                            f"🚨 Strangle {leg_key} was NAKED on restart",
                            f"{symbol} open @ Rs {leg['entry_price']:.2f} with no resting SL — placed fresh SL @ Rs {trigger:.2f}.",
                            priority="urgent",
                        )
                    except Exception as exc:
                        notify(
                            f"🚨 Strangle {leg_key} NAKED and SL placement FAILED",
                            f"{symbol} open @ Rs {leg['entry_price']:.2f}, no SL, and placing one failed: {exc}\nACT MANUALLY NOW.",
                            priority="urgent",
                        )

            elif not live_pos and leg["status"] == "open":
                # Closed while the bot was down (SL hit, or closed manually elsewhere).
                leg["status"] = "closed_other"
                leg["closed_at"] = datetime.now(IST).isoformat()
                changed = True
                notify(
                    f"⚠️ Strangle {leg_key} closed while bot was down",
                    f"{symbol} is now flat — closed via SL or manual action during the restart.",
                    priority="high",
                )

            elif not live_pos and leg["status"] == "pending" and leg.get("entry_order_id"):
                order_id = leg["entry_order_id"]
                order_id = order_id[-1] if isinstance(order_id, list) else order_id
                matching = next((o for o in live_orders if o.get("order_id") == order_id), None)
                if matching is None:
                    pass  # nothing to reconcile against; the normal entry/retry flow will handle it
                elif matching.get("status") == "COMPLETE":
                    leg["entry_price"] = float(matching.get("average_price") or 0)
                    leg["entry_status"] = "filled"
                    leg["status"] = "open"
                    changed = True
                elif matching.get("status") in ("REJECTED", "CANCELLED"):
                    leg["entry_status"] = "failed"
                    changed = True
                    notify(
                        f"🚨 Strangle {leg_key} entry was rejected/cancelled before restart",
                        f"{symbol}: order {order_id} status {matching.get('status')}. No auto-retry — check /strangle_status.",
                        priority="urgent",
                    )
                # else still OPEN/TRIGGER PENDING — leave as attempted; the normal fill path (or a
                # fresh enter_strangle call, since entry_completed is still False) will pick it up.

        if changed:
            save_strangle_state(state)


def _cancel_open_orders(kite: KiteConnect, symbols: list[str]) -> int:
    """Cancel any pending/open orders for the given symbols before placing exit orders."""
    cancelled = 0
    try:
        orders = kite.orders()
    except Exception as exc:
        log.warning("Could not fetch open orders: %s — skipping cancel step", exc)
        return 0

    pending_statuses = {"OPEN", "TRIGGER PENDING"}
    for order in orders:
        if order.get("tradingsymbol") in symbols and order.get("status") in pending_statuses:
            try:
                kite.cancel_order(variety=order["variety"], order_id=order["order_id"])
                log.info("Cancelled pending order %s for %s", order["order_id"], order["tradingsymbol"])
                cancelled += 1
            except Exception as exc:
                log.warning("Could not cancel order %s: %s", order["order_id"], exc)
    return cancelled


def exit_non_hedge_positions(kite: KiteConnect, exclude_symbols: frozenset = frozenset()) -> tuple[list, list, list]:
    """
    Limit-exit all open positions with LTP >= HEDGE_PRICE_THRESHOLD.
    Cancels any pending orders for those symbols first to avoid double-exits.
    Exits sold legs (short) first, then bought legs (long).
    Splits large orders into chunks to stay within exchange freeze limits.
    exclude_symbols are skipped entirely (not even reported as hedges/skipped) — used
    to keep the auto-strangle feature's positions independent of these account-wide
    exits, since they're managed by their own SL/3pm logic instead.
    Returns (exited_symbols, skipped_symbols, failed_symbols).
    """
    data = kite.positions()
    net = [p for p in data.get("net", []) if p["quantity"] != 0 and p["tradingsymbol"] not in exclude_symbols]

    sold_legs   = [p for p in net if p["quantity"] < 0 and p["last_price"] >= HEDGE_PRICE_THRESHOLD]
    bought_legs = [p for p in net if p["quantity"] > 0 and p["last_price"] >= HEDGE_PRICE_THRESHOLD]
    hedges      = [p for p in net if p["last_price"] < HEDGE_PRICE_THRESHOLD]

    to_exit = sold_legs + bought_legs
    if not to_exit:
        return [], [p["tradingsymbol"] for p in hedges], []

    # Fetch live LTPs for limit price calculation
    exchange_symbols = [f"{p['exchange']}:{p['tradingsymbol']}" for p in to_exit]
    try:
        ltp_data = kite.ltp(exchange_symbols)
    except Exception as exc:
        log.warning("LTP fetch failed, using last_price: %s", exc)
        ltp_data = {}

    # Fetch lot sizes for splitting
    symbols = [p["tradingsymbol"] for p in to_exit]
    lot_sizes = _get_lot_sizes(kite, symbols)

    # Cancel any pending orders for these symbols before placing exit orders
    n_cancelled = _cancel_open_orders(kite, symbols)
    if n_cancelled:
        log.info("Cancelled %d pending order(s) before placing exit orders.", n_cancelled)

    exited, failed = [], []

    for pos in to_exit:
        symbol  = pos["tradingsymbol"]
        qty     = pos["quantity"]
        tx      = kite.TRANSACTION_TYPE_BUY if qty < 0 else kite.TRANSACTION_TYPE_SELL

        # Aggressive limit price — 1% of LTP, minimum Rs 1, for fast fill
        key = f"{pos['exchange']}:{symbol}"
        ltp = ltp_data.get(key, {}).get("last_price", pos["last_price"])
        buffer = max(1.0, round(ltp * 0.01, 1))
        limit_price = round(ltp - buffer if tx == kite.TRANSACTION_TYPE_SELL else ltp + buffer, 1)

        # Split into chunks within freeze limit
        lot_size  = lot_sizes.get(symbol, 1)
        max_chunk = (FREEZE_QTY_LIMIT // lot_size) * lot_size if lot_size > 1 else FREEZE_QTY_LIMIT
        remaining = abs(qty)
        success   = True

        while remaining > 0:
            chunk = min(remaining, max_chunk)
            try:
                order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=pos["exchange"],
                    tradingsymbol=symbol,
                    transaction_type=tx,
                    quantity=chunk,
                    product=pos["product"],
                    order_type=kite.ORDER_TYPE_LIMIT,
                    price=limit_price,
                )
                log.info("Exit order: %s %s qty=%d @ %.1f order_id=%s", tx, symbol, chunk, limit_price, order_id)
                remaining -= chunk
            except Exception as exc:
                log.error("Failed to exit %s qty=%d: %s", symbol, chunk, exc)
                failed.append(f"{symbol} (ERROR: {exc})")
                success = False
                break

        if success:
            exited.append(symbol)

    skipped = [p["tradingsymbol"] for p in hedges]
    return exited, skipped, failed


def _start_cooloff(tracker: "PositionTracker") -> datetime:
    """Arms the post-exit cool-off window; returns the IST timestamp it runs until."""
    until = datetime.now(IST) + timedelta(minutes=COOLOFF_MINUTES)
    with tracker.lock:
        tracker.cooloff_until = until
    return until


def _cooloff_active(tracker: "PositionTracker") -> bool:
    with tracker.lock:
        until = tracker.cooloff_until
    return until is not None and datetime.now(IST) < until


# ---- Greeks ----

def _get_option_meta(kite: KiteConnect, symbols: list[str]) -> dict[str, dict]:
    """Fetch strike/expiry/instrument_type/underlying/lot_size for NFO symbols."""
    try:
        instruments = kite.instruments("NFO")
        return {
            i["tradingsymbol"]: {
                "strike": float(i["strike"]),
                "expiry": i["expiry"],
                "instrument_type": i["instrument_type"],
                "name": i["name"],
                "lot_size": int(i["lot_size"]),
            }
            for i in instruments if i["tradingsymbol"] in symbols
        }
    except Exception as exc:
        log.warning("Could not fetch option metadata: %s — Greeks unavailable", exc)
        return {}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def _bs_price(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if is_call else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _implied_vol(price: float, S: float, K: float, T: float, r: float, is_call: bool) -> float | None:
    """Bisection solve for IV from the option's own LTP — BS price is monotonic in sigma, so this always converges."""
    if T <= 0 or price <= 0:
        return None
    lo, hi = 0.001, 5.0
    if _bs_price(S, K, T, r, hi, is_call) < price:
        return None  # quote implies vol beyond a sane range — skip rather than report garbage
    for _ in range(50):
        mid = (lo + hi) / 2
        if _bs_price(S, K, T, r, mid, is_call) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def _bs_greeks(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> dict:
    if T <= 0 or sigma <= 0:
        itm = (S > K) if is_call else (S < K)
        return {"delta": (1.0 if is_call else -1.0) if itm else 0.0, "theta": 0.0, "vega": 0.0}
    d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    pdf_d1 = _norm_pdf(d1)
    vega = S * pdf_d1 * math.sqrt(T) / 100  # rupees per share, per 1 point of IV
    if is_call:
        delta = _norm_cdf(d1)
        theta = (-(S * pdf_d1 * sigma) / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365
    else:
        delta = _norm_cdf(d1) - 1
        theta = (-(S * pdf_d1 * sigma) / (2 * math.sqrt(T)) + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365
    return {"delta": delta, "theta": theta, "vega": vega}


def compute_portfolio_greeks(kite: KiteConnect, details: list) -> tuple[dict, list]:
    """
    Net delta/theta/vega per underlying for all option/future legs in `details`
    (from PositionTracker.compute_pnl). IV is backed out from each leg's own LTP,
    so results track the market's current pricing rather than an assumed volatility.
    Returns (per_underlying, skipped) — skipped lists symbols we couldn't model
    (missing instrument metadata or no spot quote for the underlying).
    """
    symbols = [d["symbol"] for d in details]
    if not symbols:
        return {}, []

    meta = _get_option_meta(kite, symbols)

    underlyings = {m["name"] for m in meta.values()}
    spot_symbols = {name: INDEX_SPOT_SYMBOLS.get(name, f"NSE:{name}") for name in underlyings}
    try:
        spot_data = kite.ltp(list(spot_symbols.values())) if spot_symbols else {}
    except Exception as exc:
        log.warning("Could not fetch spot LTPs for Greeks: %s", exc)
        spot_data = {}
    spots = {name: spot_data.get(sym, {}).get("last_price") for name, sym in spot_symbols.items()}

    result: dict[str, dict] = {}
    skipped = []

    for d in details:
        info = meta.get(d["symbol"])
        if not info:
            skipped.append(d["symbol"])
            continue

        bucket = result.setdefault(
            info["name"], {"delta": 0.0, "theta": 0.0, "vega": 0.0, "lot_size": info["lot_size"]}
        )

        if info["instrument_type"] == "FUT":
            bucket["delta"] += d["qty"]
            continue

        if info["instrument_type"] not in ("CE", "PE"):
            skipped.append(d["symbol"])
            continue

        spot = spots.get(info["name"])
        if not spot:
            skipped.append(d["symbol"])
            continue

        expiry = info["expiry"].date() if isinstance(info["expiry"], datetime) else info["expiry"]
        T = max((expiry - date.today()).days, 0) / 365
        is_call = info["instrument_type"] == "CE"

        iv = _implied_vol(d["ltp"], spot, info["strike"], T, RISK_FREE_RATE, is_call)
        if iv is None:
            skipped.append(d["symbol"])
            continue

        greeks = _bs_greeks(spot, info["strike"], T, RISK_FREE_RATE, iv, is_call)
        bucket["delta"] += greeks["delta"] * d["qty"]
        bucket["theta"] += greeks["theta"] * d["qty"]
        bucket["vega"] += greeks["vega"] * d["qty"]

    return result, skipped


def format_greeks(per_underlying: dict, skipped: list) -> str:
    if not per_underlying and not skipped:
        return "No open positions."

    lines = ["📐 PORTFOLIO GREEKS"]
    total_delta = total_theta = total_vega = 0.0
    for name, g in per_underlying.items():
        lots = g["delta"] / g["lot_size"] if g["lot_size"] else 0.0
        lines.append(
            f"{name}: delta {g['delta']:,.0f} ({lots:+.1f} lots) | "
            f"theta Rs {g['theta']:,.0f}/day | vega Rs {g['vega']:,.0f} per 1% IV"
        )
        total_delta += g["delta"]
        total_theta += g["theta"]
        total_vega += g["vega"]

    if len(per_underlying) > 1:
        lines.append(
            f"Total: delta {total_delta:,.0f} | theta Rs {total_theta:,.0f}/day | vega Rs {total_vega:,.0f} per 1% IV"
        )

    if skipped:
        lines.append("")
        lines.append(f"Unmodeled (missing data): {', '.join(skipped)}")

    return "\n".join(lines)


# ---- Position tracking ----

class PositionTracker:
    """Holds current positions and live LTPs, computes P&L."""

    def __init__(self, kite: KiteConnect):
        self.kite = kite
        self.lock = threading.Lock()
        self.positions = {}       # instrument_token -> position dict
        self.ltp = {}             # instrument_token -> last traded price
        self.realized_pnl = 0.0          # realized P&L from positions closed earlier today
        self.last_milestone = None       # last Rs 5000 bucket that triggered an alert
        self.last_milestone_alert = 0.0  # timestamp of last milestone alert (cooldown)

        # Trailing profit-lock state
        self.trail_armed = False
        self.trail_peak = 0.0
        self.trail_exit_level = None
        self.trail_breached = False
        self.last_exit_alert = 0
        self.breach_since = None        # timestamp when breach started
        self.auto_exited = False        # only fire auto-exit once per breach
        self.last_peak_alert_time = 0.0     # timestamp of last new-peak notification
        self.last_peak_alert_level = 0.0    # peak value at last new-peak notification
        self.profit_target_hit = False   # only fire profit target exit once
        self.loss_warning_1_hit = False  # -20k warning
        self.loss_warning_2_hit = False  # -30k cut size warning
        self.loss_limit_hit = False      # -40k hard shutdown

        # Green day floor state
        self.green_day_armed = False    # True once P&L hits GREEN_DAY_ACTIVATION
        self.green_day_exited = False   # only fire once

        # Post-exit cool-off: set to a future timestamp by the loss-limit/profit-target/
        # trail-activation exit handlers; while now < cooloff_until, on_order_update and the
        # REST safety-net poll auto-square-off any new/added position instead of protecting
        # it with an SL. None means no cool-off is active.
        self.cooloff_until: datetime | None = None

        # Position size limit state
        self.oversize_since = {}        # symbol -> timestamp when it first exceeded MAX_POSITION_QTY
        self.last_oversize_alert = 0.0  # timestamp of last oversize alert (60s cooldown)

        # Session P&L range, for the EOD summary / history
        self.session_peak_pnl = 0.0
        self.session_trough_pnl = 0.0

        # Running {symbol: {"qty": signed int, "avg_cost": float}}, fed by on_order_update
        # fills — real-time position state (qty + blended cost) so the protective SL can be
        # resized to match on every fill, without waiting for the next REST position refresh.
        self.live_position = {}
        # Serializes SL cancel-then-replace so near-simultaneous fills on the same symbol
        # (e.g. one order split into several by the exchange) don't race each other.
        self.sl_order_lock = threading.Lock()

        # Auto-strangle state. strangle_symbols is checked by on_order_update to route
        # fills away from the legacy ATR/Turtle SL path — populated before any strangle
        # order is placed (see enter_strangle) so there's no window where an early fill
        # could slip through to the wrong handler.
        self.strangle_state = load_strangle_state()
        self.strangle_symbols: set[str] = set()
        self.strangle_lock = threading.Lock()
        self.strangle_manual_exit_order_ids: set[str] = set()
        self.strangle_eod_exit_order_ids: set[str] = set()

        self.refresh_positions()

    def refresh_positions(self):
        """
        Pull the latest open positions (day + net) from Kite REST API. Strangle-owned
        symbols are excluded entirely here — they're tracked independently via
        strangle_state, so they never contribute to total_pnl/details (compute_pnl
        reads from self.positions + self.realized_pnl), never get swept by the global
        loss-limit/trailing/profit-target/green-day exit checks (which act on whatever
        this method surfaces), and never re-enter the legacy ATR SL safety-net loop.
        """
        try:
            data = self.kite.positions()
            net = data.get("net", [])
            with self.strangle_lock:
                excluded = frozenset(self.strangle_symbols)
            with self.lock:
                self.positions = {
                    p["instrument_token"]: p for p in net
                    if p["quantity"] != 0 and p["tradingsymbol"] not in excluded
                }
                # Realized P&L from positions closed earlier today (strangle-owned excluded too)
                self.realized_pnl = sum(
                    float(p.get("pnl", 0)) for p in net
                    if p["quantity"] == 0 and p["tradingsymbol"] not in excluded
                )
            log.info(f"Refreshed positions: {len(self.positions)} open, realized P&L: {self.realized_pnl:.2f}")
        except Exception as e:
            log.error(f"Failed to refresh positions: {e}")

    def instrument_tokens(self):
        with self.lock:
            return list(self.positions.keys())

    def update_ticks(self, ticks):
        with self.lock:
            for t in ticks:
                self.ltp[t["instrument_token"]] = t["last_price"]

    def compute_pnl(self):
        """
        Returns (total_pnl, per_position_list)
        per_position_list: list of dicts with tradingsymbol, quantity, avg_price, ltp, pnl
        """
        with self.lock:
            total = self.realized_pnl  # start with realized P&L from fully closed positions
            details = []
            for token, pos in self.positions.items():
                qty = pos["quantity"]
                avg_price = pos["average_price"]
                live_ltp = self.ltp.get(token, pos.get("last_price", avg_price))
                kite_ltp = pos.get("last_price", avg_price)
                kite_pnl = float(pos.get("pnl", 0))
                # Use Kite's P&L as base (handles partial closes correctly)
                # then add live tick movement since last REST refresh
                pnl = kite_pnl + (live_ltp - kite_ltp) * qty
                total += pnl
                details.append({
                    "symbol": pos["tradingsymbol"],
                    "qty": qty,
                    "avg_price": avg_price,
                    "ltp": live_ltp,
                    "pnl": pnl,
                })
            return total, details


    def update_trailing_stop(self, total_pnl):
        """
        Returns an event string if something alert-worthy just happened:
          "armed"    -> trailing just activated
          "new_peak" -> peak (and therefore exit floor) moved up
          "breach"   -> P&L dropped to/below the exit floor (repeat-alert territory)
          "recovered"-> P&L moved back above the exit floor after a breach
          None       -> nothing alert-worthy
        """
        event = None

        if not self.trail_armed:
            if total_pnl >= TRAIL_ACTIVATION_THRESHOLD:
                self.trail_armed = True
                self.trail_peak = total_pnl
                self.trail_exit_level = self.trail_peak - _trail_drawdown(self.trail_peak)
                event = "armed"
            return event

        # Already armed — track new peaks
        if total_pnl > self.trail_peak:
            self.trail_peak = total_pnl
            new_exit_level = self.trail_peak - _trail_drawdown(self.trail_peak)
            if self.trail_exit_level is None or new_exit_level > self.trail_exit_level:
                self.trail_exit_level = new_exit_level
                if not self.trail_breached:
                    event = "new_peak"

        if total_pnl <= self.trail_exit_level:
            if not self.trail_breached:
                self.trail_breached = True
                event = "breach"
        else:
            if self.trail_breached:
                self.trail_breached = False
                event = "recovered"

        return event


def format_summary(total_pnl, details):
    lines = [f"P&L Update — {datetime.now().strftime('%H:%M:%S')}"]
    lines.append(f"Total: Rs {total_pnl:,.2f}")
    lines.append("")
    for d in sorted(details, key=lambda x: -abs(x["pnl"])):
        lines.append(f"{d['symbol']}: {d['qty']} @ {d['avg_price']:.2f} -> LTP {d['ltp']:.2f} | Rs {d['pnl']:,.2f}")
    return "\n".join(lines)


def format_status(tracker: "PositionTracker", total_pnl: float, details: list, strangle_pnl: float | None = None) -> str:
    """
    Full status: P&L, active safety-net levels, headroom, and pause/mute state.
    total_pnl/details are manual/other-positions only — strangle legs are tracked
    independently (see refresh_positions) and never enter this stream, so strangle_pnl
    is shown as its own separate line rather than folded into total_pnl.
    """
    now_ist = datetime.now(IST)
    close_dt = now_ist.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    remaining = (close_dt - now_ist).total_seconds()
    close_line = (
        "Market closed"
        if remaining <= 0
        else f"Time to close: {int(remaining // 3600)}h {int(remaining % 3600 // 60)}m"
    )

    lines = [f"📊 STATUS — {now_ist.strftime('%H:%M:%S')}"]
    lines.append(f"Manual/other P&L: Rs {total_pnl:,.2f}  |  Open positions: {len(details)}")
    if strangle_pnl is not None:
        lines.append(f"Strangle P&L: Rs {strangle_pnl:,.2f}  (see /strangle_status for detail)")
        lines.append(f"Combined P&L: Rs {total_pnl + strangle_pnl:,.2f}")
    lines.append(close_line)
    lines.append("")

    if tracker.trail_armed:
        cushion = total_pnl - tracker.trail_exit_level
        lines.append(
            f"Trailing lock: ARMED | peak Rs {tracker.trail_peak:,.2f} | "
            f"floor Rs {tracker.trail_exit_level:,.2f} | cushion Rs {cushion:,.2f}"
        )
    else:
        lines.append(f"Trailing lock: not armed (arms at Rs {TRAIL_ACTIVATION_THRESHOLD:,.0f})")

    if tracker.green_day_armed:
        state = "exited" if tracker.green_day_exited else "armed"
        lines.append(f"Green day floor: {state} at Rs {GREEN_DAY_FLOOR:,.0f}")

    lines.append(f"Loss limit: Rs {LOSS_LIMIT:,.0f}  |  headroom Rs {total_pnl - LOSS_LIMIT:,.2f}")
    if tracker.loss_warning_2_hit:
        lines.append("  status: warning 2 (-30k) fired — size cut suggested")
    elif tracker.loss_warning_1_hit:
        lines.append("  status: warning 1 (-20k) fired")

    lines.append(f"Profit target: Rs {PROFIT_TARGET:,.0f}" + (" — hit" if tracker.profit_target_hit else ""))

    if tracker.cooloff_until and now_ist < tracker.cooloff_until:
        remaining_min = int((tracker.cooloff_until - now_ist).total_seconds() // 60) + 1
        lines.append(
            f"Cool-off: ACTIVE until {tracker.cooloff_until.strftime('%H:%M:%S')} "
            f"({remaining_min}m left) — new positions auto-squared-off"
        )

    lines.append("")
    lines.append(f"Auto-exit: {'PAUSED' if PAUSE_FILE.exists() else 'enabled'}")
    lines.append(f"SL orders: {'PAUSED' if PAUSE_SL_FILE.exists() else 'enabled'}")
    lines.append(f"Notifications: {'MUTED' if MUTE_FILE.exists() else 'on'}")

    if details:
        lines.append("")
        for d in sorted(details, key=lambda x: -abs(x["pnl"])):
            lines.append(f"{d['symbol']}: {d['qty']} @ {d['avg_price']:.2f} -> LTP {d['ltp']:.2f} | Rs {d['pnl']:,.2f}")

    return "\n".join(lines)


def _compute_strangle_pnl(kite: KiteConnect, tracker: "PositionTracker") -> float | None:
    """
    Current combined strangle P&L (both legs) — unrealized for legs still open, realized
    for legs already closed today. Returns None if nothing's been entered today (so
    callers can distinguish "no strangle today" from "strangle is flat at Rs 0").
    """
    with tracker.strangle_lock:
        legs = {k: dict(v) for k, v in tracker.strangle_state["legs"].items()}

    if not any(l.get("tradingsymbol") for l in legs.values()):
        return None

    symbols = [l["tradingsymbol"] for l in legs.values() if l.get("tradingsymbol") and l["status"] == "open"]
    live_ltp = {}
    if symbols:
        try:
            ltp_data = kite.ltp([f"NFO:{s}" for s in symbols])
            live_ltp = {s: ltp_data.get(f"NFO:{s}", {}).get("last_price") for s in symbols}
        except Exception as exc:
            log.warning("Could not fetch live LTP for strangle P&L: %s", exc)

    total = 0.0
    for leg in legs.values():
        symbol = leg.get("tradingsymbol")
        if not symbol or leg.get("entry_price") is None or not leg.get("lot_size"):
            continue
        qty = leg["lot_size"] * STRANGLE_LOTS
        if leg["status"] == "open":
            cur = live_ltp.get(symbol)
            if cur is not None:
                total += (leg["entry_price"] - cur) * qty  # short leg: profit as price falls
        elif leg.get("exit_price") is not None:
            total += (leg["entry_price"] - leg["exit_price"]) * qty
    return total


def format_strangle_status(kite: KiteConnect, tracker: "PositionTracker") -> str:
    """Today's strangle: expiry/spot at entry, and per-leg symbol/entry/current LTP/SL/status."""
    with tracker.strangle_lock:
        state = dict(tracker.strangle_state)
        legs = {k: dict(v) for k, v in state["legs"].items()}

    if not state.get("entry_attempted"):
        msg = "📐 STRANGLE — not entered today yet."
        if state.get("skip_today"):
            msg += " (skip_today is set — today's entry will be skipped)"
        return msg

    lines = [f"📐 STRANGLE — {state['date']}"]
    spot = state.get("spot_at_entry")
    lines.append(f"Expiry: {state.get('expiry') or 'n/a'}" + (f"  |  Spot at entry: Rs {spot:.2f}" if spot else ""))

    symbols = [l["tradingsymbol"] for l in legs.values() if l.get("tradingsymbol")]
    live_ltp = {}
    if symbols:
        try:
            ltp_data = kite.ltp([f"NFO:{s}" for s in symbols])
            live_ltp = {s: ltp_data.get(f"NFO:{s}", {}).get("last_price") for s in symbols}
        except Exception as exc:
            log.warning("Could not fetch live LTP for strangle status: %s", exc)

    total_pnl = 0.0
    for leg_key, leg in legs.items():
        if not leg.get("tradingsymbol"):
            lines.append(f"{leg_key}: not resolved")
            continue
        cur = live_ltp.get(leg["tradingsymbol"])
        cur_str = f"Rs {cur:.2f}" if cur is not None else "n/a"
        entry_str = f"Rs {leg['entry_price']:.2f}" if leg.get("entry_price") is not None else "pending"
        sl_str = f"Rs {leg['sl_trigger']:.2f}" if leg.get("sl_trigger") is not None else "none"
        lines.append(f"{leg_key} {leg['tradingsymbol']}: entry {entry_str} | now {cur_str} | SL {sl_str} | {leg['status']}")
        qty = (leg.get("lot_size") or 0) * STRANGLE_LOTS
        if leg.get("entry_price") is not None and qty:
            if leg["status"] == "open" and cur is not None:
                total_pnl += (leg["entry_price"] - cur) * qty
            elif leg.get("exit_price") is not None:
                total_pnl += (leg["entry_price"] - leg["exit_price"]) * qty

    lines.append(f"Strangle P&L: Rs {total_pnl:,.2f}")
    lines.append("")
    entry_state = "completed" if state.get("entry_completed") else "incomplete"
    eod_state = "done" if state.get("eod_square_off_completed") else ("attempted" if state.get("eod_square_off_attempted") else "pending")
    lines.append(f"Entry: {entry_state}  |  EOD square-off: {eod_state}")
    lines.append(f"Auto-entry: {'PAUSED' if PAUSE_STRANGLE_FILE.exists() else 'enabled'}  |  SL multiplier: {STRANGLE_SL_MULTIPLIER}x")
    return "\n".join(lines)


# ---- Daily history / EOD summary ----

def _load_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("History file unreadable — starting fresh.")
        return []


def append_history(tracker: "PositionTracker", total_pnl: float) -> dict:
    """Appends (or replaces, if already recorded today) today's summary to HISTORY_FILE."""
    today = date.today().isoformat()
    record = {
        "date": today,
        "final_pnl": round(total_pnl, 2),
        "peak_pnl": round(tracker.session_peak_pnl, 2),
        "trough_pnl": round(tracker.session_trough_pnl, 2),
        "trail_armed": tracker.trail_armed,
        "trail_peak": round(tracker.trail_peak, 2) if tracker.trail_armed else None,
        "green_day_armed": tracker.green_day_armed,
        "loss_limit_hit": tracker.loss_limit_hit,
        "profit_target_hit": tracker.profit_target_hit,
        "auto_exit_fired": bool(
            tracker.auto_exited or tracker.green_day_exited
            or tracker.profit_target_hit or tracker.loss_limit_hit
        ),
    }

    history = [h for h in _load_history() if h.get("date") != today]
    history.append(record)
    try:
        HISTORY_FILE.write_text(json.dumps(history, indent=2))
    except OSError as exc:
        log.error("Could not write history file: %s", exc)

    return record


def format_eod_summary(record: dict) -> str:
    lines = [f"📅 EOD SUMMARY — {record['date']}"]
    lines.append(f"Final P&L: Rs {record['final_pnl']:,.2f}")
    lines.append(f"Peak: Rs {record['peak_pnl']:,.2f}  |  Trough: Rs {record['trough_pnl']:,.2f}")

    giveback = record["peak_pnl"] - record["final_pnl"]
    if giveback > 0:
        lines.append(f"Gave back Rs {giveback:,.2f} from the day's peak")

    if record["trail_armed"]:
        lines.append(f"Trailing lock was armed (peak Rs {record['trail_peak']:,.2f})")
    if record["auto_exit_fired"]:
        lines.append("⚠️ An auto-exit fired today")

    return "\n".join(lines)


def format_history(history: list, n: int = 7) -> str:
    if not history:
        return "No history yet."

    lines = [f"📅 P&L HISTORY (last {min(n, len(history))} day(s))"]
    for rec in history[-n:]:
        flag = " ⚠️exit" if rec.get("auto_exit_fired") else ""
        lines.append(f"{rec['date']}: Rs {rec['final_pnl']:,.2f} (peak Rs {rec['peak_pnl']:,.2f}){flag}")

    return "\n".join(lines)


# ---- Live-tunable thresholds via Telegram ----

SETTABLE_PARAMS: dict[str, tuple[str, type]] = {
    "loss_warning_1": ("LOSS_WARNING_1", float),
    "loss_warning_2": ("LOSS_WARNING_2", float),
    "loss_limit": ("LOSS_LIMIT", float),
    "profit_target": ("PROFIT_TARGET", float),
    "trail_activation": ("TRAIL_ACTIVATION_THRESHOLD", float),
    "green_day_activation": ("GREEN_DAY_ACTIVATION", float),
    "green_day_floor": ("GREEN_DAY_FLOOR", float),
    "milestone_step": ("MILESTONE_STEP", float),
    "max_position_qty": ("MAX_POSITION_QTY", int),
    "hedge_price_threshold": ("HEDGE_PRICE_THRESHOLD", float),
    "exit_buffer_seconds": ("EXIT_BUFFER_SECONDS", int),
    "strangle_sl_multiplier": ("STRANGLE_SL_MULTIPLIER", float),
    "strangle_lots": ("STRANGLE_LOTS", int),
    "cooloff_minutes": ("COOLOFF_MINUTES", int),
    "atr_base_multiplier": ("ATR_BASE_MULTIPLIER", float),
    "atr_reference_vix": ("ATR_REFERENCE_VIX", float),
    "atr_vix_sensitivity": ("ATR_VIX_SENSITIVITY", float),
    "atr_min_multiplier": ("ATR_MIN_MULTIPLIER", float),
    "atr_max_multiplier": ("ATR_MAX_MULTIPLIER", float),
}


def _handle_set_command(text: str, reply) -> None:
    """Handles '/set' (list current values) and '/set <param> <value>' (update one live)."""
    parts = text.split()

    if len(parts) == 1:
        lines = [f"{key} = {globals()[var_name]}" for key, (var_name, _) in SETTABLE_PARAMS.items()]
        reply("Current settings (/set <param> <value> to change):\n" + "\n".join(lines))
        return

    if len(parts) != 3:
        reply("Usage: /set <param> <value>\nSend /set with no args to list params.")
        return

    _, key, raw_value = parts
    if key not in SETTABLE_PARAMS:
        reply(f"Unknown param '{key}'.\nAvailable: {', '.join(SETTABLE_PARAMS)}")
        return

    var_name, caster = SETTABLE_PARAMS[key]
    try:
        value = caster(raw_value)
    except ValueError:
        reply(f"Invalid value '{raw_value}' for {key} (expected {caster.__name__}).")
        return

    globals()[var_name] = value
    reply(f"✅ {var_name} = {value}")
    log.info("Updated %s to %s via Telegram /set.", var_name, value)


# ---- Telegram command listener ----

def _telegram_command_listener(tracker_ref: list, kite_ref: list):
    """
    Background thread that polls Telegram for commands and acts on them.
    tracker_ref/kite_ref are one-element lists so we can access objects created after this thread starts.
    Supported commands:
      /pause      — disable auto-exit (creates pause file)
      /resume     — enable auto-exit (removes pause file)
      /pause_sl   — stop placing/resizing protective SL orders (creates pause file)
      /resume_sl  — resume placing protective SL orders (removes pause file)
      /strangle_status  — today's auto-strangle: legs, entry/current price, SL, status
      /skip_strangle    — skip today's strangle entry (only before it's been attempted)
      /close_strangle   — square off any open strangle leg right now
      /pause_strangle   — stop future days' strangle auto-entry (creates pause file)
      /resume_strangle  — resume strangle auto-entry (removes pause file)
      /status     — send current P&L snapshot
      /greeks     — send portfolio delta/theta/vega
    """
    url_base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    offset = 0

    def reply(text: str):
        try:
            requests.post(f"{url_base}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        except Exception:
            pass

    while True:
        try:
            resp = requests.get(
                f"{url_base}/getUpdates",
                params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                timeout=35,
            )
            if not resp.ok:
                time.sleep(5)
                continue
            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip().lower()
                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue
                if text == "/pause":
                    PAUSE_FILE.touch()
                    reply("⏸ Auto-exit PAUSED. Send /resume to re-enable.")
                    log.info("Auto-exit paused via Telegram.")
                elif text == "/resume":
                    PAUSE_FILE.unlink(missing_ok=True)
                    reply("▶️ Auto-exit RESUMED.")
                    log.info("Auto-exit resumed via Telegram.")
                elif text == "/pause_sl":
                    PAUSE_SL_FILE.touch()
                    reply("⏸ SL-order placement PAUSED. Send /resume_sl to re-enable.")
                    log.info("SL-order placement paused via Telegram.")
                elif text == "/resume_sl":
                    PAUSE_SL_FILE.unlink(missing_ok=True)
                    reply("▶️ SL-order placement RESUMED.")
                    log.info("SL-order placement resumed via Telegram.")
                elif text == "/strangle_status":
                    tracker = tracker_ref[0] if tracker_ref else None
                    kite = kite_ref[0] if kite_ref else None
                    if tracker and kite:
                        reply(format_strangle_status(kite, tracker))
                    else:
                        reply("Monitor not ready yet.")
                elif text == "/skip_strangle":
                    tracker = tracker_ref[0] if tracker_ref else None
                    if not tracker:
                        reply("Monitor not ready yet.")
                    elif tracker.strangle_state.get("entry_attempted"):
                        reply("Too late — today's entry was already attempted. Use /close_strangle to exit early instead.")
                    else:
                        with tracker.strangle_lock:
                            tracker.strangle_state["skip_today"] = True
                            save_strangle_state(tracker.strangle_state)
                        reply("⏭ Today's strangle entry will be skipped.")
                        log.info("Strangle entry skipped for today via Telegram.")
                elif text == "/close_strangle":
                    tracker = tracker_ref[0] if tracker_ref else None
                    kite = kite_ref[0] if kite_ref else None
                    if tracker and kite:
                        reply("Closing any open strangle legs...")
                        threading.Thread(target=lambda: square_off_strangle(kite, tracker, "manual"), daemon=True).start()
                    else:
                        reply("Monitor not ready yet.")
                elif text == "/pause_strangle":
                    PAUSE_STRANGLE_FILE.touch()
                    reply("⏸ Strangle auto-entry PAUSED for future days. Send /resume_strangle to re-enable.")
                    log.info("Strangle auto-entry paused via Telegram.")
                elif text == "/resume_strangle":
                    PAUSE_STRANGLE_FILE.unlink(missing_ok=True)
                    reply("▶️ Strangle auto-entry RESUMED.")
                    log.info("Strangle auto-entry resumed via Telegram.")
                elif text == "/status":
                    tracker = tracker_ref[0] if tracker_ref else None
                    kite = kite_ref[0] if kite_ref else None
                    if tracker:
                        total_pnl, details = tracker.compute_pnl()
                        strangle_pnl = _compute_strangle_pnl(kite, tracker) if kite else None
                        reply(format_status(tracker, total_pnl, details, strangle_pnl))
                    else:
                        reply("Monitor not ready yet.")
                elif text == "/set" or text.startswith("/set "):
                    _handle_set_command(text, reply)
                elif text == "/greeks":
                    tracker = tracker_ref[0] if tracker_ref else None
                    kite = kite_ref[0] if kite_ref else None
                    if tracker and kite:
                        _, details = tracker.compute_pnl()
                        per_underlying, skipped = compute_portfolio_greeks(kite, details)
                        reply(format_greeks(per_underlying, skipped))
                    else:
                        reply("Monitor not ready yet.")
                elif text == "/history":
                    reply(format_history(_load_history()))
                elif text == "/stop":
                    MUTE_FILE.touch()
                    reply("🔕 Notifications muted. Monitor is still running. Send /start to resume.")
                    log.info("Notifications muted via Telegram /stop.")
                elif text == "/start":
                    MUTE_FILE.unlink(missing_ok=True)
                    reply("🔔 Notifications resumed.")
                    log.info("Notifications resumed via Telegram /start.")
                elif text == "/help":
                    reply(
                        "/stop — mute all notifications\n"
                        "/start — resume notifications\n"
                        "/pause — disable auto-exit\n"
                        "/resume — enable auto-exit\n"
                        "/pause_sl — stop placing protective SL orders\n"
                        "/resume_sl — resume placing protective SL orders\n"
                        "/strangle_status — today's auto-strangle: legs, entry/current price, SL, status\n"
                        "/skip_strangle — skip today's strangle entry (before it's been attempted)\n"
                        "/close_strangle — square off any open strangle leg right now\n"
                        "/pause_strangle — stop future days' strangle auto-entry\n"
                        "/resume_strangle — resume strangle auto-entry\n"
                        "/status — full status (P&L, floors, headroom, pause/mute state)\n"
                        "/greeks — portfolio delta/theta/vega by underlying\n"
                        "/history — last 7 days' EOD P&L summary\n"
                        "/set — list tunable thresholds\n"
                        "/set <param> <value> — change a threshold live, e.g. /set loss_limit -50000"
                    )
        except Exception as exc:
            log.warning("Telegram command listener error: %s", exc)
            time.sleep(5)


# ---- Main ----

def main():
    log.info("P&L monitor (WebSocket) started.")

    try:
        access_token = _read_access_token()
    except RuntimeError as exc:
        log.error(str(exc))
        notify("🔑 Access token error", f"{exc}\nRun generate_token.py and restart the service.", priority="high")
        return

    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(access_token)

    tracker = PositionTracker(kite)
    reconcile_strangle_state_on_startup(kite, tracker)
    tracker.refresh_positions()  # re-apply the strangle exclusion now that strangle_symbols is populated

    # Start Telegram command listener in background — before the market-open
    # wait below, so /status etc. respond even in the pre-market window.
    tracker_ref = [tracker]
    kite_ref = [kite]
    cmd_thread = threading.Thread(target=_telegram_command_listener, args=(tracker_ref, kite_ref), daemon=True)
    cmd_thread.start()
    log.info("Telegram command listener started.")

    # Wait until market opens if we're launched early
    while not is_market_open():
        log.info("Market closed. Sleeping 60 s.")
        time.sleep(60)

    if not tracker.instrument_tokens():
        log.warning("No open positions found. The script will keep watching and "
                     "re-check positions periodically — open a trade and it'll pick it up.")

    kws = KiteTicker(API_KEY, access_token)

    def on_connect(ws, response):
        tokens = tracker.instrument_tokens()
        if tokens:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
        log.info(f"WebSocket connected. Subscribed to {len(tokens)} instruments.")
        total_pnl, _ = tracker.compute_pnl()
        notify(
            "📡 P&L Monitor started",
            f"Watching {len(tokens)} open position(s)\nCurrent P&L: Rs {total_pnl:,.2f}",
        )

    def on_ticks(ws, ticks):
        tracker.update_ticks(ticks)

    def on_close(ws, code, reason):
        log.warning(f"WebSocket closed: {code} {reason}")

    def on_error(ws, code, reason):
        log.error(f"WebSocket error: {code} {reason}")

    def on_reconnect(ws, attempts_count):
        log.info(f"WebSocket reconnecting, attempt {attempts_count}")

    def on_noreconnect(ws):
        log.error("WebSocket gave up reconnecting.")
        notify("⚠️ Connection lost", "WebSocket could not reconnect. Please check the server.", priority="high")

    def on_order_update(ws, data):
        """
        Fires within ~seconds of any of the account's orders completing — the fast path for
        keeping the protective SL in sync with the position, instead of waiting up to 2 min
        for the next REST position refresh. Every fill that leaves a nonzero position (a
        fresh open, adding more, or a partial reduction) gets the SL resized to match the
        new quantity and blended average cost. A fill that flattens the position cancels any
        leftover SL instead, so a stale order can't misfire into an unintended new position.
        """
        try:
            if data.get("status") != "COMPLETE":
                return
            exchange = data.get("exchange")
            symbol = data.get("tradingsymbol")
            qty = int(data.get("quantity") or 0)
            txn = data.get("transaction_type")
            avg_price = float(data.get("average_price") or 0)
            product = data.get("product")
            try:
                token = int(data.get("instrument_token"))
            except (TypeError, ValueError):
                token = None
            if exchange != "NFO" or not symbol or qty <= 0 or not token:
                return

            # Strangle-owned fills never touch the legacy ATR/Turtle SL path below —
            # they're routed to their own handler with their own premium-multiple SL.
            # A symbol stays in strangle_symbols for the rest of the day even after the
            # leg closes, so a stray late duplicate fill can't fall through here either.
            if symbol in tracker.strangle_symbols:
                threading.Thread(target=_on_strangle_leg_filled, args=(kite, tracker, data), daemon=True).start()
                return

            d = qty if txn == "BUY" else -qty
            with tracker.lock:
                prev = tracker.live_position.get(symbol, {"qty": 0, "avg_cost": 0.0, "stop_anchor": 0.0})
                prev_qty, prev_avg = prev["qty"], prev["avg_cost"]

                if prev_qty == 0:
                    # fresh open — anchor is this fill's own price
                    new_qty, new_avg, new_anchor = d, avg_price, avg_price
                elif (prev_qty > 0) == (d > 0):
                    # adding to the same side (pyramiding) — anchor ratchets to the latest add,
                    # Turtle-style, instead of loosening back to the blended average
                    new_qty = prev_qty + d
                    new_avg = (prev_qty * prev_avg + d * avg_price) / new_qty
                    new_anchor = avg_price
                elif abs(d) <= abs(prev_qty):
                    # partial (or exact full) reduction — cost basis and anchor are unchanged
                    new_qty = prev_qty + d
                    new_avg = prev_avg
                    new_anchor = prev["stop_anchor"]
                else:
                    # reversed past flat — a fresh position in the new direction
                    new_qty = prev_qty + d
                    new_avg = avg_price
                    new_anchor = avg_price

                tracker.live_position[symbol] = {"qty": new_qty, "avg_cost": new_avg, "stop_anchor": new_anchor}

            if new_qty == 0:
                # fully flat — clear any leftover SL so it can't later misfire into a fresh position
                threading.Thread(target=_cancel_open_orders, args=(kite, [symbol]), daemon=True).start()
                return

            # Cool-off enforcement — a fill that INCREASES exposure (fresh open or adding to an
            # existing side) during the post-exit cool-off window gets squared off immediately
            # instead of protected with an SL. A pure reduction is left alone (falls through to
            # the normal SL-resize path below) since it's derisking, not a new order.
            if _cooloff_active(tracker) and abs(new_qty) > abs(prev_qty):
                log.warning("Cool-off violation: %s (qty %d during cool-off) — auto squaring off.", symbol, new_qty)

                def _enforce_cooloff(sym=symbol):
                    try:
                        exited, skipped, failed = exit_non_hedge_positions(kite, frozenset(tracker.strangle_symbols))
                        lines = []
                        if exited:
                            lines.append(f"Exited: {', '.join(exited)}")
                        if failed:
                            lines.append(f"FAILED: {', '.join(failed)}")
                        notify(
                            f"🚫 Cool-off violation — {sym}",
                            "\n".join(lines) if lines else "No positions to exit.",
                            priority="urgent",
                        )
                    except Exception as exc:
                        log.error("Cool-off enforcement failed: %s", exc)
                        notify("🚫 Cool-off enforcement ERROR", str(exc), priority="urgent")

                threading.Thread(target=_enforce_cooloff, daemon=True).start()
                return

            pos = {
                "tradingsymbol": symbol,
                "quantity": new_qty,
                "average_price": new_avg,
                "stop_anchor": new_anchor,
                "last_price": avg_price,
                "exchange": exchange,
                "product": product,
            }
            threading.Thread(target=_ensure_sl_order, args=(kite, tracker, token, pos), daemon=True).start()
        except Exception as exc:
            log.warning("on_order_update handling failed: %s", exc)

    kws.on_connect = on_connect
    kws.on_ticks = on_ticks
    kws.on_close = on_close
    kws.on_error = on_error
    kws.on_reconnect = on_reconnect
    kws.on_noreconnect = on_noreconnect
    kws.on_order_update = on_order_update

    # Run the ticker in a background thread so we can run our own alert loop here
    kws.connect(threaded=True)

    last_position_refresh = time.time()
    last_trailing_heartbeat = 0
    last_heartbeat = time.time()
    eod_summary_sent = False

    try:
        while True:
            now = time.time()

            # Periodically re-check for new/closed positions (every 2 min)
            if now - last_position_refresh > 120:
                old_tokens = set(tracker.instrument_tokens())
                tracker.refresh_positions()
                new_tokens = set(tracker.instrument_tokens())
                if new_tokens != old_tokens:
                    kws.subscribe(list(new_tokens))
                    kws.set_mode(kws.MODE_FULL, list(new_tokens))
                    log.info("Position set changed — re-subscribed.")
                    # SL protection for newly opened non-hedge positions — this REST poll is a
                    # 2-min-lagging safety net; on_order_update below is the fast path that
                    # normally already handles it (sl_placed_symbols dedupes the two). Same
                    # lagging safety net applies to cool-off enforcement: on_order_update is the
                    # fast path there too, this just catches anything it missed.
                    added_tokens = new_tokens - old_tokens
                    if added_tokens:
                        with tracker.lock:
                            new_pos = {t: tracker.positions[t] for t in added_tokens if t in tracker.positions}
                        if _cooloff_active(tracker) and new_pos:
                            log.warning("Cool-off violation caught by REST safety net: %s — auto squaring off.",
                                        ", ".join(p["tradingsymbol"] for p in new_pos.values()))
                            try:
                                exited, skipped, failed = exit_non_hedge_positions(kite, frozenset(tracker.strangle_symbols))
                                lines = []
                                if exited:
                                    lines.append(f"Exited: {', '.join(exited)}")
                                if failed:
                                    lines.append(f"FAILED: {', '.join(failed)}")
                                notify(
                                    "🚫 Cool-off violation (REST safety net)",
                                    "\n".join(lines) if lines else "No positions to exit.",
                                    priority="urgent",
                                )
                            except Exception as exc:
                                log.error("Cool-off enforcement failed: %s", exc)
                                notify("🚫 Cool-off enforcement ERROR", str(exc), priority="urgent")
                        else:
                            for token, pos in new_pos.items():
                                _ensure_sl_order(kite, tracker, token, pos)
                last_position_refresh = now

            total_pnl, details = tracker.compute_pnl()
            tracker.session_peak_pnl = max(tracker.session_peak_pnl, total_pnl)
            tracker.session_trough_pnl = min(tracker.session_trough_pnl, total_pnl)

            # ---- Strangle entry (9:20-9:35 window) and 3pm square-off ----
            now_ist = datetime.now(IST)
            strangle_entry_start = now_ist.replace(hour=STRANGLE_ENTRY_TIME[0], minute=STRANGLE_ENTRY_TIME[1], second=0, microsecond=0)
            strangle_entry_cutoff = now_ist.replace(hour=STRANGLE_ENTRY_CUTOFF[0], minute=STRANGLE_ENTRY_CUTOFF[1], second=0, microsecond=0)
            strangle_exit_time = now_ist.replace(hour=STRANGLE_EXIT_TIME[0], minute=STRANGLE_EXIT_TIME[1], second=0, microsecond=0)

            if (strangle_entries_enabled()
                    and not is_nse_holiday(now_ist.date())
                    and strangle_entry_start <= now_ist < strangle_entry_cutoff
                    and not tracker.strangle_state.get("entry_attempted")
                    and not tracker.strangle_state.get("skip_today")):
                tracker.strangle_state["entry_attempted"] = True
                save_strangle_state(tracker.strangle_state)  # persist BEFORE dispatch — avoids a double-fire next tick
                threading.Thread(target=enter_strangle, args=(kite, tracker), daemon=True).start()

            if (now_ist >= strangle_exit_time
                    and tracker.strangle_state.get("entry_completed")
                    and not tracker.strangle_state.get("eod_square_off_attempted")):
                tracker.strangle_state["eod_square_off_attempted"] = True
                save_strangle_state(tracker.strangle_state)
                threading.Thread(target=lambda: square_off_strangle(kite, tracker, "eod"), daemon=True).start()

            # ---- EOD summary: fires once, the first loop iteration after market close ----
            if not eod_summary_sent and not is_market_open():
                record = append_history(tracker, total_pnl)
                notify("📅 EOD Summary", format_eod_summary(record))
                log.info("EOD summary recorded: %s", record)
                eod_summary_sent = True

            # ---- Position size limit check ----
            breaching = [
                d for d in details
                if abs(d["qty"]) > MAX_POSITION_QTY and d["ltp"] >= HEDGE_PRICE_THRESHOLD
            ]
            # Track when each symbol first breached
            breaching_symbols = {d["symbol"] for d in breaching}
            for d in breaching:
                if d["symbol"] not in tracker.oversize_since:
                    tracker.oversize_since[d["symbol"]] = now
            # Clear symbols that are no longer breaching
            for sym in list(tracker.oversize_since):
                if sym not in breaching_symbols:
                    del tracker.oversize_since[sym]
            # Alert every 60s while any position is breaching
            if breaching and now - tracker.last_oversize_alert >= 60:
                tracker.last_oversize_alert = now
                lines = []
                for d in breaching:
                    elapsed = int((now - tracker.oversize_since[d["symbol"]]) / 60)
                    lines.append(f"{d['symbol']}: qty {abs(d['qty'])} — {elapsed} min exceeded")
                notify(
                    f"⚠️ Max lot size exceeded (limit: {MAX_POSITION_QTY})",
                    "\n".join(lines),
                    priority="high",
                )

            # ---- Trailing profit-lock (the important one) ----
            event = tracker.update_trailing_stop(total_pnl)

            if event == "armed":
                tracker.last_peak_alert_level = tracker.trail_peak
                tracker.last_peak_alert_time = now
                drawdown = _trail_drawdown(tracker.trail_peak)
                notify(
                    "🔒 Trailing lock armed",
                    f"P&L hit Rs {total_pnl:,.2f}\nExit floor: Rs {tracker.trail_exit_level:,.2f} (Rs {drawdown:,.0f} drawdown)",
                    priority="high",
                )
            elif event == "new_peak":
                peak_move = tracker.trail_peak - tracker.last_peak_alert_level
                time_since = now - tracker.last_peak_alert_time
                if peak_move >= 2_000 or time_since >= 120:
                    drawdown = _trail_drawdown(tracker.trail_peak)
                    notify(
                        f"📈 New peak: Rs {tracker.trail_peak:,.2f}",
                        f"Exit floor raised to Rs {tracker.trail_exit_level:,.2f} (Rs {drawdown:,.0f} drawdown)",
                    )
                    tracker.last_peak_alert_level = tracker.trail_peak
                    tracker.last_peak_alert_time = now
            elif event == "breach":
                tracker.breach_since = now
                tracker.auto_exited = False
                buffer_msg = f"Exiting in {EXIT_BUFFER_SECONDS}s if not recovered." if auto_exit_enabled() else "Auto-exit PAUSED."
                notify(
                    "🚨 Trailing floor breached",
                    f"P&L Rs {total_pnl:,.2f} hit floor Rs {tracker.trail_exit_level:,.2f}\n{buffer_msg}",
                    priority="urgent",
                )
                tracker.last_exit_alert = now
            elif event == "recovered":
                tracker.breach_since = None
                tracker.auto_exited = False
                notify(
                    "✅ Back above exit floor",
                    f"P&L Rs {total_pnl:,.2f} (floor Rs {tracker.trail_exit_level:,.2f})",
                )

            # Repeat the EXIT NOW alert while still breached, so it can't be missed
            if tracker.trail_breached and now - tracker.last_exit_alert >= EXIT_ALERT_REPEAT_SECONDS:
                seconds_left = max(0, EXIT_BUFFER_SECONDS - int(now - (tracker.breach_since or now)))
                msg = (f"Auto-exiting in {seconds_left}s..." if AUTO_EXIT and seconds_left > 0
                       else "Positions being exited." if AUTO_EXIT
                       else "EXIT NOW.")
                notify(
                    "🚨 Still below exit floor",
                    f"P&L Rs {total_pnl:,.2f} vs floor Rs {tracker.trail_exit_level:,.2f}. {msg}",
                    priority="urgent",
                )
                tracker.last_exit_alert = now

            # ---- Auto-exit after buffer period ----
            if (auto_exit_enabled()
                    and tracker.trail_breached
                    and not tracker.auto_exited
                    and tracker.breach_since is not None
                    and now - tracker.breach_since >= EXIT_BUFFER_SECONDS):
                tracker.auto_exited = True
                log.info("Auto-exit triggered after %ds breach.", EXIT_BUFFER_SECONDS)
                try:
                    exited, skipped, failed = exit_non_hedge_positions(kite, frozenset(tracker.strangle_symbols))
                    cooloff_until = _start_cooloff(tracker)
                    lines = ["🔴 AUTO-EXIT EXECUTED"]
                    if exited:
                        lines.append(f"Exited: {', '.join(exited)}")
                    if skipped:
                        lines.append(f"Kept (hedge): {', '.join(skipped)}")
                    if failed:
                        lines.append(f"FAILED: {', '.join(failed)}")
                    lines.append(f"Cool-off: new positions auto-squared-off until {cooloff_until.strftime('%H:%M:%S')}")
                    notify("🔴 Auto-exit executed", "\n".join(lines[1:]), priority="urgent")
                except Exception as exc:
                    log.error("Auto-exit failed: %s", exc)
                    notify("🔴 Auto-exit ERROR", str(exc), priority="urgent")

            # ---- Green day floor ----
            if not tracker.trail_armed:
                if not tracker.green_day_armed and total_pnl >= GREEN_DAY_ACTIVATION:
                    tracker.green_day_armed = True
                    log.info("Green day floor armed at Rs %.0f.", GREEN_DAY_FLOOR)
                    notify(
                        "🟢 Green day floor armed",
                        f"P&L hit Rs {total_pnl:,.2f} — floor locked at Rs {GREEN_DAY_FLOOR:,.0f}",
                        priority="high",
                    )
                if (auto_exit_enabled()
                        and tracker.green_day_armed
                        and not tracker.green_day_exited
                        and total_pnl <= GREEN_DAY_FLOOR):
                    tracker.green_day_exited = True
                    log.info("Green day floor Rs %.0f breached — auto-exiting.", GREEN_DAY_FLOOR)
                    try:
                        exited, skipped, failed = exit_non_hedge_positions(kite, frozenset(tracker.strangle_symbols))
                        lines = []
                        if exited:
                            lines.append(f"Exited: {', '.join(exited)}")
                        if skipped:
                            lines.append(f"Kept (hedge): {', '.join(skipped)}")
                        if failed:
                            lines.append(f"FAILED: {', '.join(failed)}")
                        notify(
                            f"🟢 Green day floor Rs {GREEN_DAY_FLOOR:,.0f} breached — exited",
                            "\n".join(lines) if lines else "No positions to exit.",
                            priority="urgent",
                        )
                    except Exception as exc:
                        log.error("Green day exit failed: %s", exc)
                        notify("🟢 Green day exit ERROR", str(exc), priority="urgent")

            # ---- Profit target exit ----
            if (not tracker.profit_target_hit
                    and total_pnl >= PROFIT_TARGET):
                tracker.profit_target_hit = True
                log.info("Profit target Rs %.0f hit — auto-exiting.", PROFIT_TARGET)
                try:
                    exited, skipped, failed = exit_non_hedge_positions(kite, frozenset(tracker.strangle_symbols))
                    cooloff_until = _start_cooloff(tracker)
                    lines = []
                    if exited:
                        lines.append(f"Exited: {', '.join(exited)}")
                    if skipped:
                        lines.append(f"Kept (hedge): {', '.join(skipped)}")
                    if failed:
                        lines.append(f"FAILED: {', '.join(failed)}")
                    lines.append(f"Cool-off: new positions auto-squared-off until {cooloff_until.strftime('%H:%M:%S')}")
                    notify(
                        f"🎯 Profit target Rs {PROFIT_TARGET:,.0f} hit — exited",
                        "\n".join(lines) if lines else "No positions to exit.",
                        priority="high",
                    )
                except Exception as exc:
                    log.error("Profit target exit failed: %s", exc)
                    notify("🎯 Profit target exit ERROR", str(exc), priority="urgent")

            # ---- Loss limit exit ----
            # ---- Loss warning 1: -20k ----
            if not tracker.loss_warning_1_hit and total_pnl <= LOSS_WARNING_1:
                tracker.loss_warning_1_hit = True
                log.info("Loss warning 1 at Rs %.0f.", total_pnl)
                notify(
                    "⚠️ Loss warning — Rs 20k down",
                    f"P&L is Rs {total_pnl:,.2f}. Stay cautious.",
                    priority="high",
                )

            # ---- Loss warning 2: -30k ----
            if not tracker.loss_warning_2_hit and total_pnl <= LOSS_WARNING_2:
                tracker.loss_warning_2_hit = True
                log.info("Loss warning 2 at Rs %.0f — cut position size.", total_pnl)
                notify(
                    "🔴 Loss at Rs 30k — cut position size by 50%",
                    f"P&L is Rs {total_pnl:,.2f}. Hard shutdown at Rs {LOSS_LIMIT:,.0f}.",
                    priority="urgent",
                )

            # ---- Loss limit: -40k hard shutdown ----
            if (not tracker.loss_limit_hit
                    and total_pnl <= LOSS_LIMIT):
                tracker.loss_limit_hit = True
                log.info("Loss limit Rs %.0f hit — auto-exiting.", LOSS_LIMIT)
                try:
                    exited, skipped, failed = exit_non_hedge_positions(kite, frozenset(tracker.strangle_symbols))
                    cooloff_until = _start_cooloff(tracker)
                    lines = []
                    if exited:
                        lines.append(f"Exited: {', '.join(exited)}")
                    if skipped:
                        lines.append(f"Kept (hedge): {', '.join(skipped)}")
                    if failed:
                        lines.append(f"FAILED: {', '.join(failed)}")
                    lines.append(f"Cool-off: new positions auto-squared-off until {cooloff_until.strftime('%H:%M:%S')}")
                    notify(
                        f"🛑 Loss limit Rs {LOSS_LIMIT:,.0f} hit — exited",
                        "\n".join(lines) if lines else "No positions to exit.",
                        priority="urgent",
                    )
                except Exception as exc:
                    log.error("Loss limit exit failed: %s", exc)
                    notify("🛑 Loss limit exit ERROR", str(exc), priority="urgent")

            # ---- Liveness heartbeat: proves the monitor is still alive even when nothing crosses a threshold ----
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                notify(
                    "💓 Heartbeat",
                    f"Monitor alive. Positions: {len(details)} | P&L: Rs {total_pnl:,.2f}",
                )
                last_heartbeat = now

            # ---- Trailing heartbeat: every 60 s once armed ----
            if tracker.trail_armed and now - last_trailing_heartbeat >= 60:
                notify("📊 P&L Update", format_summary(total_pnl, details))
                last_trailing_heartbeat = now

            # ---- Milestone alerts (every Rs 5000 step, until trailing takes over) ----
            if not tracker.trail_armed:
                milestone = math.floor(total_pnl / MILESTONE_STEP) * MILESTONE_STEP
                if (milestone != tracker.last_milestone
                        and now - tracker.last_milestone_alert >= 60):
                    tracker.last_milestone = milestone
                    tracker.last_milestone_alert = now
                    notify("📊 P&L Milestone", format_summary(total_pnl, details))

            time.sleep(TRAIL_CHECK_INTERVAL)

    except KeyboardInterrupt:
        log.info("Stopping...")
        kws.close()


if __name__ == "__main__":
    main()
