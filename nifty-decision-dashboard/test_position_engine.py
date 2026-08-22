"""
Unit tests for position_engine.py — the hysteresis state machine. Scripts
sequences of hand-built IndicatorSnapshots through a PositionHealthEngine and
verifies: a lone ADX wobble never advances state; a genuine multi-signal
cluster does, after confirm_bars; near-threshold oscillation doesn't flap;
recovery is slower than deterioration. No Kite session, no candle I/O.
"""

import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from candles import IST
from config import IndicatorConfig, PositionEngineConfig
from indicators import AroonResult, DmiAdxResult, SRLevel, SwingPoint
from position_engine import PositionHealthEngine, PositionHealthState
from snapshot import IndicatorSnapshot, TimeframeIndicators

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


T0 = datetime(2026, 8, 20, 9, 15, 0, tzinfo=IST)
DUMMY_CANDLE = {"date": T0, "open": 24000, "high": 24010, "low": 23990, "close": 24000, "volume": 100}


def make_snapshot(price=24100, vwap_v=24050, ema_fast=24080, ema_slow=24050,
                   pdi=30, mdi=10, aroon_up=80, aroon_down=20, adx_prev=25, adx_now=26,
                   swing_high_label=None, swing_low_label=None, sr_levels=None,
                   atr_v=20, ind_cfg=None):
    """All-favorable-for-LONG by default; individual kwargs flip one signal
    unfavorable at a time so each test can isolate exactly what it's probing."""
    candles = [{**DUMMY_CANDLE, "close": price}]
    swing_points = []
    if swing_high_label:
        swing_points.append(SwingPoint(index=0, date=T0, kind="high", price=price + 10, label=swing_high_label))
    if swing_low_label:
        swing_points.append(SwingPoint(index=1, date=T0, kind="low", price=price - 10, label=swing_low_label))

    tf = TimeframeIndicators(
        candles=candles,
        ema_fast=[ema_fast], ema_slow=[ema_slow], atr=[atr_v],
        aroon=AroonResult(up=[aroon_up], down=[aroon_down]),
        dmi_adx=DmiAdxResult(plus_di=[pdi], minus_di=[mdi], adx=[adx_prev, adx_now]),
        vwap_value=[vwap_v, vwap_v], vwap_is_twap=[False, False], vwap_price=[price, price],
    )
    return IndicatorSnapshot(config=ind_cfg or IndicatorConfig(), candles_1m=[], tf2=tf, tf5=tf,
                              opening_range=None, swing_points_5m=swing_points, sr_levels=sr_levels or [])


cfg = PositionEngineConfig()


# ============================================================
print("=== A lone ADX wobble never advances state ===")
# ============================================================
engine = PositionHealthEngine(cfg)
adx_wobble_snap = make_snapshot(adx_prev=26, adx_now=25)  # only "adx declining" is unfavorable, everything else favorable
for i in range(50):  # run it for far longer than any confirm_bars setting
    result = engine.update(adx_wobble_snap, "LONG")
check("50 ticks of a lone ADX-only signal never leaves TREND_HEALTHY",
      engine.state == PositionHealthState.TREND_HEALTHY, str(engine.state))
check("score reflects exactly 1 signal (adx)", result.score.signal_count == 1, str(result.score.signals))
check("min_signals gate (2) blocks any transition regardless of score magnitude", cfg.min_signals == 2)


# ============================================================
print("\n=== A genuine cluster escalates after confirm_bars ===")
# ============================================================
bad_cluster_snap = make_snapshot(
    price=23900, vwap_v=24000, ema_fast=23950, ema_slow=24000,
    pdi=10, mdi=30, aroon_up=20, aroon_down=80, adx_prev=26, adx_now=25,
    swing_high_label="LH", swing_low_label="LL",
)
engine2 = PositionHealthEngine(cfg)
states_seen = []
for i in range(cfg.confirm_bars):
    r = engine2.update(bad_cluster_snap, "LONG")
    states_seen.append(r.state)
    if i < cfg.confirm_bars - 1:
        check(f"tick {i+1}/{cfg.confirm_bars}: still TREND_HEALTHY (hysteresis not yet satisfied)",
              r.state == PositionHealthState.TREND_HEALTHY, str(r.state))
check(f"after {cfg.confirm_bars} consecutive bad ticks: escalated to TREND_MATURING",
      states_seen[-1] == PositionHealthState.TREND_MATURING, str(states_seen[-1]))
check("full cluster: signal_count >= min_signals", r.score.signal_count >= cfg.min_signals, str(r.score.signals))
check("full cluster: score >= maturing_enter_score", r.score.total >= cfg.maturing_enter_score, str(r.score.total))


# ============================================================
print("\n=== Near-threshold oscillation doesn't flap ===")
# ============================================================
# Alternates between a healthy tick and a bad-cluster tick every other bar —
# the pending streak must reset each time the candidate flips, so it should
# NEVER accumulate enough consecutive confirmations to transition, no matter
# how many ticks pass.
healthy_snap = make_snapshot()
engine3 = PositionHealthEngine(cfg)
for i in range(40):
    snap = bad_cluster_snap if i % 2 == 0 else healthy_snap
    r3 = engine3.update(snap, "LONG")
check("40 ticks of alternating healthy/bad never escalates (streak keeps resetting)",
      engine3.state == PositionHealthState.TREND_HEALTHY, str(engine3.state))


# ============================================================
print("\n=== Recovery is slower than deterioration (asymmetric hysteresis) ===")
# ============================================================
check("config: recover_confirm_bars > confirm_bars", cfg.recover_confirm_bars > cfg.confirm_bars,
      f"{cfg.recover_confirm_bars} vs {cfg.confirm_bars}")

engine4 = PositionHealthEngine(cfg)
for _ in range(cfg.confirm_bars):
    engine4.update(bad_cluster_snap, "LONG")
check("engine4 escalated to TREND_MATURING", engine4.state == PositionHealthState.TREND_MATURING, str(engine4.state))

# Now feed healthy ticks — must NOT recover after only confirm_bars ticks
# (that would mean recovery is as fast as deterioration), but MUST recover
# by recover_confirm_bars ticks.
for _ in range(cfg.confirm_bars):
    r4 = engine4.update(healthy_snap, "LONG")
check(f"after only {cfg.confirm_bars} good ticks: has NOT yet recovered",
      engine4.state == PositionHealthState.TREND_MATURING, str(engine4.state))

remaining = cfg.recover_confirm_bars - cfg.confirm_bars
for _ in range(remaining):
    r4 = engine4.update(healthy_snap, "LONG")
check(f"after {cfg.recover_confirm_bars} total good ticks: recovered to TREND_HEALTHY",
      engine4.state == PositionHealthState.TREND_HEALTHY, str(engine4.state))


# ============================================================
print("\n=== SHORT direction mirrors LONG ===")
# ============================================================
bad_for_short_snap = make_snapshot(
    price=24100, vwap_v=24000, ema_fast=24080, ema_slow=24000,
    pdi=30, mdi=10, aroon_up=80, aroon_down=20, adx_prev=26, adx_now=25,
    swing_high_label="HH", swing_low_label="HL",
)
engine5 = PositionHealthEngine(cfg)
for _ in range(cfg.confirm_bars):
    r5 = engine5.update(bad_for_short_snap, "SHORT")
check("SHORT: symmetric bad cluster escalates to TREND_MATURING",
      engine5.state == PositionHealthState.TREND_MATURING, str(engine5.state))
# The same snapshot is entirely FAVORABLE for a LONG, so a LONG engine fed
# the identical data must never escalate.
engine6 = PositionHealthEngine(cfg)
for _ in range(20):
    engine6.update(bad_for_short_snap, "LONG")
check("the SAME data is favorable for LONG -> stays TREND_HEALTHY",
      engine6.state == PositionHealthState.TREND_HEALTHY, str(engine6.state))


# ============================================================
print("\n=== important_sr: only the correct level kind counts ===")
# ============================================================
# A RESISTANCE level sitting above price must NOT count as "broken support"
# for a LONG position — it's simply a level price hasn't reached yet.
resistance_above = [SRLevel(price=24110, touches=5, kind="resistance")]
snap_resistance_only = make_snapshot(price=24100, sr_levels=resistance_above, atr_v=20)
score = position_engine_score = None
from position_engine import _score_deterioration  # noqa: E402
score = _score_deterioration(snap_resistance_only, "LONG", cfg)
check("untouched resistance above price does NOT trigger important_sr for LONG",
      "important_sr" not in score.signals, str(score.signals))

# A SUPPORT level that price has now fallen below (within 2 ATR) SHOULD count.
broken_support = [SRLevel(price=24105, touches=5, kind="support")]
snap_broken_support = make_snapshot(price=24100, sr_levels=broken_support, atr_v=20)
score2 = _score_deterioration(snap_broken_support, "LONG", cfg)
check("a support level price has fallen below DOES trigger important_sr for LONG",
      "important_sr" in score2.signals, str(score2.signals))


# ============================================================
print("\n=== bad direction argument raises ===")
# ============================================================
try:
    PositionHealthEngine(cfg).update(healthy_snap, "SIDEWAYS")
    check("invalid direction raises ValueError", False)
except ValueError:
    check("invalid direction raises ValueError", True)


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
