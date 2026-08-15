"""
Offline unit test for compute_vix_atr_multiplier — the pure function behind the
VIX-adaptive ATR stop-loss multiplier for manual trades (with-websockets/pnl_monitor.py).
No Kite session or network access needed; this only tests the formula/clamping logic.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "with-websockets"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Importing the real module needs KITE_API_KEY etc. at import time (module-level
# os.environ[...] reads) — set harmless dummies so we can import just the pure function
# without a live .env. Nothing here makes a network call.
import os
os.environ.setdefault("KITE_API_KEY", "dummy")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")

from pnl_monitor import compute_vix_atr_multiplier, ATR_BASE_MULTIPLIER, ATR_MIN_MULTIPLIER, ATR_MAX_MULTIPLIER

failures = []


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  {status}  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("\n=== VIX -> ATR multiplier ===\n")

# At the reference VIX (15), multiplier should equal the base multiplier exactly.
check("VIX=15 (reference) -> base multiplier", compute_vix_atr_multiplier(15) == ATR_BASE_MULTIPLIER)

# Formula: 1.5 + (VIX - 15) * 0.1
check("VIX=10 -> 1.0", compute_vix_atr_multiplier(10) == 1.0)
check("VIX=20 -> 2.0", compute_vix_atr_multiplier(20) == 2.0)
check("VIX=25 -> 2.5", compute_vix_atr_multiplier(25) == 2.5)

# Clamping
check("VIX=0 (extreme low) clamps to MIN_MULTIPLIER", compute_vix_atr_multiplier(0) == ATR_MIN_MULTIPLIER)
check("VIX=100 (extreme spike) clamps to MAX_MULTIPLIER", compute_vix_atr_multiplier(100) == ATR_MAX_MULTIPLIER)

# Monotonic — higher VIX should never produce a smaller multiplier
prev = compute_vix_atr_multiplier(5)
monotonic = True
for vix in range(6, 60):
    cur = compute_vix_atr_multiplier(vix)
    if cur < prev:
        monotonic = False
        break
    prev = cur
check("monotonic non-decreasing across VIX 5-60", monotonic)

print(
    "\nNote: the reference bands in the original spec (e.g. \"20-25 VIX -> 2.5-3.5x\") are "
    "described there as an approximation for sanity-checking, not an exact target. The "
    "literal formula (base=1.5, reference=15, sensitivity=0.1) only reaches 2.5x at "
    "VIX=25, not the top of that band's range — flagging this since it's the kind of gap "
    "that matters before trusting these numbers with real size. Not fixed here since the "
    "exact constants were explicitly specified."
)

print(f"\n{'='*50}")
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
else:
    print("All checks passed.")
