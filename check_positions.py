import os
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from pathlib import Path

load_dotenv()
kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
kite.set_access_token(Path(".access_token").read_text().strip())

net = kite.positions()["net"]
open_pos = [p for p in net if p["quantity"] != 0]

print(f"{'Symbol':<30} {'Qty':>6}  {'Avg':>8}  {'LTP':>8}  {'P&L':>10}  Product")
print("-" * 80)
for p in open_pos:
    print(f"{p['tradingsymbol']:<30} {p['quantity']:>6}  {p['average_price']:>8.2f}  {p['last_price']:>8.2f}  {p['pnl']:>10.2f}  {p['product']}")
