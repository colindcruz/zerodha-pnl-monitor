"""
NIFTY Intraday Trade Decision Dashboard — server
==================================================

A small, fully independent process — does NOT touch with-websockets/
pnl_monitor.py, long_option_live.py, or live-dashboard/server.py, their
state files, or their logic. Reads the SAME .access_token file the trading
bot writes/reads (read-only — no login flow of its own), opens its own
read-only Kite WebSocket connection on the NIFTY 50 index (every indicator
except VWAP) plus the current-month NIFTY futures contract (VWAP only — the
index itself carries no real traded volume; see state.py's
resolve_nifty_futures_contract), and serves a browser dashboard converting
live market data into small, actionable decisions.

Phase 1 is advisory-only: this process NEVER places an order. Every
Entry Permission this service emits is something a human reads and then
manually acts on (or doesn't) through some other channel.

Auth pattern, KiteTicker wiring, graceful-shutdown fix, and the
build_app() factory all mirror live-dashboard/server.py's own — see that
file's module docstring and comments for the reasoning behind each; the one
deliberate addition here is on_noreconnect (live-dashboard omits it, but
long_option_live.py includes it) — stale advisory signals going unnoticed
during a lost connection is a real risk for THIS service in particular,
so this mirrors the more cautious of the two existing precedents.

Meant to sit behind a TLS-terminating reverse proxy (Caddy — see
../Caddyfile) on its own nip.io subdomain, the only thing exposed to the
public internet; this process itself binds to 127.0.0.1 only. See
../MANUAL.md section 12 for deployment.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import WSCloseCode, WSMsgType, web
from dotenv import load_dotenv
from kiteconnect import KiteConnect, KiteTicker

from config import DashboardConfig
from state import DashboardState

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nifty-decision-dashboard")

# ============================================================
# CONFIG
# ============================================================

API_KEY = os.environ["KITE_API_KEY"]
ACCESS_TOKEN_PATH = Path(os.getenv("ACCESS_TOKEN_PATH", "../.access_token"))

DECISION_DASHBOARD_PASSWORD = os.environ["DECISION_DASHBOARD_PASSWORD"]
DECISION_DASHBOARD_SESSION_SECRET = os.environ["DECISION_DASHBOARD_SESSION_SECRET"]
DECISION_DASHBOARD_PORT = int(os.getenv("DECISION_DASHBOARD_PORT", "8766"))

RECOMPUTE_INTERVAL_SECONDS = int(os.getenv("RECOMPUTE_INTERVAL_SECONDS", "15"))
POSITIONS_POLL_SECONDS = int(os.getenv("POSITIONS_POLL_SECONDS", "20"))

STRANGLE_STATE_FILE = Path(os.getenv("STRANGLE_STATE_FILE", "../strangle_state.json"))
HEDGE_STATE_FILE = Path(os.getenv("HEDGE_STATE_FILE", "../hedge_state.json"))
LONG_OPTION_STATE_FILE = Path(os.getenv("LONG_OPTION_STATE_FILE", "../long_option_state.json"))
TICK_LOG_FILE = Path(os.getenv("TICK_LOG_FILE", "nifty_decision_tick_log.jsonl"))
TRADE_LOG_FILE = Path(os.getenv("TRADE_LOG_FILE", "nifty_decision_trade_log.jsonl"))

# Prebuilt historical-replay snapshots (see replay_build.py) — one JSON file
# per date, each an array of state-payload dicts (same shape as
# DashboardState.latest), one entry per 5-min bar. Built offline/on-demand,
# never written by server.py itself.
REPLAY_DATA_DIR = Path(os.getenv("REPLAY_DATA_DIR", "replay_data"))
_REPLAY_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

IST = ZoneInfo("Asia/Kolkata")
COOKIE_NAME = "decision_dashboard_session"
SESSION_MAX_AGE_SECONDS = 30 * 86400  # 30 days

HERE = Path(__file__).parent
DASHBOARD_HTML = (HERE / "dashboard.html").read_text(encoding="utf-8")
LOGIN_HTML = (HERE / "login.html").read_text(encoding="utf-8")
REPLAY_HTML = (HERE / "replay.html").read_text(encoding="utf-8")
DASHBOARD_RENDER_JS = (HERE / "dashboard_render.js").read_text(encoding="utf-8")


def _read_access_token() -> str:
    """Own small copy, not a shared import — see live-dashboard/server.py's
    identical function/docstring for why (this repo's established
    convention: per-feature duplication over coupling unrelated services to
    the live trading bot's file)."""
    if ACCESS_TOKEN_PATH.exists():
        token = ACCESS_TOKEN_PATH.read_text().strip()
        if token:
            return token
    token = os.getenv("KITE_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("No access token found. Run generate_token.py first.")
    return token


# ============================================================
# SESSION AUTH — identical scheme to live-dashboard/server.py
# ============================================================

def make_session_cookie() -> str:
    issued = str(int(time.time()))
    sig = hmac.new(DECISION_DASHBOARD_SESSION_SECRET.encode(), issued.encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{sig}"


def verify_session_cookie(value: str) -> bool:
    if not value or "." not in value:
        return False
    issued_str, _, sig = value.partition(".")
    expected = hmac.new(DECISION_DASHBOARD_SESSION_SECRET.encode(), issued_str.encode(), hashlib.sha256).hexdigest()
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
    if not hmac.compare_digest(str(password), DECISION_DASHBOARD_PASSWORD):
        body = LOGIN_HTML.replace("<!--ERROR-->", '<p class="error">Wrong password.</p>')
        return web.Response(text=body, content_type="text/html", status=401)
    resp = web.HTTPFound("/")
    resp.set_cookie(COOKIE_NAME, make_session_cookie(), max_age=SESSION_MAX_AGE_SECONDS,
                     httponly=True, secure=True, samesite="Lax")
    return resp


async def get_logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE_NAME)
    return resp


async def get_dashboard(request: web.Request) -> web.Response:
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


async def handle_state(request: web.Request) -> web.Response:
    return web.json_response(dashboard_state.latest or {"status": "warming up"})


async def get_dashboard_render_js(request: web.Request) -> web.Response:
    return web.Response(text=DASHBOARD_RENDER_JS, content_type="application/javascript")


async def get_replay_page(request: web.Request) -> web.Response:
    return web.Response(text=REPLAY_HTML, content_type="text/html")


async def handle_replay_dates(request: web.Request) -> web.Response:
    """Every date this dashboard has a prebuilt historical replay for (see
    replay_build.py) — the replay page's own date picker, not live state."""
    if not REPLAY_DATA_DIR.exists():
        return web.json_response({"dates": []})
    dates = sorted(p.stem for p in REPLAY_DATA_DIR.glob("*.json") if _REPLAY_DATE_RE.match(p.stem))
    return web.json_response({"dates": dates})


async def handle_replay_data(request: web.Request) -> web.Response:
    date = request.match_info.get("date", "")
    if not _REPLAY_DATE_RE.match(date):
        return web.json_response({"error": "date must be YYYY-MM-DD"}, status=400)
    path = REPLAY_DATA_DIR / f"{date}.json"
    if not path.is_file():
        return web.json_response({"error": f"no replay built for {date}"}, status=404)
    return web.Response(text=path.read_text(encoding="utf-8"), content_type="application/json")


# ============================================================
# KITE — read-only session, one KiteTicker connection subscribed to both
# the NIFTY 50 index and the current-month NIFTY futures contract (VWAP source)
# ============================================================

kite = KiteConnect(api_key=API_KEY)
access_token = _read_access_token()
kite.set_access_token(access_token)
kws = KiteTicker(API_KEY, access_token)

dashboard_state = DashboardState(
    kite, DashboardConfig(), tick_log_path=TICK_LOG_FILE, trade_log_path=TRADE_LOG_FILE,
    strangle_state_path=STRANGLE_STATE_FILE, hedge_state_path=HEDGE_STATE_FILE,
    long_option_state_path=LONG_OPTION_STATE_FILE,
)

ws_clients: set[web.WebSocketResponse] = set()
clients_lock = threading.Lock()
main_loop: asyncio.AbstractEventLoop | None = None


async def _safe_send(client: web.WebSocketResponse, payload: str) -> None:
    try:
        await client.send_str(payload)
    except Exception:
        pass


async def broadcast_state() -> None:
    payload = json.dumps({"type": "state", "data": dashboard_state.latest})
    with clients_lock:
        targets = list(ws_clients)
    for client in targets:
        await _safe_send(client, payload)


def on_ticks(ws, ticks):
    for tick in ticks:
        token = tick.get("instrument_token")
        ltt = tick.get("last_trade_time")
        ts = ltt if hasattr(ltt, "isoformat") else datetime.now(IST)
        if token == dashboard_state.spot_token:
            dashboard_state.on_tick(ts, tick.get("last_price"), tick.get("volume_traded"))
        elif token == dashboard_state.futures_token:
            dashboard_state.on_futures_tick(ts, tick.get("last_price"), tick.get("volume_traded"))


def on_connect(ws, response):
    log.info("Kite ticker connected")
    tokens = [dashboard_state.resolve_spot_token()]
    if dashboard_state.futures_token:
        tokens.append(dashboard_state.futures_token)
    ws.subscribe(tokens)
    ws.set_mode(ws.MODE_FULL, tokens)


def on_close(ws, code, reason):
    log.warning("Kite ticker closed: %s %s", code, reason)


def on_error(ws, code, reason):
    log.error("Kite ticker error: %s %s", code, reason)


def on_reconnect(ws, attempt):
    log.warning("Kite ticker reconnecting (attempt %d)", attempt)


def on_noreconnect(ws):
    """live-dashboard/server.py doesn't implement this; long_option_live.py
    does. This service is a live advisory signal a human is meant to trust
    in the moment — a silently-stale dashboard is a worse failure mode here
    than for a passive price viewer, so this mirrors the more cautious
    precedent rather than the simpler one."""
    log.error("Kite ticker gave up reconnecting.")


kws.on_ticks = on_ticks
kws.on_connect = on_connect
kws.on_close = on_close
kws.on_error = on_error
kws.on_reconnect = on_reconnect
kws.on_noreconnect = on_noreconnect


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    with clients_lock:
        ws_clients.add(ws)
    try:
        await _safe_send(ws, json.dumps({"type": "state", "data": dashboard_state.latest}))
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
            # No client->server messages are meaningful here (read-only feed) — just
            # keep the loop alive until the client disconnects.
    finally:
        with clients_lock:
            ws_clients.discard(ws)
    return ws


# ============================================================
# BACKGROUND LOOP — periodic recompute, independent of tick arrival rate
# (a quiet market shouldn't stall the dashboard's own recompute cadence)
# ============================================================

async def _recompute_loop() -> None:
    while True:
        try:
            dashboard_state.recompute(datetime.now(IST))
            await broadcast_state()
        except Exception:
            log.exception("recompute tick failed")
        await asyncio.sleep(RECOMPUTE_INTERVAL_SECONDS)


# ============================================================
# APP
# ============================================================

async def on_startup(app: web.Application) -> None:
    global main_loop
    main_loop = asyncio.get_event_loop()
    dashboard_state.resolve_spot_token()
    dashboard_state.backfill(datetime.now(IST))
    dashboard_state.fetch_prev_day_ohlc(datetime.now(IST))
    # Resolved BEFORE kws.connect() so on_connect()'s initial subscribe
    # already knows about it — VWAP just stays sourced from the index (its
    # own TWAP fallback) if this comes back empty, never fatal.
    dashboard_state.resolve_futures_token(datetime.now(IST))
    dashboard_state.backfill_futures(datetime.now(IST))
    kws.connect(threaded=True)
    app["recompute_task"] = asyncio.create_task(_recompute_loop())


async def on_shutdown(app: web.Application) -> None:
    """Proactively closes every open browser WebSocket connection — same fix
    live-dashboard/server.py applies, same reason: without it a single open
    tab turns SIGTERM into a multi-second hang instead of an immediate
    clean exit."""
    task = app.get("recompute_task")
    if task:
        task.cancel()
    with clients_lock:
        clients = list(ws_clients)
    for ws in clients:
        try:
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutting down")
        except Exception:
            pass


def build_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/login", get_login)
    app.router.add_post("/login", post_login)
    app.router.add_get("/logout", get_logout)
    app.router.add_get("/", get_dashboard)
    app.router.add_get("/dashboard_render.js", get_dashboard_render_js)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/replay", get_replay_page)
    app.router.add_get("/api/replay-dates", handle_replay_dates)
    app.router.add_get("/api/replay/{date}", handle_replay_data)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="127.0.0.1", port=DECISION_DASHBOARD_PORT)
