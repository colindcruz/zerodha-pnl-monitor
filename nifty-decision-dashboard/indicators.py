"""
Pure-Python technical indicators — no numpy/pandas anywhere in this repo (see
with-websockets/pnl_monitor.py's _fetch_atr, the only prior indicator code
that exists here, confirmed via repo-wide grep to be the sole precedent).
Every function here takes/returns plain lists and dicts, defensively returns
None/empty for insufficient bars, and never raises on bad input — callers get
a graceful "not enough data yet" rather than a traceback.

Candle shape used throughout this service: {"date": datetime, "open": float,
"high": float, "low": float, "close": float, "volume": int}. Every indicator
function returns a list the SAME LENGTH as its input candles/values list, with
None in positions where there isn't yet enough history — this lets snapshot.py
read "current" (index -1) and "N bars ago" (for slope) uniformly, instead of
each indicator trimming its own output to a different length.

DMI/ADX use Wilder smoothing internally (seed = SMA of the first `period`
true ranges/DMs, then recursive smoothing) to be internally consistent — this
is deliberately NOT the same as pnl_monitor.py's own ATR, which uses a plain
SMA over the last N true ranges. That divergence is intentional, not an
oversight: Wilder's method is what DMI/ADX are defined in terms of, a plain
SMA ATR would silently disagree with the ADX derived from the same true
ranges. See config.py's IndicatorConfig for period defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _as_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt  # already-naive candle timestamps are assumed to already be IST
    return dt.astimezone(IST)


def typical_price(candle: dict) -> float:
    return (candle["high"] + candle["low"] + candle["close"]) / 3.0


# ============================================================
# EMA
# ============================================================

def ema(values: list[Optional[float]], period: int) -> list[Optional[float]]:
    """Standard EMA, seeded with a simple average of the first `period`
    values. None input values (or too few bars) propagate as None."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out

    window = values[:period]
    if any(v is None for v in window):
        return out
    seed = sum(window) / period
    out[period - 1] = seed

    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, n):
        v = values[i]
        if v is None:
            prev = None
        elif prev is None:
            prev = v
        else:
            prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def closes(candles: list[dict]) -> list[float]:
    return [c["close"] for c in candles]


# ============================================================
# ATR (Wilder-smoothed)
# ============================================================

def true_ranges(candles: list[dict]) -> list[Optional[float]]:
    """TR per bar; index 0 has no prior close so it's None (not h-l — that
    would understate a gap-open bar's real range as "no info" instead of
    honestly reporting we can't compute it yet)."""
    n = len(candles)
    out: list[Optional[float]] = [None] * n
    for i in range(1, n):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        out[i] = max(h - l, abs(h - pc), abs(l - pc))
    return out


def _wilder_smooth(values: list[Optional[float]], period: int) -> list[Optional[float]]:
    """Shared Wilder recursive-smoothing routine used by ATR, DMI's smoothed
    +DM/-DM/TR, and ADX's smoothing of DX. `values` may have leading Nones
    (e.g. true_ranges' index-0 None) — the seed window is the first `period`
    non-None values found contiguously from wherever real data starts."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None or n - start < period:
        return out

    window = values[start:start + period]
    seed = sum(window) / period
    seed_idx = start + period - 1
    out[seed_idx] = seed

    prev = seed
    for i in range(seed_idx + 1, n):
        v = values[i]
        if v is None:
            prev = None
        elif prev is None:
            prev = v
        else:
            prev = (prev * (period - 1) + v) / period
        out[i] = prev
    return out


def atr(candles: list[dict], period: int) -> list[Optional[float]]:
    return _wilder_smooth(true_ranges(candles), period)


# ============================================================
# DMI / ADX (Wilder)
# ============================================================

@dataclass
class DmiAdxResult:
    plus_di: list[Optional[float]]
    minus_di: list[Optional[float]]
    adx: list[Optional[float]]


def dmi_adx(candles: list[dict], di_period: int, adx_period: int) -> DmiAdxResult:
    n = len(candles)
    if n < 2:
        return DmiAdxResult([None] * n, [None] * n, [None] * n)

    plus_dm: list[Optional[float]] = [None] * n
    minus_dm: list[Optional[float]] = [None] * n
    for i in range(1, n):
        up_move = candles[i]["high"] - candles[i - 1]["high"]
        down_move = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0

    tr = true_ranges(candles)
    smoothed_tr = _wilder_smooth(tr, di_period)
    smoothed_plus_dm = _wilder_smooth(plus_dm, di_period)
    smoothed_minus_dm = _wilder_smooth(minus_dm, di_period)

    plus_di: list[Optional[float]] = [None] * n
    minus_di: list[Optional[float]] = [None] * n
    dx: list[Optional[float]] = [None] * n
    for i in range(n):
        st, spd, smd = smoothed_tr[i], smoothed_plus_dm[i], smoothed_minus_dm[i]
        if st is None or spd is None or smd is None or st == 0:
            continue
        pdi = 100.0 * spd / st
        mdi = 100.0 * smd / st
        plus_di[i] = pdi
        minus_di[i] = mdi
        denom = pdi + mdi
        dx[i] = 100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0

    adx = _wilder_smooth(dx, adx_period)
    return DmiAdxResult(plus_di, minus_di, adx)


# ============================================================
# Aroon
# ============================================================

@dataclass
class AroonResult:
    up: list[Optional[float]]
    down: list[Optional[float]]


def aroon(candles: list[dict], period: int) -> AroonResult:
    n = len(candles)
    up: list[Optional[float]] = [None] * n
    down: list[Optional[float]] = [None] * n
    if period <= 0:
        return AroonResult(up, down)

    for i in range(period, n):
        window = candles[i - period:i + 1]  # period+1 bars, inclusive of current
        highs = [c["high"] for c in window]
        lows = [c["low"] for c in window]
        bars_since_high = len(window) - 1 - highs.index(max(highs))
        bars_since_low = len(window) - 1 - lows.index(min(lows))
        up[i] = 100.0 * (period - bars_since_high) / period
        down[i] = 100.0 * (period - bars_since_low) / period
    return AroonResult(up, down)


# ============================================================
# VWAP (cumulative from session start, per IST calendar day) with a TWAP
# fallback for zero-volume sessions — NIFTY 50 is an index, not a traded
# instrument, so its ticks may carry no real volume_traded at all. See
# config.py / the plan's "VWAP volume risk" note: this can only be confirmed
# against real live ticks, this fallback exists precisely so the dashboard
# degrades gracefully rather than dividing by zero or freezing at the
# session's first price.
# ============================================================

@dataclass
class VwapResult:
    value: list[Optional[float]]
    is_twap_fallback: list[bool]


def vwap(candles: list[dict]) -> VwapResult:
    n = len(candles)
    value: list[Optional[float]] = [None] * n
    is_twap: list[bool] = [False] * n

    cum_pv = 0.0
    cum_v = 0.0
    cum_tp = 0.0
    count = 0
    session_date = None

    for i, c in enumerate(candles):
        this_date = _as_ist(c["date"]).date()
        if session_date is None or this_date != session_date:
            session_date = this_date
            cum_pv = cum_v = cum_tp = 0.0
            count = 0

        tp = typical_price(c)
        vol = c.get("volume") or 0
        cum_pv += tp * vol
        cum_v += vol
        cum_tp += tp
        count += 1

        if cum_v > 0:
            value[i] = cum_pv / cum_v
            is_twap[i] = False
        else:
            value[i] = cum_tp / count
            is_twap[i] = True

    return VwapResult(value, is_twap)


# ============================================================
# Price structure — swing-fractal HH/HL/LH/LL detection
# ============================================================

@dataclass
class SwingPoint:
    index: int
    date: datetime
    kind: str        # "high" | "low"
    price: float
    # "HH" | "LH" | "EQH" (highs, vs the previous swing high) |
    # "HL" | "LL" | "EQL" (lows, vs the previous swing low) |
    # None (first swing of its kind — nothing to compare against yet)
    label: Optional[str]


def swing_points(candles: list[dict], fractal_bars: int) -> list[SwingPoint]:
    """A tie (this swing exactly equals the previous one of the same kind —
    only realistically possible on a perfectly flat synthetic series, real
    tick data essentially never repeats a price exactly) is EQH/EQL, not
    forced into HH/LH — a flat market must read as structurally neutral, not
    as a fabricated "lower high" purely from a strict > comparison."""
    n = len(candles)
    points: list[SwingPoint] = []
    last_high: Optional[float] = None
    last_low: Optional[float] = None

    for i in range(fractal_bars, n - fractal_bars):
        window = candles[i - fractal_bars:i + fractal_bars + 1]
        this_high = candles[i]["high"]
        this_low = candles[i]["low"]

        if this_high == max(c["high"] for c in window):
            if last_high is None:
                label = None
            elif this_high > last_high:
                label = "HH"
            elif this_high < last_high:
                label = "LH"
            else:
                label = "EQH"
            points.append(SwingPoint(i, candles[i]["date"], "high", this_high, label))
            last_high = this_high

        if this_low == min(c["low"] for c in window):
            if last_low is None:
                label = None
            elif this_low > last_low:
                label = "HL"
            elif this_low < last_low:
                label = "LL"
            else:
                label = "EQL"
            points.append(SwingPoint(i, candles[i]["date"], "low", this_low, label))
            last_low = this_low

    return points


# ============================================================
# Support / resistance clustering
# ============================================================

@dataclass
class SRLevel:
    price: float
    touches: int
    kind: str  # "support" | "resistance" | "mixed" (clustered from both swing highs and lows)


def support_resistance_levels(candles: list[dict], sr_cluster_atr_multiple: float,
                               sr_min_touches: int, fractal_bars: int,
                               lookback_bars: int, atr_values: list[Optional[float]]) -> list[SRLevel]:
    """Clusters swing highs/lows within `sr_cluster_atr_multiple` ATRs of each
    other into levels; keeps levels with >= sr_min_touches. Uses the average
    of the recent (non-None) ATR readings as the clustering distance, since a
    single latest-bar ATR would make the clustering unstable bar-to-bar."""
    recent = candles[-lookback_bars:] if lookback_bars > 0 else candles
    offset = len(candles) - len(recent)
    pts = swing_points(recent, fractal_bars)

    recent_atrs = [a for a in atr_values[-lookback_bars:] if a is not None] if lookback_bars > 0 \
        else [a for a in atr_values if a is not None]
    if not recent_atrs or not pts:
        return []
    avg_atr = sum(recent_atrs) / len(recent_atrs)
    cluster_dist = avg_atr * sr_cluster_atr_multiple
    if cluster_dist <= 0:
        return []

    sorted_pts = sorted(pts, key=lambda p: p.price)
    clusters: list[list[SwingPoint]] = []
    for p in sorted_pts:
        if clusters and p.price - clusters[-1][-1].price <= cluster_dist:
            clusters[-1].append(p)
        else:
            clusters.append([p])

    levels = []
    for cluster in clusters:
        if len(cluster) < sr_min_touches:
            continue
        price = sum(p.price for p in cluster) / len(cluster)
        kinds = {p.kind for p in cluster}
        kind = "resistance" if kinds == {"high"} else "support" if kinds == {"low"} else "mixed"
        levels.append(SRLevel(price=price, touches=len(cluster), kind=kind))

    del offset  # offset kept for future use (absolute-index attribution); not needed by current callers
    return sorted(levels, key=lambda lv: lv.price)


def nearest_level(levels: list[SRLevel], price: float, direction: int) -> Optional[SRLevel]:
    """Nearest S/R level in the given direction (+1 = look above price, for a
    long/CE-buy runway; -1 = look below, for a short/PE-buy runway)."""
    candidates = [lv for lv in levels if (lv.price > price if direction > 0 else lv.price < price)]
    if not candidates:
        return None
    return min(candidates, key=lambda lv: abs(lv.price - price))


# ============================================================
# Opening range (first N minutes of the session)
# ============================================================

@dataclass
class OpeningRange:
    high: float
    low: float


def opening_range(candles: list[dict], minutes: int) -> Optional[OpeningRange]:
    """`candles` must be 1-min bars starting at session open (candles.py's
    backfill convention) for the minute cutoff to mean what it says."""
    if not candles:
        return None
    start = _as_ist(candles[0]["date"])
    window = [c for c in candles if (_as_ist(c["date"]) - start).total_seconds() < minutes * 60]
    if not window:
        return None
    return OpeningRange(high=max(c["high"] for c in window), low=min(c["low"] for c in window))
