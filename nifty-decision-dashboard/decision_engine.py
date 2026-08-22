"""
Decision Engine — the combinator: Trend + Entry + Location -> Entry
Permission / Trade Direction. Pure functions only, no engine imports its
config from another (decision_engine.py duplicates
min_entry_score_for_entry rather than importing entry_engine.py's config —
see config.py's DecisionEngineConfig docstring) so each stays independently
tunable/testable.

THE critical invariant this module exists to enforce: a maxed Trend+Entry
score must still resolve to WAIT_FOR_PULLBACK ("do not chase") when Location
is bad (VERY_EXTENDED or POOR runway) — never the other way around. A good
Location can only VETO a strong Trend+Entry read; it can never promote a
weak one into ENTER. Entry Permission always carries `reasons` explaining
exactly which gate passed or failed — this output is read by a human placing
a manual order, "trust me" is not an acceptable answer here.

`DecisionResult.armed` is a purely additive signal, distinct from
`permission`: True when the 5-min Trend has qualified (trend_aligned) but
the 2-min Entry Engine hasn't scored a valid setup yet (not entry_ok) —
i.e. "bias established, now watching the 2-min chart for a pullback entry,"
matching this dashboard's documented bias -> permission (5-min) /
pullback -> structure -> continuation -> entry (2-min) methodology. It is
never True once entry_ok is True, since at that point the outcome is
already decided as ENTER or WAIT_FOR_PULLBACK (a different, later kind of
"waiting" — already-qualified-but-overextended, not "still watching for
structure"). `permission` itself is untouched by this field; it stays
exactly the three-way ENTER/WAIT_FOR_PULLBACK/NO_TRADE decision it always
was.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from config import DecisionEngineConfig
from entry_engine import EntryResult
from location_engine import LocationResult
from trend_engine import TrendDirection, TrendResult

_BULLISH = {TrendDirection.STRONG_BULL, TrendDirection.BULL, TrendDirection.WEAK_BULL}
_BEARISH = {TrendDirection.STRONG_BEAR, TrendDirection.BEAR, TrendDirection.WEAK_BEAR}


class EntryPermission(str, Enum):
    ENTER = "ENTER"
    WAIT_FOR_PULLBACK = "WAIT_FOR_PULLBACK"  # trend+entry strong enough, but location says don't chase
    NO_TRADE = "NO_TRADE"                    # trend and/or entry not strong enough, location is irrelevant


@dataclass
class DecisionResult:
    direction: Optional[str]   # "LONG" | "SHORT" | None (no actionable direction)
    permission: EntryPermission
    reasons: list
    armed: bool = False  # 5-min bias qualified, 2-min entry setup not yet scored — see module docstring


def _trend_sign(direction: TrendDirection) -> int:
    if direction in _BULLISH:
        return 1
    if direction in _BEARISH:
        return -1
    return 0


def evaluate_decision(trend: TrendResult, entry: EntryResult, location: LocationResult,
                       cfg: DecisionEngineConfig = None) -> DecisionResult:
    cfg = cfg or DecisionEngineConfig()
    reasons = []

    if entry.direction != location.direction:
        return DecisionResult(direction=None, permission=EntryPermission.NO_TRADE,
                               reasons=[f"internal inconsistency: entry evaluated for {entry.direction}, "
                                        f"location evaluated for {location.direction}"])

    candidate_direction = entry.direction
    trend_sign = _trend_sign(trend.direction)
    candidate_sign = 1 if candidate_direction == "LONG" else -1

    trend_aligned = trend_sign == candidate_sign and abs(trend.score) >= cfg.min_trend_band_for_entry
    if trend_sign != candidate_sign:
        reasons.append(f"Trend ({trend.direction.value}, score {trend.score}) does not support a "
                        f"{candidate_direction} entry")
    elif not trend_aligned:
        reasons.append(f"Trend score {trend.score} too weak for a {candidate_direction} entry "
                        f"(need |score| >= {cfg.min_trend_band_for_entry})")
    else:
        reasons.append(f"Trend supports {candidate_direction} (score {trend.score}, {trend.direction.value})")

    entry_ok = entry.score >= cfg.min_entry_score_for_entry
    reasons.append(f"Entry score {entry.score}/5 ({entry.label.value}) "
                    f"{'meets' if entry_ok else 'does not meet'} the {cfg.min_entry_score_for_entry}-point bar")

    if not trend_aligned or not entry_ok:
        armed = trend_aligned and not entry_ok
        if armed:
            reasons.append(f"ARMED: 5-min bias qualifies for {candidate_direction}, "
                            f"watching the 2-min chart for a pullback entry")
        return DecisionResult(direction=candidate_direction, permission=EntryPermission.NO_TRADE,
                               reasons=reasons, armed=armed)

    location_veto = (location.extension.value in cfg.veto_extension_levels
                      or location.runway.value in cfg.veto_runway_levels)
    reasons.extend(location.reasons)

    if location_veto:
        reasons.append(f"DO NOT CHASE: Trend+Entry qualify, but Location is "
                        f"{location.extension.value}/{location.runway.value} -> WAIT_FOR_PULLBACK")
        return DecisionResult(direction=candidate_direction, permission=EntryPermission.WAIT_FOR_PULLBACK,
                               reasons=reasons)

    reasons.append("Trend, Entry, and Location all qualify -> ENTER")
    return DecisionResult(direction=candidate_direction, permission=EntryPermission.ENTER, reasons=reasons)
