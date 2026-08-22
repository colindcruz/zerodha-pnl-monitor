"""
Unit tests for trend_engine.py. Feeds real IndicatorSnapshots built from
synthetic sessions (via fixtures.py) into evaluate_trend() and checks the
resulting classification — no Kite session, no candle-fetching I/O.
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from config import TrendEngineConfig
from fixtures import candles_from_closes, default_config, flat, steady_trend, warmup_bars
from snapshot import build_snapshot
from trend_engine import MomentumState, TrendDirection, TrendStrength, VolatilityLevel, classify_volatility, evaluate_trend

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


icfg = default_config()
tcfg = TrendEngineConfig()
n = warmup_bars(icfg)


# ============================================================
print("=== Strong, clean uptrend ===")
# ============================================================
up_prices = steady_trend(24000, 1.0, n)
up_snap = build_snapshot(candles_from_closes(up_prices), icfg)
up_result = evaluate_trend(up_snap, tcfg)
check("uptrend: score is positive", up_result.score > 0, str(up_result.score))
check("uptrend: direction is a bull variant",
      up_result.direction in (TrendDirection.STRONG_BULL, TrendDirection.BULL, TrendDirection.WEAK_BULL),
      str(up_result.direction))
check("uptrend: aroon vote is bullish", up_result.votes["aroon"] == 1, str(up_result.votes))
check("uptrend: ema_structure vote is bullish", up_result.votes["ema_structure"] == 1, str(up_result.votes))
check("uptrend: dmi vote is bullish", up_result.votes["dmi"] == 1, str(up_result.votes))
check("uptrend: 5 votes present", len(up_result.votes) == 5, str(up_result.votes))
check("uptrend: reasons list has one entry per vote", len(up_result.reasons) == 5)


# ============================================================
print("\n=== Strong, clean downtrend (mirror) ===")
# ============================================================
down_prices = steady_trend(24000, -1.0, n)
down_snap = build_snapshot(candles_from_closes(down_prices), icfg)
down_result = evaluate_trend(down_snap, tcfg)
check("downtrend: score is negative", down_result.score < 0, str(down_result.score))
check("downtrend: direction is a bear variant",
      down_result.direction in (TrendDirection.STRONG_BEAR, TrendDirection.BEAR, TrendDirection.WEAK_BEAR),
      str(down_result.direction))
check("downtrend: dmi vote is bearish", down_result.votes["dmi"] == -1, str(down_result.votes))


# ============================================================
print("\n=== Flat / no trend ===")
# ============================================================
flat_prices = flat(24000, n)
flat_snap = build_snapshot(candles_from_closes(flat_prices, wiggle=0.1), icfg)
flat_result = evaluate_trend(flat_snap, tcfg)
check("flat: score is 0", flat_result.score == 0, str(flat_result.score))
check("flat: direction is NEUTRAL", flat_result.direction == TrendDirection.NEUTRAL, str(flat_result.direction))
check("flat: trend strength is WEAK (near-zero ADX)", flat_result.strength == TrendStrength.WEAK,
      str(flat_result.strength))


# ============================================================
print("\n=== Trend Strength bands (independent of direction) ===")
# ============================================================
check("strong uptrend has meaningfully elevated trend strength (not WEAK)",
      up_result.strength != TrendStrength.WEAK, str(up_result.strength))


# ============================================================
print("\n=== Momentum: MATURING (ADX plateaus, DI spread narrows after a strong run) ===")
# ============================================================
# A strong, fast trend for the first stretch (builds ADX + wide DI spread),
# then a much slower/choppier drift for the remainder (DI spread narrows;
# ADX stops climbing). This is the scenario the module docstring calls out:
# "high but falling ADX + weakening Aroon + shrinking candles" should read
# as MATURING, not STRENGTHENING, purely because ADX's absolute level is
# still elevated.
strong_leg = steady_trend(24000, 1.5, n)
peak = strong_leg[-1]
fade_leg = [peak + 0.05 * i for i in range(n // 2)]  # much shallower continuation
maturing_prices = strong_leg + fade_leg
maturing_snap = build_snapshot(candles_from_closes(maturing_prices), icfg)
maturing_result = evaluate_trend(maturing_snap, tcfg)
check("maturing scenario: momentum is MATURING or DETERIORATING (not STRENGTHENING)",
      maturing_result.momentum in (MomentumState.MATURING, MomentumState.DETERIORATING),
      f"momentum={maturing_result.momentum}")


# ============================================================
print("\n=== ADX direction ===")
# ============================================================
check("steady uptrend: ADX direction is UP (building trend) or FLAT (already saturated at 100)",
      up_result.adx_direction in ("UP", "FLAT"), str(up_result.adx_direction))
check("flat/no-trend session: ADX direction is FLAT (near-zero ADX, nothing moving it)",
      flat_result.adx_direction == "FLAT", str(flat_result.adx_direction))


# ============================================================
print("\n=== Volatility classification ===")
# ============================================================
check("steady uptrend (constant candle range throughout): volatility reads NORMAL",
      up_result.volatility == VolatilityLevel.NORMAL, str(up_result.volatility))


def two_phase_candles(phase1_price_start, phase1_bars, phase1_wiggle, phase2_bars, phase2_wiggle):
    """A steady-drift first phase at one candle-range width, then a second
    phase at a different width, continuing from EXACTLY where the first
    phase's own last close left off (not an independent price series — a
    price gap at the join would itself spike TR, confounding the very ATR
    regime change this is trying to isolate) — used to force a sharp ATR
    change near the end of the session, which classify_volatility should
    then pick up."""
    phase1 = candles_from_closes(steady_trend(phase1_price_start, 0.2, phase1_bars), wiggle=phase1_wiggle)
    phase2_start_date = phase1[-1]["date"] + (phase1[-1]["date"] - phase1[-2]["date"])
    phase2_prices = steady_trend(phase1[-1]["close"] + 0.2, 0.2, phase2_bars)
    phase2 = candles_from_closes(phase2_prices, start=phase2_start_date, wiggle=phase2_wiggle)
    return phase1 + phase2


# A quiet, narrow-range regime for warmup, followed by a sharp widening in
# the final `volatility_lookback_bars` tf5-bars (25 one-min bars = 5 tf5
# bars, matching the config default) — ATR should still be catching up
# (Wilder-smoothed, so it lags), reading meaningfully higher than it was
# `volatility_lookback_bars` tf5-bars ago -> HIGH.
TRANSITION_1M_BARS = 25  # 5 tf5-bars, matching TrendEngineConfig.volatility_lookback_bars
expanding_candles = two_phase_candles(24000, n - TRANSITION_1M_BARS, 1, TRANSITION_1M_BARS, 15)
expanding_result = evaluate_trend(build_snapshot(expanding_candles, icfg), tcfg)
check("narrow-then-wide session: volatility reads HIGH",
      expanding_result.volatility == VolatilityLevel.HIGH, str(expanding_result.volatility))

contracting_candles = two_phase_candles(24000, n - TRANSITION_1M_BARS, 15, TRANSITION_1M_BARS, 1)
contracting_result = evaluate_trend(build_snapshot(contracting_candles, icfg), tcfg)
check("wide-then-narrow session: volatility reads LOW",
      contracting_result.volatility == VolatilityLevel.LOW, str(contracting_result.volatility))


# ============================================================
print("\n=== Insufficient data ===")
# ============================================================
tiny_snap = build_snapshot(candles_from_closes(steady_trend(24000, 1.0, 20)), icfg)
tiny_result = evaluate_trend(tiny_snap, tcfg)
check("tiny session: momentum flagged insufficient_data", tiny_result.insufficient_data is True)
check("tiny session: adx_direction is None (nothing to compare)", tiny_result.adx_direction is None)

# Regression test: a session with FEWER tf5-bars than momentum_lookback_bars
# itself (not just fewer than the ADX seed period) hits a distinct early-
# return branch inside _momentum_state — a real bug shipped once where that
# branch's tuple was one element short (missing adx_direction), which only
# surfaced live because every other fixture happened to have more bars than
# this. 10 one-min bars = 2 tf5-bars, comfortably below lookback=3.
micro_snap = build_snapshot(candles_from_closes(steady_trend(24000, 1.0, 10)), icfg)
micro_result = evaluate_trend(micro_snap, tcfg)  # must not raise (ValueError: not enough values to unpack)
check("micro session (fewer tf5-bars than momentum_lookback_bars): does not raise, flags insufficient_data",
      micro_result.insufficient_data is True)
check("micro session: adx_direction is None", micro_result.adx_direction is None)


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
