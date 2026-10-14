import pytest
from arb.var_client import VarClient, VarRateLimited, VarRejected, swap_instrument, format_qty
from arb.var_limiter import VarLimiter

class FakeTransport:
    def __init__(self, responses): self.responses = list(responses); self.calls = []
    async def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.responses.pop(0)

async def _nosleep(s): pass

def client(responses):
    t = FakeTransport(responses)
    return VarClient(t, VarLimiter(0, 600, 3, sleep=_nosleep)), t

def test_swap_instrument():
    assert swap_instrument("XAGS", "commodity") == {"instrument_type": "swap", "underlying": "XAGS",
        "settlement_asset": "USDC", "kind": "commodity", "funding_interval_s": 0}

def test_format_qty():
    assert format_qty(15.0) == "15"
    assert format_qty(0.000116) == "0.000116"

async def test_quote_parses():
    c, t = client([(200, {"bid": "67.06", "ask": "67.11", "qty": "15", "quote_id": "q1"})])
    q = await c.quote("XAGS", "commodity", 15)
    assert (q.bid, q.ask, q.quote_id) == (67.06, 67.11, "q1")
    assert t.calls[0][1] == "/api/quotes/indicative"

async def test_market_and_reduce_paths():
    c, t = client([(200, {"rfq_id": "r1"}), (200, {"rfq_id": "r2"})])
    assert await c.market("q1", "sell", 0.001) == "r1"
    assert t.calls[0][1] == "/api/orders/new/market"
    assert t.calls[0][2] == {"quote_id": "q1", "side": "sell", "max_slippage": 0.001}
    assert await c.market("q2", "buy", 0.001, reduce_only=True) == "r2"
    assert t.calls[1][1] == "/api/quotes/accept" and t.calls[1][2]["is_reduce_only"] is True

async def test_429_raises_rate_limited_and_records_strike():
    c, t = client([(429, {"wait_until_seconds": 15})])
    with pytest.raises(VarRateLimited) as e:
        await c.quote("XAGS", "commodity", 15)
    assert e.value.wait_s == 15
    assert c.limiter.strikes_recent() == 1

async def test_other_error_rejected():
    c, t = client([(422, {"error_message": "max"})])
    with pytest.raises(VarRejected):
        await c.market("q", "buy", 0.001)

async def test_positions_parsing():
    c, t = client([(200, [{"position_info": {"instrument": {"underlying": "XAGS"}, "qty": "-15",
                    "avg_entry_price": "67.1"}, "estimated_liquidation_price": "72.0", "value": "-1006"}])])
    p = await c.positions()
    assert p["XAGS"]["qty"] == -15.0 and p["XAGS"]["liq"] == 72.0

async def test_order_status_finds_rfq():
    c, t = client([(200, {"result": [{"rfq_id": "a", "status": "cleared", "price": "1"}, {"rfq_id": "b"}]})])
    assert (await c.order_status("a"))["status"] == "cleared"


async def test_authenticated_reads_update_session_without_logging_tokens():
    c, _ = client([(401, {}), (200, [])])
    with pytest.raises(VarRejected):
        await c.positions()
    assert c._health_ok is False
    await c.positions()
    assert c._health_ok is True
