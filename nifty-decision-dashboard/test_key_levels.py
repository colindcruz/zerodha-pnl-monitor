"""
Unit tests for key_levels.py. Hand-built IndicatorSnapshots — no Kite
session, no candle I/O.
"""

import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from candles import IST
from config import IndicatorConfig
from indicators import AroonResult, DmiAdxResult, OpeningRange, SRLevel
from key_levels import PrevDayOHLC, compute_key_levels
from snapshot import IndicatorSnapshot, TimeframeIndicators

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


T0 = datetime(2026, 8, 20, 9, 15, 0, tzinfo=IST)
CANDLE = {"date": T0, "open": 24000, "high": 24010, "low": 23990, "close": 24000, "volume": 100}


def make_snapshot(price=24000, vwap_v=23980, opening_range=None, sr_levels=None):
    tf = TimeframeIndicators(
        candles=[{**CANDLE, "close": price}], ema_fast=[price], ema_slow=[price], atr=[20],
        aroon=AroonResult(up=[50], down=[50]), dmi_adx=DmiAdxResult(plus_di=[20], minus_di=[20], adx=[20]),
        vwap_value=[vwap_v], vwap_is_twap=[False],
    )
    return IndicatorSnapshot(config=IndicatorConfig(), candles_1m=[], tf2=tf, tf5=tf,
                              opening_range=opening_range, swing_points_5m=[], sr_levels=sr_levels or [])


# ============================================================
print("=== basic levels ===")
# ============================================================
snap = make_snapshot(price=24000, vwap_v=23980)
result = compute_key_levels(snap)
check("price reported correctly", result.price == 24000)
vwap_level = next((lv for lv in result.levels if lv.name == "VWAP"), None)
check("VWAP level present", vwap_level is not None)
if vwap_level:
    check("VWAP distance == vwap - price (-20)", vwap_level.distance_points == -20, str(vwap_level.distance_points))


# ============================================================
print("\n=== prev-day levels ===")
# ============================================================
prev_day = PrevDayOHLC(high=24100, low=23900, close=24050)
result2 = compute_key_levels(snap, prev_day)
names = {lv.name for lv in result2.levels}
check("prev day high/low/close all present", {"Prev Day High", "Prev Day Low", "Prev Day Close"} <= names, str(names))
pdh = next(lv for lv in result2.levels if lv.name == "Prev Day High")
check("prev day high distance == 100", pdh.distance_points == 100, str(pdh.distance_points))

# Classic floor-trader pivots: P = (H+L+C)/3, R1 = 2P-L, S1 = 2P-H.
# H=24100, L=23900, C=24050 -> P = 72050/3 = 24016.667, R1 = 24133.333, S1 = 23933.333.
pivot_names = {"Pivot (P)", "R1", "S1"}
check("pivot levels present", pivot_names <= names, str(names))
pivot_level = next(lv for lv in result2.levels if lv.name == "Pivot (P)")
r1_level = next(lv for lv in result2.levels if lv.name == "R1")
s1_level = next(lv for lv in result2.levels if lv.name == "S1")
check("pivot price hand-computed", abs(pivot_level.price - 24016.6667) < 0.001, str(pivot_level.price))
check("R1 hand-computed (2P - L)", abs(r1_level.price - 24133.3333) < 0.001, str(r1_level.price))
check("S1 hand-computed (2P - H)", abs(s1_level.price - 23933.3333) < 0.001, str(s1_level.price))
check("R1 above pivot above S1", r1_level.price > pivot_level.price > s1_level.price)

result_no_prev = compute_key_levels(snap, None)
check("no prev-day data -> no pivot levels either", not any(lv.name in pivot_names for lv in result_no_prev.levels))
check("no prev-day data -> no prev-day levels",
      not any("Prev Day" in lv.name for lv in result_no_prev.levels))


# ============================================================
print("\n=== opening range ===")
# ============================================================
snap_or = make_snapshot(price=24000, opening_range=OpeningRange(high=24050, low=23950))
result3 = compute_key_levels(snap_or)
or_names = {lv.name for lv in result3.levels}
check("opening range high/low present", {"Opening Range High", "Opening Range Low"} <= or_names, str(or_names))


# ============================================================
print("\n=== nearest S/R above and below ===")
# ============================================================
levels = [SRLevel(price=24080, touches=3, kind="resistance"), SRLevel(price=23920, touches=3, kind="support")]
snap_sr = make_snapshot(price=24000, sr_levels=levels)
result4 = compute_key_levels(snap_sr)
above = next((lv for lv in result4.levels if "Above" in lv.name), None)
below = next((lv for lv in result4.levels if "Below" in lv.name), None)
check("nearest resistance above found", above is not None and above.price == 24080, str(above))
check("nearest support below found", below is not None and below.price == 23920, str(below))
check("above-level label mentions Resistance", above is not None and "Resistance" in above.name, str(above))
check("below-level label mentions Support", below is not None and "Support" in below.name, str(below))


# ============================================================
print("\n=== sorted by distance ===")
# ============================================================
distances = [lv.distance_points for lv in result4.levels]
check("levels sorted ascending by signed distance", distances == sorted(distances), str(distances))


# ============================================================
print("\n=== empty candles -> empty result ===")
# ============================================================
empty_tf = TimeframeIndicators(candles=[], ema_fast=[], ema_slow=[], atr=[],
                                aroon=AroonResult(up=[], down=[]), dmi_adx=DmiAdxResult(plus_di=[], minus_di=[], adx=[]),
                                vwap_value=[], vwap_is_twap=[])
empty_snap = IndicatorSnapshot(config=IndicatorConfig(), candles_1m=[], tf2=empty_tf, tf5=empty_tf,
                                opening_range=None, swing_points_5m=[], sr_levels=[])
empty_result = compute_key_levels(empty_snap)
check("empty candles -> price is None, no levels", empty_result.price is None and empty_result.levels == [])


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
