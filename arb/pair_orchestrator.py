"""Mode paire pétrole : orchestrateur.

Tient une paire couverte USOILP/UKOILP sur les swaps Var et, toutes les cycle_min, la retourne
(sens voulu différent) ou la ferme puis la rouvre (même sens), selon le z du ratio WTI/Brent.
Aucune jambe Hyperliquid. Le compte détient d'autres positions (cuivre...) : tout ce qui lit
ou ferme des positions est filtré sur les DEUX seuls actifs de la paire. Tous les ordres passent
par PairExecutor (garde d'équilibre), y compris les fermetures d'arrêt et de séance.
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from arb.config import GlobalCfg, PairCfg
from arb.journal import SafeJournal
from arb.pair_exec import Fill, PairExecutor, plan_cycle, target_usd
from arb.pair_signal import RatioSignal, TrendSignal
from arb.tv_candles import TradingViewCandles
from arb.schedule import parse_sessions
from arb.var_client import VarError, parse_max_leverage
from arb.var_ws import VarPriceStream

ACTIVE, PAUSE, BLOCKED, STOPPED, WAITING = "ACTIF", "PAUSE", "BLOQUÉ", "ARRÊTÉ", "ATTENTE"
_LEGS = ("wti", "brent")
STARTUP_REASON = "position pétrole existante, vérifier"
_LIQ_CHECK_S = 30.0       # garde de liquidation : une lecture des positions au plus toutes les 30 s
_READ_BUDGET_S = 1.0      # lectures périodiques : jamais d'attente du limiteur au-delà
_CLOSE_RETRY_S = 30.0     # nouvel essai d'une fermeture échouée
_SESSIONS_REFRESH_S = 3600.0
_SESSIONS_RETRY_S = 60.0  # séances inconnues (lecture en échec, toutes expirées) : relecture rapide
_BALANCE_REFRESH_S = 300.0
STRIKES_REASON = "limite Var (strikes)"
_WS_RECONNECT_AFTER_S = 15.0  # flux Var muet depuis plus longtemps : reconnexion forcée
_WS_RECONNECT_RETRY_S = 30.0  # puis nouvel essai au plus toutes les 30 s
_ASSET_STALE_S = 60.0  # dernier prix reçu d'un actif plus vieux : flux non frais, même si d'autres messages arrivent


class PairOrchestrator:
    def __init__(self, cfg: PairCfg, g: GlobalCfg, var, journal, clock=time.time, stream=None,
                 sleep=asyncio.sleep) -> None:
        self.cfg, self.g, self.var = cfg, g, var
        # aucune écriture journal ne doit interrompre une fermeture (log console en cas d'échec).
        self.journal = SafeJournal.wrap(journal)
        self.clock, self.sleep = clock, sleep
        self.assets = {"wti": cfg.wti, "brent": cfg.brent}
        self.stream = stream if stream is not None else VarPriceStream({cfg.wti: cfg.kind, cfg.brent: cfg.kind})
        self.signal = RatioSignal(cfg.z_window_min)
        self.trend = TrendSignal(cfg.trend_window_min, cfg.trend_confirm_min,
                                 cfg.trend_min_move_bps, cfg.trend_min_r2)
        self.tv = (TradingViewCandles(cfg.tv_wti_symbol, cfg.tv_brent_symbol,
                   window_min=cfg.trend_window_min, stale_s=cfg.tv_stale_s, clock=clock)
                   if cfg.signal_mode == "trend_hold" and cfg.trend_source == "tradingview" else None)
        self._strategy_reason = None
        self._manual_entry_side = None
        self._manual_entry_executed = None
        self._quoted_cost_bps = None
        self._projected_move_bps = None
        self.executor = PairExecutor(var, cfg, self.journal, clock=clock, sleep=sleep)
        self.engines: dict = {}  # interface de l'orchestrateur classique : aucun marché exposé
        self.progress = lambda step: None
        self.stop_progress = lambda step: None
        self.killed = False
        self.kill_reason: Optional[str] = None
        self.emergency = False
        self.state, self.reason = ACTIVE, None
        self.sessions: dict[str, list] = {}
        self.var_balance = 0.0
        self.session_started_at = clock()
        # Sens du dernier cycle (conservé après une fermeture de séance, pour le préchauffage).
        self.pref_side: Optional[str] = None
        # Par jambe : [quantité signée, prix moyen], tenu à partir des exécutions.
        self._book = {leg: [0.0, 0.0] for leg in _LEGS}
        self._last_cycle_end: Optional[float] = None
        self._flat_reason: Optional[str] = None  # fermeture demandée par la garde de liquidation
        self._next_close_try = 0.0
        self._next_liq_check = 0.0
        self._ws_reconnect_at = 0.0
        self._ws_muted = False
        self._cycle_retry_at = 0.0
        self._sessions_warned = False
        self._daily_err = False
        self._lock = asyncio.Lock()  # un seul passage d'ordres à la fois (cycle, fermeture, arrêt)
        self.cycles = 0
        self.last_cycle_at: Optional[float] = None
        self._orders = 0
        self._volume = 0.0
        self._realized = 0.0
        self._slip_sum = 0.0
        self._half_sum = 0.0

    # -- démarrage ---------------------------------------------------------------------
    def _load_strikes(self) -> None:
        """Comme Orchestrator._load_strikes : recharger les strikes Var des dernières 24 h et
        persister les suivants (la suspension des cotations met la paire en PAUSE)."""
        lim = getattr(self.var, "limiter", None)
        if lim is None or not hasattr(lim, "load_strikes"):
            return
        try:
            lim.load_strikes(self.journal.strikes_since(self.clock() - 86400))
        except Exception as exc:
            self.journal.record_incident("PAIR", "warn", f"strikes Var non rechargés : {exc}")
        lim.on_strike = self.journal.record_strike

    def _strikes_suspended(self) -> bool:
        lim = getattr(self.var, "limiter", None)
        try:
            return bool(lim is not None and lim.quotes_suspended())
        except Exception:
            return False

    async def setup(self) -> None:
        self._load_strikes()
        self.progress("Lecture des séances Var")
        try:
            await self._load_sessions(await self.var.supported_assets())
        except Exception as exc:  # séances inconnues : aucun cycle (ATTENTE), incident au premier tick
            self.journal.record_incident("PAIR", "warn", f"lecture des séances Var impossible : {exc}")
        self.progress("Réglage du levier Var")
        for a in self.assets.values():
            if await self._set_var_leverage(a, self.cfg.var_leverage) is None:
                self._set_state(BLOCKED, f"levier Var non réglé sur {a}")
        self.progress("Lecture des positions pétrole")
        pos = self._pair_positions(await self.var.positions())
        usd = {leg: self._usd(pos[leg]) for leg in _LEGS}
        # Carnet rempli depuis les positions réelles dans TOUS les cas (même en BLOQUÉ) : les gardes
        # de séance, de liquidation et de perte du jour doivent savoir qu'une position est ouverte.
        for leg in _LEGS:
            self._book[leg] = [pos[leg]["qty"], pos[leg]["entry"] if pos[leg]["qty"] else 0.0]
        n_open = sum(1 for leg in _LEGS if pos[leg]["qty"] != 0)
        if n_open:
            side = self._side_of(pos)
            if side is None or abs(abs(usd["wti"]) - abs(usd["brent"])) > self.cfg.tranche_usd:
                msg = (f"{STARTUP_REASON} (WTI {usd['wti']:.0f} $, Brent {usd['brent']:.0f} $)")
                self._set_state(BLOCKED, msg)
                self.journal.record_incident("PAIR", "error", msg)
            else:
                self.pref_side = side
                # position adoptée : le premier cycle part une période plus tard.
                self._last_cycle_end = self.clock()
                self.journal.record_incident("PAIR", "warn", f"position existante adoptée en sens {side}")
        await self._refresh_balance()
        self.progress(None)

    async def _load_sessions(self, assets: dict) -> None:
        for a in self.assets.values():
            entry = ((assets or {}).get(a) or [{}])[0]
            sess = parse_sessions(entry)
            if sess or a not in self.sessions:  # une relecture vide ne remplace pas les séances connues
                self.sessions[a] = sess

    async def _set_var_leverage(self, a: str, lev: int) -> Optional[int]:
        """Comme Orchestrator._set_var_leverage : sur un 422 « range max N », nouvel essai avec N."""
        try:
            await self.var.set_leverage(a, lev)
            return lev
        except VarError as exc:
            cap = parse_max_leverage(getattr(exc, "body", None))
            if cap is None or cap < 1 or cap >= lev:
                self.journal.record_incident(a, "error", f"levier Var non réglé : {exc}")
                return None
        try:
            await self.var.set_leverage(a, cap)
        except VarError as exc:
            self.journal.record_incident(a, "error", f"levier Var non réglé (maximum {cap}) : {exc}")
            return None
        self.journal.record_incident(a, "warn", f"levier Var borné au maximum de l'actif : {lev} -> {cap}")
        return cap

    # -- positions (filtrées sur les deux actifs) ----------------------------------------
    def _pair_positions(self, raw: dict) -> dict[str, dict]:
        out = {}
        for leg, a in self.assets.items():
            p = (raw or {}).get(a) or {}
            out[leg] = {"qty": float(p.get("qty", 0.0) or 0.0), "entry": float(p.get("entry", 0.0) or 0.0),
                        "liq": p.get("liq"), "value": float(p.get("value", 0.0) or 0.0)}
        return out

    def _usd(self, p: dict, leg: Optional[str] = None) -> float:
        """Notionnel signé d'une position : value de Var, sinon quantité au prix du flux ou d'entrée."""
        q = p["qty"]
        if q == 0:
            return 0.0
        if p.get("value"):
            return math.copysign(abs(p["value"]), q)
        px = (self._price(leg) if leg else None) or p.get("entry") or 0.0
        return q * px

    @staticmethod
    def _side_of(pos: dict) -> Optional[str]:
        w, b = pos["wti"]["qty"], pos["brent"]["qty"]
        if w < 0 < b:
            return "A"
        if b < 0 < w:
            return "B"
        return None

    async def _read_positions(self, deadline: Optional[float] = None) -> dict[str, dict]:
        if deadline is None:
            return self._pair_positions(await self.var.positions())
        return self._pair_positions(await self.var.positions(deadline=deadline))

    def _has_pos(self) -> bool:
        return any(self._book[leg][0] != 0 for leg in _LEGS)

    # -- prix ------------------------------------------------------------------------------
    def _latest(self, leg: str) -> Optional[dict]:
        try:
            return self.stream.latest(self.assets[leg])
        except Exception:
            return None

    def _price(self, leg: str) -> Optional[float]:
        u = self._latest(leg)
        px = (u or {}).get("mark")
        return float(px) if px else None

    def _last_px(self, leg: str, p: dict) -> float:
        """Dernier prix connu d'une jambe : flux, sinon value/qty de Var, sinon prix moyen ou d'entrée."""
        px = self._price(leg)
        if not px and p.get("qty") and p.get("value"):
            px = abs(p["value"] / p["qty"])
        return px or self._book[leg][1] or p.get("entry") or 0.0

    def _fresh(self, now: float) -> bool:
        """Flux vivant : dernier message websocket reçu (last_msg_ms du flux) depuis moins de
        price_stale_s, un prix connu pour chaque jambe. Un prix qui ne bouge pas n'est pas un flux
        muet. Borne par actif : un prix reçu il y a plus de _ASSET_STALE_S n'est plus frais.
        Sans last_msg_ms : dernière réception de chaque actif."""
        stale_ms = self.cfg.price_stale_s * 1000.0
        last_msg = getattr(self.stream, "last_msg_ms", None)
        for leg in _LEGS:
            u = self._latest(leg)
            if not u or not u.get("mark"):
                return False
            if u.get("ts_recv_ms") is None:
                return False
            age_ms = now * 1000.0 - float(u["ts_recv_ms"])
            if age_ms > (stale_ms if last_msg is None else _ASSET_STALE_S * 1000.0):
                return False
        return last_msg is None or now * 1000.0 - float(last_msg) <= stale_ms

    # -- séances ---------------------------------------------------------------------------
    def _phase(self, now: float) -> tuple[str, Optional[str]]:
        """("open" | "no_cycle" | "close" | "closed" | "unknown", motif). Fenêtre de la paire = intersection
        des séances des deux actifs : depuis la plus tardive des ouvertures, jusqu'à la plus
        proche des fermetures."""
        t = datetime.fromtimestamp(now, tz=timezone.utc)
        cur = []
        if not self._sessions_known(t):
            return "unknown", "séances inconnues"
        for a in self.assets.values():
            s = next(((op, cl) for op, cl in self.sessions.get(a, []) if op <= t < cl), None)
            if s is None:
                return "closed", f"marché {a} fermé"
            cur.append(s)
        since_open = t - max(op for op, _ in cur)
        to_close = min(cl for _, cl in cur) - t
        if to_close <= timedelta(minutes=self.cfg.close_before_min):
            return "close", "fermeture de séance proche"
        if to_close <= timedelta(minutes=self.cfg.no_cycle_before_close_min):
            return "no_cycle", "aucun cycle avant la fermeture de séance"
        if since_open < timedelta(minutes=self.cfg.reopen_after_min):
            return "no_cycle", "ouverture de séance récente"
        return "open", None

    def _sessions_known(self, t: datetime) -> bool:
        """Au moins une séance en cours ou future pour chacun des deux actifs (toutes expirées :
        séances inconnues)."""
        return all(any(cl > t for _, cl in self.sessions.get(a) or []) for a in self.assets.values())

    def _sessions_delay(self) -> float:
        known = self._sessions_known(datetime.fromtimestamp(self.clock(), tz=timezone.utc))
        return _SESSIONS_REFRESH_S if known else _SESSIONS_RETRY_S

    # -- boucle --------------------------------------------------------------------------
    def _ws_watchdog(self, now: float) -> None:
        """Run réel du 09/10 : le websocket Var s'est tu 1 h 30 sans se reconnecter et la paire est
        restée figée en ATTENTE. Au-delà de _WS_RECONNECT_AFTER_S sans message, on force une
        reconnexion (puis au plus toutes les _WS_RECONNECT_RETRY_S)."""
        last = getattr(self.stream, "last_msg_ms", None)
        if last is None:
            ts = [u.get("ts_recv_ms") for u in (self._latest(leg) for leg in _LEGS) if u]
            last = max((t for t in ts if t is not None), default=None)
        if last is None:
            return  # pas encore de premier message (démarrage)
        silent = now - float(last) / 1000.0
        if silent <= _WS_RECONNECT_AFTER_S:
            if self._ws_muted:
                self._ws_muted = False
                self.journal.record_incident("PAIR", "info", "flux de prix Var rétabli")
            return
        if now < self._ws_reconnect_at:
            return
        self._ws_reconnect_at = now + _WS_RECONNECT_RETRY_S
        if not self._ws_muted:
            self._ws_muted = True
            self.journal.record_incident("PAIR", "warn", f"flux de prix Var muet depuis {silent:.0f} s : reconnexion")
        fr = getattr(self.stream, "force_reconnect", None)
        if callable(fr):
            try:
                fr()
            except Exception:
                pass

    async def tick(self) -> None:
        now = self.clock()
        self._ws_watchdog(now)
        fresh = self._fresh(now)
        if fresh:
            self.signal.add(now, self._price("wti"), self._price("brent"))
            if self.tv is None:
                self.trend.add(now, self._price("wti"), self._price("brent"))
        if self.tv is not None:
            snapshot = self.tv.snapshot(now)
            if snapshot["ready"]:
                self.trend.replace_closed(snapshot["points"], now)
            else:
                self.trend.reset()
        phase, _ = self._phase(now)
        if self.killed:  # arrêt : on ne fait que refermer ce qui resterait ouvert
            if self._has_pos() and now >= self._next_close_try:
                await self._close_pair("arrêt")
            return
        # En BLOQUÉ, seuls les cycles sont interdits : perte du jour, garde de liquidation et
        # fermeture de séance ferment toujours la paire (reduce-only vers 0).
        if phase == "unknown":
            if not self._sessions_warned:
                self._sessions_warned = True
                self.journal.record_incident("PAIR", "error", "séances Var inconnues : aucun cycle"
                                             + (", fermeture de la paire" if self._has_pos() else ""))
        else:
            self._sessions_warned = False
        try:
            daily = float(self.journal.daily_pnl())
        except Exception as exc:
            daily = None
            if not self._daily_err:
                self._daily_err = True
                self.journal.record_incident("PAIR", "error", f"perte du jour illisible : {exc}")
            self._set_state(PAUSE, "perte du jour illisible")
        if daily is not None:
            self._daily_err = False
            if daily <= -self.cfg.daily_loss_usd:
                await self.kill(f"perte du jour {daily:.2f} $ (limite {self.cfg.daily_loss_usd:.0f} $)")
                return
        await self._liq_guard(now)
        if self._has_pos() and (phase in ("close", "unknown") or self._flat_reason):
            if now >= self._next_close_try:
                why = {"close": "fermeture de séance", "unknown": "séances inconnues"}.get(phase, self._flat_reason)
                if await self._close_pair(why) and phase == "close":
                    self.signal.reset()  # nouveau préchauffage à la réouverture
                    self.trend.reset()
            return
        # Strikes Var (cotations suspendues) : aucun cycle ni ouverture, les fermetures ci-dessus
        # restent permises. La PAUSE se lève seule à la fin de la suspension.
        # Une autre PAUSE (liquidation, échec, journal) ou un BLOQUÉ n'est jamais écrasé ni levé ici.
        if self._strikes_suspended():
            if self.state == ACTIVE:
                self._set_state(PAUSE, STRIKES_REASON)
            return
        if self.state == PAUSE and self.reason == STRIKES_REASON:
            self._set_state(ACTIVE, None)
        if self.state != ACTIVE or phase != "open" or not fresh or now < self._cycle_retry_at:
            return
        if self._manual_entry_side is None and self._has_pos() and self._last_cycle_end is not None and \
                now < self._last_cycle_end + self.cfg.cycle_min * 60:
            return
        await self._cycle()

    async def _refresh_loop(self, stop: asyncio.Event) -> None:
        """Séances (toutes les heures : la liste Var peut ne couvrir que les prochains jours) et
        solde (toutes les 5 min), dans une tâche à part : jamais d'attente dans la boucle de tick.
        Une lecture en échec garde la valeur précédente."""
        next_sessions = self.clock() + self._sessions_delay()
        next_balance = self.clock() + _BALANCE_REFRESH_S
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), max(0.0, min(next_sessions, next_balance) - self.clock()))
                return
            except asyncio.TimeoutError:
                pass
            if self.clock() >= next_balance:
                next_balance = self.clock() + _BALANCE_REFRESH_S
                await self._refresh_balance()
            if self.clock() >= next_sessions:
                try:
                    await self._load_sessions(await self.var.supported_assets())
                except Exception:
                    pass
                next_sessions = self.clock() + self._sessions_delay()

    async def _refresh_balance(self) -> None:
        if not hasattr(self.var, "portfolio"):
            return
        try:
            self.var_balance = (await self.var.portfolio())["balance"]
        except Exception:
            pass

    async def _liq_guard(self, now: float) -> None:
        """Distance de liquidation sous var_liq_distance_min sur une jambe : fermeture, PAUSE."""
        if not self._has_pos() or now < self._next_liq_check:
            return
        self._next_liq_check = now + _LIQ_CHECK_S
        try:
            pos = await self._read_positions(deadline=time.monotonic() + _READ_BUDGET_S)
        except Exception:  # VarRateLimited ou autre : lecture ignorée, nouvel essai plus tard
            return
        for leg, a in self.assets.items():
            p = pos[leg]
            liq = p.get("liq")
            if not liq or p["qty"] == 0:
                continue
            px = self._price(leg) or (abs(p["value"] / p["qty"]) if p["value"] else p["entry"])
            if px and abs(px - float(liq)) / px < self.g.var_liq_distance_min:
                msg = f"distance de liquidation {abs(px - float(liq)) / px:.1%} sur {a} (prix {px}, liquidation {liq})"
                self._set_state(PAUSE, msg)
                self._flat_reason = "garde de liquidation"
                self.journal.record_incident("PAIR", "error", msg + " : fermeture de la paire")
                await self._close_pair(self._flat_reason)
                return

    async def _cycle(self) -> None:
        async with self._lock:
            if self.killed or self.state != ACTIVE:
                return
            try:
                pos = await self._read_positions()
            except Exception as exc:
                self.journal.record_incident("PAIR", "warn", f"cycle reporté : positions illisibles ({exc})")
                self._cycle_retry_at = self.clock() + _CLOSE_RETRY_S
                return
            try:
                await self._cycle_orders(pos)
            except Exception as exc:
                await self._crashed("cycle", exc)
                self._cycle_retry_at = self.clock() + _CLOSE_RETRY_S

    async def _crashed(self, label: str, exc: Exception) -> None:
        """Erreur imprévue pendant un passage d'ordres (un ordre a peut-être passé) : BLOQUÉ,
        incident, puis réconciliation sur les positions réelles. L'appelant fixe le délai avant
        le prochain essai (jamais de rafale d'ordres)."""
        msg = f"{label} : erreur imprévue ({type(exc).__name__}: {exc})"
        self._set_state(BLOCKED, msg)
        self.journal.record_incident("PAIR", "error", msg)
        try:
            await self._reconcile()
        except Exception as exc2:
            self.journal.record_incident("PAIR", "warn", f"réconciliation impossible : {exc2}")

    def request_entry(self, side: str) -> None:
        """One explicit operator entry; it does not fabricate or change the trend signal."""
        if side not in ("A", "B"):
            raise ValueError("sens invalide")
        if self._manual_entry_side is not None:
            raise ValueError("entrée déjà demandée")
        self._manual_entry_side = side
        self._cycle_retry_at = 0.0
        self.journal.record_incident("PAIR", "warn", f"entrée ponctuelle explicitement demandée en sens {side}")

    async def _cycle_orders(self, pos: dict) -> None:
        cur_side = self._side_of(pos)
        manual_side = self._manual_entry_side
        volume_due = (cur_side is not None and self.cfg.volume_cycle_min > 0
                      and self._last_cycle_end is not None
                      and self.clock() >= self._last_cycle_end + self.cfg.volume_cycle_min * 60)
        recycle = False
        required_volume = self.cfg.volume_cycle_required and self.cfg.volume_cycle_min > 0
        if manual_side is not None:
            side = manual_side
            self._strategy_reason = "entrée ponctuelle demandée par l'opérateur, sans confirmation de tendance"
            self._projected_move_bps = None
            self._quoted_cost_bps = None
            self._cycle_retry_at = self.clock() + self.cfg.cycle_min * 60
        elif self.cfg.signal_mode == "trend_hold":
            if self.tv is not None and not self.tv.snapshot(self.clock())["ready"]:
                self._strategy_reason = self.tv.snapshot(self.clock())["reason"]
                self._cycle_retry_at = self.clock() + 30
                return
            decision = self.trend.status()
            side = decision["side"]
            self._strategy_reason = decision["reason"]
            initial = not decision["ready"] and self.cfg.warmup_entry_side is not None
            if initial:
                side = self.pref_side or self.cfg.warmup_entry_side
                self._strategy_reason = "sens initial fixé, tendance encore non confirmée"
            self._projected_move_bps = (abs(decision["slope_bps_min"]) * self.cfg.trend_projection_min
                                        if decision["slope_bps_min"] is not None else None)
            self._cycle_retry_at = self.clock() + (self.cfg.cycle_min * 60 if decision["ready"] else 30)
            if self.cfg.volume_cycle_min > 0 and cur_side and self._last_cycle_end is not None:
                due = self._last_cycle_end + self.cfg.volume_cycle_min * 60
                if due > self.clock():
                    self._cycle_retry_at = min(self._cycle_retry_at, due)
            volume_action = required_volume and (volume_due or (cur_side is None and decision["ready"]))
            if side is None:
                if not volume_due and not volume_action:
                    if cur_side:
                        self._strategy_reason += " : paire conservée"
                    return
                if cur_side is not None:
                    side = cur_side
                else:
                    slope = decision["slope_bps_min"]
                    side = self.pref_side or ("A" if slope is not None and slope < 0 else "B")
                    self._strategy_reason += " : entrée volume sans confirmation de tendance"
            recycle = volume_due and side == cur_side
            resize = side == cur_side and any(
                abs(abs(self._usd(pos[leg], leg)) - self.cfg.leg_usd) > self.cfg.tranche_usd / 2
                for leg in _LEGS)
            if side == cur_side and not resize and not recycle:
                self._strategy_reason += " : paire conservée, aucun ordre"
                return
            if volume_action:
                # Explicit mandatory-volume mode: trend strength and estimated CPM
                # cannot veto the scheduled cycle. Fresh executable quotes, max
                # slippage, position balance and all risk/session guards still apply.
                self._quoted_cost_bps = None
                self._strategy_reason += " : cycle volume obligatoire"
            elif not await self._trend_cost_gate(initial=initial, volume=recycle):
                return
            if recycle:
                self._strategy_reason += " : cycle volume, fermeture puis réouverture"
        else:
            side = self.signal.side(self.pref_side or "A")
        flip = cur_side is not None and side != cur_side
        if self.cfg.signal_mode == "trend_hold" and side == cur_side and not recycle:
            cur = {leg: self._usd(pos[leg], leg) for leg in _LEGS}
        else:
            cur = self._plan_start(pos)
        reopen = not flip if self.cfg.signal_mode == "mean_reversion" else recycle
        steps = plan_cycle(cur, target_usd(side, self.cfg.leg_usd), self.cfg.tranche_usd, reopen=reopen)
        prices = {leg: self._price(leg) for leg in _LEGS}
        # Consume before execution: uncertain/failed fills never repeat a forced entry.
        if manual_side is not None:
            self._manual_entry_side = None
        ok = await self._execute(steps, prices, "cycle")
        self._last_cycle_end = self.clock()
        if ok:
            if manual_side is not None:
                self._manual_entry_executed = manual_side
            self.pref_side = side
            self.cycles += 1
            self.last_cycle_at = self._last_cycle_end

    async def _trend_cost_gate(self, *, initial=False, volume=False) -> bool:
        """Compare une extrapolation de pente aux spreads RFQ, sans ordre.

        Coût aller-retour exprimé en pb du notionnel d'UNE jambe : somme
        des spreads complets des deux actifs, plus réserve de glissement.
        Ce filtre ne garantit ni la continuation de la pente ni un profit.
        Les sorties de sécurité ne passent jamais par ce filtre.
        """
        spread = 0.0
        try:
            for leg in _LEGS:
                price = self._price(leg)
                if price is None or not math.isfinite(price) or price <= 0:
                    raise ValueError("prix invalide")
                q = await self.var.quote(self.assets[leg], self.cfg.kind, self.cfg.tranche_usd / price)
                if not all(math.isfinite(v) for v in (q.bid, q.ask, q.mid)) or not (0 < q.bid <= q.ask):
                    raise ValueError("cotation invalide")
                spread += (q.ask - q.bid) / q.mid * 1e4
        except Exception as exc:
            self._strategy_reason = f"filtre coût : cotation indisponible ({type(exc).__name__})"
            self._quoted_cost_bps = None
            self._cycle_retry_at = self.clock() + 30
            return False
        self._quoted_cost_bps = spread + self.cfg.trend_slip_reserve_bps
        if initial or volume:
            # Aller-retour : volume 4 x notionnel d'une jambe, coût estimé
            # notionnel x somme des spreads. 1 pb / 4 = 25 $/M.
            estimated_cpm = self._quoted_cost_bps * 25
            if estimated_cpm > self.cfg.cost_alert_per_m:
                self._strategy_reason = ("cycle volume différé" if volume else "entrée immédiate différée") + " : spread estimé au-dessus du budget CPM"
                self._cycle_retry_at = self.clock() + 30
                return False
            return True
        minimum = self._quoted_cost_bps * self.cfg.trend_cost_multiplier
        if self._projected_move_bps is None or self._projected_move_bps <= minimum:
            self._strategy_reason = "tendance trop faible face au spread coté : aucun ordre"
            return False
        return True

    def _plan_start(self, pos: dict) -> dict[str, float]:
        """Position de départ pour plan_cycle, arrondie à un nombre entier de tranches (au moins
        une) : chaque jambe ouverte est fermée en entier, et les fermetures partent en quantité
        (PairExecutor), seul le nombre de tranches compte."""
        tr = self.cfg.tranche_usd
        out = {}
        for leg in _LEGS:
            q = pos[leg]["qty"]
            if q == 0:
                out[leg] = 0.0
                continue
            n = max(1, round(abs(self._usd(pos[leg], leg)) / tr))
            out[leg] = math.copysign(n * tr, q)
        return out

    async def _close_pair(self, why: str) -> bool:
        """Ferme les deux jambes (reduce-only, en quantité), via PairExecutor. Vrai si à plat."""
        async with self._lock:
            try:
                pos = await self._read_positions()
            except Exception as exc:
                self._next_close_try = self.clock() + _CLOSE_RETRY_S
                self.journal.record_incident("PAIR", "error", f"fermeture ({why}) : positions illisibles ({exc})")
                return False
            if all(pos[leg]["qty"] == 0 for leg in _LEGS):
                for leg in _LEGS:
                    self._book[leg] = [0.0, 0.0]
                return True
            # reopen=True : chaque jambe est ramenée à 0 en tranches reduce-only (sans, plan_cycle
            # produirait des ordres ordinaires).
            try:
                steps = plan_cycle(self._plan_start(pos), {"wti": 0.0, "brent": 0.0}, self.cfg.tranche_usd,
                                   reopen=True)
                prices = {leg: self._last_px(leg, pos[leg]) for leg in _LEGS}
                ok = await self._execute(steps, prices, f"fermeture ({why})")
            except Exception as exc:
                await self._crashed(f"fermeture ({why})", exc)
                ok = False
            if not ok:
                self._next_close_try = self.clock() + _CLOSE_RETRY_S
            return ok and not self._has_pos()

    async def _execute(self, steps, prices, label: str) -> bool:
        """Passe les ordres, comptabilise les exécutions, puis réconcilie sur les positions réelles."""
        res = await self.executor.run_steps(steps, prices)
        for f in res.filled:
            self._apply_fill(f)
        if not res.ok:
            state = BLOCKED if (res.reason or "").startswith("déséquilibre") else PAUSE
            msg = f"{label} : {res.reason}"
            self._set_state(state, msg)
            self.journal.record_incident("PAIR", "error", msg)
        await self._reconcile()
        return res.ok and self.state != BLOCKED

    async def _reconcile(self) -> None:
        """Positions relues des 2 actifs : la position suivie est recalée sur la réelle ; écart
        de plus d'une tranche entre les jambes (ou suivi faux de plus d'une tranche) : BLOQUÉ."""
        try:
            pos = await self._read_positions()
        except Exception as exc:
            self.journal.record_incident("PAIR", "warn", f"réconciliation impossible : {exc}")
            return
        tr = self.cfg.tranche_usd
        drift = []
        for leg in _LEGS:
            real, book = pos[leg]["qty"], self._book[leg]
            px = self._price(leg) or pos[leg]["entry"] or book[1]
            if abs(real - book[0]) * (px or 0.0) > tr:
                drift.append(f"{self.assets[leg]} suivi {book[0]:g}, réel {real:g}")
            if real == 0:
                self._book[leg] = [0.0, 0.0]
            else:
                self._book[leg] = [real, book[1] if book[0] * real > 0 and book[1] else pos[leg]["entry"]]
        usd = {leg: self._usd(pos[leg], leg) for leg in _LEGS}
        gap = abs(usd["wti"]) - abs(usd["brent"])
        problem = None
        if abs(gap) > tr:
            problem = f"écart de {gap:.0f} $ entre les jambes à la réconciliation"
        elif pos["wti"]["qty"] != 0 and self._side_of(pos) is None:
            problem = "jambes dans le même sens à la réconciliation"
        elif drift:
            problem = "écart entre position suivie et réelle : " + ", ".join(drift)
        if problem:
            self._set_state(BLOCKED, problem)
            self.journal.record_incident("PAIR", "error", problem)

    # -- PnL ---------------------------------------------------------------------------------
    def _apply_fill(self, f: Fill) -> None:
        """Position par jambe au prix moyen ; une réduction réalise (exec - moyen) * qté pour un
        long, (moyen - exec) * qté pour un short, journalisé en ligne « pair_pnl »."""
        self._orders += 1
        self._volume += f.qty * f.exec_px
        self._slip_sum += f.slip_bps
        self._half_sum += f.half_spread_bps
        q, avg = self._book[f.leg]
        d = f.qty if f.side == "buy" else -f.qty
        if q == 0 or q * d > 0:
            nq = q + d
            self._book[f.leg] = [nq, (abs(q) * avg + f.qty * f.exec_px) / abs(nq)]
            return
        closed = min(abs(d), abs(q))
        pnl = (f.exec_px - avg) * closed if q > 0 else (avg - f.exec_px) * closed
        self._realized += pnl
        self.journal.record_arb(self.assets[f.leg], "", 0.0, 0.0, 0.0, f.side, f.exec_px, closed,
                                0.0, pnl, 0.0, "pair_pnl")
        nq = round(q + d, 9)
        if abs(nq) <= 1e-12:
            self._book[f.leg] = [0.0, 0.0]
        elif nq * q > 0:
            self._book[f.leg] = [nq, avg]
        else:
            self._book[f.leg] = [nq, f.exec_px]  # retournement : le reste ouvre au prix exécuté

    def _unrealized(self) -> float:
        out = 0.0
        for leg in _LEGS:
            q, avg = self._book[leg]
            px = self._price(leg)
            if q and px:
                out += (px - avg) * q
        return out

    # -- commandes ---------------------------------------------------------------------------
    def _set_state(self, state: str, reason: Optional[str]) -> None:
        if self.state == BLOCKED and state == PAUSE:
            return  # BLOQUÉ n'est levé que par l'opérateur
        self.state, self.reason = state, reason

    def resume(self) -> None:
        """Déblocage manuel : PAUSE ou BLOQUÉ -> ACTIF."""
        if self.state in (PAUSE, BLOCKED):
            self.state, self.reason = ACTIVE, None
            self._flat_reason = None
            self._next_close_try = 0.0
            self.journal.record_incident("PAIR", "warn", "paire débloquée par l'opérateur")

    async def kill(self, reason: str, emergency: bool = True) -> None:
        self.emergency = emergency or (self.killed and self.emergency)
        self.killed, self.kill_reason = True, reason
        label = "arrêt d'urgence" if emergency else "arrêt"
        self.journal.record_incident("PAIR", "error" if emergency else "warn", f"{label} : {reason}")
        self.stop_progress("Fermeture de la paire pétrole")
        await self._close_pair("arrêt")

    async def close_for_stop(self, timeout_s: float, poll_s: float = 2.0) -> bool:
        """Arrêt normal : ferme la paire, puis relit les positions des deux actifs jusqu'à ce que
        les deux soient sous stop_dust_usd. Faux au délai (le bot reste arrêté et réessaie)."""
        deadline = self.clock() + timeout_s
        await self.kill("arrêt demandé", emergency=False)
        dust = getattr(self.g, "stop_dust_usd", 12.0)
        while True:
            self.stop_progress("Vérification des positions")
            try:
                pos = await self._read_positions()
                if all(abs(self._usd(pos[leg], leg)) < dust for leg in _LEGS):
                    return True
            except Exception as exc:
                self.journal.record_incident("PAIR", "warn", f"vérification des positions impossible : {exc}")
            left = deadline - self.clock()
            if left <= 0:
                return False
            await self.sleep(min(poll_s, left))

    async def is_flat(self) -> bool:
        """Arrêt d'urgence par fichier KILL : vrai si les deux positions pétrole RÉELLES sont sous
        stop_dust_usd. Suivi au-dessus du seuil : faux sans lecture. Suivi à plat mais position
        réelle ouverte : le suivi est recalé sur le réel, pour que la boucle réessaie la fermeture."""
        dust = getattr(self.g, "stop_dust_usd", 12.0)
        if any(abs(self._book[leg][0]) * (self._price(leg) or self._book[leg][1]) >= dust for leg in _LEGS):
            return False
        try:
            pos = await self._read_positions()
        except Exception:
            return False
        if all(abs(self._usd(pos[leg], leg)) < dust for leg in _LEGS):
            return True
        for leg in _LEGS:
            self._book[leg] = [pos[leg]["qty"], pos[leg]["entry"] if pos[leg]["qty"] else 0.0]
        return False

    async def run(self, stop: asyncio.Event, account: str = "") -> None:
        bg = [asyncio.create_task(self.stream.run(stop)), asyncio.create_task(self._refresh_loop(stop))]
        if self.tv is not None:
            bg.append(asyncio.create_task(self.tv.run(stop)))
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except Exception as exc:
                    self.journal.record_incident("PAIR", "error", f"tick : {type(exc).__name__}: {exc}")
                try:
                    await asyncio.wait_for(stop.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
            # un cycle lancé par un autre chemin (kill, arrêt) se termine avant la sortie
            async with self._lock:
                pass
        finally:
            for t in bg:
                t.cancel()
            await asyncio.gather(*bg, return_exceptions=True)

    # -- statut ------------------------------------------------------------------------------
    def status(self) -> dict:
        now = self.clock()
        phase, why = self._phase(now)
        fresh = self._fresh(now)
        if self.killed:
            state, reason = STOPPED, self.kill_reason
        elif self.state != ACTIVE:
            state, reason = self.state, self.reason
        elif phase != "open":
            state, reason = WAITING, why
        elif not fresh:
            state, reason = WAITING, "flux de prix muet ou en préchauffage"
        else:
            state, reason = ACTIVE, self._strategy_reason
            if self.cfg.signal_mode == "trend_hold" and not self._has_pos() and reason:
                state = WAITING
        pw, pb = self._price("wti"), self._price("brent")
        unreal = self._unrealized()
        vol = self._volume
        cost = -(self._realized + unreal) / vol * 1e6 if vol > 0 else None
        try:
            day_start = datetime.fromtimestamp(now, tz=timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            vol_day = float(self.journal.volume_since(day_start, "pair_fill"))
        except Exception:
            vol_day = vol
        if not self._has_pos():
            nxt = 0.0
        elif self._last_cycle_end is None:
            nxt = 0.0
        else:
            nxt = max(0.0, self._last_cycle_end + self.cfg.cycle_min * 60 - now)
        if self.cfg.signal_mode == "trend_hold":
            position_due = self._last_cycle_end + self.cfg.cycle_min * 60 if self._has_pos() and self._last_cycle_end is not None else 0
            nxt = max(0.0, self._cycle_retry_at - now, position_due - now)
        held = {leg: self._book[leg][0] for leg in _LEGS}
        side = "A" if held["wti"] < 0 < held["brent"] else ("B" if held["brent"] < 0 < held["wti"] else None)
        pair = {"state": state, "reason": reason, "side": side,
                "pos_usd": {leg: held[leg] * (self._price(leg) or self._book[leg][1]) for leg in _LEGS},
                "ratio": pw / pb if pw and pb else None, "z": self.signal.z(), "ready": self.signal.ready(),
                "next_cycle_in_s": nxt, "cycles": self.cycles,
                "volume_session_usd": vol, "volume_day_usd": vol_day,
                "pnl_session_usd": self._realized, "pnl_unrealized_usd": unreal, "cost_per_m": cost,
                "avg_slip_bps": self._slip_sum / self._orders if self._orders else None,
                "avg_half_spread_bps": self._half_sum / self._orders if self._orders else None,
                "last_cycle_at": self.last_cycle_at,
                "cost_alert": bool(cost is not None and vol >= 1e6 and cost > self.cfg.cost_alert_per_m)}
        pair["signal_mode"] = self.cfg.signal_mode
        pair["manual_entry_pending"] = self._manual_entry_side
        pair["manual_entry_executed"] = self._manual_entry_executed
        pair["volume_cycle_min"] = self.cfg.volume_cycle_min
        pair["volume_cycle_required"] = self.cfg.volume_cycle_required
        pair["next_volume_cycle_in_s"] = (max(0.0, self._last_cycle_end + self.cfg.volume_cycle_min * 60 - now)
            if self.cfg.volume_cycle_min > 0 and self._has_pos() and self._last_cycle_end is not None else None)
        pair["warmup_entry_side"] = self.cfg.warmup_entry_side
        if self.cfg.signal_mode == "trend_hold":
            pair["trend"] = self.trend.status()
            pair["quoted_roundtrip_cost_bps"] = self._quoted_cost_bps
            pair["projected_move_bps"] = self._projected_move_bps
            pair["trend_source"] = self.cfg.trend_source
            if self.tv is not None:
                tv_status = self.tv.snapshot(now)
                pair["tradingview"] = {k: v for k, v in tv_status.items() if k != "points"}
                pair["signal_ratio"] = tv_status["ratio"]
                if state == ACTIVE and not tv_status["ready"]:
                    pair["state"], pair["reason"] = WAITING, tv_status["reason"]
        try:
            session_pnl, _ = self.journal.pnl_since(self.session_started_at)
        except Exception:
            session_pnl = None
        try:
            daily = self.journal.daily_pnl()
        except Exception:
            daily = None
        lim = getattr(self.var, "limiter", None)
        session_ok = getattr(self.var, "_health_ok", None)
        bridge = getattr(getattr(self.var, "transport", None), "bridge", None)
        if bridge is not None and not bridge.connected:
            session_ok = False
        return {"markets": [], "pair": pair, "session_pnl": session_pnl, "session_trades": self._orders,
                "session_started_at": self.session_started_at, "daily_pnl": daily,
                "killed": self.killed, "kill_reason": self.kill_reason, "emergency": self.emergency,
                "paper": bool(getattr(self.var, "paper", False)),
                "strikes": lim.strikes_recent() if lim is not None else 0,
                "quotes_suspended": lim.quotes_suspended() if lim is not None else False,
                "var_balance": self.var_balance, "session_ok": session_ok}
