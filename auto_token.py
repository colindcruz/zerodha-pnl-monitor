"""
Automated Zerodha token generation using user ID, password, and TOTP secret.
Runs as a cron job at 8:45 AM IST every weekday.
Saves the access token and restarts the monitor.
"""
import os
import sys
import subprocess
import time
import pyotp
import requests
from urllib.parse import urlparse, parse_qs
from kiteconnect import KiteConnect
from pathlib import Path
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

API_KEY            = os.environ["KITE_API_KEY"]
API_SECRET         = os.environ["KITE_API_SECRET"]
USER_ID            = os.environ["ZERODHA_USER_ID"]
PASSWORD           = os.environ["ZERODHA_PASSWORD"]
TOTP_SECRET        = os.environ["ZERODHA_TOTP_SECRET"]
ACCESS_TOKEN_PATH  = SCRIPT_DIR / os.getenv("ACCESS_TOKEN_PATH", ".access_token")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]


def notify(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception:
        pass


def run():
    print("Generating Zerodha access token...")

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # Step 1: Login with user ID + password
    resp = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": USER_ID, "password": PASSWORD},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise Exception(f"Login failed: {data.get('message', data)}")

    request_id = data["data"]["request_id"]
    print(f"Login successful. request_id: {request_id[:8]}...")

    # Step 2: Submit TOTP — avoid generating a code right at the edge of its
    # 30s window, since network latency could land the request in the next
    # window and get it rejected as invalid.
    if int(time.time()) % 30 > 25:
        time.sleep(31 - (int(time.time()) % 30))
    totp_code = pyotp.TOTP(TOTP_SECRET).now()
    resp = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id": USER_ID,
            "request_id": request_id,
            "twofa_value": totp_code,
        },
        timeout=15,
    )
    twofa_data = resp.json()
    if twofa_data.get("status") != "success":
        raise Exception(f"2FA failed: {twofa_data.get('message', twofa_data)}")
    print("TOTP verified.")

    # Step 3: Follow OAuth flow — stop before the final redirect to 127.0.0.1
    resp = session.get(
        f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3",
        allow_redirects=False,
        timeout=15,
    )
    # Follow redirects manually until we hit the callback URL with request_token
    for _ in range(5):
        location = resp.headers.get("Location", "")
        params = parse_qs(urlparse(location).query)
        request_token = params.get("request_token", [None])[0]
        if request_token:
            break
        if not location or "127.0.0.1" in location or location.startswith("/callback"):
            # Extract token from this URL directly
            request_token = params.get("request_token", [None])[0]
            break
        resp = session.get(location if location.startswith("http") else f"https://kite.zerodha.com{location}",
                           allow_redirects=False, timeout=15)

    if not request_token:
        raise Exception(f"Could not get request_token. Last redirect: {location}")
    print(f"Got request_token: {request_token[:8]}...")

    # Step 4: Exchange for access token
    kite = KiteConnect(api_key=API_KEY)
    session_data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = session_data["access_token"]

    # Step 5: Save token
    ACCESS_TOKEN_PATH.write_text(access_token)
    print(f"Access token saved.")

    # Step 6: Restart monitor
    subprocess.run(["systemctl", "restart", "pnl-monitor"], check=True)
    print("Monitor restarted.")

    # Step 7: Restart the live dashboard too, if it's installed — it holds its own
    # Kite session (same access token file) and needs a fresh KiteTicker connection
    # every morning just like the monitor does. Not fatal if this service doesn't
    # exist on a given box (e.g. dashboard not deployed there).
    dashboard_restarted = subprocess.run(
        ["systemctl", "restart", "live-dashboard"], check=False,
    ).returncode == 0
    if dashboard_restarted:
        print("Live dashboard restarted.")

    notify("✅ Token generated & monitor restarted. Ready for 9:15 AM.")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        notify(f"❌ Auto token generation FAILED: {exc}")
        sys.exit(1)
