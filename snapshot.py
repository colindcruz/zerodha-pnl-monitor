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
total_pnl = sum(float(p.get("pnl", 0)) for p in net)

lines = ["📊 <b>Intraday P&L Snapshot</b>"]
if net:
    for p in net:
        lines.append(f"{p['tradingsymbol']}: ₹{float(p['pnl']):,.2f}")
else:
    lines.append("No open positions.")
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
