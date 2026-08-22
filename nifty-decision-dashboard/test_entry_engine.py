"""
Unit tests for entry_engine.py. Builds minimal-but-real TimeframeIndicators
directly from hand-crafted 2-min candle sequences (via indicators.py's own
functions — already verified correct by test_indicators.py) rather than a
full build_snapshot() pipeline, since entry_engine.py only ever reads
snapshot.tf2 and each test needs precise control over the signal/confirm
bar's OHLC shape. No Kite session, no candle-fetching I/O.
"""

import sys
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from candles import IST
from config import EntryEngineConfig
from entry_engine import EntrySetupLabel, evaluate_entry
from indicators import aroon, atr, dmi_adx, vwap
from snapshot import IndicatorSnapshot, TimeframeIndicators

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


T0 = datetime(2026, 8, 20, 9, 15, 0, tzinfo=IST)


def bar(minute, o, h, l, c, v=100):
    return {"date": T0 + timedelta(minutes=2 * minute), "open": o, "high": h, "low": l, "close": c, "volume": v}


def tf_from_candles(candle_list, cfg_ind_periods=(20, 14)):
    ema_period, atr_period = cfg_ind_periods
    closes = [c["close"] for c in candle_list]
    from indicators import ema as ema_fn
    v = vwap(candle_list)
    return TimeframeIndicators(
        candles=candle_list,
        ema_fast=ema_fn(closes, ema_period),
        ema_slow=ema_fn(closes, 50),
        atr=atr(candle_list, atr_period),
        aroon=aroon(candle_list, 14),
        dmi_adx=dmi_adx(candle_list, 14, 14),
        vwap_value=v.value,
        vwap_is_twap=v.is_twap_fallback,
    )


def snapshot_from(candle_list) -> IndicatorSnapshot:
    tf = tf_from_candles(candle_list)
    from config import IndicatorConfig
    return IndicatorSnapshot(config=IndicatorConfig(), candles_1m=[], tf2=tf, tf5=tf,
                              opening_range=None, swing_points_5m=[], sr_levels=[])


cfg = EntryEngineConfig()


def padding(n=30, base=24000, step=-3, rng=24):
    """A gentle downtrend so a later hammer-and-confirm has real 'extreme' to
    pull back to and enough history to seed EMA20/ATR."""
    out = []
    price = base
    for i in range(n):
        o = price + rng / 4
        c = price - rng / 4
        h = price + rng / 2
        l = price - rng / 2
        out.append(bar(i, o, h, l, c))
        price += step
    return out


# ============================================================
print("=== insufficient data ===")
# ============================================================
tiny_snap = snapshot_from([bar(0, 100, 101, 99, 100)])
tiny_result = evaluate_entry(tiny_snap, "LONG", cfg)
check("1 candle -> insufficient_data flagged", tiny_result.insufficient_data is True)
check("1 candle -> score 0 / NO_SETUP", tiny_result.score == 0 and tiny_result.label == EntrySetupLabel.NO_SETUP)


# ============================================================
print("\n=== component: wick_reversal ===")
# ============================================================
pad = padding()
atr_series = atr(pad, 14)
atr_v = atr_series[-1]
last_price = pad[-1]["close"]

# Strong lower-wick hammer signal bar, followed by a neutral confirm bar (not
# engineered to confirm) so only the wick_reversal component is isolated.
hammer_signal = bar(len(pad), last_price, last_price + 2, last_price - 20, last_price + 1)
neutral_confirm = bar(len(pad) + 1, last_price + 1, last_price + 2, last_price, last_price + 1)
candles_hammer = pad + [hammer_signal, neutral_confirm]
res_hammer = evaluate_entry(snapshot_from(candles_hammer), "LONG", cfg)
check("hammer signal bar: wick_reversal component scores 1", res_hammer.components.get("wick_reversal") == 1,
      str(res_hammer.components))

# A bar with no lower wick at all (closes/opens right at its own low) should
# NOT score the wick point.
flat_signal = bar(len(pad), last_price, last_price + 1, last_price, last_price + 1)
candles_flat = pad + [flat_signal, neutral_confirm]
res_flat = evaluate_entry(snapshot_from(candles_flat), "LONG", cfg)
check("no-wick signal bar: wick_reversal component scores 0", res_flat.components.get("wick_reversal") == 0,
      str(res_flat.components))


# ============================================================
print("\n=== component: close_location ===")
# ============================================================
# Close pinned right at the bar's high -> best possible close location for LONG.
strong_close_signal = bar(len(pad), last_price, last_price + 10, last_price - 10, last_price + 9.8)
res_close_strong = evaluate_entry(snapshot_from(pad + [strong_close_signal, neutral_confirm]), "LONG", cfg)
check("close pinned near high: close_location scores 1", res_close_strong.components.get("close_location") == 1,
      str(res_close_strong.components))

# Close at the dead center of the range -> should fail the location check.
mid_close_signal = bar(len(pad), last_price - 10, last_price + 10, last_price - 10, last_price)
res_close_mid = evaluate_entry(snapshot_from(pad + [mid_close_signal, neutral_confirm]), "LONG", cfg)
check("close at range midpoint: close_location scores 0", res_close_mid.components.get("close_location") == 0,
      str(res_close_mid.components))


# ============================================================
print("\n=== component: confirmation ===")
# ============================================================
signal_bar = bar(len(pad), last_price, last_price + 5, last_price - 5, last_price)
confirms = bar(len(pad) + 1, last_price, last_price + 8, last_price, last_price + 7)  # closes above signal high (5)
res_confirm_yes = evaluate_entry(snapshot_from(pad + [signal_bar, confirms]), "LONG", cfg)
check("confirm bar closes beyond signal high: confirmation scores 1",
      res_confirm_yes.components.get("confirmation") == 1, str(res_confirm_yes.components))

no_confirm = bar(len(pad) + 1, last_price, last_price + 2, last_price - 2, last_price - 1)
res_confirm_no = evaluate_entry(snapshot_from(pad + [signal_bar, no_confirm]), "LONG", cfg)
check("confirm bar fails to break signal high: confirmation scores 0",
      res_confirm_no.components.get("confirmation") == 0, str(res_confirm_no.components))


# ============================================================
print("\n=== direction symmetry: same data, opposite direction flips ema_slope ===")
# ============================================================
uptrend_pad = []
price = 24000
for i in range(30):
    uptrend_pad.append(bar(i, price - 3, price + 6, price - 6, price + 3))
    price += 4
up_last = uptrend_pad[-1]["close"]
up_signal = bar(30, up_last, up_last + 5, up_last - 5, up_last)
up_confirm = bar(31, up_last, up_last + 2, up_last - 2, up_last)
snap_up = snapshot_from(uptrend_pad + [up_signal, up_confirm])
res_long = evaluate_entry(snap_up, "LONG", cfg)
res_short = evaluate_entry(snap_up, "SHORT", cfg)
check("rising EMA20: ema_slope favorable for LONG", res_long.components.get("ema_slope") == 1,
      str(res_long.components))
check("rising EMA20: ema_slope NOT favorable for SHORT (mirror of LONG)",
      res_short.components.get("ema_slope") == 0, str(res_short.components))


# ============================================================
print("\n=== label thresholds ===")
# ============================================================
from entry_engine import _label_for_score  # noqa: E402
check("score 5 -> STRONG_SETUP", _label_for_score(5, cfg) == EntrySetupLabel.STRONG_SETUP)
check("score 3 -> VALID_SETUP", _label_for_score(3, cfg) == EntrySetupLabel.VALID_SETUP)
check("score 1 -> WEAK_SETUP", _label_for_score(1, cfg) == EntrySetupLabel.WEAK_SETUP)
check("score 0 -> NO_SETUP", _label_for_score(0, cfg) == EntrySetupLabel.NO_SETUP)


# ============================================================
print("\n=== bad direction argument raises ===")
# ============================================================
try:
    evaluate_entry(snap_up, "SIDEWAYS", cfg)
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
