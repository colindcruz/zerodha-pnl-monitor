"""
Unit tests for replay_build.py. Sets a dummy KITE_API_KEY before import
(module-level `os.environ["KITE_API_KEY"]` read, same convention as
server.py) — never used for a real network call, since every test here
drives a FakeKite instead of a real KiteConnect session.
"""

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.setdefault("KITE_API_KEY", "test-key-not-real")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from candles import IST
from fixtures import candles_from_closes, steady_trend
import replay_build
from replay_build import ReplayKite, build_replay, most_recent_trading_day

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


REPLAY_DATE = date(2026, 8, 21)


class FakeKite:
    """Mimics the subset of KiteConnect build_replay() actually calls:
    ltp() and historical_data()."""

    def __init__(self, day_candles, prev_day_candles, spot_token=256265):
        self.spot_token = spot_token
        self._day_candles = day_candles
        self._prev_day_candles = prev_day_candles
        self.historical_calls = []

    def ltp(self, symbols):
        last_price = self._day_candles[-1]["close"] if self._day_candles else 24000.0
        return {"NSE:NIFTY 50": {"instrument_token": self.spot_token, "last_price": last_price}}

    def historical_data(self, token, from_date, to_date, interval):
        self.historical_calls.append((token, from_date, to_date, interval))
        return self._prev_day_candles if interval == "day" else self._day_candles


def full_day_candles(n_minutes=375, start_price=24000):
    start = datetime(REPLAY_DATE.year, REPLAY_DATE.month, REPLAY_DATE.day, 9, 15, tzinfo=IST)
    return candles_from_closes(steady_trend(start_price, 0.5, n_minutes), start=start)


prev_day = [{
    "date": datetime(2026, 8, 20, 15, 30, tzinfo=IST),
    "open": 23900, "high": 23950, "low": 23850, "close": 23920, "volume": 1000,
}]


# ============================================================
print("=== most_recent_trading_day ===")
# ============================================================
fake_empty_then_full = FakeKite([], [])


class SequenceKite(FakeKite):
    """Returns empty historical data for the first N calls (simulating a
    weekend/holiday), then real candles — most_recent_trading_day should
    walk backward past the empty days."""

    def __init__(self, empty_days, day_candles):
        super().__init__(day_candles, [])
        self.empty_days = empty_days
        self.call_count = 0

    def historical_data(self, token, from_date, to_date, interval):
        self.call_count += 1
        if self.call_count <= self.empty_days:
            return []
        return self._day_candles


seq_kite = SequenceKite(empty_days=2, day_candles=full_day_candles())
found = most_recent_trading_day(seq_kite, 256265, now=datetime(2026, 8, 22, 12, 0, tzinfo=IST))
check("walks backward past empty (non-trading) days", found == date(2026, 8, 20), str(found))

no_data_kite = SequenceKite(empty_days=99, day_candles=[])
try:
    most_recent_trading_day(no_data_kite, 256265, now=datetime(2026, 8, 22, 12, 0, tzinfo=IST))
    check("raises when no trading day found in 10 days", False)
except RuntimeError:
    check("raises when no trading day found in 10 days", True)


# ============================================================
print("\n=== build_replay: full day, correct bar count ===")
# ============================================================
day_candles = full_day_candles(n_minutes=375)
fake_kite = FakeKite(day_candles, prev_day)

with tempfile.TemporaryDirectory() as d:
    replay_build.REPLAY_DATA_DIR = Path(d)
    out_path = build_replay(REPLAY_DATE, kite=fake_kite)

    check("output file was created", out_path.exists())
    check("output path matches expected naming", out_path.name == f"{REPLAY_DATE}.json", str(out_path))

    snapshots = json.loads(out_path.read_text())
    expected_bars = 375 // 5
    check(f"snapshot count == {expected_bars} (375 one-min candles / 5)", len(snapshots) == expected_bars,
          str(len(snapshots)))

    first, last = snapshots[0], snapshots[-1]
    check("first snapshot's ts is the first 5-min bar (09:19, the 5th one-min candle)",
          first["ts"].startswith("2026-08-21T09:19"), first["ts"])
    check("tick_seq increments monotonically across the whole replay",
          [s["tick_seq"] for s in snapshots] == list(range(1, expected_bars + 1)),
          str([s["tick_seq"] for s in snapshots[:5]]))

    check("positions are empty in every single snapshot (no historical positions API)",
          all(s["positions"] == [] for s in snapshots))
    check("position_health is empty in every snapshot (nothing to track)",
          all(s["position_health"] == {} for s in snapshots))

    check("prev-day pivot level appears once prev-day OHLC was supplied",
          any(lv["name"] == "Pivot (P)" for lv in last["key_levels"]["levels"]), str(last["key_levels"]))

    # Pivot = (23950+23850+23920)/3 = 23906.667
    pivot_lv = next(lv for lv in last["key_levels"]["levels"] if lv["name"] == "Pivot (P)")
    check("pivot value hand-computed from the supplied prev-day OHLC",
          abs(pivot_lv["price"] - 23906.6667) < 0.01, str(pivot_lv["price"]))

    check("trend_age_bars reaches a meaningful value by the end of a steady trending day",
          last["trend_age_bars"] > 1, str(last["trend_age_bars"]))

    check("replay logs were written to REPLAY_DATA_DIR/_logs, not the live service's own log files",
          (Path(d) / "_logs" / f"{REPLAY_DATE}.tick_log.jsonl").exists())


# ============================================================
print("\n=== build_replay: missing prev-day data degrades gracefully ===")
# ============================================================
fake_kite_no_prev = FakeKite(day_candles, [])
with tempfile.TemporaryDirectory() as d:
    replay_build.REPLAY_DATA_DIR = Path(d)
    out_path2 = build_replay(REPLAY_DATE, kite=fake_kite_no_prev)
    snapshots2 = json.loads(out_path2.read_text())
    check("no exception raised despite missing prev-day OHLC", len(snapshots2) > 0)
    check("no Pivot/Prev-Day levels present when prev-day OHLC is unavailable",
          not any(lv["name"].startswith("Prev Day") or lv["name"] == "Pivot (P)"
                  for lv in snapshots2[-1]["key_levels"]["levels"]),
          str(snapshots2[-1]["key_levels"]))


# ============================================================
print("\n=== build_replay: raises a clear error for a day with no data at all ===")
# ============================================================
fake_kite_empty = FakeKite([], [])
with tempfile.TemporaryDirectory() as d:
    replay_build.REPLAY_DATA_DIR = Path(d)
    try:
        build_replay(REPLAY_DATE, kite=fake_kite_empty)
        check("raises RuntimeError for a non-trading day", False)
    except RuntimeError:
        check("raises RuntimeError for a non-trading day", True)


# ============================================================
print("\n=== ReplayKite: positions always empty, historical_data/ltp pass through ===")
# ============================================================
underlying = FakeKite(day_candles, prev_day)
wrapped = ReplayKite(underlying)
check("ReplayKite.positions() always returns empty net", wrapped.positions() == {"net": []})
check("ReplayKite.ltp() passes through to the underlying session",
      wrapped.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["instrument_token"] == 256265)
check("ReplayKite.historical_data() passes through to the underlying session",
      wrapped.historical_data(256265, "x", "y", "minute") == day_candles)


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
