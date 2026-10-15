"""Mode paire pétrole : planification des ordres et exécution avec garde d'équilibre.

La paire (WTI court / Brent long, ou l'inverse) est retournée en tranches alternées wti, brent,
pour que l'écart entre les deux jambes ne dépasse jamais une tranche. Si un ordre échoue deux fois,
on rééquilibre la jambe la plus grosse en reduce-only avant de rendre la main.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

from arb.config import PairCfg
from arb.journal import SafeJournal
from arb.var_client import VarRateLimited, VarRejected
from arb.var_limiter import HEDGE



class Step(NamedTuple):
    leg: str  # "wti" | "brent"
    side: str  # "buy" | "sell"
    usd: float
    reduce_only: bool


_LEGS = ("wti", "brent")
_FINAL_FAIL = {"rejected", "canceled", "cancelled", "failed", "expired"}
_CONFIRM_WINDOW_S = 6.0  # fenêtre de confirmation d'un ordre à l'issue incertaine
_CONFIRM_POLL_S = 0.5
_CONFIRM_POLLS = int(_CONFIRM_WINDOW_S / _CONFIRM_POLL_S)
# Fraction d'une tranche tolérée comme écart résiduel (contrôle final et rééquilibrage) : 250 $ pour
# une tranche de 2 500 $. Absorbe un mouvement relatif WTI/Brent pendant un cycle, tout en gardant
# le déséquilibre bien sous une tranche.
_BALANCE_TOL_TRANCHE = 0.1


def target_usd(side: str, leg_usd: float) -> dict[str, float]:
    """Sens A : WTI court, Brent long. Sens B : l'inverse. Montants signés en $ (positif = long)."""
    sign = 1.0 if side == "A" else -1.0
    return {"wti": -sign * leg_usd, "brent": sign * leg_usd}


def _slices(frm: float, to: float, tranche: float, reduce_only: bool) -> list[Step]:
    """Tranches (side, usd, reduce_only) pour aller de frm à to, sans leg."""
    out, pos = [], frm
    while abs(to - pos) > 1e-9:
        d = to - pos
        usd = min(tranche, abs(d))
        out.append(("buy" if d > 0 else "sell", usd, reduce_only))
        pos += usd if d > 0 else -usd
    return out


def _leg_slices(cur: float, target: float, tranche: float, reopen: bool) -> list[tuple]:
    flip = cur * target < 0
    out: list[tuple] = []
    if cur != 0 and (flip or reopen):
        out += _slices(cur, 0.0, tranche, True)
        cur = 0.0
    if cur != target:
        out += _slices(cur, target, tranche, False)
    return out


def plan_cycle(cur: dict[str, float], target: dict[str, float], tranche: float, reopen: bool) -> list[Step]:
    """Close both legs before any opening, alternating legs within each phase.

    Unequal starting positions can require different numbers of closing
    slices. Interleaving each leg's complete plan would open one leg while
    the other still holds its previous direction during a flip.
    """
    per = {leg: _leg_slices(cur[leg], target[leg], tranche, reopen) for leg in _LEGS}
    steps: list[Step] = []
    for reduce_only in (True, False):
        phase = {leg: [s for s in per[leg] if s[2] == reduce_only] for leg in _LEGS}
        for i in range(max(len(phase["wti"]), len(phase["brent"]))):
            for leg in _LEGS:
                if i < len(phase[leg]):
                    side, usd, ro = phase[leg][i]
                    steps.append(Step(leg, side, usd, ro))
    return steps


@dataclass
class Fill:
    leg: str
    side: str
    usd: float
    qty: float
    quote_px: float
    exec_px: float
    slip_bps: float
    half_spread_bps: float
    latency_ms: float
    reduce_only: bool


@dataclass
class ExecResult:
    ok: bool
    filled: list = field(default_factory=list)
    pos_delta_usd: dict = field(default_factory=lambda: {"wti": 0.0, "brent": 0.0})
    reason: Optional[str] = None
    deferred: bool = False


class _OrderFailed(Exception):
    """Ordre non exécuté ou issue inconnue : la séquence s'arrête."""


class _EntryDeferred(_OrderFailed):
    """Opening vetoed before market submission; no retry or rebalance is needed."""


class _Unknown(_OrderFailed):
    """Issue inconnue après la fenêtre de confirmation : l'ordre peut encore se régler."""

    def __init__(self, msg: str, ctx: tuple) -> None:
        super().__init__(msg)
        self.ctx = ctx  # (leg, side, qty, reduce_only, quote, t0, base)


class _Refused(_OrderFailed):
    """Échec CERTAIN (aucun ordre n'est parti ou il est confirmé non exécuté) : on peut renvoyer."""

    def __init__(self, msg: str, wait_s: float = 0.0) -> None:
        super().__init__(msg)
        self.wait_s = wait_s


class PairExecutor:
    """Règle C1 du moteur : on ne renvoie JAMAIS un ordre dont l'issue est incertaine. Dans ce cas
    (statut en attente, erreur de sondage, 5xx ou délai sur market), on confirme par les positions."""

    def __init__(self, var, cfg: PairCfg, journal, clock=time.time, sleep=asyncio.sleep) -> None:
        self.var, self.cfg, self.journal = var, cfg, SafeJournal.wrap(journal)
        self.clock, self.sleep = clock, sleep

    def _asset(self, leg: str) -> str:
        return self.cfg.wti if leg == "wti" else self.cfg.brent

    async def _read_positions(self) -> dict[str, float]:
        """Quantités signées réelles des 2 actifs de la paire (2 lectures max)."""
        last: Optional[Exception] = None
        for i in range(2):
            try:
                pos = await self.var.positions()
                return {leg: float((pos.get(self._asset(leg)) or {}).get("qty", 0.0) or 0.0) for leg in _LEGS}
            except VarRateLimited as exc:
                last = exc
                await self.sleep(max(0.0, exc.wait_s))
            except Exception as exc:
                last = exc
                await self.sleep(_CONFIRM_POLL_S)
        raise _OrderFailed(f"lecture des positions impossible : {last}")

    def _fill(self, leg, side, qty, reduce_only, q, exec_px, t0) -> Fill:
        quote_px = q.ask if side == "buy" else q.bid
        px = quote_px if exec_px is None else exec_px
        if side == "buy":
            slip = (px - quote_px) / quote_px * 1e4
        else:
            slip = (quote_px - px) / quote_px * 1e4
        half = (q.ask - q.bid) / 2 / q.mid * 1e4
        lat = (self.clock() - t0) * 1000.0
        fill = Fill(leg, side, qty * px, qty, quote_px, px, slip, half, lat, reduce_only)
        self.journal.record_pair_fill(
            self._asset(leg), "", 0.0, 0.0, 0.0, side, px, qty, -slip, 0.0, lat, "pair_fill",
            quote_mid=q.mid, quote_px=quote_px, half_spread_bps=half, slip_bps=slip,
            price_source="quote_proxy" if exec_px is None else "order_status",
            qty_source="requested_unverified", reduce_only=reduce_only)
        return fill

    async def _once(self, leg: str, side: str, qty: float, reduce_only: bool, *, entry_guard=None) -> Fill:
        asset = self._asset(leg)
        base = (await self._read_positions())[leg]  # référence avant l'envoi
        t0 = self.clock()
        try:
            q = await self.var.quote(asset, self.cfg.kind, qty, HEDGE)
        except VarRateLimited as exc:
            raise _Refused(f"cotation limitée : {exc}", exc.wait_s)
        except Exception as exc:  # rien n'est parti
            raise _Refused(f"cotation échouée : {type(exc).__name__}: {exc}")
        if not reduce_only and entry_guard is not None:
            # This check follows every awaited read/quote immediately before
            # the first opening. A definite refusal will repeat it on retry.
            try:
                veto = entry_guard()
            except Exception as exc:
                raise _EntryDeferred(f"entry guard unavailable ({type(exc).__name__})") from exc
            if veto is not None:
                raise _EntryDeferred(str(veto))
        uncertain = False
        rfq = None
        try:
            rfq = await self.var.market(q.quote_id, side, self.cfg.max_slippage, reduce_only, HEDGE)
        except VarRateLimited as exc:
            raise _Refused(f"market limité : {exc}", exc.wait_s)
        except VarRejected as exc:
            if exc.status < 500:
                raise _Refused(f"market refusé : {exc}")
            uncertain = True
        except Exception:
            uncertain = True  # 5xx, délai... : l'ordre a peut-être passé
        # Confirmation : statut (si rfq connu) et positions, pendant toute la fenêtre. Un statut final
        # d'échec autorise le renvoi ; rien de confirmé à l'échéance = issue inconnue, SANS renvoi.
        # L'ordre est peut-être parti : toute erreur imprévue (prix illisible, division par zéro...)
        # suit aussi le chemin de l'issue inconnue.
        ctx = (leg, side, qty, reduce_only, q, t0, base)
        try:
            return await self._confirm(rfq, uncertain, ctx)
        except _OrderFailed:
            raise
        except Exception as exc:
            raise _Unknown(f"erreur imprévue après l'envoi : {type(exc).__name__}: {exc}", ctx)

    async def _confirm(self, rfq, uncertain: bool, ctx: tuple) -> Fill:
        leg, side, qty, reduce_only, q, t0, base = ctx
        sign = 1.0 if side == "buy" else -1.0
        for _ in range(_CONFIRM_POLLS):
            if rfq is not None:
                try:
                    st = await self.var.order_status(rfq, prio=HEDGE)
                except VarRateLimited as exc:
                    await self.sleep(max(0.0, exc.wait_s))
                    st = None
                except Exception:
                    st = None
                status = (st or {}).get("status")
                if status == "cleared":
                    price = st.get("price")
                    return self._fill(leg, side, qty, reduce_only, q,
                                      float(price) if price not in (None, "") else None, t0)
                if status in _FINAL_FAIL:
                    raise _Refused(f"statut {status} pour {rfq}")
            if uncertain or rfq is not None:
                try:
                    cur = (await self._read_positions())[leg]
                except _OrderFailed:
                    cur = None
                if cur is not None and (cur - base) * sign >= 0.5 * qty:
                    return self._fill(leg, side, qty, reduce_only, q, None, t0)
            await self.sleep(_CONFIRM_POLL_S)
        raise _Unknown(f"issue inconnue après {_CONFIRM_WINDOW_S:.0f} s (aucun statut final, position inchangée)",
                       ctx)

    async def _order(self, leg: str, side: str, qty: float, reduce_only: bool, *, entry_guard=None) -> Fill:
        last: Optional[_Refused] = None
        for i in range(2):
            try:
                if entry_guard is None:
                    return await self._once(leg, side, qty, reduce_only)
                return await self._once(leg, side, qty, reduce_only, entry_guard=entry_guard)
            except _Refused as exc:
                last = exc
                if i == 0:
                    await self.sleep(max(0.0, exc.wait_s))
        raise _OrderFailed(str(last))

    @staticmethod
    def _apply(res: ExecResult, f: Fill) -> None:
        res.filled.append(f)
        res.pos_delta_usd[f.leg] += f.qty * f.exec_px * (1 if f.side == "buy" else -1)

    @staticmethod
    def _close_quantities(steps: list[Step], held: dict[str, float]) -> dict[int, float]:
        """Quantité de chaque étape reduce-only : la quantité détenue est répartie entre les tranches
        de fermeture de la jambe, la dernière prend le reste exact (fin à 0 pile)."""
        out: dict[int, float] = {}
        for leg in _LEGS:
            idx = [i for i, s in enumerate(steps) if s.leg == leg and s.reduce_only]
            total, used = abs(held[leg]), 0.0
            for n, i in enumerate(idx):
                if n == len(idx) - 1:
                    out[i] = round(total - used, 9)
                else:
                    out[i] = round(total / len(idx), 3)
                    used += out[i]
        return out

    async def run_steps(self, steps: list[Step], prices: dict[str, float], *, entry_guard=None) -> ExecResult:
        """Exécute les étapes. Les positions de départ sont lues sur Var : les fermetures reduce-only
        sont exécutées en quantité, les ouvertures en usd / prix.

        entry_guard synchronously returns None to allow the first opening or
        a reason string to defer it. Once opening has begun, finish its hedge
        phase under the existing execution safety rules.
        """
        res = ExecResult(ok=True)
        opening_started = False
        steps = [Step(*s) for s in steps]
        try:
            held = await self._read_positions()
        except _OrderFailed as exc:
            res.ok, res.reason = False, str(exc)
            return res
        closes = self._close_quantities(steps, held)
        for i, (leg, side, usd, ro) in enumerate(steps):
            qty = closes[i] if ro else round(usd / prices[leg], 3)
            if qty <= 0:
                continue
            if i:
                await self.sleep(self.cfg.order_gap_s)
            try:
                if entry_guard is not None and not ro and not opening_started:
                    f = await self._order(leg, side, qty, ro, entry_guard=entry_guard)
                else:
                    f = await self._order(leg, side, qty, ro)
            except _EntryDeferred as exc:
                res.ok, res.deferred, res.reason = False, True, str(exc)
                return res
            except _Unknown as exc:
                res.ok = False
                res.reason = (f"déséquilibre possible : issue inconnue pour l'ordre {i + 1}/{len(steps)} "
                              f"{leg} {side} {usd:.0f} $ ({exc})")
                await self._second_window(res, exc.ctx)
                await self._rebalance(res)
                return res
            except _OrderFailed as exc:
                res.ok = False
                res.reason = f"ordre {i + 1}/{len(steps)} {leg} {side} {usd:.0f} $ échoué : {exc}"
                await self._rebalance(res)
                return res
            except Exception as exc:  # filet : un ordre a peut-être passé
                res.ok = False
                res.reason = (f"déséquilibre possible : erreur imprévue sur l'ordre {i + 1}/{len(steps)} "
                              f"{leg} {side} {usd:.0f} $ ({type(exc).__name__}: {exc})")
                await self._rebalance(res)
                return res
            self._apply(res, f)
            if not ro:
                opening_started = True
        # contrôle final : jamais ok=True avec une paire déséquilibrée (remplissage partiel...)
        try:
            gap = self._gap_usd(await self._valued_positions())
        except Exception as exc:
            res.ok, res.reason = False, f"déséquilibre : contrôle final impossible ({exc})"
            return res
        if abs(gap) > _BALANCE_TOL_TRANCHE * self.cfg.tranche_usd:
            res.ok, res.reason = False, f"écart final de {gap:.0f} $ entre les jambes"
            await self._rebalance(res)
        return res

    async def _second_window(self, res: ExecResult, ctx: tuple) -> None:
        """Seconde fenêtre avant tout rééquilibrage : l'ordre incertain peut encore se régler, et
        rééquilibrer trop tôt laisserait une jambe seule. S'il se confirme, il est compté."""
        leg, side, qty, ro, q, t0, base = ctx
        sign = 1.0 if side == "buy" else -1.0
        for _ in range(_CONFIRM_POLLS):
            await self.sleep(_CONFIRM_POLL_S)
            try:
                cur = (await self._read_positions())[leg]
            except _OrderFailed:
                continue
            if (cur - base) * sign >= 0.5 * qty:
                try:
                    self._apply(res, self._fill(leg, side, qty, ro, q, None, t0))
                except Exception:
                    pass  # ordre confirmé mais non valorisable : la réconciliation recale le suivi
                return

    async def _valued_positions(self) -> dict[str, tuple[float, float]]:
        """Par jambe : (quantité signée, prix courant). Prix = value/qty de positions() si dispo,
        sinon mid d'une cotation fraîche."""
        raw = None
        for i in range(2):
            try:
                raw = await self.var.positions()
                break
            except VarRateLimited as exc:
                if i:
                    raise _OrderFailed(f"lecture des positions impossible : {exc}")
                await self.sleep(max(0.0, exc.wait_s))
            except Exception as exc:
                raise _OrderFailed(f"lecture des positions impossible : {exc}")
        out = {}
        for leg in _LEGS:
            p = raw.get(self._asset(leg)) or {}
            qty = float(p.get("qty", 0.0) or 0.0)
            value = float(p.get("value", 0.0) or 0.0)
            if qty == 0:
                out[leg] = (0.0, 0.0)
            elif value:
                out[leg] = (qty, abs(value) / abs(qty))
            else:
                try:
                    q = await self.var.quote(self._asset(leg), self.cfg.kind, abs(qty), HEDGE)
                except Exception as exc:
                    raise _OrderFailed(f"cotation de valorisation impossible : {exc}")
                out[leg] = (qty, q.mid)
        return out

    @staticmethod
    def _gap_usd(v: dict[str, tuple[float, float]]) -> float:
        return abs(v["wti"][0]) * v["wti"][1] - abs(v["brent"][0]) * v["brent"][1]

    async def _rebalance(self, res: ExecResult) -> None:
        """Depuis les positions RÉELLES, valorisées au prix courant : reduce-only en quantité sur
        la plus grosse jambe, puis relecture de l'écart."""
        tol = _BALANCE_TOL_TRANCHE * self.cfg.tranche_usd
        orig = res.reason
        try:
            v = await self._valued_positions()
            gap = self._gap_usd(v)
            if abs(gap) <= tol:
                return
            leg = "wti" if gap > 0 else "brent"
            qty_pos, px = v[leg]
            qty = min(round(abs(gap) / px, 3), abs(qty_pos))
            side = "sell" if qty_pos > 0 else "buy"
            self._apply(res, await self._order(leg, side, qty, True))
            gap = self._gap_usd(await self._valued_positions())
            if abs(gap) > tol:
                raise _OrderFailed(f"écart résiduel {gap:.0f} $")
        except Exception as exc:
            res.reason = f"déséquilibre : rééquilibrage échoué ({exc}) après {orig}"
