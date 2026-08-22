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
from trend_engine import MomentumState, TrendDirection, TrendStrength, evaluate_trend

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
print("\n=== Insufficient data ===")
# ============================================================
tiny_snap = build_snapshot(candles_from_closes(steady_trend(24000, 1.0, 20)), icfg)
tiny_result = evaluate_trend(tiny_snap, tcfg)
check("tiny session: momentum flagged insufficient_data", tiny_result.insufficient_data is True)


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
