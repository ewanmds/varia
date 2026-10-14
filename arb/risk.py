from __future__ import annotations

import time
from typing import Optional

from arb.config import GlobalCfg, MarketCfg


STALE_QUOTE_REASON = "cotation Var périmée"


def quote_block_reason(*, var_age: float, hl_stalled: bool, fills_alive: bool, strikes_suspended: bool,
                       sanity: bool, basis_dev_bps: float, cfg: MarketCfg, g: GlobalCfg, roll_window: bool,
                       daily_pnl: float, gross_hl_usd: float, gross_var_usd: float, hl_margin_ratio: float,
                       var_liq_distance: Optional[float], session_ok: bool,
                       book_stale: bool = False) -> Optional[str]:
    if not session_ok:
        return "session Var inactive"
    if daily_pnl <= -g.max_daily_loss_usd:
        return "perte journalière max atteinte"
    if var_age > g.quote_max_age_s:
        return STALE_QUOTE_REASON
    if hl_stalled:
        return "websocket HL muet"
    if not fills_alive:
        return "flux des fills HL coupé"
    # amendement t14 (fix) : carnet HL de CE marché périmé (silence global mais le carnet de ce
    # coin précis n'a pas été rafraîchi depuis longtemps) -> ce marché seul cesse de coter.
    if book_stale:
        return "carnet HL périmé"
    if strikes_suspended:
        return "cotations Var suspendues (strikes)"
    if not sanity:
        return "écart HL/Var hors garde-fou"
    if cfg.basis_mode == "ema":
        if cfg.roll_block and roll_window:
            return "fenêtre de roll"
        if basis_dev_bps > cfg.basis_guard_bps:
            return "basis hors moyenne"
    if gross_hl_usd > g.max_gross_usd or gross_var_usd > g.max_gross_usd:
        return "exposition brute max"
    if hl_margin_ratio > g.hl_margin_ratio_max:
        return "marge HL tendue"
    if var_liq_distance is not None and var_liq_distance < g.var_liq_distance_min:
        return "liquidation Var trop proche"
    return None


class ReconcileTracker:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._pending: dict[str, float] = {}
        self._corrected_at: dict[str, float] = {}

    def observe(self, market: str, delta_usd: float, fresh: bool, hedge_inflight: bool,
                fill_usd: float, max_delta_usd: float) -> str:
        if abs(delta_usd) < 0.2 * fill_usd:
            self._pending.pop(market, None)
            return "ok"
        if not fresh or hedge_inflight:
            return "wait"
        if abs(delta_usd) > max_delta_usd:
            self._pending.pop(market, None)
            return "block"
        prev = self._pending.get(market)
        if prev is None or (prev > 0) != (delta_usd > 0) or abs(delta_usd - prev) > 0.2 * abs(prev):
            self._pending[market] = delta_usd
            return "wait"
        self._pending.pop(market, None)
        last = self._corrected_at.get(market)
        if last is not None and self._clock() - last < 3600:
            return "block"
        if abs(delta_usd) > fill_usd:
            return "block"
        return "correct"

    def mark_corrected(self, market: str) -> None:
        self._corrected_at[market] = self._clock()
