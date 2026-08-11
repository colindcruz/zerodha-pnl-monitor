"""
Dry-run test for tiered loss warnings.
Simulates P&L dropping through -20k, -30k, and -40k.
Sends real Telegram + ntfy notifications but does NOT place any orders.
"""
import os
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from pathlib import Path
import requests
import time

load_dotenv()

API_KEY    = os.environ["KITE_API_KEY"]
TOKEN      = Path(os.getenv("ACCESS_TOKEN_PATH", ".access_token")).read_text().strip()
BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")

LOSS_WARNING_1        = float(os.getenv("LOSS_WARNING_1", "-20000"))
LOSS_WARNING_2        = float(os.getenv("LOSS_WARNING_2", "-30000"))
LOSS_LIMIT            = float(os.getenv("LOSS_LIMIT", "-40000"))
HEDGE_PRICE_THRESHOLD = float(os.getenv("HEDGE_PRICE_THRESHOLD", "5.0"))

kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(TOKEN)


def notify(title, body, priority="default"):
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
            headers={"Title": title, "Priority": priority},
            timeout=10,
        )
    print(f"[NOTIFY] {title} | {body}")


def get_exit_list():
    net = kite.positions().get("net", [])
    open_pos = [p for p in net if p["quantity"] != 0]
    to_exit = [p for p in open_pos if p["last_price"] >= HEDGE_PRICE_THRESHOLD]
    hedges  = [p for p in open_pos if p["last_price"] < HEDGE_PRICE_THRESHOLD]
    return to_exit, hedges


print("=== LOSS WARNING DRY-RUN TEST ===\n")

to_exit, hedges = get_exit_list()
print("Current open positions:")
for p in to_exit:
    print(f"  Would EXIT: {p['tradingsymbol']} qty={p['quantity']} ltp={p['last_price']}")
for p in hedges:
    print(f"  Would KEEP (hedge): {p['tradingsymbol']} ltp={p['last_price']}")
print()

# Step 1: -20k warning
fake_pnl = LOSS_WARNING_1
print(f"Step 1 — P&L drops to Rs {fake_pnl:,.0f}")
notify(
    "⚠️ [TEST] Loss warning — Rs 20k down",
    f"P&L is Rs {fake_pnl:,.2f}. Stay cautious.",
    priority="high",
)
time.sleep(2)

# Step 2: -30k cut size warning
fake_pnl = LOSS_WARNING_2
print(f"\nStep 2 — P&L drops to Rs {fake_pnl:,.0f}")
notify(
    "🔴 [TEST] Loss at Rs 30k — cut position size by 50%",
    f"P&L is Rs {fake_pnl:,.2f}. Hard shutdown at Rs {LOSS_LIMIT:,.0f}.",
    priority="urgent",
)
time.sleep(2)

# Step 3: -40k hard shutdown (dry run)
fake_pnl = LOSS_LIMIT
print(f"\nStep 3 — P&L drops to Rs {fake_pnl:,.0f} (hard shutdown)")
exit_symbols  = [p["tradingsymbol"] for p in to_exit]
hedge_symbols = [p["tradingsymbol"] for p in hedges]
lines = []
if exit_symbols:
    lines.append(f"Would exit: {', '.join(exit_symbols)}")
if hedge_symbols:
    lines.append(f"Would keep (hedge): {', '.join(hedge_symbols)}")
lines.append("(DRY RUN — no orders placed)")
notify(
    f"🛑 [TEST] Loss limit Rs {LOSS_LIMIT:,.0f} hit — exited",
    "\n".join(lines) if lines else "No open positions.",
    priority="urgent",
)

print("\n=== TEST COMPLETE — no orders were placed ===")
