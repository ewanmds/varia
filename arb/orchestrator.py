from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Optional

from arb.config import GlobalCfg, MarketCfg
from arb.engine import BLOCKED, EXITING, HEDGING, HedgeGate, IDLE, MarketEngine, PAUSED, QUOTING
from arb.hl_client import HLRefused
from arb.hl_feed import HLFeed
from arb.journal import SafeJournal
from arb.pricing import BasisEma, half_spread_rel, round_px, round_sz, sanity_ok, ws_reference
from arb.risk import STALE_QUOTE_REASON, ReconcileTracker, quote_block_reason
from arb.schedule import CLOSED, CLOSING, CLOSING_TAKER, FLAT, WIDE, is_roll_window, parse_sessions, phase_at
from arb.var_client import VarError, parse_max_leverage
from arb.var_feed import VarFeed
from arb.var_limiter import EXIT
from arb.var_ws import VarPriceStream

DEFAULT_EXPORT_DIR = Path("data") / "exports"

# fix r1 finding 6 : au moins ce délai entre deux tentatives de sortie taker pour un même marché.
_TAKER_RETRY_S = 5.0
# fix r1 finding 6 : délai après un échec "sortie Var non confirmée" (détecté via pos_var != 0
# après un flatten taker : engine.flatten() ne remet pos_var à 0 que si la sortie est confirmée).
_TAKER_RETRY_AFTER_VAR_UNCONFIRMED_S = 30.0
# I9/N2 : après un refus explicite de HL (compte non éligible), nouvel essai toutes les 10 min.
_DEADMAN_RETRY_AFTER_REFUSAL_S = 600.0
# L7 : nouveaux essais immédiats de l'annulation globale sur silence HL (espacés de 1 s).
_GLOBAL_CANCEL_RETRIES = 3
# I5 : delta au démarrage au-delà duquel le marché démarre BLOQUÉ (fraction de fill_usd).
_STARTUP_DELTA_FRAC = 0.2
STARTUP_DELTA_REASON = "delta au démarrage, vérifier puis débloquer"
VAR_LEVERAGE_REASON = "levier Var non réglé"
# L2 : motif de PAUSE après une réduction forcée, et fenêtre au-delà de laquelle une nouvelle
# réduction forcée sur le même marché n'est plus considérée comme une récidive.
FORCED_PAUSE_REASON = "réduction forcée, refroidissement"
_FORCED_REPEAT_WINDOW_S = 1800.0
# L3 : le basis rapide n'est mis à jour qu'avec un mid HL (bbo) d'au plus cet âge.
_FAST_BASIS_HL_MAX_AGE_S = 2.0
# L2 : historique des prix websocket Var gardé par marché (mouvement récent, L4).
_WS_HIST_S = 10.0
# L2 : au plus une reconnexion forcée du websocket Var muet toutes les _WS_RECONNECT_S.
_WS_RECONNECT_S = 10.0


class AsyncHL:
    """Adaptateur async autour du HLClient synchrone (appels HTTP/SDK dans un thread)."""

    def __init__(self, client) -> None:
        self.c = client

    async def place_alo(self, coin, is_buy, sz, px):
        return await asyncio.to_thread(self.c.place_alo, coin, is_buy, sz, px)

    async def modify(self, coin, oid, is_buy, sz, px):
        return await asyncio.to_thread(self.c.modify_alo, coin, oid, is_buy, sz, px)

    async def cancel(self, coin, oid):
        return await asyncio.to_thread(self.c.cancel, coin, oid)

    async def ioc(self, coin, is_buy, sz, px, reduce_only=False):
        if reduce_only:
            return await asyncio.to_thread(self.c.ioc, coin, is_buy, sz, px, reduce_only=True)
        return await asyncio.to_thread(self.c.ioc, coin, is_buy, sz, px)

    async def cancel_all(self, coins):
        return await asyncio.to_thread(self.c.cancel_all, coins)

    async def positions(self):
        return await asyncio.to_thread(self.c.positions)

    async def positions_detail(self):
        return await asyncio.to_thread(self.c.positions_detail)

    async def margin_ratio(self):
        return await asyncio.to_thread(self.c.margin_ratio)

    async def mids(self):
        return await asyncio.to_thread(self.c.mids)

    async def rate_limit(self):
        return await asyncio.to_thread(self.c.rate_limit)

    async def open_orders(self):
        return await asyncio.to_thread(self.c.open_orders)

    async def schedule_cancel(self, when_ms):
        return await asyncio.to_thread(self.c.schedule_cancel, when_ms)


class Orchestrator:
    def __init__(self, g: GlobalCfg, markets: list[MarketCfg], hl_client, var_client, journal,
                 clock=time.time) -> None:
        self.g = g
        self.markets = {m.var_asset: m for m in markets}
        self.hl_client = hl_client
        self.hl = AsyncHL(hl_client)
        self.var = var_client
        # C2 : aucune écriture journal ne doit interrompre le trading (log console en cas d'échec).
        self.journal = SafeJournal.wrap(journal)
        self._clock = clock
        self.gate = HedgeGate()
        self.feed = VarFeed(var_client, markets, g.var_quote_interval_s,
                            hot_interval_s=g.hot_quote_interval_s)
        # F2 : un marché chaud (ordre chase au repos ou inefficience récente) est coté en priorité.
        self.feed.hot_fn = self._market_hot
        self.engines: dict[str, MarketEngine] = {}
        self.sessions: dict[str, list] = {}
        self.basis: dict[str, BasisEma] = {m.var_asset: BasisEma(m.basis_ema_tau_min * 60) for m in markets}
        # L2 : flux de prix Var par websocket (lancé par run()), dernier prix par marché et
        # historique court ; L3 : basis rapide (prix websocket Var contre mid bbo HL).
        self.var_ws = VarPriceStream({m.var_asset: m.var_kind for m in markets}, on_update=self._on_var_ws)
        self.basis_fast: dict[str, BasisEma] = {m.var_asset: BasisEma(m.basis_fast_tau_s) for m in markets}
        # actif -> (prix, réception, horodatage serveur ou None, réception du prix précédent ou None)
        self._ws_px: dict[str, tuple] = {}
        # Revue : demi-spreads relatifs (pb) des dernières cotations RFQ, par marché (lag_taker).
        self._rfq_hs: dict[str, deque] = {m.var_asset: deque(maxlen=20) for m in markets}
        self._rfq_hs_ts: dict[str, float] = {}
        self._ws_hist: dict[str, deque] = {m.var_asset: deque() for m in markets}
        self._var_ws_started_at: Optional[float] = None
        self._ws_last_px_s: Optional[float] = None
        self._ws_muted = False
        self._ws_reconnect_at = 0.0
        # source de la référence Var utilisée au dernier tick, par marché ("ws" ou "rfq").
        self._var_ref_src: dict[str, str] = {}
        # L4 : réveil immédiat de la boucle de tick à chaque prix websocket Var quand un marché
        # est en lag_taker (sinon cadence fixe de 0,5 s).
        self._wake = asyncio.Event()
        self.tracker = ReconcileTracker(clock=clock)
        self.hl_feed: Optional[HLFeed] = None
        self.session_ok = True
        self.killed = False
        self.kill_reason: Optional[str] = None
        # Vrai pour un arrêt d'urgence, faux pour un arrêt normal qui ferme les positions
        # (close_for_stop) : même chemin de sortie, seuls le libellé et l'interface changent.
        self.emergency = False
        self.hl_margin = 0.0
        self.var_balance = 0.0
        self.var_liq: dict[str, Optional[float]] = {}
        self.last_prices: dict[str, dict] = {}
        # tâches de routage des fills (asyncio.create_task) : gardées vivantes le temps de leur
        # exécution, retirées via add_done_callback dès qu'elles se terminent (I5 sinon le garbage
        # collector peut annuler une tâche encore en vol).
        self._fill_tasks: set = set()
        # fix r1 finding 1 : vrai pendant tout un épisode de silence HL (WS ou fills) DONT
        # l'annulation globale a réussi, pour ne déclencher celle-ci qu'une fois par épisode (fix
        # r2 A : si l'annulation globale échoue, on ne marque PAS l'épisode -> nouvel essai au tick
        # suivant tant que le silence persiste).
        self._hl_silence_active = False
        # fix r1 finding 1 : vrai tant qu'aucune réconciliation n'a réussi depuis le dernier
        # épisode de silence HL -> la cotation reste suspendue jusque-là.
        self._need_reconcile = False
        # fix r2 B : instant (time.monotonic()) où le dernier épisode de silence HL s'est terminé.
        # reconcile() ne lève _need_reconcile que si sa lecture des positions a COMMENCÉ après cet
        # instant (pas juste après que l'annulation globale a réussi) : une réconciliation entamée
        # pendant le silence ne doit jamais lever le drapeau, même si elle se termine après.
        self._silence_ended_at = 0.0
        # L7 : vrai dès la détection d'un silence HL et jusqu'à ce que le flux reparte : les
        # moteurs ne posent ni ne modifient plus aucun ordre (MarketEngine.halt_fn).
        self._hl_halt = False
        self._global_cancel_retry_s = 1.0
        # fix r1 finding 2 : état à restaurer, par marché, à la réouverture (mémorisé juste avant
        # d'entrer en CLOSING/CLOSING_TAKER/FLAT).
        self._pre_exit_state: dict[str, str] = {}
        # fix r1 finding 6 : plus tôt instant (time.monotonic()) autorisé pour la prochaine
        # tentative de sortie taker, par marché.
        self._next_taker_ts: dict[str, float] = {}
        # fix r1 finding 14 : dernier q.ts déjà appliqué à l'EMA de basis, par marché (n'appliquer
        # une mise à jour qu'une fois par nouvelle cotation Var, pas à chaque tick()).
        self._last_basis_ts: dict[str, float] = {}
        # C1d : dernière lecture de la limite de débit HL par adresse (userRateLimit) et mode
        # "annulation seule" quand la marge restante passe sous hl_min_request_headroom.
        self.hl_rate: Optional[dict] = None
        self._cancel_only = False
        # I9 : prochain réarmement du dead-man switch (time.monotonic) et refus déjà signalé.
        self._next_deadman = 0.0
        self._deadman_refused = False
        # I4 : tâche qui exécute tick() en ce moment, et événement "aucun tick en cours".
        self._tick_task = None
        self._tick_idle = asyncio.Event()
        self._tick_idle.set()
        # I3d : marchés déjà signalés "position ouverte à la fermeture" pour la séance en cours.
        self._open_at_close_warned: set = set()
        # Étape de démarrage en cours (affichée par l'interface), None hors démarrage.
        self.progress = lambda step: None
        # Début de la session (création de l'orchestrateur) pour le PnL de session.
        self.session_started_at = time.time()
        # Étape d'arrêt en cours (arrêt propre ou d'urgence), affichage seulement : ne change
        # jamais l'ordre de l'arrêt.
        self.stop_progress = lambda step: None
        # D3 : instants (time.monotonic) de la dernière lecture de marge HL réussie et de la
        # dernière réduction forcée pour marge HL.
        self._margin_read_at = 0.0
        self._margin_reduced_at = -1.0
        # L2 : instant (time.monotonic) de la dernière réduction forcée par marché, et fin du
        # refroidissement en cours.
        self._forced_at: dict[str, float] = {}
        self._forced_cooldown_until: dict[str, float] = {}
        # L4 : une seule réconciliation à la fois.
        self._reconcile_lock = asyncio.Lock()

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc)

    def _book(self, coin):
        return self.hl_feed.book(coin) if self.hl_feed else (None, None)

    def _hl_dead(self) -> bool:
        # amendement t14 (fix) : silence GLOBAL = flux des fills coupé OU aucun message (l2Book ou
        # userFills, tous coins confondus) depuis hl_ws_stall_s. Remplace l'ancien `stalled()` par
        # coin (trop sensible à la cadence réelle, en rafales, du flux l2Book de ce dex : cf.
        # task-14-fix.md). time.time() (pas self._clock(), utilisé par les tests pour simuler des
        # dates de session) pour rester sur la même horloge que HLFeed (par défaut aussi time.time).
        if not self.hl_feed:
            return True
        return (not self.hl_feed.fills_alive()) or (time.time() - self.hl_feed.last_any_msg() > self.g.hl_ws_stall_s)

    def _mid_for(self, asset: str, eng: MarketEngine) -> float:
        # fix r1 finding 13 : à défaut de mid HL (carnet vide/websocket pas encore chaud), se
        # replier sur le mid Var puis sur le dernier prix de référence connu.
        hl_mid = self.hl_feed.mid(eng.cfg.hl_coin) if self.hl_feed else None
        if hl_mid:
            return hl_mid
        q = self.feed.latest(asset)
        if q:
            return q.mid
        return self.feed._ref.get(asset, 0.0)

    # -- prix Var par websocket (L2) et basis rapide (L3) --------------------
    def _on_var_ws(self, asset: str, upd: dict) -> None:
        """Rappel de VarPriceStream (boucle asyncio) à chaque prix websocket Var. Ne lève jamais :
        une exception ici couperait la connexion du flux à chaque message."""
        try:
            self._on_var_ws_update(asset, upd)
        except Exception as exc:
            self.journal.record_incident(asset, "warn", f"prix websocket Var ignoré : {type(exc).__name__}: {exc}")

    def _on_var_ws_update(self, asset: str, upd: dict) -> None:
        m = self.markets.get(asset)
        try:
            mark = float(upd["mark"])
            recv_s = float(upd["ts_recv_ms"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            return
        if m is None or mark <= 0:
            return
        srv = upd.get("ts_server_ms")
        server_s = float(srv) / 1000.0 if srv else None
        prev = self._ws_px.get(asset)
        self._ws_px[asset] = (mark, recv_s, server_s, prev[1] if prev else None)
        self._ws_last_px_s = recv_s if self._ws_last_px_s is None else max(self._ws_last_px_s, recv_s)
        hist = self._ws_hist.setdefault(asset, deque())
        hist.append((recv_s, mark))
        while hist and hist[0][0] < recv_s - _WS_HIST_S:
            hist.popleft()
        # L3 : basis rapide = mid bbo HL (frais) - prix websocket Var.
        if self.hl_feed is not None:
            hl_mid = self.hl_feed.mid(m.hl_coin)
            if hl_mid and self.hl_feed.book_age(m.hl_coin) <= _FAST_BASIS_HL_MAX_AGE_S:
                self.basis_fast[asset].update(hl_mid, mark, recv_s)
        if m.entry_mode == "lag_taker":
            self._wake.set()

    def _ws_price(self, asset: str) -> Optional[tuple[float, float]]:
        """(prix, âge en s) du dernier prix websocket Var de ce marché, ou None."""
        p = self._ws_px.get(asset)
        if p is None:
            return None
        return p[0], max(0.0, time.time() - p[1])

    def _ws_move_bps(self, asset: str, window_s: float) -> float:
        """L4 : variation (pb, log) du prix websocket Var contre le plus ancien prix des
        `window_s` dernières secondes."""
        hist = self._ws_hist.get(asset)
        if not hist:
            return 0.0
        t_last, px_last = hist[-1]
        ref = next((px for t, px in hist if t >= t_last - window_s), px_last)
        return (px_last / ref - 1.0) * 1e4 if ref > 0 else 0.0

    def _var_reference(self, a: str, m: MarketCfg, rfq):
        """L2 : (référence de décision, source, âge). Prix websocket ± demi-spread de la
        dernière cotation RFQ tant que les deux sont assez frais, hors mode maker ; sinon la
        cotation RFQ (règles d'âge actuelles)."""
        ws = self._ws_price(a)
        rfq_age = self.feed.age(a)
        if (m.entry_mode != "maker" and self.g.var_ws_enabled and ws is not None
                and ws[1] <= self.g.var_ws_max_age_s and rfq_age <= self.g.var_ws_spread_max_age_s):
            mark, age = ws
            # revue : l'âge qui compte pour le garde-fou « cotation périmée » est le plus grand
            # des deux (le demi-spread vient de la cotation RFQ).
            return ws_reference(rfq, mark, self._ws_px[a][1]), "ws", max(age, rfq_age)
        return rfq, "rfq", rfq_age

    def _lag_spread_bps(self, a: str, m: MarketCfg, rfq) -> float:
        """Revue : demi-spread Var (pb) retenu par lag_taker : max(dernière cotation RFQ, médiane
        des récentes) x lag_spread_mult."""
        if self._rfq_hs_ts.get(a) != rfq.ts:
            self._rfq_hs_ts[a] = rfq.ts
            self._rfq_hs.setdefault(a, deque(maxlen=20)).append(half_spread_rel(rfq) * 1e4)
        last = half_spread_rel(rfq) * 1e4
        hist = self._rfq_hs.get(a) or [last]
        return max(last, median(hist)) * m.lag_spread_mult

    def _check_var_ws(self) -> None:
        """L2 : alerte (une fois par épisode) si le websocket Var ne publie plus aucun prix depuis
        var_ws_silence_s, avec reconnexion forcée ; incident de reprise ensuite."""
        if self._var_ws_started_at is None:
            return
        now = time.time()
        last = self._ws_last_px_s if self._ws_last_px_s is not None else self._var_ws_started_at
        silent = now - last
        if silent > self.g.var_ws_silence_s:
            if not self._ws_muted:
                self._ws_muted = True
                self.journal.record_incident(
                    "*", "warn", f"flux de prix Var muet depuis {silent:.0f} s : repli sur les cotations RFQ")
            mono = time.monotonic()
            if mono >= self._ws_reconnect_at:
                self._ws_reconnect_at = mono + _WS_RECONNECT_S
                fr = getattr(self.var_ws, "force_reconnect", None)
                if callable(fr):
                    try:
                        fr()
                    except Exception:
                        pass
        elif self._ws_muted:
            self._ws_muted = False
            self.journal.record_incident("*", "info", "flux de prix Var rétabli")

    # -- limite de débit HL (C1d) ------------------------------------------
    def _market_hot(self, asset: str) -> bool:
        eng = self.engines.get(asset)
        return eng is not None and eng.state == QUOTING and eng.chase_hot()

    def cancel_only(self) -> bool:
        return self._cancel_only

    async def refresh_rate_limit(self) -> None:
        """Lit `userRateLimit` et bascule le mode "annulation seule" selon la marge restante. Une
        lecture en échec garde l'état précédent (la requête info ne coûte rien sur ce budget)."""
        try:
            rl = await self.hl.rate_limit()
        except Exception as exc:
            self.journal.record_incident("*", "warn", f"lecture de la limite de débit HL impossible : {exc}")
            return
        self.hl_rate = rl
        low = rl["headroom"] < self.g.hl_min_request_headroom
        if low and not self._cancel_only:
            self.journal.record_incident(
                "*", "error", f"limite de débit HL : marge {rl['headroom']} < {self.g.hl_min_request_headroom}, "
                              "annulation seule (aucun nouvel ordre)")
            # N3 : en entrant en annulation seule, retirer tous nos ordres (le dead-man switch
            # n'est plus renouvelé dans ce mode).
            try:
                await self.hl.cancel_all([m.hl_coin for m in self.markets.values()])
            except Exception as exc:
                self.journal.record_incident("*", "error", f"annulation globale (annulation seule) échouée : {exc}")
            else:
                for e in self.engines.values():
                    e.clear_orders()
        elif not low and self._cancel_only:
            self.journal.record_incident("*", "warn", f"limite de débit HL : marge {rl['headroom']}, reprise des ordres")
        self._cancel_only = low

    # -- initialisation ----------------------------------------------------
    def _load_strikes(self) -> None:
        """I6 : recharger les strikes Var des dernières 24 h et persister les suivants."""
        lim = getattr(self.var, "limiter", None)
        if lim is None or not hasattr(lim, "load_strikes"):
            return
        try:
            lim.load_strikes(self.journal.strikes_since(time.time() - 86400))
        except Exception as exc:
            self.journal.record_incident("*", "warn", f"strikes Var non rechargés : {exc}")
        lim.on_strike = self.journal.record_strike

    async def setup(self) -> None:
        self._load_strikes()
        self.progress("Lecture des marchés et des positions")
        meta, assets, mids, hl_pos, var_pos = await asyncio.gather(
            asyncio.to_thread(self.hl_client.load_meta), self.var.supported_assets(),
            self.hl.mids(), self.hl.positions(), self.var.positions())
        self.progress("Annulation des ordres restants")
        await self._cancel_stale_orders()
        self.progress("Préparation des 6 marchés")
        lev_jobs = []
        for a, m in self.markets.items():
            cm = meta.get(m.hl_coin)
            fee = m.hl_maker_fee_bps if m.hl_maker_fee_bps is not None else \
                await asyncio.to_thread(self.hl_client.maker_fee_bps, m.hl_coin)
            taker_fee = None
            if hasattr(self.hl_client, "taker_fee_bps"):
                try:
                    taker_fee = await asyncio.to_thread(self.hl_client.taker_fee_bps, m.hl_coin)
                except Exception:
                    taker_fee = None
            eng = MarketEngine(m, self.g, self.hl, self.var, self.journal, self.gate,
                               sz_decimals=cm.sz_decimals if cm else 2, fee_bps=fee, taker_fee_bps=taker_fee)
            eng.cancel_only_fn = self.cancel_only
            eng.kill_fn = lambda: self.killed
            eng.halt_fn = lambda: self._hl_halt
            # F1/F3 : cotation fraîche à la demande et échec du dernier rafraîchissement.
            eng.fresh_quote_fn = lambda a=a: self.feed.request_fresh(a)
            eng.quote_refresh_failed_fn = lambda a=a: self.feed.refresh_failed(a)
            # L4 : en paper, un IOC lag_taker ne produit aucun fill userFills (simulé par le moteur).
            eng.paper = bool(getattr(self.hl_client, "paper", False))
            self.engines[a] = eng
            entry = (assets.get(a) or [{}])[0]
            self.sessions[a] = parse_sessions(entry)
            var_px = float(entry.get("price", 0) or 0)
            hl_mid = mids.get(m.hl_coin, 0.0)
            if cm is None or var_px <= 0 or hl_mid <= 0 or abs(hl_mid / var_px - 1) > 0.01:
                # amendement t12 §1 : block() est asynchrone, et journalise déjà l'incident.
                await eng.block(f"contrôle d'unités échoué (HL {hl_mid}, Var {var_px})")
                continue
            if not m.enabled:
                eng.state = IDLE
            self.feed.set_ref_price(a, var_px)
            eng.pos_hl = float(hl_pos.get(m.hl_coin, 0.0))
            eng.pos_var = float((var_pos.get(a) or {}).get("qty", 0.0))
            # I5 (spec §6 "Démarrage") : aucune cotation tant qu'un delta existant n'est pas
            # expliqué. Le marché démarre BLOQUÉ ; l'opérateur vérifie puis clique "Débloquer".
            delta_usd = (eng.pos_hl + eng.pos_var) * hl_mid
            if abs(delta_usd) >= _STARTUP_DELTA_FRAC * m.fill_usd:
                await eng.block(STARTUP_DELTA_REASON + f" ({delta_usd:.0f} $)")
            # L1 : levier Var borné au maximum de l'actif ; échec = marché BLOQUÉ.
            lev_jobs.append((a, m, eng, cm))
        # Réglage des leviers en parallèle sur les 6 marchés (le démarrage séquentiel prenait
        # environ 20 s) : les appels Var restent sérialisés par le limiteur, ceux de HL par le
        # verrou de HLClient, mais les deux venues avancent en même temps.
        self.progress("Réglage des leviers Var et HL")
        await asyncio.gather(*(self._setup_leverage(*job) for job in lev_jobs))
        self.progress("Lecture du quota de requêtes HL")
        await self.refresh_rate_limit()
        self.progress(None)

    async def _setup_leverage(self, a: str, m: MarketCfg, eng, cm) -> None:
        var_lev = await self._set_var_leverage(a, m.var_leverage)
        if var_lev is None:
            await eng.block(VAR_LEVERAGE_REASON)
            var_lev = m.var_leverage
        try:
            await asyncio.to_thread(self.hl_client.set_cross_leverage, m.hl_coin,
                                    min(var_lev, cm.max_leverage))
        except Exception as exc:
            self.journal.record_incident(a, "warn", f"levier HL non réglé : {exc}")

    async def _cancel_stale_orders(self) -> None:
        """Au démarrage, annule les ordres HL déjà ouverts sur nos coins (restes d'un run
        précédent interrompu : le 23/09, des bots orphelins avaient été coupés sans annuler)."""
        if not hasattr(self.hl_client, "open_orders"):
            return
        coins = [m.hl_coin for m in self.markets.values()]
        try:
            stale = [o for o in await asyncio.to_thread(self.hl_client.open_orders) if o.get("coin") in coins]
        except Exception as exc:
            self.journal.record_incident("*", "warn", f"lecture des ordres ouverts au démarrage impossible : {exc}")
            return
        if not stale:
            return
        self.journal.record_incident("*", "warn", f"{len(stale)} ordre(s) HL restant(s) d'un run précédent : annulation")
        try:
            await self.hl.cancel_all(coins)
        except Exception as exc:
            self.journal.record_incident("*", "error", f"annulation des ordres restants impossible : {exc}")

    async def _set_var_leverage(self, a: str, lev: int) -> Optional[int]:
        """L1 : règle le levier Var. Sur un 422 « range max N » (seul format connu pour le
        maximum, le 23/09 sur les pétroles), nouvel essai avec N. Renvoie le levier retenu, ou
        None si le réglage a échoué (incident journalisé)."""
        try:
            await self.var.set_leverage(a, lev)
            return lev
        except VarError as exc:
            cap = parse_max_leverage(getattr(exc, "body", None))
            if cap is None or cap < 1 or cap >= lev:
                self.journal.record_incident(a, "error", f"{VAR_LEVERAGE_REASON} : {exc}")
                return None
        try:
            await self.var.set_leverage(a, cap)
        except VarError as exc:
            self.journal.record_incident(a, "error", f"{VAR_LEVERAGE_REASON} (maximum {cap}) : {exc}")
            return None
        self.journal.record_incident(a, "warn", f"levier Var borné au maximum de l'actif : {lev} -> {cap}")
        return cap

    # -- fills ---------------------------------------------------------------
    def on_fill(self, fill: dict) -> None:
        for a, m in self.markets.items():
            if m.hl_coin == fill["coin"]:
                # amendement t12 §5 : get_running_loop() (on tourne déjà dans la boucle event-loop
                # du feed HL) et référence gardée dans self._fill_tasks tant que la tâche est en vol.
                t = asyncio.get_running_loop().create_task(self.engines[a].on_fill(fill))
                self._fill_tasks.add(t)
                t.add_done_callback(self._fill_tasks.discard)

    # -- boucle de cotation ---------------------------------------------------
    async def tick(self) -> None:
        # I4 : kill() attend la fin du tick en cours avant son annulation globale finale.
        self._tick_task = asyncio.current_task()
        self._tick_idle.clear()
        try:
            await self._tick_body()
        finally:
            self._tick_task = None
            self._tick_idle.set()

    def _orders_at_risk(self) -> bool:
        """N3 : vrai si un moteur a un ordre au repos ou une annulation non confirmée."""
        return any(any(o is not None for o in e.orders.values()) or e._cancel_failed
                   for e in self.engines.values())

    async def _deadman(self) -> None:
        """I9/N2 : dead-man switch HL. Réarme scheduleCancel(maintenant + deadman_window_s) toutes
        les deadman_renew_s : si le bot se fige ou perd le réseau, HL annule lui-même nos ordres.
        N3 : pas de renouvellement en annulation seule ni sans ordre au repos (économie du budget
        de requêtes) ; le prochain ordre posé déclenche le réarmement au tick suivant."""
        mono = time.monotonic()
        if mono < self._next_deadman:
            return
        if self._cancel_only or not self._orders_at_risk():
            return
        try:
            await self.hl.schedule_cancel(int(time.time() * 1000) + int(self.g.deadman_window_s * 1000))
        except HLRefused as exc:
            # N2 : refus explicite de HL (status "err") : pause de 10 min, incident une fois.
            self._next_deadman = mono + _DEADMAN_RETRY_AFTER_REFUSAL_S
            if not self._deadman_refused:
                self._deadman_refused = True
                self.journal.record_incident("*", "warn", f"scheduleCancel indisponible : {exc}")
            return
        except Exception as exc:
            # N2 : panne (réseau, SDK...) : nouvel essai à la prochaine cadence.
            self._next_deadman = mono + self.g.deadman_renew_s
            self.journal.record_incident("*", "warn", f"scheduleCancel en échec, nouvel essai : {type(exc).__name__}: {exc}")
            return
        self._deadman_refused = False
        self._next_deadman = mono + self.g.deadman_renew_s

    def _has_position(self, eng: MarketEngine) -> bool:
        return abs(eng.pos_hl) > 1e-12 or abs(eng.pos_var) > 1e-12

    def _var_mid(self, a: str) -> Optional[float]:
        """Mid Var corrigé du basis (même unité que le prix HL), repli de prix des sorties."""
        q = self.feed.latest(a)
        if q is None:
            return None
        return q.mid + ((self.last_prices.get(a) or {}).get("basis") or 0.0)

    async def _taker_exit(self, a: str, eng: MarketEngine, hl_dead: bool) -> bool:
        """Sortie taker d'un marché avec les garde-fous communs (session Var vivante, flux HL
        vivant, backoff par marché). Vrai si une tentative a eu lieu."""
        if not self.session_ok or hl_dead or time.monotonic() < self._next_taker_ts.get(a, 0.0):
            return False
        # L3 : sortie déjà en cours, ou IOC de sortie pas encore confirmé par une relecture.
        if eng.exit_inflight or eng.exit_reread_pending():
            return False
        b, k = self._book(eng.cfg.hl_coin)
        await eng.flatten(True, b, k, self._var_mid(a))
        delay = _TAKER_RETRY_AFTER_VAR_UNCONFIRMED_S if eng.pos_var != 0 else _TAKER_RETRY_S
        self._next_taker_ts[a] = time.monotonic() + delay
        return True

    async def _killed_exits(self, hl_dead: bool) -> None:
        """I3d : après un kill, les sorties partielles sont réessayées à chaque tick (backoff)."""
        for a, eng in self.engines.items():
            try:
                eng.book = self._book(eng.cfg.hl_coin)
                await eng.retry_pending_cancels()
                if self._has_position(eng):
                    await self._taker_exit(a, eng, hl_dead)
                eng.state = IDLE
            except Exception as exc:
                self.journal.record_incident(a, "error", f"sortie après arrêt d'urgence : {type(exc).__name__}: {exc}")

    async def _tick_body(self) -> None:
        await self._deadman()
        self._check_var_ws()
        if self.killed:
            await self._killed_exits(self._hl_dead())
            return
        now = self._now()
        daily = self.journal.daily_pnl()
        if daily <= -self.g.max_daily_loss_usd:
            await self.kill("perte journalière max")
            return

        # fix r1 finding 1 : flux HL muet (websocket ou fills) -> annulation globale au niveau
        # exchange, une seule fois par épisode de silence, puis on suspend la recotation jusqu'à
        # ce qu'une réconciliation réussisse.
        hl_dead = self._hl_dead()
        # L7 : plus aucun placement ni modify dès la détection, avant l'annulation globale.
        self._hl_halt = hl_dead
        if hl_dead:
            if not self._hl_silence_active:
                coins = [m.hl_coin for m in self.markets.values()]
                # L7 : jusqu'à 3 nouveaux essais immédiats, espacés de 1 s (le 23/09, 3 échecs
                # sur 3 ticks, 18 s d'ordres au carnet sans flux de prix).
                last_exc = None
                for attempt in range(1 + _GLOBAL_CANCEL_RETRIES):
                    if attempt:
                        await asyncio.sleep(self._global_cancel_retry_s)
                    try:
                        await self.hl.cancel_all(coins)
                    except Exception as exc:
                        last_exc = exc
                        continue
                    last_exc = None
                    break
                if last_exc is not None:
                    exc = last_exc
                    # fix r2 A : l'annulation globale a échoué -> ne PAS marquer l'épisode actif ni
                    # vider les carnets des moteurs (on ne sait pas ce qui est réellement annulé
                    # côté exchange) ; repli moteur par moteur (protégé), et nouvel essai de
                    # l'annulation globale au tick suivant tant que le silence persiste.
                    self.journal.record_incident(
                        "*", "error", f"annulation globale HL échouée ({1 + _GLOBAL_CANCEL_RETRIES} essais), "
                                      f"repli moteur par moteur : {exc}")
                    for e in self.engines.values():
                        try:
                            await e.cancel_all()
                        except Exception as exc2:
                            self.journal.record_incident(
                                e.cfg.var_asset, "error",
                                f"annulation de repli échouée : {type(exc2).__name__}: {exc2}")
                else:
                    self._hl_silence_active = True
                    self._need_reconcile = True
                    for e in self.engines.values():
                        e.clear_orders()
                    self.journal.record_incident(
                        "*", "error",
                        "flux HL muet : annulation globale, recotation suspendue jusqu'à réconciliation")
        else:
            if self._hl_silence_active:
                # fix r2 B : mémoriser la fin de CET épisode de silence (pas juste "flux vivant").
                self._silence_ended_at = time.monotonic()
            self._hl_silence_active = False

        gross_hl = sum(abs(e.pos_hl) * self._mid_for(a, e) for a, e in self.engines.items())
        gross_var = sum(abs(e.pos_var) * ((self.feed.latest(a).mid if self.feed.latest(a) else 0))
                        for a, e in self.engines.items())

        for a, eng in self.engines.items():
            # amendement t12 §5 : une exception sur un marché ne doit jamais empêcher les autres
            # de coter.
            try:
                await self._tick_market(a, eng, now, daily, hl_dead, gross_hl, gross_var)
            except Exception as exc:
                self.journal.record_incident(a, "error", f"tick : {type(exc).__name__}: {exc}")

    async def _tick_market(self, a: str, eng: MarketEngine, now: datetime, daily: float, hl_dead: bool,
                           gross_hl: float, gross_var: float) -> None:
        m = eng.cfg
        # amendement t12 §2 : mettre à jour eng.book en tout premier, avant toute autre action pour
        # ce marché (le moteur s'en sert pour ses débouclages IOC).
        eng.book = self._book(m.hl_coin)
        hl_bid, hl_ask = eng.book
        # I2 : réessayer les annulations HL non confirmées, quel que soit l'état du marché.
        await eng.retry_pending_cancels()
        phase = phase_at(now, self.sessions.get(a, []), self.g)
        if phase in (CLOSING, CLOSING_TAKER, FLAT):
            # amendement t12 §6 : un marché BLOQUÉ n'est jamais débouclé automatiquement (attente
            # d'une intervention manuelle).
            if eng.state == BLOCKED:
                return
            # fix r1 finding 2 : un marché IDLE sans position ne fait pas de flatten, juste cancel.
            if eng.state == IDLE and eng.pos_hl == 0 and eng.pos_var == 0:
                await eng.cancel_all()
                return
            taker = phase != CLOSING
            if taker:
                # fix r1 finding 3 : session Var morte -> jamais de sortie taker (fermerait HL en
                # laissant Var nu). Seulement annuler et journaliser, intervention manuelle.
                if not self.session_ok:
                    await eng.cancel_all()
                    self.journal.record_incident(
                        a, "error", "session Var tombée : positions conservées, intervention manuelle")
                    return
                # fix r1 finding 6 : pas de sortie taker tant que le flux HL est muet, et backoff
                # par marché entre deux tentatives.
                if hl_dead:
                    await eng.cancel_all()
                    return
                if time.monotonic() < self._next_taker_ts.get(a, 0.0):
                    await eng.cancel_all()
                    return
                # L3 : pas de nouvelle sortie taker avant la relecture qui suit un IOC de sortie.
                if self._has_position(eng) and (eng.exit_inflight or eng.exit_reread_pending()):
                    await eng.cancel_all()
                    return
            # fix r1 finding 2 : mémoriser l'état pré-fermeture avant de flatten (une seule fois
            # par épisode de fermeture : setdefault).
            self._pre_exit_state.setdefault(a, eng.state)
            # I3a : la sortie taker relit les positions réelles ; inutile (et coûteux en requêtes
            # Var) quand le cache, tenu à jour par les fills et la réconciliation, est déjà à plat.
            await eng.flatten(taker and self._has_position(eng), hl_bid, hl_ask, self._var_mid(a))
            if taker:
                # fix r1 finding 6 : pos_var encore non nul après un flatten taker = sortie Var non
                # confirmée (engine.flatten() ne le remet à 0 que si elle l'est) -> backoff long.
                delay = _TAKER_RETRY_AFTER_VAR_UNCONFIRMED_S if eng.pos_var != 0 else _TAKER_RETRY_S
                self._next_taker_ts[a] = time.monotonic() + delay
            return
        if phase == CLOSED:
            # fix r2 E : dépiler et restaurer l'état mémorisé AVANT cancel_all() (QUOTING seulement
            # s'il était QUOTING ; PAUSED/IDLE restent tels quels) : si cancel_all() lève ensuite,
            # l'état est déjà correctement restauré (pas bloqué en EXITING/pas forcé à QUOTING).
            pre = self._pre_exit_state.pop(a, None)
            if eng.state == EXITING:
                eng.state = pre if pre is not None else QUOTING
            # I3d : une position encore ouverte marché fermé = alerte, une fois par séance.
            if self._has_position(eng) and a not in self._open_at_close_warned:
                self._open_at_close_warned.add(a)
                self.journal.record_incident(
                    a, "error", f"position ouverte à la fermeture (HL {eng.pos_hl}, Var {eng.pos_var})")
            await eng.cancel_all()
            return
        self._open_at_close_warned.discard(a)
        if eng.state == EXITING:
            eng.state = QUOTING
        if eng.state in (IDLE, BLOCKED):
            return
        # L2 : fin du refroidissement après une réduction forcée : reprise de la cotation si le
        # marché est à plat et toujours dans la PAUSE posée par cette réduction.
        until = self._forced_cooldown_until.get(a)
        if until is not None and time.monotonic() >= until and not self._has_position(eng):
            self._forced_cooldown_until.pop(a, None)
            if eng.state == PAUSED and eng.last_reason and eng.last_reason.startswith(FORCED_PAUSE_REASON):
                eng.resume()
        # S2 (spec §6) : marge HL tendue ou liquidation Var trop proche -> en plus de bloquer la
        # cotation (quote_block_reason), réduire les deux jambes du marché concerné (sortie taker,
        # avec le backoff des sorties). Marge HL (cross, globale) : le marché au plus gros brut HL.
        force = self._forced_reduction_reason(a, eng)
        if force is not None:
            prior = eng.state
            if await self._taker_exit(a, eng, hl_dead):
                self.journal.record_incident(a, "error", f"réduction forcée des deux jambes : {force}")
                if force.startswith("marge HL"):
                    self._margin_reduced_at = time.monotonic()
                # la sortie laisse le marché en SORTIE : restaurer l'état d'avant (un marché en
                # PAUSE ne doit pas repasser en cotation à cause de cette réduction).
                if eng.state == EXITING:
                    eng.state = prior
                await self._after_forced_reduction(a, eng, prior, force)
            return
        # fix r1 finding 1 : pas de recotation tant qu'une réconciliation n'a pas confirmé les
        # positions après un épisode de silence HL.
        if self._need_reconcile:
            return
        rfq = self.feed.latest(a)
        if rfq is None or eng.state != QUOTING:
            return
        # L2 : référence de décision (prix websocket Var quand il est frais, sinon RFQ).
        q, src, var_age = self._var_reference(a, m, rfq)
        self._var_ref_src[a] = src
        hl_mid = self.hl_feed.mid(m.hl_coin) if self.hl_feed else None
        basis = 0.0
        dev = 0.0
        if m.basis_mode == "ema" and hl_mid:
            ema = self.basis[a]
            dev = ema.deviation_bps(hl_mid, rfq.mid)
            # fix r1 finding 14 : ne mettre à jour l'EMA qu'une fois par nouvelle cotation Var
            # (q.ts inchangé -> tick() rejoué entre deux polls du VarFeed).
            if self._last_basis_ts.get(a) != rfq.ts:
                ema.update(hl_mid, rfq.mid, rfq.ts)
                self._last_basis_ts[a] = rfq.ts
            basis = ema.value or 0.0
        fast = self.basis_fast[a].value
        fast_warm = fast is not None and self.basis_fast[a].warm_s() >= m.basis_fast_tau_s
        # (lag_taker : le moteur refuse toute prise tant que le basis rapide n'est pas chauffé.)
        if src == "ws" and fast is not None and ((m.basis_mode == "ema" and fast_warm)
                                                 or m.entry_mode == "lag_taker"):
            # L3 : décision des pétroles sur le basis rapide ; L4 : la prise sur retard corrige
            # le prix juste Var du basis rapide sur tous les marchés (comme tools/leadlag.py). Le
            # garde-fou basis_guard_bps reste sur l'EMA lente (dev ci-dessus).
            basis = fast
        sane = bool(hl_mid) and sanity_ok(hl_mid, q.mid + basis, m.sanity_bps)
        # amendement t14 (fix) : garde-fou PAR MARCHÉ, distinct du silence global `hl_dead` déjà
        # géré plus haut (cancel_all + _need_reconcile) : un carnet périmé pour CE coin seulement
        # bloque juste ce marché (pas d'annulation exchange globale).
        book_stale = self.hl_feed.book_age(m.hl_coin) > self.g.hl_book_max_age_s if self.hl_feed else True

        def _reason(age: float) -> Optional[str]:
            return quote_block_reason(
                var_age=age, hl_stalled=hl_dead,
                fills_alive=self.hl_feed.fills_alive() if self.hl_feed else False,
                strikes_suspended=self.var.limiter.quotes_suspended(), sanity=sane, basis_dev_bps=dev, cfg=m,
                g=self.g, roll_window=is_roll_window(now.date()), daily_pnl=daily, gross_hl_usd=gross_hl,
                gross_var_usd=gross_var, hl_margin_ratio=self.hl_margin, var_liq_distance=self.var_liq.get(a),
                session_ok=self.session_ok, book_stale=book_stale)

        reason = _reason(var_age)
        # L8 : cotation seulement périmée (aucun autre motif) et plus jeune que quote_cancel_age_s :
        # garder les ordres au repos, ne rien reposer.
        hold = (reason == STALE_QUOTE_REASON and var_age <= self.g.quote_cancel_age_s
                and _reason(0.0) is None)
        lag = None
        if m.entry_mode == "lag_taker":
            # L4 : âges du prix websocket Var et du bbo HL, mouvement récent du prix Var.
            wp = self._ws_px.get(a)
            ws_recv = wp[1] if wp else None
            rfq_age = self.feed.age(a)
            lag = {"src": src, "ws_age": (time.time() - ws_recv) if (src == "ws" and ws_recv) else None,
                   # revue : retard de transport (réception - horodatage serveur) et écart avec le
                   # prix websocket précédent de ce marché.
                   "ws_transport_s": (wp[1] - wp[2]) if (wp and wp[2] is not None) else None,
                   "ws_prev_gap_s": (wp[1] - wp[3]) if (wp and wp[3] is not None) else None,
                   "rfq_age": rfq_age,
                   "spread_ok": rfq_age <= self.g.var_ws_spread_max_age_s,
                   "hs_bps": self._lag_spread_bps(a, m, rfq),
                   "book_sizes": self.hl_feed.book_sizes(m.hl_coin)
                   if self.hl_feed and hasattr(self.hl_feed, "book_sizes") else (None, None),
                   "bbo_age": self.hl_feed.book_age(m.hl_coin) if self.hl_feed else None,
                   "move_bps": self._ws_move_bps(a, m.lag_move_window_s),
                   # basis rapide chauffé sur au moins sa constante de temps : sinon sa première
                   # valeur (un seul écart instantané) pourrait contenir le retard lui-même.
                   "basis_ready": fast is not None and self.basis_fast[a].warm_s() >= m.basis_fast_tau_s}
        await eng.requote(q, basis, hl_bid, hl_ask, 2.0 if phase == WIDE else 1.0, reason, hold=hold, rfq=rfq,
                          lag=lag)
        self.last_prices[a] = {"basis": basis}

    async def _after_forced_reduction(self, a: str, eng: MarketEngine, prior: str, force: str) -> None:
        """L2 : un marché qui cotait (ou couvrait) passe en PAUSE avec refroidissement ; une 2e
        réduction forcée en moins de 30 min le passe en BLOQUÉ. Un nouvel essai de sortie pendant
        la PAUSE (sortie partielle) n'est pas une récidive."""
        if prior not in (QUOTING, HEDGING):
            return
        now = time.monotonic()
        last = self._forced_at.get(a)
        self._forced_at[a] = now
        if last is not None and now - last < _FORCED_REPEAT_WINDOW_S:
            self._forced_cooldown_until.pop(a, None)
            await eng.block(f"2e réduction forcée en moins de 30 min ({force})")
            return
        self._forced_cooldown_until[a] = now + self.g.forced_reduction_cooldown_s
        await eng.pause(f"{FORCED_PAUSE_REASON} {self.g.forced_reduction_cooldown_s / 60:.0f} min ({force})")
        self.journal.record_incident(
            a, "warn", f"marché en pause {self.g.forced_reduction_cooldown_s / 60:.0f} min après réduction forcée")

    def _forced_reduction_reason(self, a: str, eng: MarketEngine) -> Optional[str]:
        if not self._has_position(eng):
            return None
        liq = self.var_liq.get(a)
        if liq is not None and liq < self.g.var_liq_distance_min:
            return f"liquidation Var trop proche ({liq:.1%})"
        if self.hl_margin > self.g.hl_margin_ratio_max:
            # D3 : après une réduction pour marge HL, attendre une nouvelle lecture de marge
            # avant de choisir un autre marché (la marge lue est antérieure à la réduction).
            if self._margin_reduced_at >= self._margin_read_at:
                return None
            largest = max(self.engines.items(), key=lambda kv: abs(kv[1].pos_hl) * self._mid_for(kv[0], kv[1]))[0]
            if largest == a:
                return f"marge HL tendue ({self.hl_margin:.0%})"
        return None

    # -- réconciliation -------------------------------------------------------
    async def reconcile(self) -> None:
        # L4 : sérialisée (deux réconciliations qui se chevauchent pourraient corriger deux fois
        # le même delta).
        async with self._reconcile_lock:
            await self._reconcile()

    async def _reconcile(self) -> None:
        # fix r2 B : horodater le DÉBUT de cette réconciliation (avant toute lecture), pour ne
        # lever _need_reconcile que si elle a commencé strictement après la fin du dernier épisode
        # de silence HL connu (sinon une réconciliation entamée pendant le silence, ou avant que le
        # flux ne reparte, pourrait lever le drapeau sur la base de positions déjà obsolètes).
        started_at = time.monotonic()
        # C1d : limite de débit HL relue toutes les reconcile_interval_s (10 s par défaut).
        await self.refresh_rate_limit()
        read_at = time.monotonic()
        try:
            hl_pos = await self.hl.positions()
            var_pos = await self.var.positions()
            self.hl_margin = await self.hl.margin_ratio()
            self._margin_read_at = time.monotonic()
            self.var_balance = (await self.var.portfolio())["balance"]
        except Exception as exc:
            self.journal.record_incident("*", "warn", f"réconciliation impossible : {exc}")
            return
        # fix r1 finding 1 / fix r2 B : une lecture de positions réussie lève la suspension de
        # cotation posée après un épisode de silence HL, seulement si elle a commencé après la fin
        # de ce silence ET qu'aucun nouvel épisode n'est en cours.
        if not self._hl_silence_active and started_at > self._silence_ended_at:
            self._need_reconcile = False
        for a, eng in self.engines.items():
            try:
                await self._reconcile_market(a, eng, hl_pos, var_pos, read_at)
            except Exception as exc:
                self.journal.record_incident(a, "error", f"réconciliation : {type(exc).__name__}: {exc}")

    async def _reconcile_market(self, a: str, eng: MarketEngine, hl_pos: dict, var_pos: dict,
                                read_at: float = 0.0) -> None:
        m = eng.cfg
        real_hl = float(hl_pos.get(m.hl_coin, 0.0))
        vp = var_pos.get(a) or {}
        real_var = float(vp.get("qty", 0.0))
        mid = self._mid_for(a, eng)
        liq = vp.get("liq")
        self.var_liq[a] = abs(liq / mid - 1) if liq and mid else None
        # amendement t12 §4 : ne jamais écraser pos_hl/pos_var pendant qu'une couverture de ce
        # marché est en vol, ni pendant qu'un flatten (de n'importe quel marché, le verrou est
        # partagé) tient self.gate.lock.
        busy = eng.hedge_inflight or self.gate.lock.locked()
        if not busy:
            eng.pos_hl, eng.pos_var = real_hl, real_var
            eng.positions_read_at = read_at
            # L6 : positions réelles à plat sur les deux jambes et aucune couverture en cours :
            # plus rien à couvrir (le 23/09, UKOILP est resté avec pending_qty = 5,28 à plat).
            if real_hl == 0 and real_var == 0 and eng.pending_qty != 0:
                self.journal.record_incident(
                    a, "warn", f"quantité en attente {eng.pending_qty} remise à 0 (positions réelles à plat)")
                eng.pending_qty = 0.0
                eng._pending_t = 0.0
        delta_usd = (real_hl + real_var) * mid
        # fix r1 finding 7 : `busy` (pas seulement hedge_inflight de ce marché) passé à observe().
        action = self.tracker.observe(a, delta_usd, True, busy, m.fill_usd, m.max_delta_usd)
        if action == "block":
            await eng.cancel_all()
            await eng.block(f"delta anormal {delta_usd:.0f} $")
            return
        if action != "correct":
            return
        # fix r1 finding 9 : pas de correction automatique pour un marché BLOQUÉ ou IDLE, seulement
        # observer et journaliser.
        if eng.state in (BLOCKED, IDLE):
            self.journal.record_incident(
                a, "warn", f"delta observé sans correction (marché {eng.state}) : {delta_usd:.0f} $")
            return
        self.tracker.mark_corrected(a)
        await self._apply_correction(a, eng, m, delta_usd, mid)

    async def _apply_correction(self, a: str, eng: MarketEngine, m: MarketCfg, delta_usd: float, mid: float) -> None:
        # fix r1 finding 7/8 : correction (Var ET HL) posée sous le verrou du gate, jamais en même
        # temps qu'une couverture/sortie d'un marché ; on relit HL et Var sous le verrou et on
        # recalcule le delta avant d'agir, sans mise à jour optimiste préalable.
        async with self.gate.lock:
            if eng.pending_qty != 0:
                self.journal.record_incident(a, "warn", "correction annulée : couverture en attente")
                return
            try:
                hl_pos2 = await self.hl.positions_detail()
                var_pos2 = await self.var.positions()
            except Exception as exc:
                self.journal.record_incident(a, "warn", f"correction annulée : relecture impossible ({exc})")
                return
            h2 = hl_pos2.get(m.hl_coin) or {}
            r_hl = float(h2.get("szi", 0.0) or 0.0)
            vp2 = var_pos2.get(a) or {}
            r_var = float(vp2.get("qty", 0.0))
            new_delta = (r_hl + r_var) * mid
            if abs(delta_usd) > 1e-9 and abs(new_delta - delta_usd) > 0.2 * abs(delta_usd):
                self.journal.record_incident(
                    a, "warn", f"correction annulée : delta changé ({delta_usd:.0f} $ -> {new_delta:.0f} $)")
                return
            qty = abs(r_hl + r_var)
            if qty <= 0:
                return
            # fix r2 C : revérifier pending_qty juste avant d'envoyer l'ordre de correction (un fill
            # concurrent a pu le modifier entre la vérification initiale et ici, même sous le
            # verrou : on_fill() écrit pending_qty sans le prendre).
            if eng.pending_qty != 0:
                self.journal.record_incident(a, "warn", "correction annulée : couverture en attente")
                return
            # L4 : état revérifié sous le verrou, juste avant l'envoi : le marché a pu passer
            # BLOQUÉ (couverture incertaine...) pendant qu'on attendait le verrou.
            if eng.state in (BLOCKED, IDLE):
                self.journal.record_incident(a, "warn", f"correction annulée : marché {eng.state}")
                return
            if abs(r_var) > abs(r_hl):
                side = "sell" if r_var > 0 else "buy"
                try:
                    q = await self.var.quote(a, m.var_kind, qty, prio=EXIT)
                    await self.var.market(q.quote_id, side, self.g.var_max_slippage, reduce_only=True, prio=EXIT)
                except VarError as exc:
                    await eng.block(f"correction Var échouée : {exc}")
                    return
                # I8 : PnL réalisé estimé contre le prix d'entrée moyen Var.
                var_px = q.bid if side == "sell" else q.ask
                entry = vp2.get("entry")
                sv = 1.0 if r_var > 0 else -1.0
                pnl = sv * (var_px - entry) * qty if entry else 0.0
                self.journal.record_arb(a, "", None, 0.0, 0.0, side, var_px, qty,
                                        pnl / max(1e-12, qty * var_px) * 1e4, pnl, 0.0, "correction")
            else:
                sell = r_hl > 0
                bid, ask = eng.book
                raw_px = (bid or mid) * 0.995 if sell else (ask or mid) * 1.005
                px = round_px(raw_px, eng.sz_decimals, not sell)
                sz = round_sz(qty, eng.sz_decimals)
                # I3b : correction en reduce-only.
                res = await self.hl.ioc(m.hl_coin, not sell, sz, px, reduce_only=True)
                # fix r1 finding 12 (minor) : journaliser le résultat réel de l'IOC.
                self.journal.record_incident(
                    a, "warn", f"correction HL IOC : filled={res.get('filled_sz')} err={res.get('error')}")
                filled = float(res.get("filled_sz") or 0.0)
                if filled > 0:
                    fpx = float(res.get("avg_px") or px)
                    entry = h2.get("entry")
                    fee = eng.taker_fee_bps / 1e4 * filled * fpx
                    pnl = ((fpx - entry) if sell else (entry - fpx)) * filled if entry else 0.0
                    pnl -= fee
                    self.journal.record_arb(a, "A" if sell else "B", fpx, filled, fee, "", None, 0.0,
                                            pnl / max(1e-12, filled * fpx) * 1e4, pnl, 0.0, "correction")
            self.journal.record_incident(a, "warn", f"delta corrigé ({delta_usd:.0f} $)")
            # I3e : positions du moteur relues après la correction.
            try:
                hl_pos3 = await self.hl.positions()
                var_pos3 = await self.var.positions()
                eng.pos_hl = float(hl_pos3.get(m.hl_coin, 0.0))
                eng.pos_var = float((var_pos3.get(a) or {}).get("qty", 0.0))
            except Exception as exc:
                self.journal.record_incident(a, "warn", f"relecture après correction impossible : {exc}")

    # -- santé de la session Var ------------------------------------------------
    async def _health_once(self) -> None:
        # fix r1 finding 10 : une exception de var.health() = session considérée inactive, jamais
        # une exception qui remonte et tue la boucle.
        try:
            ok = await self.var.health()
        except Exception as exc:
            ok = False
            self.journal.record_incident("*", "warn", f"health() a levé une exception : {type(exc).__name__}: {exc}")
        if not ok and self.session_ok:
            self.journal.record_incident("*", "error", "session Var tombée : reconnectez WalletConnect")
            for e in self.engines.values():
                try:
                    await e.cancel_all()
                except Exception as exc:
                    self.journal.record_incident(
                        e.cfg.var_asset, "warn",
                        f"annulation HL échouée après perte de session : {type(exc).__name__}: {exc}")
        self.session_ok = ok

    async def health_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._health_once()
            await asyncio.sleep(30)

    # -- arrêt d'urgence ---------------------------------------------------------
    async def kill(self, reason: str, emergency: bool = True) -> None:
        """Annule tous les ordres HL et ferme les deux jambes de chaque marché (Var d'abord, puis
        HL en IOC reduce-only). `emergency=False` : arrêt normal (close_for_stop), même sortie ;
        un arrêt d'urgence reste marqué comme tel même si un arrêt normal suit."""
        self.emergency = emergency or (self.killed and self.emergency)
        self.killed, self.kill_reason = True, reason
        if emergency:
            self.journal.record_incident("*", "error", f"arrêt d'urgence : {reason}")
        else:
            self.journal.record_incident("*", "warn", f"arrêt : fermeture des positions ({reason})")
        self.stop_progress("Annulation des ordres HL")
        # fix r1 finding 4 : annulation globale au niveau exchange d'abord (protégée).
        coins = [m.hl_coin for m in self.markets.values()]
        try:
            await self.hl.cancel_all(coins)
        except Exception as exc:
            self.journal.record_incident("*", "error", f"annulation globale HL échouée : {exc}")
        for e in self.engines.values():
            # fix r1 finding 3 : session Var morte -> pas de flatten taker (fermerait HL en
            # laissant Var nu), seulement annuler et journaliser.
            if not self.session_ok:
                try:
                    await e.cancel_all()
                except Exception as exc:
                    self.journal.record_incident(
                        e.cfg.var_asset, "error", f"annulation HL échouée : {type(exc).__name__}: {exc}")
                self.journal.record_incident(
                    e.cfg.var_asset, "error", "session Var tombée : positions conservées, intervention manuelle")
                e.state = IDLE
                continue
            # fix r1 finding 4 : un flatten qui lève sur un marché ne doit jamais empêcher les
            # autres d'être débouclés.
            b, a = self._book(e.cfg.hl_coin)
            e.progress_fn = self.stop_progress
            try:
                await e.flatten(True, b, a, self._var_mid(e.cfg.var_asset))
            except Exception as exc:
                self.journal.record_incident(
                    e.cfg.var_asset, "error", f"sortie d'urgence échouée : {type(exc).__name__}: {exc}")
            finally:
                e.progress_fn = lambda step: None
            e.state = IDLE
            # I3d : prochaine tentative de sortie (si partielle) par tick(), avec le backoff.
            self._next_taker_ts[e.cfg.var_asset] = time.monotonic() + (
                _TAKER_RETRY_AFTER_VAR_UNCONFIRMED_S if e.pos_var != 0 else _TAKER_RETRY_S)
        # I4 : attendre la fin du tick en cours (sauf si kill() est appelé depuis ce tick même),
        # puis une annulation globale finale : un requote en vol a pu poser un ordre entre-temps.
        self.stop_progress("Annulation des ordres HL")
        cur = asyncio.current_task()
        if self._tick_task is not None and self._tick_task is not cur:
            try:
                await asyncio.wait_for(self._tick_idle.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                self.journal.record_incident("*", "error", "arrêt d'urgence : le tick en cours ne s'est pas terminé")
        try:
            await self.hl.cancel_all(coins)
        except Exception as exc:
            self.journal.record_incident("*", "error", f"annulation globale HL finale échouée : {exc}")

    # -- arrêt normal avec fermeture des positions ------------------------------------
    async def positions_flat(self, dust_usd: float = 0.0, warned: Optional[set] = None) -> bool:
        """Vrai si les positions RÉELLES, relues sur HL et sur Var, sont à plat sur tous les
        marchés (activés ou non). Une lecture impossible compte comme « pas à plat ». Une jambe
        dont le notionnel (meilleur mid disponible) est sous `dust_usd` compte comme à plat :
        incident unique par marché et par venue (`warned`), à solder à la main."""
        try:
            hl_pos = await self.hl.positions()
            var_pos = await self.var.positions()
        except Exception as exc:
            self.journal.record_incident(
                "*", "warn", f"vérification des positions impossible : {type(exc).__name__}: {exc}")
            return False
        warned = set() if warned is None else warned
        flat = True
        for a, eng in self.engines.items():
            legs = (("HL", float((hl_pos or {}).get(eng.cfg.hl_coin, 0.0) or 0.0)),
                    ("Var", float(((var_pos or {}).get(a) or {}).get("qty", 0.0) or 0.0)))
            for venue, qty in legs:
                if abs(qty) <= 1e-12:
                    continue
                mid = self._mid_for(a, eng) or 0.0
                notional = abs(qty) * mid
                # mid inconnu : impossible de juger, la jambe compte comme ouverte.
                if mid > 0 and notional < dust_usd:
                    if (a, venue) not in warned:
                        warned.add((a, venue))
                        self.journal.record_incident(
                            a, "warn", f"arrêt : reliquat de {notional:.2f} $ sur {a} ({venue}) sous le "
                                       f"minimum, à solder à la main")
                    continue
                flat = False
        return flat

    async def close_for_stop(self, timeout_s: float, poll_s: float = 2.0) -> bool:
        """Arrêt normal : même sortie que l'arrêt d'urgence (kill), puis attente que les positions
        réelles soient à plat sur les deux venues. Les sorties partielles sont réessayées par
        tick() (_killed_exits, avec le backoff). Un reliquat sous `stop_dust_usd` ne bloque pas
        l'arrêt (sa sortie est quand même tentée par kill). Faux si des positions restent
        ouvertes au délai : l'orchestrateur reste alors en état « arrêté » et continue de
        réessayer."""
        dust = getattr(self.g, "stop_dust_usd", 0.0)
        warned: set = set()
        # Premier relevé avant la sortie : signale tout de suite les reliquats déjà présents.
        await self.positions_flat(dust, warned)
        await self.kill("arrêt demandé", emergency=False)
        deadline = time.monotonic() + timeout_s
        while True:
            self.stop_progress("Vérification des positions")
            if await self.positions_flat(dust, warned):
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            await asyncio.sleep(min(poll_s, left))

    # -- statut pour l'interface --------------------------------------------------
    def _bbo_age(self, coin: str) -> Optional[float]:
        if not self.hl_feed:
            return None
        age = self.hl_feed.book_age(coin)
        return None if age == float("inf") else age

    def _session_pnl(self) -> dict:
        try:
            pnl, n = self.journal.pnl_since(self.session_started_at)
        except Exception:
            pnl, n = None, 0
        return {"session_pnl": pnl, "session_trades": n, "session_started_at": self.session_started_at}

    def status(self) -> dict:
        out = []
        for a, e in self.engines.items():
            q = self.feed.latest(a)
            b, k = self._book(e.cfg.hl_coin)
            mid = (b + k) / 2 if b and k else (q.mid if q else 0)
            out.append({"asset": a, "coin": e.cfg.hl_coin, "state": e.state, "reason": e.last_reason,
                        "var_bid": q.bid if q else None, "var_ask": q.ask if q else None,
                        "var_age": self.feed.age(a) if q else None, "hl_bid": b, "hl_ask": k,
                        "buy_px": (e.orders.get("buy") or {}).get("px"), "sell_px": (e.orders.get("sell") or {}).get("px"),
                        "pos_hl": e.pos_hl, "pos_var": e.pos_var, "delta_usd": (e.pos_hl + e.pos_var) * mid,
                        "pending_qty": e.pending_qty,
                        "basis": (self.last_prices.get(a) or {}).get("basis"), "enabled": e.cfg.enabled,
                        "entry_mode": e.cfg.entry_mode, "min_edge_bps": e.cfg.min_edge_bps,
                        "edge_now_bps": e.edge_now_bps, "edge_now_side": e.edge_now_side,
                        "chase": dict(e.chase_stats), "hot": self._market_hot(a),
                        "var_ref": self._var_ref_src.get(a),
                        "var_ws_age": ws[1] if (ws := self._ws_price(a)) else None,
                        "var_ws_px": ws[0] if ws else None,
                        "hl_bbo_age": self._bbo_age(e.cfg.hl_coin),
                        "basis_fast": self.basis_fast[a].value if a in self.basis_fast else None,
                        "lag": dict(e.lag_stats)})
        edges = {e.cfg.min_edge_bps for e in self.engines.values()}
        return {"markets": out, "min_edge_bps": edges.pop() if len(edges) == 1 else None,
                "session_ok": self.session_ok,
                "hl_ws_ok": bool(self.hl_feed) and not self._hl_dead(),
                "strikes": self.var.limiter.strikes_recent(), "quotes_suspended": self.var.limiter.quotes_suspended(),
                "strikes_24h": (self.var.limiter.strikes_24h() if hasattr(self.var.limiter, "strikes_24h") else None),
                "hl_margin_ratio": self.hl_margin, "var_balance": self.var_balance,
                "hl_rate": self.hl_rate, "hl_cancel_only": self._cancel_only,
                "daily_pnl": self.journal.daily_pnl(), **self._session_pnl(), "killed": self.killed, "kill_reason": self.kill_reason,
                "emergency": self.emergency,
                "paper": bool(getattr(self.hl_client, "paper", False))}

    # -- export CSV quotidien -------------------------------------------------------
    async def _daily_export(self, stop: asyncio.Event, out_dir: Path) -> None:
        exported_for: Optional[date] = None
        while not stop.is_set():
            now = self._now()
            if now.hour == 21 and now.minute == 5 and exported_for != now.date():
                try:
                    self.journal.export_csv(now.date(), out_dir)
                except Exception as exc:
                    self.journal.record_incident("*", "warn", f"export CSV échoué : {exc}")
                exported_for = now.date()
            await asyncio.sleep(30)

    # -- boucle principale ---------------------------------------------------------
    async def run(self, stop: asyncio.Event, account: str, out_dir: Path = DEFAULT_EXPORT_DIR) -> None:
        loop = asyncio.get_running_loop()
        self.hl_feed = HLFeed([m.hl_coin for m in self.markets.values()], account, self.on_fill, loop)
        self.hl_feed.start()
        tasks = [asyncio.create_task(self.feed.run(stop)), asyncio.create_task(self.health_loop(stop)),
                asyncio.create_task(self._daily_export(stop, out_dir))]
        if self.g.var_ws_enabled:
            # L2 : prix Var temps réel (lecture seule, reconnexion intégrée à VarPriceStream.run).
            self._var_ws_started_at = time.time()
            tasks.append(asyncio.create_task(self.var_ws.run(stop)))

        async def _loop(fn, every):
            while not stop.is_set():
                try:
                    await fn()
                except Exception as exc:
                    self.journal.record_incident("*", "error", f"{fn.__name__} : {exc}")
                await asyncio.sleep(every)

        async def _tick_loop():
            # L4 : cadence de 0,5 s, ou tout de suite après un prix websocket Var d'un marché en
            # lag_taker (self._wake).
            while not stop.is_set():
                try:
                    await self.tick()
                except Exception as exc:
                    self.journal.record_incident("*", "error", f"tick : {exc}")
                try:
                    await asyncio.wait_for(self._wake.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()

        tasks += [asyncio.create_task(_tick_loop()),
                  asyncio.create_task(_loop(self.reconcile, self.g.reconcile_interval_s))]
        try:
            await stop.wait()
        finally:
            # fix r2 D : ordre d'arrêt précis.
            coins = [m.hl_coin for m in self.markets.values()]
            # 1) annulation globale au niveau exchange, protégée.
            self.stop_progress("Annulation des ordres HL")
            try:
                await self.hl.cancel_all(coins)
            except Exception as exc:
                self.journal.record_incident("*", "error", f"annulation globale HL échouée (arrêt) : {exc}")
            self.stop_progress("Attente des couvertures en cours")
            # 2) attendre que self._fill_tasks soit VIDE (budget 15 s ; les tâches ajoutées pendant
            # l'attente, par un fill encore en vol, comptent aussi -> on relit l'ensemble à chaque
            # itération plutôt que de figer une liste au départ).
            deadline = time.monotonic() + 15.0
            while self._fill_tasks and time.monotonic() < deadline:
                pending = list(self._fill_tasks)
                await asyncio.wait(pending, timeout=max(0.0, deadline - time.monotonic()))
            # 3) attendre que le verrou de couverture soit libre (budget 15 s), pour ne pas couper
            # une couverture/sortie/correction en cours.
            deadline = time.monotonic() + 15.0
            while self.gate.lock.locked() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            self.stop_progress("Fermeture des connexions")
            # 4) annuler et ATTENDRE les tâches de boucle (tick, reconcile, feed, health, export).
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # 5) dans tous les cas : annulation globale à nouveau (protégée), puis arrêt du feed HL
            # en tout dernier.
            try:
                await self.hl.cancel_all(coins)
            except Exception as exc:
                self.journal.record_incident("*", "error", f"annulation globale finale échouée : {exc}")
            # I9 : désarmer le dead-man switch (évite un déclenchement inutile, 10 par jour max).
            # N2 : toujours tenter le désarmement.
            try:
                await self.hl.schedule_cancel(None)
            except Exception:
                pass
            self.hl_feed.stop()
