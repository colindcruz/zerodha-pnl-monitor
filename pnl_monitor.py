"""
Zerodha Intraday P&L Monitor
Polls positions every N minutes during market hours and fires Telegram alerts
when P&L crosses configured thresholds.
"""

import json
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from kiteconnect import KiteConnect

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

API_KEY = os.environ["KITE_API_KEY"]
ACCESS_TOKEN_PATH = Path(os.getenv("ACCESS_TOKEN_PATH", ".access_token"))

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

LOSS_THRESHOLD = float(os.getenv("LOSS_THRESHOLD", "-5000"))
PROFIT_THRESHOLD = float(os.getenv("PROFIT_THRESHOLD", "10000"))
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "5"))

STATE_FILE = Path(os.getenv("STATE_FILE", "alert_state.json"))
LOG_FILE = Path(os.getenv("LOG_FILE", "pnl_monitor.log"))

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = (9, 15)   # HH, MM
MARKET_CLOSE = (15, 45)  # options can be bought/sold until 15:45 even though the market is only partially open past 15:30

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load today's alert state; return fresh state if stale or missing."""
    today = date.today().isoformat()
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            if state.get("date") == today:
                return state
        except (json.JSONDecodeError, KeyError):
            pass
    return {"date": today, "loss_fired": False, "profit_fired": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message: str, retries: int = 3) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            log.info("Telegram alert sent.")
            return
        except requests.RequestException as exc:
            log.warning("Telegram attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(2 ** attempt)
    log.error("All Telegram retries exhausted — alert not delivered.")


# ---------------------------------------------------------------------------
# Kite helpers
# ---------------------------------------------------------------------------

def build_kite() -> KiteConnect:
    """Construct a KiteConnect instance using the daily access token."""
    access_token = _read_access_token()
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(access_token)
    return kite


def _read_access_token() -> str:
    # Prefer dedicated token file; fall back to env var
    if ACCESS_TOKEN_PATH.exists():
        token = ACCESS_TOKEN_PATH.read_text().strip()
        if token:
            return token
    token = os.getenv("KITE_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError(
            "No access token found. Run generate_token.py first."
        )
    return token


def fetch_pnl(kite: KiteConnect) -> float:
    """Return total intraday P&L across all net positions."""
    positions = kite.positions()
    net = positions.get("net", [])
    return sum(float(p.get("pnl", 0)) for p in net)


# ---------------------------------------------------------------------------
# Market hours check
# ---------------------------------------------------------------------------

def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t <= MARKET_CLOSE


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    log.info(
        "P&L monitor started. Loss threshold: %.0f | Profit threshold: %.0f | "
        "Poll interval: %d min",
        LOSS_THRESHOLD, PROFIT_THRESHOLD, POLL_INTERVAL_MINUTES,
    )

    while True:
        if not is_market_open():
            log.info("Market closed. Sleeping 60 s.")
            time.sleep(60)
            continue

        state = load_state()

        try:
            kite = build_kite()
            pnl = fetch_pnl(kite)
            log.info("P&L: %.2f", pnl)

            if pnl <= LOSS_THRESHOLD and not state["loss_fired"]:
                msg = (
                    f"⚠️ <b>LOSS ALERT</b>\n"
                    f"Intraday P&L has hit <b>₹{pnl:,.2f}</b>\n"
                    f"Threshold: ₹{LOSS_THRESHOLD:,.0f}"
                )
                send_telegram(msg)
                state["loss_fired"] = True
                save_state(state)

            elif pnl >= PROFIT_THRESHOLD and not state["profit_fired"]:
                msg = (
                    f"✅ <b>PROFIT ALERT</b>\n"
                    f"Intraday P&L has reached <b>₹{pnl:,.2f}</b>\n"
                    f"Threshold: ₹{PROFIT_THRESHOLD:,.0f}"
                )
                send_telegram(msg)
                state["profit_fired"] = True
                save_state(state)

        except RuntimeError as exc:
            # Missing / expired token — alert and stop polling until fixed
            log.error("Token error: %s", exc)
            send_telegram(
                f"🔑 <b>Access token error</b>\n{exc}\n"
                "Please run <code>generate_token.py</code> and restart the service."
            )
            # Back off for 10 minutes before retrying so logs aren't spammed
            time.sleep(600)
            continue

        except Exception as exc:  # noqa: BLE001
            log.warning("API error (will retry): %s", exc, exc_info=True)
            # Exponential-ish back-off capped at one poll interval
            time.sleep(min(60, POLL_INTERVAL_MINUTES * 60))
            continue

        time.sleep(POLL_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    run()
