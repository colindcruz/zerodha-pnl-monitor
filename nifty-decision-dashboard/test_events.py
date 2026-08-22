"""
Unit tests for events.py — the deduplicated "what changed" feed. No Kite
session, no candle I/O.
"""

import sys
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from events import EventFeed

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


T0 = datetime(2026, 8, 20, 9, 15, 0)


# ============================================================
print("=== no event on the first tick (nothing to compare against) ===")
# ============================================================
feed = EventFeed()
new_events = feed.update(T0, trend_direction="BULL")
check("first tick emits no event", new_events == [], str(new_events))
check("feed.events is still empty", feed.events == [])


# ============================================================
print("\n=== unchanged value across ticks emits nothing ===")
# ============================================================
new_events2 = feed.update(T0 + timedelta(minutes=5), trend_direction="BULL")
check("repeating the same value emits no event", new_events2 == [])


# ============================================================
print("\n=== a genuine transition emits exactly one event ===")
# ============================================================
new_events3 = feed.update(T0 + timedelta(minutes=10), trend_direction="STRONG_BULL")
check("transition emits exactly 1 event", len(new_events3) == 1, str(new_events3))
if new_events3:
    ev = new_events3[0]
    check("old_value captured", ev.old_value == "BULL", str(ev.old_value))
    check("new_value captured", ev.new_value == "STRONG_BULL", str(ev.new_value))
    check("category is 'trend_direction'", ev.category == "trend_direction", str(ev.category))
    check("message mentions both values", "BULL" in ev.message and "STRONG_BULL" in ev.message, ev.message)


# ============================================================
print("\n=== independent fields tracked independently ===")
# ============================================================
feed2 = EventFeed()
feed2.update(T0, trend_direction="BULL", trend_strength="WEAK")
ev_a = feed2.update(T0 + timedelta(minutes=5), trend_direction="STRONG_BULL", trend_strength="WEAK")
check("only trend_direction changed -> exactly 1 event, not 2", len(ev_a) == 1, str(ev_a))
check("the event is for trend_direction", ev_a[0].category == "trend_direction" if ev_a else False)


# ============================================================
print("\n=== None values are ignored (field not yet available this tick) ===")
# ============================================================
feed3 = EventFeed()
feed3.update(T0, position_health="TREND_HEALTHY")
ev_b = feed3.update(T0 + timedelta(minutes=5), position_health=None)  # e.g. position was closed
check("None value doesn't emit a spurious event", ev_b == [])
ev_c = feed3.update(T0 + timedelta(minutes=10), position_health="TREND_HEALTHY")
check("value unchanged from before the None gap -> still no event", ev_c == [])


# ============================================================
print("\n=== enum-like values (with .value) are stringified correctly ===")
# ============================================================
class FakeEnum:
    def __init__(self, value):
        self.value = value


feed4 = EventFeed()
feed4.update(T0, momentum=FakeEnum("STABLE"))
ev_d = feed4.update(T0 + timedelta(minutes=5), momentum=FakeEnum("MATURING"))
check("enum-like .value attribute used for comparison/message", len(ev_d) == 1 and ev_d[0].new_value == "MATURING",
      str(ev_d))


# ============================================================
print("\n=== max_events trims the history ===")
# ============================================================
feed5 = EventFeed(max_events=3)
feed5.update(T0, trend_direction="A")
for i, v in enumerate(["B", "C", "D", "E"]):
    feed5.update(T0 + timedelta(minutes=i + 1), trend_direction=v)
check("event history capped at max_events", len(feed5.events) == 3, str(len(feed5.events)))
check("only the MOST RECENT events survive the cap",
      [e.new_value for e in feed5.events] == ["C", "D", "E"], str([e.new_value for e in feed5.events]))


# ============================================================
print("\n=== recent(n) returns the last n events ===")
# ============================================================
feed6 = EventFeed()
feed6.update(T0, trend_direction="A")
for i, v in enumerate(["B", "C", "D"]):
    feed6.update(T0 + timedelta(minutes=i + 1), trend_direction=v)
check("recent(2) returns exactly the last 2 events",
      [e.new_value for e in feed6.recent(2)] == ["C", "D"], str([e.new_value for e in feed6.recent(2)]))


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
