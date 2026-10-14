"""Mode paire pétrole en paper : carnet de positions simulé pour les deux actifs de la paire.

Les cotations restent réelles (prix vrais), mais aucun ordre ne part : `market` met à jour une
quantité simulée au prix coté (ask à l'achat, bid à la vente), `order_status` renvoie « cleared »
et `positions()` renvoie les seules positions simulées de la paire. Sans ce carnet, les
fermetures en quantité et les contrôles d'équilibre de PairExecutor liraient les vraies
positions (nulles) et le paper ne testerait rien.
"""
from __future__ import annotations

from typing import Optional

from arb.config import PairCfg
from arb.var_errors import VarRejected


class PaperPairVar:
    paper = True

    def __init__(self, inner, cfg: PairCfg) -> None:
        self._inner = inner
        self._assets = (cfg.wti, cfg.brent)
        self._qty = {a: 0.0 for a in self._assets}
        self._entry = {a: 0.0 for a in self._assets}
        self._mid: dict[str, float] = {}
        self._quotes: dict[str, tuple] = {}
        self._fills: dict[str, float] = {}
        self._n = 0

    async def quote(self, asset, kind, qty, *a, **k):
        q = await self._inner.quote(asset, kind, qty, *a, **k)
        self._quotes[q.quote_id] = (asset, float(qty), q.bid, q.ask)
        self._mid[asset] = q.mid
        return q

    async def market(self, quote_id: str, side: str, max_slippage: float, reduce_only: bool = False,
                     prio: int = 0, deadline: Optional[float] = None) -> str:
        asset, qty, bid, ask = self._quotes.pop(quote_id)
        if asset not in self._qty:
            raise ValueError(f"paper : actif hors paire {asset}")
        held = self._qty[asset]
        sign = 1.0 if side == "buy" else -1.0
        if reduce_only and (held * sign >= 0 or qty > abs(held) + 1e-9):
            # refus plutôt que bornage : la quantité comptée par l'exécuteur reste la quantité
            # réellement exécutée (carnet PnL et carnet simulé cohérents).
            raise VarRejected(422, f"paper : reduce-only {side} {qty} au-delà de la position {held}")
        px = ask if side == "buy" else bid
        new = round(held + sign * qty, 9)
        if held == 0 or held * sign > 0:
            tot = abs(held) + qty
            self._entry[asset] = (abs(held) * self._entry[asset] + qty * px) / tot if tot else 0.0
        elif abs(new) > 1e-12 and new * held < 0:
            self._entry[asset] = px  # retournement : le reste ouvre au prix exécuté
        self._qty[asset] = 0.0 if abs(new) <= 1e-12 else new
        if self._qty[asset] == 0:
            self._entry[asset] = 0.0
        self._n += 1
        rfq = f"paper-{self._n}"
        self._fills[rfq] = px
        return rfq

    async def order_status(self, rfq, prio: int = 0, deadline: Optional[float] = None) -> dict:
        return {"status": "cleared", "price": self._fills.get(rfq)}

    async def set_leverage(self, asset, leverage) -> None:
        return None

    async def positions(self, deadline: Optional[float] = None) -> dict:
        out = {}
        for a, q in self._qty.items():
            if q:
                px = self._mid.get(a) or self._entry[a]
                out[a] = {"qty": q, "entry": self._entry[a], "liq": None, "value": q * px}
        return out

    def __getattr__(self, name):  # supported_assets, portfolio, health, limiter...
        return getattr(self._inner, name)
