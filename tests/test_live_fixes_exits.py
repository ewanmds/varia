"""L2 et L3 (run réel du 23/09) : réduction forcée suivie d'une PAUSE avec refroidissement,
BLOQUÉ à la 2e en moins de 30 min ; une seule sortie HL par position, pas de nouvel essai
avant une relecture des positions."""
import asyncio
import time

from arb.config import GlobalCfg
from arb.engine import BLOCKED, PAUSED, QUOTING
from arb.orchestrator import Orchestrator
from tests.test_final_exits import NOON, VCX
from tests.test_orchestrator import HLC, J, M, FakeHLFeed


class HLCFlat(HLC):
    """IOC de sortie rempli : la position HL disparaît vraiment."""

    def ioc(self, coin, is_buy, sz, px, reduce_only=False):
        self.iocs.append((coin, is_buy, sz, px))
        self.pos = {coin: 0.0}
        return {"oid": 2, "filled_sz": sz, "avg_px": px, "error": None}


async def _xags_like(h=None):
    """Position XAGS du 23/09 : short HL 7,55, long Var 7,55, liquidation Var à 5,4 %."""
    h = h or HLCFlat()
    o = Orchestrator(GlobalCfg(), [M], h, VCX(), J(), clock=lambda: NOON)
    await o.setup()
    e = o.engines["XAGS"]
    h.pos = {"xyz:SILVER": -7.55}
    o.var.pos = {"XAGS": {"qty": 7.55, "entry": 67.0}}
    e.pos_hl, e.pos_var = -7.55, 7.55
    o.var_liq["XAGS"] = 0.054
    o.hl_feed = FakeHLFeed()
    return o, e, h


def test_l2_cooldown_default_is_10_min():
    assert GlobalCfg().forced_reduction_cooldown_s == 600.0


async def test_l2_forced_reduction_pauses_market_instead_of_requoting():
    o, e, h = await _xags_like()
    await o.tick()
    assert len(h.iocs) == 1
    assert e.state == PAUSED            # avant : restauré à QUOTING, et le marché se refaisait remplir
    assert "réduction forcée" in e.last_reason
    # pendant le refroidissement, aucune recotation
    o.var_liq["XAGS"] = None
    await o.tick()
    assert e.state == PAUSED


async def test_l2_cooldown_expiry_resumes_quoting():
    o, e, h = await _xags_like()
    await o.tick()
    assert e.state == PAUSED
    o.var_liq["XAGS"] = None
    o._forced_cooldown_until["XAGS"] = time.monotonic() - 1
    await o.tick()
    assert e.state == QUOTING


async def test_l2_second_forced_reduction_within_30_min_blocks():
    o, e, h = await _xags_like()
    await o.tick()
    assert e.state == PAUSED
    # fin du refroidissement, le marché recote et se fait remplir à nouveau
    o._forced_cooldown_until["XAGS"] = time.monotonic() - 1
    await o.tick()
    assert e.state == QUOTING
    h.pos = {"xyz:SILVER": -7.55}
    o.var.pos = {"XAGS": {"qty": 7.55, "entry": 67.0}}
    e.pos_hl, e.pos_var = -7.55, 7.55
    o._next_taker_ts["XAGS"] = 0.0
    await o.reconcile()
    o.var_liq["XAGS"] = 0.054
    await o.tick()
    assert len(h.iocs) == 2
    assert e.state == BLOCKED


class HLCStale(HLC):
    """IOC de sortie qui remplit mais dont la réponse est perdue (délai HTTP), puis état HL
    relu en retard une fois (clearinghouseState pas encore à jour)."""

    def __init__(self):
        super().__init__()
        self.stale = None

    def ioc(self, coin, is_buy, sz, px, reduce_only=False):
        self.iocs.append((coin, is_buy, sz, px))
        if self.pos.get(coin, 0.0) == 0.0:
            return {"oid": None, "filled_sz": 0.0, "avg_px": None,
                    "error": "Reduce only order would increase position. asset=110026"}
        self.stale = dict(self.pos)
        self.pos = {coin: 0.0}
        raise TimeoutError("réponse HL perdue")

    def positions_detail(self):
        if self.stale is not None:
            st, self.stale = self.stale, None
            return {c: {"szi": v, "entry": None} for c, v in st.items()}
        return super().positions_detail()


async def test_l3_no_second_exit_before_positions_reread():
    """06:31:52 : la sortie HL est repartie sur une position déjà fermée (« Reduce only order
    would increase position »)."""
    o, e, h = await _xags_like(HLCStale())
    await o.tick()
    assert len(h.iocs) == 1
    o._next_taker_ts["XAGS"] = 0.0      # backoff de 5 s écoulé
    await o.tick()
    assert len(h.iocs) == 1              # pas de 2e IOC tant que les positions n'ont pas été relues
    await o.reconcile()                  # relecture : HL à plat
    o._next_taker_ts["XAGS"] = 0.0
    await o.tick()
    assert len(h.iocs) == 1 and e.pos_hl == 0.0


async def test_l3_exit_guard_single_flatten_in_flight():
    o, e, h = await _xags_like(HLCStale())
    b, k = o._book("xyz:SILVER")
    await asyncio.gather(e.flatten(True, b, k), e.flatten(True, b, k))
    assert len(h.iocs) == 1
