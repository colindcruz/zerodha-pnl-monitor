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
from positions_kite_adapter import label_positions
from position_engine import PositionHealthEngine, state_severity
from snapshot import build_snapshot
from trend_engine import TrendDirection, evaluate_trend

log = logging.getLogger("nifty-decision-dashboard")

_BULLISH = {TrendDirection.STRONG_BULL, TrendDirection.BULL, TrendDirection.WEAK_BULL}


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

    # -- recompute -----------------------------------------------------------

    def recompute(self, now: datetime = None) -> dict:
        now = now or datetime.now(IST)
        one_min = self.accumulator.as_sorted_list()
        if not one_min:
            return self.latest

        snapshot = build_snapshot(one_min, self.cfg.indicator)
        spot = one_min[-1]["close"]

        trend = evaluate_trend(snapshot, self.cfg.trend)
        trend_sign = 1 if trend.direction in _BULLISH else -1
        candidate_direction = "LONG" if trend_sign >= 0 else "SHORT"

        entry = evaluate_entry(snapshot, candidate_direction, self.cfg.entry)
        location = evaluate_location(snapshot, candidate_direction, self.cfg.location)
        decision = evaluate_decision(trend, entry, location, self.cfg.decision)
        key_levels = compute_key_levels(snapshot, self.prev_day)

        labeled_positions = self.poll_positions(now)
        position_health = {}
        for pos in labeled_positions:
            symbol = pos["tradingsymbol"]
            direction = "LONG" if pos["quantity"] > 0 else "SHORT"
            engine = self.position_engines.setdefault(symbol, PositionHealthEngine(self.cfg.position))
            result = engine.update(snapshot, direction)
            position_health[symbol] = result
            trade = self.tracked_trades.get(symbol)
            if trade is not None:
                update_excursion(trade, spot if symbol.startswith("NIFTY") else pos.get("last_price", spot))

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
        self.latest = {
            "ts": now.isoformat(), "tick_seq": self.tick_seq, "spot": spot,
            "trend": asdict(trend), "entry": asdict(entry), "location": asdict(location),
            "decision": asdict(decision), "key_levels": asdict(key_levels),
            "vwap_is_twap_fallback": vwap_is_twap,
            "positions": labeled_positions,
            "position_health": {s: asdict(r) for s, r in position_health.items()},
            "events": [asdict(e) for e in self.event_feed.recent(20)],
        }
        return self.latest
