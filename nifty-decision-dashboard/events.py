"""
"What changed" event feed: deduplicated, transitions-only. Every recompute
tick carries a full state snapshot regardless of whether anything actually
changed (see logging_jsonl.py) — this module is what turns that into a
short, glanceable list of only the moments something meaningfully shifted
(trend direction, trend strength, momentum, entry setup label, decision
permission, position health state), instead of a feed that fires 375+
times a session and buries the signal in noise.

Stateful by design (an EventFeed instance persists across ticks, mirroring
long-option's LiveEngine holding per-run state) — "did this change since
last tick" is inherently a comparison against the previous tick's value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Event:
    timestamp: datetime
    category: str
    message: str
    old_value: Optional[str]
    new_value: str


_FIELDS = (
    # (key, category, human label)
    ("trend_direction", "trend_direction", "Trend direction"),
    ("trend_strength", "trend_strength", "Trend strength"),
    ("momentum", "momentum", "Momentum"),
    ("entry_setup", "entry_setup", "Entry setup"),
    ("decision_permission", "decision", "Entry permission"),
    ("position_health", "position_health", "Position health"),
)


def _value_str(value) -> Optional[str]:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


class EventFeed:
    def __init__(self, max_events: int = 200):
        self.max_events = max_events
        self.events: list[Event] = []
        self._last: dict = {}

    def update(self, ts: datetime, **kwargs) -> list[Event]:
        """kwargs: any subset of the keys in _FIELDS (trend_direction=...,
        trend_strength=..., momentum=..., entry_setup=..., decision_permission=...,
        position_health=...). Fields not passed (or passed as None) are left
        untouched — a caller only reports what it actually has this tick (e.g.
        position_health only exists once a position is being tracked)."""
        new_events = []
        for key, category, label in _FIELDS:
            if key not in kwargs:
                continue
            new_value = _value_str(kwargs[key])
            if new_value is None:
                continue
            old_value = self._last.get(key)
            if old_value is not None and old_value != new_value:
                ev = Event(timestamp=ts, category=category,
                           message=f"{label} changed: {old_value} -> {new_value}",
                           old_value=old_value, new_value=new_value)
                self.events.append(ev)
                new_events.append(ev)
            self._last[key] = new_value

        if len(self.events) > self.max_events:
            self.events = self.events[-self.max_events:]
        return new_events

    def recent(self, n: int = 20) -> list[Event]:
        return self.events[-n:]
