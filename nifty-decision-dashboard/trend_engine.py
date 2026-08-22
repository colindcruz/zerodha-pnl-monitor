"""
Trend Engine (5-min). Pure: IndicatorSnapshot -> TrendResult. Combines five
INDEPENDENT +-1 votes (Aroon, EMA fast/slow structure, VWAP position+slope,
DMI, price structure) into a -5..+5 score, which maps to a TrendDirection
band. EMA periods are IndicatorConfig.ema_fast_period/ema_slow_period.
ADX explicitly does NOT vote here — it only feeds Trend Strength, a wholly
separate classification of how convincingly the market is trending, not
which way. A market can score STRONG BULL on direction while ADX still
reads WEAK (a fast, thin move that hasn't built real strength yet), and the
UI must show both, never collapse them into one number.

Momentum State is a third, independent classification: it looks at whether
ADX and the DI+/DI- spread are still expanding or already contracting, which
is NOT the same question as "how high is ADX right now." The canonical case
this exists to catch: ADX is still numerically high (so Trend Strength still
reads STRONG) but has stopped rising and the DI spread is narrowing — that
reads MATURING here, not STRENGTHENING, even though a naive "ADX is high"
check would say the opposite.

Opening-Range Breakout (ORB) fallback: for roughly the first hour of every
session (see config.py's orb_fallback_minutes), the 5-vote system above has
no usable data yet — most of its indicators need far more bar history than
exists that early. Rather than sit on NEUTRAL/insufficient_data through an
obvious early move, evaluate_trend() substitutes a breakout read during
this window (_evaluate_orb()), measured against the SECOND 5-min candle's
own high/low (see _orb_reference_range() — deliberately skipping the noisy
first bar, a common ORB variant) — see TrendReadMode / TrendResult.mode,
which tells the caller (and the UI) which method actually produced a given
read.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from candles import session_start
from config import TrendEngineConfig
from indicators import OpeningRange
from snapshot import IndicatorSnapshot, TimeframeIndicators


class TrendDirection(str, Enum):
    STRONG_BULL = "STRONG_BULL"
    BULL = "BULL"
    WEAK_BULL = "WEAK_BULL"
    NEUTRAL = "NEUTRAL"
    WEAK_BEAR = "WEAK_BEAR"
    BEAR = "BEAR"
    STRONG_BEAR = "STRONG_BEAR"


class TrendStrength(str, Enum):
    WEAK = "WEAK"
    DEVELOPING = "DEVELOPING"
    STRONG = "STRONG"
    VERY_STRONG = "VERY_STRONG"


class MomentumState(str, Enum):
    STRENGTHENING = "STRENGTHENING"
    STABLE = "STABLE"
    MATURING = "MATURING"
    DETERIORATING = "DETERIORATING"


class VolatilityLevel(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


class TrendReadMode(str, Enum):
    STANDARD = "STANDARD"                            # the full 5-vote system
    OPENING_RANGE_BREAKOUT = "OPENING_RANGE_BREAKOUT"  # early-session fallback — see module docstring


@dataclass
class TrendResult:
    direction: TrendDirection
    score: int                     # -5..+5
    strength: TrendStrength
    momentum: MomentumState
    volatility: VolatilityLevel
    adx_direction: Optional[str]   # "UP" | "DOWN" | "FLAT" | None (insufficient data)
    votes: dict                    # {"aroon": -1|0|1, ...} — one entry per voting signal; empty in ORB mode
    reasons: list                  # human-readable one-liners, for Detailed Mode / event feed
    insufficient_data: bool = False
    mode: TrendReadMode = TrendReadMode.STANDARD


def _last_valid(series: list) -> Optional[float]:
    for v in reversed(series):
        if v is not None:
            return v
    return None


def _vote_aroon(tf: TimeframeIndicators, cfg: TrendEngineConfig) -> tuple[int, str]:
    up, down = tf.aroon.up[-1], tf.aroon.down[-1]
    if up is None or down is None:
        return 0, "Aroon: insufficient data"
    diff = up - down
    if diff >= cfg.aroon_min_separation:
        return 1, f"Aroon: up {up:.0f} well above down {down:.0f} -> bullish"
    if -diff >= cfg.aroon_min_separation:
        return -1, f"Aroon: down {down:.0f} well above up {up:.0f} -> bearish"
    return 0, f"Aroon: up {up:.0f} / down {down:.0f} not separated enough -> neutral"


def _vote_ema_structure(tf: TimeframeIndicators, cfg: TrendEngineConfig,
                         fast_period: int, slow_period: int) -> tuple[int, str]:
    fast, slow, atr_v = tf.ema_fast[-1], tf.ema_slow[-1], tf.atr[-1]
    if fast is None or slow is None or atr_v is None or atr_v == 0:
        return 0, "EMA structure: insufficient data"
    threshold = cfg.ema_structure_atr_threshold * atr_v
    diff = fast - slow
    if diff >= threshold:
        return 1, f"EMA structure: EMA{fast_period} above EMA{slow_period} by {diff:.1f} -> bullish"
    if -diff >= threshold:
        return -1, f"EMA structure: EMA{fast_period} below EMA{slow_period} by {-diff:.1f} -> bearish"
    return 0, f"EMA structure: EMA{fast_period}/{slow_period} too close -> neutral"


def _vote_vwap(tf: TimeframeIndicators, cfg: TrendEngineConfig) -> tuple[int, str]:
    vwap_price = tf.vwap_price[-1] if tf.vwap_price else None
    vwap_now = tf.vwap_value[-1]
    atr_v = tf.atr[-1]
    if vwap_price is None or vwap_now is None or atr_v is None or atr_v == 0:
        return 0, "VWAP: insufficient data"

    lookback = cfg.vwap_slope_lookback_bars
    vwap_prior = tf.vwap_value[-1 - lookback] if len(tf.vwap_value) > lookback else None

    position_signal = 0
    dist = vwap_price - vwap_now
    threshold = cfg.vwap_position_atr_threshold * atr_v
    if dist >= threshold:
        position_signal = 1
    elif -dist >= threshold:
        position_signal = -1

    slope_signal = 0
    if vwap_prior is not None:
        if vwap_now > vwap_prior:
            slope_signal = 1
        elif vwap_now < vwap_prior:
            slope_signal = -1

    combined = position_signal + slope_signal
    vote = 1 if combined > 0 else -1 if combined < 0 else 0
    return vote, (f"VWAP: price {'above' if position_signal >= 0 else 'below'} VWAP, "
                  f"slope {'up' if slope_signal > 0 else 'down' if slope_signal < 0 else 'flat'}")


def _vote_dmi(tf: TimeframeIndicators, cfg: TrendEngineConfig) -> tuple[int, str]:
    pdi, mdi = tf.dmi_adx.plus_di[-1], tf.dmi_adx.minus_di[-1]
    if pdi is None or mdi is None:
        return 0, "DMI: insufficient data"
    diff = pdi - mdi
    if diff >= cfg.dmi_min_separation:
        return 1, f"DMI: +DI {pdi:.0f} above -DI {mdi:.0f} -> bullish"
    if -diff >= cfg.dmi_min_separation:
        return -1, f"DMI: -DI {mdi:.0f} above +DI {pdi:.0f} -> bearish"
    return 0, f"DMI: +DI {pdi:.0f} / -DI {mdi:.0f} not separated enough -> neutral"


def _vote_price_structure(snapshot: IndicatorSnapshot) -> tuple[int, str]:
    highs = [p for p in snapshot.swing_points_5m if p.kind == "high" and p.label is not None]
    lows = [p for p in snapshot.swing_points_5m if p.kind == "low" and p.label is not None]
    if not highs or not lows:
        return 0, "Price structure: not enough swing points yet"
    last_high, last_low = highs[-1].label, lows[-1].label
    if last_high == "HH" and last_low == "HL":
        return 1, "Price structure: higher highs + higher lows -> bullish"
    if last_high == "LH" and last_low == "LL":
        return -1, "Price structure: lower highs + lower lows -> bearish"
    return 0, f"Price structure: mixed ({last_high}/{last_low}) -> neutral"


def _direction_from_score(score: int, cfg: TrendEngineConfig) -> TrendDirection:
    a = abs(score)
    if a >= cfg.strong_band:
        return TrendDirection.STRONG_BULL if score > 0 else TrendDirection.STRONG_BEAR
    if a >= cfg.moderate_band:
        return TrendDirection.BULL if score > 0 else TrendDirection.BEAR
    if a >= cfg.weak_band:
        return TrendDirection.WEAK_BULL if score > 0 else TrendDirection.WEAK_BEAR
    return TrendDirection.NEUTRAL


def _trend_strength(adx: Optional[float], cfg: TrendEngineConfig) -> TrendStrength:
    if adx is None:
        return TrendStrength.WEAK
    if adx >= cfg.adx_very_strong:
        return TrendStrength.VERY_STRONG
    if adx >= cfg.adx_strong:
        return TrendStrength.STRONG
    if adx >= cfg.adx_developing:
        return TrendStrength.DEVELOPING
    return TrendStrength.WEAK


def _momentum_state(tf: TimeframeIndicators, cfg: TrendEngineConfig) -> tuple[MomentumState, bool]:
    lookback = cfg.momentum_lookback_bars
    adx_series = tf.dmi_adx.adx
    pdi_series = tf.dmi_adx.plus_di
    mdi_series = tf.dmi_adx.minus_di
    if len(adx_series) <= lookback:
        return MomentumState.STABLE, True, None

    adx_now, adx_before = adx_series[-1], adx_series[-1 - lookback]
    pdi_now, mdi_now = pdi_series[-1], mdi_series[-1]
    pdi_before, mdi_before = pdi_series[-1 - lookback], mdi_series[-1 - lookback]
    if None in (adx_now, adx_before, pdi_now, mdi_now, pdi_before, mdi_before):
        return MomentumState.STABLE, True, None

    adx_delta = adx_now - adx_before
    spread_now = abs(pdi_now - mdi_now)
    spread_before = abs(pdi_before - mdi_before)
    spread_delta = spread_now - spread_before
    eps = cfg.momentum_flat_epsilon
    adx_direction = "UP" if adx_delta > eps else "DOWN" if adx_delta < -eps else "FLAT"

    if adx_delta > eps and spread_delta > eps:
        return MomentumState.STRENGTHENING, False, adx_direction
    if adx_delta < -eps and spread_delta < -eps:
        return MomentumState.DETERIORATING, False, adx_direction
    if adx_delta <= eps and spread_delta < -eps:
        # ADX not clearly still rising, but directional conviction (DI spread)
        # is already narrowing — high-but-fading, not accelerating.
        return MomentumState.MATURING, False, adx_direction
    return MomentumState.STABLE, False, adx_direction


def classify_volatility(tf: TimeframeIndicators, cfg: TrendEngineConfig) -> VolatilityLevel:
    """ATR now vs ATR `volatility_lookback_bars` bars ago, as a ratio — an
    independent read from Extension (location_engine.py), which measures
    DISTANCE traveled from VWAP/EMA20, not the RATE candles are widening or
    narrowing. Defaults to NORMAL on insufficient data (a volatility read
    nobody can support yet shouldn't itself read as alarmingly HIGH or
    suspiciously LOW)."""
    lookback = cfg.volatility_lookback_bars
    series = tf.atr
    if len(series) <= lookback:
        return VolatilityLevel.NORMAL
    now, before = series[-1], series[-1 - lookback]
    if now is None or before is None or before == 0:
        return VolatilityLevel.NORMAL
    ratio = now / before
    if ratio >= cfg.volatility_expansion_ratio:
        return VolatilityLevel.HIGH
    if ratio <= cfg.volatility_contraction_ratio:
        return VolatilityLevel.LOW
    return VolatilityLevel.NORMAL


def _elapsed_session_minutes(snapshot: IndicatorSnapshot) -> Optional[float]:
    """Minutes from 09:15 IST to the latest 1-min candle — None if there's
    no candle data at all yet."""
    if not snapshot.candles_1m:
        return None
    last_date = snapshot.candles_1m[-1]["date"]
    return (last_date - session_start(last_date)).total_seconds() / 60


def _orb_reference_range(snapshot: IndicatorSnapshot):
    """The ORB fallback's own reference range: the high/low of the SECOND
    5-min candle (tf5.candles[1], i.e. 09:20-09:25 IST), deliberately
    skipping the first 5-min candle (09:15-09:20) — the opening print tends
    to carry the pre-market order-imbalance settling out and is often the
    single noisiest bar of the day, so anchoring the breakout range to the
    bar right after it is a common ORB variant meant to avoid that.

    Deliberately NOT the same thing as IndicatorConfig.opening_range_minutes
    (the 15-minute window key_levels.py's "Opening Range High/Low" shows) —
    that's a separate, general-purpose concept used elsewhere on the
    dashboard; this one is specific to the ORB fallback only. Returns None
    until that second 5-min candle has actually closed (the first ~10
    minutes of the session), in which case evaluate_trend() falls through
    to the standard path, same as it always has."""
    candles = snapshot.tf5.candles
    if len(candles) < 2:
        return None
    second = candles[1]
    return OpeningRange(high=second["high"], low=second["low"])


def _evaluate_orb(snapshot: IndicatorSnapshot, cfg: TrendEngineConfig) -> Optional[TrendResult]:
    """Opening-range breakout read — see module docstring and
    _orb_reference_range()."""
    orange = _orb_reference_range(snapshot)
    if orange is None or not snapshot.candles_1m:
        return None
    or_width = orange.high - orange.low
    if or_width <= 0:
        return None

    price = snapshot.candles_1m[-1]["close"]
    if price > orange.high:
        sign, distance, edge_desc = 1, price - orange.high, f"above the 2nd 5-min candle's high ({orange.high:.1f})"
    elif price < orange.low:
        sign, distance, edge_desc = -1, orange.low - price, f"below the 2nd 5-min candle's low ({orange.low:.1f})"
    else:
        sign, distance, edge_desc = 0, 0.0, f"still inside the 2nd 5-min candle's range ({orange.low:.1f}-{orange.high:.1f})"

    ratio = distance / or_width
    if sign == 0 or ratio <= cfg.orb_weak_ratio:
        score = 0
    elif ratio < cfg.orb_moderate_ratio:
        score = sign * 1
    elif ratio < cfg.orb_strong_ratio:
        score = sign * 3
    else:
        score = sign * 5
    direction = _direction_from_score(score, cfg)

    reason = (f"Opening range breakout: price {distance:.1f} pts {edge_desc} "
              f"(range width {or_width:.1f} pts) -> {direction.value}")
    return TrendResult(
        direction=direction, score=score, strength=TrendStrength.WEAK,
        momentum=MomentumState.STABLE, volatility=VolatilityLevel.NORMAL, adx_direction=None,
        votes={}, reasons=[reason, "Standard trend read not yet available this early in the session "
                                    "(indicators still warming up) — using opening-range breakout instead."],
        insufficient_data=True, mode=TrendReadMode.OPENING_RANGE_BREAKOUT,
    )


def evaluate_trend(snapshot: IndicatorSnapshot, cfg: TrendEngineConfig = None) -> TrendResult:
    cfg = cfg or TrendEngineConfig()
    tf = snapshot.tf5

    elapsed = _elapsed_session_minutes(snapshot)
    if elapsed is not None and elapsed < cfg.orb_fallback_minutes:
        orb_result = _evaluate_orb(snapshot, cfg)
        if orb_result is not None:
            return orb_result
        # Opening range itself hasn't formed yet — fall through below.

    votes = {}
    reasons = []
    for name, fn in (
        ("aroon", lambda: _vote_aroon(tf, cfg)),
        ("ema_structure", lambda: _vote_ema_structure(tf, cfg, snapshot.config.ema_fast_period,
                                                       snapshot.config.ema_slow_period)),
        ("vwap", lambda: _vote_vwap(tf, cfg)),
        ("dmi", lambda: _vote_dmi(tf, cfg)),
        ("price_structure", lambda: _vote_price_structure(snapshot)),
    ):
        vote, reason = fn()
        votes[name] = vote
        reasons.append(reason)

    score = sum(votes.values())
    direction = _direction_from_score(score, cfg)
    strength = _trend_strength(_last_valid(tf.dmi_adx.adx), cfg)
    momentum, insufficient, adx_direction = _momentum_state(tf, cfg)
    volatility = classify_volatility(tf, cfg)

    return TrendResult(
        direction=direction, score=score, strength=strength, momentum=momentum,
        volatility=volatility, adx_direction=adx_direction,
        votes=votes, reasons=reasons, insufficient_data=insufficient,
    )
