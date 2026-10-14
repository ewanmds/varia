"""L8 (run réel du 23/09) : cotations Var jugées périmées 9 à 33 % du temps (âge médian 2 s,
seuil 3 s) -> annulations et reposes inutiles, budget HL qui fond."""
import time
from pathlib import Path

from arb.config import GlobalCfg, load_config
from arb.engine import QUOTING
from arb.orchestrator import Orchestrator
from arb.pricing import VarQuote
from tests.test_final_exits import NOON, VCX
from tests.test_orchestrator import HLC, J, M, FakeHLFeed


class HLCRec(HLC):
    def __init__(self):
        super().__init__()
        self.cancels, self.placed = [], []

    def cancel(self, coin, oid):
        self.cancels.append(oid)
        return True

    def place_alo(self, *a):
        self.placed.append(a)
        return super().place_alo(*a)


def test_l8_defaults():
    g = GlobalCfg()
    assert g.quote_max_age_s == 5.0 and g.quote_cancel_age_s == 8.0
    g2, _ = load_config(Path(__file__).resolve().parent.parent / "config.toml")
    assert g2.quote_max_age_s == 5.0 and g2.quote_cancel_age_s == 8.0


async def _resting(age):
    h = HLCRec()
    o = Orchestrator(GlobalCfg(), [M], h, VCX(), J(), clock=lambda: NOON)
    await o.setup()
    o.hl_feed = FakeHLFeed(book=(67.04, 67.06), mid=67.05)
    e = o.engines["XAGS"]
    e.orders = {"buy": {"oid": 11, "px": 66.95, "sz": 14.9}, "sell": {"oid": 12, "px": 67.15, "sz": 14.9}}
    e._my_oids |= {11, 12}
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 14.9, ts=time.time() - age, quote_id="q")
    return o, e, h


async def test_l8_stale_but_young_quote_keeps_resting_orders():
    o, e, h = await _resting(6.0)                # > 5 s (périmée) mais < 8 s
    await o.tick()
    assert h.cancels == [] and h.placed == []
    assert e.orders["buy"] and e.orders["sell"]
    assert e.last_reason == "cotation Var périmée" and e.state == QUOTING


async def test_l8_quote_older_than_cancel_age_cancels():
    o, e, h = await _resting(9.0)
    await o.tick()
    assert sorted(h.cancels) == [11, 12]


async def test_l8_stale_with_other_reason_still_cancels():
    o, e, h = await _resting(6.0)
    o.session_ok = False                         # autre motif : annulation immédiate
    await o.tick()
    assert sorted(h.cancels) == [11, 12]


async def test_l8_stale_empty_side_is_not_placed():
    o, e, h = await _resting(6.0)
    e.orders["sell"] = None
    await o.tick()
    assert h.placed == []
