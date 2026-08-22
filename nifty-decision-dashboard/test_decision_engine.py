"""
Unit tests for decision_engine.py — THE single most important test in this
service. Enumerates the Trend x Entry x Location outcome matrix with
hand-built result dataclasses (no candle data, no Kite session) and verifies
the one invariant the whole engine exists to protect: a maxed Trend+Entry
score must still resolve to WAIT_FOR_PULLBACK when Location is bad, never
ENTER — a good Location can only veto, never promote.
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from config import DecisionEngineConfig
from decision_engine import EntryPermission, evaluate_decision
from entry_engine import EntryResult, EntrySetupLabel
from location_engine import ExtensionLevel, LocationResult, RunwayLevel
from trend_engine import MomentumState, TrendDirection, TrendResult, TrendStrength, VolatilityLevel

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def mk_trend(direction, score):
    return TrendResult(direction=direction, score=score, strength=TrendStrength.STRONG,
                        momentum=MomentumState.STABLE, volatility=VolatilityLevel.NORMAL,
                        adx_direction="FLAT", votes={}, reasons=[])


def mk_entry(direction, score):
    label = (EntrySetupLabel.STRONG_SETUP if score >= 4 else EntrySetupLabel.VALID_SETUP if score >= 3
             else EntrySetupLabel.WEAK_SETUP if score >= 1 else EntrySetupLabel.NO_SETUP)
    return EntryResult(direction=direction, score=score, label=label, components={}, reasons=[])


def mk_location(direction, extension, runway):
    return LocationResult(direction=direction, extension=extension, extension_atr_distance=1.0,
                           runway=runway, reward_to_stop=2.5, nearest_level_price=100.0, reasons=[])


cfg = DecisionEngineConfig()
GOOD_LOC = lambda d: mk_location(d, ExtensionLevel.NORMAL, RunwayLevel.EXCELLENT)
VERY_EXTENDED_LOC = lambda d: mk_location(d, ExtensionLevel.VERY_EXTENDED, RunwayLevel.EXCELLENT)
POOR_RUNWAY_LOC = lambda d: mk_location(d, ExtensionLevel.NORMAL, RunwayLevel.POOR)
BOTH_BAD_LOC = lambda d: mk_location(d, ExtensionLevel.VERY_EXTENDED, RunwayLevel.POOR)


# ============================================================
print("=== THE critical invariant: maxed scores + bad location -> never ENTER ===")
# ============================================================
maxed_trend = mk_trend(TrendDirection.STRONG_BULL, 5)
maxed_entry = mk_entry("LONG", 5)

r1 = evaluate_decision(maxed_trend, maxed_entry, VERY_EXTENDED_LOC("LONG"), cfg)
check("maxed Trend+Entry + VERY_EXTENDED -> WAIT_FOR_PULLBACK, not ENTER",
      r1.permission == EntryPermission.WAIT_FOR_PULLBACK, str(r1.permission))
check("...direction is still reported as LONG (explainability)", r1.direction == "LONG", str(r1.direction))
check("...reasons explain the veto", any("DO NOT CHASE" in r for r in r1.reasons), str(r1.reasons))

r2 = evaluate_decision(maxed_trend, maxed_entry, POOR_RUNWAY_LOC("LONG"), cfg)
check("maxed Trend+Entry + POOR runway -> WAIT_FOR_PULLBACK, not ENTER",
      r2.permission == EntryPermission.WAIT_FOR_PULLBACK, str(r2.permission))

r3 = evaluate_decision(maxed_trend, maxed_entry, BOTH_BAD_LOC("LONG"), cfg)
check("maxed Trend+Entry + BOTH extension and runway bad -> still WAIT_FOR_PULLBACK (never ENTER)",
      r3.permission == EntryPermission.WAIT_FOR_PULLBACK, str(r3.permission))

r4 = evaluate_decision(maxed_trend, maxed_entry, GOOD_LOC("LONG"), cfg)
check("maxed Trend+Entry + good location -> ENTER", r4.permission == EntryPermission.ENTER, str(r4.permission))
check("...reasons explain the pass", any("ENTER" in r for r in r4.reasons), str(r4.reasons))


# ============================================================
print("\n=== Good location alone can never PROMOTE a weak Trend/Entry to ENTER ===")
# ============================================================
weak_trend = mk_trend(TrendDirection.WEAK_BULL, 1)  # below min_trend_band_for_entry (2)
r5 = evaluate_decision(weak_trend, maxed_entry, GOOD_LOC("LONG"), cfg)
check("weak trend + strong entry + perfect location -> still NO_TRADE",
      r5.permission == EntryPermission.NO_TRADE, str(r5.permission))

weak_entry = mk_entry("LONG", 2)  # below min_entry_score_for_entry (3)
r6 = evaluate_decision(maxed_trend, weak_entry, GOOD_LOC("LONG"), cfg)
check("strong trend + weak entry + perfect location -> still NO_TRADE",
      r6.permission == EntryPermission.NO_TRADE, str(r6.permission))


# ============================================================
print("\n=== Direction consistency ===")
# ============================================================
bear_trend = mk_trend(TrendDirection.STRONG_BEAR, -5)
r7 = evaluate_decision(bear_trend, maxed_entry, GOOD_LOC("LONG"), cfg)  # entry says LONG, trend says BEAR
check("trend direction contradicts candidate direction -> NO_TRADE",
      r7.permission == EntryPermission.NO_TRADE, str(r7.permission))

neutral_trend = mk_trend(TrendDirection.NEUTRAL, 0)
r8 = evaluate_decision(neutral_trend, maxed_entry, GOOD_LOC("LONG"), cfg)
check("NEUTRAL trend -> NO_TRADE regardless of entry/location", r8.permission == EntryPermission.NO_TRADE)

r9 = evaluate_decision(maxed_trend, mk_entry("SHORT", 5), GOOD_LOC("LONG"), cfg)
check("entry/location direction mismatch -> NO_TRADE with direction=None",
      r9.permission == EntryPermission.NO_TRADE and r9.direction is None, f"{r9.permission} {r9.direction}")


# ============================================================
print("\n=== SHORT side mirrors LONG ===")
# ============================================================
maxed_short_trend = mk_trend(TrendDirection.STRONG_BEAR, -5)
maxed_short_entry = mk_entry("SHORT", 5)
r10 = evaluate_decision(maxed_short_trend, maxed_short_entry, GOOD_LOC("SHORT"), cfg)
check("SHORT: maxed + good location -> ENTER", r10.permission == EntryPermission.ENTER, str(r10.permission))
r11 = evaluate_decision(maxed_short_trend, maxed_short_entry, VERY_EXTENDED_LOC("SHORT"), cfg)
check("SHORT: maxed + VERY_EXTENDED -> WAIT_FOR_PULLBACK", r11.permission == EntryPermission.WAIT_FOR_PULLBACK)


# ============================================================
print("\n=== armed: 5-min bias qualified, 2-min entry not yet scored ===")
# ============================================================
r12 = evaluate_decision(maxed_trend, weak_entry, GOOD_LOC("LONG"), cfg)
check("strong trend + weak entry -> armed=True (watching for the 2-min setup)", r12.armed is True, str(r12.armed))
check("...permission is still plain NO_TRADE (armed doesn't change permission)",
      r12.permission == EntryPermission.NO_TRADE, str(r12.permission))
check("...direction is still reported (LONG)", r12.direction == "LONG", str(r12.direction))
check("...reasons explain ARMED", any("ARMED" in r for r in r12.reasons), str(r12.reasons))

r13 = evaluate_decision(weak_trend, maxed_entry, GOOD_LOC("LONG"), cfg)
check("weak trend + strong entry -> NOT armed (bias itself hasn't qualified yet)",
      r13.armed is False, str(r13.armed))

check("fully qualified + good location (ENTER) -> not armed (past the armed phase)",
      r4.armed is False, str(r4.armed))
check("fully qualified + bad location (WAIT_FOR_PULLBACK) -> not armed (different kind of waiting)",
      r1.armed is False, str(r1.armed))

check("SHORT side: strong trend + weak entry also arms (direction-tagged)",
      evaluate_decision(maxed_short_trend, mk_entry("SHORT", 2), GOOD_LOC("SHORT"), cfg).armed is True)


# ============================================================
print("\n=== Every result always explains itself ===")
# ============================================================
for r in (r1, r2, r3, r4, r5, r6, r7, r8, r9, r10, r11):
    check(f"reasons non-empty for permission={r.permission}", len(r.reasons) > 0)


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
