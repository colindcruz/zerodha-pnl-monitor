"""
Every threshold, weight, and lookback period used anywhere in the four engines
lives here — nothing hard-coded inline in indicators.py/trend_engine.py/etc.
These are reasoning-based starting values (mirroring long_option_trade_engine.py's
own EngineConfig docstring convention), not yet backtested; expect to retune via
this file alone once real tick data exists.

One dataclass per engine, plus IndicatorConfig for the shared indicator periods
they all draw from.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IndicatorConfig:
    """Lookback periods for the shared indicator layer (indicators.py). Same
    values feed both the 5-min Trend Engine and the 2-min Entry Engine — each
    engine's own candle series (built at its own timeframe) is what actually
    differs, not the period counts applied to that series."""

    # 9/21 — a standard scalping EMA pair, not the more common swing-trading
    # 20/50 — chosen specifically to warm up faster on 5-min bars (45/105
    # min vs. 100/250 min), closing most of the gap to when the ORB
    # fallback's own window ends (60 min; see TrendEngineConfig). Every
    # place that describes these in a message to the user reads the actual
    # configured periods back out (snapshot.config.ema_fast_period/
    # ema_slow_period) rather than hard-coding "20"/"50", so retuning this
    # again later doesn't leave stale numbers anywhere.
    ema_fast_period: int = 9
    ema_slow_period: int = 21
    atr_period: int = 14
    aroon_period: int = 14
    dmi_period: int = 14         # Wilder-smoothed DI+/DI-/ADX, all share this period
    adx_period: int = 14         # second Wilder smoothing pass, applied to DX

    opening_range_minutes: int = 15  # 09:15-09:30 IST

    # Support/resistance clustering: swing points within this many ATRs of each
    # other are merged into one level; a level needs at least this many distinct
    # touches to count as "real" rather than noise.
    sr_cluster_atr_multiple: float = 0.15
    sr_min_touches: int = 2
    sr_lookback_bars: int = 100  # on the 5-min series

    # Price-structure swing-fractal detection: a bar is a swing high/low if it's
    # the local extreme within this many bars on each side.
    swing_fractal_bars: int = 2


@dataclass
class TrendEngineConfig:
    """5-minute Trend Engine. Five independent ±1 votes (Aroon, EMA
    fast/slow structure, VWAP position+slope, DMI, price structure) sum to
    a -5..+5 score; ADX does NOT vote — it only feeds Trend Strength, a
    separate classification. See trend_engine.py's module docstring for
    why. EMA periods themselves live in IndicatorConfig (ema_fast_period/
    ema_slow_period), shared with the Entry Engine."""

    # Score -> TrendDirection band edges (symmetric around 0).
    strong_band: int = 4     # |score| >= 4 -> STRONG BULL/BEAR
    moderate_band: int = 2    # |score| >= 2 -> BULL/BEAR
    weak_band: int = 1        # |score| >= 1 -> WEAK BULL/BEAR; 0 -> NEUTRAL

    # DMI vote: DI+ vs DI- with a minimum separation to avoid flip-flopping when
    # they're nearly crossed.
    dmi_min_separation: float = 2.0

    # VWAP vote: price above/below VWAP by at least this many ATRs counts as a
    # position vote; VWAP slope (current vs N bars ago) contributes the rest.
    vwap_position_atr_threshold: float = 0.1
    vwap_slope_lookback_bars: int = 3

    # Aroon vote: Aroon-Up/Aroon-Down separation needed to count as directional.
    aroon_min_separation: float = 20.0

    # EMA fast/slow structure vote: EMA fast above/below EMA slow by at
    # least this many ATRs, to avoid voting on a near-flat cross.
    ema_structure_atr_threshold: float = 0.05

    # Trend Strength (ADX-based, independent classification, does not feed the
    # -5..+5 score). Three boundaries -> four bands: < adx_developing -> WEAK;
    # [adx_developing, adx_strong) -> DEVELOPING; [adx_strong, adx_very_strong)
    # -> STRONG; >= adx_very_strong -> VERY STRONG.
    adx_developing: float = 20.0
    adx_strong: float = 30.0
    adx_very_strong: float = 40.0

    # Momentum State: classifies ADX's own trajectory + DI spread trajectory
    # over this many bars, independent of ADX's absolute level — this is what
    # lets "high but falling ADX" read as MATURING/DETERIORATING rather than
    # STRENGTHENING just because the level itself is still high.
    momentum_lookback_bars: int = 3
    momentum_flat_epsilon: float = 0.5  # |delta| below this counts as "flat", not rising/falling

    # Volatility: ATR now vs ATR `volatility_lookback_bars` bars ago, as a
    # ratio — expanding fast enough counts as HIGH, contracting enough as
    # LOW, otherwise NORMAL. An independent read from Extension (which is
    # about DISTANCE traveled, not the RATE candles are widening/narrowing).
    # lookback_bars=5 (not fewer): ATR is Wilder-smoothed over atr_period,
    # so after only 3 smoothing steps the ratio's floor as the new true
    # range shrinks toward zero asymptotes to (1 - 1/atr_period)^3 ≈ 0.80 —
    # i.e. LOW would be almost mathematically unreachable at 3 bars given
    # the 0.80 contraction threshold below. 5 steps pushes that floor to
    # ≈0.69, comfortably below 0.80 for a genuinely sharp contraction.
    volatility_lookback_bars: int = 5
    volatility_expansion_ratio: float = 1.25
    volatility_contraction_ratio: float = 0.80

    # Opening-Range Breakout fallback: the standard 5-vote system needs
    # ~70+ minutes of 5-min-bar history before ANY vote can compute at all
    # (Aroon/DMI need aroon_period/dmi_period bars; EMA structure needs
    # ema_slow_period bars, longer still) — for roughly the first hour of
    # every session it would otherwise read NEUTRAL/insufficient_data
    # throughout, even against an obvious early move. During this window,
    # direction/score are instead derived from a breakout read: how far
    # price has moved past the reference range's own high/low, in units of
    # that range's own width (see trend_engine.py's _evaluate_orb()). The
    # reference range is the SECOND 5-min candle's own high/low (not the
    # first — see _orb_reference_range()'s docstring for why — and NOT
    # IndicatorConfig.opening_range_minutes's 15-minute window, a separate
    # concept used elsewhere on the dashboard). Momentum/Volatility/ADX-
    # direction are NOT substituted — they stay honestly "insufficient
    # data" during this window, since ORB has no equivalent read for those.
    orb_fallback_minutes: int = 60
    orb_weak_ratio: float = 0.0      # any close beyond the reference range at all -> at least WEAK
    orb_moderate_ratio: float = 0.5   # beyond by 50% of the reference range's own width -> BULL/BEAR
    orb_strong_ratio: float = 1.0     # beyond by a full reference-range width -> STRONG_BULL/STRONG_BEAR


@dataclass
class EntryEngineConfig:
    """2-minute Entry Engine. 0-5 point score: EMA fast slope, proximity to
    8-bar extreme, wick-reversal quality, candle close location,
    confirmation candle. Each sub-score contributes at most 1 point. The
    EMA period itself is IndicatorConfig.ema_fast_period, shared with the
    Trend Engine."""

    extreme_lookback_bars: int = 8
    proximity_atr_threshold: float = 0.3   # within this many ATRs of the 8-bar extreme

    ema_slope_lookback_bars: int = 2
    ema_slope_atr_threshold: float = 0.05  # EMA fast must have moved at least this many ATRs

    # Wick-reversal quality: opposite-direction wick must be at least this
    # fraction of the candle's total range to count as a genuine rejection wick.
    wick_min_fraction_of_range: float = 0.35

    # Candle close location: close must be within this fraction of the
    # candle's range from the favorable extreme (0 = at the extreme, 1 = at
    # the opposite extreme) to score the point.
    close_location_max_fraction: float = 0.35

    # Confirmation candle: the bar after the signal bar must itself close in
    # the signal direction (higher close than open for bullish, lower for
    # bearish) to score the point — a signal is only ever scored using the bar
    # immediately after the setup bar, not the setup bar itself, so this point
    # is necessarily unavailable until one extra bar has closed.
    require_confirmation_close_beyond_signal_high_low: bool = True

    # Entry Engine score -> qualitative label band edges.
    strong_setup: int = 4   # score >= 4
    valid_setup: int = 3    # score >= 3
    # score < 3 -> WEAK / NO SETUP


@dataclass
class LocationEngineConfig:
    """Location & Extension Engine. Extension: ATR-normalized distance to
    VWAP/EMA fast. Runway/Location: direction-aware distance to the next
    S/R level plus reward-vs-stop ratio."""

    extension_normal_atr: float = 1.0        # < this -> NORMAL
    extension_extended_atr: float = 2.0       # < this (and >= normal) -> EXTENDED; >= this -> VERY EXTENDED

    # Runway: distance in ATRs from current price to the nearest S/R level in
    # the trade direction, versus distance to the initial stop (typically
    # initial_stop_atr_multiple ATRs on the other side) — the ratio is the
    # reward:stop the Runway grade is actually judging.
    initial_stop_atr_multiple: float = 1.0
    runway_excellent_reward_to_stop: float = 3.0
    runway_good_reward_to_stop: float = 2.0
    runway_marginal_reward_to_stop: float = 1.0
    # ratio below runway_marginal_reward_to_stop -> POOR

    # A runway measured against a level closer than this many ATRs is treated
    # as "no real room" (POOR) regardless of the nominal ratio — guards
    # against a technically-fine ratio computed off a S/R level that is
    # actually right on top of price.
    min_absolute_runway_atr: float = 0.5


@dataclass
class DecisionEngineConfig:
    """Combinator: Trend + Entry + Location -> Entry Permission / Trade
    Direction. The DO-NOT-CHASE override is the critical invariant here — a
    maxed Trend+Entry score must still resolve to DO NOT CHASE when Location
    is bad, never the other way around (a good Location can't rescue a weak
    Trend/Entry read either, it can only veto a strong one)."""

    min_trend_band_for_entry: int = 2     # trend score |x| must be >= this (BULL/BEAR or stronger)
    min_entry_score_for_entry: int = 3    # entry_engine.py's "valid_setup" threshold, duplicated here on purpose —
                                           # decision_engine.py must not import entry_engine.py's config to stay
                                           # independently testable/tunable
    veto_extension_levels: tuple = ("VERY_EXTENDED",)
    veto_runway_levels: tuple = ("POOR",)


@dataclass
class PositionEngineConfig:
    """Position Management Engine. Weighted deterioration checklist +
    hysteresis state machine. Weights encode the spec's stated importance
    hierarchy: price structure > important S/R > VWAP > EMA structure > DMI
    > Aroon > ADX. A transition requires BOTH score >= threshold AND
    distinct_signal_count >= min_signals — the count gate is what
    structurally guarantees a lone ADX wobble (weight 1, count 1) can never
    alone move the state, regardless of how the score threshold is tuned."""

    weights: dict = field(default_factory=lambda: {
        "price_structure": 6,
        "important_sr": 5,
        "vwap": 4,
        "ema_structure": 3,
        "dmi": 2,
        "aroon": 1,
        "adx": 1,
    })
    min_signals: int = 2

    # Enter thresholds (score must reach this to CANDIDATE a more severe state).
    maturing_enter_score: int = 3
    deteriorating_enter_score: int = 6
    at_risk_enter_score: int = 10
    failed_enter_score: int = 14

    # Exit (recovery) thresholds — deliberately lower than the matching enter
    # threshold, asymmetric on purpose (fast to warn, slow to clear).
    maturing_exit_score: int = 1
    deteriorating_exit_score: int = 3
    at_risk_exit_score: int = 6
    failed_exit_score: int = 10

    # Hysteresis bar counts (on the 5-min series, so e.g. confirm_bars=2 == 10 minutes).
    confirm_bars: int = 2
    recover_confirm_bars: int = 4  # > confirm_bars: recovery is slower than deterioration, by design


@dataclass
class DashboardConfig:
    indicator: IndicatorConfig = field(default_factory=IndicatorConfig)
    trend: TrendEngineConfig = field(default_factory=TrendEngineConfig)
    entry: EntryEngineConfig = field(default_factory=EntryEngineConfig)
    location: LocationEngineConfig = field(default_factory=LocationEngineConfig)
    decision: DecisionEngineConfig = field(default_factory=DecisionEngineConfig)
    position: PositionEngineConfig = field(default_factory=PositionEngineConfig)
