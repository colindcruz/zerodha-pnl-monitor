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

import math
import os
import time
import logging
import threading
from datetime import datetime
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

# ---- Auto-exit on trailing breach ----
AUTO_EXIT               = os.getenv("AUTO_EXIT", "true").lower() == "true"
HEDGE_PRICE_THRESHOLD   = float(os.getenv("HEDGE_PRICE_THRESHOLD", "5.0"))  # positions with LTP below this are kept
EXIT_BUFFER_SECONDS     = int(os.getenv("EXIT_BUFFER_SECONDS", "30"))  # wait this long below floor before exiting

# ---- Profit target exit ----
PROFIT_TARGET           = float(os.getenv("PROFIT_TARGET", "80000"))   # exit all non-hedge positions when P&L hits this

# ---- Loss warnings + hard limit ----
LOSS_WARNING_1          = float(os.getenv("LOSS_WARNING_1", "-20000"))  # warning alert
LOSS_WARNING_2          = float(os.getenv("LOSS_WARNING_2", "-30000"))  # cut position size alert
LOSS_LIMIT              = float(os.getenv("LOSS_LIMIT", "-40000"))      # hard shutdown — exit all

# ---- Position size limit ----
MAX_POSITION_QTY        = int(os.getenv("MAX_POSITION_QTY", "1950"))    # max qty per individual non-hedge position

LOG_FILE = os.getenv("LOG_FILE", "pnl_monitor.log")

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN  = (9, 15)
MARKET_CLOSE = (15, 30)

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


def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t <= MARKET_CLOSE


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
    """Send to both Telegram and ntfy."""
    send_telegram(f"{title}\n{body}" if title else body)
    send_ntfy(title, body, priority)


# ---- Auto-exit helper ----

FREEZE_QTY_LIMIT = 1800  # NSE exchange freeze quantity per order for F&O
ATR_PERIOD = 14
ATR_INTERVAL = "5minute"


def _fetch_atr(kite: KiteConnect, instrument_token: int, symbol: str) -> float | None:
    """Fetch 5-min historical data and compute 14-period ATR for a symbol."""
    from datetime import timedelta
    try:
        now_ist = datetime.now(IST)
        from_dt = now_ist - timedelta(hours=3)  # 3 hrs covers 14+ 5-min bars
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

def _place_sl_order(kite: KiteConnect, pos: dict, atr: float) -> tuple[float, float, list]:
    """Place SL-Limit order at 2x ATR from average entry price."""
    symbol  = pos["tradingsymbol"]
    qty     = pos["quantity"]
    avg     = pos["average_price"]
    sl_dist = 2 * atr

    if qty > 0:  # LONG — sell SL below entry
        tx            = kite.TRANSACTION_TYPE_SELL
        trigger       = round(avg - sl_dist, 1)
        buffer        = max(1.0, round(trigger * 0.01, 1))
        limit_price   = round(trigger - buffer, 1)
    else:        # SHORT — buy SL above entry
        tx            = kite.TRANSACTION_TYPE_BUY
        trigger       = round(avg + sl_dist, 1)
        buffer        = max(1.0, round(trigger * 0.01, 1))
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


def _get_lot_sizes(kite: KiteConnect, symbols: list[str]) -> dict[str, int]:
    """Fetch lot sizes for a list of NFO tradingsymbols."""
    try:
        instruments = kite.instruments("NFO")
        return {i["tradingsymbol"]: int(i["lot_size"]) for i in instruments if i["tradingsymbol"] in symbols}
    except Exception as exc:
        log.warning("Could not fetch lot sizes: %s — defaulting to 1", exc)
        return {}


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


def exit_non_hedge_positions(kite: KiteConnect) -> tuple[list, list, list]:
    """
    Limit-exit all open positions with LTP >= HEDGE_PRICE_THRESHOLD.
    Cancels any pending orders for those symbols first to avoid double-exits.
    Exits sold legs (short) first, then bought legs (long).
    Splits large orders into chunks to stay within exchange freeze limits.
    Returns (exited_symbols, skipped_symbols, failed_symbols).
    """
    data = kite.positions()
    net = [p for p in data.get("net", []) if p["quantity"] != 0]

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

        # Position size limit state
        self.oversize_since = {}        # symbol -> timestamp when it first exceeded MAX_POSITION_QTY
        self.last_oversize_alert = 0.0  # timestamp of last oversize alert (60s cooldown)

        self.refresh_positions()

    def refresh_positions(self):
        """Pull the latest open positions (day + net) from Kite REST API."""
        try:
            data = self.kite.positions()
            net = data.get("net", [])
            with self.lock:
                self.positions = {
                    p["instrument_token"]: p for p in net if p["quantity"] != 0
                }
                # Realized P&L from positions closed earlier today
                self.realized_pnl = sum(
                    float(p.get("pnl", 0)) for p in net if p["quantity"] == 0
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


# ---- Main ----

def main():
    log.info("P&L monitor (WebSocket) started.")

    # Wait until market opens if we're launched early
    while not is_market_open():
        log.info("Market closed. Sleeping 60 s.")
        time.sleep(60)

    try:
        access_token = _read_access_token()
    except RuntimeError as exc:
        log.error(str(exc))
        notify("🔑 Access token error", f"{exc}\nRun generate_token.py and restart the service.", priority="high")
        return

    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(access_token)

    tracker = PositionTracker(kite)

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

    kws.on_connect = on_connect
    kws.on_ticks = on_ticks
    kws.on_close = on_close
    kws.on_error = on_error
    kws.on_reconnect = on_reconnect
    kws.on_noreconnect = on_noreconnect

    # Run the ticker in a background thread so we can run our own alert loop here
    kws.connect(threaded=True)

    last_position_refresh = time.time()
    last_trailing_heartbeat = 0

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
                    # ATR notification for newly opened non-hedge positions
                    added_tokens = new_tokens - old_tokens
                    if added_tokens:
                        with tracker.lock:
                            new_pos = {t: tracker.positions[t] for t in added_tokens if t in tracker.positions}
                        for token, pos in new_pos.items():
                            if pos["last_price"] >= HEDGE_PRICE_THRESHOLD:
                                direction = "LONG" if pos["quantity"] > 0 else "SHORT"
                                atr = _fetch_atr(kite, token, pos["tradingsymbol"])
                                atr_line = f"5-min ATR (14): Rs {atr}" if atr else "ATR unavailable"
                                sl_line = ""
                                if atr:
                                    try:
                                        trigger, limit_price, order_ids = _place_sl_order(kite, pos, atr)
                                        sl_line = f"SL order placed: trigger Rs {trigger} | limit Rs {limit_price}"
                                    except Exception as exc:
                                        log.error("SL order failed for %s: %s", pos["tradingsymbol"], exc)
                                        sl_line = f"SL order FAILED: {exc}"
                                notify(
                                    f"📐 New position: {pos['tradingsymbol']}",
                                    f"{direction} {abs(pos['quantity'])} qty\n{atr_line}\n{sl_line}".strip(),
                                )
                last_position_refresh = now

            total_pnl, details = tracker.compute_pnl()

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
                buffer_msg = f"Exiting in {EXIT_BUFFER_SECONDS}s if not recovered." if AUTO_EXIT else ""
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
            if (AUTO_EXIT
                    and tracker.trail_breached
                    and not tracker.auto_exited
                    and tracker.breach_since is not None
                    and now - tracker.breach_since >= EXIT_BUFFER_SECONDS):
                tracker.auto_exited = True
                log.info("Auto-exit triggered after %ds breach.", EXIT_BUFFER_SECONDS)
                try:
                    exited, skipped, failed = exit_non_hedge_positions(kite)
                    lines = ["🔴 AUTO-EXIT EXECUTED"]
                    if exited:
                        lines.append(f"Exited: {', '.join(exited)}")
                    if skipped:
                        lines.append(f"Kept (hedge): {', '.join(skipped)}")
                    if failed:
                        lines.append(f"FAILED: {', '.join(failed)}")
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
                if (AUTO_EXIT
                        and tracker.green_day_armed
                        and not tracker.green_day_exited
                        and total_pnl <= GREEN_DAY_FLOOR):
                    tracker.green_day_exited = True
                    log.info("Green day floor Rs %.0f breached — auto-exiting.", GREEN_DAY_FLOOR)
                    try:
                        exited, skipped, failed = exit_non_hedge_positions(kite)
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
                    exited, skipped, failed = exit_non_hedge_positions(kite)
                    lines = []
                    if exited:
                        lines.append(f"Exited: {', '.join(exited)}")
                    if skipped:
                        lines.append(f"Kept (hedge): {', '.join(skipped)}")
                    if failed:
                        lines.append(f"FAILED: {', '.join(failed)}")
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
                    exited, skipped, failed = exit_non_hedge_positions(kite)
                    lines = []
                    if exited:
                        lines.append(f"Exited: {', '.join(exited)}")
                    if skipped:
                        lines.append(f"Kept (hedge): {', '.join(skipped)}")
                    if failed:
                        lines.append(f"FAILED: {', '.join(failed)}")
                    notify(
                        f"🛑 Loss limit Rs {LOSS_LIMIT:,.0f} hit — exited",
                        "\n".join(lines) if lines else "No positions to exit.",
                        priority="urgent",
                    )
                except Exception as exc:
                    log.error("Loss limit exit failed: %s", exc)
                    notify("🛑 Loss limit exit ERROR", str(exc), priority="urgent")

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
