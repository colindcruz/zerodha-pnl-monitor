"""
Daily access-token generator for Kite Connect.

Run this script each morning before market open:
    python generate_token.py

It will:
  1. Print the Kite login URL — open it in your browser.
  2. After logging in (with your Zerodha credentials + TOTP), you are redirected
     to your redirect URL with ?request_token=... in the query string.
  3. Paste that request_token here; the script exchanges it for an access_token
     and writes it to .access_token (read by pnl_monitor.py).

Optional automation:
    pip install pyotp
    Set ZERODHA_USER_ID, ZERODHA_PASSWORD, and ZERODHA_TOTP_SECRET in .env to skip
    the manual login/TOTP steps (same variable names auto_token.py uses).
"""

import os
import sys
import urllib.parse
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ["KITE_API_KEY"]
API_SECRET = os.environ["KITE_API_SECRET"]
ACCESS_TOKEN_PATH = Path(os.getenv("ACCESS_TOKEN_PATH", ".access_token"))
REDIRECT_URL = os.getenv("KITE_REDIRECT_URL", "https://127.0.0.1")

# ---------------------------------------------------------------------------
# Optional: automated TOTP login
# ---------------------------------------------------------------------------

def _try_automated_login() -> str | None:
    """
    If ZERODHA_TOTP_SECRET is set and pyotp is installed, perform a headless
    login and return the request_token without any manual steps.
    Returns None if automation is not configured.
    """
    totp_secret = os.getenv("ZERODHA_TOTP_SECRET", "")
    user_id = os.getenv("ZERODHA_USER_ID", "")
    password = os.getenv("ZERODHA_PASSWORD", "")

    if not (totp_secret and user_id and password):
        return None

    try:
        import pyotp  # noqa: PLC0415
    except ImportError:
        print("pyotp not installed — falling back to manual login.")
        return None

    try:
        session = requests.Session()

        # Step 1: initiate login
        login_url = "https://kite.zerodha.com/api/login"
        r = session.post(login_url, data={"user_id": user_id, "password": password}, timeout=15)
        r.raise_for_status()
        data = r.json()
        request_id = data["data"]["request_id"]

        # Step 2: submit TOTP
        totp = pyotp.TOTP(totp_secret).now()
        twofa_url = "https://kite.zerodha.com/api/twofa"
        r = session.post(
            twofa_url,
            data={"user_id": user_id, "request_id": request_id, "twofa_value": totp},
            timeout=15,
        )
        r.raise_for_status()

        # Step 3: follow redirect to extract request_token
        login_redirect = (
            f"https://kite.zerodha.com/connect/login"
            f"?api_key={API_KEY}&v=3"
        )
        r = session.get(login_redirect, timeout=15, allow_redirects=True)
        parsed = urllib.parse.urlparse(r.url)
        params = urllib.parse.parse_qs(parsed.query)
        token = params.get("request_token", [None])[0]
        if token:
            print("Automated login succeeded.")
        return token

    except Exception as exc:  # noqa: BLE001
        print(f"Automated login failed ({exc}), falling back to manual flow.")
        return None


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------

def exchange_token(request_token: str) -> str:
    """Exchange request_token for an access_token via Kite REST API."""
    import hashlib

    checksum = hashlib.sha256(
        f"{API_KEY}{request_token}{API_SECRET}".encode()
    ).hexdigest()

    url = "https://api.kite.trade/session/token"
    resp = requests.post(
        url,
        data={
            "api_key": API_KEY,
            "request_token": request_token,
            "checksum": checksum,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["data"]["access_token"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("Zerodha Kite — Daily Access Token Generator")
    print("=" * 60)

    request_token = _try_automated_login()

    if not request_token:
        login_url = (
            f"https://kite.zerodha.com/connect/login"
            f"?api_key={API_KEY}&v=3"
        )
        print(f"\nOpen this URL in your browser:\n\n  {login_url}\n")
        print(
            "After logging in you will be redirected to your redirect URL.\n"
            "The URL will contain ?request_token=XXXXXXXX\n"
        )
        request_token = input("Paste the request_token here: ").strip()

    if not request_token:
        print("No request_token provided. Exiting.")
        sys.exit(1)

    print("\nExchanging request_token for access_token...")
    try:
        access_token = exchange_token(request_token)
    except requests.HTTPError as exc:
        print(f"Token exchange failed: {exc}")
        sys.exit(1)

    ACCESS_TOKEN_PATH.write_text(access_token)
    print(f"\nAccess token written to: {ACCESS_TOKEN_PATH.resolve()}")
    print("You can now start (or restart) pnl_monitor.py.\n")


if __name__ == "__main__":
    main()
