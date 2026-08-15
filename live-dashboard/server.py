"""
Live NIFTY strangle-leg dashboard — server
===========================================

A small, fully independent process: does NOT touch with-websockets/pnl_monitor.py,
its state files, or its logic. Reads the same .access_token file the live trading bot
writes/reads, opens its own read-only Kite WebSocket connection, and serves a browser
dashboard with a candle-timeframe selector and CE/PE strike dropdowns.

Read-only by design: no order-placement endpoint exists anywhere in this file.

Meant to sit behind a TLS-terminating reverse proxy (Caddy — see ../Caddyfile) that is
the only thing exposed to the public internet; this process itself binds to
127.0.0.1 only. See ../MANUAL.md section 11 for deployment.

Secrets come from a .env file in this directory (or the parent — python-dotenv walks
up) — see .env.example in the project root for the new DASHBOARD_* variables, or copy
this directory's own template.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web, WSMsgType
from dotenv import load_dotenv
from kiteconnect import KiteConnect, KiteTicker

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("live-dashboard")

# ============================================================
# CONFIG
# ============================================================

API_KEY = os.environ["KITE_API_KEY"]
ACCESS_TOKEN_PATH = Path(os.getenv("ACCESS_TOKEN_PATH", "../.access_token"))

DASHBOARD_PASSWORD = os.environ["DASHBOARD_PASSWORD"]
DASHBOARD_SESSION_SECRET = os.environ["DASHBOARD_SESSION_SECRET"]
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8765"))

STRIKE_STEP = 50  # NIFTY strike spacing. Change to 100 if this is ever pointed at Bank Nifty.
IST = ZoneInfo("Asia/Kolkata")
INDEX_SPOT_SYMBOL = "NSE:NIFTY 50"

COOKIE_NAME = "dashboard_session"
SESSION_MAX_AGE_SECONDS = 30 * 86400  # 30 days

HERE = Path(__file__).parent
DASHBOARD_HTML = (HERE / "dashboard.html").read_text(encoding="utf-8")
LOGIN_HTML = (HERE / "login.html").read_text(encoding="utf-8")


def _read_access_token() -> str:
    """Mirrors with-websockets/pnl_monitor.py:_read_access_token — same file, same
    fallback order, kept as its own small copy rather than a shared import (this repo's
    established convention: dedicated per-feature functions, not shared generic helpers,
    to keep the live trading bot's file untouched by unrelated features)."""
    if ACCESS_TOKEN_PATH.exists():
        token = ACCESS_TOKEN_PATH.read_text().strip()
        if token:
            return token
    token = os.getenv("KITE_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("No access token found. Run generate_token.py first.")
    return token


# ============================================================
# SESSION AUTH — simple password + signed cookie, single user
# ============================================================

def make_session_cookie() -> str:
    issued = str(int(time.time()))
    sig = hmac.new(DASHBOARD_SESSION_SECRET.encode(), issued.encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{sig}"


def verify_session_cookie(value: str) -> bool:
    if not value or "." not in value:
        return False
    issued_str, _, sig = value.partition(".")
    expected = hmac.new(DASHBOARD_SESSION_SECRET.encode(), issued_str.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        issued = int(issued_str)
    except ValueError:
        return False
    return (time.time() - issued) < SESSION_MAX_AGE_SECONDS


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path == "/login":
        return await handler(request)
    cookie = request.cookies.get(COOKIE_NAME, "")
    if not verify_session_cookie(cookie):
        if request.path == "/ws":
            raise web.HTTPUnauthorized()
        raise web.HTTPFound("/login")
    return await handler(request)


async def get_login(request: web.Request) -> web.Response:
    return web.Response(text=LOGIN_HTML, content_type="text/html")


async def post_login(request: web.Request) -> web.Response:
    data = await request.post()
    password = data.get("password", "")
    if not hmac.compare_digest(str(password), DASHBOARD_PASSWORD):
        body = LOGIN_HTML.replace("<!--ERROR-->", '<p class="error">Wrong password.</p>')
        return web.Response(text=body, content_type="text/html", status=401)
    resp = web.HTTPFound("/")
    resp.set_cookie(
        COOKIE_NAME, make_session_cookie(),
        max_age=SESSION_MAX_AGE_SECONDS, httponly=True, secure=True, samesite="Lax",
    )
    return resp


async def get_logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE_NAME)
    return resp


async def get_dashboard(request: web.Request) -> web.Response:
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


# ============================================================
# KITE — instrument cache + strike resolution
# (mirrors with-websockets/pnl_monitor.py:_resolve_strangle_legs's ATM/expiry math,
# but caches the NFO dump in memory instead of calling kite.instruments() on every
# request — the bot only resolves strikes twice a day, this dashboard would otherwise
# re-download the full dump on every dropdown load)
# ============================================================

_instrument_cache: dict = {"nifty_options": [], "fetched_at": 0.0}


def _norm_expiry(i: dict):
    e = i["expiry"]
    return e.date() if isinstance(e, datetime) else e


def refresh_instrument_cache(kite: KiteConnect) -> None:
    instruments = kite.instruments("NFO")
    nifty_options = [i for i in instruments if i["name"] == "NIFTY" and i["instrument_type"] in ("CE", "PE")]
    _instrument_cache["nifty_options"] = nifty_options
    _instrument_cache["fetched_at"] = time.time()
    log.info("Instrument cache refreshed: %d NIFTY option rows", len(nifty_options))


def _current_expiry():
    today = date.today()
    upcoming = sorted({_norm_expiry(i) for i in _instrument_cache["nifty_options"] if _norm_expiry(i) >= today})
    return upcoming[0] if upcoming else None


async def handle_strikes(request: web.Request) -> web.Response:
    opt_type = request.query.get("type", "CE").upper()
    if opt_type not in ("CE", "PE"):
        return web.json_response({"error": "type must be CE or PE"}, status=400)

    try:
        spot = kite.ltp([INDEX_SPOT_SYMBOL])[INDEX_SPOT_SYMBOL]["last_price"]
    except Exception as exc:
        return web.json_response({"error": f"could not fetch spot: {exc}"}, status=502)

    atm = round(spot / STRIKE_STEP) * STRIKE_STEP
    expiry = _current_expiry()
    if expiry is None:
        return web.json_response({"error": "no upcoming NIFTY expiry found"}, status=502)

    rows = _instrument_cache["nifty_options"]
    strikes = []
    for n in range(-10, 11):
        strike = atm + n * STRIKE_STEP
        match = next(
            (i for i in rows if _norm_expiry(i) == expiry and float(i["strike"]) == strike and i["instrument_type"] == opt_type),
            None,
        )
        if not match:
            continue
        otm = n if opt_type == "CE" else -n
        if otm > 0:
            label = f"{int(strike)} {opt_type} ({otm} OTM)"
        elif otm < 0:
            label = f"{int(strike)} {opt_type} ({-otm} ITM)"
        else:
            label = f"{int(strike)} {opt_type} (ATM)"
        strikes.append({
            "strike": strike,
            "tradingsymbol": match["tradingsymbol"],
            "instrument_token": match["instrument_token"],
            "otm": otm,
            "label": label,
        })

    return web.json_response({"atm": atm, "spot": spot, "expiry": expiry.isoformat(), "strikes": strikes})


async def handle_historical(request: web.Request) -> web.Response:
    token = request.query.get("token")
    interval = request.query.get("interval", "minute")
    from_str = request.query.get("from")
    to_str = request.query.get("to")
    if not token or not from_str or not to_str:
        return web.json_response({"error": "token, from, to are required"}, status=400)

    try:
        candles = kite.historical_data(int(token), from_str, to_str, interval)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=502)

    out = [
        {
            "date": c["date"].isoformat() if hasattr(c["date"], "isoformat") else c["date"],
            "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"],
            "volume": c.get("volume", 0),
        }
        for c in candles
    ]
    return web.json_response({"candles": out})


# ============================================================
# LIVE TICKER — one shared upstream KiteTicker connection, fanned out to whichever
# browser WebSocket clients currently care about each instrument_token. Unlike the
# trading bot's ticker (which only ever grows its subscription set), this one performs
# real subscribe/unsubscribe via a per-token refcount, since strike selections change
# far more often here.
# ============================================================

clients_lock = threading.Lock()
ws_clients: dict[web.WebSocketResponse, set[int]] = {}
token_refcount: dict[int, int] = {}
main_loop: asyncio.AbstractEventLoop | None = None

kite = KiteConnect(api_key=API_KEY)
access_token = _read_access_token()
kite.set_access_token(access_token)
kws = KiteTicker(API_KEY, access_token)


def _ticker_subscribe(tokens: list[int]) -> None:
    with clients_lock:
        new_tokens = []
        for t in tokens:
            token_refcount[t] = token_refcount.get(t, 0) + 1
            if token_refcount[t] == 1:
                new_tokens.append(t)
    if new_tokens:
        kws.subscribe(new_tokens)
        kws.set_mode(kws.MODE_FULL, new_tokens)


def _ticker_unsubscribe(tokens: list[int]) -> None:
    with clients_lock:
        remove_tokens = []
        for t in tokens:
            if t not in token_refcount:
                continue
            token_refcount[t] -= 1
            if token_refcount[t] <= 0:
                del token_refcount[t]
                remove_tokens.append(t)
    if remove_tokens:
        kws.unsubscribe(remove_tokens)


async def _safe_send(client: web.WebSocketResponse, msg: str) -> None:
    try:
        await client.send_str(msg)
    except Exception:
        pass


def on_ticks(ws, ticks):
    for tick in ticks:
        token = tick["instrument_token"]
        ltt = tick.get("last_trade_time")
        payload = json.dumps({
            "type": "tick",
            "instrument_token": token,
            "last_price": tick.get("last_price"),
            "timestamp": ltt.isoformat() if hasattr(ltt, "isoformat") else datetime.now(IST).isoformat(),
        })
        with clients_lock:
            targets = [c for c, toks in ws_clients.items() if token in toks]
        if targets and main_loop is not None:
            for client in targets:
                asyncio.run_coroutine_threadsafe(_safe_send(client, payload), main_loop)


def on_connect(ws, response):
    log.info("Kite ticker connected")
    with clients_lock:
        tokens = list(token_refcount.keys())
    if tokens:
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)


def on_close(ws, code, reason):
    log.warning("Kite ticker closed: %s %s", code, reason)


def on_error(ws, code, reason):
    log.error("Kite ticker error: %s %s", code, reason)


def on_reconnect(ws, attempt):
    log.warning("Kite ticker reconnecting (attempt %d)", attempt)


kws.on_ticks = on_ticks
kws.on_connect = on_connect
kws.on_close = on_close
kws.on_error = on_error
kws.on_reconnect = on_reconnect


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    with clients_lock:
        ws_clients[ws] = set()

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                if msg.type == WSMsgType.ERROR:
                    break
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            mtype = data.get("type")
            try:
                tokens = [int(t) for t in data.get("tokens", [])]
            except (TypeError, ValueError):
                continue
            if not tokens:
                continue
            if mtype == "subscribe":
                with clients_lock:
                    ws_clients[ws] |= set(tokens)
                _ticker_subscribe(tokens)
            elif mtype == "unsubscribe":
                with clients_lock:
                    ws_clients[ws] -= set(tokens)
                _ticker_unsubscribe(tokens)
    finally:
        with clients_lock:
            leftover = list(ws_clients.pop(ws, set()))
        if leftover:
            _ticker_unsubscribe(leftover)

    return ws


# ============================================================
# APP
# ============================================================

async def on_startup(app: web.Application) -> None:
    global main_loop
    main_loop = asyncio.get_event_loop()
    refresh_instrument_cache(kite)
    kws.connect(threaded=True)


def build_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/login", get_login)
    app.router.add_post("/login", post_login)
    app.router.add_get("/logout", get_logout)
    app.router.add_get("/", get_dashboard)
    app.router.add_get("/api/strikes", handle_strikes)
    app.router.add_get("/api/historical", handle_historical)
    app.router.add_get("/ws", handle_ws)
    app.on_startup.append(on_startup)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="127.0.0.1", port=DASHBOARD_PORT)
