"""Correctifs du premier run réel (2026-09-23) : L0, démarrages concurrents.

Le 23/09, 3 clics sur « Démarrer » pendant le setup (environ 20 s) ont lancé 3 orchestrateurs
en parallèle (3 x 422 levier par pétrole, 3 x « scheduleCancel indisponible » en 14 s)."""
import asyncio

import main


class J:
    def __init__(self):
        self.inc = []

    def record_incident(self, *a):
        self.inc.append(a)


class A:
    def agent_key_for(self, account):
        return None


class G:
    var_cdp_port = 9223
    var_min_interval_s = 0.25
    strike_window_s = 600
    strike_max = 3
    var_daily_strike_max = 5


def _patch(monkeypatch, tmp_path, built):
    monkeypatch.setattr(main, "CONTROL_DIR", tmp_path)

    class T:
        def __init__(self, port):
            pass

        async def connect(self):
            await asyncio.sleep(0.05)

        async def close(self):
            pass

    class O:
        def __init__(self, *a, **k):
            built.append(self)

        async def setup(self):
            await asyncio.sleep(0.2)   # le setup réel dure environ 20 s

        async def run(self, stop, account):
            await stop.wait()

    monkeypatch.setattr(main, "CdpTransport", T)
    monkeypatch.setattr(main, "Orchestrator", O)
    monkeypatch.setattr(main, "HLClient", lambda *a, **k: type("H", (), {"paper": True})())


async def test_l0_second_start_during_setup_is_refused(tmp_path, monkeypatch):
    built = []
    _patch(monkeypatch, tmp_path, built)
    ctx = main.Ctx(G(), [], A(), J(), paper=True)
    res = await asyncio.gather(ctx.start_bot("0xabc"), ctx.start_bot("0xabc"), ctx.start_bot("0xabc"),
                               return_exceptions=True)
    assert len(built) == 1
    assert sum(isinstance(r, RuntimeError) for r in res) == 2
    await ctx.stop_bot()


async def test_l0_start_allowed_again_after_failed_setup(tmp_path, monkeypatch):
    built = []
    _patch(monkeypatch, tmp_path, built)

    async def boom(self):
        raise RuntimeError("setup KO")

    ctx = main.Ctx(G(), [], A(), J(), paper=True)
    orig = main.Orchestrator.setup
    main.Orchestrator.setup = boom
    try:
        try:
            await ctx.start_bot("0xabc")
        except RuntimeError:
            pass
    finally:
        main.Orchestrator.setup = orig
    await ctx.start_bot("0xabc")
    assert ctx.orch is not None
    await ctx.stop_bot()


async def test_stop_bot_exposes_stop_steps_then_clears(tmp_path, monkeypatch):
    """Animation d'arrêt : l'étape d'arrêt est visible pendant stop_bot(), les étapes de
    l'orchestrateur (stop_progress) y sont relayées, et tout est effacé à la fin."""
    built = []
    _patch(monkeypatch, tmp_path, built)
    seen = []

    async def run(self, stop, account):
        await stop.wait()
        seen.append(ctx.stop_step)
        self.stop_progress("Attente des couvertures en cours")
        seen.append(ctx.stop_step)

    monkeypatch.setattr(main.Orchestrator, "run", run)
    ctx = main.Ctx(G(), [], A(), J(), paper=True)
    await ctx.start_bot("0xabc")
    assert ctx.stop_step is None
    await ctx.stop_bot()
    assert seen == ["Annulation des ordres HL", "Attente des couvertures en cours"]
    assert ctx.stop_step is None and ctx.orch is None
