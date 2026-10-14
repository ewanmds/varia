"""L9 (run réel du 23/09) : pas de dead-man switch HL (scheduleCancel exige 1 M$ de volume,
le compte en a 64 k$). Arrêt propre sur SIGTERM, SIGHUP et SIGINT, annulation globale à la
sortie du processus."""
import signal
import pytest
import socket
import threading

import uvicorn.server
from fastapi.testclient import TestClient

import main


class Ctx:
    def __init__(self):
        self.stopped = 0
        self.hl = None
        self.markets = []

    async def stop_bot(self):
        self.stopped += 1


def test_l9_app_shutdown_runs_clean_stop():
    ctx = Ctx()
    app = main.build_app(ctx)
    with TestClient(app):
        pass                                  # sortie du contexte = événement shutdown
    assert ctx.stopped == 1


def test_explicit_startup_runs_pair_and_shutdown_still_stops():
    event = threading.Event()

    class StartCtx(Ctx):
        async def start_bot(self, account):
            assert account == ""
            event.set()

    ctx = StartCtx()
    with TestClient(main.build_app(ctx, start_pair=True)):
        assert event.wait(2)
    assert ctx.stopped == 1


def test_port_collision_prevents_startup_before_any_orders(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "build_app", lambda *a, **k: calls.append(1))
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        with pytest.raises(OSError):
            main.run_local_server(Ctx(), occupied.getsockname()[1], start_pair=True)
    assert not calls


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP indisponible sur Windows")
def test_l9_sighup_triggers_same_clean_shutdown_as_sigterm(monkeypatch):
    monkeypatch.setattr(uvicorn.server, "HANDLED_SIGNALS", (signal.SIGINT, signal.SIGTERM))
    prev = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        main.install_signal_handling()
        assert set(uvicorn.server.HANDLED_SIGNALS) == {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
        main.install_signal_handling()            # idempotent
        assert len(uvicorn.server.HANDLED_SIGNALS) == 3
        # signal relancé par uvicorn après son arrêt propre : SystemExit (donc atexit), pas une
        # terminaison brute
        assert signal.getsignal(signal.SIGHUP) is main._exit_on_signal
        assert signal.getsignal(signal.SIGTERM) is main._exit_on_signal
        try:
            main._exit_on_signal(signal.SIGHUP, None)
            raise AssertionError("SystemExit attendu")
        except SystemExit as exc:
            assert exc.code == 128 + signal.SIGHUP
    finally:
        for s, h in prev.items():
            signal.signal(s, h)


def test_l9_atexit_cancels_all_hl_orders():
    class HL:
        def __init__(self):
            self.calls = []

        def cancel_all(self, coins):
            self.calls.append(coins)

    class Mk:
        def __init__(self, c):
            self.hl_coin = c

    ctx = Ctx()
    main.cancel_all_at_exit(ctx)              # aucun client : rien à faire, pas d'erreur
    ctx.hl = HL()
    ctx.markets = [Mk("xyz:SILVER"), Mk("xyz:GOLD")]
    main.cancel_all_at_exit(ctx)
    assert ctx.hl.calls == [["xyz:SILVER", "xyz:GOLD"]]


def test_l9_atexit_never_raises():
    class HL:
        def cancel_all(self, coins):
            raise RuntimeError("réseau coupé")

    ctx = Ctx()
    ctx.hl = HL()
    ctx.markets = []
    main.cancel_all_at_exit(ctx)
