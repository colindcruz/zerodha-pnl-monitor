"""
Unit tests for snapshot.py — the IndicatorSnapshot composition point. Builds
snapshots from synthetic 1-min candle sessions (via fixtures.py) rather than
a live Kite session; verifies the bucketing/indicator wiring is correct, not
the indicator math itself (that's test_indicators.py's job).
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from candles import bucket_start
from fixtures import candles_from_closes, default_config, steady_trend, warmup_bars
from indicators import vwap
from snapshot import build_snapshot

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


cfg = default_config()
n = warmup_bars(cfg)
prices = steady_trend(24000, 0.5, n)
candles = candles_from_closes(prices)
snap = build_snapshot(candles, cfg)


# ============================================================
print("=== bucketing wiring ===")
# ============================================================
expected_5m_bars = len({bucket_start(c["date"], 5) for c in candles})
expected_2m_bars = len({bucket_start(c["date"], 2) for c in candles})
check("tf5 candle count matches distinct 5-min buckets", len(snap.tf5.candles) == expected_5m_bars,
      f"{len(snap.tf5.candles)} vs {expected_5m_bars}")
check("tf2 candle count matches distinct 2-min buckets", len(snap.tf2.candles) == expected_2m_bars,
      f"{len(snap.tf2.candles)} vs {expected_2m_bars}")
check("indicator series length matches candle count (tf5)", len(snap.tf5.ema_fast) == len(snap.tf5.candles))
check("indicator series length matches candle count (tf2)", len(snap.tf2.ema_fast) == len(snap.tf2.candles))


# ============================================================
print("\n=== VWAP sampling correctness ===")
# ============================================================
v1m = vwap(candles)
# Spot-check: the VWAP value on the LAST tf5 bucket must equal the 1-min
# VWAP's value at the last 1-min candle overall (both cover the same
# session-to-date accumulation, and the last tf5 bucket's last constituent
# 1-min bar is necessarily the session's last 1-min bar).
check("VWAP sampled onto tf5's last bucket matches 1-min VWAP's last value",
      snap.tf5.vwap_value[-1] == v1m.value[-1], f"{snap.tf5.vwap_value[-1]} vs {v1m.value[-1]}")
check("VWAP sampled onto tf2's last bucket matches 1-min VWAP's last value",
      snap.tf2.vwap_value[-1] == v1m.value[-1], f"{snap.tf2.vwap_value[-1]} vs {v1m.value[-1]}")
check("no VWAP TWAP-fallback flagged for a normal-volume session", not any(snap.tf5.vwap_is_twap))


# ============================================================
print("\n=== VWAP sourced from a separate instrument (e.g. NIFTY futures) ===")
# ============================================================
zero_vol_index = candles_from_closes(prices, volume=0)  # simulates the real NIFTY 50 index: no real volume
no_futures_snap = build_snapshot(zero_vol_index, cfg)
check("without a futures source: a zero-volume index falls back to TWAP",
      any(no_futures_snap.tf5.vwap_is_twap))

# A separate "futures" price series, at a deliberate premium to the index so
# the test can tell which series actually got used, with real volume.
futures_prices = [p + 50 for p in prices]
futures_candles = candles_from_closes(futures_prices, volume=500)
with_futures_snap = build_snapshot(zero_vol_index, cfg, vwap_source_candles=futures_candles)
check("with a futures source: TWAP fallback no longer flagged",
      not any(with_futures_snap.tf5.vwap_is_twap))

futures_v1m = vwap(futures_candles)
check("VWAP actually comes from the futures series, not the index's own (TWAP) computation",
      with_futures_snap.tf5.vwap_value[-1] == futures_v1m.value[-1],
      f"{with_futures_snap.tf5.vwap_value[-1]} vs {futures_v1m.value[-1]}")
check("...and is therefore clearly different from the index-only (TWAP) value",
      with_futures_snap.tf5.vwap_value[-1] != no_futures_snap.tf5.vwap_value[-1],
      f"{with_futures_snap.tf5.vwap_value[-1]} vs {no_futures_snap.tf5.vwap_value[-1]}")

check("every OTHER indicator (e.g. EMA) is unaffected by which series VWAP came from",
      with_futures_snap.tf5.ema_fast[-1] == no_futures_snap.tf5.ema_fast[-1],
      f"{with_futures_snap.tf5.ema_fast[-1]} vs {no_futures_snap.tf5.ema_fast[-1]}")
check("tf2 VWAP is also sourced from futures", with_futures_snap.tf2.vwap_value[-1] is not None
      and not with_futures_snap.tf2.vwap_is_twap[-1])

check("vwap_price (paired with VWAP for distance comparisons) is the FUTURES close, not the index close",
      with_futures_snap.tf5.vwap_price[-1] == futures_candles[-1]["close"],
      f"{with_futures_snap.tf5.vwap_price[-1]} vs futures={futures_candles[-1]['close']} "
      f"index={zero_vol_index[-1]['close']}")
check("without a futures source, vwap_price falls back to the index's own close (same series VWAP used)",
      no_futures_snap.tf5.vwap_price[-1] == zero_vol_index[-1]["close"])


# ============================================================
print("\n=== warmup: enough history for every indicator to be non-None ===")
# ============================================================
check("tf5 EMA(slow) is non-None at the last bar", snap.tf5.ema_slow[-1] is not None)
check("tf5 EMA(fast) is non-None at the last bar", snap.tf5.ema_fast[-1] is not None)
check("tf5 ATR is non-None at the last bar", snap.tf5.atr[-1] is not None)
check("tf5 Aroon up/down non-None at the last bar",
      snap.tf5.aroon.up[-1] is not None and snap.tf5.aroon.down[-1] is not None)
check("tf5 ADX is non-None at the last bar", snap.tf5.dmi_adx.adx[-1] is not None)
check("tf5 DI+/DI- non-None at the last bar",
      snap.tf5.dmi_adx.plus_di[-1] is not None and snap.tf5.dmi_adx.minus_di[-1] is not None)
check("tf2 EMA(fast) is non-None at the last bar", snap.tf2.ema_fast[-1] is not None)


# ============================================================
print("\n=== opening range & S/R ===")
# ============================================================
check("opening_range is populated (session-length input covers first 15 min)", snap.opening_range is not None)
if snap.opening_range:
    check("opening_range.high >= opening_range.low", snap.opening_range.high >= snap.opening_range.low)

check("swing_points_5m is a list (may be empty for a pure monotonic trend)",
      isinstance(snap.swing_points_5m, list))
check("sr_levels is a list", isinstance(snap.sr_levels, list))

# A choppy, repeatedly-touched series should actually produce S/R levels.
from fixtures import choppy  # noqa: E402
choppy_prices = choppy(24000, 40, n, period_minutes=30)
choppy_candles = candles_from_closes(choppy_prices)
choppy_snap = build_snapshot(choppy_candles, cfg)
check("choppy session produces at least one S/R level", len(choppy_snap.sr_levels) >= 1,
      str(choppy_snap.sr_levels))


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
