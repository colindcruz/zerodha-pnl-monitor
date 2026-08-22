"""
Key-levels panel: VWAP, previous-day High/Low/Close, opening-range High/Low,
and the nearest S/R levels above and below price — each with its
distance-in-points from the current price, per the spec's requirement for a
numeric (not just qualitative) levels panel. Pure: IndicatorSnapshot (+
optionally a PrevDayOHLC fetched separately, since that's outside today's
session candles) -> KeyLevelsResult.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from indicators import nearest_level
from snapshot import IndicatorSnapshot


@dataclass
class PrevDayOHLC:
    high: float
    low: float
    close: float


@dataclass
class KeyLevel:
    name: str
    price: float
    distance_points: float  # signed: positive = above current price, negative = below


@dataclass
class KeyLevelsResult:
    price: Optional[float]
    levels: list


def compute_key_levels(snapshot: IndicatorSnapshot, prev_day: Optional[PrevDayOHLC] = None) -> KeyLevelsResult:
    tf = snapshot.tf5
    if not tf.candles:
        return KeyLevelsResult(price=None, levels=[])

    price = tf.candles[-1]["close"]
    levels: list[KeyLevel] = []

    def add(name: str, level_price: Optional[float]) -> None:
        if level_price is not None:
            levels.append(KeyLevel(name=name, price=level_price, distance_points=level_price - price))

    add("VWAP", tf.vwap_value[-1])

    if prev_day is not None:
        add("Prev Day High", prev_day.high)
        add("Prev Day Low", prev_day.low)
        add("Prev Day Close", prev_day.close)
        # Classic floor-trader pivots (from prev-day OHLC — the standard
        # convention; no "close" of today exists yet to compute today's own).
        pivot = (prev_day.high + prev_day.low + prev_day.close) / 3.0
        add("Pivot (P)", pivot)
        add("R1", 2 * pivot - prev_day.low)
        add("S1", 2 * pivot - prev_day.high)

    if snapshot.opening_range is not None:
        add("Opening Range High", snapshot.opening_range.high)
        add("Opening Range Low", snapshot.opening_range.low)

    above = nearest_level(snapshot.sr_levels, price, direction=1)
    below = nearest_level(snapshot.sr_levels, price, direction=-1)
    if above is not None:
        add(f"Nearest {above.kind.capitalize()} Above", above.price)
    if below is not None:
        add(f"Nearest {below.kind.capitalize()} Below", below.price)

    levels.sort(key=lambda lv: lv.distance_points)
    return KeyLevelsResult(price=price, levels=levels)
