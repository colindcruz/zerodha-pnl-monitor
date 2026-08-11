import os
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from pathlib import Path
import requests

load_dotenv()
api_key = os.environ["KITE_API_KEY"]
token = Path(".access_token").read_text().strip()
kite = KiteConnect(api_key=api_key)
kite.set_access_token(token)

positions = kite.positions()
net = positions.get("net", [])

open_pos = [p for p in net if p["quantity"] != 0]
closed_pos = [p for p in net if p["quantity"] == 0]

open_pnl = sum(float(p.get("pnl", 0)) for p in open_pos)
realized_pnl = sum(float(p.get("pnl", 0)) for p in closed_pos)
total_pnl = open_pnl + realized_pnl

lines = ["📊 <b>Intraday P&L Snapshot</b>"]
if open_pos:
    for p in sorted(open_pos, key=lambda x: -abs(float(x["pnl"]))):
        lines.append(f"{p['tradingsymbol']}: ₹{float(p['pnl']):,.2f}")
else:
    lines.append("No open positions.")
if realized_pnl != 0:
    lines.append(f"Realized (closed): ₹{realized_pnl:,.2f}")
lines.append(f"<b>Total: ₹{total_pnl:,.2f}</b>")

msg = "\n".join(lines)
bot = os.environ["TELEGRAM_BOT_TOKEN"]
chat = os.environ["TELEGRAM_CHAT_ID"]
r = requests.post(
    f"https://api.telegram.org/bot{bot}/sendMessage",
    json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
    timeout=10,
)
print("Sent!" if r.ok else f"Failed: {r.text}")
