"""
Shared synthetic-data helpers for the test suite (test_snapshot.py,
test_trend_engine.py, test_entry_engine.py, test_location_engine.py,
test_decision_engine.py, test_position_engine.py). NOT itself a test — no
assertions here, just candle/session generators so each engine's tests can
build a real IndicatorSnapshot from a plausible synthetic session instead of
hand-populating every indicator series field by field (which would be both
extremely tedious and much easier to get subtly wrong than just generating
consistent OHLC and running it through the real snapshot/indicator pipeline
those tests are also implicitly exercising).

Deliberately named without a test_ prefix so it's never mistaken for a
runnable test script itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from candles import IST
from config import DashboardConfig, IndicatorConfig
from snapshot import IndicatorSnapshot, build_snapshot

SESSION_START = datetime(2026, 8, 20, 9, 15, 0, tzinfo=IST)


def candles_from_closes(closes: list[float], start: datetime = SESSION_START,
                         wiggle: float = 0.5, volume: int = 100) -> list[dict]:
    """Turns a plain list of closing prices into 1-min OHLC candles: each
    bar's open is the previous bar's close (first bar opens at its own
    close), high/low pad the open-close range by `wiggle` so ATR/TR are
    never degenerately zero unless the caller wants them to be."""
    candles = []
    prev_close = closes[0]
    for i, c in enumerate(closes):
        o = prev_close
        h = max(o, c) + wiggle
        l = min(o, c) - wiggle
        candles.append({
            "date": start + timedelta(minutes=i), "open": o, "high": h,
            "low": l, "close": c, "volume": volume,
        })
        prev_close = c
    return candles


def steady_trend(start_price: float, points_per_minute: float, num_minutes: int) -> list[float]:
    return [start_price + points_per_minute * i for i in range(num_minutes)]


def flat(price: float, num_minutes: int) -> list[float]:
    return [price] * num_minutes


def choppy(center: float, amplitude: float, num_minutes: int, period_minutes: int = 10) -> list[float]:
    """Deterministic zig-zag (no randomness — tests must be reproducible)
    oscillating around `center` with a triangular wave, period
    `period_minutes`."""
    out = []
    for i in range(num_minutes):
        phase = (i % period_minutes) / period_minutes  # 0..1
        tri = 1 - abs(2 * phase - 1) * 2  # triangle wave in [-1, 1]... see below
        # abs(2*phase-1) in [0,1], *2 in [0,2], 1-that in [-1,1]: peaks at
        # phase=0.5, troughs at phase=0/1 — a clean triangular oscillation.
        out.append(center + amplitude * tri)
    return out


def trend_then_reverse(start_price: float, points_per_minute: float, trend_minutes: int,
                        reverse_minutes: int) -> list[float]:
    up = steady_trend(start_price, points_per_minute, trend_minutes)
    peak = up[-1]
    down = [peak - points_per_minute * i for i in range(1, reverse_minutes + 1)]
    return up + down


def default_config() -> IndicatorConfig:
    return DashboardConfig().indicator


def snapshot_from_closes(closes: list[float], cfg: IndicatorConfig = None,
                          start: datetime = SESSION_START, wiggle: float = 0.5,
                          volume: int = 100) -> IndicatorSnapshot:
    cfg = cfg or default_config()
    candles = candles_from_closes(closes, start=start, wiggle=wiggle, volume=volume)
    return build_snapshot(candles, cfg)


def warmup_bars(cfg: IndicatorConfig = None) -> int:
    """How many 1-min bars are needed before the 5-min series has enough
    history for every indicator (EMA50, DMI/ADX, Aroon, S/R lookback) to be
    non-None — used by engine tests to pad synthetic sessions instead of
    guessing a bar count and finding out the hard way an indicator is still
    None. +10 bars of slack on top of the theoretical minimum."""
    cfg = cfg or default_config()
    needed_5m_bars = max(cfg.ema_slow_period, cfg.dmi_period * 2, cfg.aroon_period,
                          cfg.sr_lookback_bars) + 10
    return needed_5m_bars * 5
