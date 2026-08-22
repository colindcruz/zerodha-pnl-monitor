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
