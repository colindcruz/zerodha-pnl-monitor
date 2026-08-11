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

# ---- Trailing profit-lock ----
TRAIL_ACTIVATION_THRESHOLD  = float(os.getenv("TRAIL_ACTIVATION_THRESHOLD", "40000"))
TRAIL_PERCENT               = float(os.getenv("TRAIL_PERCENT", "20"))
EXIT_ALERT_REPEAT_SECONDS   = int(os.getenv("EXIT_ALERT_REPEAT_SECONDS", "15"))
TRAIL_CHECK_INTERVAL        = int(os.getenv("TRAIL_CHECK_INTERVAL", "1"))

# ---- Auto-exit on trailing breach ----
AUTO_EXIT               = os.getenv("AUTO_EXIT", "true").lower() == "true"
HEDGE_PRICE_THRESHOLD   = float(os.getenv("HEDGE_PRICE_THRESHOLD", "5.0"))  # positions with LTP below this are kept
EXIT_BUFFER_SECONDS     = int(os.getenv("EXIT_BUFFER_SECONDS", "30"))  # wait this long below floor before exiting

# ---- Profit target exit ----
PROFIT_TARGET           = float(os.getenv("PROFIT_TARGET", "80000"))   # exit all non-hedge positions when P&L hits this
LOSS_LIMIT              = float(os.getenv("LOSS_LIMIT", "-40000"))     # exit all non-hedge positions when P&L hits this

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
    try:
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode(),
            headers={"Title": title, "Priority": priority},
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

def exit_non_hedge_positions(kite: KiteConnect) -> tuple[list, list]:
    """
    Market-exit all open positions with LTP >= HEDGE_PRICE_THRESHOLD.
    Exits sold legs (short) first, then bought legs (long).
    Returns (exited_symbols, skipped_symbols).
    """
    data = kite.positions()
    net = [p for p in data.get("net", []) if p["quantity"] != 0]

    sold_legs  = [p for p in net if p["quantity"] < 0 and p["last_price"] >= HEDGE_PRICE_THRESHOLD]
    bought_legs = [p for p in net if p["quantity"] > 0 and p["last_price"] >= HEDGE_PRICE_THRESHOLD]
    hedges     = [p for p in net if p["last_price"] < HEDGE_PRICE_THRESHOLD]

    exited, failed = [], []

    for pos in sold_legs + bought_legs:
        symbol = pos["tradingsymbol"]
        qty    = pos["quantity"]
        tx     = kite.TRANSACTION_TYPE_BUY if qty < 0 else kite.TRANSACTION_TYPE_SELL
        try:
            kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=pos["exchange"],
                tradingsymbol=symbol,
                transaction_type=tx,
                quantity=abs(qty),
                product=pos["product"],
                order_type=kite.ORDER_TYPE_MARKET,
            )
            log.info("Exit order placed: %s qty=%d", symbol, qty)
            exited.append(symbol)
        except Exception as exc:
            log.error("Failed to exit %s: %s", symbol, exc)
            failed.append(f"{symbol} (ERROR: {exc})")

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
        self.breach_since = None       # timestamp when breach started
        self.auto_exited = False       # only fire auto-exit once per breach
        self.profit_target_hit = False  # only fire profit target exit once
        self.loss_limit_hit = False     # only fire loss limit exit once

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
                last_position_refresh = now

            total_pnl, details = tracker.compute_pnl()

            # ---- Trailing profit-lock (the important one) ----
            event = tracker.update_trailing_stop(total_pnl)

            if event == "armed":
                notify(
                    "🔒 Trailing lock armed",
                    f"P&L hit Rs {total_pnl:,.2f}\nExit floor: Rs {tracker.trail_exit_level:,.2f} ({TRAIL_PERCENT}% trail)",
                    priority="high",
                )
            elif event == "new_peak":
                notify(
                    f"📈 New peak: Rs {tracker.trail_peak:,.2f}",
                    f"Exit floor raised to Rs {tracker.trail_exit_level:,.2f}",
                )
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
