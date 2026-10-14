from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from arb.config import MarketCfg

PERP_MAX_DECIMALS = 6
SIG_FIGS = 5


@dataclass(frozen=True)
class VarQuote:
    bid: float
    ask: float
    qty: float
    ts: float
    quote_id: str = ""

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


def half_spread_rel(q: VarQuote) -> float:
    """Demi-spread relatif d'une cotation (0,0005 = 5 pb)."""
    m = q.mid
    return max(0.0, (q.ask - q.bid) / 2.0 / m) if m > 0 else 0.0


def ws_reference(rfq: VarQuote, mark: float, ts: float) -> VarQuote:
    """L2 (levers-2026-09-23) : bid/ask exécutable Var estimé à partir du prix websocket (sans
    bid/ask) et du demi-spread relatif de la DERNIÈRE cotation RFQ du marché. `ts` = réception du
    prix websocket (horloge murale, même échelle que VarQuote.ts)."""
    hs = half_spread_rel(rfq)
    return VarQuote(mark * (1 - hs), mark * (1 + hs), rfq.qty, ts=ts, quote_id="ws")


def _decimals_for(px: float, sz_decimals: int) -> int:
    max_dec = max(0, PERP_MAX_DECIMALS - sz_decimals)
    if px <= 0:
        return max_dec
    int_digits = int(math.floor(math.log10(px))) + 1
    return max(0, min(max_dec, SIG_FIGS - int_digits))


def round_px(px: float, sz_decimals: int, is_buy: bool) -> float:
    """Prix HL valide : 5 chiffres significatifs, au plus 6 - szDecimals décimales.
    Un achat arrondit vers le bas, une vente vers le haut (on ne dégrade jamais la marge)."""
    d = _decimals_for(px, sz_decimals)
    f = 10 ** d
    v = math.floor(px * f + 1e-9) / f if is_buy else math.ceil(px * f - 1e-9) / f
    return round(v, d)


def round_sz(sz: float, sz_decimals: int) -> float:
    f = 10 ** sz_decimals
    return math.floor(sz * f + 1e-9) / f


def inventory_ratio(pos_usd: float, max_pos_usd: float) -> float:
    if max_pos_usd <= 0:
        return 0.0
    return max(-1.0, min(1.0, pos_usd / max_pos_usd))


def side_margins_bps(cfg: MarketCfg, fee_bps: float, ratio: float,
                     wide_mult: float) -> tuple[Optional[float], Optional[float]]:
    """Marges (pb) du côté achat et du côté vente HL. ratio > 0 = long HL."""
    base = (cfg.min_edge_bps + fee_bps + cfg.slip_reserve_bps) * wide_mult
    exit_total = cfg.exit_edge_bps + fee_bps + cfg.slip_reserve_bps
    span = max(0.0, base - exit_total)
    a = abs(ratio)
    reducing = base - a * span
    loading = None if a >= 1.0 else base + a * span
    if ratio > 0:
        return loading, reducing
    if ratio < 0:
        return reducing, loading
    return base, base


def chase_margins_bps(cfg: MarketCfg, fee_bps: float, ratio: float,
                      wide_mult: float) -> tuple[Optional[float], Optional[float]]:
    """Seuils (pb, frais maker et réserve inclus) du mode chase, côté achat et côté vente HL.
    Même biais d'inventaire que le mode maker, sauf que le côté qui réduit une position ouverte
    prend directement le seuil de sortie (exit_edge_bps) : la position se referme dès que
    l'écart inverse apparaît."""
    m_buy, m_sell = side_margins_bps(cfg, fee_bps, ratio, wide_mult)
    exit_total = cfg.exit_edge_bps + fee_bps + cfg.slip_reserve_bps
    if ratio > 0:
        m_sell = exit_total
    elif ratio < 0:
        m_buy = exit_total
    return m_buy, m_sell


def maker_prices(q: VarQuote, basis: float, cfg: MarketCfg, fee_bps: float, pos_usd: float,
                 wide_mult: float, hl_bid: Optional[float], hl_ask: Optional[float],
                 sz_decimals: int) -> tuple[Optional[float], Optional[float]]:
    m_buy, m_sell = side_margins_bps(cfg, fee_bps, inventory_ratio(pos_usd, cfg.max_position_usd), wide_mult)
    buy = sell = None
    if m_buy is not None:
        px = (q.bid + basis) * (1 - m_buy / 1e4)
        if hl_ask is not None and px >= hl_ask:
            px = hl_ask * (1 - 1e-5)
        buy = round_px(px, sz_decimals, True)
        if hl_ask is not None and buy >= hl_ask:
            buy = None
    if m_sell is not None:
        px = (q.ask + basis) * (1 + m_sell / 1e4)
        if hl_bid is not None and px <= hl_bid:
            px = hl_bid * (1 + 1e-5)
        sell = round_px(px, sz_decimals, False)
        if hl_bid is not None and sell <= hl_bid:
            sell = None
    return buy, sell


class BasisEma:
    def __init__(self, tau_s: float) -> None:
        self.tau_s = tau_s
        self.value: Optional[float] = None
        self._ts: Optional[float] = None
        self._t0: Optional[float] = None

    def warm_s(self) -> float:
        """Durée (s) couverte par les mises à jour depuis la première (chauffe de l'EMA)."""
        if self._t0 is None or self._ts is None:
            return 0.0
        return max(0.0, self._ts - self._t0)

    def update(self, hl_mid: float, var_mid: float, ts: float) -> None:
        b = hl_mid - var_mid
        if self.value is None or self._ts is None:
            self.value, self._ts, self._t0 = b, ts, ts
            return
        dt = max(0.0, ts - self._ts)
        alpha = 1.0 - math.exp(-dt / self.tau_s) if self.tau_s > 0 else 1.0
        self.value += alpha * (b - self.value)
        self._ts = ts

    def deviation_bps(self, hl_mid: float, var_mid: float) -> float:
        if self.value is None or var_mid <= 0:
            return 0.0
        return abs((hl_mid - var_mid) - self.value) / var_mid * 1e4


def sanity_ok(hl_mid: float, var_mid_adj: float, sanity_bps: float) -> bool:
    if hl_mid <= 0 or var_mid_adj <= 0:
        return False
    return abs(hl_mid - var_mid_adj) / var_mid_adj * 1e4 <= sanity_bps


def edge_bps(hl_is_buy: bool, hl_px: float, var_px: float, fee_bps: float) -> float:
    """Écart réalisé net des frais HL. Achat HL puis vente Var : (var - hl) / hl."""
    raw = (var_px - hl_px) / hl_px if hl_is_buy else (hl_px - var_px) / var_px
    return raw * 1e4 - fee_bps
