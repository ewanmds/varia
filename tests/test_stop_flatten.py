"""Arrêt normal (bouton « Arrêter », fichier data/STOP) : fermeture des positions sur les deux
venues (même chemin de sortie que l'arrêt d'urgence), vérification sur les positions RÉELLES,
puis seulement arrêt du bot. Positions encore ouvertes au délai : le bot reste en marche."""
import asyncio
import dataclasses

from fastapi.testclient import TestClient

import main
from arb.config import GlobalCfg
from arb.engine import IDLE
from arb.orchestrator import Orchestrator
from arb.server import create_app
from tests.test_orchestrator import HLC2, J, M, M2, VC2


# -- orchestrateur -----------------------------------------------------------------------------

def _open_positions(h, v):
    h.pos = {"xyz:SILVER": 3.0, "xyz:GOLD": -0.5}
    v.pos = {"XAGS": {"qty": -3.0, "entry": 67, "liq": None},
             "GOLD": {"qty": 0.5, "entry": 2000, "liq": None}}


async def _orch(disabled_gold=False):
    h, v = HLC2(), VC2()
    m2 = dataclasses.replace(M2, enabled=False) if disabled_gold else M2
    o = Orchestrator(GlobalCfg(), [M, m2], h, v, J(), clock=lambda: 0)
    await o.setup()
    _open_positions(h, v)
    return o, h, v


def _closing_flatten(o, h, v, asset, coin):
    calls = []

    async def fake(taker, b, a, var_mid=None):
        calls.append(taker)
        v.pos.pop(asset, None)          # Var d'abord
        h.pos.pop(coin, None)           # puis HL
    o.engines[asset].flatten = fake
    return calls


def test_global_cfg_default_stop_flatten_timeout():
    assert GlobalCfg().stop_flatten_timeout_s == 120.0


async def test_close_for_stop_flattens_every_market_including_disabled_then_verifies():
    o, h, v = await _orch(disabled_gold=True)
    assert o.engines["GOLD"].state == IDLE
    c1 = _closing_flatten(o, h, v, "XAGS", "xyz:SILVER")
    c2 = _closing_flatten(o, h, v, "GOLD", "xyz:GOLD")
    steps = []
    o.stop_progress = steps.append
    ok = await o.close_for_stop(timeout_s=1.0, poll_s=0.01)
    assert ok is True
    assert c1 == [True] and c2 == [True]          # flatten(taker=True) sur chaque marché
    assert "Vérification des positions" in steps
    assert o.killed is True and o.emergency is False
    assert not any("arrêt d'urgence" in i[2] for i in o.journal.inc)


async def test_close_for_stop_waits_until_both_venues_flat():
    o, h, v = await _orch()
    _closing_flatten(o, h, v, "GOLD", "xyz:GOLD")

    async def partial(taker, b, a, var_mid=None):
        v.pos.pop("XAGS", None)                 # Var fermé, HL pas encore
    o.engines["XAGS"].flatten = partial

    async def later():
        await asyncio.sleep(0.05)
        h.pos.pop("xyz:SILVER", None)           # nouvel essai (tick) réussi
    t = asyncio.create_task(later())
    ok = await o.close_for_stop(timeout_s=2.0, poll_s=0.01)
    await t
    assert ok is True and h.pos == {} and v.pos == {}


async def test_close_for_stop_timeout_returns_false_and_keeps_killed_state():
    o, h, v = await _orch()

    async def noop(taker, b, a, var_mid=None):
        return None
    for e in o.engines.values():
        e.flatten = noop
    t0 = asyncio.get_running_loop().time()
    ok = await o.close_for_stop(timeout_s=0.1, poll_s=0.01)
    assert ok is False
    assert asyncio.get_running_loop().time() - t0 < 1.0
    assert o.killed is True                      # les sorties restent réessayées par tick()


async def test_positions_flat_read_failure_is_not_flat():
    o, h, v = await _orch()
    h.pos, v.pos = {}, {}

    async def boom():
        raise RuntimeError("Var muet")
    v.positions = boom
    assert await o.positions_flat() is False
    assert any("vérification des positions" in i[2] for i in o.journal.inc)


async def test_positions_flat_true_when_only_zero_positions():
    o, h, v = await _orch()
    h.pos = {"xyz:SILVER": 0.0}
    v.pos = {"XAGS": {"qty": 0.0}}
    assert await o.positions_flat() is True


async def test_emergency_kill_during_normal_stop_marks_emergency():
    o, h, v = await _orch()
    _closing_flatten(o, h, v, "XAGS", "xyz:SILVER")
    _closing_flatten(o, h, v, "GOLD", "xyz:GOLD")
    await o.kill("arrêt demandé", emergency=False)
    assert o.status()["emergency"] is False
    await o.kill("manuel")
    assert o.status()["emergency"] is True and o.status()["killed"] is True


# -- Ctx (main.py) -------------------------------------------------------------------------------

class CJ:
    def __init__(self): self.inc = []
    def record_incident(self, *a): self.inc.append(a)


class FOrch:
    def __init__(self, result=True):
        self.result, self.calls, self.killed = result, [], None

    async def close_for_stop(self, timeout_s):
        self.calls.append(timeout_s)
        return self.result

    async def kill(self, reason):
        self.killed = reason


def _ctx(tmp_path, monkeypatch, result=True, g=None):
    monkeypatch.setattr(main, "CONTROL_DIR", tmp_path)
    ctx = main.Ctx(g if g is not None else GlobalCfg(), [], None, CJ(), paper=True)
    ctx.orch = FOrch(result)
    ctx.stop_event = asyncio.Event()

    async def _run():
        await ctx.stop_event.wait()
    ctx._task = asyncio.get_running_loop().create_task(_run())
    return ctx


async def test_close_and_stop_flattens_then_stops(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, g=GlobalCfg(stop_flatten_timeout_s=42.0))
    orch = ctx.orch
    await ctx.close_and_stop()
    assert orch.calls == [42.0]
    assert ctx.orch is None and ctx._task is None and ctx.stop_step is None


async def test_close_and_stop_timeout_keeps_bot_running_and_raises(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, result=False)
    orch = ctx.orch
    try:
        await ctx.close_and_stop()
        raise AssertionError("RuntimeError attendu")
    except RuntimeError as exc:
        assert str(exc) == "arrêt : positions non fermées, nouvel essai en cours"
    assert orch.calls == [120.0]                 # défaut sans config
    assert ctx.orch is orch and not ctx.stop_event.is_set() and not ctx._task.done()
    assert ("*", "error", "arrêt : positions non fermées, nouvel essai en cours") in ctx.journal.inc
    assert ctx.stop_step is None
    ctx.stop_event.set()
    await ctx._task


async def test_close_and_stop_refuses_concurrent_call(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    gate = asyncio.Event()

    async def slow(timeout_s):
        await gate.wait()
        return True
    ctx.orch.close_for_stop = slow
    first = asyncio.create_task(ctx.close_and_stop())
    await asyncio.sleep(0)
    try:
        await ctx.close_and_stop()
        raise AssertionError("RuntimeError attendu")
    except RuntimeError as exc:
        assert "déjà en cours" in str(exc)
    gate.set()
    await first
    assert ctx.orch is None


async def test_plain_stop_bot_does_not_flatten(tmp_path, monkeypatch):
    """Arrêt du processus (SIGTERM, SIGHUP, atexit) : annulation seule, pas de fermeture."""
    ctx = _ctx(tmp_path, monkeypatch)
    orch = ctx.orch
    await ctx.stop_bot()
    assert orch.calls == [] and ctx.orch is None


async def test_stop_file_failure_keeps_watching(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, result=False)
    orch = ctx.orch
    (tmp_path / "STOP").touch()
    watch = asyncio.create_task(ctx._watch_control_files())
    for _ in range(100):
        if orch.calls:
            break
        await asyncio.sleep(0.01)
    assert orch.calls and not watch.done() and ctx.orch is orch
    (tmp_path / "KILL").touch()                  # KILL inchangé : arrêt d'urgence puis arrêt
    await asyncio.wait_for(watch, 5)
    assert orch.killed == "fichier KILL" and ctx.orch is None


# -- serveur ---------------------------------------------------------------------------------------

class SAuth:
    def require_auth(self, token, acc):
        if token != "t":
            raise RuntimeError("non")


class SCtx:
    def __init__(self, fail=False):
        self.auth, self.orch, self.paper, self.fail = SAuth(), None, False, fail
        self.closed, self.stopped = 0, 0

    async def close_and_stop(self):
        self.closed += 1
        if self.fail:
            raise RuntimeError("arrêt : positions non fermées, nouvel essai en cours")

    async def stop_bot(self):
        self.stopped += 1


def _client(ctx):
    return TestClient(create_app(ctx), base_url="http://127.0.0.1", headers={"x-auth-token": "t"})


def test_api_stop_closes_positions():
    ctx = SCtx()
    res = _client(ctx).post("/api/bot/stop")
    assert res.status_code == 200 and ctx.closed == 1 and ctx.stopped == 0


def test_api_stop_returns_error_when_positions_remain():
    ctx = SCtx(fail=True)
    res = _client(ctx).post("/api/bot/stop")
    assert res.status_code == 409
    assert res.json()["detail"] == "arrêt : positions non fermées, nouvel essai en cours"


# -- reliquat sous le minimum (stop_dust_usd) -----------------------------------------------------

def test_global_cfg_default_stop_dust_usd():
    assert GlobalCfg().stop_dust_usd == 12.0


async def test_dust_only_residual_lets_stop_complete_with_one_incident():
    o, h, v = await _orch()
    _closing_flatten(o, h, v, "GOLD", "xyz:GOLD")
    tries = []

    async def dust_left(taker, b, a, var_mid=None):
        tries.append(taker)                      # sortie tentée, mais HL refuse le reliquat
        v.pos.pop("XAGS", None)
        h.pos["xyz:SILVER"] = 0.016              # 0,016 x 67,08 = 1,07 $ sur une seule jambe
    o.engines["XAGS"].flatten = dust_left
    ok = await o.close_for_stop(timeout_s=0.3, poll_s=0.01)
    assert ok is True and tries == [True]
    msgs = [i for i in o.journal.inc if "reliquat" in i[2]]
    assert len(msgs) == 1
    assert msgs[0][0] == "XAGS" and msgs[0][1] == "warn"
    assert msgs[0][2] == "arrêt : reliquat de 1.07 $ sur XAGS (HL) sous le minimum, à solder à la main"


async def test_residual_above_dust_threshold_still_blocks_stop():
    o, h, v = await _orch()
    _closing_flatten(o, h, v, "GOLD", "xyz:GOLD")

    async def left(taker, b, a, var_mid=None):
        v.pos.pop("XAGS", None)
        h.pos["xyz:SILVER"] = 0.2                # 0,2 x 67,08 = 13,4 $ > 12 $
    o.engines["XAGS"].flatten = left
    ok = await o.close_for_stop(timeout_s=0.1, poll_s=0.01)
    assert ok is False
    assert not any("reliquat" in i[2] for i in o.journal.inc)


async def test_dust_on_var_leg_counts_as_flat():
    o, h, v = await _orch()
    h.pos = {}
    v.pos = {"GOLD": {"qty": 0.004}}             # 0,004 x 2000,5 = 8 $
    warned = set()
    assert await o.positions_flat(12.0, warned) is True
    assert await o.positions_flat(12.0, warned) is True
    msgs = [i for i in o.journal.inc if "reliquat" in i[2]]
    assert len(msgs) == 1 and "GOLD (Var)" in msgs[0][2]
