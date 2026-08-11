"""Diagnostic — shows all positions including closed ones and total P&L."""
import os
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from pathlib import Path

load_dotenv()
kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
kite.set_access_token(Path(os.getenv("ACCESS_TOKEN_PATH", ".access_token")).read_text().strip())

net = kite.positions()["net"]

print(f"\n{'Symbol':<30} {'Qty':>6}  {'Avg':>8}  {'LTP':>8}  {'P&L (Kite)':>12}  Status")
print("-" * 85)

open_pnl, closed_pnl = 0.0, 0.0
for p in net:
    status = "OPEN" if p["quantity"] != 0 else "CLOSED"
    print(f"{p['tradingsymbol']:<30} {p['quantity']:>6}  {p['average_price']:>8.2f}  {p['last_price']:>8.2f}  {p['pnl']:>12.2f}  {status}")
    if p["quantity"] != 0:
        open_pnl += p["pnl"]
    else:
        closed_pnl += p["pnl"]

total = open_pnl + closed_pnl
print("-" * 85)
print(f"{'Open positions P&L:':<50} {open_pnl:>12.2f}")
print(f"{'Closed/realized P&L:':<50} {closed_pnl:>12.2f}")
print(f"{'TOTAL (what Zerodha shows):':<50} {total:>12.2f}")
print(f"{'What our monitor currently shows:':<50} {open_pnl:>12.2f}  ← missing closed P&L")
