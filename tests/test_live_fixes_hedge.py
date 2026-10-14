"""L5 et L6 (run réel du 23/09) : budget de couverture et lecture de base en échec."""
import asyncio
import time

from arb.config import GlobalCfg
from arb.engine import BLOCKED, PAUSED, QUOTING
from arb.var_errors import VarRejected
from tests.test_engine import FakeVar, Q, eng


def _fill(e, sz=5.28):
    return {"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": sz,
            "oid": e.orders["buy"]["oid"], "fee": 0, "tid": 1, "time": 1}


# -- L6 ----------------------------------------------------------------------------------------

class RejectedThenBaselineFails(FakeVar):
    """UKOILP 06:42:37 : 1er rfq envoyé puis rejeté (statut final), nouvel essai : la lecture de
    base échoue 2 fois, puis toute lecture des positions échoue."""

    def __init__(self):
        super().__init__()
        self.positions_calls = 0

    async def order_status(self, rfq_id, prio=3, **kw):
        return {"status": "rejected"}

    async def positions(self, **kw):
        self.positions_calls += 1
        if self.positions_calls == 1:
            return {"XAGS": {"qty": 0.0}}
        raise VarRejected(502, {"error_message": "bad gateway"})


async def test_l6_no_live_var_order_unwinds_hl_and_pauses():
    e = eng(RejectedThenBaselineFails(), g=GlobalCfg(hedge_retry_s=3.0))
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill(_fill(e))
    assert e.var.market_calls == 1          # un seul rfq, rejeté : aucun ordre Var vivant
    assert e.hl.iocs                         # débouclage HL
    assert e.state == PAUSED                 # et non BLOQUÉ
    assert e.pending_qty == 0 and e.pos_hl == 0
    # le motif de l'échec de lecture est conservé pour le diagnostic
    msg = [i[2] for i in e.journal.incidents if "base" in i[2]]
    assert msg and "502" in msg[-1]


class PendingThenFinalReadFails(FakeVar):
    """Un rfq vivant (statut jamais final) et lecture finale impossible : incertitude, BLOQUÉ."""

    def __init__(self):
        super().__init__()
        self.positions_calls = 0

    async def order_status(self, rfq_id, prio=3, **kw):
        return {"status": "pending"}

    async def positions(self, **kw):
        self.positions_calls += 1
        if self.positions_calls == 1:
            return {"XAGS": {"qty": 0.0}}
        raise RuntimeError("positions API down")


async def test_l6_live_var_order_still_blocks_when_uncertain():
    e = eng(PendingThenFinalReadFails(), g=GlobalCfg(hedge_retry_s=1.5))
    e._confirm_window_s = 100.0
    e._final_read_reserve_s = 0.3
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill(_fill(e))
    assert not e.hl.iocs and e.state == BLOCKED


# -- L5 ----------------------------------------------------------------------------------------

async def test_l5_budget_starts_after_gate_lock():
    """Le délai de couverture ne court pas pendant l'attente du verrou partagé."""
    e = eng(FakeVar(), g=GlobalCfg(hedge_retry_s=0.5))
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.gate.lock.acquire()

    async def release_later():
        await asyncio.sleep(0.8)            # plus long que tout le budget de couverture
        e.gate.lock.release()

    await asyncio.gather(e.on_fill(_fill(e)), release_later())
    assert not e.hl.iocs
    assert e.journal.arbs and e.journal.arbs[0][-1] == "hedged"


class PendingForever(FakeVar):
    """USOILP 06:32:12 : rfq envoyé, statut jamais final, budget épuisé dans la boucle."""

    async def order_status(self, rfq_id, prio=3, **kw):
        return {"status": "pending"}

    async def positions(self, **kw):
        return {"XAGS": {"qty": 0.0}}


async def test_l5_exhausted_budget_message_is_not_a_var_block():
    from arb.var_limiter import VarLimiter

    class LimitedVar(PendingForever):
        def __init__(self):
            super().__init__()
            self.limiter = VarLimiter(0.25, 600, 3)

        async def order_status(self, rfq_id, prio=3, deadline=None, **kw):
            await self.limiter.acquire(prio, deadline=deadline)
            return {"status": "pending"}

    e = eng(LimitedVar(), g=GlobalCfg(hedge_retry_s=1.2))
    e._confirm_window_s = 100.0
    e._final_read_reserve_s = 0.3
    e._status_poll_s = 0.2
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill(_fill(e))
    msgs = " ".join(i[2] for i in e.journal.incidents)
    assert "Var bloqué" not in msgs          # aucun 429 Var : le budget était simplement épuisé
    assert "budget de couverture épuisé" in msgs
