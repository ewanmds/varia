from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from arb.config import MarketCfg
from arb.pricing import VarQuote
from arb.var_client import VarError
from arb.var_limiter import QuotesSuspended


class VarFeed:
    """Cotations Var indicatives, une requête à la fois.

    Ordonnancement (F1/F2, docs/superpowers/fresh-quote-2026-09-23.md) : chaque marché est coté
    toutes les `interval_s` (rotation habituelle) ; un marché « chaud » (`hot_fn`) l'est toutes les
    `hot_interval_s` ; une cotation fraîche demandée par `request_fresh` passe en premier. Deux
    requêtes du feed sont toujours espacées d'au moins `min_gap_s` (défaut : l'intervalle du
    limiteur Var), le tout ralenti par 2 pendant 1 h après un strike (I6). Quand la demande
    dépasse ce plafond, les marchés chauds passent avant la rotation, sauf un marché froid dont la
    dernière tentative date de plus de 2 intervalles (jamais affamé)."""

    _IDLE_S = 0.05   # granularité d'attente quand rien n'est dû (réactivité à request_fresh)

    def __init__(self, client, markets: list[MarketCfg], interval_s: float,
                 sleep=asyncio.sleep, clock=time.time, hot_interval_s: float = 1.0,
                 min_gap_s: Optional[float] = None) -> None:
        self.client = client
        self.markets = {m.var_asset: m for m in markets}
        self.interval_s = interval_s
        self.hot_interval_s = hot_interval_s
        self._min_gap_s = min_gap_s
        self._sleep = sleep
        self._clock = clock
        self._quotes: dict[str, VarQuote] = {}
        self._ref: dict[str, float] = {}
        self.errors: dict[str, int] = {a: 0 for a in self.markets}
        # F2 : vrai si le marché est chaud (fourni par l'orchestrateur).
        self.hot_fn: Callable[[str], bool] = lambda asset: False
        # F1 : cotations fraîches demandées, dans l'ordre des demandes (sans doublon).
        self._urgent: list[str] = []
        # début (horloge du feed) de la dernière tentative par marché, et échec de celle-ci.
        self._last_try: dict[str, float] = {}
        self._failed: dict[str, bool] = {}
        self._last_send = -1e18

    def latest(self, asset: str) -> Optional[VarQuote]:
        return self._quotes.get(asset)

    def age(self, asset: str) -> float:
        q = self._quotes.get(asset)
        return float("inf") if q is None else max(0.0, self._clock() - q.ts)

    def set_ref_price(self, asset: str, px: float) -> None:
        if px > 0:
            self._ref[asset] = px

    def qty_for(self, asset: str) -> Optional[float]:
        ref = self._ref.get(asset)
        m = self.markets.get(asset)
        if not ref or m is None:
            return None
        return m.fill_usd / ref

    def request_fresh(self, asset: str) -> None:
        """F1 : demander une cotation fraîche de `asset`, servie avant toute autre (même file,
        même limiteur que les cotations normales)."""
        m = self.markets.get(asset)
        if m is not None and m.enabled and asset not in self._urgent:
            self._urgent.append(asset)

    def refresh_failed(self, asset: str) -> bool:
        """F3 : vrai si la dernière tentative de cotation de `asset` n'a rien rapporté."""
        return self._failed.get(asset, False)

    def _pollable(self, asset: str) -> bool:
        m = self.markets.get(asset)
        return m is not None and m.enabled and self.qty_for(asset) is not None

    async def poll_once(self, asset: str) -> None:
        m = self.markets[asset]
        qty = self.qty_for(asset)
        if qty is None or not m.enabled:
            return
        self._last_try[asset] = self._clock()
        try:
            q = await self.client.quote(asset, m.var_kind, qty)
        except (VarError, QuotesSuspended):
            self.errors[asset] = self.errors.get(asset, 0) + 1
            self._failed[asset] = True
            return
        self._failed[asset] = False
        self._quotes[asset] = q
        self.set_ref_price(asset, q.mid)

    def _mult(self) -> float:
        lim = getattr(self.client, "limiter", None)
        return lim.quote_interval_mult() if lim is not None and hasattr(lim, "quote_interval_mult") else 1.0

    def min_gap_s(self) -> float:
        if self._min_gap_s is not None:
            base = self._min_gap_s
        else:
            lim = getattr(self.client, "limiter", None)
            base = float(getattr(lim, "min_interval_s", 0.0) or 0.0)
        return base * self._mult()

    def _is_hot(self, asset: str) -> bool:
        try:
            return bool(self.hot_fn(asset))
        except Exception:
            return False

    def next_asset(self) -> tuple[Optional[str], float]:
        """Marché à coter maintenant (ou None) et, sinon, délai avant la prochaine échéance."""
        while self._urgent:
            a = self._urgent.pop(0)
            if self._pollable(a):
                return a, 0.0
        now, mult = self._clock(), self._mult()
        cold_s, hot_s = self.interval_s * mult, self.hot_interval_s * mult
        starving, hot_due, cold_due = [], [], []
        next_due = float("inf")
        for a in self.markets:
            if not self._pollable(a):
                continue
            since = now - self._last_try.get(a, -1e18)
            if self._is_hot(a):
                period = hot_s
                if since >= hot_s:
                    hot_due.append((-since, a))
            else:
                period = cold_s
                if since >= 2 * cold_s:
                    starving.append((-since, a))
                elif since >= cold_s:
                    cold_due.append((-since, a))
            next_due = min(next_due, period - since)
        for group in (starving, hot_due, cold_due):
            if group:
                return min(group)[1], 0.0
        return None, max(0.0, next_due)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            gap = self._last_send + self.min_gap_s() - self._clock()
            if gap > 0:
                await self._sleep(gap)
                continue
            a, wait = self.next_asset()
            if a is None:
                await self._sleep(min(self._IDLE_S, wait) if wait > 0 else self._IDLE_S)
                continue
            self._last_send = self._clock()
            await self.poll_once(a)

    def step_s(self) -> float:
        """Pause moyenne entre deux cotations de la rotation. I6 : doublée pendant 1 h après un
        strike Var."""
        return self.interval_s * self._mult() / max(1, len(self.markets))
