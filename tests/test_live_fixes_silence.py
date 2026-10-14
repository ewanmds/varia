"""L7 (run réel du 23/09, 06:39:52) : annulation globale en échec 3 fois de suite pendant un
silence HL (« 5, puis 2, puis 4 ordre(s) HL encore ouvert(s) »), succès à 06:40:10."""
from arb.config import GlobalCfg
from arb.orchestrator import Orchestrator
from arb.pricing import VarQuote
from tests.test_final_exits import NOON, VCX
from tests.test_orchestrator import HLC, J, M, FakeHLFeed


class HLCFlaky(HLC):
    def __init__(self, fails):
        super().__init__()
        self.fails = fails
        self.placed = []

    def cancel_all(self, coins):
        self.cancel_all_calls.append(list(coins))
        if self.fails > 0:
            self.fails -= 1
            raise RuntimeError(f"{self.fails + 1} ordre(s) HL encore ouvert(s) après annulation globale")

    def place_alo(self, *a):
        self.placed.append(a)
        return super().place_alo(*a)


async def _silent(fails):
    h = HLCFlaky(fails)
    o = Orchestrator(GlobalCfg(), [M], h, VCX(), J(), clock=lambda: NOON)
    await o.setup()
    o._global_cancel_retry_s = 0.01
    o.hl_feed = FakeHLFeed(stalled=True)
    return o, h


async def test_l7_global_cancel_retried_within_same_tick():
    o, h = await _silent(fails=2)
    await o.tick()
    assert len(h.cancel_all_calls) == 3
    assert o._hl_silence_active and o._need_reconcile
    assert any("flux HL muet" in i[2] for i in o.journal.inc)


async def test_l7_retries_capped_then_next_tick():
    o, h = await _silent(fails=10)
    await o.tick()
    assert len(h.cancel_all_calls) == 4          # 1 essai + 3 nouveaux essais espacés
    assert not o._hl_silence_active


async def test_l7_no_placement_while_silence_detected():
    o, h = await _silent(fails=10)
    await o.tick()
    e = o.engines["XAGS"]
    # un requote qui arriverait pendant le silence (tâche concurrente) ne pose rien
    q = VarQuote(67.0, 67.1, 7.5, ts=0, quote_id="q")
    e.book = (66.9, 67.2)
    await e.requote(q, 0.0, 66.9, 67.2, 1.0, None)
    assert h.placed == []
