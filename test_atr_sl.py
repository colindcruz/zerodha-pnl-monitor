"""
Dry-run test for ATR calculation and SL order placement.
Fetches real ATR from historical data, calculates SL levels,
sends real notification but does NOT place any orders.
"""
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote
import requests

load_dotenv()

API_KEY    = os.environ["KITE_API_KEY"]
TOKEN      = Path(os.getenv("ACCESS_TOKEN_PATH", ".access_token")).read_text().strip()
BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
HEDGE_PRICE_THRESHOLD = float(os.getenv("HEDGE_PRICE_THRESHOLD", "5.0"))
ATR_PERIOD = 14
IST = ZoneInfo("Asia/Kolkata")

kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(TOKEN)


def notify(title, body):
    msg = f"{title}\n{body}"
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": msg},
        timeout=10,
    )
    if NTFY_TOPIC:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": quote(title), "Priority": "default"},
            timeout=10,
        )
    print(f"[NOTIFY] {title}\n        {body}\n")


def fetch_atr(instrument_token, symbol):
    now_ist = datetime.now(IST)
    from_dt = now_ist - timedelta(days=2)  # look back 2 days to cover closed market periods
    try:
        records = kite.historical_data(
            instrument_token,
            from_date=from_dt.replace(tzinfo=None),
            to_date=now_ist.replace(tzinfo=None),
            interval="5minute",
        )
        print(f"  Fetched {len(records)} 5-min bars for {symbol}")
        if len(records) < ATR_PERIOD + 1:
            print(f"  Not enough bars for ATR ({len(records)} bars)")
            return None
        trs = []
        for i in range(1, len(records)):
            h, l, pc = records[i]["high"], records[i]["low"], records[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return round(sum(trs[-ATR_PERIOD:]) / ATR_PERIOD, 2)
    except Exception as exc:
        print(f"  ATR fetch failed: {exc}")
        return None


def calc_sl(pos, atr):
    qty = pos["quantity"]
    avg = pos["average_price"]
    sl_dist = 2 * atr
    if qty > 0:
        trigger     = round(avg - sl_dist, 1)
        buffer      = min(10.0, max(1.0, round(trigger * 0.01, 1)))
        limit_price = round(trigger - buffer, 1)
        tx          = "SELL"
    else:
        trigger     = round(avg + sl_dist, 1)
        buffer      = min(10.0, max(1.0, round(trigger * 0.01, 1)))
        limit_price = round(trigger + buffer, 1)
        tx          = "BUY"
    return tx, trigger, limit_price


print("=== ATR + SL ORDER DRY-RUN TEST ===\n")

net = kite.positions()["net"]
non_hedges = [p for p in net if p["quantity"] != 0 and p["last_price"] >= HEDGE_PRICE_THRESHOLD]

if not non_hedges:
    # No live non-hedge positions — use a mock position with a real instrument token
    # Pick the first available NFO instrument for ATR test
    print("No non-hedge positions open. Using mock position with Nifty futures for ATR test.\n")
    instruments = kite.instruments("NFO")
    nifty_fut = next((i for i in instruments if "NIFTY" in i["tradingsymbol"] and i["instrument_type"] == "FUT"), None)
    if not nifty_fut:
        print("Could not find Nifty futures instrument.")
        exit(1)
    mock_pos = {
        "tradingsymbol": nifty_fut["tradingsymbol"],
        "instrument_token": nifty_fut["instrument_token"],
        "quantity": -50,  # mock SHORT
        "average_price": nifty_fut.get("last_price", 24000) or 24000,
        "last_price": nifty_fut.get("last_price", 24000) or 24000,
        "exchange": "NFO",
        "product": "NRML",
    }
    positions_to_test = [mock_pos]
else:
    positions_to_test = non_hedges
    print(f"Found {len(non_hedges)} non-hedge position(s).\n")

for pos in positions_to_test:
    symbol    = pos["tradingsymbol"]
    token     = pos["instrument_token"]
    direction = "LONG" if pos["quantity"] > 0 else "SHORT"
    qty       = abs(pos["quantity"])
    avg       = pos["average_price"]

    print(f"Position: {symbol} | {direction} {qty} qty | avg Rs {avg}")

    atr = fetch_atr(token, symbol)
    if atr:
        print(f"  ATR (14-period, 5-min): Rs {atr}")
        tx, trigger, limit_price = calc_sl(pos, atr)
        print(f"  SL: {tx} at trigger Rs {trigger}, limit Rs {limit_price}")
        print(f"  (DRY RUN — no order placed)")
        notify(
            f"📐 [TEST] New position: {symbol}",
            f"{direction} {qty} qty | avg Rs {avg}\n"
            f"5-min ATR (14): Rs {atr}\n"
            f"SL order would be placed: trigger Rs {trigger} | limit Rs {limit_price}\n"
            f"(DRY RUN — no order placed)",
        )
    else:
        print(f"  ATR unavailable — SL not placed")
        notify(f"📐 [TEST] New position: {symbol}", f"{direction} {qty} qty\nATR unavailable")

print("=== TEST COMPLETE ===")
