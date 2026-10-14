from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from arb.var_errors import VarBudgetExceeded, VarRateLimited  # noqa: F401

HEDGE, EXIT, QUOTE, READ = 0, 1, 2, 3

_DAY_S = 86400.0


class QuotesSuspended(Exception):
    pass


def next_midnight_utc(wall_ts: float) -> float:
    d = datetime.fromtimestamp(wall_ts, tz=timezone.utc).date() + timedelta(days=1)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


class VarLimiter:
    def __init__(self, min_interval_s: float, strike_window_s: float, strike_max: int,
                 clock=time.monotonic, sleep=asyncio.sleep, daily_strike_max: int = 5,
                 wall=time.time, slow_after_strike_s: float = 3600.0) -> None:
        self.min_interval_s = min_interval_s
        self.strike_window_s = strike_window_s
        self.strike_max = strike_max
        self.daily_strike_max = daily_strike_max
        self.slow_after_strike_s = slow_after_strike_s
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self._last = -1e18
        self._blocked_until = 0.0
        self._strikes: deque[float] = deque()
        self._suspended_until = 0.0
        # I6 : strikes des dernières 24 h (horloge murale, pour survivre à un redémarrage) et
        # suspension des cotations jusqu'à minuit UTC au-delà de daily_strike_max.
        self._day_strikes: deque[float] = deque()
        self._day_suspended_until = 0.0
        self._last_strike_wall: Optional[float] = None
        # I6 : rappel appelé à chaque nouveau strike (horodatage mural) pour le persister.
        self.on_strike: Optional[Callable[[float], None]] = None
        self._urgent_waiting = 0
        self._lock = asyncio.Lock()

    def _prune(self) -> None:
        now = self._clock()
        while self._strikes and now - self._strikes[0] > self.strike_window_s:
            self._strikes.popleft()

    def _prune_day(self) -> None:
        now = self._wall()
        while self._day_strikes and now - self._day_strikes[0] > _DAY_S:
            self._day_strikes.popleft()

    def strikes_recent(self) -> int:
        self._prune()
        return len(self._strikes)

    def strikes_24h(self) -> int:
        self._prune_day()
        return len(self._day_strikes)

    def quotes_suspended(self) -> bool:
        return self._clock() < self._suspended_until or self._wall() < self._day_suspended_until

    def blocked_for(self) -> float:
        return max(0.0, self._blocked_until - self._clock())

    def quote_interval_mult(self) -> float:
        """I6 : cadence des cotations Var divisée par deux pendant 1 h après un strike."""
        if self._last_strike_wall is not None and self._wall() - self._last_strike_wall < self.slow_after_strike_s:
            return 2.0
        return 1.0

    def _add_day_strike(self, wall_ts: float) -> None:
        self._day_strikes.append(wall_ts)
        if self._last_strike_wall is None or wall_ts > self._last_strike_wall:
            self._last_strike_wall = wall_ts
        self._prune_day()
        if len(self._day_strikes) >= self.daily_strike_max:
            self._day_suspended_until = max(self._day_suspended_until, next_midnight_utc(wall_ts))

    def record_limited(self, wait_s: float) -> None:
        now = self._clock()
        self._blocked_until = max(self._blocked_until, now + max(0.0, wait_s))
        self._strikes.append(now)
        if self.strikes_recent() >= self.strike_max:
            self._suspended_until = now + self.strike_window_s
        w = self._wall()
        self._add_day_strike(w)
        if self.on_strike is not None:
            try:
                self.on_strike(w)
            except Exception:
                pass

    def load_strikes(self, wall_timestamps: Iterable[float]) -> None:
        """I6 : recharge au démarrage les strikes persistés (dernières 24 h)."""
        now_w, now_m = self._wall(), self._clock()
        for ts in sorted(float(t) for t in wall_timestamps):
            if now_w - ts > _DAY_S:
                continue
            self._add_day_strike(ts)
            age = now_w - ts
            if age <= self.strike_window_s:
                self._strikes.append(now_m - age)
        if self.strikes_recent() >= self.strike_max and self._strikes:
            self._suspended_until = max(self._suspended_until, self._strikes[-1] + self.strike_window_s)

    async def acquire(self, prio: int, deadline: Optional[float] = None) -> None:
        """Attend le droit d'envoyer une requête. I1 : si `deadline` (même horloge que le
        limiteur, time.monotonic par défaut) est fourni et que l'attente nécessaire dépasse le
        temps restant, lève VarRateLimited(attente) SANS dormir (et sans compter de strike)."""
        urgent = prio <= EXIT
        if not urgent and self.quotes_suspended():
            raise QuotesSuspended()
        if urgent:
            self._urgent_waiting += 1
        try:
            while True:
                if not urgent and self._urgent_waiting > 0:
                    await self._sleep(0.05)
                    continue
                async with self._lock:
                    now = self._clock()
                    wait = max(self._blocked_until - now, self._last + self.min_interval_s - now)
                    if wait <= 0:
                        self._last = now
                        return
                    if deadline is not None and wait > deadline - now:
                        raise VarBudgetExceeded(wait)
                await self._sleep(wait)
                if not urgent and self.quotes_suspended():
                    raise QuotesSuspended()
        finally:
            if urgent:
                self._urgent_waiting -= 1
