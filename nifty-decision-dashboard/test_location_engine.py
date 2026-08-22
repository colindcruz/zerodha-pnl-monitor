"""
Unit tests for location_engine.py. Builds minimal IndicatorSnapshots with
directly-controlled tf5 values (price/VWAP/EMA20/ATR) and sr_levels, rather
than a full build_snapshot() pipeline — each test needs precise control over
extension distance and runway geometry. No Kite session.
"""

import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from candles import IST
from config import IndicatorConfig, LocationEngineConfig
from indicators import SRLevel, aroon, dmi_adx
from location_engine import ExtensionLevel, RunwayLevel, evaluate_location
from snapshot import IndicatorSnapshot, TimeframeIndicators

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


T0 = datetime(2026, 8, 20, 9, 15, 0, tzinfo=IST)
DUMMY_CANDLE = {"date": T0, "open": 24000, "high": 24010, "low": 23990, "close": 24000, "volume": 100}


def make_snapshot(price, vwap_v, ema20, atr_v, sr_levels, vwap_price=None):
    tf = TimeframeIndicators(
        candles=[{**DUMMY_CANDLE, "close": price}],
        ema_fast=[ema20], ema_slow=[ema20], atr=[atr_v],
        aroon=aroon([DUMMY_CANDLE], 14), dmi_adx=dmi_adx([DUMMY_CANDLE], 14, 14),
        vwap_value=[vwap_v], vwap_is_twap=[False],
        vwap_price=[vwap_price if vwap_price is not None else price],
    )
    return IndicatorSnapshot(config=IndicatorConfig(), candles_1m=[], tf2=tf, tf5=tf,
                              opening_range=None, swing_points_5m=[], sr_levels=sr_levels)


cfg = LocationEngineConfig()


# ============================================================
print("=== Extension ===")
# ============================================================
snap_normal = make_snapshot(price=24000, vwap_v=23995, ema20=23998, atr_v=20, sr_levels=[])
res = evaluate_location(snap_normal, "LONG", cfg)
check("price near VWAP/EMA (<1 ATR) -> NORMAL", res.extension == ExtensionLevel.NORMAL, str(res.extension))

snap_extended = make_snapshot(price=24050, vwap_v=23995, ema20=23998, atr_v=20, sr_levels=[])
res2 = evaluate_location(snap_extended, "LONG", cfg)
check("price 55pts / 20ATR = 2.75 ATR from VWAP -> VERY_EXTENDED",
      res2.extension == ExtensionLevel.VERY_EXTENDED, str(res2.extension))

snap_mid = make_snapshot(price=24025, vwap_v=23995, ema20=23998, atr_v=20, sr_levels=[])
res3 = evaluate_location(snap_mid, "LONG", cfg)
check("price ~30pts / 20 ATR = 1.5 ATR -> EXTENDED (between normal and very-extended)",
      res3.extension == ExtensionLevel.EXTENDED, str(res3.extension))

check("extension uses the MORE distant of VWAP/EMA20",
      evaluate_location(make_snapshot(24000, vwap_v=23999, ema20=23900, atr_v=20, sr_levels=[]), "LONG", cfg)
      .extension == ExtensionLevel.VERY_EXTENDED)  # close to VWAP but 5 ATR from EMA20

# VWAP distance must use vwap_price (the same instrument VWAP was computed
# from, e.g. futures close), not the index close — otherwise a futures-index
# premium alone would read as "extended" with zero real price movement.
# Index price == VWAP exactly (0 distance); a +50 futures premium (vwap_price
# 50pts above vwap_v) must NOT count as extension since the pair is
# consistent (0 ATR distance in futures terms). EMA sits far away (100pts /
# 20 ATR = 5) so the correct result is VERY_EXTENDED only because of the EMA
# leg, not the (irrelevant) VWAP leg.
snap_premium_but_flat = make_snapshot(price=24000, vwap_v=23950, ema20=23900, atr_v=20, sr_levels=[],
                                       vwap_price=23950)
res_premium = evaluate_location(snap_premium_but_flat, "LONG", cfg)
check("a futures premium alone (index price == VWAP) contributes zero VWAP extension",
      res_premium.extension == ExtensionLevel.VERY_EXTENDED and res_premium.extension_atr_distance == 5.0,
      f"extension={res_premium.extension} dist={res_premium.extension_atr_distance}")


# ============================================================
print("\n=== Runway ===")
# ============================================================
# Resistance 90pts above price, ATR=20, initial_stop_atr_multiple=1.0 ->
# stop=20, reward=90, ratio=4.5 -> EXCELLENT (>=3.0).
levels_far = [SRLevel(price=24090, touches=3, kind="resistance")]
res_excellent = evaluate_location(make_snapshot(24000, 24000, 24000, 20, levels_far), "LONG", cfg)
check("reward:stop 4.5 -> EXCELLENT", res_excellent.runway == RunwayLevel.EXCELLENT, str(res_excellent.runway))

# reward=50, stop=20, ratio=2.5 -> GOOD ([2.0,3.0)).
levels_good = [SRLevel(price=24050, touches=3, kind="resistance")]
res_good = evaluate_location(make_snapshot(24000, 24000, 24000, 20, levels_good), "LONG", cfg)
check("reward:stop 2.5 -> GOOD", res_good.runway == RunwayLevel.GOOD, str(res_good.runway))

# reward=25, stop=20, ratio=1.25 -> MARGINAL ([1.0,2.0)).
levels_marginal = [SRLevel(price=24025, touches=3, kind="resistance")]
res_marginal = evaluate_location(make_snapshot(24000, 24000, 24000, 20, levels_marginal), "LONG", cfg)
check("reward:stop 1.25 -> MARGINAL", res_marginal.runway == RunwayLevel.MARGINAL, str(res_marginal.runway))

# reward=10 (< min_absolute_runway_atr(0.5)*20=10 -> not strictly less, boundary) use 8 to be safely POOR.
levels_poor = [SRLevel(price=24008, touches=3, kind="resistance")]
res_poor = evaluate_location(make_snapshot(24000, 24000, 24000, 20, levels_poor), "LONG", cfg)
check("resistance right on top of price -> POOR", res_poor.runway == RunwayLevel.POOR, str(res_poor.runway))

# No level at all in the LONG direction -> GOOD (unverified clear runway), not EXCELLENT, not POOR.
res_no_level = evaluate_location(make_snapshot(24000, 24000, 24000, 20, []), "LONG", cfg)
check("no S/R level found -> GOOD (not EXCELLENT, not POOR)", res_no_level.runway == RunwayLevel.GOOD,
      str(res_no_level.runway))
check("no S/R level found -> nearest_level_price is None", res_no_level.nearest_level_price is None)


# ============================================================
print("\n=== Direction-awareness ===")
# ============================================================
# A resistance ABOVE price only matters for LONG; for SHORT, nearest_level
# looks BELOW price and should find nothing here -> GOOD fallback, not POOR.
res_short_ignores_resistance_above = evaluate_location(
    make_snapshot(24000, 24000, 24000, 20, levels_far), "SHORT", cfg)
check("SHORT direction ignores a resistance level above price (looks below instead)",
      res_short_ignores_resistance_above.runway == RunwayLevel.GOOD,
      str(res_short_ignores_resistance_above.runway))

levels_support_below = [SRLevel(price=23910, touches=3, kind="support")]
res_short_support = evaluate_location(make_snapshot(24000, 24000, 24000, 20, levels_support_below), "SHORT", cfg)
check("SHORT direction correctly finds a support level below price",
      res_short_support.nearest_level_price == 23910, str(res_short_support.nearest_level_price))
check("SHORT reward:stop 4.5 (90pts/20) -> EXCELLENT", res_short_support.runway == RunwayLevel.EXCELLENT)


# ============================================================
print("\n=== bad direction argument raises ===")
# ============================================================
try:
    evaluate_location(snap_normal, "SIDEWAYS", cfg)
    check("invalid direction raises ValueError", False)
except ValueError:
    check("invalid direction raises ValueError", True)


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
