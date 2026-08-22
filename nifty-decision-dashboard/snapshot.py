"""
IndicatorSnapshot — the single composition point that turns raw 1-min candles
into everything the four engines consume. Built once per recompute tick by
state.py; each engine only ever sees this snapshot, never candles.py or
indicators.py directly — this is what keeps "four engines kept strictly
separate" real, and each independently unit-testable.

VWAP is a running total from session start, not a per-bucket statistic (see
live-dashboard/dashboard.html's own comment on this) — so it is ALWAYS
computed once at 1-min granularity and then *sampled* onto the 2-min/5-min
series at each bucket's last 1-min bar, never recomputed directly from
coarser bars (which would silently produce a different, wrong number). Every
other indicator (EMA/ATR/DMI/Aroon/price-structure/S-R) is timeframe-native
and computed directly on whichever bucketed series it belongs to.

VWAP's source candles can differ from every other indicator's: NIFTY 50 is
an index, not a traded instrument, so its own ticks may carry no real
volume at all (see vwap()'s TWAP fallback) — build_snapshot() accepts an
OPTIONAL separate `vwap_source_candles` series (e.g. the current-month
NIFTY futures contract, which DOES carry real traded volume) to compute a
genuine VWAP from instead, while every other indicator still comes from
`one_min_candles` (the index) as always. The two series don't need to share
timestamps 1:1 — VWAP values are sampled onto each tf5/tf2 bucket by WALL-
CLOCK bucket start (candles.bucket_start is instrument-agnostic, purely a
function of IST session time), so a futures candle at 09:20 correctly lines
up with the index's own 09:20 bucket even though they're different
instruments with independently-arriving ticks.

Because VWAP's own price can come from a different instrument than the
index, `TimeframeIndicators.vwap_price` carries the SAME-instrument price
paired with `vwap_value` (the futures close, sampled onto each bucket the
same way `vwap_value` itself is) — any "distance from VWAP" comparison
(trend_engine's VWAP vote, location_engine's extension distance,
position_engine's unfavorable-side check, state.py's display) must use
`vwap_price`, not `candles[-1]["close"]`, or it silently bakes in the
futures-index premium as a structural offset on every reading. Comparisons
against index-native indicators (EMA, S/R, price structure) correctly keep
using `candles[-1]["close"]`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from candles import bucket_candles, sample_last_per_bucket
from config import IndicatorConfig
from indicators import (
    AroonResult,
    DmiAdxResult,
    OpeningRange,
    SRLevel,
    SwingPoint,
    aroon,
    atr,
    closes,
    dmi_adx,
    ema,
    opening_range,
    support_resistance_levels,
    swing_points,
    vwap,
)


@dataclass
class TimeframeIndicators:
    candles: list[dict]
    ema_fast: list[Optional[float]]
    ema_slow: list[Optional[float]]
    atr: list[Optional[float]]
    aroon: AroonResult
    dmi_adx: DmiAdxResult
    vwap_value: list[Optional[float]]
    vwap_is_twap: list[bool]
    vwap_price: list[Optional[float]]


@dataclass
class IndicatorSnapshot:
    config: IndicatorConfig
    candles_1m: list[dict]
    tf5: TimeframeIndicators
    tf2: TimeframeIndicators
    opening_range: Optional[OpeningRange]
    swing_points_5m: list[SwingPoint]
    sr_levels: list[SRLevel]


def _build_timeframe(one_min_candles: list[dict], minutes: int, cfg: IndicatorConfig,
                      vwap_value_by_bucket: dict, vwap_twap_by_bucket: dict,
                      vwap_price_by_bucket: dict) -> TimeframeIndicators:
    tf_candles = bucket_candles(one_min_candles, minutes)
    c = closes(tf_candles)
    return TimeframeIndicators(
        candles=tf_candles,
        ema_fast=ema(c, cfg.ema_fast_period),
        ema_slow=ema(c, cfg.ema_slow_period),
        atr=atr(tf_candles, cfg.atr_period),
        aroon=aroon(tf_candles, cfg.aroon_period),
        dmi_adx=dmi_adx(tf_candles, cfg.dmi_period, cfg.adx_period),
        vwap_value=[vwap_value_by_bucket.get(tc["date"]) for tc in tf_candles],
        vwap_is_twap=[vwap_twap_by_bucket.get(tc["date"], False) for tc in tf_candles],
        vwap_price=[vwap_price_by_bucket.get(tc["date"]) for tc in tf_candles],
    )


def build_snapshot(one_min_candles: list[dict], cfg: IndicatorConfig,
                    vwap_source_candles: list[dict] = None) -> IndicatorSnapshot:
    """one_min_candles: ascending, session-to-date 1-min candles for the
    index/spot (from candles.OneMinuteAccumulator.as_sorted_list(), seeded
    from backfill) — used for every indicator. vwap_source_candles:
    OPTIONAL, same shape, from a DIFFERENT instrument that carries real
    volume (e.g. NIFTY futures) — used for VWAP only, if provided; falls
    back to computing VWAP from one_min_candles itself (with its own TWAP
    fallback for zero volume) when not given, i.e. today's original
    behavior. See module docstring."""
    vwap_source = vwap_source_candles if vwap_source_candles else one_min_candles
    v = vwap(vwap_source)

    vwap_source_closes = closes(vwap_source)
    vwap_value_by_bucket_5 = sample_last_per_bucket(vwap_source, v.value, 5)
    vwap_twap_by_bucket_5 = sample_last_per_bucket(vwap_source, v.is_twap_fallback, 5)
    vwap_price_by_bucket_5 = sample_last_per_bucket(vwap_source, vwap_source_closes, 5)
    vwap_value_by_bucket_2 = sample_last_per_bucket(vwap_source, v.value, 2)
    vwap_twap_by_bucket_2 = sample_last_per_bucket(vwap_source, v.is_twap_fallback, 2)
    vwap_price_by_bucket_2 = sample_last_per_bucket(vwap_source, vwap_source_closes, 2)

    tf5 = _build_timeframe(one_min_candles, 5, cfg, vwap_value_by_bucket_5, vwap_twap_by_bucket_5,
                            vwap_price_by_bucket_5)
    tf2 = _build_timeframe(one_min_candles, 2, cfg, vwap_value_by_bucket_2, vwap_twap_by_bucket_2,
                            vwap_price_by_bucket_2)

    sw = swing_points(tf5.candles, cfg.swing_fractal_bars)
    sr = support_resistance_levels(
        tf5.candles, cfg.sr_cluster_atr_multiple, cfg.sr_min_touches,
        cfg.swing_fractal_bars, cfg.sr_lookback_bars, tf5.atr,
    )
    orange = opening_range(one_min_candles, cfg.opening_range_minutes)

    return IndicatorSnapshot(
        config=cfg, candles_1m=one_min_candles, tf5=tf5, tf2=tf2,
        opening_range=orange, swing_points_5m=sw, sr_levels=sr,
    )
