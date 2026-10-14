import asyncio
import pytest
from arb.config import MarketCfg
from arb.pricing import VarQuote
from arb.var_feed import VarFeed
from arb.var_client import VarRateLimited
from arb.var_limiter import QuotesSuspended

M = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", fill_usd=1000)

class FakeVar:
    def __init__(self, exc=None): self.exc = exc; self.calls = []
    async def quote(self, asset, kind, qty, prio=2):
        self.calls.append((asset, qty))
        if self.exc: raise self.exc
        return VarQuote(66.9, 67.1, qty, ts=100.0, quote_id="q")

async def test_poll_uses_fill_qty_and_stores():
    v = FakeVar(); f = VarFeed(v, [M], 2.0, clock=lambda: 101.0)
    f.set_ref_price("XAGS", 50.0)
    await f.poll_once("XAGS")
    assert v.calls[0] == ("XAGS", 20.0)
    assert f.latest("XAGS").bid == 66.9
    assert f.age("XAGS") == 1.0
    # le mid de la cotation devient le prix de référence
    assert f.qty_for("XAGS") == 1000 / 67.0

async def test_poll_without_ref_price_skips():
    v = FakeVar(); f = VarFeed(v, [M], 2.0)
    await f.poll_once("XAGS")
    assert v.calls == [] and f.latest("XAGS") is None

async def test_errors_are_swallowed():
    for exc in (VarRateLimited(5), QuotesSuspended()):
        v = FakeVar(exc); f = VarFeed(v, [M], 2.0)
        f.set_ref_price("XAGS", 67.0)
        await f.poll_once("XAGS")
        assert f.latest("XAGS") is None and f.errors["XAGS"] == 1


# -- F2 / F1 : ordonnancement (docs/superpowers/fresh-quote-2026-09-23.md) -----------------------

class Clock:
    def __init__(self, t=0.0): self.t = t
    def __call__(self): return self.t


class TimedVar:
    """Cotation instantanée ; ts = horloge simulée au moment de la réponse."""
    def __init__(self, clock, fail=()):
        self.clock, self.fail, self.calls = clock, set(fail), []
    async def quote(self, asset, kind, qty, prio=2):
        self.calls.append((round(self.clock.t, 3), asset))
        if asset in self.fail:
            raise VarRateLimited(1)
        return VarQuote(1.0, 1.1, qty, ts=self.clock.t, quote_id="q")


def _mk_feed(n=6, disabled=(), fail=(), hot=()):
    ck = Clock()
    ms = [MarketCfg(var_asset=f"A{i}", var_kind="index", hl_coin=f"xyz:A{i}", fill_usd=500,
                    enabled=f"A{i}" not in disabled) for i in range(n)]
    v = TimedVar(ck, fail)
    f = VarFeed(v, ms, 2.0, clock=ck, hot_interval_s=1.0, min_gap_s=0.25)
    for m in ms:
        f.set_ref_price(m.var_asset, 1.0)
    f.hot_fn = lambda a: a in hot
    return f, v, ck


async def _run_for(f, ck, seconds):
    stop = asyncio.Event()

    async def fake_sleep(s):
        ck.t += max(s, 1e-3)
        if ck.t >= seconds:
            stop.set()
    f._sleep = fake_sleep
    await f.run(stop)


def _rate(v, asset=None, t0=4.0, t1=24.0):
    n = sum(1 for t, a in v.calls if t0 <= t < t1 and (asset is None or a == asset))
    return n / (t1 - t0)


async def test_rotation_normal_rate_unchanged_each_market_every_interval():
    f, v, ck = _mk_feed(disabled=("A2",))
    await _run_for(f, ck, 24.0)
    for a in ("A0", "A1", "A3", "A4", "A5"):
        assert _rate(v, a) == pytest.approx(0.5, abs=0.06)       # toutes les 2 s
    assert _rate(v, "A2") == 0
    assert _rate(v) == pytest.approx(2.5, abs=0.2)


async def test_hot_market_quoted_at_least_every_hot_interval():
    f, v, ck = _mk_feed(disabled=("A2",), hot=("A5",))
    await _run_for(f, ck, 24.0)
    assert _rate(v, "A5") == pytest.approx(1.0, abs=0.1)
    ts = [t for t, a in v.calls if a == "A5" and t >= 4.0]
    assert max(b - a for a, b in zip(ts, ts[1:])) <= 1.0 + 0.26
    for a in ("A0", "A1", "A3", "A4"):
        assert _rate(v, a) == pytest.approx(0.5, abs=0.06)       # rotation au rythme habituel
    assert _rate(v) == pytest.approx(3.0, abs=0.2)


async def test_three_hot_markets_share_under_limiter_cap():
    f, v, ck = _mk_feed(disabled=("A2",), hot=("A3", "A4", "A5"))
    await _run_for(f, ck, 24.0)
    assert _rate(v) <= 4.0 + 1e-9
    gaps = [b[0] - a[0] for a, b in zip(v.calls, v.calls[1:])]
    assert min(gaps) >= 0.25 - 1e-9
    for a in ("A3", "A4", "A5"):
        assert _rate(v, a) >= 0.9
    for a in ("A0", "A1"):
        assert _rate(v, a) >= 0.25                                # jamais affamé


async def test_all_hot_never_exceeds_cap_and_no_cold_starvation():
    f, v, ck = _mk_feed(hot=("A0", "A1", "A2", "A3", "A4", "A5"))
    await _run_for(f, ck, 24.0)
    assert _rate(v) <= 4.0 + 1e-9
    for i in range(6):
        ts = [t for t, a in v.calls if a == f"A{i}" and t >= 4.0]
        assert max(b - a for a, b in zip(ts, ts[1:])) <= 2.0          # jamais plus vieux que la rotation


async def test_request_fresh_is_served_first():
    f, v, ck = _mk_feed()
    await _run_for(f, ck, 3.0)
    n = len(v.calls)
    f.request_fresh("A3")
    await _run_for(f, ck, 3.3)
    assert v.calls[n][1] == "A3"


async def test_request_fresh_deduplicated_and_ignores_disabled():
    f, v, ck = _mk_feed(disabled=("A1",))
    await _run_for(f, ck, 3.0)
    n = len(v.calls)
    f.request_fresh("A3"); f.request_fresh("A3"); f.request_fresh("A1"); f.request_fresh("ZZ")
    await _run_for(f, ck, 3.26)
    assert [a for _, a in v.calls[n:]].count("A3") == 1 and "A1" not in [a for _, a in v.calls[n:]]


async def test_refresh_failed_tracks_last_attempt():
    f, v, ck = _mk_feed(fail=("A0",))
    assert f.refresh_failed("A0") is False
    await f.poll_once("A0")
    assert f.refresh_failed("A0") is True
    await f.poll_once("A1")
    assert f.refresh_failed("A1") is False


async def test_strike_slows_hot_and_rotation():
    class Lim:
        min_interval_s = 0.25
        def quote_interval_mult(self): return 2.0
    f, v, ck = _mk_feed(disabled=("A2",), hot=("A5",))
    v.limiter = Lim()
    await _run_for(f, ck, 24.0)
    assert _rate(v, "A5") == pytest.approx(0.5, abs=0.06)
    assert _rate(v, "A0") == pytest.approx(0.25, abs=0.06)
