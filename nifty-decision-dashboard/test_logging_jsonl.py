"""
Unit tests for logging_jsonl.py — JSONL round-trip + MFE/MAE tracking. Real
temp files on disk, no Kite session.
"""

import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from logging_jsonl import TrackedTrade, log_tick, log_trade_event, update_excursion
from trend_engine import MomentumState, TrendDirection, TrendResult, TrendStrength

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}]  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


T0 = datetime(2026, 8, 20, 9, 15, 0)


# ============================================================
print("=== log_tick round-trip ===")
# ============================================================
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "tick_log.jsonl"
    trend = TrendResult(direction=TrendDirection.STRONG_BULL, score=5, strength=TrendStrength.STRONG,
                         momentum=MomentumState.STABLE, votes={"aroon": 1}, reasons=["Aroon: bullish"])
    log_tick(path, T0, tick_seq=1, spot=24000.5, trend=trend, positions=[{"tradingsymbol": "NIFTY26AUG25000CE"}])

    lines = path.read_text().strip().split("\n")
    check("exactly one line written", len(lines) == 1, str(len(lines)))
    record = json.loads(lines[0])
    check("ts round-trips as ISO string", record["ts"] == T0.isoformat(), record["ts"])
    check("tick_seq preserved", record["tick_seq"] == 1)
    check("spot preserved", record["spot"] == 24000.5)
    check("trend dataclass flattened to a dict", record["trend"]["score"] == 5, str(record["trend"]))
    check("trend enum serialized as its plain string value",
          record["trend"]["direction"] == "STRONG_BULL", str(record["trend"]["direction"]))
    check("positions list preserved", record["positions"] == [{"tradingsymbol": "NIFTY26AUG25000CE"}])
    check("absent engines serialize as None", record["entry"] is None and record["decision"] is None)

    log_tick(path, T0, tick_seq=2, spot=24001.0)
    lines2 = path.read_text().strip().split("\n")
    check("second call appends (2 lines total)", len(lines2) == 2, str(len(lines2)))


# ============================================================
print("\n=== update_excursion (MFE/MAE) ===")
# ============================================================
trade_long = TrackedTrade(trade_id="t1", symbol="NIFTY26AUG25000CE", direction="LONG",
                           entry_price=100.0, entry_ts=T0)
update_excursion(trade_long, 110.0)  # +10 favorable
check("LONG: favorable move updates MFE", trade_long.mfe == 10.0, str(trade_long.mfe))
check("LONG: MAE unaffected by a favorable move", trade_long.mae == 0.0)

update_excursion(trade_long, 95.0)  # -5 from entry (adverse)
check("LONG: adverse move updates MAE", trade_long.mae == 5.0, str(trade_long.mae))
check("LONG: MFE stays at its prior peak (10), not overwritten by a smaller favorable reading",
      trade_long.mfe == 10.0, str(trade_long.mfe))

update_excursion(trade_long, 90.0)  # -10 from entry, more adverse
check("LONG: MAE tracks the new, deeper adverse excursion", trade_long.mae == 10.0, str(trade_long.mae))

trade_short = TrackedTrade(trade_id="t2", symbol="NIFTY26AUG25000PE", direction="SHORT",
                            entry_price=100.0, entry_ts=T0)
update_excursion(trade_short, 90.0)  # price DOWN 10 is FAVORABLE for a short
check("SHORT: price falling is favorable -> MFE updates", trade_short.mfe == 10.0, str(trade_short.mfe))
update_excursion(trade_short, 115.0)  # price UP 15 from entry is adverse for a short
check("SHORT: price rising is adverse -> MAE updates", trade_short.mae == 15.0, str(trade_short.mae))


# ============================================================
print("\n=== log_trade_event ===")
# ============================================================
with tempfile.TemporaryDirectory() as d:
    path2 = Path(d) / "trade_log.jsonl"
    trade = TrackedTrade(trade_id="t3", symbol="NIFTY26AUG25000CE", direction="LONG",
                          entry_price=100.0, entry_ts=T0, dashboard_state_at_entry={"trend": "BULL"})
    log_trade_event(path2, "ENTRY_DETECTED", trade, price=100.0, ts=T0)
    update_excursion(trade, 120.0)
    log_trade_event(path2, "EXIT_DETECTED", trade, price=118.0, ts=T0, extra={"exit_reason": "manual"})

    lines = path2.read_text().strip().split("\n")
    check("two lifecycle events logged", len(lines) == 2, str(len(lines)))
    entry_rec = json.loads(lines[0])
    exit_rec = json.loads(lines[1])
    check("entry event type correct", entry_rec["event"] == "ENTRY_DETECTED")
    check("dashboard_state_at_entry preserved", entry_rec["dashboard_state_at_entry"] == {"trend": "BULL"})
    check("exit event carries the mfe accumulated before it was logged", exit_rec["mfe"] == 20.0, str(exit_rec["mfe"]))
    check("exit event carries extra fields (exit_reason)", exit_rec["exit_reason"] == "manual", str(exit_rec))
    check("trade_id consistent across both events", entry_rec["trade_id"] == exit_rec["trade_id"] == "t3")


# ============================================================
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
