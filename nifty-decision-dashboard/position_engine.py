"""
Position Management Engine. Stateful hysteresis state machine — mirrors
long_option_trade_engine.py's TradeManagementEngine/Position style
(direction-agnostic via a `direction` argument instead of Position.sign, a
mutable engine object that owns transition state instead of a pure function,
one instance per tracked position). Pure otherwise: no I/O, no broker calls,
consumes IndicatorSnapshot ticks and returns a PositionHealthResult.

State ladder (severity-ordered): TREND_HEALTHY -> TREND_MATURING ->
MOMENTUM_DETERIORATING -> STRUCTURE_AT_RISK -> TREND_FAILED. Each tick scores
a weighted checklist against the position's direction (price structure >
important S/R > VWAP > EMA structure > DMI > Aroon > ADX, per config.py's
PositionEngineConfig) and only escalates to a more severe state when BOTH the
weighted score clears that state's enter threshold AND at least
min_signals distinct signals contributed — the count gate is what
structurally guarantees a lone ADX wobble (weight 1, count 1) can never by
itself move the state, no matter how the score threshold is tuned.

Hysteresis is a single-step Schmitt trigger per state boundary: escalating
one level requires `confirm_bars` consecutive ticks whose raw candidate is
"one level worse"; recovering one level requires `recover_confirm_bars`
consecutive ticks whose score has dropped below that state's (lower,
asymmetric) exit threshold. Recovery is deliberately slower than
deterioration — fast to warn, slow to clear, matching a capital-protection
bias. Transitions are single-step per tick (never a multi-level cascade in
one update) — unlike long_option_trade_engine.py's threshold cascade, which
exists to handle a price GAP across several point-thresholds in one tick,
this engine's input is a slowly-varying discretized score, not a raw price,
so a single-level-per-tick model is the right fit and keeps the hysteresis
bookkeeping to one pending-candidate/streak pair instead of one per level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from config import PositionEngineConfig
from snapshot import IndicatorSnapshot


class PositionHealthState(str, Enum):
    TREND_HEALTHY = "TREND_HEALTHY"
    TREND_MATURING = "TREND_MATURING"
    MOMENTUM_DETERIORATING = "MOMENTUM_DETERIORATING"
    STRUCTURE_AT_RISK = "STRUCTURE_AT_RISK"
    TREND_FAILED = "TREND_FAILED"


_LADDER = [
    PositionHealthState.TREND_HEALTHY,
    PositionHealthState.TREND_MATURING,
    PositionHealthState.MOMENTUM_DETERIORATING,
    PositionHealthState.STRUCTURE_AT_RISK,
    PositionHealthState.TREND_FAILED,
]
_SEVERITY = {s: i for i, s in enumerate(_LADDER)}


def state_severity(state: PositionHealthState) -> int:
    """Public accessor for _SEVERITY — used by state.py to find the single
    worst-health position across everything currently tracked, without
    reaching into this module's private ladder ordering."""
    return _SEVERITY[state]


def _enter_threshold(state: PositionHealthState, cfg: PositionEngineConfig) -> Optional[int]:
    return {
        PositionHealthState.TREND_MATURING: cfg.maturing_enter_score,
        PositionHealthState.MOMENTUM_DETERIORATING: cfg.deteriorating_enter_score,
        PositionHealthState.STRUCTURE_AT_RISK: cfg.at_risk_enter_score,
        PositionHealthState.TREND_FAILED: cfg.failed_enter_score,
    }.get(state)


def _exit_threshold(state: PositionHealthState, cfg: PositionEngineConfig) -> Optional[int]:
    return {
        PositionHealthState.TREND_MATURING: cfg.maturing_exit_score,
        PositionHealthState.MOMENTUM_DETERIORATING: cfg.deteriorating_exit_score,
        PositionHealthState.STRUCTURE_AT_RISK: cfg.at_risk_exit_score,
        PositionHealthState.TREND_FAILED: cfg.failed_exit_score,
    }.get(state)


@dataclass
class DeteriorationScore:
    total: int
    signal_count: int
    signals: dict          # {"price_structure": weight, ...} — only triggered signals appear
    reasons: list


@dataclass
class PositionHealthResult:
    state: PositionHealthState
    candidate_state: PositionHealthState  # what this tick's score alone suggests, pre-hysteresis
    pending_state: Optional[PositionHealthState]
    pending_streak: int
    score: DeteriorationScore


def _score_deterioration(snapshot: IndicatorSnapshot, direction: str, cfg: PositionEngineConfig) -> DeteriorationScore:
    tf = snapshot.tf5
    signals: dict = {}
    reasons: list = []
    w = cfg.weights

    if not tf.candles:
        return DeteriorationScore(total=0, signal_count=0, signals={}, reasons=["insufficient data"])

    price = tf.candles[-1]["close"]

    # 1. price structure: last swing high/low has reversed against direction.
    highs = [p for p in snapshot.swing_points_5m if p.kind == "high" and p.label]
    lows = [p for p in snapshot.swing_points_5m if p.kind == "low" and p.label]
    if highs and lows:
        last_high, last_low = highs[-1].label, lows[-1].label
        bad = (direction == "LONG" and last_high == "LH" and last_low == "LL") or \
              (direction == "SHORT" and last_high == "HH" and last_low == "HL")
        if bad:
            signals["price_structure"] = w["price_structure"]
            reasons.append(f"Price structure has reversed against the {direction} position "
                            f"({last_high}/{last_low})")

    # 2. important S/R: a well-touched SUPPORT level (for a LONG) or
    # RESISTANCE level (for a SHORT) that price has recently broken through
    # to the wrong side of. Deliberately restricted to the level kind that
    # was actually supposed to hold in this direction — a resistance level
    # sitting above an untouched LONG position isn't "broken support," it's
    # just a level price hasn't reached yet, and must never score here.
    # "Important" = more touches than the bare clustering minimum; "recently
    # broken" = within 2 ATR, not some level from the other side of the
    # session.
    atr_v = tf.atr[-1]
    if atr_v:
        important = [lv for lv in snapshot.sr_levels if lv.touches >= snapshot.config.sr_min_touches + 1]
        if direction == "LONG":
            relevant = [lv for lv in important if lv.kind in ("support", "mixed")]
            broken = [lv for lv in relevant if price < lv.price and abs(lv.price - price) <= 2 * atr_v]
        else:
            relevant = [lv for lv in important if lv.kind in ("resistance", "mixed")]
            broken = [lv for lv in relevant if price > lv.price and abs(lv.price - price) <= 2 * atr_v]
        if broken:
            signals["important_sr"] = w["important_sr"]
            reasons.append(f"Price broke through an important level near {broken[0].price:.1f}")

    # 3. VWAP: price on the unfavorable side of VWAP. Compared in the same
    # instrument's terms as VWAP itself was computed from (vwap_price, e.g.
    # futures close) rather than the index close, so a futures-index
    # premium never counts as "unfavorable" on its own — see snapshot.py.
    vwap_v = tf.vwap_value[-1]
    vwap_price = tf.vwap_price[-1] if tf.vwap_price else None
    if vwap_v is not None and vwap_price is not None:
        bad = (direction == "LONG" and vwap_price < vwap_v) or (direction == "SHORT" and vwap_price > vwap_v)
        if bad:
            signals["vwap"] = w["vwap"]
            reasons.append(f"Price is on the unfavorable side of VWAP ({vwap_price:.1f} vs {vwap_v:.1f})")

    # 4. EMA structure: EMA fast has crossed to the unfavorable side of EMA slow.
    fast, slow = tf.ema_fast[-1], tf.ema_slow[-1]
    if fast is not None and slow is not None:
        bad = (direction == "LONG" and fast < slow) or (direction == "SHORT" and fast > slow)
        if bad:
            signals["ema_structure"] = w["ema_structure"]
            reasons.append(f"EMA{snapshot.config.ema_fast_period} has crossed to the unfavorable side "
                            f"of EMA{snapshot.config.ema_slow_period}")

    # 5. DMI: the unfavorable DI now dominates.
    pdi, mdi = tf.dmi_adx.plus_di[-1], tf.dmi_adx.minus_di[-1]
    if pdi is not None and mdi is not None:
        bad = (direction == "LONG" and mdi > pdi) or (direction == "SHORT" and pdi > mdi)
        if bad:
            signals["dmi"] = w["dmi"]
            reasons.append("DMI: the unfavorable directional index now dominates")

    # 6. Aroon: the unfavorable side now dominates.
    up, down = tf.aroon.up[-1], tf.aroon.down[-1]
    if up is not None and down is not None:
        bad = (direction == "LONG" and down > up) or (direction == "SHORT" and up > down)
        if bad:
            signals["aroon"] = w["aroon"]
            reasons.append("Aroon: the unfavorable side now dominates")

    # 7. ADX: momentum itself is declining (a falling ADX, direction-agnostic
    # — a weakening trend is a risk to a position regardless of which way it
    # was trending, this is the lowest-weighted, most easily-outvoted signal
    # by design, matching the spec's stated importance hierarchy).
    adx_series = tf.dmi_adx.adx
    if len(adx_series) >= 2 and adx_series[-1] is not None and adx_series[-2] is not None:
        if adx_series[-1] < adx_series[-2]:
            signals["adx"] = w["adx"]
            reasons.append("ADX is declining")

    total = sum(signals.values())
    return DeteriorationScore(total=total, signal_count=len(signals), signals=signals, reasons=reasons)


class PositionHealthEngine:
    """One instance per tracked position (mirrors TradeManagementEngine being
    called per-Position in long_option_trade_engine.py) — the hysteresis
    streak is per-position state, not global."""

    def __init__(self, cfg: PositionEngineConfig = None):
        self.cfg = cfg or PositionEngineConfig()
        self.state = PositionHealthState.TREND_HEALTHY
        self._pending_state: Optional[PositionHealthState] = None
        self._pending_streak = 0

    def update(self, snapshot: IndicatorSnapshot, direction: str) -> PositionHealthResult:
        if direction not in ("LONG", "SHORT"):
            raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")

        score = _score_deterioration(snapshot, direction, self.cfg)
        raw_candidate = self._raw_candidate(score)

        if raw_candidate == self.state:
            self._pending_state = None
            self._pending_streak = 0
        elif raw_candidate == self._pending_state:
            self._pending_streak += 1
        else:
            self._pending_state = raw_candidate
            self._pending_streak = 1

        if self._pending_state is not None:
            escalating = _SEVERITY[self._pending_state] > _SEVERITY[self.state]
            required = self.cfg.confirm_bars if escalating else self.cfg.recover_confirm_bars
            if self._pending_streak >= required:
                self.state = self._pending_state
                self._pending_state = None
                self._pending_streak = 0

        return PositionHealthResult(
            state=self.state, candidate_state=raw_candidate,
            pending_state=self._pending_state, pending_streak=self._pending_streak, score=score,
        )

    def _raw_candidate(self, score: DeteriorationScore) -> PositionHealthState:
        idx = _SEVERITY[self.state]

        if idx < len(_LADDER) - 1:
            worse = _LADDER[idx + 1]
            enter = _enter_threshold(worse, self.cfg)
            if enter is not None and score.total >= enter and score.signal_count >= self.cfg.min_signals:
                return worse

        if idx > 0:
            exit_threshold = _exit_threshold(self.state, self.cfg)
            if exit_threshold is not None and score.total < exit_threshold:
                return _LADDER[idx - 1]

        return self.state
