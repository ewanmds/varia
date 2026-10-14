from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from arb.config import ENTRY_MODES, GlobalCfg, MarketCfg
from arb.journal import SafeJournal
from arb.pricing import (VarQuote, chase_margins_bps, edge_bps, half_spread_rel, inventory_ratio, maker_prices,
                         round_px, round_sz)
from arb.var_client import VarError, VarRateLimited
from arb.var_errors import VarBudgetExceeded, VarRejected
from arb.var_limiter import EXIT, HEDGE

QUOTING, HEDGING, PAUSED, BLOCKED, EXITING, IDLE = "cote", "couverture", "pause", "bloqué", "sortie", "arrêt"

CANCEL_ONLY_REASON = "limite de débit HL : annulation seule"

# C1c : backoff par côté après une erreur de placement.
_BACKOFF_MIN_S = 1.0
_BACKOFF_MAX_S = 30.0
# N1 : après ce nombre d'échecs d'annulation consécutifs, relire les ordres ouverts du coin.
_CANCEL_CHECK_AFTER = 3

# Statuts finaux Var. "cleared" = succès ; tout le reste ci-dessous = échec définitif du rfq
# (un statut absent ou non listé ici est traité comme "en attente").
_VAR_FINAL_FAIL = {"rejected", "canceled", "cancelled", "failed", "expired"}

# Âge au-delà duquel un fill orphelin (oid pas encore enregistré par ce moteur) est abandonné
# plutôt que rejoué quand l'oid finit par être connu.
_ORPHAN_MAX_AGE_S = 10.0


class HedgeGate:
    """Verrou partagé entre tous les MarketEngine : une seule couverture (ou sortie) Var en vol
    à la fois, tous marchés confondus. L'orchestrateur doit instancier UN SEUL HedgeGate et le
    passer à chaque MarketEngine."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()


class MarketEngine:
    def __init__(self, cfg: MarketCfg, g: GlobalCfg, hl, var, journal, gate: HedgeGate,
                 sz_decimals: int, fee_bps: float, taker_fee_bps: Optional[float] = None) -> None:
        self.cfg, self.g, self.hl, self.var, self.gate = cfg, g, hl, var, gate
        # C2 : aucune écriture journal ne doit pouvoir interrompre une couverture ou une sortie.
        self.journal = SafeJournal.wrap(journal)
        self.sz_decimals = sz_decimals
        self.fee_bps = fee_bps
        # I8 : frais taker HL estimés (sorties, corrections) ; à défaut 3 fois le maker.
        self.taker_fee_bps = taker_fee_bps if taker_fee_bps is not None else 3.0 * fee_bps
        self.state = QUOTING
        self.pos_hl = 0.0
        self.pos_var = 0.0
        self.orders: dict[str, Optional[dict]] = {"buy": None, "sell": None}
        self.pending_qty = 0.0
        self._pending_px = 0.0
        self._pending_t = 0.0
        self.last_reason: Optional[str] = None
        self.hedge_inflight = False
        # meilleur bid/ask HL connu, mis à jour par l'orchestrateur à chaque tick (I3) ; sert de
        # repli de prix pour l'IOC d'urgence d'un débouclage.
        self.book: tuple[Optional[float], Optional[float]] = (None, None)
        # oids de tous les ordres HL placés par ce moteur (actifs ou annulés récemment) : un fill
        # sur l'un d'eux doit toujours être pris en compte, même si l'ordre n'est plus "actif" (C3).
        self._my_oids: set = set()
        # fills reçus pour un oid pas encore dans self._my_oids (arrivés avant l'écriture de
        # place_alo() dans self.orders/_my_oids) : mis en tampon, rejoués quand l'oid est enregistré.
        self._orphan_fills: dict = {}
        # fenêtre de confirmation par relecture des positions Var avant de considérer un rfq
        # bloqué comme réellement en échec (C1). Exposée comme attribut pour pouvoir être réduite
        # dans les tests.
        self._confirm_window_s = 3.0
        # budget réservé, à la fin du délai global de couverture, pour une dernière lecture des
        # positions Var avant tout débouclage (C1a). Exposé pour les tests.
        self._final_read_reserve_s = 1.0
        # I6 : intervalle de sondage de order_status (Var), 1 s pour ménager la limite de débit.
        self._status_poll_s = 1.0
        # vrai si le dernier hedge() réussi a dû utiliser le prix de la cotation (faute de prix
        # confirmé par le statut ou par la relecture des positions) plutôt qu'un prix réalisé.
        self._hedge_px_is_quote = False
        # quantité (signée) actuellement "réclamée" par une couverture en vol : sert à ce que les
        # fills arrivant pendant cette couverture calculent leur propre prix moyen/horodatage
        # plutôt que de se mélanger au lot déjà capturé par hedge() (N4).
        self._claim_qty = 0.0
        # C1 : horloge monotone (surchargée dans les tests), horodatage du dernier placement réussi
        # et backoff par côté après une erreur de placement (1 s doublé jusqu'à 30 s).
        self._mono = time.monotonic
        self._last_place: dict[str, float] = {"buy": -1e18, "sell": -1e18}
        self._backoff_s: dict[str, float] = {"buy": 0.0, "sell": 0.0}
        self._backoff_until: dict[str, float] = {"buy": 0.0, "sell": 0.0}
        # C1d : vrai quand la marge de requêtes HL est trop basse (fourni par l'orchestrateur) :
        # plus aucun placement ni modify, seules les annulations passent.
        self.cancel_only_fn: Callable[[], bool] = lambda: False
        # I4 : vrai quand l'orchestrateur est en arrêt d'urgence (fourni par l'orchestrateur).
        self.kill_fn: Callable[[], bool] = lambda: False
        # L7 : vrai dès qu'un silence HL est détecté (fourni par l'orchestrateur) : plus aucun
        # placement ni modify, avant même l'annulation globale.
        self.halt_fn: Callable[[], bool] = lambda: False
        # I2 : côtés dont la dernière annulation n'a pas été confirmée par HL (ordre gardé,
        # nouvel essai au tick suivant via retry_pending_cancels()).
        self._cancel_failed: set = set()
        # N1 : backoff par côté des nouvelles tentatives d'annulation (1 s doublé jusqu'à 30 s) et
        # nombre d'échecs consécutifs ; au-delà de _CANCEL_CHECK_AFTER, relecture du carnet.
        self._cancel_backoff_s: dict[str, float] = {"buy": 0.0, "sell": 0.0}
        self._cancel_retry_at: dict[str, float] = {"buy": 0.0, "sell": 0.0}
        self._cancel_fails: dict[str, int] = {"buy": 0, "sell": 0}
        # L3 (run réel du 23/09) : vrai pendant une sortie taker (attente du verrou comprise) :
        # une 2e sortie demandée entre-temps est ignorée.
        self.exit_inflight = False
        # L3 : instant (time.monotonic) du dernier IOC de sortie HL, et début de la dernière
        # relecture des positions appliquée à ce moteur par la réconciliation. Tant que la
        # relecture est antérieure à l'IOC, l'orchestrateur ne relance pas de sortie.
        self.exit_ioc_at: Optional[float] = None
        self.positions_read_at: float = 0.0
        # L6 : cause de la dernière lecture des positions Var en échec.
        self._last_read_err = ""
        # Mode chase : instant (self._mono) de la pose de l'ordre suivi par côté (durée de vie,
        # non remise à zéro par un modify), inefficience vue au tick précédent, et compteurs.
        self._chase_born: dict[str, float] = {"buy": 0.0, "sell": 0.0}
        self._chase_seen: dict[str, bool] = {"buy": False, "sell": False}
        # Revue : pas de nouvelle pose avant cet instant (self._mono), par côté, après une annulation.
        self._chase_cooldown_until: dict[str, float] = {"buy": 0.0, "sell": 0.0}
        self.chase_stats = {"detections": 0, "place_attempts": 0, "placed": 0, "modified": 0, "cancelled": 0,
                            "fresh_requests": 0, "not_confirmed": 0, "stale_cancels": 0}
        # F1-F5 (docs/superpowers/fresh-quote-2026-09-23.md). Horloge murale (même échelle que
        # VarQuote.ts, surchargée dans les tests).
        self._wall = time.time
        # F1 : demande d'une cotation Var fraîche pour ce marché (fournie par l'orchestrateur :
        # VarFeed.request_fresh), et fin de la fenêtre pendant laquelle on n'en redemande pas.
        self.fresh_quote_fn: Callable[[], None] = lambda: None
        self._fresh_req_until = -1e18
        # F1 : horodatage (q.ts) de la cotation qui a détecté l'inefficience, par côté ; une pose
        # n'a lieu que sur une cotation strictement plus récente. None = pas de détection en cours.
        self._detect_qts: dict[str, Optional[float]] = {"buy": None, "sell": None}
        # F2 : dernier instant (self._mono) où une inefficience a été vue (marché « chaud »).
        self._chase_seen_at = -1e18
        # F3 : vrai si la dernière tentative de cotation du VarFeed pour ce marché a échoué.
        self.quote_refresh_failed_fn: Callable[[], bool] = lambda: False
        # F4/F5 : contexte de décision (âge de la cotation, prix Var de référence brut, basis) de
        # chaque ordre chase, par oid ; celui à attacher au prochain ordre placé ; celui du lot de
        # fills en attente de couverture ; dernier basis vu en chase (repli).
        self._ctx_by_oid: dict = {}
        self._next_ctx: Optional[dict] = None
        self._pending_ctx: Optional[dict] = None
        self._last_basis = 0.0
        # Écart courant au meilleur prix du carnet HL (pb, net des frais maker et de la réserve,
        # donc comparable à min_edge_bps), meilleur des deux côtés, pour l'interface.
        self.edge_now_bps: Optional[float] = None
        self.edge_now_side: Optional[str] = None
        # Étape d'arrêt en cours (affichage seulement), fournie par l'orchestrateur pendant un kill.
        self.progress_fn: Callable[[Optional[str]], None] = lambda step: None
        # L4 (levers-2026-09-23) : mode lag_taker. Vrai en mode paper (fourni par l'orchestrateur) :
        # un IOC paper ne produit aucun fill userFills, le fill est alors simulé ici.
        self.paper = False
        # instant (self._mono) de la dernière prise, inefficience vue au tick précédent (par côté),
        # compteurs et oid fictifs des fills paper.
        self._lag_last_take = -1e18
        self._lag_seen: dict[str, bool] = {"buy": False, "sell": False}
        self._paper_oid = 0
        self.lag_stats = {"signals": 0, "taken": 0, "filled": 0, "no_fill": 0, "errors": 0,
                          "skipped_age": 0, "skipped_ws_stale": 0, "skipped_spread": 0,
                          "skipped_basis": 0, "skipped_move": 0, "skipped_size": 0, "skipped_interval": 0,
                          "filled_usd": 0.0, "hedged": 0, "expected_bps_sum": 0.0, "realized_bps_sum": 0.0}

    # -- état ------------------------------------------------------------
    def pos_usd(self, mid: float) -> float:
        return self.pos_hl * mid

    async def pause(self, reason: str) -> None:
        # I2/N3 : un marché déjà BLOQUÉ ne doit jamais être rétrogradé en PAUSE.
        if self.state == BLOCKED:
            return
        self.state, self.last_reason = PAUSED, reason
        # r3-5 : une annulation qui lève ici ne doit jamais s'échapper (on est parfois appelé
        # depuis le `finally` implicite d'un flux de couverture déjà en train de gérer une
        # erreur ; laisser fuir casserait l'appelant).
        try:
            await self.cancel_all()
        except Exception as exc:
            self.journal.record_incident(self.cfg.var_asset, "warn",
                                         f"annulation HL lors de la pause échouée : {type(exc).__name__}: {exc}")

    async def block(self, reason: str) -> None:
        self.state, self.last_reason = BLOCKED, reason
        try:
            await self.cancel_all()
        except Exception as exc:
            self.journal.record_incident(self.cfg.var_asset, "warn",
                                         f"annulation HL lors du blocage échouée : {type(exc).__name__}: {exc}")
        self.journal.record_incident(self.cfg.var_asset, "error", f"bloqué : {reason}")

    def resume(self) -> None:
        if self.state in (PAUSED, BLOCKED, IDLE):
            self.state, self.last_reason = QUOTING, None

    # -- ordres ----------------------------------------------------------
    async def _cancel_side(self, side: str, cooldown: bool = True) -> None:
        o = self.orders.get(side)
        if o is None:
            self._cancel_failed.discard(side)
            return
        try:
            ok = await self.hl.cancel(self.cfg.hl_coin, o["oid"])
        except Exception:
            # I2 : l'ordre est peut-être encore au carnet : le garder et réessayer plus tard.
            self._note_cancel_failure(side)
            raise
        if ok:
            self.orders[side] = None
            self._reset_cancel_state(side)
            if cooldown and self.cfg.entry_mode == "chase":
                # revue : toute annulation (écart refermé, cotation périmée, durée de vie,
                # blocage, couverture, pause...) retarde la prochaine pose de ce côté.
                self._chase_cooldown_until[side] = self._mono() + self.cfg.chase_replace_cooldown_s
            return
        # I2 : annulation non confirmée -> on GARDE l'ordre (il est peut-être encore au carnet) et
        # on réessaie plus tard (N1 : avec backoff). Un seul incident par ordre.
        if side not in self._cancel_failed:
            self.journal.record_incident(self.cfg.var_asset, "warn",
                                         f"annulation HL non confirmée (oid={o['oid']}), nouvel essai avec backoff")
        self._note_cancel_failure(side)

    def _note_cancel_failure(self, side: str) -> None:
        self._cancel_failed.add(side)
        self._cancel_fails[side] += 1
        prev = self._cancel_backoff_s[side]
        self._cancel_backoff_s[side] = _BACKOFF_MIN_S if prev <= 0 else min(_BACKOFF_MAX_S, prev * 2)
        self._cancel_retry_at[side] = self._mono() + self._cancel_backoff_s[side]

    def _reset_cancel_state(self, side: str) -> None:
        self._cancel_failed.discard(side)
        self._cancel_fails[side] = 0
        self._cancel_backoff_s[side] = 0.0
        self._cancel_retry_at[side] = 0.0

    async def _open_orders_on_coin(self) -> list:
        orders = await self.hl.open_orders()
        return [o for o in (orders or []) if o.get("coin") == self.cfg.hl_coin]

    async def cancel_all(self) -> None:
        await self._cancel_side("buy")
        await self._cancel_side("sell")

    async def retry_pending_cancels(self) -> None:
        """I2 : réessayer les annulations non confirmées, quel que soit l'état du marché. N1 :
        backoff par côté (1 s doublé jusqu'à 30 s) ; après 3 échecs, relire les ordres ouverts du
        coin et vider le slot si l'ordre n'y est plus."""
        for side in list(self._cancel_failed):
            if self._mono() < self._cancel_retry_at[side]:
                continue
            o = self.orders.get(side)
            if o is not None and self._cancel_fails[side] >= _CANCEL_CHECK_AFTER:
                try:
                    live = {int(x.get("oid", 0)) for x in await self._open_orders_on_coin()}
                except Exception as exc:
                    self.journal.record_incident(self.cfg.var_asset, "warn",
                                                 f"lecture des ordres ouverts HL impossible : {exc}")
                    live = None
                if live is not None and int(o["oid"]) not in live:
                    self.journal.record_incident(self.cfg.var_asset, "warn",
                                                 f"ordre HL {o['oid']} absent du carnet : slot libéré")
                    self.orders[side] = None
                    self._reset_cancel_state(side)
                    continue
            try:
                await self._cancel_side(side)
            except Exception as exc:
                self.journal.record_incident(self.cfg.var_asset, "warn",
                                             f"nouvel essai d'annulation HL échoué : {type(exc).__name__}: {exc}")

    def clear_orders(self) -> None:
        """Vide le carnet d'ordres connus de ce moteur SANS repasser par l'exchange (fix r1 finding
        1) : utilisé après une annulation globale déclenchée ailleurs (ex. `hl.cancel_all()` sur
        flux HL muet, déjà fait au niveau exchange). Les oids restent dans `_my_oids` : un fill
        tardif sur un ordre qu'on vient d'oublier ici doit rester pris en compte (C3)."""
        self.orders = {"buy": None, "sell": None}
        for side in ("buy", "sell"):
            self._reset_cancel_state(side)

    async def _replay_orphans(self, oid) -> None:
        fills = self._orphan_fills.pop(oid, [])
        if not fills:
            return
        now = time.time()
        for ts, fill in fills:
            if now - ts > _ORPHAN_MAX_AGE_S:
                self.journal.record_incident(self.cfg.var_asset, "warn",
                                             f"fill orphelin abandonné (oid={oid}, âge={now - ts:.1f}s)")
                continue
            await self.on_fill(fill)

    def _aborted(self) -> bool:
        """I4 : vrai si, après un `await`, ce moteur ne doit plus avoir d'ordre au carnet."""
        return self.state != QUOTING or bool(self.kill_fn()) or bool(self.halt_fn())

    def _too_aggressive(self, side: str, cur_px: float, target_px: float, q: VarQuote, basis: float) -> bool:
        """C1a : l'ordre au repos est trop agressif si son écart attendu contre la cotation Var
        courante est sous min_edge_bps. Seuil plafonné par l'écart de la cible elle-même : le côté
        qui réduit l'inventaire vise volontairement moins que min_edge_bps (exit_edge_bps), et un
        ordre posé exactement à la cible ne doit pas être reposé à chaque tick."""
        is_buy = side == "buy"
        var_ref = (q.bid if is_buy else q.ask) + basis
        e_cur = edge_bps(is_buy, cur_px, var_ref, self.fee_bps)
        e_tgt = edge_bps(is_buy, target_px, var_ref, self.fee_bps)
        return e_cur < min(self.cfg.min_edge_bps, e_tgt) - 1e-9

    def _bump_backoff(self, side: str, err) -> None:
        prev = self._backoff_s[side]
        self._backoff_s[side] = _BACKOFF_MIN_S if prev <= 0 else min(_BACKOFF_MAX_S, prev * 2)
        self._backoff_until[side] = self._mono() + self._backoff_s[side]

    def _placed(self, side: str, oid, px: float, sz: float) -> None:
        self.orders[side] = {"oid": oid, "px": px, "sz": sz}
        self._my_oids.add(oid)
        if self._next_ctx is not None:
            # F4 : contexte de la décision de pose ou de déplacement de cet ordre.
            self._ctx_by_oid[oid] = self._next_ctx
            while len(self._ctx_by_oid) > 64:
                self._ctx_by_oid.pop(next(iter(self._ctx_by_oid)))
        self._last_place[side] = self._mono()
        self._backoff_s[side] = 0.0
        self._backoff_until[side] = 0.0
        # N5 : un nouvel ordre suivi remplace l'ancien : plus d'annulation en attente sur ce côté.
        self._reset_cancel_state(side)

    async def _place(self, side: str, sz: float, px: float) -> None:
        try:
            res = await self.hl.place_alo(self.cfg.hl_coin, side == "buy", sz, px)
        except Exception as exc:
            res = {"oid": None, "error": f"{type(exc).__name__}: {exc}"}
        if res.get("oid") and not res.get("error"):
            self._placed(side, res["oid"], px, sz)
            if self._aborted():
                # I4 : l'état a changé (ou kill) pendant le placement : retirer l'ordre aussitôt.
                await self._cancel_side(side)
            # rejouer les fills arrivés (WS) avant que cet oid soit connu du moteur.
            await self._replay_orphans(res["oid"])
        else:
            self._bump_backoff(side, res.get("error"))

    async def _cancel_untracked(self) -> None:
        """N4 : après un modify à l'issue inconnue, annuler tout ordre de ce coin que le moteur ne
        suit pas (l'ordre modifié a pu être reposé sous un oid qu'on ne connaît pas)."""
        tracked = {int(o["oid"]) for o in self.orders.values() if o is not None}
        try:
            live = await self._open_orders_on_coin()
        except Exception as exc:
            self.journal.record_incident(self.cfg.var_asset, "warn",
                                         f"modify incertain, lecture des ordres ouverts impossible : {exc}")
            return
        for o in live:
            oid = int(o.get("oid", 0))
            if oid in tracked:
                continue
            self._my_oids.add(oid)   # un fill tardif sur cet ordre reste pris en compte (C3)
            try:
                await self.hl.cancel(self.cfg.hl_coin, oid)
            except Exception as exc:
                self.journal.record_incident(self.cfg.var_asset, "warn",
                                             f"annulation d'un ordre HL non suivi ({oid}) échouée : {exc}")

    async def _modify(self, side: str, cur: dict, sz: float, px: float) -> bool:
        """C1b : déplace l'ordre existant en une action. Faux si HL refuse ou si l'issue est
        inconnue (repli cancel+place ; N4 : dans le second cas, ordres non suivis annulés avant)."""
        modify = getattr(self.hl, "modify", None)
        if modify is None:
            return False
        try:
            res = await modify(self.cfg.hl_coin, cur["oid"], side == "buy", sz, px)
        except Exception as exc:
            self.journal.record_incident(self.cfg.var_asset, "warn",
                                         f"modify HL incertain ({type(exc).__name__}: {exc}), vérification du carnet")
            await self._cancel_untracked()
            return False
        if res.get("error"):
            return False
        oid = res.get("oid")
        if not oid:
            # N4 : ni erreur ni oid au repos : état inconnu.
            self.journal.record_incident(self.cfg.var_asset, "warn", "modify HL sans oid, vérification du carnet")
            await self._cancel_untracked()
            return False
        # N4 : oid éventuellement différent : c'est lui qu'on suit désormais (_placed l'ajoute à
        # _my_oids ; l'ancien y reste pour ses fills tardifs).
        self._placed(side, oid, px, sz)
        if self._aborted():
            await self._cancel_side(side)
        await self._replay_orphans(oid)
        return True

    async def _requote_side(self, side: str, px: Optional[float], sz: float, q: VarQuote, basis: float,
                            cancel_only: bool) -> None:
        cur = self.orders.get(side)
        if px is None or sz <= 0:
            await self._cancel_side(side)
            return
        now = self._mono()
        held = cancel_only or now < self._backoff_until[side]
        if cur is None:
            if not held:
                await self._place(side, sz, px)
            return
        if self._too_aggressive(side, cur["px"], px, q, basis):
            if held:
                # pas le droit de reposer : au moins retirer l'ordre devenu dangereux.
                await self._cancel_side(side)
                return
        else:
            passive_bps = ((px - cur["px"]) if side == "buy" else (cur["px"] - px)) / px * 1e4
            if passive_bps < self.cfg.reprice_passive_bps:
                return
            if now - self._last_place[side] < self.g.min_reprice_interval_s:
                return
            if held:
                return
        if await self._modify(side, cur, sz, px):
            return
        # repli : annuler puis reposer, seulement si l'annulation est confirmée (I2).
        await self._cancel_side(side)
        if self.orders.get(side) is not None:
            self._bump_backoff(side, "annulation non confirmée")
            return
        if self._aborted():
            return
        await self._place(side, sz, px)

    def _update_edge_now(self, q: VarQuote, basis: float, hl_bid, hl_ask) -> None:
        """Écart courant au best du carnet HL, net des frais maker et de la réserve de glissement
        (affichage : l'inefficience est là quand il atteint min_edge_bps)."""
        cost = self.fee_bps + self.cfg.slip_reserve_bps
        best, side = None, None
        vb, va = q.bid + basis, q.ask + basis
        if hl_bid and vb > 0:
            best, side = (vb - hl_bid) / vb * 1e4 - cost, "buy"
        if hl_ask and va > 0:
            e = (hl_ask - va) / va * 1e4 - cost
            if best is None or e > best:
                best, side = e, "sell"
        self.edge_now_bps, self.edge_now_side = best, side

    async def requote(self, q: VarQuote, basis: float, hl_bid, hl_ask, wide_mult: float,
                      block_reason: Optional[str], hold: bool = False,
                      rfq: Optional[VarQuote] = None, lag: Optional[dict] = None) -> None:
        """`q` : référence Var de décision (L2 : prix websocket ± demi-spread RFQ quand il est
        frais, sinon la cotation RFQ). `rfq` : dernière cotation RFQ du marché (défaut : `q`),
        qui porte la confirmation F1 du chase."""
        if self.state != QUOTING:
            return
        self._update_edge_now(q, basis, hl_bid, hl_ask)
        if self.cfg.entry_mode == "chase":
            await self._requote_chase(q, basis, hl_bid, hl_ask, wide_mult, block_reason, rfq or q)
            return
        if self.cfg.entry_mode == "lag_taker":
            await self._requote_lag(q, basis, hl_bid, hl_ask, wide_mult, block_reason, lag)
            return
        if block_reason is not None:
            self.last_reason = block_reason
            # L8 : `hold` = cotation Var seulement périmée (et encore sous quote_cancel_age_s) :
            # les ordres au repos restent, on s'abstient simplement d'en poser ou d'en déplacer.
            if not hold:
                await self.cancel_all()
            return
        cancel_only = bool(self.cancel_only_fn())
        self.last_reason = CANCEL_ONLY_REASON if cancel_only else None
        mid = q.mid + basis
        buy_px, sell_px = maker_prices(q, basis, self.cfg, self.fee_bps, self.pos_usd(mid), wide_mult,
                                       hl_bid, hl_ask, self.sz_decimals)
        sz = round_sz(self.cfg.fill_usd / mid, self.sz_decimals)
        for side, px in (("buy", buy_px), ("sell", sell_px)):
            if self._aborted():
                return
            await self._requote_side(side, px, sz, q, basis, cancel_only)

    async def set_entry_mode(self, mode: str) -> None:
        """L5 : changement de mode d'entrée à chaud. Les ordres au repos de l'ancien mode sont
        annulés et l'état propre au chase est remis à zéro."""
        if mode not in ENTRY_MODES:
            raise ValueError(f"mode d'entrée invalide : {mode}")
        if mode == self.cfg.entry_mode:
            return
        self.cfg.entry_mode = mode
        self._chase_seen = {"buy": False, "sell": False}
        self._detect_qts = {"buy": None, "sell": None}
        self._lag_seen = {"buy": False, "sell": False}
        await self.cancel_all()

    def _cap_loading(self, sizes: dict, px: float) -> dict:
        """Revue : un côté qui charge (qui augmente |position|) n'est permis que si
        |position| + taille x prix reste sous max_position_usd ; sinon taille 0."""
        room = self.cfg.max_position_usd - abs(self.pos_hl) * px
        for side in ("buy", "sell"):
            loading = self.pos_hl >= 0 if side == "buy" else self.pos_hl <= 0
            if loading and sizes[side] * px > room + 1e-9:
                sizes[side] = 0.0
        return sizes

    # -- mode lag_taker (L4) ---------------------------------------------------
    async def _requote_lag(self, q: VarQuote, basis: float, hl_bid, hl_ask, wide_mult: float,
                           block_reason: Optional[str], lag: Optional[dict]) -> None:
        """Prise en IOC sur HL quand le prix websocket Var a bougé et que le meilleur prix HL
        n'a pas suivi (docs/superpowers/levers-2026-09-23.md, L4). Aucun ordre au repos."""
        for side in ("buy", "sell"):
            if self.orders.get(side) is not None:
                # reste d'un autre mode (changement à chaud) : ce mode ne laisse rien au carnet.
                await self._cancel_side(side, cooldown=False)
        if block_reason is not None:
            self.last_reason = block_reason
            self._lag_seen = {"buy": False, "sell": False}
            return
        if not (self.halt_fn() or self.kill_fn()):
            # revue : un reliquat sous min_hedge_usd n'attend jamais indéfiniment.
            await self._settle_lag_dust()
        cancel_only = bool(self.cancel_only_fn())
        self.last_reason = CANCEL_ONLY_REASON if cancel_only else None
        cfg = self.cfg
        fair = q.mid + basis
        # revue : demi-spread avec marge (max(dernier, médiane récente) x 1,5), fourni par
        # l'orchestrateur ; à défaut celui de la référence.
        hs_bps = (lag or {}).get("hs_bps")
        if hs_bps is None:
            hs_bps = half_spread_rel(q) * 1e4
        ratio = inventory_ratio(self.pos_usd(fair), cfg.max_position_usd)
        # seuil = min_edge (ou exit_edge côté qui réduit) + frais taker + demi-spread Var + réserve,
        # avec le même biais d'inventaire que le chase.
        m_buy, m_sell = chase_margins_bps(cfg, self.taker_fee_bps + hs_bps, ratio, wide_mult)
        sz = round_sz(cfg.fill_usd / fair, self.sz_decimals) if fair > 0 else 0.0
        reduce = round_sz(abs(self.pos_hl), self.sz_decimals)
        sizes = {"buy": min(sz, reduce) if self.pos_hl < 0 else sz,
                 "sell": min(sz, reduce) if self.pos_hl > 0 else sz}
        sizes = self._cap_loading(sizes, fair)
        for side, margin, best in (("buy", m_buy, hl_ask), ("sell", m_sell, hl_bid)):
            is_buy = side == "buy"
            gap = None
            if margin is not None and best and fair > 0 and sizes[side] > 0:
                gap = (fair - best) / best * 1e4 if is_buy else (best - fair) / fair * 1e4
            seen = gap is not None and gap >= margin
            if seen and not self._lag_seen[side]:
                self.lag_stats["signals"] += 1
            self._lag_seen[side] = seen
            if not seen or cancel_only or self._aborted():
                continue
            if await self._lag_take(side, sizes[side], best, q, basis, gap, margin, hs_bps, lag):
                return

    async def _lag_take(self, side: str, sz: float, best: float, q: VarQuote, basis: float, gap: float,
                        margin: float, hs_bps: float, lag: Optional[dict]) -> bool:
        """Garde-fous puis IOC au meilleur prix HL. Vrai si un IOC est parti."""
        cfg = self.cfg
        is_buy = side == "buy"
        lag = lag or {}
        ws_age, bbo_age = lag.get("ws_age"), lag.get("bbo_age")
        if (lag.get("src") != "ws" or ws_age is None or ws_age > cfg.lag_var_max_age_s
                or bbo_age is None or bbo_age > cfg.lag_hl_max_age_s):
            self.lag_stats["skipped_age"] += 1
            return False
        transport, prev_gap = lag.get("ws_transport_s"), lag.get("ws_prev_gap_s")
        if (transport is None or transport > cfg.lag_ws_max_transport_s
                or prev_gap is None or prev_gap > cfg.lag_ws_max_prev_gap_s):
            # revue : prix websocket en retard côté serveur, ou saut de rattrapage après un trou.
            self.lag_stats["skipped_ws_stale"] += 1
            return False
        if not lag.get("spread_ok"):
            # revue : aucun demi-spread RFQ de moins de var_ws_spread_max_age_s.
            self.lag_stats["skipped_spread"] += 1
            return False
        if not lag.get("basis_ready", True):
            self.lag_stats["skipped_basis"] += 1
            return False
        move = float(lag.get("move_bps") or 0.0)
        if (move if is_buy else -move) < cfg.lag_min_move_bps:
            # le prix Var n'a pas bougé (dans le sens de la prise) : pas une avance de Var.
            self.lag_stats["skipped_move"] += 1
            return False
        now = self._mono()
        if now - self._lag_last_take < cfg.lag_min_interval_s:
            self.lag_stats["skipped_interval"] += 1
            return False
        if now < self._backoff_until[side] or self.pending_qty != 0 or self.exit_inflight:
            return False
        # revue : taille bornée à la taille visible au meilleur prix (bbo), et jamais sous
        # min_hedge_usd (un reliquat non couvrable).
        bsz, asz = lag.get("book_sizes") or (None, None)
        visible = asz if is_buy else bsz
        if not visible or visible <= 0:
            self.lag_stats["skipped_size"] += 1
            return False
        sz = round_sz(min(sz, visible), self.sz_decimals)
        if sz <= 0 or sz * best < cfg.min_hedge_usd:
            self.lag_stats["skipped_size"] += 1
            return False
        self._lag_last_take = now
        px = round_px(best, self.sz_decimals, is_buy)
        expected = gap - self.taker_fee_bps - hs_bps
        ctx = {"age": ws_age, "var_px": q.bid if is_buy else q.ask, "basis": basis, "taker": True,
               "expected_bps": expected}
        self.lag_stats["taken"] += 1
        try:
            res = await self.hl.ioc(cfg.hl_coin, is_buy, sz, px)
        except Exception as exc:
            # revue : un IOC en erreur (délai dépassé...) a peut-être été exécuté : relecture
            # immédiate de la position HL et couverture de l'écart par le chemin normal.
            self.lag_stats["errors"] += 1
            self._bump_backoff(side, str(exc))
            self.journal.record_incident(
                cfg.var_asset, "warn", f"prise sur retard : IOC HL en erreur ({type(exc).__name__}: {exc}), "
                                       "relecture de la position HL")
            await self._recover_ioc_error(is_buy, sz, px, ctx)
            return True
        filled = float(res.get("filled_sz") or 0.0)
        oid = res.get("oid")
        avg = res.get("avg_px") or px
        self.journal.record_incident(
            cfg.var_asset, "info",
            f"prise sur retard : {'achat' if is_buy else 'vente'} HL {sz} à {px} (rempli {filled}), "
            f"Var websocket {q.mid:.6g} + basis {basis:+.4f}, écart {gap:.1f} pb pour un seuil de "
            f"{margin:.1f} pb, écart attendu net {expected:+.1f} pb, mouvement Var {move:+.1f} pb, "
            f"âge Var {ws_age:.2f} s, âge bbo {bbo_age:.2f} s")
        if res.get("error"):
            self.lag_stats["errors"] += 1
            self._bump_backoff(side, res.get("error"))
            self.journal.record_incident(cfg.var_asset, "warn", f"prise sur retard : IOC HL refusé : {res.get('error')}")
            return True
        if filled <= 0:
            self.lag_stats["no_fill"] += 1
        else:
            self.lag_stats["filled"] += 1
            self.lag_stats["filled_usd"] += filled * avg
        if oid:
            # le fill arrive par userFills (avant ou après cette réponse) : oid rattaché à ce
            # moteur, contexte de décision attaché, fills déjà reçus rejoués.
            self._my_oids.add(oid)
            self._ctx_by_oid[oid] = ctx
            await self._replay_orphans(oid)
        elif filled > 0:
            if self.paper:
                await self._synthetic_fill(is_buy, filled, avg, ctx)
            else:
                self.journal.record_incident(
                    cfg.var_asset, "error",
                    f"prise sur retard : IOC rempli ({filled}) sans oid, fill non rattachable : la "
                    "réconciliation corrigera le delta")
        # revue : reliquat sous min_hedge_usd (fill partiel) fermé tout de suite.
        await self._settle_lag_dust(min_age_s=0.0)
        return True

    async def _synthetic_fill(self, is_buy: bool, sz: float, px: float, ctx: dict) -> None:
        """Fill simulé (paper, ou écart de position constaté après un IOC en erreur), rattaché à
        un oid fictif négatif pour passer par le chemin normal on_fill puis hedge."""
        self._paper_oid -= 1
        fake = self._paper_oid
        self._my_oids.add(fake)
        self._ctx_by_oid[fake] = ctx
        await self.on_fill({"coin": self.cfg.hl_coin, "side": "B" if is_buy else "A", "px": px,
                            "sz": sz, "oid": fake, "fee": 0.0, "tid": 0, "time": 0})

    async def _recover_ioc_error(self, is_buy: bool, sz: float, px: float, ctx: dict) -> None:
        """Revue : après un IOC en erreur, la position HL réelle est relue ; l'écart avec la
        position connue du moteur (dans le sens de l'IOC, au plus sa taille) est traité comme un
        fill et couvert. Les fills userFills de cet IOC (oid inconnu) restent en tampon orphelin
        et ne sont jamais rejoués : pas de double comptage."""
        coin = self.cfg.hl_coin
        try:
            pos = await self.hl.positions()
        except Exception as exc:
            await self.block(f"IOC HL en erreur et position HL illisible ({type(exc).__name__}: {exc}), "
                             "vérifier à la main")
            return
        real = float((pos or {}).get(coin, 0.0) or 0.0)
        diff = real - self.pos_hl
        half = 10 ** -self.sz_decimals / 2
        if abs(diff) <= half:
            self.journal.record_incident(self.cfg.var_asset, "info",
                                         "prise sur retard : IOC en erreur, aucune exécution constatée")
            return
        if (diff > 0) != is_buy or abs(diff) > sz + half:
            self.journal.record_incident(
                self.cfg.var_asset, "error",
                f"prise sur retard : IOC en erreur, écart de position HL inattendu ({diff:+g}) : "
                "la réconciliation tranchera")
            return
        filled = round_sz(abs(diff) + half / 2, self.sz_decimals)
        self.journal.record_incident(self.cfg.var_asset, "warn",
                                     f"prise sur retard : IOC en erreur mais exécuté ({filled}), couverture")
        self.lag_stats["filled"] += 1
        self.lag_stats["filled_usd"] += filled * px
        await self._synthetic_fill(is_buy, filled, px, ctx)
        await self._settle_lag_dust(min_age_s=0.0)

    async def _settle_lag_dust(self, min_age_s: float = 1.0) -> None:
        """Revue : une quantité HL non couverte sous min_hedge_usd (fill partiel d'un IOC) est
        refermée sur HL par un IOC reduce-only, avec incident. pending_qty ne reste jamais bloqué :
        s'il reste un résidu après cet IOC, il est remis à 0 (incident error) et la
        réconciliation corrige le delta réel. Verrou de couverture occupé (autre marché) : rien,
        nouvel essai au tick suivant (le tick ne doit jamais attendre ce verrou)."""
        if self.pending_qty == 0 or self.hedge_inflight or self.gate.lock.locked():
            return
        async with self.gate.lock:
            pend = self.pending_qty
            if pend == 0 or self.hedge_inflight:
                return
            ref_px = self._pending_px or 0.0
            if ref_px <= 0 or abs(pend) * ref_px >= self.cfg.min_hedge_usd:
                return
            if self._pending_t and time.time() - self._pending_t < min_age_s:
                return
            long_ = pend > 0
            bid, ask = self.book
            ref = bid if long_ else ask
            ref = ref if ref else ref_px
            px = round_px(ref * 0.995 if long_ else ref * 1.005, self.sz_decimals, not long_)
            sz = round_sz(abs(pend), self.sz_decimals)
            filled, err, exit_px = 0.0, None, px
            if sz > 0:
                try:
                    res = await self.hl.ioc(self.cfg.hl_coin, not long_, sz, px, reduce_only=True)
                    filled = float(res.get("filled_sz") or 0.0)
                    exit_px = res.get("avg_px") or px
                    err = res.get("error")
                except Exception as exc:
                    err = f"{type(exc).__name__}: {exc}"
            signed = filled if long_ else -filled
            self.pos_hl -= signed
            self.pending_qty -= signed
            pnl = ((exit_px - ref_px) if long_ else (ref_px - exit_px)) * filled
            if filled > 0:
                self.journal.record_arb(self.cfg.var_asset, "B" if long_ else "A", ref_px, filled, 0.0, "unwind",
                                        exit_px, filled, pnl / max(1e-12, filled * ref_px) * 1e4, pnl, 0.0,
                                        "unwound")
            msg = (f"reliquat HL {pend:+g} sous min_hedge_usd ({self.cfg.min_hedge_usd:.0f} $) fermé sur HL "
                   f"en reduce-only : {filled} rempli")
            if err or abs(self.pending_qty) > 10 ** -self.sz_decimals / 2:
                self.journal.record_incident(self.cfg.var_asset, "error",
                                             msg + f", reste {self.pending_qty:+g} remis à 0 (erreur : {err}) : "
                                                   "la réconciliation corrigera le delta")
            else:
                self.journal.record_incident(self.cfg.var_asset, "warn", msg)
            self.pending_qty = 0.0
            self._pending_t = 0.0
            self._pending_ctx = None

    # -- mode chase ---------------------------------------------------------
    async def _chase_cancel(self, side: str, cooldown: bool = True) -> None:
        if self.orders.get(side) is None:
            return
        await self._cancel_side(side, cooldown=cooldown)
        if self.orders.get(side) is None:
            self.chase_stats["cancelled"] += 1

    async def _requote_chase(self, q: VarQuote, basis: float, hl_bid, hl_ask, wide_mult: float,
                             block_reason: Optional[str], rfq: Optional[VarQuote] = None) -> None:
        """Mode chase : un ordre ALO au meilleur prix du carnet HL, seulement pendant une
        inefficience (docs/superpowers/chase-mode-2026-09-23.md). Tout motif de blocage, y compris
        la cotation Var seulement périmée (pas de `hold` ici), annule aussitôt."""
        if block_reason is not None:
            self.last_reason = block_reason
            self._chase_seen = {"buy": False, "sell": False}
            self._detect_qts = {"buy": None, "sell": None}
            for side in ("buy", "sell"):
                await self._chase_cancel(side)
            return
        cancel_only = bool(self.cancel_only_fn())
        self.last_reason = CANCEL_ONLY_REASON if cancel_only else None
        self._last_basis = basis
        mid = q.mid + basis
        ratio = inventory_ratio(self.pos_usd(mid), self.cfg.max_position_usd)
        m_buy, m_sell = chase_margins_bps(self.cfg, self.fee_bps, ratio, wide_mult)
        sz = round_sz(self.cfg.fill_usd / mid, self.sz_decimals)
        # revue : le côté qui réduit la position ne la retourne jamais (taille bornée à |pos_hl|).
        reduce = round_sz(abs(self.pos_hl), self.sz_decimals)
        sizes = {"buy": min(sz, reduce) if self.pos_hl < 0 else sz,
                 "sell": min(sz, reduce) if self.pos_hl > 0 else sz}
        # revue : le côté qui charge ne pose que si la position resterait sous max_position_usd.
        sizes = self._cap_loading(sizes, mid)
        for side, margin, best in (("buy", m_buy, hl_bid), ("sell", m_sell, hl_ask)):
            if self._aborted():
                return
            await self._chase_side(side, margin, best, q, basis, sizes[side], cancel_only, rfq or q)

    def chase_hot(self) -> bool:
        """F2 : marché « chaud » (coté en priorité par le VarFeed) : un ordre chase au repos, ou
        une inefficience vue dans les hot_window_s dernières secondes."""
        if self.cfg.entry_mode != "chase":
            return False
        if any(o is not None for o in self.orders.values()):
            return True
        return self._mono() - self._chase_seen_at <= self.g.hot_window_s

    def _request_fresh(self) -> None:
        """F1 : demander une cotation fraîche, au plus une fois par fenêtre de refroidissement."""
        now = self._mono()
        if now < self._fresh_req_until:
            return
        self._fresh_req_until = now + self.cfg.chase_replace_cooldown_s
        self.chase_stats["fresh_requests"] += 1
        try:
            self.fresh_quote_fn()
        except Exception as exc:
            self.journal.record_incident(self.cfg.var_asset, "warn",
                                         f"demande de cotation fraîche échouée : {type(exc).__name__}: {exc}")

    def _decision_ctx(self, side: str, q: VarQuote, basis: float, age: float) -> dict:
        return {"age": age, "var_px": q.bid if side == "buy" else q.ask, "basis": basis}

    async def _chase_side(self, side: str, margin: Optional[float], best, q: VarQuote, basis: float,
                          sz: float, cancel_only: bool, rfq: Optional[VarQuote] = None) -> None:
        is_buy = side == "buy"
        # L2 : la confirmation F1 porte sur la cotation RFQ (le prix websocket de la référence `q`
        # avance chaque seconde et ne vaut pas confirmation).
        rq = rfq or q
        target = floor = None
        if margin is not None and best and sz > 0:
            var_ref = (q.bid if is_buy else q.ask) + basis
            # plancher strict face à la cotation Var ACTUELLE : jamais d'ordre en deçà.
            floor = var_ref * (1 - margin / 1e4) if is_buy else var_ref * (1 + margin / 1e4)
            if (best <= floor) if is_buy else (best >= floor):
                target = best
        if target is not None and not self._chase_seen[side]:
            self.chase_stats["detections"] += 1
        self._chase_seen[side] = target is not None
        cur = self.orders.get(side)
        now = self._mono()
        if target is None:
            if self._detect_qts[side] is not None and rq.ts > self._detect_qts[side]:
                # F1 : la cotation plus récente n'a pas confirmé l'inefficience.
                self.chase_stats["not_confirmed"] += 1
            self._detect_qts[side] = None
            # fin de l'inefficience (ou côté fermé par la limite de position) : annulation.
            await self._chase_cancel(side)
            return
        self._chase_seen_at = now
        # F3 : âge de la cotation Var utilisée pour décider. Revue : avec une référence websocket,
        # le plus grand des âges du prix websocket et de la cotation RFQ (demi-spread, F1).
        age = max(0.0, self._wall() - min(q.ts, rq.ts))
        max_age = self.cfg.chase_max_quote_age_s
        if cur is None:
            if cancel_only:
                return
            # F1 : pas de pose sur la cotation qui a détecté l'inefficience ; une cotation plus
            # récente (demandée au VarFeed) doit la confirmer. F3 : ni sur une cotation trop vieille.
            det = self._detect_qts[side]
            if det is None:
                self._detect_qts[side] = det = rq.ts
            confirmed = not self.cfg.chase_fresh_confirm or rq.ts > det
            if not confirmed or age > max_age:
                self._request_fresh()
                return
            if rq is not q:
                # L2 : l'écart réévalué sur la cotation fraîche elle-même (F1) doit tenir aussi.
                r_ref = (rq.bid if is_buy else rq.ask) + basis
                r_floor = r_ref * (1 - margin / 1e4) if is_buy else r_ref * (1 + margin / 1e4)
                if not ((target <= r_floor) if is_buy else (target >= r_floor)):
                    self.chase_stats["not_confirmed"] += 1
                    self._detect_qts[side] = None
                    return
            if (now < self._backoff_until[side] or now < self._chase_cooldown_until[side]
                    or now - self._last_place[side] < self.cfg.chase_min_modify_interval_s):
                return
            self.chase_stats["place_attempts"] += 1
            self._next_ctx = self._decision_ctx(side, q, basis, age)
            try:
                await self._place(side, sz, target)
            finally:
                self._next_ctx = None
            if self.orders.get(side) is not None:
                self._chase_born[side] = now
                self.chase_stats["placed"] += 1
                # une nouvelle pose après annulation exigera une nouvelle confirmation.
                self._detect_qts[side] = None
            return
        if now - self._chase_born[side] >= self.cfg.chase_ttl_s:
            await self._chase_cancel(side)
            return
        stale = age > max_age
        if stale and (self.quote_refresh_failed_fn()
                      or age > max_age + self.g.hot_quote_interval_s):
            # F3 : cotation trop vieille ET le suivi rapproché n'a pas pu la rafraîchir.
            await self._chase_cancel(side)
            if self.orders.get(side) is None:
                self.chase_stats["stale_cancels"] += 1
            return
        if cur["px"] == target:
            return
        cur_ok = (cur["px"] <= floor) if is_buy else (cur["px"] >= floor)
        # F3 : pas de déplacement sur une cotation trop vieille (l'ordre reste s'il respecte
        # encore le plancher, en attendant la cotation du suivi rapproché).
        can_modify = (not cancel_only and not stale and now >= self._backoff_until[side]
                      and now - self._last_place[side] >= self.cfg.chase_min_modify_interval_s)
        if not can_modify:
            # l'ordre au repos ne peut pas encore suivre : le garder seulement s'il respecte
            # toujours le plancher.
            if not cur_ok:
                await self._chase_cancel(side)
            return
        msz = round_sz(min(cur["sz"], sz), self.sz_decimals)
        if msz <= 0:
            await self._chase_cancel(side)
            return
        self._next_ctx = self._decision_ctx(side, q, basis, age)
        try:
            await self._chase_move(side, cur, sz, msz, target)
        finally:
            self._next_ctx = None

    async def _chase_move(self, side: str, cur: dict, sz: float, msz: float, target: float) -> None:
        if await self._modify(side, cur, msz, target):
            self.chase_stats["modified"] += 1
            await self._chase_after_modify(side, cur, sz, target)
            return
        # repli : annuler puis reposer, seulement si l'annulation est confirmée (I2). Ce n'est
        # qu'un déplacement : pas de délai avant repose.
        await self._chase_cancel(side, cooldown=False)
        if self.orders.get(side) is not None:
            self._bump_backoff(side, "annulation non confirmée")
            return
        if self._aborted():
            return
        # revue : un fill pendant l'annulation a pu réduire le reste (`cur` est décrémenté par
        # on_fill tant qu'il est l'ordre suivi).
        msz = round_sz(min(cur["sz"], sz), self.sz_decimals)
        if msz <= 0:
            return
        self.chase_stats["place_attempts"] += 1
        await self._place(side, msz, target)
        if self.orders.get(side) is not None:
            self.chase_stats["placed"] += 1
            self._chase_born[side] = self._mono()   # revue : nouvel ordre, nouvelle durée de vie

    async def _chase_after_modify(self, side: str, old: dict, sz: float, target: float) -> None:
        """Revue : un fill sur l'ancien ordre pendant le modify décrémente `old`, puis `_placed`
        écrase la taille avec celle d'avant le fill. On réaligne sur le reste réel, et on annule
        le nouvel ordre si l'ancien a été entièrement rempli ou si le marché ne cote plus."""
        new = self.orders.get(side)
        if new is None:
            return
        half = 10 ** -self.sz_decimals / 2
        remaining = round_sz(min(old["sz"], sz), self.sz_decimals)
        if old["sz"] <= half or remaining <= 0 or self._aborted():
            await self._chase_cancel(side)
            return
        if remaining >= new["sz"] - half:
            return
        if not await self._modify(side, new, remaining, target):
            if self.orders.get(side) is not None and self.orders[side]["sz"] > remaining + half:
                await self._chase_cancel(side)

    # -- fills et couverture ---------------------------------------------
    async def on_fill(self, fill: dict) -> None:
        # C3 : tout fill sur un oid que ce moteur a placé doit être pris en compte, même si
        # l'ordre a depuis été annulé (plus dans self.orders). Un oid encore inconnu (fill arrivé
        # avant l'écriture de place_alo()) est mis en tampon et rejoué par requote() (finding 6).
        if fill["oid"] not in self._my_oids:
            self._orphan_fills.setdefault(fill["oid"], []).append((time.time(), fill))
            return
        side = "buy" if fill["side"] == "B" else "sell"
        o = self.orders.get(side)
        is_active = o is not None and o["oid"] == fill["oid"]
        signed = fill["sz"] if side == "buy" else -fill["sz"]
        self.pos_hl += signed
        prev = self.pending_qty
        total = prev + signed
        # N4 : ne moyenner le prix/horodatage que sur la part de `pending_qty` pas encore
        # réclamée par une couverture en vol (`_claim_qty`). Le résidu qui arrive pendant une
        # couverture démarre son propre lot plutôt que de se mélanger au prix du lot déjà capturé
        # par hedge().
        free_prev = prev - self._claim_qty
        # I5 : ne moyenner que des fills de même sens (ou premier fill du lot libre). Un fill de
        # sens opposé (netting du résidu libre) écrase le prix par celui du dernier fill.
        same_dir = free_prev == 0 or (free_prev > 0) == (signed > 0)
        if same_dir and free_prev != 0:
            free_total = free_prev + signed
            self._pending_px = (self._pending_px * abs(free_prev) + fill["px"] * fill["sz"]) / max(1e-12, abs(free_total))
        else:
            # nouveau lot libre (premier fill, ou fill arrivé pendant une couverture qui a tout
            # réclamé, ou netting de sens opposé) : prix et horodatage repartent de ce fill.
            self._pending_px = fill["px"]
            self._pending_t = time.time()
        self.pending_qty = total
        if not self._pending_t:
            self._pending_t = time.time()
        ctx = self._ctx_by_oid.get(fill["oid"])
        if ctx is not None:
            # F4/F5 : contexte de décision de l'ordre rempli, repris par la couverture.
            self._pending_ctx = ctx
        if is_active:
            o["sz"] -= fill["sz"]
            if o["sz"] <= 10 ** -self.sz_decimals / 2:
                self.orders[side] = None
        other = "sell" if side == "buy" else "buy"
        # r3-3 : une annulation qui lève ici ne doit jamais empêcher le déclenchement de la
        # couverture (l'exposition HL est déjà là, il faut la couvrir quoi qu'il arrive).
        try:
            await self._cancel_side(other)
        except Exception as exc:
            self.journal.record_incident(self.cfg.var_asset, "warn",
                                         f"annulation de l'autre côté HL échouée après fill : {type(exc).__name__}: {exc}")
        if abs(self.pending_qty) * fill["px"] >= self.cfg.min_hedge_usd:
            await self.hedge()

    async def _safe_positions(self, deadline: Optional[float] = None) -> Optional[dict]:
        try:
            if deadline is None:
                return await self.var.positions()
            return await self.var.positions(deadline=deadline)
        except Exception as exc:
            # L6 : garder la cause (le 23/09, l'échec de la lecture de base n'était pas traçable).
            self._last_read_err = f"{type(exc).__name__}: {str(exc)[:150]}"
            return None

    async def _position_qty(self, deadline: Optional[float] = None) -> Optional[float]:
        pos = await self._safe_positions(deadline)
        if pos is None:
            return None
        return pos.get(self.cfg.var_asset, {}).get("qty")

    async def hedge(self) -> None:
        if self.cfg.entry_mode == "chase":
            # revue : en chase, plus aucun ordre au carnet pendant une couverture, y compris
            # pendant l'attente du verrou (une autre couverture peut le tenir plusieurs secondes).
            for side in ("buy", "sell"):
                try:
                    await self._chase_cancel(side)
                except Exception as exc:
                    self.journal.record_incident(self.cfg.var_asset, "warn",
                                                 f"annulation chase avant couverture échouée : {type(exc).__name__}: {exc}")
        async with self.gate.lock:
            if self.pending_qty == 0:
                return
            # C2 : capturer la quantité à couvrir AU DÉBUT. Un fill qui arrive pendant cette
            # couverture (traité en tâche concurrente) s'ajoutera à self.pending_qty pendant
            # qu'on est en vol ; on ne couvre ici que `signed`, et le reste sera repris par
            # l'appel à hedge() que ce fill concurrent déclenche lui-même (il attendra le verrou).
            signed = self.pending_qty
            qty = abs(signed)
            hl_is_buy = signed > 0
            var_side = "sell" if hl_is_buy else "buy"
            pending_px = self._pending_px
            pending_t = self._pending_t
            ctx = self._pending_ctx
            prior_state = self.state
            self.state, self.hedge_inflight = HEDGING, True
            self._claim_qty = signed
            deadline = time.monotonic() + self.g.hedge_retry_s
            # C1a : réserver le temps d'une dernière lecture des positions avant tout débouclage.
            # Si le budget est trop court pour se le permettre, on ne réserve rien (comportement
            # d'avant, utilisé par les tests à hedge_retry_s court).
            loop_deadline = deadline - self._final_read_reserve_s if self.g.hedge_retry_s > self._final_read_reserve_s else deadline
            var_px = None
            last_err = ""
            # L6 : vrai tant qu'un ordre Var a pu partir sans que son issue soit connue. Un rfq au
            # statut final d'échec (rejected...) n'est plus vivant : si la suite échoue sans
            # qu'aucun autre ordre ne parte, c'est un échec sans ordre Var -> débouclage + PAUSE
            # (le 23/09, UKOILP a été BLOQUÉ dans ce cas).
            rfq_live = False
            try:
                # N2 : une annulation qui lève ne doit pas empêcher la couverture d'avoir lieu, ni
                # laisser hedge_inflight bloqué à True (le `finally` ci-dessous s'en charge de
                # toute façon, mais on ne veut pas non plus interrompre la tentative de couverture).
                try:
                    await self.cancel_all()
                except Exception as exc:
                    self.journal.record_incident(self.cfg.var_asset, "error",
                                                 f"annulation HL avant couverture échouée : {type(exc).__name__}: {exc}")

                rfq = None
                quote_px = None
                baseline_qty = None
                confirm_deadline = None
                baseline_failed = False
                while var_px is None and time.monotonic() < loop_deadline:
                    try:
                        if rfq is None:
                            q = await self.var.quote(self.cfg.var_asset, self.cfg.var_kind, qty, prio=HEDGE,
                                                     deadline=loop_deadline)
                            quote_px = q.bid if var_side == "sell" else q.ask
                            # C1 : lire les positions Var juste avant d'envoyer l'ordre, pour
                            # pouvoir confirmer un remplissage par différence si le statut reste
                            # muet. C1b : si cette lecture échoue, la retenter une fois ; si elle
                            # échoue encore, ne pas envoyer d'ordre (on ne pourra pas confirmer).
                            baseline = await self._safe_positions(loop_deadline)
                            if baseline is None:
                                baseline = await self._safe_positions(loop_deadline)
                            if baseline is None:
                                last_err = ("lecture des positions Var (base) impossible après 2 tentatives : "
                                            f"{self._last_read_err}")
                                baseline_failed = True
                                break
                            baseline_qty = baseline.get(self.cfg.var_asset, {}).get("qty", 0.0) or 0.0
                            # L6 : issue inconnue dès l'envoi ; seul un refus certain (429, limiteur
                            # local, rejet HTTP 4xx) garantit qu'aucun ordre n'est parti.
                            rfq_live = True
                            try:
                                rfq = await self.var.market(q.quote_id, var_side, self.g.var_max_slippage,
                                                            prio=HEDGE, deadline=loop_deadline)
                            except (VarRateLimited, VarRejected) as exc:
                                if isinstance(exc, VarRateLimited) or getattr(exc, "status", 500) < 500:
                                    rfq_live = False
                                raise
                            confirm_deadline = time.monotonic() + self._confirm_window_s
                        st = None
                        try:
                            st = await self.var.order_status(rfq, prio=HEDGE, deadline=loop_deadline)
                        except VarRateLimited:
                            raise
                        except VarError as exc:
                            last_err = str(exc)[:200]
                            st = None
                        except Exception as exc:
                            last_err = f"{type(exc).__name__}: {str(exc)[:150]}"
                            st = None
                        status = (st or {}).get("status")
                        if status == "cleared":
                            price = st.get("price")
                            self._hedge_px_is_quote = price in (None, "")
                            var_px = float(price) if price not in (None, "") else quote_px
                            break
                        if status in _VAR_FINAL_FAIL:
                            # C1 : ce rfq est définitivement mort, on peut en émettre un nouveau.
                            last_err = f"statut {status}"
                            rfq = None
                            rfq_live = False
                            await asyncio.sleep(0.5)
                            continue
                        # statut inconnu ou en attente : NE JAMAIS renvoyer un nouvel ordre tant
                        # que ce rfq n'est pas confirmé en échec (C1). On confirme au pire par
                        # relecture des positions.
                        now = time.monotonic()
                        if confirm_deadline is not None and now >= confirm_deadline:
                            cur_qty = await self._position_qty(loop_deadline)
                            if cur_qty is not None:
                                delta = cur_qty - baseline_qty
                                if abs(delta - (-signed)) <= max(1e-6, qty * 0.02):
                                    var_px = quote_px
                                    self._hedge_px_is_quote = True
                                    break
                            confirm_deadline = now + self._confirm_window_s
                        await asyncio.sleep(self._status_poll_s)
                    except VarRateLimited as exc:
                        local = isinstance(exc, VarBudgetExceeded)
                        last_err = (f"attente limiteur {exc.wait_s:.1f}s" if local else f"429 ({exc.wait_s}s)")
                        remaining = loop_deadline - time.monotonic()
                        if exc.wait_s >= remaining:
                            # I1 (spec §6) : Var bloqué au-delà du budget de couverture -> ne pas
                            # attendre, passer tout de suite à la lecture finale / au débouclage HL.
                            # L5 : distinguer un vrai refus Var (429) du simple espacement du
                            # limiteur local sur un budget déjà épuisé (cas USOILP du 23/09).
                            if local:
                                last_err = (f"budget de couverture épuisé ({self.g.hedge_retry_s:.0f}s, "
                                            f"attente limiteur {exc.wait_s:.1f}s)")
                            else:
                                last_err = f"Var bloqué {exc.wait_s:.1f}s, au-delà du budget de couverture"
                            if rfq is not None:
                                last_err += " ; ordre Var sans statut final"
                            break
                        await asyncio.sleep(max(0.0, exc.wait_s))
                    except VarError as exc:
                        last_err = str(exc)[:200]
                        await asyncio.sleep(0.5)
                    except Exception as exc:
                        # C5 : toute exception (Playwright, KeyError...), pas seulement VarError.
                        last_err = f"{type(exc).__name__}: {str(exc)[:150]}"
                        await asyncio.sleep(0.5)

                # C1a : si un rfq a été envoyé et qu'on n'a toujours pas de confirmation, une
                # DERNIÈRE lecture des positions Var, TOUJOURS, avant d'envisager un débouclage.
                if var_px is None and not last_err and time.monotonic() >= loop_deadline:
                    last_err = f"budget de couverture épuisé ({self.g.hedge_retry_s:.0f}s)"
                    if rfq is not None:
                        last_err += " ; ordre Var sans statut final"
                uncertain = False
                if var_px is None and rfq_live:
                    cur_qty = await self._position_qty(deadline)
                    if cur_qty is None:
                        uncertain = True
                        last_err = (last_err + " ; " if last_err else "") + \
                            f"lecture finale des positions Var impossible : {self._last_read_err}"
                    else:
                        delta = cur_qty - (baseline_qty or 0.0)
                        if abs(delta - (-signed)) <= max(1e-6, qty * 0.02):
                            var_px = quote_px
                            self._hedge_px_is_quote = True

                latency = (time.time() - pending_t) * 1000 if pending_t else 0.0
                if var_px is not None:
                    # L4 : une entrée lag_taker (ctx "taker") est mesurée comme le chase, frais
                    # taker HL.
                    taker = ctx is not None and bool(ctx.get("taker"))
                    chase = self.cfg.entry_mode == "chase" or taker
                    fee_e = self.taker_fee_bps if taker else self.fee_bps
                    # F5 : en chase, l'écart est mesuré contre var_px + basis utilisé à la
                    # décision, comme la décision elle-même (brut conservé dans l'incident F4).
                    basis_e = (ctx["basis"] if ctx is not None else self._last_basis) if chase else 0.0
                    e = edge_bps(hl_is_buy, pending_px, var_px + basis_e, fee_e)
                    status_label = "hedged_px_quote" if self._hedge_px_is_quote else "hedged"
                    # C2 : positions, quantité en attente et état mis à jour AVANT le journal.
                    self.pos_var -= signed
                    self.pending_qty -= signed
                    if self.pending_qty == 0:
                        self._pending_t = 0.0
                    # r3-4 : un pause()/block() externe pendant que cette couverture était en vol
                    # a déjà changé self.state (ce n'est alors plus HEDGING) -> on ne touche pas à
                    # l'état, pour ne pas écraser cette décision externe. Sinon (état encore
                    # HEDGING, personne n'est intervenu), on applique la règle N3/I1 : ne revenir
                    # à QUOTING que si l'état antérieur à hedge() était QUOTING ; sinon (PAUSED,
                    # BLOCKED, EXITING, IDLE...) on restaure cet état antérieur tel quel.
                    if self.state == HEDGING:
                        if prior_state == QUOTING:
                            self.state, self.last_reason = QUOTING, None
                        else:
                            self.state = prior_state
                    self.journal.record_arb(self.cfg.var_asset, "B" if hl_is_buy else "A", pending_px, qty,
                                            fee_e / 1e4 * qty * pending_px, var_side, var_px, qty,
                                            e, e / 1e4 * qty * pending_px, latency, status_label)
                    if taker:
                        self.lag_stats["hedged"] += 1
                        self.lag_stats["expected_bps_sum"] += float(ctx.get("expected_bps") or 0.0)
                        self.lag_stats["realized_bps_sum"] += e
                    if chase:
                        self._record_slippage(ctx, hl_is_buy, var_side, pending_px, var_px, basis_e, latency)
                    if self.pending_qty == 0:
                        self._pending_ctx = None
                elif uncertain:
                    # C1a : impossible de confirmer OU d'infirmer la couverture Var -> on ne
                    # débouble PAS HL à l'aveugle (risque de double exposition inverse). On bloque
                    # et on laisse une intervention manuelle / la réconciliation trancher.
                    reason = f"couverture Var incertaine, vérifier à la main : {last_err}"
                    # r3-2 : block() journalise déjà l'incident ("bloqué : {reason}") -> pas de
                    # doublon ici.
                    await self.block(reason)
                else:
                    bid, ask = self.book
                    ref = bid if hl_is_buy else ask
                    ref = ref if ref else pending_px
                    raw_px = ref * 0.995 if hl_is_buy else ref * 1.005
                    px = round_px(raw_px, self.sz_decimals, not hl_is_buy)
                    ioc_sz = round_sz(qty, self.sz_decimals)
                    err = None
                    try:
                        res = await self.hl.ioc(self.cfg.hl_coin, not hl_is_buy, ioc_sz, px)
                        exit_px = res.get("avg_px") or px
                        filled = res.get("filled_sz") or 0.0
                        err = res.get("error")
                    except Exception as exc:
                        exit_px, filled = px, 0.0
                        err = f"{type(exc).__name__}: {exc}"
                    self.pos_hl += -filled if hl_is_buy else filled
                    covered_signed = filled if hl_is_buy else -filled
                    residual = signed - covered_signed
                    pnl = ((exit_px - pending_px) if hl_is_buy else (pending_px - exit_px)) * filled
                    reason = f"couverture échouée : {last_err}"
                    if err:
                        reason += f" ; IOC échoué : {err}"
                    # I2 : garder le résidu non débouclé dans pending_qty, ne pas l'effacer. On
                    # soustrait seulement la part couverte par cet IOC (`covered_signed`), pas
                    # `signed`, pour ne pas écraser un fill concurrent arrivé pendant cette
                    # couverture (C2) et déjà additionné à self.pending_qty par on_fill.
                    self.pending_qty -= covered_signed
                    if residual != 0:
                        reason += f" ; résidu HL non couvert {residual}"
                    if self.pending_qty == 0:
                        self._pending_t = 0.0
                    # C2 : position et quantité en attente déjà à jour ; journal ensuite, protégé.
                    self.journal.record_arb(self.cfg.var_asset, "B" if hl_is_buy else "A", pending_px, filled,
                                            0.0, "unwind", exit_px, filled,
                                            pnl / max(1e-12, qty * pending_px) * 1e4,
                                            pnl, latency, "unwound")
                    # r3-1 (spec §4.5) : un débouclage HL complet et sans erreur IOC (résidu nul)
                    # se termine en PAUSE ; seul un débouclage partiel ou une erreur IOC se
                    # termine en BLOQUÉ. pause() ne journalise pas d'incident (contrairement à
                    # block(), qui journalise déjà "bloqué : {reason}") : on en garde une trace
                    # explicite seulement dans ce cas pour ne pas perdre l'événement (r3-2 : pas
                    # de doublon quand block() suit, puisqu'il journalise lui-même `reason`).
                    if err or residual != 0:
                        await self.block(reason)
                    else:
                        self.journal.record_incident(self.cfg.var_asset, "error", reason)
                        await self.pause(reason)
            finally:
                self.hedge_inflight = False
                self._claim_qty = 0.0

        # Hors du verrou : si un fill concurrent a laissé un résidu au-dessus du seuil pendant
        # que cette couverture réussissait, on le couvre à son tour (C2).
        if var_px is not None and abs(self.pending_qty) * self._pending_px >= self.cfg.min_hedge_usd:
            await self.hedge()

    def _record_slippage(self, ctx: Optional[dict], hl_is_buy: bool, var_side: str, hl_px: float,
                         var_px: float, basis: float, latency_ms: float) -> None:
        """F4 : mesure du glissement d'une couverture chase (analyse de fin de journée). Glissement
        Var positif = défavorable (achat Var plus cher, ou vente moins chère, que la cotation de
        décision)."""
        taker = ctx is not None and bool(ctx.get("taker"))
        raw = edge_bps(hl_is_buy, hl_px, var_px, self.taker_fee_bps if taker else self.fee_bps)
        if ctx is not None and ctx.get("var_px"):
            dec = ctx["var_px"]
            slip = (var_px - dec) / dec * 1e4 if var_side == "buy" else (dec - var_px) / dec * 1e4
            head = f"âge cotation {ctx['age']:.2f} s, glissement Var {slip:+.1f} pb"
            if taker:
                # L4 : écart net attendu à la décision (comparable à tools/leadlag.py).
                head = f"prise sur retard, écart attendu {float(ctx.get('expected_bps') or 0.0):+.1f} pb, " + head
        else:
            head = "âge cotation inconnu, glissement Var inconnu"
        self.journal.record_incident(
            self.cfg.var_asset, "info",
            f"glissement couverture : {head}, latence fill-couverture {latency_ms:.0f} ms, "
            f"écart brut {raw:+.1f} pb, basis {basis:+.4f}")

    # -- sortie avant fermeture -------------------------------------------
    async def _close_var(self, var_pos: float) -> tuple[float, Optional[float], Optional[VarQuote]]:
        """Referme la position Var (reduce-only) et confirme. Renvoie (quantité fermée confirmée,
        prix de sortie, cotation utilisée)."""
        side = "sell" if var_pos > 0 else "buy"
        qty = abs(var_pos)
        # r3-5 : une exception non-VarError (Playwright, réseau...) autour de quote()/market() ne
        # doit pas s'échapper de la sortie ; on journalise et on confirme par relecture.
        rfq = q = None
        try:
            q = await self.var.quote(self.cfg.var_asset, self.cfg.var_kind, qty, prio=EXIT)
            rfq = await self.var.market(q.quote_id, side, self.g.var_max_slippage, reduce_only=True, prio=EXIT)
        except Exception as exc:
            self.journal.record_incident(self.cfg.var_asset, "error",
                                         f"sortie Var échouée : {type(exc).__name__}: {exc}")
        cleared_px = None
        cleared = False
        if rfq is not None:
            deadline = time.monotonic() + self._confirm_window_s
            while time.monotonic() < deadline:
                # N5/r3-5 : une erreur pendant le sondage ne fait pas abandonner la confirmation.
                try:
                    st = await self.var.order_status(rfq, prio=EXIT)
                except VarRateLimited as exc:
                    await asyncio.sleep(max(0.0, min(exc.wait_s, deadline - time.monotonic())))
                    continue
                except Exception:
                    await asyncio.sleep(self._status_poll_s)
                    continue
                status = (st or {}).get("status")
                if status == "cleared":
                    cleared = True
                    price = st.get("price")
                    cleared_px = float(price) if price not in (None, "") else None
                    break
                if status in _VAR_FINAL_FAIL:
                    break
                await asyncio.sleep(self._status_poll_s)
        if cleared:
            closed = qty
        else:
            cur = await self._position_qty()
            if cur is None:
                closed = 0.0
            elif cur == 0 or (cur > 0) != (var_pos > 0):
                closed = qty
            else:
                closed = max(0.0, qty - abs(cur))
            if closed <= 1e-12:
                closed = 0.0
            elif qty - closed <= max(1e-6, qty * 0.02):
                closed = qty
        if closed == 0.0:
            self.journal.record_incident(self.cfg.var_asset, "error", "sortie Var non confirmée")
        elif closed < qty:
            self.journal.record_incident(self.cfg.var_asset, "error",
                                         f"sortie Var partielle : {closed} sur {qty}")
        px = cleared_px
        if px is None and q is not None:
            px = q.bid if side == "sell" else q.ask
        return closed, px, q

    async def flatten(self, taker: bool, hl_bid, hl_ask, var_mid: Optional[float] = None) -> None:
        """Sortie du marché. En maker (taker=False) : annuler et passer en SORTIE. En taker :
        relire les positions réelles (I3a), fermer Var d'abord et confirmer, puis HL en IOC
        reduce-only pour la quantité couverte confirmée plus l'éventuel excédent HL non couvert
        (I3b/c), et journaliser le PnL réalisé (I8)."""
        # L3 : une seule sortie taker à la fois par marché.
        if taker:
            if self.exit_inflight:
                return
            self.exit_inflight = True
        try:
            await self._flatten(taker, hl_bid, hl_ask, var_mid)
        finally:
            if taker:
                self.exit_inflight = False

    def exit_reread_pending(self) -> bool:
        """L3 : vrai si un IOC de sortie HL a été envoyé depuis la dernière relecture des
        positions : le cache (pos_hl) n'est pas fiable, on ne retente pas de sortie."""
        return self.exit_ioc_at is not None and self.positions_read_at <= self.exit_ioc_at

    async def _flatten(self, taker: bool, hl_bid, hl_ask, var_mid: Optional[float]) -> None:
        # I4 : prendre le verrou de couverture pour toute la sortie (attend la fin d'une
        # couverture en cours, et empêche qu'une nouvelle couverture démarre pendant la sortie).
        async with self.gate.lock:
            try:
                await self.cancel_all()
            except Exception as exc:
                self.journal.record_incident(self.cfg.var_asset, "warn",
                                             f"annulation HL avant sortie échouée : {type(exc).__name__}: {exc}")
            self.state = EXITING
            if not taker:
                return
            # I3a : dimensionner la sortie sur les positions RÉELLES, relues sous le verrou.
            try:
                hl_detail = await self.hl.positions_detail()
                var_detail = await self.var.positions()
            except Exception as exc:
                self.journal.record_incident(self.cfg.var_asset, "error",
                                             f"sortie reportée : relecture des positions impossible ({type(exc).__name__}: {exc})")
                return
            h = (hl_detail or {}).get(self.cfg.hl_coin) or {}
            v = (var_detail or {}).get(self.cfg.var_asset) or {}
            hl_pos = float(h.get("szi", 0.0) or 0.0)
            hl_entry = h.get("entry")
            var_pos = float(v.get("qty", 0.0) or 0.0)
            var_entry = v.get("entry")
            self.pos_hl, self.pos_var = hl_pos, var_pos
            paired = min(abs(hl_pos), abs(var_pos)) if hl_pos * var_pos < 0 else 0.0

            # I3c : Var d'abord (la venue qui ferme), confirmée.
            var_closed, var_px, q = 0.0, None, None
            if var_pos != 0:
                self.progress_fn("Fermeture des positions Var")
                var_closed, var_px, q = await self._close_var(var_pos)
                self.pos_var = var_pos - (var_closed if var_pos > 0 else -var_closed)

            # puis HL : la part couverte dont la jambe Var est confirmée fermée, plus l'excédent HL
            # non couvert (le fermer réduit le risque). Jamais la part dont la jambe Var reste
            # ouverte : on ne laisse jamais Var nu.
            hl_target = (abs(hl_pos) - paired) + min(paired, var_closed)
            hl_filled, hl_px = 0.0, None
            sell = hl_pos > 0
            sz = round_sz(hl_target, self.sz_decimals) if hl_pos != 0 else 0.0
            if sz > 0:
                ref = hl_bid if sell else hl_ask
                if not ref:
                    ref = var_mid if var_mid else (q.mid if q is not None else None)
                    if ref:
                        self.journal.record_incident(self.cfg.var_asset, "warn",
                                                     "carnet HL vide : prix limite de sortie pris sur le mid Var ± 0,5 %")
                if not ref:
                    self.journal.record_incident(self.cfg.var_asset, "error",
                                                 "sortie HL impossible : ni carnet HL ni prix Var")
                else:
                    px = round_px(ref * 0.995 if sell else ref * 1.005, self.sz_decimals, not sell)
                    self.progress_fn("Fermeture des positions HL")
                    self.exit_ioc_at = time.monotonic()
                    try:
                        # I3b : sortie en reduce-only (ne peut jamais ouvrir ni retourner la position).
                        res = await self.hl.ioc(self.cfg.hl_coin, not sell, sz, px, reduce_only=True)
                        hl_filled = res.get("filled_sz") or 0.0
                        hl_px = res.get("avg_px") or px
                        if res.get("error"):
                            self.journal.record_incident(self.cfg.var_asset, "error",
                                                         f"sortie HL : {res.get('error')}")
                    except Exception as exc:
                        self.journal.record_incident(self.cfg.var_asset, "error",
                                                     f"sortie HL échouée : {type(exc).__name__}: {exc}")
                    self.pos_hl += -hl_filled if sell else hl_filled
            if self.pos_hl == 0:
                self.pending_qty = 0.0
            # I8 : PnL réalisé de la sortie, compté dans la perte journalière.
            if var_closed > 0 or hl_filled > 0:
                self._record_exit(hl_pos, hl_entry, hl_filled, hl_px, var_pos, var_entry, var_closed,
                                  var_px, paired)

    def _record_exit(self, hl_pos, hl_entry, hl_filled, hl_px, var_pos, var_entry, var_closed, var_px,
                     paired) -> None:
        """Estimation du PnL réalisé d'une sortie. Part couverte : l'écart d'entrée a déjà été
        compté par la couverture, seul compte l'écart de sortie (prix HL contre prix Var). Parts
        non couvertes : contre le prix d'entrée moyen de la venue. Frais taker HL déduits."""
        s = 1.0 if hl_pos > 0 else -1.0
        hl_pair = min(hl_filled, min(paired, var_closed))
        hl_naked = hl_filled - hl_pair
        var_pair = min(var_closed, paired)
        var_naked = var_closed - var_pair
        pair_q = min(hl_pair, var_pair)
        pnl = 0.0
        if pair_q > 0 and hl_px and var_px:
            pnl += s * (hl_px - var_px) * pair_q
        if hl_naked > 0 and hl_px and hl_entry:
            pnl += s * (hl_px - hl_entry) * hl_naked
        if var_naked > 0 and var_px and var_entry:
            sv = 1.0 if var_pos > 0 else -1.0
            pnl += sv * (var_px - var_entry) * var_naked
        fee = self.taker_fee_bps / 1e4 * hl_filled * (hl_px or 0.0)
        pnl -= fee
        notional = max(hl_filled * (hl_px or 0.0), var_closed * (var_px or 0.0), 1e-12)
        self.journal.record_arb(self.cfg.var_asset, "A" if hl_pos > 0 else "B", hl_px, hl_filled, fee,
                                ("sell" if var_pos > 0 else "buy") if var_closed > 0 else "", var_px,
                                var_closed, pnl / notional * 1e4, pnl, 0.0, "exit")
