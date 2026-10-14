import asyncio

import main
from arb.config import GlobalCfg


class J:
    def __init__(self): self.inc = []
    def record_incident(self, *a): self.inc.append(a)


class Orch:
    def __init__(self): self.killed, self.closed = None, 0
    async def kill(self, reason): self.killed = reason
    async def close_for_stop(self, timeout_s):
        self.closed += 1
        return True


def _ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONTROL_DIR", tmp_path)
    ctx = main.Ctx(GlobalCfg(), [], None, J(), paper=True)
    ctx.orch = Orch()
    ctx.stop_event = asyncio.Event()

    async def _run():
        await ctx.stop_event.wait()
    ctx._task = asyncio.get_running_loop().create_task(_run())
    return ctx


async def test_kill_file_kills_then_stops(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    orch = ctx.orch
    (tmp_path / "KILL").touch()
    await asyncio.wait_for(ctx._watch_control_files(), 5)
    assert orch.killed == "fichier KILL"
    assert ctx.orch is None and not (tmp_path / "KILL").exists()


async def test_stop_file_closes_positions_then_stops(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    orch = ctx.orch
    (tmp_path / "STOP").touch()
    await asyncio.wait_for(ctx._watch_control_files(), 5)
    # STOP = arrêt normal avec fermeture des positions (close_for_stop), pas l'arrêt d'urgence
    assert orch.closed == 1 and orch.killed is None and ctx.orch is None


async def test_pair_kill_file_stops_only_once_flat(tmp_path, monkeypatch):
    from arb.config import PairCfg
    ctx = _ctx(tmp_path, monkeypatch)
    ctx.pair = PairCfg(enabled=True)

    class PairOrch(Orch):
        flat = False

        async def is_flat(self):
            return self.flat

    orch = ctx.orch = PairOrch()
    (tmp_path / "KILL").touch()
    w = asyncio.get_running_loop().create_task(ctx._watch_control_files())
    await asyncio.sleep(1.5)
    # fermeture en échec : le bot reste en marche (arrêté), la boucle réessaie la fermeture
    assert orch.killed == "fichier KILL" and not w.done()
    assert ctx.orch is orch and not ctx.stop_event.is_set()
    orch.flat = True
    await asyncio.wait_for(w, 5)
    assert ctx.orch is None
