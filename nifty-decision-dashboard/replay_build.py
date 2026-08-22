"""
Builds a prebuilt historical replay file for the /replay page (see
server.py's /api/replay-dates + /api/replay/{date}, and replay.html): feeds
one full real trading day's NIFTY 1-min candles through the EXACT same
DashboardState/engine pipeline server.py uses live, capturing one snapshot
per 5-min bar close (starting 09:15 IST) — the same cadence the Trend and
Position Management engines already run at (see state.py's bar-gating
module docstring). The resulting file is what lets you step through a real
day bar-by-bar with Prev/Next in the browser.

Run manually, on demand, for whichever day you want to review:

    venv/bin/python replay_build.py 2026-08-21
    venv/bin/python replay_build.py            # most recent trading day

Deliberately NOT part of server.py's own runtime, and never invoked
automatically — this reads real Kite historical data (safe, read-only,
same shared .access_token every other service here already uses) but only
ever WRITES to replay_data/, never to the live service's own logs or any
other system's state files.

Positions are intentionally always empty in every replay: Kite's
positions() API only ever returns TODAY's current holdings, never a
point-in-time snapshot of what was held on some past date — showing
"today's real positions" superimposed on a HISTORICAL day's price action
would be actively misleading, not just unhelpful. The Position Management
and Open Position panels are simply blank throughout every replay; this is
a Trend/Entry/Location/Decision review tool, not a position-history tool.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from kiteconnect import KiteConnect

from candles import IST, normalize_historical
from config import DashboardConfig
from key_levels import PrevDayOHLC
from state import DashboardState

load_dotenv()

API_KEY = os.environ["KITE_API_KEY"]
ACCESS_TOKEN_PATH = Path(os.getenv("ACCESS_TOKEN_PATH", "../.access_token"))
REPLAY_DATA_DIR = Path(os.getenv("REPLAY_DATA_DIR", "replay_data"))
BARS_PER_STEP = 5  # 5-min bars — matches Trend/Position Management's own bar cadence


def _read_access_token() -> str:
    """Own small copy, same convention as server.py's — see that module's
    docstring for why this isn't a shared import."""
    if ACCESS_TOKEN_PATH.exists():
        token = ACCESS_TOKEN_PATH.read_text().strip()
        if token:
            return token
    token = os.getenv("KITE_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("No access token found. Run generate_token.py first.")
    return token


class ReplayKite:
    """Wraps a real, read-only KiteConnect session — historical_data/ltp
    pass straight through, but positions() always reports empty (see
    module docstring: Kite has no historical positions API, so a replay
    can never show what was actually held on a past day)."""

    def __init__(self, kite: KiteConnect):
        self._kite = kite

    def ltp(self, symbols):
        return self._kite.ltp(symbols)

    def historical_data(self, token, from_date, to_date, interval):
        return self._kite.historical_data(token, from_date, to_date, interval)

    def positions(self):
        return {"net": []}


def most_recent_trading_day(kite: KiteConnect, spot_token: int, now: datetime = None) -> date:
    """Walks backward from today until kite.historical_data actually
    returns candles for that date — checked against real historical data
    rather than a weekday-only heuristic, so it correctly skips holidays
    too, not just weekends."""
    now = now or datetime.now(IST)
    probe = now.date()
    for _ in range(10):
        candles = kite.historical_data(spot_token, f"{probe} 09:15:00", f"{probe} 15:30:00", "minute")
        if candles:
            return probe
        probe -= timedelta(days=1)
    raise RuntimeError("Could not find a recent trading day with historical data in the last 10 days")


def build_replay(replay_date: date, kite=None) -> Path:
    """`kite` is optional and duck-typed (needs historical_data + ltp) —
    lets this be exercised in tests against a fake session; main() always
    calls it with the real, live-session default."""
    if kite is None:
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(_read_access_token())

    spot_info = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]
    spot_token = spot_info["instrument_token"]

    day_start = datetime(replay_date.year, replay_date.month, replay_date.day, 9, 15, tzinfo=IST)
    day_end = datetime(replay_date.year, replay_date.month, replay_date.day, 15, 30, tzinfo=IST)
    raw = kite.historical_data(spot_token, day_start.strftime("%Y-%m-%d %H:%M:%S"),
                                day_end.strftime("%Y-%m-%d %H:%M:%S"), "minute")
    if not raw:
        raise RuntimeError(f"No historical candles for {replay_date} — either not a trading day, or older "
                            f"than Kite's historical-data retention window.")
    candles = normalize_historical(raw)
    print(f"Fetched {len(candles)} one-min candles for {replay_date}.")

    prev_raw = kite.historical_data(spot_token, (replay_date - timedelta(days=10)).isoformat(),
                                     replay_date.isoformat(), "day")
    prev_candles = [c for c in normalize_historical(prev_raw) if c["date"].date() < replay_date] if prev_raw else []

    replay_kite = ReplayKite(kite)
    logs_dir = REPLAY_DATA_DIR / "_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    state = DashboardState(
        replay_kite, DashboardConfig(),
        tick_log_path=logs_dir / f"{replay_date}.tick_log.jsonl",
        trade_log_path=logs_dir / f"{replay_date}.trade_log.jsonl",
        strangle_state_path=REPLAY_DATA_DIR / "_no_strangle.json",
        hedge_state_path=REPLAY_DATA_DIR / "_no_hedge.json",
        long_option_state_path=REPLAY_DATA_DIR / "_no_long_option.json",
    )
    state.spot_token = spot_token

    if prev_candles:
        last = prev_candles[-1]
        state.prev_day = PrevDayOHLC(high=last["high"], low=last["low"], close=last["close"])
        print(f"Prev-day OHLC: H={last['high']} L={last['low']} C={last['close']}")
    else:
        print("WARNING: no prev-day OHLC found — Prev Day / pivot levels will be absent from every bar.")

    snapshots = []
    for i in range(BARS_PER_STEP, len(candles) + 1, BARS_PER_STEP):
        state.accumulator.seed_from_historical(candles[:i])
        step_time = candles[i - 1]["date"]
        snapshots.append(state.recompute(step_time))

    REPLAY_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPLAY_DATA_DIR / f"{replay_date}.json"
    out_path.write_text(json.dumps(snapshots))
    print(f"Wrote {len(snapshots)} bar snapshots ({BARS_PER_STEP}-min each, starting 09:15 IST) to {out_path}")
    return out_path


def main() -> None:
    if len(sys.argv) > 1:
        replay_date = date.fromisoformat(sys.argv[1])
    else:
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(_read_access_token())
        spot_token = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["instrument_token"]
        replay_date = most_recent_trading_day(kite, spot_token)
        print(f"No date given — using most recent trading day: {replay_date}")
    build_replay(replay_date)


if __name__ == "__main__":
    main()
