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
                      vwap_1m_value: list[Optional[float]], vwap_1m_is_twap: list[bool]) -> TimeframeIndicators:
    tf_candles = bucket_candles(one_min_candles, minutes)
    c = closes(tf_candles)
    vwap_value_by_bucket = sample_last_per_bucket(one_min_candles, vwap_1m_value, minutes)
    vwap_twap_by_bucket = sample_last_per_bucket(one_min_candles, vwap_1m_is_twap, minutes)
    return TimeframeIndicators(
        candles=tf_candles,
        ema_fast=ema(c, cfg.ema_fast_period),
        ema_slow=ema(c, cfg.ema_slow_period),
        atr=atr(tf_candles, cfg.atr_period),
        aroon=aroon(tf_candles, cfg.aroon_period),
        dmi_adx=dmi_adx(tf_candles, cfg.dmi_period, cfg.adx_period),
        vwap_value=[vwap_value_by_bucket.get(tc["date"]) for tc in tf_candles],
        vwap_is_twap=[vwap_twap_by_bucket.get(tc["date"], False) for tc in tf_candles],
    )


def build_snapshot(one_min_candles: list[dict], cfg: IndicatorConfig) -> IndicatorSnapshot:
    """one_min_candles: ascending, session-to-date 1-min candles (from
    candles.OneMinuteAccumulator.as_sorted_list(), seeded from backfill)."""
    v = vwap(one_min_candles)
    tf5 = _build_timeframe(one_min_candles, 5, cfg, v.value, v.is_twap_fallback)
    tf2 = _build_timeframe(one_min_candles, 2, cfg, v.value, v.is_twap_fallback)

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
