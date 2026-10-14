import asyncio
import pytest
from arb.var_limiter import VarLimiter, QuotesSuspended, HEDGE, QUOTE

class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t

def make():
    c = Clock()
    async def fake_sleep(s): c.t += s
    return VarLimiter(0.25, 600, 3, clock=c, sleep=fake_sleep), c

async def test_min_interval():
    lim, c = make()
    await lim.acquire(QUOTE)
    t0 = c.t
    await lim.acquire(QUOTE)
    assert c.t - t0 >= 0.25

async def test_rate_limited_blocks_then_strikes_suspend_quotes():
    lim, c = make()
    lim.record_limited(15)
    assert lim.blocked_for() == pytest.approx(15)
    await lim.acquire(HEDGE)          # attend la fin du blocage
    assert lim.blocked_for() == 0
    lim.record_limited(1); lim.record_limited(1)
    assert lim.strikes_recent() == 3
    assert lim.quotes_suspended()
    with pytest.raises(QuotesSuspended):
        await lim.acquire(QUOTE)
    await lim.acquire(HEDGE)          # les couvertures passent toujours

async def test_strikes_expire():
    lim, c = make()
    for _ in range(3): lim.record_limited(0)
    c.t += 601
    assert lim.strikes_recent() == 0
    assert not lim.quotes_suspended()
