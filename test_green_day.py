"""
Dry-run test for green day floor logic.
Simulates P&L rising to Rs 20k then dropping to Rs 5k.
Sends real Telegram + ntfy notifications but does NOT place any orders.
"""
import os
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from pathlib import Path
import requests
import time

load_dotenv()

API_KEY   = os.environ["KITE_API_KEY"]
TOKEN     = Path(os.getenv("ACCESS_TOKEN_PATH", ".access_token")).read_text().strip()
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")

GREEN_DAY_ACTIVATION = float(os.getenv("GREEN_DAY_ACTIVATION", "20000"))
GREEN_DAY_FLOOR      = float(os.getenv("GREEN_DAY_FLOOR", "5000"))
HEDGE_PRICE_THRESHOLD = float(os.getenv("HEDGE_PRICE_THRESHOLD", "5.0"))

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
            data=body.encode(),
            headers={"Title": title, "Priority": "high"},
            timeout=10,
        )
    print(f"[NOTIFY] {title} | {body}")


def get_exit_list():
    net = kite.positions().get("net", [])
    open_pos = [p for p in net if p["quantity"] != 0]
    sold  = [p for p in open_pos if p["quantity"] < 0 and p["last_price"] >= HEDGE_PRICE_THRESHOLD]
    bought = [p for p in open_pos if p["quantity"] > 0 and p["last_price"] >= HEDGE_PRICE_THRESHOLD]
    hedges = [p for p in open_pos if p["last_price"] < HEDGE_PRICE_THRESHOLD]
    return sold + bought, hedges


# ---- Simulate ----

print("=== GREEN DAY FLOOR DRY-RUN TEST ===\n")

to_exit, hedges = get_exit_list()

print(f"Current open positions:")
for p in to_exit:
    print(f"  Would EXIT: {p['tradingsymbol']} qty={p['quantity']} ltp={p['last_price']}")
for p in hedges:
    print(f"  Would KEEP (hedge): {p['tradingsymbol']} ltp={p['last_price']}")
print()

# Step 1: P&L crosses GREEN_DAY_ACTIVATION
fake_pnl = GREEN_DAY_ACTIVATION + 1
print(f"Step 1 — P&L rises to Rs {fake_pnl:,.0f} (crosses Rs {GREEN_DAY_ACTIVATION:,.0f})")
notify(
    "🟢 [TEST] Green day floor armed",
    f"P&L hit Rs {fake_pnl:,.2f} — floor locked at Rs {GREEN_DAY_FLOOR:,.0f}",
)
time.sleep(2)

# Step 2: P&L drops to GREEN_DAY_FLOOR
fake_pnl = GREEN_DAY_FLOOR
print(f"\nStep 2 — P&L drops to Rs {fake_pnl:,.0f} (hits floor)")
exit_symbols = [p["tradingsymbol"] for p in to_exit]
hedge_symbols = [p["tradingsymbol"] for p in hedges]
lines = []
if exit_symbols:
    lines.append(f"Would exit: {', '.join(exit_symbols)}")
if hedge_symbols:
    lines.append(f"Would keep (hedge): {', '.join(hedge_symbols)}")
lines.append("(DRY RUN — no orders placed)")
notify(
    f"🟢 [TEST] Green day floor Rs {GREEN_DAY_FLOOR:,.0f} breached — exited",
    "\n".join(lines) if lines else "No open positions.",
)

print("\n=== TEST COMPLETE — no orders were placed ===")
