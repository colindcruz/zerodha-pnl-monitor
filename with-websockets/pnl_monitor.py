"""
Real-time P&L Monitor for Zerodha Kite positions
==================================================

What it does:
- Connects to Kite's WebSocket ticker (KiteTicker) for live tick-by-tick prices
- Auto-detects all your open positions via kite.positions()
- Computes running P&L on every tick using live LTP
- Sends Telegram alerts:
    1. A periodic summary every PERIODIC_INTERVAL_SECONDS
    2. An immediate alert whenever total P&L crosses PROFIT_THRESHOLD or LOSS_THRESHOLD

Secrets are loaded from a .env file (see .env.example).
Run generate_token.py each morning to refresh the daily access token.
"""

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

PERIODIC_INTERVAL_SECONDS   = int(os.getenv("PERIODIC_INTERVAL_SECONDS", "30"))
PROFIT_THRESHOLD             = float(os.getenv("PROFIT_THRESHOLD", "5000"))
LOSS_THRESHOLD               = float(os.getenv("LOSS_THRESHOLD", "-5000"))
THRESHOLD_COOLDOWN_SECONDS   = int(os.getenv("THRESHOLD_COOLDOWN_SECONDS", "300"))

# ---- Trailing profit-lock ----
TRAIL_ACTIVATION_THRESHOLD  = float(os.getenv("TRAIL_ACTIVATION_THRESHOLD", "40000"))
TRAIL_PERCENT               = float(os.getenv("TRAIL_PERCENT", "20"))
EXIT_ALERT_REPEAT_SECONDS   = int(os.getenv("EXIT_ALERT_REPEAT_SECONDS", "15"))
TRAIL_CHECK_INTERVAL        = int(os.getenv("TRAIL_CHECK_INTERVAL", "1"))

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

# ---- Telegram helper ----

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        if not resp.ok:
            log.error(f"Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        log.error(f"Telegram send error: {e}")


# ---- Position tracking ----

class PositionTracker:
    """Holds current positions and live LTPs, computes P&L."""

    def __init__(self, kite: KiteConnect):
        self.kite = kite
        self.lock = threading.Lock()
        self.positions = {}       # instrument_token -> position dict
        self.ltp = {}             # instrument_token -> last traded price
        self.last_threshold_fire = {"profit": 0, "loss": 0}

        # Trailing profit-lock state
        self.trail_armed = False
        self.trail_peak = 0.0
        self.trail_exit_level = None
        self.trail_breached = False
        self.last_exit_alert = 0

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
            log.info(f"Refreshed positions: {len(self.positions)} open")
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
            total = 0.0
            details = []
            for token, pos in self.positions.items():
                qty = pos["quantity"]
                avg_price = pos["average_price"]
                ltp = self.ltp.get(token, pos.get("last_price", avg_price))
                multiplier = pos.get("multiplier", 1) or 1
                pnl = (ltp - avg_price) * qty * multiplier
                total += pnl
                details.append({
                    "symbol": pos["tradingsymbol"],
                    "qty": qty,
                    "avg_price": avg_price,
                    "ltp": ltp,
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
                self.trail_exit_level = self.trail_peak * (1 - TRAIL_PERCENT / 100)
                event = "armed"
            return event

        # Already armed — track new peaks
        if total_pnl > self.trail_peak:
            self.trail_peak = total_pnl
            new_exit_level = self.trail_peak * (1 - TRAIL_PERCENT / 100)
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
        send_telegram(f"🔑 {exc}\nRun generate_token.py and restart the service.")
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
        send_telegram(
            f"📡 P&L Monitor started\n"
            f"Watching {len(tokens)} open position(s)\n"
            f"Current P&L: Rs {total_pnl:,.2f}"
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
        send_telegram("P&L Monitor: WebSocket connection lost and could not reconnect. Please check the server.")

    kws.on_connect = on_connect
    kws.on_ticks = on_ticks
    kws.on_close = on_close
    kws.on_error = on_error
    kws.on_reconnect = on_reconnect
    kws.on_noreconnect = on_noreconnect

    # Run the ticker in a background thread so we can run our own alert loop here
    kws.connect(threaded=True)

    last_periodic = 0
    last_position_refresh = time.time()

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
                last_position_refresh = now

            total_pnl, details = tracker.compute_pnl()

            # ---- Trailing profit-lock (the important one) ----
            event = tracker.update_trailing_stop(total_pnl)

            if event == "armed":
                send_telegram(
                    f"🔒 TRAILING LOCK ARMED\n"
                    f"P&L hit Rs {total_pnl:,.2f} (threshold Rs {TRAIL_ACTIVATION_THRESHOLD:,.0f}).\n"
                    f"Exit floor set at Rs {tracker.trail_exit_level:,.2f} "
                    f"({TRAIL_PERCENT}% trail from peak)."
                )
            elif event == "new_peak":
                send_telegram(
                    f"📈 NEW PEAK: Rs {tracker.trail_peak:,.2f}\n"
                    f"Exit floor raised to Rs {tracker.trail_exit_level:,.2f}"
                )
            elif event == "breach":
                send_telegram(
                    f"🚨🚨 EXIT NOW 🚨🚨\n"
                    f"P&L Rs {total_pnl:,.2f} has dropped to/below your trailing exit floor "
                    f"of Rs {tracker.trail_exit_level:,.2f} (peak was Rs {tracker.trail_peak:,.2f}).\n"
                    f"CLOSE THE POSITION."
                )
                tracker.last_exit_alert = now
            elif event == "recovered":
                send_telegram(
                    f"✅ Back above exit floor. P&L Rs {total_pnl:,.2f} "
                    f"(floor Rs {tracker.trail_exit_level:,.2f})"
                )

            # Repeat the EXIT NOW alert while still breached, so it can't be missed
            if tracker.trail_breached and now - tracker.last_exit_alert >= EXIT_ALERT_REPEAT_SECONDS:
                send_telegram(
                    f"🚨 STILL BELOW EXIT FLOOR — P&L Rs {total_pnl:,.2f} "
                    f"vs floor Rs {tracker.trail_exit_level:,.2f}. EXIT NOW."
                )
                tracker.last_exit_alert = now

            # ---- Routine heartbeat summary ----
            if now - last_periodic >= PERIODIC_INTERVAL_SECONDS:
                if details:
                    send_telegram(format_summary(total_pnl, details))
                last_periodic = now

            # ---- Optional simple one-shot thresholds (independent of trailing) ----
            if total_pnl >= PROFIT_THRESHOLD and now - tracker.last_threshold_fire["profit"] > THRESHOLD_COOLDOWN_SECONDS:
                send_telegram(f"🟢 PROFIT THRESHOLD HIT\nTotal P&L: Rs {total_pnl:,.2f}")
                tracker.last_threshold_fire["profit"] = now

            if total_pnl <= LOSS_THRESHOLD and now - tracker.last_threshold_fire["loss"] > THRESHOLD_COOLDOWN_SECONDS:
                send_telegram(f"🔴 LOSS THRESHOLD HIT\nTotal P&L: Rs {total_pnl:,.2f}")
                tracker.last_threshold_fire["loss"] = now

            time.sleep(TRAIL_CHECK_INTERVAL)

    except KeyboardInterrupt:
        log.info("Stopping...")
        kws.close()


if __name__ == "__main__":
    main()
