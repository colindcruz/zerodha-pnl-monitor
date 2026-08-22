"""
Unit tests for indicators.py. Follows this repo's existing test style (see
test_long_option_engine.py): a plain script, no test framework, plain
assertions via check(). Pure math only — no Kite session, no candle I/O.
"""

import sys
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from indicators import (
    aroon,
    atr,
    dmi_adx,
    ema,
    nearest_level,
    opening_range,
    support_resistance_levels,
    swing_points,
    true_ranges,
    typical_price,
    vwap,
)

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


T0 = datetime(2026, 8, 22, 9, 15, 0)


def mk(o, h, l, c, v=1000, minute=0):
    return {"date": T0 + timedelta(minutes=minute), "open": o, "high": h, "low": l, "close": c, "volume": v}


def close(a, b, eps=1e-6):
    return abs(a - b) < eps


# ============================================================
print("=== EMA ===")
# ============================================================
# period=3 on [1,2,3,4,5,6]: seed = avg(1,2,3) = 2 at idx 2.
# k = 2/4 = 0.5. idx3: 4*0.5+2*0.5=3. idx4: 5*0.5+3*0.5=4. idx5: 6*0.5+4*0.5=5.
vals = [1, 2, 3, 4, 5, 6]
e = ema(vals, 3)
check("EMA: insufficient bars are None", e[0] is None and e[1] is None)
check("EMA: seed at index period-1 == SMA", close(e[2], 2.0), str(e[2]))
check("EMA: index3 == 3.0", close(e[3], 3.0), str(e[3]))
check("EMA: index4 == 4.0", close(e[4], 4.0), str(e[4]))
check("EMA: index5 == 5.0", close(e[5], 5.0), str(e[5]))

check("EMA: period<=0 returns all-None", all(v is None for v in ema(vals, 0)))
check("EMA: too few bars returns all-None", all(v is None for v in ema([1, 2], 5)))


# ============================================================
print("\n=== ATR (Wilder) ===")
# ============================================================
# 5 bars, TR fixed at 10 for every bar after the first (h-l=10, no gaps) ->
# hand-computed: TR[1..4] = 10,10,10,10. period=3: seed = avg(TR[1],TR[2],TR[3]) = 10
# at index 3. index4: Wilder smooth of a constant series stays 10.
candles = [mk(100, 105, 95, 100, minute=i) for i in range(5)]
trs = true_ranges(candles)
check("TR: index0 is None", trs[0] is None)
check("TR: constant range -> TR==10 everywhere else", all(close(t, 10) for t in trs[1:]))

a = atr(candles, 3)
check("ATR: None before seed", a[0] is None and a[1] is None and a[2] is None)
check("ATR: seed == 10 at index3", close(a[3], 10), str(a[3]))
check("ATR: constant TR stays 10 after seeding", close(a[4], 10), str(a[4]))

# Varying TR: hand-verify one Wilder smoothing step.
candles2 = [mk(100, 110, 90, 100, minute=0), mk(100, 108, 98, 105, minute=1),
            mk(105, 120, 100, 110, minute=2), mk(110, 115, 105, 112, minute=3)]
# TR[1] = max(108-98, |108-100|, |98-100|) = max(10,8,2) = 10
# TR[2] = max(120-100, |120-105|, |100-105|) = max(20,15,5) = 20
# TR[3] = max(115-105, |115-110|, |105-110|) = max(10,5,5) = 10
trs2 = true_ranges(candles2)
check("TR varying: TR[1]==10", close(trs2[1], 10), str(trs2[1]))
check("TR varying: TR[2]==20", close(trs2[2], 20), str(trs2[2]))
check("TR varying: TR[3]==10", close(trs2[3], 10), str(trs2[3]))
# period=2: seed = avg(TR[1],TR[2]) = 15 at index2. index3: (15*(2-1)+10)/2 = 12.5
a2 = atr(candles2, 2)
check("ATR varying: seed==15", close(a2[2], 15), str(a2[2]))
check("ATR varying: smoothed step ==12.5", close(a2[3], 12.5), str(a2[3]))


# ============================================================
print("\n=== DMI / ADX ===")
# ============================================================
# Degenerate zero-range candles (open==high==low==close) marching up by a
# constant 2 points/bar: TR_i == |p_i - p_{i-1}| == 2 exactly (the h-l term
# is 0, so the gap terms dominate TR completely), and plus_dm_i == 2 exactly
# (up_move==2 > down_move==-2, up_move>0) with minus_dm==0 throughout ->
# DI+ == 100*2/2 == 100 exactly, DI- == 0, DX == 100, so ADX (an average of a
# constant 100 series) is also exactly 100 once seeded.
up_candles = [mk(100 + 2 * i, 100 + 2 * i, 100 + 2 * i, 100 + 2 * i, minute=i) for i in range(25)]
res = dmi_adx(up_candles, di_period=5, adx_period=5)
computable_plus = [v for v in res.plus_di if v is not None]
computable_minus = [v for v in res.minus_di if v is not None]
computable_adx = [v for v in res.adx if v is not None]
check("DMI uptrend: +DI == 100 throughout", all(close(v, 100) for v in computable_plus), str(computable_plus[:3]))
check("DMI uptrend: -DI == 0 throughout", all(close(v, 0) for v in computable_minus), str(computable_minus[:3]))
check("ADX uptrend: has computable values", len(computable_adx) > 0)
check("ADX uptrend: == 100 once seeded (constant DX=100 series)", all(close(v, 100) for v in computable_adx),
      str(computable_adx[:3]))

# Strict downtrend: mirror image (degenerate zero-range candles) -> -DI == 100, +DI == 0.
down_candles = [mk(200 - 2 * i, 200 - 2 * i, 200 - 2 * i, 200 - 2 * i, minute=i) for i in range(25)]
res_down = dmi_adx(down_candles, di_period=5, adx_period=5)
cp = [v for v in res_down.plus_di if v is not None]
cm = [v for v in res_down.minus_di if v is not None]
check("DMI downtrend: +DI == 0 throughout", all(close(v, 0) for v in cp))
check("DMI downtrend: -DI == 100 throughout", all(close(v, 100) for v in cm))

# Flat/choppy series with no directional movement -> DI+ and DI- both stay
# at/near 0 (no expansion in either direction).
flat_candles = [mk(100, 101, 99, 100, minute=i) for i in range(20)]
res_flat = dmi_adx(flat_candles, di_period=5, adx_period=5)
cpf = [v for v in res_flat.plus_di if v is not None]
cmf = [v for v in res_flat.minus_di if v is not None]
check("DMI flat: +DI stays 0", all(close(v, 0) for v in cpf), str(cpf[:3]))
check("DMI flat: -DI stays 0", all(close(v, 0) for v in cmf), str(cmf[:3]))

check("DMI: n<2 returns all-None", all(v is None for v in dmi_adx([mk(1, 1, 1, 1)], 5, 5).plus_di))


# ============================================================
print("\n=== Aroon ===")
# ============================================================
# period=5, most recent bar (index -1) makes both a new period-high AND a
# new period-low (a single big-range bar) -> bars_since_high=0,
# bars_since_low=0 -> Aroon Up == Aroon Down == 100.
bars = [mk(100, 100 + i, 100 - i, 100, minute=i) for i in range(6)]
ar = aroon(bars, 5)
check("Aroon: last bar is both period high & low -> Up==Down==100",
      close(ar.up[5], 100) and close(ar.down[5], 100), f"up={ar.up[5]} down={ar.down[5]}")

# The period-high occurred exactly 2 bars ago, nothing since -> Aroon Up should
# reflect that: 100*(5-2)/5 = 60.
bars2 = [mk(100, 100, 90, 95, minute=0), mk(100, 105, 90, 95, minute=1),
         mk(100, 110, 90, 95, minute=2), mk(100, 130, 90, 95, minute=3),
         mk(100, 108, 90, 95, minute=4), mk(100, 106, 90, 95, minute=5)]
ar2 = aroon(bars2, 5)
check("Aroon: high 2 bars ago -> Up==60", close(ar2.up[5], 60), str(ar2.up[5]))
check("Aroon: period<=0 stays empty", all(v is None for v in aroon(bars, 0).up))


# ============================================================
print("\n=== VWAP (with TWAP fallback) ===")
# ============================================================
# Hand-computed cumulative VWAP over 3 bars with real volume.
vwap_candles = [mk(100, 102, 98, 100, v=10, minute=0),   # tp=(102+98+100)/3=100
                mk(100, 106, 100, 104, v=20, minute=1),  # tp=(106+100+104)/3=103.333...
                mk(104, 108, 102, 106, v=30, minute=2)]  # tp=(108+102+106)/3=105.333...
v = vwap(vwap_candles)
tp0 = typical_price(vwap_candles[0])
tp1 = typical_price(vwap_candles[1])
tp2 = typical_price(vwap_candles[2])
expected_vwap1 = (tp0 * 10 + tp1 * 20) / 30
expected_vwap2 = (tp0 * 10 + tp1 * 20 + tp2 * 30) / 60
check("VWAP: bar0 == its own typical price (only volume so far)", close(v.value[0], tp0), str(v.value[0]))
check("VWAP: bar1 hand-computed", close(v.value[1], expected_vwap1), f"{v.value[1]} vs {expected_vwap1}")
check("VWAP: bar2 hand-computed", close(v.value[2], expected_vwap2), f"{v.value[2]} vs {expected_vwap2}")
check("VWAP: no TWAP fallback used when volume is present", not any(v.is_twap_fallback))

# Zero-volume session -> TWAP fallback (plain average of typical price).
zero_vol_candles = [mk(100, 102, 98, 100, v=0, minute=0), mk(100, 106, 100, 104, v=0, minute=1)]
v0 = vwap(zero_vol_candles)
check("VWAP: zero volume triggers TWAP fallback", all(v0.is_twap_fallback))
tp0z = typical_price(zero_vol_candles[0])
tp1z = typical_price(zero_vol_candles[1])
check("VWAP: TWAP bar0 == its own typical price", close(v0.value[0], tp0z))
check("VWAP: TWAP bar1 == average of both typical prices", close(v0.value[1], (tp0z + tp1z) / 2))

# New IST calendar day resets the accumulator.
day2_candles = list(vwap_candles) + [
    {**mk(200, 202, 198, 200, v=5), "date": vwap_candles[-1]["date"] + timedelta(days=1)}
]
v2 = vwap(day2_candles)
check("VWAP: new day resets accumulator", close(v2.value[3], typical_price(day2_candles[3])), str(v2.value[3]))


# ============================================================
print("\n=== Price structure (swing points) & S/R clustering ===")
# ============================================================
# Simple V-shape: down then up, fractal_bars=2 -> the single low at the
# bottom of the V should be detected as a swing low.
v_shape = [mk(100, 100, 100 - i, 100, minute=i) for i in range(5)] + \
          [mk(100, 100, 90 + i, 100, minute=5 + i) for i in range(5)]
pts = swing_points(v_shape, fractal_bars=2)
lows = [p for p in pts if p.kind == "low"]
check("Swing points: V-shape bottom detected as a low", any(close(p.price, 90) for p in lows),
      str([p.price for p in lows]))

# Two touches at the same level (within clustering distance) should form one
# S/R level with touches==2; a lone outlier swing should not qualify (needs
# sr_min_touches=2).
touch_series = (
    [mk(100, 100, 100 - i, 100, minute=i) for i in range(3)] +          # dips to 97
    [mk(100, 100 + i, 97, 100, minute=3 + i) for i in range(3)] +        # back up
    [mk(100, 100, 97 - i * 0.05, 100, minute=6 + i) for i in range(3)] + # dips to ~97 again
    [mk(100, 100 + i, 96.9, 100, minute=9 + i) for i in range(3)]
)
atrs = [1.0] * len(touch_series)  # fixed ATR so clustering distance is deterministic
levels = support_resistance_levels(
    touch_series, sr_cluster_atr_multiple=0.5, sr_min_touches=2,
    fractal_bars=2, lookback_bars=len(touch_series), atr_values=atrs,
)
check("S/R: at least one clustered level found", len(levels) >= 1, str(levels))
if levels:
    check("S/R: clustered level has >=2 touches", levels[0].touches >= 2, str(levels[0]))

near = nearest_level(levels, price=99, direction=-1)
check("nearest_level: returns a level below price when direction=-1",
      near is None or near.price < 99, str(near))
check("nearest_level: empty levels -> None", nearest_level([], price=100, direction=1) is None)


# ============================================================
print("\n=== Opening range ===")
# ============================================================
or_candles = [mk(100, 100 + i, 100 - i, 100, minute=i) for i in range(20)]
orv = opening_range(or_candles, minutes=15)
check("Opening range: high == max of first 15 bars", close(orv.high, max(c["high"] for c in or_candles[:15])),
      str(orv.high))
check("Opening range: low == min of first 15 bars", close(orv.low, min(c["low"] for c in or_candles[:15])),
      str(orv.low))
check("Opening range: empty input -> None", opening_range([], 15) is None)


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
