"""
Integration test for state.py's orchestration, against a small fake Kite
object (duck-typed: historical_data/positions/ltp) instead of a live
session — this is the "recorded tick-replay harness" the plan calls for,
in its simplest form. Verifies backfill, recompute wiring, position
poll/diff (entry/exit detection), and JSONL logging all work together
end-to-end.
"""

import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from config import DashboardConfig
from fixtures import SESSION_START, candles_from_closes, default_config, steady_trend, warmup_bars
from state import DashboardState

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


class FakeKite:
    def __init__(self, one_min_candles, prev_day_candles, positions_sequence, spot_token=256265):
        self.spot_token = spot_token
        self._one_min = one_min_candles
        self._prev_day = prev_day_candles
        self._positions_sequence = positions_sequence
        self._positions_call_count = 0

    def ltp(self, symbols):
        return {"NSE:NIFTY 50": {"instrument_token": self.spot_token, "last_price": self._one_min[-1]["close"]}}

    def historical_data(self, token, from_date, to_date, interval):
        return self._prev_day if interval == "day" else self._one_min

    def positions(self):
        idx = min(self._positions_call_count, len(self._positions_sequence) - 1)
        result = self._positions_sequence[idx]
        self._positions_call_count += 1
        return {"net": result}


icfg = default_config()
n = warmup_bars(icfg)
prices = steady_trend(24000, 1.0, n)
one_min = candles_from_closes(prices)
now = one_min[-1]["date"]

prev_day_candles = [{
    "date": (SESSION_START - timedelta(days=1)).replace(hour=15, minute=30),
    "open": 23900, "high": 23950, "low": 23850, "close": 23920, "volume": 1000,
}]

no_positions = []
with_position = [{"exchange": "NFO", "tradingsymbol": "NIFTY26AUG25000CE", "quantity": 50,
                   "average_price": 100.0, "last_price": 110.0}]

positions_sequence = [no_positions, no_positions, with_position, with_position, no_positions]

with tempfile.TemporaryDirectory() as d:
    tick_log = Path(d) / "tick.jsonl"
    trade_log = Path(d) / "trade.jsonl"
    fake_kite = FakeKite(one_min, prev_day_candles, positions_sequence)
    state = DashboardState(
        fake_kite, DashboardConfig(), tick_log_path=tick_log, trade_log_path=trade_log,
        strangle_state_path=Path(d) / "no_strangle.json", hedge_state_path=Path(d) / "no_hedge.json",
        long_option_state_path=Path(d) / "no_lo.json",
    )

    # ============================================================
    print("=== setup: spot token, backfill, prev-day ===")
    # ============================================================
    token = state.resolve_spot_token()
    check("spot token resolved from fake ltp()", token == 256265, str(token))

    state.backfill(now)
    check("backfill populated the accumulator", len(state.accumulator.as_sorted_list()) == len(one_min),
          f"{len(state.accumulator.as_sorted_list())} vs {len(one_min)}")

    state.fetch_prev_day_ohlc(now)
    check("prev-day OHLC resolved", state.prev_day is not None)
    if state.prev_day:
        check("prev-day high correct", state.prev_day.high == 23950, str(state.prev_day.high))

    # ============================================================
    print("\n=== recompute tick 1: no positions ===")
    # ============================================================
    result1 = state.recompute(now)
    check("recompute returns a populated dict", "trend" in result1 and "decision" in result1, str(result1.keys()))
    check("tick_seq starts at 1", result1["tick_seq"] == 1, str(result1["tick_seq"]))
    check("trend score is positive (steady uptrend fixture)", result1["trend"]["score"] > 0, str(result1["trend"]))
    check("no positions -> empty positions list", result1["positions"] == [])
    check("tick log has 1 line after first recompute", len(tick_log.read_text().strip().split("\n")) == 1)

    # ============================================================
    print("\n=== recompute tick 2: still no positions (idempotent) ===")
    # ============================================================
    result2 = state.recompute(now + timedelta(minutes=1))
    check("tick_seq increments", result2["tick_seq"] == 2, str(result2["tick_seq"]))

    # ============================================================
    print("\n=== recompute tick 3: a NIFTY position appears -> ENTRY_DETECTED ===")
    # ============================================================
    result3 = state.recompute(now + timedelta(minutes=2))
    check("position now appears in labeled positions", len(result3["positions"]) == 1, str(result3["positions"]))
    check("position labeled 'manual' (not tracked by any state file)",
          result3["positions"][0]["owner"] == "manual", str(result3["positions"][0]))
    check("a PositionHealthEngine now exists for the symbol",
          "NIFTY26AUG25000CE" in state.position_engines)
    check("position_health present in the returned state",
          "NIFTY26AUG25000CE" in result3["position_health"], str(result3["position_health"].keys()))

    trade_lines = trade_log.read_text().strip().split("\n") if trade_log.exists() else []
    check("ENTRY_DETECTED logged to the trade log", len(trade_lines) == 1, str(trade_lines))
    if trade_lines:
        entry_rec = json.loads(trade_lines[0])
        check("entry event type correct", entry_rec["event"] == "ENTRY_DETECTED", str(entry_rec))
        check("entry price from average_price", entry_rec["entry_price"] == 100.0, str(entry_rec))

    # ============================================================
    print("\n=== recompute tick 4: position still open (no duplicate ENTRY_DETECTED) ===")
    # ============================================================
    state.recompute(now + timedelta(minutes=3))
    trade_lines_after_tick4 = trade_log.read_text().strip().split("\n")
    check("still only 1 trade-log line (no duplicate entry event)", len(trade_lines_after_tick4) == 1,
          str(trade_lines_after_tick4))

    # ============================================================
    print("\n=== recompute tick 5: position disappears -> EXIT_DETECTED ===")
    # ============================================================
    state.recompute(now + timedelta(minutes=4))
    trade_lines_final = trade_log.read_text().strip().split("\n")
    check("EXIT_DETECTED logged (2 lines total now)", len(trade_lines_final) == 2, str(trade_lines_final))
    if len(trade_lines_final) == 2:
        exit_rec = json.loads(trade_lines_final[1])
        check("exit event type correct", exit_rec["event"] == "EXIT_DETECTED", str(exit_rec))
    check("engine/tracked-trade cleaned up after exit", "NIFTY26AUG25000CE" not in state.position_engines)

    # ============================================================
    print("\n=== event feed stays quiet when nothing actually changed ===")
    # ============================================================
    # This scripted run holds a steady uptrend throughout every recompute
    # call (backfill loads the full session once; ticks only move the
    # clock, not the underlying candles), so trend/strength/momentum never
    # transition — the event feed must correctly emit ZERO events across
    # all 5 ticks rather than firing on every recompute just because it was
    # called. (A position appearing/disappearing is intentionally NOT an
    # event-feed transition either, by events.py's own design — see
    # test_events.py's "no event on the first tick"; that's the trade log's
    # job, already verified above via ENTRY/EXIT_DETECTED.)
    check("event feed emitted zero events across 5 ticks of genuinely unchanged classifications",
          state.event_feed.events == [], str(state.event_feed.events))

    # ============================================================
    print("\n=== tick log has one line per recompute call (5 total) ===")
    # ============================================================
    check("tick log has 5 lines", len(tick_log.read_text().strip().split("\n")) == 5,
          str(len(tick_log.read_text().strip().split("\n"))))


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
