"""
Orchestrator: owns the live Kite session's read side (candle backfill + live
tick accumulation), and on each recompute tick wires
candles -> snapshot -> {trend, entry, location, decision} engines, plus a
per-symbol Position Management Engine for every currently-held NIFTY
position (see positions_kite_adapter.py — this tracks EVERYTHING
NIFTY-related in the account, not just this service's own advisory calls).

Deliberately takes a duck-typed `kite` object (anything with
`historical_data`/`positions`/`ltp` methods matching KiteConnect's
signatures) rather than importing KiteConnect directly — this is what lets
DashboardState be exercised in tests against a small fake, the same
reasoning long_option_live.py's LiveEngine gives for not being driven by
module-level globals.

Trend Engine and Position Management Engine are both explicitly 5-minute-bar
concepts (see their own module docstrings — position_engine.py's hysteresis
`confirm_bars`/`recover_confirm_bars` are documented in terms of 5-min bar
counts). server.py calls recompute() on a fast wall-clock cadence
(RECOMPUTE_INTERVAL_SECONDS, for a responsive-feeling UI), which is NOT the
same thing — recomputing those two engines on every 15-second tick would
make the hysteresis fire many times faster than its own config intends.
This module gates trend/position-health re-evaluation to actual new-5-min-
bar boundaries (tracked via _last_tf5_bar_date) and only re-runs
Entry/Location/Decision (2-min/live-price concepts) on every tick.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from candles import IST, OneMinuteAccumulator, normalize_historical, today_ist_date
from config import DashboardConfig
from decision_engine import evaluate_decision
from entry_engine import evaluate_entry
from events import EventFeed
from key_levels import PrevDayOHLC, compute_key_levels
from location_engine import evaluate_location
from logging_jsonl import TrackedTrade, log_tick, log_trade_event, update_excursion
from long_option_state_reader import read_open_anchor
from positions_kite_adapter import label_positions
from position_engine import PositionHealthEngine, state_severity
from snapshot import build_snapshot
from trend_engine import TrendDirection, evaluate_trend

log = logging.getLogger("nifty-decision-dashboard")

_BULLISH = {TrendDirection.STRONG_BULL, TrendDirection.BULL, TrendDirection.WEAK_BULL}
_BEARISH = {TrendDirection.STRONG_BEAR, TrendDirection.BEAR, TrendDirection.WEAK_BEAR}

VOTE_PERSISTENCE_WINDOW = 5  # bars — "Persistence: N/5" in the UI


def _event_asdict(event) -> dict:
    """Event.timestamp is a raw datetime — dataclasses.asdict() leaves it as
    one, not a JSON-safe string (unlike log_tick's use of asdict() on the
    engine results, which never carry a bare datetime field). Every place
    an Event ends up in self.latest must go through this, not a plain
    asdict(), or json.dumps() in server.py's broadcast_state()/handle_ws()
    raises TypeError the moment a real event actually fires."""
    return {**asdict(event), "timestamp": event.timestamp.isoformat()}


def _trend_sign(direction: TrendDirection) -> int:
    if direction in _BULLISH:
        return 1
    if direction in _BEARISH:
        return -1
    return 0


class DashboardState:
    def __init__(self, kite, cfg: DashboardConfig = None, tick_log_path: Path = None,
                 trade_log_path: Path = None, strangle_state_path=None, hedge_state_path=None,
                 long_option_state_path=None):
        self.kite = kite
        self.cfg = cfg or DashboardConfig()
        self.tick_log_path = tick_log_path or Path("nifty_decision_tick_log.jsonl")
        self.trade_log_path = trade_log_path or Path("nifty_decision_trade_log.jsonl")
        self.strangle_state_path = strangle_state_path or Path("../strangle_state.json")
        self.hedge_state_path = hedge_state_path or Path("../hedge_state.json")
        self.long_option_state_path = long_option_state_path or Path("../long_option_state.json")

        self.spot_token: Optional[int] = None
        self.accumulator = OneMinuteAccumulator()
        self.event_feed = EventFeed()
        self.position_engines: dict[str, PositionHealthEngine] = {}
        self.tracked_trades: dict[str, TrackedTrade] = {}
        self.last_positions: dict[str, dict] = {}
        self.prev_day: Optional[PrevDayOHLC] = None
        self.tick_seq = 0
        self.latest: dict = {}

        # Bar-boundary-gated state (see module docstring) — only ever
        # touched from _on_new_bar / recompute, never re-derived per tick.
        self._last_tf5_bar_date = None
        self._cached_trend = None
        self._cached_position_health: dict = {}
        self._trend_age_bars = 0
        self._trend_sign_state = 0
        self._vote_history: list = []

    # -- setup -----------------------------------------------------------

    def resolve_spot_token(self) -> int:
        if self.spot_token is None:
            data = self.kite.ltp(["NSE:NIFTY 50"])
            self.spot_token = data["NSE:NIFTY 50"]["instrument_token"]
        return self.spot_token

    def backfill(self, now: datetime = None) -> None:
        """Backfills 1-min candles from 09:15 IST today through `now` (or the
        current time)."""
        now = now or datetime.now(IST)
        token = self.resolve_spot_token()
        session_start_str = now.astimezone(IST).strftime("%Y-%m-%d 09:15:00")
        now_str = now.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
        try:
            raw = self.kite.historical_data(token, session_start_str, now_str, "minute")
        except Exception as exc:
            log.warning("Backfill failed: %s", exc)
            return
        self.accumulator.seed_from_historical(normalize_historical(raw))

    def fetch_prev_day_ohlc(self, now: datetime = None) -> None:
        now = now or datetime.now(IST)
        token = self.resolve_spot_token()
        today = today_ist_date(now)
        try:
            raw = self.kite.historical_data(
                token, (today - timedelta(days=10)).isoformat(), today.isoformat(), "day",
            )
        except Exception as exc:
            log.warning("Prev-day OHLC fetch failed: %s", exc)
            return
        candles = [c for c in normalize_historical(raw) if c["date"].date() < today] if raw else []
        if candles:
            last = candles[-1]
            self.prev_day = PrevDayOHLC(high=last["high"], low=last["low"], close=last["close"])

    # -- live ticks --------------------------------------------------------

    def on_tick(self, ts: datetime, price: float, cum_volume: Optional[float] = None) -> None:
        self.accumulator.on_tick(ts, price, cum_volume)

    # -- positions -----------------------------------------------------------

    def poll_positions(self, now: datetime = None) -> list:
        now = now or datetime.now(IST)
        try:
            broker_positions = self.kite.positions()["net"]
        except Exception as exc:
            log.warning("positions() poll failed: %s", exc)
            return list(self.last_positions.values())

        labeled = label_positions(broker_positions, self.strangle_state_path, self.hedge_state_path,
                                   self.long_option_state_path)
        self._enrich_long_option_detail(labeled)
        by_symbol = {p["tradingsymbol"]: p for p in labeled}

        for symbol, pos in by_symbol.items():
            if symbol not in self.last_positions:
                direction = "LONG" if pos["quantity"] > 0 else "SHORT"
                trade = TrackedTrade(
                    trade_id=f"{symbol}@{now.isoformat()}", symbol=symbol, direction=direction,
                    entry_price=float(pos.get("average_price") or pos.get("last_price") or 0.0),
                    entry_ts=now, dashboard_state_at_entry=self.latest or None,
                )
                self.tracked_trades[symbol] = trade
                self.position_engines[symbol] = PositionHealthEngine(self.cfg.position)
                log_trade_event(self.trade_log_path, "ENTRY_DETECTED", trade, price=trade.entry_price, ts=now)

        for symbol in list(self.last_positions.keys()):
            if symbol not in by_symbol:
                trade = self.tracked_trades.pop(symbol, None)
                self.position_engines.pop(symbol, None)
                if trade is not None:
                    last_price = self.last_positions[symbol].get("last_price", trade.entry_price)
                    log_trade_event(self.trade_log_path, "EXIT_DETECTED", trade, price=last_price, ts=now)

        self.last_positions = by_symbol
        return labeled

    def _enrich_long_option_detail(self, labeled: list) -> None:
        """Attaches the long-option engine's own T1/T2/stop-trail state
        (read-only — see long_option_state_reader.py) to positions it owns,
        for the Open Position panel."""
        for pos in labeled:
            if pos.get("owner") == "long_option":
                detail = read_open_anchor(self.long_option_state_path, pos["tradingsymbol"])
                if detail:
                    pos["long_option_detail"] = detail

    # -- bar-boundary-gated engines -------------------------------------

    def _on_new_bar(self, snapshot):
        """Trend Engine + vote-persistence history + trend-age all advance
        exactly once per NEW 5-min bar, never per recompute tick."""
        trend = evaluate_trend(snapshot, self.cfg.trend)
        sign = _trend_sign(trend.direction)
        if sign != 0 and sign == self._trend_sign_state:
            self._trend_age_bars += 1
        else:
            self._trend_age_bars = 1 if sign != 0 else 0
        self._trend_sign_state = sign

        self._vote_history.append(trend.votes)
        if len(self._vote_history) > VOTE_PERSISTENCE_WINDOW:
            self._vote_history.pop(0)

        self._cached_trend = trend
        return trend

    def _vote_persistence(self) -> dict:
        """{"aroon": {"count": 5, "of": 5}, ...} — how many of the last few
        bars each individual vote has matched its CURRENT value."""
        if not self._vote_history:
            return {}
        current = self._vote_history[-1]
        return {
            key: {"count": sum(1 for v in self._vote_history if v.get(key) == value),
                  "of": len(self._vote_history)}
            for key, value in current.items()
        }

    def _update_position_health(self, snapshot, labeled_positions: list) -> dict:
        position_health = {}
        for pos in labeled_positions:
            symbol = pos["tradingsymbol"]
            direction = "LONG" if pos["quantity"] > 0 else "SHORT"
            engine = self.position_engines.setdefault(symbol, PositionHealthEngine(self.cfg.position))
            position_health[symbol] = engine.update(snapshot, direction)
        self._cached_position_health = position_health
        return position_health

    # -- derived, glanceable summaries -------------------------------------

    @staticmethod
    def _market_tone(trend_sign: int) -> str:
        return "POSITIVE" if trend_sign > 0 else "NEGATIVE" if trend_sign < 0 else "NEUTRAL"

    @staticmethod
    def _intraday_bias(trend_sign: int, snapshot) -> dict:
        orange = snapshot.opening_range
        vwap_v = snapshot.tf5.vwap_value[-1] if snapshot.tf5.vwap_value else None
        if trend_sign > 0:
            level = orange.low if orange is not None else vwap_v
            return {"label": "LONG BIAS",
                    "message": f"Shorts only below {level:.0f} with confirmation" if level is not None
                    else "Shorts only on a confirmed breakdown"}
        if trend_sign < 0:
            level = orange.high if orange is not None else vwap_v
            return {"label": "SHORT BIAS",
                    "message": f"Longs only above {level:.0f} with confirmation" if level is not None
                    else "Longs only on a confirmed breakout"}
        return {"label": "NEUTRAL", "message": "No directional bias yet"}

    @staticmethod
    def _trend_detail(snapshot) -> dict:
        """Raw indicator numbers behind the Trend Engine's votes (Aroon
        up/down, EMA20/50, VWAP distance, DI+/-, ADX, ATR, price structure
        label) — the engines themselves only return the +-1 vote and a
        human sentence (see trend_engine.py's TrendResult); this is a
        UI-serving bundle assembled straight from the same snapshot, not a
        change to the engine's own pure return type."""
        tf = snapshot.tf5
        price = tf.candles[-1]["close"] if tf.candles else None
        vwap_v = tf.vwap_value[-1]
        highs = [p for p in snapshot.swing_points_5m if p.kind == "high" and p.label]
        lows = [p for p in snapshot.swing_points_5m if p.kind == "low" and p.label]
        return {
            "aroon_up": tf.aroon.up[-1], "aroon_down": tf.aroon.down[-1],
            "ema_fast": tf.ema_fast[-1], "ema_slow": tf.ema_slow[-1],
            "vwap": vwap_v,
            "vwap_distance_points": (price - vwap_v) if (price is not None and vwap_v is not None) else None,
            "plus_di": tf.dmi_adx.plus_di[-1], "minus_di": tf.dmi_adx.minus_di[-1], "adx": tf.dmi_adx.adx[-1],
            "atr": tf.atr[-1],
            "structure_label": f"{highs[-1].label}/{lows[-1].label}" if highs and lows else None,
        }

    @staticmethod
    def _upcoming_levels(key_levels, limit: int = 3) -> list:
        """The nearest few key levels (excluding VWAP, already shown
        prominently elsewhere), sorted by absolute distance from price."""
        candidates = sorted((lv for lv in key_levels.levels if lv.name != "VWAP"),
                             key=lambda lv: abs(lv.distance_points))
        return [{"name": lv.name, "price": lv.price, "distance_points": lv.distance_points}
                for lv in candidates[:limit]]

    # -- recompute -----------------------------------------------------------

    def recompute(self, now: datetime = None) -> dict:
        now = now or datetime.now(IST)
        one_min = self.accumulator.as_sorted_list()
        if not one_min:
            return self.latest

        snapshot = build_snapshot(one_min, self.cfg.indicator)
        spot = one_min[-1]["close"]

        new_bar = bool(snapshot.tf5.candles) and (
            self._last_tf5_bar_date is None or snapshot.tf5.candles[-1]["date"] != self._last_tf5_bar_date
        )
        if new_bar:
            self._last_tf5_bar_date = snapshot.tf5.candles[-1]["date"]
            trend = self._on_new_bar(snapshot)
        else:
            trend = self._cached_trend or evaluate_trend(snapshot, self.cfg.trend)

        trend_sign = _trend_sign(trend.direction)
        candidate_direction = "LONG" if trend_sign >= 0 else "SHORT"

        entry = evaluate_entry(snapshot, candidate_direction, self.cfg.entry)
        location = evaluate_location(snapshot, candidate_direction, self.cfg.location)
        decision = evaluate_decision(trend, entry, location, self.cfg.decision)
        key_levels = compute_key_levels(snapshot, self.prev_day)

        labeled_positions = self.poll_positions(now)
        if new_bar:
            position_health = self._update_position_health(snapshot, labeled_positions)
        else:
            live_symbols = {p["tradingsymbol"] for p in labeled_positions}
            position_health = {s: r for s, r in self._cached_position_health.items() if s in live_symbols}
            self._cached_position_health = position_health

        for pos in labeled_positions:
            trade = self.tracked_trades.get(pos["tradingsymbol"])
            if trade is not None:
                update_excursion(trade, pos.get("last_price", spot))

        self.tick_seq += 1
        overall_position_health = None
        if position_health:
            worst_symbol = max(position_health, key=lambda s: state_severity(position_health[s].state))
            overall_position_health = position_health[worst_symbol].state

        self.event_feed.update(
            now, trend_direction=trend.direction, trend_strength=trend.strength, momentum=trend.momentum,
            entry_setup=entry.label, decision_permission=decision.permission, position_health=overall_position_health,
        )

        log_tick(
            self.tick_log_path, now, self.tick_seq, spot, trend=trend, entry=entry, location=location,
            decision=decision, positions=labeled_positions, key_levels=key_levels,
            position_health=None,  # per-symbol dict logged separately below (asdict can't flatten a dict-of-dataclass)
        )

        vwap_is_twap = snapshot.tf5.vwap_is_twap[-1] if snapshot.tf5.vwap_is_twap else False
        recent_events = self.event_feed.recent(20)
        self.latest = {
            "ts": now.isoformat(), "tick_seq": self.tick_seq, "spot": spot,
            "trend": asdict(trend), "entry": asdict(entry), "location": asdict(location),
            "decision": asdict(decision), "key_levels": asdict(key_levels),
            "vwap_is_twap_fallback": vwap_is_twap,
            "positions": labeled_positions,
            "position_health": {s: asdict(r) for s, r in position_health.items()},
            "events": [_event_asdict(e) for e in recent_events],
            "latest_signal": _event_asdict(recent_events[-1]) if recent_events else None,
            "trend_age_bars": self._trend_age_bars,
            "trend_detail": self._trend_detail(snapshot),
            "vote_persistence": self._vote_persistence(),
            "market_tone": self._market_tone(trend_sign),
            "intraday_bias": self._intraday_bias(trend_sign, snapshot),
            "upcoming_levels": self._upcoming_levels(key_levels),
        }
        return self.latest
