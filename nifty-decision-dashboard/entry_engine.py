"""
Entry Engine (2-min). Pure: IndicatorSnapshot + candidate direction ->
EntryResult. A 0-5 point pullback-reversal setup score: EMA fast slope,
proximity to the 8-bar extreme, wick-reversal quality, candle close
location, confirmation candle — each worth at most 1 point.

All five sub-scores are evaluated against the SIGNAL bar (the second-to-last
2-min candle), except confirmation, which checks whether the bar AFTER it
(the most recent candle) actually confirmed the setup. This is deliberate,
not an off-by-one: a signal is only ever scored using the bar immediately
after the setup bar, never the setup bar itself, so the confirmation point
is structurally unavailable until one extra bar has closed — see
config.py's EntryEngineConfig docstring.

direction is "LONG" (bullish pullback-reversal: price pulled back near the
8-bar low, rejected it with a lower wick, closed strong, confirmed by a
break above the signal bar's high) or "SHORT" (the mirror image, using
Position.direction's convention from long_option_trade_engine.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from config import EntryEngineConfig
from snapshot import IndicatorSnapshot


class EntrySetupLabel(str, Enum):
    STRONG_SETUP = "STRONG_SETUP"
    VALID_SETUP = "VALID_SETUP"
    WEAK_SETUP = "WEAK_SETUP"
    NO_SETUP = "NO_SETUP"


@dataclass
class EntryResult:
    direction: str          # "LONG" | "SHORT"
    score: int               # 0..5
    label: EntrySetupLabel
    components: dict          # {"ema_slope": 0|1, ...}
    reasons: list
    insufficient_data: bool = False


def _label_for_score(score: int, cfg: EntryEngineConfig) -> EntrySetupLabel:
    if score >= cfg.strong_setup:
        return EntrySetupLabel.STRONG_SETUP
    if score >= cfg.valid_setup:
        return EntrySetupLabel.VALID_SETUP
    if score >= 1:
        return EntrySetupLabel.WEAK_SETUP
    return EntrySetupLabel.NO_SETUP


def evaluate_entry(snapshot: IndicatorSnapshot, direction: str, cfg: EntryEngineConfig = None) -> EntryResult:
    cfg = cfg or EntryEngineConfig()
    if direction not in ("LONG", "SHORT"):
        raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")

    tf = snapshot.tf2
    candles = tf.candles
    n = len(candles)
    if n < 2:
        return EntryResult(direction=direction, score=0, label=EntrySetupLabel.NO_SETUP,
                            components={}, reasons=["insufficient data (need >= 2 candles)"],
                            insufficient_data=True)

    signal_idx = n - 2
    confirm_idx = n - 1
    signal_bar = candles[signal_idx]
    confirm_bar = candles[confirm_idx]
    atr_v = tf.atr[signal_idx]

    components = {}
    reasons = []

    # 1. EMA fast slope, in the setup's favor, over the signal bar.
    lookback = cfg.ema_slope_lookback_bars
    prior_idx = signal_idx - lookback
    ema_now = tf.ema_fast[signal_idx]
    ema_before = tf.ema_fast[prior_idx] if prior_idx >= 0 else None
    slope_ok = False
    if ema_now is not None and ema_before is not None and atr_v:
        sign = 1 if direction == "LONG" else -1
        slope_ok = sign * (ema_now - ema_before) >= cfg.ema_slope_atr_threshold * atr_v
    components["ema_slope"] = 1 if slope_ok else 0
    reasons.append(f"EMA{snapshot.config.ema_fast_period} slope {'favorable' if slope_ok else 'not favorable'} "
                    f"for {direction}")

    # 2. Proximity to the 8-bar extreme (the pullback level being bought/sold).
    lb = cfg.extreme_lookback_bars
    window = candles[max(0, signal_idx - lb + 1):signal_idx + 1]
    extreme = min(c["low"] for c in window) if direction == "LONG" else max(c["high"] for c in window)
    proximity_ok = False
    if atr_v:
        proximity_ok = abs(signal_bar["close"] - extreme) <= cfg.proximity_atr_threshold * atr_v
    components["proximity"] = 1 if proximity_ok else 0
    reasons.append(f"close within {cfg.proximity_atr_threshold} ATR of {lb}-bar extreme"
                    if proximity_ok else f"close not near the {lb}-bar extreme")

    # 3. Wick-reversal quality (rejection wick opposite the setup direction).
    rng = signal_bar["high"] - signal_bar["low"]
    wick_ok = False
    if rng > 0:
        if direction == "LONG":
            wick = min(signal_bar["open"], signal_bar["close"]) - signal_bar["low"]
        else:
            wick = signal_bar["high"] - max(signal_bar["open"], signal_bar["close"])
        wick_ok = (wick / rng) >= cfg.wick_min_fraction_of_range
    components["wick_reversal"] = 1 if wick_ok else 0
    reasons.append("rejection wick present" if wick_ok else "no meaningful rejection wick")

    # 4. Candle close location (close near the favorable extreme of its own range).
    close_loc_ok = False
    if rng > 0:
        if direction == "LONG":
            frac_from_favorable = (signal_bar["high"] - signal_bar["close"]) / rng
        else:
            frac_from_favorable = (signal_bar["close"] - signal_bar["low"]) / rng
        close_loc_ok = frac_from_favorable <= cfg.close_location_max_fraction
    components["close_location"] = 1 if close_loc_ok else 0
    reasons.append("close near the favorable extreme" if close_loc_ok else "close not near the favorable extreme")

    # 5. Confirmation candle — the bar AFTER the signal bar, not the signal bar itself.
    if direction == "LONG":
        confirm_ok = confirm_bar["close"] > signal_bar["high"]
    else:
        confirm_ok = confirm_bar["close"] < signal_bar["low"]
    components["confirmation"] = 1 if confirm_ok else 0
    reasons.append("confirmation candle broke the signal bar's extreme" if confirm_ok
                    else "confirmation candle did not break the signal bar's extreme")

    score = sum(components.values())
    return EntryResult(direction=direction, score=score, label=_label_for_score(score, cfg),
                        components=components, reasons=reasons, insufficient_data=False)
