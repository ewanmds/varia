"""L1 (run réel du 23/09) : levier Var par défaut 3, borné au maximum Var, marché BLOQUÉ si
le réglage échoue."""
from datetime import datetime, timezone

from arb.config import GlobalCfg, MarketCfg
from arb.engine import BLOCKED, QUOTING
from arb.orchestrator import Orchestrator
from arb.var_client import parse_max_leverage
from arb.var_errors import VarRejected
from tests.test_orchestrator import HLC, J, VC

NOON = datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp()
# corps exact renvoyé par Var le 23/09 pour USOILP et UKOILP
BODY_422 = {"error_message": 'leverage: Validation error: range [{"max": Number(5.0)}]'}


class VCMax(VC):
    """Var qui refuse tout levier au-delà de `max_lev` avec le 422 observé."""

    def __init__(self, max_lev=5):
        super().__init__()
        self.max_lev = max_lev

    async def set_leverage(self, a, l):
        self.lev.append((a, l))
        if l > self.max_lev:
            raise VarRejected(422, BODY_422)


class HLCLev(HLC):
    def __init__(self):
        super().__init__()
        self.hl_lev = []

    def set_cross_leverage(self, coin, lev):
        self.hl_lev.append((coin, lev))


def test_l1_default_var_leverage_is_5():
    assert MarketCfg(var_asset="X", var_kind="index", hl_coin="xyz:X").var_leverage == 5


def test_l1_parse_max_leverage_from_observed_422():
    assert parse_max_leverage(BODY_422) == 5
    assert parse_max_leverage(str(BODY_422)) == 5
    assert parse_max_leverage({"error_message": "autre chose"}) is None
    assert parse_max_leverage(None) is None


async def test_l1_leverage_above_var_max_is_retried_at_max():
    m = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", var_leverage=10)
    h, v = HLCLev(), VCMax(5)
    o = Orchestrator(GlobalCfg(), [m], h, v, J(), clock=lambda: NOON)
    await o.setup()
    assert v.lev == [("XAGS", 10), ("XAGS", 5)]
    assert o.engines["XAGS"].state == QUOTING
    # levier HL aligné sur le levier Var réellement retenu
    assert h.hl_lev == [("xyz:SILVER", 5)]


async def test_l1_leverage_failure_blocks_market():
    class VCKo(VC):
        async def set_leverage(self, a, l):
            raise VarRejected(500, {"error_message": "boom"})

    m = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER")
    o = Orchestrator(GlobalCfg(), [m], HLCLev(), VCKo(), J(), clock=lambda: NOON)
    await o.setup()
    e = o.engines["XAGS"]
    assert e.state == BLOCKED and e.last_reason == "levier Var non réglé"


async def test_l1_leverage_retry_failure_blocks_market():
    class VCKo2(VCMax):
        async def set_leverage(self, a, l):
            self.lev.append((a, l))
            raise VarRejected(422, BODY_422)

    m = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", var_leverage=10)
    v = VCKo2()
    o = Orchestrator(GlobalCfg(), [m], HLCLev(), v, J(), clock=lambda: NOON)
    await o.setup()
    assert v.lev == [("XAGS", 10), ("XAGS", 5)]
    assert o.engines["XAGS"].state == BLOCKED
