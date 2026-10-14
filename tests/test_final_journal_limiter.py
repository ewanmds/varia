"""Vague finale : C2 (journal et couverture), I1 (deadline du limiteur), I6 (strikes Var)."""
import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest

from arb.config import GlobalCfg, MarketCfg
from arb.engine import HedgeGate, MarketEngine, PAUSED, QUOTING
from arb.journal import Journal, SafeJournal
from arb.pricing import VarQuote
from arb.var_client import VarClient, VarRateLimited
from arb.var_feed import VarFeed
from arb.var_limiter import EXIT, HEDGE, QUOTE, QuotesSuspended, VarLimiter, next_midnight_utc

M = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", fill_usd=1000, min_hedge_usd=20, entry_mode="maker")
Q = VarQuote(67.00, 67.10, 15, ts=0)


class HL:
    def __init__(self):
        self.oid, self.iocs = 100, []

    async def place_alo(self, coin, is_buy, sz, px):
        self.oid += 1
        return {"oid": self.oid, "filled_sz": 0.0, "avg_px": None, "error": None}

    async def cancel(self, coin, oid):
        return True

    async def ioc(self, coin, is_buy, sz, px, reduce_only=False):
        self.iocs.append((coin, is_buy, sz, px))
        return {"oid": 1, "filled_sz": sz, "avg_px": px, "error": None}


class Var:
    def __init__(self):
        self.market_calls = 0

    async def quote(self, asset, kind, qty, prio=2, **kw):
        return VarQuote(67.05, 67.10, qty, ts=0, quote_id="q")

    async def market(self, *a, **kw):
        self.market_calls += 1
        return "r1"

    async def order_status(self, rfq, prio=3, **kw):
        return {"status": "cleared", "price": "67.05"}

    async def positions(self, **kw):
        return {}


class BrokenJournal:
    def __init__(self):
        self.incidents = []

    def record_arb(self, *a):
        raise RuntimeError("disque plein")

    def record_incident(self, *a):
        raise RuntimeError("disque plein")


def fill(oid, sz=15.0):
    return {"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": sz, "oid": oid, "fee": 0, "tid": 1, "time": 1}


# -- C2 -----------------------------------------------------------------------------------------

async def test_c2_hedge_success_state_updated_even_if_journal_fails(capsys):
    e = MarketEngine(M, GlobalCfg(hedge_retry_s=1.0), HL(), Var(), BrokenJournal(), HedgeGate(), 2, 0.3)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill(fill(e.orders["buy"]["oid"]))
    assert e.pos_hl == 15.0 and e.pos_var == -15.0 and e.pending_qty == 0
    assert e.state == QUOTING and e.hedge_inflight is False
    assert "journal" in capsys.readouterr().err


async def test_c2_unwind_state_updated_even_if_journal_fails():
    class FailVar(Var):
        async def quote(self, *a, **k):
            raise RuntimeError("playwright")

    e = MarketEngine(M, GlobalCfg(hedge_retry_s=0.3), HL(), FailVar(), BrokenJournal(), HedgeGate(), 2, 0.3)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill(fill(e.orders["buy"]["oid"]))
    assert e.hl.iocs and e.pos_hl == 0 and e.pending_qty == 0
    assert e.state == PAUSED


async def test_c2_journal_written_after_state_update():
    seen = {}

    class SpyJournal:
        def __init__(self):
            self.engine = None

        def record_arb(self, *a):
            seen["pos_var"], seen["pending"], seen["state"] = self.engine.pos_var, self.engine.pending_qty, self.engine.state

        def record_incident(self, *a):
            pass

    j = SpyJournal()
    e = MarketEngine(M, GlobalCfg(hedge_retry_s=1.0), HL(), Var(), j, HedgeGate(), 2, 0.3)
    j.engine = e
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill(fill(e.orders["buy"]["oid"]))
    assert seen == {"pos_var": -15.0, "pending": 0.0, "state": QUOTING}


def test_c2_safe_journal_delegates_reads():
    class Inner:
        arbs = [1]

        def daily_pnl(self):
            return -3.0

    sj = SafeJournal(Inner())
    assert sj.daily_pnl() == -3.0 and sj.arbs == [1]
    assert SafeJournal.wrap(sj) is sj


def test_c2_journal_thread_safe(tmp_path):
    j = Journal(tmp_path / "j.db")
    errors = []

    def writer(k):
        try:
            for i in range(60):
                j.record_arb("XAGS", "B", 1, 1, 0, "sell", 1, 1, 0, 0.01, 0, "hedged")
                j.record_incident("XAGS", "warn", f"{k}-{i}")
                j.recent_incidents(5)
                j.daily_pnl()
        except Exception as exc:
            errors.append(exc)

    ths = [threading.Thread(target=writer, args=(k,)) for k in range(6)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert errors == []
    assert len(j.recent_arbs(1000)) == 360
    assert isinstance(j._lock, type(threading.RLock()))


def test_c2_server_read_endpoints_are_async():
    from arb.server import create_app

    class Ctx:
        orch = None
        journal = None
        auth = None
        paper = True

    app = create_app(Ctx())
    eps = {r.path: r.endpoint for r in app.routes if hasattr(r, "endpoint")}
    for path in ("/api/status", "/api/market/{asset}/params", "/api/market/{asset}/toggle", "/api/bot/kill"):
        assert asyncio.iscoroutinefunction(eps[path]), path


# -- I1 -----------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def mk_lim(**kw):
    c = Clock()
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        c.t += s

    lim = VarLimiter(0.25, 600, 3, clock=c, sleep=fake_sleep, **kw)
    return lim, c, slept


async def test_i1_acquire_with_deadline_raises_without_sleeping():
    lim, c, slept = mk_lim()
    lim.record_limited(20)
    n = lim.strikes_recent()
    with pytest.raises(VarRateLimited) as ei:
        await lim.acquire(HEDGE, deadline=c.t + 5)
    assert ei.value.wait_s == pytest.approx(20)
    assert slept == [] and lim.strikes_recent() == n    # pas de sommeil, pas de strike
    await lim.acquire(HEDGE, deadline=c.t + 30)         # budget suffisant : attend normalement
    assert slept == [pytest.approx(20)]


async def test_i1_var_client_passes_deadline():
    class T:
        async def call(self, *a):
            raise AssertionError("aucune requête ne doit partir")

    lim = VarLimiter(0.25, 600, 3)
    c = VarClient(T(), lim)
    lim.record_limited(60)
    t0 = time.monotonic()
    with pytest.raises(VarRateLimited):
        await c.quote("XAGS", "commodity", 1.0, prio=HEDGE, deadline=time.monotonic() + 5)
    with pytest.raises(VarRateLimited):
        await c.market("q", "sell", 0.001, prio=HEDGE, deadline=time.monotonic() + 5)
    assert time.monotonic() - t0 < 0.5


async def test_i1_hedge_unwinds_immediately_when_var_blocked_beyond_budget():
    class BlockedVar(Var):
        async def quote(self, asset, kind, qty, prio=2, deadline=None, **kw):
            assert deadline is not None     # hedge() passe son budget
            raise VarRateLimited(60.0)

    e = MarketEngine(M, GlobalCfg(hedge_retry_s=8.0), HL(), BlockedVar(), BrokenJournal(), HedgeGate(), 2, 0.3)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    t0 = time.monotonic()
    await e.on_fill(fill(e.orders["buy"]["oid"]))
    assert time.monotonic() - t0 < 1.0               # pas d'attente des 8 s de budget
    assert e.hl.iocs and e.pos_hl == 0 and e.state == PAUSED
    assert e.var.market_calls == 0


# -- I6 -----------------------------------------------------------------------------------------

def test_i6_journal_strikes_roundtrip(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.record_strike(100.0)
    j.record_strike(200.0)
    assert j.strikes_since(150.0) == [200.0]


async def test_i6_five_strikes_in_24h_suspend_quotes_until_midnight_utc():
    wall = {"t": datetime(2026, 9, 22, 10, tzinfo=timezone.utc).timestamp()}
    persisted = []
    lim, c, slept = mk_lim(wall=lambda: wall["t"])
    lim.on_strike = persisted.append
    for _ in range(5):
        lim.record_limited(0)
        c.t += 700          # hors de la fenêtre de 10 min : seule la règle 24 h joue
        wall["t"] += 3600
    assert lim.strikes_recent() == 0 and lim.strikes_24h() == 5
    assert lim.quotes_suspended()
    assert len(persisted) == 5
    with pytest.raises(QuotesSuspended):
        await lim.acquire(QUOTE)
    await lim.acquire(HEDGE)            # couvertures et sorties toujours permises
    await lim.acquire(EXIT)
    wall["t"] = datetime(2026, 9, 23, 0, 0, 1, tzinfo=timezone.utc).timestamp()
    assert not lim.quotes_suspended()


def test_i6_next_midnight():
    ts = datetime(2026, 9, 22, 23, 59, tzinfo=timezone.utc).timestamp()
    assert next_midnight_utc(ts) == datetime(2026, 9, 23, tzinfo=timezone.utc).timestamp()


def test_i6_load_strikes_restores_state():
    now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp()
    lim, c, slept = mk_lim(wall=lambda: now)
    lim.load_strikes([now - 90000, now - 7200, now - 300, now - 200, now - 100])
    assert lim.strikes_24h() == 4           # le strike de plus de 24 h est ignoré
    assert lim.strikes_recent() == 3        # 3 dans les 10 dernières minutes
    assert lim.quotes_suspended()           # règle 3 strikes / 10 min


def test_i6_quote_interval_doubles_for_one_hour_after_strike():
    wall = {"t": 1_000_000.0}
    lim, c, slept = mk_lim(wall=lambda: wall["t"])

    class Client:
        pass

    cl = Client()
    cl.limiter = lim
    feed = VarFeed(cl, [M], 2.0)
    assert feed.step_s() == 2.0
    lim.record_limited(1)
    assert lim.quote_interval_mult() == 2.0 and feed.step_s() == 4.0
    wall["t"] += 3601
    assert feed.step_s() == 2.0


async def test_i6_orchestrator_reloads_and_persists_strikes(tmp_path):
    from arb.orchestrator import Orchestrator
    from tests.test_orchestrator import HLC, VC

    j = Journal(tmp_path / "j.db")
    now = time.time()
    for k in range(5):
        j.record_strike(now - 60 * (k + 1))
    v = VC()
    v.limiter = VarLimiter(0.25, 600, 3)
    o = Orchestrator(GlobalCfg(), [M], HLC(), v, j, clock=lambda: 0)
    await o.setup()
    assert v.limiter.strikes_24h() == 5 and v.limiter.quotes_suspended()
    assert o.status()["strikes_24h"] == 5
    v.limiter.record_limited(1)
    assert len(j.strikes_since(now - 86400)) == 6


async def test_i6_order_status_polled_every_second():
    stamps = []

    class SlowStatus(Var):
        async def order_status(self, rfq, prio=3, **kw):
            stamps.append(time.monotonic())
            if len(stamps) < 3:
                return {"status": "pending"}
            return {"status": "cleared", "price": "67.05"}

    e = MarketEngine(M, GlobalCfg(hedge_retry_s=5.0), HL(), SlowStatus(), BrokenJournal(), HedgeGate(), 2, 0.3)
    e._confirm_window_s = 100.0
    assert e._status_poll_s == 1.0
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill(fill(e.orders["buy"]["oid"]))
    assert len(stamps) == 3 and e.pos_var == -15.0
    assert all(b - a >= 0.95 for a, b in zip(stamps, stamps[1:]))


async def test_i6_health_rate_limited_keeps_previous_state():
    class T:
        def __init__(self, responses):
            self.responses = list(responses)

        async def call(self, *a):
            return self.responses.pop(0)

    async def nosleep(s):
        pass

    c = VarClient(T([(429, {"wait_until_seconds": 0}), (401, {}), (418, {"wait_until_seconds": 0}),
                     (200, {"token": "x"})]), VarLimiter(0, 600, 3, sleep=nosleep))
    assert await c.health() is True       # 429 : pas une perte de session
    assert await c.health() is False      # 401 : session perdue
    assert await c.health() is False      # 418 : garde l'état précédent (perdue)
    assert await c.health() is True
