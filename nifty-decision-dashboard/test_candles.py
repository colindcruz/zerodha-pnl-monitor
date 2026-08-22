"""
Unit tests for candles.py — pure bucketing math + the live 1-min accumulator.
Follows this repo's existing test style: a plain script, no test framework,
plain assertions via check(). No Kite session anywhere.
"""

import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from candles import (
    IST,
    OneMinuteAccumulator,
    bucket_candles,
    bucket_start,
    normalize_historical,
    session_start,
)

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


# ============================================================
print("=== session_start / bucket_start boundary math ===")
# ============================================================
check("session_start: 10:37 IST -> 09:15 same day",
      session_start(ist(2026, 8, 20, 10, 37)) == ist(2026, 8, 20, 9, 15))
check("session_start: exactly 09:15 -> itself",
      session_start(ist(2026, 8, 20, 9, 15)) == ist(2026, 8, 20, 9, 15))
check("session_start: naive datetime treated as already-IST",
      session_start(datetime(2026, 8, 20, 10, 0)) == ist(2026, 8, 20, 9, 15))

check("bucket_start(5m): 09:15 exactly -> 09:15",
      bucket_start(ist(2026, 8, 20, 9, 15, 0), 5) == ist(2026, 8, 20, 9, 15))
check("bucket_start(5m): 09:17 -> 09:15",
      bucket_start(ist(2026, 8, 20, 9, 17, 30), 5) == ist(2026, 8, 20, 9, 15))
check("bucket_start(5m): 09:19:59 -> still 09:15",
      bucket_start(ist(2026, 8, 20, 9, 19, 59), 5) == ist(2026, 8, 20, 9, 15))
check("bucket_start(5m): 09:20:00 -> 09:20 (next bucket)",
      bucket_start(ist(2026, 8, 20, 9, 20, 0), 5) == ist(2026, 8, 20, 9, 20))
check("bucket_start(2m): 09:16:59 -> 09:15",
      bucket_start(ist(2026, 8, 20, 9, 16, 59), 2) == ist(2026, 8, 20, 9, 15))
check("bucket_start(2m): 09:17:00 -> 09:17",
      bucket_start(ist(2026, 8, 20, 9, 17, 0), 2) == ist(2026, 8, 20, 9, 17))
check("bucket_start: different calendar days bucket independently",
      bucket_start(ist(2026, 8, 21, 10, 17, 0), 5) == ist(2026, 8, 21, 10, 15))


# ============================================================
print("\n=== bucket_candles ===")
# ============================================================
def mk1m(minute, o, h, l, c, v=100, day=20):
    return {"date": ist(2026, 8, day, 9, 15, 0) + timedelta(minutes=minute),
            "open": o, "high": h, "low": l, "close": c, "volume": v}


one_min = [
    mk1m(0, 100, 102, 99, 101, v=10),
    mk1m(1, 101, 105, 100, 104, v=20),
    mk1m(2, 104, 106, 103, 105, v=15),
    mk1m(3, 105, 108, 104, 107, v=25),
    mk1m(4, 107, 109, 106, 108, v=5),
]
merged = bucket_candles(one_min, 5)
check("bucket_candles(5m): exactly one merged bucket for 5 one-min bars", len(merged) == 1, str(len(merged)))
if merged:
    b = merged[0]
    check("bucket_candles: open == first bar's open", b["open"] == 100, str(b["open"]))
    check("bucket_candles: close == last bar's close", b["close"] == 108, str(b["close"]))
    check("bucket_candles: high == max across bucket", b["high"] == 109, str(b["high"]))
    check("bucket_candles: low == min across bucket", b["low"] == 99, str(b["low"]))
    check("bucket_candles: volume == sum across bucket", b["volume"] == 75, str(b["volume"]))
    check("bucket_candles: date == bucket start", b["date"] == ist(2026, 8, 20, 9, 15, 0), str(b["date"]))

# Two buckets: bars 0-4 in bucket [09:15,09:20), bar 5 starts [09:20,09:25).
two_bucket_input = one_min + [mk1m(5, 108, 110, 107, 109, v=8)]
merged2 = bucket_candles(two_bucket_input, 5)
check("bucket_candles: 6 one-min bars across 2 buckets -> 2 merged candles", len(merged2) == 2, str(len(merged2)))
if len(merged2) == 2:
    check("bucket_candles: second bucket starts at 09:20", merged2[1]["date"] == ist(2026, 8, 20, 9, 20, 0))
    check("bucket_candles: second bucket is a single unmerged bar", merged2[1]["open"] == 108 and merged2[1]["close"] == 109)

check("bucket_candles: minutes<=1 returns an unchanged (copied) list",
      bucket_candles(one_min, 1) == one_min and bucket_candles(one_min, 1) is not one_min)

check("bucket_candles: input order doesn't matter (sorted internally)",
      bucket_candles(list(reversed(one_min)), 5) == merged)


# ============================================================
print("\n=== OneMinuteAccumulator ===")
# ============================================================
acc = OneMinuteAccumulator()
t0 = ist(2026, 8, 20, 9, 15, 10)
acc.on_tick(t0, 100.0, cum_volume=1000)
acc.on_tick(t0 + timedelta(seconds=20), 102.0, cum_volume=1050)
acc.on_tick(t0 + timedelta(seconds=40), 99.0, cum_volume=1080)
bars = acc.as_sorted_list()
check("accumulator: 3 same-minute ticks -> 1 candle", len(bars) == 1, str(len(bars)))
if bars:
    b = bars[0]
    check("accumulator: open == first tick's price", b["open"] == 100.0, str(b["open"]))
    check("accumulator: close == last tick's price", b["close"] == 99.0, str(b["close"]))
    check("accumulator: high == max tick price", b["high"] == 102.0, str(b["high"]))
    check("accumulator: low == min tick price", b["low"] == 99.0, str(b["low"]))
    check("accumulator: volume == diffed deltas (50+30), first tick contributes 0",
          b["volume"] == 80, str(b["volume"]))

acc.on_tick(t0 + timedelta(minutes=1), 103.0, cum_volume=1100)
bars2 = acc.as_sorted_list()
check("accumulator: tick in next minute opens a new candle", len(bars2) == 2, str(len(bars2)))

seeded = OneMinuteAccumulator()
seeded.seed_from_historical([mk1m(0, 50, 51, 49, 50, v=999)])
seeded.on_tick(ist(2026, 8, 20, 9, 15, 5), 60.0, cum_volume=10)  # same bucket as the seeded candle
check("accumulator: seed_from_historical doesn't overwrite an existing (already-live) bucket",
      seeded.as_sorted_list()[0]["open"] == 50, str(seeded.as_sorted_list()[0]))


# ============================================================
print("\n=== normalize_historical ===")
# ============================================================
raw = [{"date": ist(2026, 8, 20, 9, 15, 0), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "oi": 12345}]
norm = normalize_historical(raw)
check("normalize_historical: missing volume defaults to 0", norm[0]["volume"] == 0, str(norm[0]))
check("normalize_historical: extra keys (oi) dropped", "oi" not in norm[0], str(norm[0]))

raw2 = [{"date": ist(2026, 8, 20, 9, 15, 0), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 42}]
check("normalize_historical: real volume preserved", normalize_historical(raw2)[0]["volume"] == 42)


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
