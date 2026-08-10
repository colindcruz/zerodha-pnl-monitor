"""
Dry-run test for auto-exit logic.
Shows exactly which orders would be placed and in what order — no real orders sent.
"""

import os
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from pathlib import Path

load_dotenv()

HEDGE_PRICE_THRESHOLD = float(os.getenv("HEDGE_PRICE_THRESHOLD", "5.0"))

kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
kite.set_access_token(Path(os.getenv("ACCESS_TOKEN_PATH", ".access_token")).read_text().strip())

net = [p for p in kite.positions()["net"] if p["quantity"] != 0]

sold_legs   = [p for p in net if p["quantity"] < 0 and p["last_price"] >= HEDGE_PRICE_THRESHOLD]
bought_legs = [p for p in net if p["quantity"] > 0 and p["last_price"] >= HEDGE_PRICE_THRESHOLD]
hedges      = [p for p in net if p["last_price"] < HEDGE_PRICE_THRESHOLD]

print("\n=== AUTO-EXIT DRY RUN ===\n")

print(f"{'#':<4} {'Action':<8} {'Symbol':<30} {'Qty':>6}  {'LTP':>8}  {'Product'}")
print("-" * 70)

i = 1
for p in sold_legs:
    print(f"{i:<4} {'BUY':<8} {p['tradingsymbol']:<30} {abs(p['quantity']):>6}  {p['last_price']:>8.2f}  {p['product']}  ← cover short")
    i += 1

for p in bought_legs:
    print(f"{i:<4} {'SELL':<8} {p['tradingsymbol']:<30} {abs(p['quantity']):>6}  {p['last_price']:>8.2f}  {p['product']}  ← exit long")
    i += 1

if hedges:
    print()
    print("SKIPPED (hedge — LTP below Rs %.0f):" % HEDGE_PRICE_THRESHOLD)
    for p in hedges:
        print(f"  {p['tradingsymbol']:<30} qty={p['quantity']:>6}  ltp={p['last_price']:>6.2f}")

if not sold_legs and not bought_legs:
    print("  No non-hedge positions to exit.")

print("\n✅ Dry run complete — no orders were placed.\n")
