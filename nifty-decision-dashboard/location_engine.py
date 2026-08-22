"""
Location & Extension Engine. Pure: IndicatorSnapshot + candidate direction ->
LocationResult. Two independent classifications:

  Extension — how far price has already travelled from its "fair value"
  anchors (VWAP, EMA20), ATR-normalized: NORMAL / EXTENDED / VERY EXTENDED.
  Uses the MORE distant of VWAP/EMA20 (max, not average) — a price that's
  merely close to one of the two isn't "not extended," it needs to not be
  far from either.

  Runway/Location — direction-aware distance to the next S/R level versus
  the distance to the initial stop, i.e. the reward:stop ratio a trade
  taken right now would actually be risking: EXCELLENT / GOOD / MARGINAL /
  POOR.

This engine's sole reason for existing, per the module layout: a maxed
Trend+Entry score must still be overridable to DO NOT CHASE when Extension
is VERY_EXTENDED or Runway is POOR — decision_engine.py enforces that veto,
but the classification driving it lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from config import LocationEngineConfig
from indicators import nearest_level
from snapshot import IndicatorSnapshot


class ExtensionLevel(str, Enum):
    NORMAL = "NORMAL"
    EXTENDED = "EXTENDED"
    VERY_EXTENDED = "VERY_EXTENDED"


class RunwayLevel(str, Enum):
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    MARGINAL = "MARGINAL"
    POOR = "POOR"


@dataclass
class LocationResult:
    direction: str
    extension: ExtensionLevel
    extension_atr_distance: Optional[float]
    runway: RunwayLevel
    reward_to_stop: Optional[float]
    nearest_level_price: Optional[float]
    reasons: list
    insufficient_data: bool = False


def _extension(price: float, vwap_v: Optional[float], ema20: Optional[float],
                atr_v: Optional[float], cfg: LocationEngineConfig) -> tuple[ExtensionLevel, Optional[float], str]:
    if atr_v is None or atr_v == 0 or (vwap_v is None and ema20 is None):
        return ExtensionLevel.NORMAL, None, "Extension: insufficient data (defaulting to NORMAL)"

    dists = []
    if vwap_v is not None:
        dists.append(abs(price - vwap_v))
    if ema20 is not None:
        dists.append(abs(price - ema20))
    atr_dist = max(dists) / atr_v

    if atr_dist >= cfg.extension_extended_atr:
        level = ExtensionLevel.VERY_EXTENDED
    elif atr_dist >= cfg.extension_normal_atr:
        level = ExtensionLevel.EXTENDED
    else:
        level = ExtensionLevel.NORMAL
    return level, atr_dist, f"Extension: {atr_dist:.2f} ATR from VWAP/EMA20 -> {level.value}"


def _runway(price: float, direction: str, sr_levels, atr_v: Optional[float],
            cfg: LocationEngineConfig) -> tuple[RunwayLevel, Optional[float], Optional[float], str]:
    if atr_v is None or atr_v == 0:
        return RunwayLevel.MARGINAL, None, None, "Runway: insufficient data (defaulting to MARGINAL)"

    look_dir = 1 if direction == "LONG" else -1
    level = nearest_level(sr_levels, price, look_dir)
    stop_distance = cfg.initial_stop_atr_multiple * atr_v

    if level is None:
        return RunwayLevel.GOOD, None, None, ("Runway: no known S/R level in this direction — "
                                               "treated as GOOD (clear, but unverified) rather than EXCELLENT")

    reward = abs(level.price - price)
    if reward < cfg.min_absolute_runway_atr * atr_v:
        return (RunwayLevel.POOR, reward / stop_distance if stop_distance else None, level.price,
                f"Runway: nearest level only {reward:.1f} pts away (< {cfg.min_absolute_runway_atr} ATR) -> POOR")

    ratio = reward / stop_distance if stop_distance else None
    if ratio is None:
        return RunwayLevel.MARGINAL, None, level.price, "Runway: stop distance is zero, cannot rate reward:stop"

    if ratio >= cfg.runway_excellent_reward_to_stop:
        rl = RunwayLevel.EXCELLENT
    elif ratio >= cfg.runway_good_reward_to_stop:
        rl = RunwayLevel.GOOD
    elif ratio >= cfg.runway_marginal_reward_to_stop:
        rl = RunwayLevel.MARGINAL
    else:
        rl = RunwayLevel.POOR
    return rl, ratio, level.price, f"Runway: reward:stop {ratio:.2f} to nearest level @ {level.price:.1f} -> {rl.value}"


def evaluate_location(snapshot: IndicatorSnapshot, direction: str, cfg: LocationEngineConfig = None) -> LocationResult:
    cfg = cfg or LocationEngineConfig()
    if direction not in ("LONG", "SHORT"):
        raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")

    tf = snapshot.tf5
    if not tf.candles:
        return LocationResult(direction=direction, extension=ExtensionLevel.NORMAL, extension_atr_distance=None,
                               runway=RunwayLevel.MARGINAL, reward_to_stop=None, nearest_level_price=None,
                               reasons=["insufficient data"], insufficient_data=True)

    price = tf.candles[-1]["close"]
    vwap_v = tf.vwap_value[-1]
    ema20 = tf.ema_fast[-1]
    atr_v = tf.atr[-1]

    ext_level, ext_dist, ext_reason = _extension(price, vwap_v, ema20, atr_v, cfg)
    run_level, ratio, level_price, run_reason = _runway(price, direction, snapshot.sr_levels, atr_v, cfg)

    insufficient = ext_dist is None or atr_v is None
    return LocationResult(
        direction=direction, extension=ext_level, extension_atr_distance=ext_dist,
        runway=run_level, reward_to_stop=ratio, nearest_level_price=level_price,
        reasons=[ext_reason, run_reason], insufficient_data=insufficient,
    )
