"""Exit a specific position by symbol — places a market SELL/BUY order."""
import os, sys
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from pathlib import Path

load_dotenv()
kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
kite.set_access_token(Path(os.getenv("ACCESS_TOKEN_PATH", ".access_token")).read_text().strip())

symbol = sys.argv[1]
net = [p for p in kite.positions()["net"] if p["tradingsymbol"] == symbol and p["quantity"] != 0]

if not net:
    print(f"No open position found for {symbol}")
    sys.exit(1)

pos = net[0]
qty = pos["quantity"]
tx = kite.TRANSACTION_TYPE_SELL if qty > 0 else kite.TRANSACTION_TYPE_BUY

order_id = kite.place_order(
    variety=kite.VARIETY_REGULAR,
    exchange=pos["exchange"],
    tradingsymbol=symbol,
    transaction_type=tx,
    quantity=abs(qty),
    product=pos["product"],
    order_type=kite.ORDER_TYPE_MARKET,
)
print(f"Order placed: {tx} {abs(qty)} {symbol} — order ID {order_id}")
