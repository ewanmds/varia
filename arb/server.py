"""Serveur FastAPI local pour le bot d'arbitrage HL <-> Var.

Reprend le flux d'authentification wallet et d'approbation d'agent du bot
HIP-4 (`/home/manao/bot hip-4/server.py`, lignes 451 à 553), adapté à
`arb/auth.py`. Rien n'est importé de l'autre projet.

`ctx` (fourni par `main.py`, ou un objet de test équivalent) porte :
  - `ctx.auth`        : une instance de `arb.auth.AuthState` (ou `None` en test)
  - `ctx.orch`        : l'`Orchestrator` en cours, ou `None` avant démarrage
  - `ctx.journal`      : un objet exposant `recent_arbs(n)` / `recent_incidents(n)`
  - `ctx.paper`        : booléen, mode simulation (aucun ordre réel)
  - `ctx.start_bot(account)` : coroutine qui construit et démarre le bot
  - `ctx.close_and_stop()` : coroutine d'arrêt normal (fermeture des positions puis arrêt),
    RuntimeError si des positions restent ouvertes au délai
  - `ctx.stop_bot()`   : repli si `close_and_stop` est absente (arrêt propre et attendu,
    puis repli sur `ctx.stop_event.set()`)

Sécurité (I7) : seuls les en-têtes Host 127.0.0.1 et localhost sont acceptés (protection contre
le DNS rebinding), et tout endpoint qui modifie quelque chose exige l'en-tête `x-auth-token`
d'une session wallet valide.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from arb import auth as authmod
from arb.config import ENTRY_MODES, PairCfg
from arb.engine import BLOCKED, IDLE
from arb.pair_orchestrator import PairOrchestrator
from arb.var_errors import VarError

WEB = Path(__file__).resolve().parents[1] / "web"

# Bornes de validation de min_edge_bps, pour POST /api/market/{asset}/params (amendement t13 §5)
# et POST /api/params/min_edge : [1, 20] pb (abaissé de 3 à 1 le 23/09 à la demande de l'utilisateur).
_MIN_EDGE_BPS_MIN = 1.0
_MIN_EDGE_BPS_MAX = 20.0
_MIN_EDGE_MSG = "le seuil doit être compris entre 1 et 20 pb"
_PARAM_FIELDS = ("min_edge_bps", "fill_usd", "max_position_usd")
# Bornes de POST /api/pair/params (mode paire pétrole) : (min, max) inclusives.
_PAIR_BOUNDS = {"leg_usd": (20.0, 10000.0), "cycle_min": (1.0, 60.0), "daily_loss_usd": (20.0, 1000.0), "volume_cycle_min": (0.0, 60.0)}
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
_AUTH_MSG = "Session wallet absente ou expirée : reconnectez le wallet."


def _min_edge_ok(v: float) -> bool:
    return math.isfinite(v) and _MIN_EDGE_BPS_MIN <= v <= _MIN_EDGE_BPS_MAX


def _market_cfgs(ctx) -> list:
    """Configurations de marché à régler : celles des moteurs en cours et celles du contexte
    (utilisées au prochain démarrage ; ce sont les mêmes objets quand le bot tourne)."""
    engines = getattr(getattr(ctx, "orch", None), "engines", None)  # absent en mode paire
    cfgs = [e.cfg for e in engines.values()] if engines else []
    for m in getattr(ctx, "markets", None) or []:
        if all(m is not c for c in cfgs):
            cfgs.append(m)
    return cfgs


def _global_min_edge(ctx) -> Optional[float]:
    edges = {c.min_edge_bps for c in _market_cfgs(ctx)}
    return edges.pop() if len(edges) == 1 else None


def create_app(ctx) -> FastAPI:
    app = FastAPI()
    @app.middleware("http")
    async def fresh_interface(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/") or request.url.path == "/api/status":
            response.headers["Cache-Control"] = "no-store"
        return response
    started_at = time.time()  # mode paire : incidents affichés depuis ce lancement
    # I7 : refuse tout en-tête Host autre que la machine locale (DNS rebinding).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    bridge = getattr(ctx, "browser_bridge", None)
    if bridge is not None:
        @app.websocket("/api/browser/bridge")
        async def browser_bridge(ws: WebSocket):
            await bridge.serve(ws)

        @app.get("/api/browser/status")
        async def browser_status():
            return {"transport": "extension", "connected": bridge.connected, "last_error": bridge.last_error}
    if WEB.exists():
        app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

    def _token(req: Request) -> str:
        return req.headers.get("x-auth-token", "")

    def _require(req: Request) -> None:
        """I7 : session wallet valide exigée (quel que soit le compte connecté)."""
        auth = getattr(ctx, "auth", None)
        if auth is None:
            raise HTTPException(400, _AUTH_MSG)
        try:
            auth.require_auth(_token(req), "")
        except RuntimeError:
            raise HTTPException(400, _AUTH_MSG)

    # -- authentification wallet (reprise du bot HIP-4) ----------------------

    @app.post("/api/auth/challenge")
    def auth_challenge(payload: Dict[str, str]):
        try:
            return ctx.auth.create_wallet_auth_challenge(payload.get("account_address", ""))
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/auth/verify")
    def auth_verify(payload: Dict[str, str]):
        try:
            return ctx.auth.verify_wallet_auth(payload.get("account_address", ""), payload.get("nonce", ""),
                                               payload.get("signature", ""))
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))

    # -- approbation de l'agent (reprise du bot HIP-4) -----------------------

    @app.get("/api/agent/status")
    def ag_status(account_address: str = Query("")):
        return authmod.agent_status(ctx.auth, account_address)

    @app.post("/api/agent/approve_challenge")
    def ag_challenge(request: Request, payload: Dict[str, Any]):
        acc = str(payload.get("account_address", ""))
        try:
            ctx.auth.require_auth(_token(request), acc)
            return ctx.auth.create_agent_approve_challenge(acc)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/agent/approve_submit")
    def ag_submit(request: Request, payload: Dict[str, Any]):
        acc = str(payload.get("account_address", ""))
        try:
            ctx.auth.require_auth(_token(request), acc)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
        try:
            return authmod.approve_submit(ctx.auth, acc, int(payload.get("nonce")), str(payload.get("signature", "")))
        except (RuntimeError, ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc))

    # -- contrôle du bot -------------------------------------------------

    @app.post("/api/bot/start")
    async def bot_start(request: Request, payload: Dict[str, Any]):
        acc = str(payload.get("account_address", ""))
        try:
            ctx.auth.require_auth(_token(request), acc)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
        try:
            await ctx.start_bot(acc)
        except (RuntimeError, VarError) as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.post("/api/bot/stop")
    async def bot_stop(request: Request):
        _require(request)
        # Arrêt normal : ferme les positions sur les deux venues, vérifie qu'elles sont à plat,
        # puis arrête le bot (main.py.Ctx.close_and_stop). Positions encore ouvertes au délai :
        # erreur 409 affichée par l'interface, le bot reste en marche et continue de fermer.
        # amendement t13 §8 : attendre la fin propre de la tâche avant de répondre ; replis
        # minimaux sinon (ctx de test).
        close = getattr(ctx, "close_and_stop", None)
        stop = getattr(ctx, "stop_bot", None)
        if callable(close):
            try:
                await close()
            except RuntimeError as exc:
                raise HTTPException(409, str(exc))
        elif callable(stop):
            await stop()
        elif getattr(ctx, "stop_event", None):
            ctx.stop_event.set()
        return {"ok": True}

    @app.post("/api/bot/kill")
    async def bot_kill(request: Request):
        _require(request)
        if ctx.orch is None:
            raise HTTPException(400, "bot non démarré")
        try:
            await ctx.orch.kill("manuel")
        finally:
            # étape d'arrêt affichée par l'interface (renseignée par l'orchestrateur pendant le kill).
            ctx.stop_step = None
        return {"ok": True}

    # -- marchés -----------------------------------------------------------

    @app.post("/api/market/{asset}/toggle")
    async def toggle(asset: str, request: Request):
        _require(request)
        e = ctx.orch.engines.get(asset) if ctx.orch else None
        if e is None:
            raise HTTPException(404, "marché inconnu")
        e.cfg.enabled = not e.cfg.enabled
        if e.cfg.enabled:
            e.resume()
        else:
            # amendement t13 §4 : ne débouclage jamais les positions ouvertes ici, seulement
            # annuler les ordres et passer à l'état IDLE.
            await e.cancel_all()
            e.state = IDLE
        return {"enabled": e.cfg.enabled}

    # C2 : les endpoints qui lisent l'orchestrateur ou le journal sont `async def` : exécutés sur
    # la boucle asyncio, jamais en parallèle d'elle dans un thread du serveur.
    @app.post("/api/market/{asset}/params")
    async def params(asset: str, payload: Dict[str, Any], request: Request):
        _require(request)
        e = ctx.orch.engines.get(asset) if ctx.orch else None
        if e is None:
            raise HTTPException(404, "marché inconnu")
        updates: Dict[str, float] = {}
        for k in _PARAM_FIELDS:
            if k not in payload:
                continue
            try:
                v = float(payload[k])
            except (TypeError, ValueError):
                raise HTTPException(400, f"{k} doit être un nombre")
            if k == "min_edge_bps":
                if not _min_edge_ok(v):
                    raise HTTPException(400, _MIN_EDGE_MSG)
            elif v <= 0:
                raise HTTPException(400, f"{k} doit être strictement positif")
            updates[k] = v
        fill_usd = updates.get("fill_usd", e.cfg.fill_usd)
        max_position_usd = updates.get("max_position_usd", e.cfg.max_position_usd)
        if fill_usd > max_position_usd:
            raise HTTPException(400, "fill_usd doit être inférieur ou égal à max_position_usd")
        for k, v in updates.items():
            setattr(e.cfg, k, v)
        # R2 : max_delta_usd suit fill_usd (1,5 x) tant qu'il n'a pas été fixé explicitement.
        if "fill_usd" in updates and getattr(e.cfg, "max_delta_auto", False):
            e.cfg.max_delta_usd = 1.5 * e.cfg.fill_usd
        return {"ok": True}

    @app.post("/api/params/min_edge")
    async def params_min_edge(payload: Dict[str, Any], request: Request):
        """Seuil d'inefficience global : min_edge_bps appliqué à chaud à tous les marchés."""
        _require(request)
        try:
            v = float(payload.get("bps"))
        except (TypeError, ValueError):
            raise HTTPException(400, _MIN_EDGE_MSG)
        if not _min_edge_ok(v):
            raise HTTPException(400, _MIN_EDGE_MSG)
        for c in _market_cfgs(ctx):
            c.min_edge_bps = v
        return {"ok": True, "min_edge_bps": v}

    @app.post("/api/market/{asset}/entry_mode")
    async def entry_mode(asset: str, payload: Dict[str, Any], request: Request):
        """L5 (levers-2026-09-23) : mode d'entrée d'un marché, à chaud : "chase", "lag_taker" ou
        "maker". Bot en marche : le moteur annule les ordres de l'ancien mode. Avant le
        démarrage : la configuration du prochain run."""
        _require(request)
        mode = payload.get("mode")
        if not isinstance(mode, str) or mode not in ENTRY_MODES:
            raise HTTPException(400, "mode d'entrée invalide (chase, lag_taker ou maker)")
        e = ctx.orch.engines.get(asset) if getattr(ctx, "orch", None) else None
        if e is not None:
            try:
                await e.set_entry_mode(mode)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            return {"ok": True, "entry_mode": mode}
        cfg = next((c for c in _market_cfgs(ctx) if getattr(c, "var_asset", None) == asset), None)
        if cfg is None:
            raise HTTPException(404, "marché inconnu")
        cfg.entry_mode = mode
        return {"ok": True, "entry_mode": mode}

    @app.post("/api/market/{asset}/unblock")
    async def unblock(asset: str, request: Request):
        """I5 : l'opérateur a vérifié le marché BLOQUÉ (delta au démarrage, anomalie...) et le
        débloque. Un marché désactivé reste à l'arrêt."""
        _require(request)
        e = ctx.orch.engines.get(asset) if ctx.orch else None
        if e is None:
            raise HTTPException(404, "marché inconnu")
        if e.state != BLOCKED:
            raise HTTPException(400, "marché non bloqué")
        e.resume()
        if not e.cfg.enabled:
            e.state = IDLE
        return {"state": e.state}

    # -- mode paire pétrole ----------------------------------------------------

    def _pair(ctx_) -> PairOrchestrator:
        orch = getattr(ctx_, "orch", None)
        if not isinstance(orch, PairOrchestrator):
            raise HTTPException(409, "mode paire pétrole inactif")
        return orch

    def _pair_cfg(ctx_) -> PairCfg:
        orch = getattr(ctx_, "orch", None)
        if isinstance(orch, PairOrchestrator):
            return orch.cfg
        cfg = getattr(ctx_, "pair", None)
        if orch is None and isinstance(cfg, PairCfg) and cfg.enabled:
            return cfg
        raise HTTPException(409, "mode paire pétrole inactif")

    @app.post("/api/pair/params")
    async def pair_params(payload: Dict[str, Any], request: Request):
        """Réglages à chaud de la paire. cycle_min s'applique au prochain décompte, leg_usd au
        prochain cycle : le cycle en cours n'est jamais interrompu (l'orchestrateur relit cfg)."""
        _require(request)
        cfg = _pair_cfg(ctx)
        updates: Dict[str, float] = {}
        for k, (lo, hi) in _PAIR_BOUNDS.items():
            if k not in payload:
                continue
            raw = payload[k]
            if isinstance(raw, bool):
                raise HTTPException(400, f"{k} doit être un nombre")
            try:
                v = float(raw)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{k} doit être un nombre")
            if not (math.isfinite(v) and lo <= v <= hi):
                raise HTTPException(400, f"{k} doit être compris entre {lo:g} et {hi:g}")
            if k == "volume_cycle_min" and 0 < v < 1:
                raise HTTPException(400, "cycle volume : 0 ou au moins 1 minute")
            updates[k] = v
        def apply():
            for k, v in updates.items():
                setattr(cfg, k, v)
            cfg.tranche_usd = cfg.effective_tranche_usd

        # Finish any in-flight cycle before changing its sizing/tolerances.
        lock = getattr(getattr(ctx, "orch", None), "_lock", None)
        if lock is not None:
            async with lock:
                apply()
        else:
            apply()
        return {"ok": True, "order_usd": cfg.effective_tranche_usd,
                **{k: getattr(cfg, k) for k in _PAIR_BOUNDS}}

    @app.post("/api/pair/resume")
    async def pair_resume(request: Request):
        """L'opérateur a vérifié la paire en PAUSE ou BLOQUÉ et la débloque."""
        _require(request)
        _pair(ctx).resume()
        return {"ok": True}

    # -- statut --------------------------------------------------------------

    @app.get("/api/status")
    async def status():
        base = ctx.orch.status() if ctx.orch else {"markets": [], "killed": False}
        try:
            cfg = _pair_cfg(ctx)
        except HTTPException:
            cfg = None
        if cfg is not None:
            if not isinstance(base.get("pair"), dict):
                base["pair"] = {"state": "ARRÊTÉ", "reason": "bot non démarré"}
            # réglages courants, pour pré-remplir les champs de l'interface
            base["pair"]["params"] = {k: getattr(cfg, k) for k in _PAIR_BOUNDS}
            base["pair"]["order_usd"] = cfg.effective_tranche_usd
        pair_mode = bool(getattr(ctx, "_pair_mode", lambda: False)())
        base["mode"] = "pair" if pair_mode else "arb"
        if pair_mode:
            # seuls les ordres de la paire (pas les anciennes lignes d'arbitrage HL ↔ Var)
            base["arbs"] = [a for a in ctx.journal.recent_arbs(300) if a.get("status") == "pair_fill"][:30]
        else:
            base["arbs"] = ctx.journal.recent_arbs(30)
        if pair_mode:
            # seuls les incidents depuis le lancement du serveur (pas ceux de l'ancien bot)
            base["incidents"] = [i for i in ctx.journal.recent_incidents(30) if (i.get("ts") or 0) >= started_at]
        else:
            base["incidents"] = ctx.journal.recent_incidents(30)
        base["running"] = ctx.orch is not None
        base["starting"] = getattr(ctx, "start_step", None)
        base["stopping"] = getattr(ctx, "stop_step", None)
        if "min_edge_bps" not in base:
            base["min_edge_bps"] = _global_min_edge(ctx)
        base["paper"] = bool(getattr(ctx, "paper", False))
        return base

    @app.get("/")
    def index():
        return FileResponse(str(WEB / "index.html"))

    return app
