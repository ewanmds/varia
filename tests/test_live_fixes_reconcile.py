"""L4 et L6 (run réel du 23/09) : réconciliation sérialisée, jamais de correction sur un marché
BLOQUÉ ou IDLE (état revérifié sous le verrou), pending_qty remis à 0 quand tout est à plat."""
import asyncio

from arb.config import GlobalCfg
from arb.engine import BLOCKED, QUOTING
from arb.orchestrator import Orchestrator
from tests.test_orchestrator import HLC, J, M, VC, FakeHLFeed


async def _usoil_like(h=None, v=None):
    """06:32:24 : short HL non couvert (environ 500 $), Var à 0."""
    h = h or HLC()
    v = v or VC()
    o = Orchestrator(GlobalCfg(), [M], h, v, J(), clock=lambda: 0)
    await o.setup()
    e = o.engines["XAGS"]
    e.book = (66.9, 67.2)
    o.hl_feed = FakeHLFeed(book=(66.9, 67.2), mid=67.0)
    h.pos = {"xyz:SILVER": -10.0}
    return o, e, h, v


async def test_l4_reconcile_calls_are_serialized():
    class HLSlow(HLC):
        def __init__(self):
            super().__init__()
            self.inflight = 0
            self.max_inflight = 0

        def positions(self):
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            import time
            time.sleep(0.05)
            self.inflight -= 1
            return dict(self.pos)

    o, e, h, v = await _usoil_like(HLSlow())
    await asyncio.gather(o.reconcile(), o.reconcile(), o.reconcile())
    assert h.max_inflight == 1


async def test_l4_no_correction_when_market_blocked_during_correction():
    """« delta observé sans correction (marché bloqué) » puis « delta corrigé » : le marché est
    passé BLOQUÉ (ou IDLE) pendant la relecture faite sous le verrou, juste avant l'envoi."""
    holder = {}

    class HLBlockDuringReread(HLC):
        def positions_detail(self):
            holder["e"].state = BLOCKED          # blocage arrivé pendant la relecture
            return super().positions_detail()

    o, e, h, v = await _usoil_like(HLBlockDuringReread())
    holder["e"] = e
    await o.reconcile()                      # 1re lecture : "wait"
    await o.reconcile()                      # 2e : "correct", relecture sous le verrou
    assert h.iocs == []
    assert any("correction annulée" in i[2] for i in o.journal.inc)


async def test_l4_single_correction_for_one_delta():
    o, e, h, v = await _usoil_like()
    for _ in range(4):
        await o.reconcile()
    assert len(h.iocs) == 1


async def test_l6_pending_qty_reset_when_both_legs_flat():
    """06:42:58 et suivants : UKOILP BLOQUÉ, positions réelles à plat, pending_qty resté à 5,28."""
    o, e, h, v = await _usoil_like()
    h.pos = {}
    e.state, e.last_reason = BLOCKED, "couverture Var incertaine"
    e.pos_hl, e.pending_qty = 5.28, 5.28
    await o.reconcile()
    assert e.pos_hl == 0.0 and e.pending_qty == 0.0


async def test_l6_pending_qty_kept_while_hedge_inflight():
    o, e, h, v = await _usoil_like()
    h.pos = {}
    e.pending_qty = 5.28
    e.hedge_inflight = True
    await o.reconcile()
    assert e.pending_qty == 5.28
