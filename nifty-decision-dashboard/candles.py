"""
Candle series: backfill from kite.historical_data, live accumulation from
ticks, and timeframe re-bucketing — all server-side in Python (unlike
live-dashboard's client-side JS bucketing), since this service's engines need
real OHLC series to compute indicators against, not just a display chart.

The bucketing algorithm below is a direct Python port of
live-dashboard/dashboard.html's bucketStartRealMs/mergeTimeframe: candle
boundaries are anchored to 09:15 IST regardless of the caller's own
timezone, computed via a fixed +05:30 offset (no DST in India, so this is
exact, not an approximation).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SESSION_START_HOUR, SESSION_START_MINUTE = 9, 15


def _as_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def session_start(dt: datetime) -> datetime:
    """09:15 IST on dt's own (IST) calendar date."""
    ist = _as_ist(dt)
    return ist.replace(hour=SESSION_START_HOUR, minute=SESSION_START_MINUTE, second=0, microsecond=0)


def bucket_start(dt: datetime, minutes: int) -> datetime:
    """Which `minutes`-wide, 09:15-IST-anchored bucket `dt` falls into. Mirrors
    dashboard.html's bucketStartRealMs exactly, just in IST-datetime terms
    instead of epoch-ms terms."""
    sess_start = session_start(dt)
    ist = _as_ist(dt)
    elapsed = (ist - sess_start).total_seconds()
    idx = int(elapsed // (minutes * 60))
    return sess_start + timedelta(minutes=minutes * idx)


def bucket_candles(one_min_candles: list[dict], minutes: int) -> list[dict]:
    """Re-buckets a list of 1-min candles (each {"date","open","high","low",
    "close","volume"}, ascending by date) into `minutes`-wide candles anchored
    to 09:15 IST. minutes==1 returns the input unchanged (as a shallow copy
    per-candle, so callers can't accidentally mutate the source list).
    Mirrors dashboard.html's mergeTimeframe: open/close come from the
    chronologically first/last 1-min candle in the bucket, high/low are the
    max/min across the whole bucket, volume sums across the bucket."""
    if minutes <= 1:
        return [dict(c) for c in one_min_candles]

    groups: dict[datetime, dict] = {}
    order: list[datetime] = []
    for c in one_min_candles:
        key = bucket_start(c["date"], minutes)
        g = groups.get(key)
        if g is None:
            g = {
                "date": key, "open": c["open"], "high": c["high"], "low": c["low"],
                "close": c["close"], "volume": c.get("volume", 0) or 0,
                "_first_date": c["date"], "_last_date": c["date"],
            }
            groups[key] = g
            order.append(key)
        else:
            if c["date"] < g["_first_date"]:
                g["open"] = c["open"]
                g["_first_date"] = c["date"]
            if c["date"] > g["_last_date"]:
                g["close"] = c["close"]
                g["_last_date"] = c["date"]
            g["high"] = max(g["high"], c["high"])
            g["low"] = min(g["low"], c["low"])
            g["volume"] += c.get("volume", 0) or 0

    order.sort()
    out = []
    for key in order:
        g = groups[key]
        out.append({
            "date": g["date"], "open": g["open"], "high": g["high"],
            "low": g["low"], "close": g["close"], "volume": g["volume"],
        })
    return out


def normalize_historical(raw_candles: list[dict]) -> list[dict]:
    """kite.historical_data rows already match {"date","open","high","low",
    "close","volume"} shape almost exactly — this just ensures volume
    defaults to 0 (indices can return volume missing/None) and drops any
    extra keys (e.g. "oi") so downstream code has one consistent shape."""
    return [
        {
            "date": c["date"], "open": c["open"], "high": c["high"],
            "low": c["low"], "close": c["close"], "volume": c.get("volume") or 0,
        }
        for c in raw_candles
    ]


class OneMinuteAccumulator:
    """Live tick -> 1-min candle accumulation, the server-side equivalent of
    dashboard.html's onTick/onSpotTick. Ticks carry cumulative day volume
    (Kite's MODE_FULL convention), so per-bucket volume is derived by diffing
    consecutive readings — the first tick after (re)start just establishes
    the baseline with no prior reading to diff against."""

    def __init__(self):
        self.candles: dict[datetime, dict] = {}
        self._last_cum_volume: float | None = None

    def on_tick(self, ts: datetime, price: float, cum_volume: float | None = None) -> None:
        delta_vol = 0
        if cum_volume is not None:
            if self._last_cum_volume is not None:
                delta_vol = max(0, cum_volume - self._last_cum_volume)
            self._last_cum_volume = cum_volume

        key = bucket_start(ts, 1)
        c = self.candles.get(key)
        if c is None:
            self.candles[key] = {"date": key, "open": price, "high": price, "low": price,
                                  "close": price, "volume": delta_vol}
        else:
            c["close"] = price
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["volume"] += delta_vol

    def seed_from_historical(self, one_min_candles: list[dict]) -> None:
        """Merge backfilled 1-min candles into the live accumulator (used once
        at startup) — a candle already accumulated live for the same bucket
        (i.e. backfill ran again after ticks had already arrived) is left
        untouched, since the live reading is more current than a re-fetch."""
        for c in one_min_candles:
            key = c["date"]
            if key not in self.candles:
                self.candles[key] = dict(c)

    def as_sorted_list(self) -> list[dict]:
        return [self.candles[k] for k in sorted(self.candles.keys())]


def sample_last_per_bucket(one_min_candles: list[dict], values: list, minutes: int) -> dict[datetime, object]:
    """Given a list of values aligned 1:1 with one_min_candles (e.g. a VWAP
    series computed at 1-min granularity), returns the value observed at the
    LAST 1-min bar within each `minutes`-wide bucket — the correct way to
    align a cumulative-from-session-start series like VWAP onto a coarser
    candle series. Re-averaging VWAP within the bucket would compute a
    different (wrong) number: VWAP is a running total from day start, not a
    per-bucket statistic. Mirrors dashboard.html's mergeTimeframe sampling
    VWAP at vwapByMinute.get(_lastT)."""
    out: dict[datetime, object] = {}
    for c, v in zip(one_min_candles, values):
        key = bucket_start(c["date"], minutes)
        out[key] = v  # ascending input -> last write per bucket wins
    return out


def today_ist_date(now: datetime = None) -> date:
    now = now or datetime.now(IST)
    return _as_ist(now).date()
