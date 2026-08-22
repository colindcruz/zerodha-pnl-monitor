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


# ============================================================
print("=== DashboardState._prev_5min_close ===")
# ============================================================
from snapshot import build_snapshot  # noqa: E402

_icfg = default_config()
_one_bar_snap = build_snapshot(candles_from_closes(steady_trend(24000, 1.0, 5)), _icfg)  # 1 tf5 bar only
check("only 1 tf5 bar -> prev_5min_close is None", DashboardState._prev_5min_close(_one_bar_snap) is None)

_two_bar_candles = candles_from_closes([24000.0] * 5 + [24010.0] * 5)  # bar1 closes 24000, bar2 closes 24010
_two_bar_snap = build_snapshot(_two_bar_candles, _icfg)
check("2 tf5 bars -> prev_5min_close is the FIRST (completed) bar's close, not the current one",
      DashboardState._prev_5min_close(_two_bar_snap) == 24000.0, str(DashboardState._prev_5min_close(_two_bar_snap)))


from datetime import date  # noqa: E402
from state import resolve_nifty_futures_contract  # noqa: E402


class FakeInstrumentsKite:
    """Minimal fake — only instruments() — for testing
    resolve_nifty_futures_contract() in isolation."""

    def __init__(self, instruments=None, raise_on_call=False):
        self._instruments = instruments or []
        self._raise = raise_on_call

    def instruments(self, exchange):
        if self._raise:
            raise RuntimeError("simulated API failure")
        return self._instruments


def _fut(name, expiry, token, symbol):
    return {"name": name, "instrument_type": "FUT", "expiry": expiry, "instrument_token": token,
            "tradingsymbol": symbol}


# ============================================================
print("\n=== resolve_nifty_futures_contract ===")
# ============================================================
multi_expiry = [
    _fut("NIFTY", date(2026, 8, 27), 111, "NIFTY26AUGFUT"),   # already expired as of 2026-08-28
    _fut("NIFTY", date(2026, 9, 24), 222, "NIFTY26SEPFUT"),   # nearest unexpired
    _fut("NIFTY", date(2026, 10, 29), 333, "NIFTY26OCTFUT"),  # further out
    _fut("BANKNIFTY", date(2026, 9, 24), 444, "BANKNIFTY26SEPFUT"),  # must be excluded (wrong name)
]
token, symbol = resolve_nifty_futures_contract(FakeInstrumentsKite(multi_expiry), date(2026, 8, 28))
check("picks the nearest UNEXPIRED NIFTY contract, skipping the already-expired one",
      token == 222 and symbol == "NIFTY26SEPFUT", f"{token} {symbol}")

token2, symbol2 = resolve_nifty_futures_contract(FakeInstrumentsKite([]), date(2026, 8, 28))
check("no instruments at all -> (None, None), no exception", token2 is None and symbol2 is None)

token3, symbol3 = resolve_nifty_futures_contract(
    FakeInstrumentsKite([_fut("BANKNIFTY", date(2026, 9, 24), 444, "BANKNIFTY26SEPFUT")]), date(2026, 8, 28))
check("only BANKNIFTY present (no NIFTY) -> (None, None)", token3 is None and symbol3 is None)

token4, symbol4 = resolve_nifty_futures_contract(FakeInstrumentsKite(raise_on_call=True), date(2026, 8, 28))
check("instruments() raising -> (None, None), never propagates", token4 is None and symbol4 is None)


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

# 6 entries for 6 total recompute() calls in this test: tick1, tick2, tick3,
# the bar-close tick inserted between tick3 and tick4, tick4, tick5.
positions_sequence = [no_positions, no_positions, with_position, with_position, with_position, no_positions]

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
    check("event feed still quiet (identical candle data, no new bar, no position)",
          state.event_feed.events == [], str(state.event_feed.events))

    # ============================================================
    print("\n=== recompute tick 3: a NIFTY position appears -> ENTRY_DETECTED ===")
    # ============================================================
    result3 = state.recompute(now + timedelta(minutes=2))
    check("position now appears in labeled positions", len(result3["positions"]) == 1, str(result3["positions"]))
    check("position labeled 'manual' (not tracked by any state file)",
          result3["positions"][0]["owner"] == "manual", str(result3["positions"][0]))
    check("a PositionHealthEngine now exists for the symbol",
          "NIFTY26AUG25000CE" in state.position_engines)
    # Position Management is bar-gated (see state.py's module docstring): no
    # NEW 5-min bar has closed since backfill, so its health isn't assessed
    # THIS tick — that's correct, not a gap. Confirmed once a genuinely new
    # bar closes, just below.
    check("position_health NOT yet populated this tick (no new 5-min bar has closed)",
          "NIFTY26AUG25000CE" not in result3["position_health"], str(result3["position_health"].keys()))

    bar_close_time = one_min[-1]["date"] + timedelta(minutes=5)
    state.on_tick(bar_close_time, one_min[-1]["close"])
    result3b = state.recompute(bar_close_time)
    check("position_health populated once a new 5-min bar actually closes",
          "NIFTY26AUG25000CE" in result3b["position_health"], str(result3b["position_health"].keys()))
    check("trend_age_bars advances on a new bar", result3b["trend_age_bars"] >= 1, str(result3b["trend_age_bars"]))
    check("vote_persistence populated once a bar has closed", bool(result3b["vote_persistence"]),
          str(result3b["vote_persistence"]))
    check("prev_5min_close is populated once enough bars exist", result3b["prev_5min_close"] is not None,
          str(result3b["prev_5min_close"]))

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
    events_before_tick5 = len(state.event_feed.events)

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
    check("a position disappearing alone fires no NEW event-feed transition (that's the trade log's job)",
          len(state.event_feed.events) == events_before_tick5, str(state.event_feed.events[events_before_tick5:]))

    # ============================================================
    print("\n=== tick log has one line per recompute call (6 total) ===")
    # ============================================================
    # tick1, tick2, tick3, the bar-close tick between tick3/tick4, tick4, tick5.
    check("tick log has 6 lines", len(tick_log.read_text().strip().split("\n")) == 6,
          str(len(tick_log.read_text().strip().split("\n"))))

    # ============================================================
    print("\n=== self.latest stays JSON-serializable once a real event fires ===")
    # ============================================================
    # Regression test: Event.timestamp is a raw datetime; a naive asdict()
    # (as opposed to state.py's _event_asdict()) leaves it as one, which
    # server.py's plain json.dumps(dashboard_state.latest) in
    # broadcast_state()/handle_ws() would then raise TypeError on — but
    # only once a REAL transition actually fires, which this scripted run
    # otherwise never triggers (steady, unchanging data throughout). Forcing
    # one directly here is what catches it.
    state.event_feed.update(now, trend_direction="BULL")
    state.event_feed.update(now + timedelta(minutes=1), trend_direction="STRONG_BULL")
    state.recompute(now + timedelta(minutes=5))
    check("events list is non-empty after forcing a real transition", len(state.latest["events"]) > 0,
          str(state.latest["events"]))
    try:
        json.dumps(state.latest)
        check("json.dumps(state.latest) succeeds with a real event present", True)
    except TypeError as exc:
        check("json.dumps(state.latest) succeeds with a real event present", False, str(exc))


# ============================================================
print("\n=== DashboardState: VWAP sourced from NIFTY futures ===")
# ============================================================
class FakeKiteWithFutures:
    """Like FakeKite, but also serves a resolvable futures contract (its
    own instruments() + historical_data() by token) with REAL volume,
    unlike the index candles here which deliberately carry none — mirrors
    the real NIFTY 50 (no volume) vs. NIFTY futures (real volume)
    situation this feature exists for."""

    def __init__(self, spot_candles, futures_candles, futures_token=555,
                 futures_symbol="NIFTY26SEPFUT", spot_token=256265):
        self.spot_token = spot_token
        self._spot_candles = spot_candles
        self._futures_candles = futures_candles
        self._futures_token = futures_token
        self._instruments_list = [_fut("NIFTY", date(2099, 1, 1), futures_token, futures_symbol)]

    def ltp(self, symbols):
        return {"NSE:NIFTY 50": {"instrument_token": self.spot_token, "last_price": self._spot_candles[-1]["close"]}}

    def instruments(self, exchange):
        return self._instruments_list

    def historical_data(self, token, from_date, to_date, interval):
        if token == self._futures_token:
            return self._futures_candles
        if token == self.spot_token:
            return self._spot_candles
        return []  # no prev-day data needed for this test

    def positions(self):
        return {"net": []}


with tempfile.TemporaryDirectory() as d:
    fut_prices = steady_trend(24000, 1.0, n)
    spot_candles_zero_vol = candles_from_closes(fut_prices, volume=0)          # simulates the real index: no volume
    futures_candles_real_vol = candles_from_closes([p + 40 for p in fut_prices], volume=1000)  # a premium, real volume

    fut_kite = FakeKiteWithFutures(spot_candles_zero_vol, futures_candles_real_vol)
    fut_state = DashboardState(
        fut_kite, DashboardConfig(),
        tick_log_path=Path(d) / "tick.jsonl", trade_log_path=Path(d) / "trade.jsonl",
        strangle_state_path=Path(d) / "no_s.json", hedge_state_path=Path(d) / "no_h.json",
        long_option_state_path=Path(d) / "no_lo.json",
    )
    fut_now = spot_candles_zero_vol[-1]["date"]
    fut_state.resolve_spot_token()
    fut_state.backfill(fut_now)

    result_no_futures = fut_state.recompute(fut_now)
    check("before futures are resolved: zero-volume index falls back to TWAP",
          result_no_futures["vwap_is_twap_fallback"] is True)
    twap_vwap_value = result_no_futures["trend_detail"]["vwap"]

    resolved_token = fut_state.resolve_futures_token(fut_now)
    check("futures contract resolved", resolved_token == 555, str(resolved_token))
    check("futures tradingsymbol captured", fut_state.futures_tradingsymbol == "NIFTY26SEPFUT",
          str(fut_state.futures_tradingsymbol))

    fut_state.backfill_futures(fut_now)
    check("futures accumulator populated by backfill_futures",
          len(fut_state.futures_accumulator.as_sorted_list()) == len(futures_candles_real_vol),
          str(len(fut_state.futures_accumulator.as_sorted_list())))

    result_with_futures = fut_state.recompute(fut_now)
    check("once futures are backfilled: TWAP fallback no longer flagged",
          result_with_futures["vwap_is_twap_fallback"] is False)
    futures_vwap_value = result_with_futures["trend_detail"]["vwap"]
    check("VWAP now reflects the futures premium, not the index's own TWAP",
          futures_vwap_value != twap_vwap_value and futures_vwap_value > twap_vwap_value,
          f"futures={futures_vwap_value} vs twap={twap_vwap_value}")

    # Live futures ticks feed the SAME accumulator on_futures_tick() writes to.
    fut_state.on_futures_tick(fut_now + timedelta(minutes=1), 30000.0, cum_volume=999999)
    check("on_futures_tick() writes into the futures accumulator, not the index one",
          fut_state.futures_accumulator.as_sorted_list()[-1]["close"] == 30000.0)


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
