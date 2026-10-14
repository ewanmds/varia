from __future__ import annotations

import argparse
import asyncio
import atexit
import getpass
import os
import signal
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from arb.auth import AuthState
from arb.browser_bridge import BrowserBridge, ExtensionTransport, prepare_extension
from arb.config import PairCfg, load_config, load_pair_config
from arb.hl_client import HLClient
from arb.journal import Journal
from arb.keystore import KeyStore, KeyStoreError
from arb.orchestrator import Orchestrator
from arb.pair_orchestrator import PairOrchestrator
from arb.pair_paper import PaperPairVar
from arb.server import create_app
from arb.var_client import CdpTransport, VarClient
from arb.var_limiter import VarLimiter

ROOT = Path(__file__).resolve().parent


class Ctx:
    """Contexte partagé entre le serveur FastAPI et le cycle de vie du bot.

    `orch` reste `None` tant que le bot n'a pas été démarré. `start_bot` le
    construit et lance sa boucle en tâche de fond. `close_and_stop` (bouton
    « Arrêter », fichier data/STOP) ferme d'abord les positions sur les deux
    venues puis appelle `stop_bot`. `stop_bot` seul (arrêt du processus :
    SIGTERM, SIGHUP, fermeture de la fenêtre) annule les ordres et garde les
    positions. Tous deux ATTENDENT que la tâche se termine avant de remettre
    `orch` à `None` (amendement t13 §8, pour ne jamais laisser `/api/bot/stop`
    répondre "ok" avant que la boucle ait vraiment fini de tourner).
    """

    def __init__(self, g, markets, auth, journal, paper, pair=None):
        self.g, self.markets, self.auth, self.journal, self.paper = g, markets, auth, journal, paper
        # Mode paire pétrole (section [pair] de config.toml) : remplace les marchés chase/lag.
        self.pair = pair if pair is not None else PairCfg()
        self.orch = None
        self.stop_event = None
        self._task = None
        self._transport = None
        # L0 (run réel du 23/09) : vrai pendant tout le setup (environ 20 s). Sans ce drapeau,
        # chaque clic sur « Démarrer » pendant le setup lançait un orchestrateur de plus (3 en
        # parallèle le 23/09, dont 2 jamais arrêtés par /api/bot/stop).
        self._starting = False
        self.start_step = None  # étape de démarrage affichée par l'interface
        self.stop_step = None   # étape d'arrêt (propre ou d'urgence) affichée par l'interface
        self._closing = False   # arrêt avec fermeture des positions en cours
        # L9 : client HL du run en cours, pour l'annulation globale synchrone à la sortie (atexit).
        self.hl = None
        self.startup_pair_entry = None
        self.browser_bridge = (BrowserBridge(prepare_extension(ROOT))
                               if g.var_transport == "extension" else None)

    def _pair_mode(self) -> bool:
        pair = getattr(self, "pair", None)
        return bool(pair is not None and pair.enabled)

    def _step(self, step) -> None:
        self.start_step = step

    def _stop_step(self, step) -> None:
        self.stop_step = step

    async def start_bot(self, account: str) -> None:
        if self.orch is not None or self._task is not None:
            raise RuntimeError("bot déjà démarré")
        if self._starting:
            raise RuntimeError("démarrage déjà en cours")
        self._starting = True
        try:
            await self._start_bot(account)
        finally:
            self._starting = False
            self.start_step = None

    async def _start_bot(self, account: str) -> None:
        if self._pair_mode():
            await self._start_pair(account)
            return
        key = self.auth.agent_key_for(account)
        if not key and not self.paper:
            raise RuntimeError("aucun agent approuvé pour ce compte")
        for stale in ("STOP", "KILL"):  # un fichier de contrôle oublié ne doit pas couper le nouveau run
            (CONTROL_DIR / stale).unlink(missing_ok=True)
        self._step("Connexion au Chrome Variational et à Hyperliquid")
        transport = CdpTransport(self.g.var_cdp_port)
        # HLClient charge les métadonnées des dex HL en HTTP synchrone (plusieurs secondes) : dans
        # un thread, en parallèle de la connexion CDP, sans bloquer la boucle (ni l'interface).
        hl, _ = await asyncio.gather(
            asyncio.to_thread(HLClient, account, key or None, paper=self.paper), transport.connect())
        var = VarClient(transport, VarLimiter(self.g.var_min_interval_s, self.g.strike_window_s, self.g.strike_max,
                                              daily_strike_max=self.g.var_daily_strike_max))
        if self.paper:
            # amendement t13 §7 : en mode paper, aucun ordre Var réel ne doit jamais partir.
            # `order_status` garde la signature réelle (rfq, prio=...) utilisée par
            # arb/engine.py. Avec price=None, MarketEngine.hedge se replie sur le bid/ask de
            # la cotation, ce qui donne un écart réaliste en simulation.
            async def _no_market(*a, **k):
                return "paper"

            async def _paper_status(rfq, prio=0):
                return {"status": "cleared", "price": None}

            async def _no_lev(*a, **k):
                return None

            var.market = _no_market
            var.order_status = _paper_status
            var.set_leverage = _no_lev
        self.hl = hl
        orch = Orchestrator(self.g, self.markets, hl, var, self.journal)
        orch.progress = self._step
        orch.stop_progress = self._stop_step
        await orch.setup()
        self._step(None)
        self.orch = orch
        self.stop_event = asyncio.Event()
        self._transport = transport

        async def _run():
            try:
                await orch.run(self.stop_event, account)
            finally:
                await transport.close()

        self._task = asyncio.get_running_loop().create_task(_run())
        self._control_task = asyncio.get_running_loop().create_task(self._watch_control_files())

    async def _start_pair(self, account: str) -> None:
        """Mode paire pétrole : Var seul (aucun client ni agent HL). Le compte ne sert qu'à
        l'authentification de l'interface."""
        for stale in ("STOP", "KILL"):
            (CONTROL_DIR / stale).unlink(missing_ok=True)
        self._step("Connexion au Chrome habituel" if self.browser_bridge else "Connexion au Chrome Variational")
        transport = ExtensionTransport(self.browser_bridge) if self.browser_bridge else CdpTransport(self.g.var_cdp_port)
        try:
            await transport.connect()
        except BaseException:
            await transport.close()
            raise
        var = VarClient(transport, VarLimiter(self.g.var_min_interval_s, self.g.strike_window_s, self.g.strike_max,
                                              daily_strike_max=self.g.var_daily_strike_max))
        if self.paper:
            # aucun ordre Var réel : market, order_status et set_leverage neutralisés, positions
            # pétrole simulées (cotations réelles).
            var = PaperPairVar(var, self.pair)
        orch = PairOrchestrator(self.pair, self.g, var, self.journal)
        orch.progress = self._step
        orch.stop_progress = self._stop_step
        try:
            await orch.setup()
        except BaseException:
            await transport.close()
            raise
        if self.startup_pair_entry is not None:
            orch.request_entry(self.startup_pair_entry)
            self.startup_pair_entry = None
        self._step(None)
        self.orch = orch
        self.stop_event = asyncio.Event()
        self._transport = transport

        async def _run():
            try:
                await orch.run(self.stop_event, account)
            finally:
                await transport.close()

        self._task = asyncio.get_running_loop().create_task(_run())
        self._control_task = asyncio.get_running_loop().create_task(self._watch_control_files())

    async def _watch_control_files(self) -> None:
        """Arrêt à distance par fichier (pour un opérateur sans session wallet, ex. Claude) :
        data/STOP = arrêt normal, comme le bouton « Arrêter » : annule les ordres, ferme les
        positions sur les deux venues (Var puis HL), vérifie qu'elles sont à plat, puis arrête le
        bot. Positions encore ouvertes au délai : le bot reste en marche, continue de fermer, et la
        surveillance continue (un nouveau STOP ou un KILL reste possible).
        data/KILL = arrêt d'urgence (annule tout, referme les deux jambes, puis arrête le bot ; en
        mode paire, seulement une fois les deux positions pétrole à plat)."""
        stop_f, kill_f = CONTROL_DIR / "STOP", CONTROL_DIR / "KILL"
        kill_pending = False
        while self._task is not None and not self._task.done():
            try:
                if kill_f.exists():
                    kill_f.unlink(missing_ok=True)
                    self.journal.record_incident("*", "error", "arrêt d'urgence demandé par fichier KILL")
                    if self.orch is not None:
                        await self.orch.kill("fichier KILL")
                    # Mode paire : arrêt de la boucle seulement une fois les deux positions pétrole à
                    # plat. Sinon le bot reste en état arrêté et réessaie la fermeture toutes les
                    # 30 s, comme le bouton d'arrêt d'urgence.
                    if not self._pair_mode() or self.orch is None or await self.orch.is_flat():
                        await self.stop_bot()
                        return
                    kill_pending = True
                    print("[control] KILL : positions pétrole non fermées, nouvel essai en cours", flush=True)
                elif kill_pending and (self.orch is None or await self.orch.is_flat()):
                    await self.stop_bot()
                    return
                if stop_f.exists():
                    stop_f.unlink(missing_ok=True)
                    self.journal.record_incident("*", "warn", "arrêt demandé par fichier STOP")
                    try:
                        await self.close_and_stop()
                    except RuntimeError as exc:  # positions encore ouvertes : on continue de surveiller
                        print(f"[control] {exc}", flush=True)
                        continue
                    return
            except Exception as exc:  # ne jamais tuer la surveillance
                print(f"[control] erreur : {exc}", flush=True)
            await asyncio.sleep(1.0)

    async def close_and_stop(self) -> None:
        """Arrêt normal : ferme les positions (même sortie que l'arrêt d'urgence), attend que les
        positions réelles soient à plat sur HL et Var, puis arrête le bot. Positions encore
        ouvertes au délai `stop_flatten_timeout_s` : RuntimeError, le bot RESTE en marche en état
        arrêté et continue de réessayer les sorties (jamais de jambe laissée nue par un arrêt)."""
        if self.orch is None or self.stop_event is None or self._task is None:
            return
        if self._closing:
            raise RuntimeError("arrêt déjà en cours")
        self._closing = True
        try:
            timeout = getattr(self.g, "stop_flatten_timeout_s", 120.0)
            self._stop_step("Fermeture de la paire pétrole" if self._pair_mode() else "Annulation des ordres HL")
            if not await self.orch.close_for_stop(timeout):
                msg = "arrêt : positions non fermées, nouvel essai en cours"
                self.journal.record_incident("*", "error", msg)
                self._stop_step(None)
                raise RuntimeError(msg)
            await self.stop_bot()
        finally:
            self._closing = False

    async def stop_bot(self) -> None:
        if self.stop_event is None or self._task is None:
            return
        self._stop_step("Arrêt de la paire pétrole" if self._pair_mode() else "Annulation des ordres HL")
        try:
            self.stop_event.set()
            await self._task
            self.orch = None
            self._task = None
            self.stop_event = None
            self._transport = None
        finally:
            self._stop_step(None)


CONTROL_DIR = ROOT / "data"


def journal_path(paper: bool) -> Path:
    """Journal séparé en paper : un run paper ne compte ni dans la perte du jour ni dans le
    volume du jour d'un run réel."""
    return ROOT / "data" / ("journal-paper.db" if paper else "journal.db")


# -- L9 : arrêt propre sur signal et annulation globale à la sortie --------------------------
# Le dead-man switch HL (scheduleCancel) exige 1 M$ de volume cumulé : indisponible sur ce
# compte (64 k$ le 23/09). Ces gardes couvrent un arrêt demandé (SIGTERM, SIGINT, fermeture de
# la fenêtre du terminal = SIGHUP), pas un kill -9 ni une coupure de courant.

def build_app(ctx, *, start_pair=False):
    """Application FastAPI, plus l'arrêt propre du bot (annulation globale des ordres HL, puis
    attente de la fin de la boucle) à l'arrêt du serveur, qu'uvicorn déclenche sur signal."""
    app = create_app(ctx)

    @asynccontextmanager
    async def lifespan(app):
        async def start_when_connected():
            try:
                bridge = getattr(ctx, "browser_bridge", None)
                for _ in range(60):
                    if bridge is None or bridge.connected:
                        await ctx.start_bot("")
                        return
                    await asyncio.sleep(1)
                print("[démarrage] extension non connectée : démarrer depuis l'interface", flush=True)
            except Exception as exc:
                print(f"[démarrage] paire non démarrée : {type(exc).__name__}: {exc}", flush=True)

        startup = asyncio.create_task(start_when_connected()) if start_pair else None
        try:
            yield
        finally:
            if startup is not None and not startup.done():
                startup.cancel()
                await asyncio.gather(startup, return_exceptions=True)
            try:
                await ctx.stop_bot()
            except Exception as exc:
                print(f"[arrêt] arrêt propre du bot en échec : {type(exc).__name__}: {exc}", flush=True)

    app.router.lifespan_context = lifespan
    return app


def run_local_server(ctx, port, *, start_pair=False):
    # Réserver le port AVANT tout démarrage : deux instances ne peuvent pas
    # envoyer des ordres avant de découvrir une collision de port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(128)
        config = uvicorn.Config(build_app(ctx, start_pair=start_pair), host="127.0.0.1", port=port, log_level="warning")
        uvicorn.Server(config).run(sockets=[listener])


def install_signal_handling() -> None:
    """uvicorn intercepte SIGINT et SIGTERM (arrêt propre du serveur, donc du bot) ; on lui
    fait aussi intercepter SIGHUP, envoyé à la fermeture de la fenêtre du terminal, qui sinon
    tuerait le processus sur-le-champ, ordres HL laissés au carnet."""
    import uvicorn.server as us
    sigs = tuple(us.HANDLED_SIGNALS)
    hup = getattr(signal, "SIGHUP", None)
    if hup is not None and hup not in sigs:
        us.HANDLED_SIGNALS = sigs + (hup,)
    # Après son arrêt propre, uvicorn restaure ces gestionnaires puis relance le signal reçu :
    # SystemExit plutôt que la terminaison brute par défaut, pour que atexit s'exécute.
    for sig in (signal.SIGTERM, hup):
        if sig is not None:
            signal.signal(sig, _exit_on_signal)


def _exit_on_signal(signum, frame) -> None:
    raise SystemExit(128 + int(signum))


def cancel_all_at_exit(ctx) -> None:
    """atexit : dernière annulation globale synchrone des ordres HL du bot (jamais d'exception)."""
    hl = getattr(ctx, "hl", None)
    if hl is None:
        return
    coins = [m.hl_coin for m in (ctx.markets or [])]
    try:
        hl.cancel_all(coins)
        print("[arrêt] annulation globale HL à la sortie : ok", flush=True)
    except Exception as exc:
        print(f"[arrêt] annulation globale HL à la sortie en échec : {type(exc).__name__}: {exc}", flush=True)


# Mot de passe du keystore mémorisé sur ce PC (fichier lisible par l'utilisateur seul, 0600).
# Compromis assumé : quiconque accède à ce compte Linux peut lire la clé de l'agent HL (qui peut
# trader mais pas retirer). Supprimer le fichier pour revenir à la saisie à chaque lancement.
SECRET_FILE = Path.home() / ".config" / "bot-arb-varia" / "keystore_secret"


def read_keystore_secret() -> tuple[str, bool]:
    """Renvoie (mot de passe, lu_depuis_le_fichier)."""
    try:
        if SECRET_FILE.exists():
            return SECRET_FILE.read_text().strip(), True
    except OSError as exc:
        print(f"[keystore] lecture de {SECRET_FILE} impossible : {exc}", flush=True)
    return getpass.getpass("Mot de passe du keystore : "), False


def offer_to_remember(secret: str) -> None:
    try:
        answer = input("Mémoriser ce mot de passe sur ce PC pour ne plus le saisir ? (o/N) : ")
    except EOFError:
        return
    if answer.strip().lower() not in ("o", "oui", "y", "yes"):
        return
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SECRET_FILE.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    os.chmod(tmp, 0o600)
    tmp.write_text(secret)
    tmp.replace(SECRET_FILE)
    print(f"Mot de passe mémorisé dans {SECRET_FILE} (lisible par vous seul).", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--paper", action="store_true", help="aucun ordre envoyé (HL et Var)")
    ap.add_argument("--start-pair", action="store_true", help="démarrage explicite de la paire dès connexion du Chrome")
    ap.add_argument("--pair-entry-side", choices=("A", "B"), help="entrée ponctuelle explicite au démarrage, sans confirmation du signal")
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args()
    if args.pair_entry_side is not None and not args.start_pair:
        raise SystemExit("--pair-entry-side exige --start-pair")
    g, markets = load_config(args.config)
    pair = load_pair_config(args.config)
    if args.start_pair and not pair.enabled:
        raise SystemExit("--start-pair exige [pair] enabled = true")
    # La paire Var seule et la simulation n'utilisent aucune clé d'agent HL.
    secret, remembered = ("", True) if args.paper or pair.enabled else read_keystore_secret()
    if not secret.strip() and not args.paper and not pair.enabled:
        # amendement t13 §10 : mot de passe vide -> pas de mode réel (la clé de l'agent ne
        # serait pas chiffrée au repos). Le mode paper reste autorisé, il n'a besoin d'aucune
        # clé d'agent.
        raise SystemExit(
            "Mot de passe du keystore vide : refusé en mode réel (la clé de l'agent ne serait "
            "pas chiffrée). Relancez avec --paper pour tester sans clé, ou fournissez un mot "
            "de passe."
        )
    try:
        auth = AuthState(KeyStore(ROOT / "secrets.enc", secret, warn_if_disabled=not (pair.enabled or args.paper)))
    except KeyStoreError:
        # R3 : ne jamais repartir d'un keystore vide sur un mauvais mot de passe.
        hint = f" (mot de passe lu dans {SECRET_FILE}, supprimez ce fichier pour le ressaisir)" if remembered else ""
        raise SystemExit("Mot de passe incorrect : impossible de déchiffrer secrets.enc. Démarrage refusé." + hint)
    if not remembered and secret.strip():
        offer_to_remember(secret)
    journal = Journal(journal_path(args.paper))
    ctx = Ctx(g, markets, auth, journal, args.paper, pair=pair)
    ctx.startup_pair_entry = args.pair_entry_side
    print(f"Interface : http://127.0.0.1:{args.port}  (paper={args.paper})")
    # L9 : SIGHUP traité comme SIGTERM, et annulation globale synchrone à la sortie.
    install_signal_handling()
    atexit.register(cancel_all_at_exit, ctx)
    # jamais 0.0.0.0 : le serveur reste local à la machine.
    run_local_server(ctx, args.port, start_pair=args.start_pair)


if __name__ == "__main__":
    main()
