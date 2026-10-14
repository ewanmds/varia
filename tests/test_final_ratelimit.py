"""Vague finale : C1 (limite de débit HL), I2 (ordres non suivis), I4 (requote interrompu),
R1 (verrou des nonces HL), I3b/I9 côté HLClient."""
import threading
import time

import pytest

from arb.config import GlobalCfg, MarketCfg
from arb.engine import BLOCKED, CANCEL_ONLY_REASON, HedgeGate, MarketEngine, PAUSED, QUOTING
from arb.hl_client import HLClient, parse_cancel_status, parse_rate_limit
from arb.orchestrator import Orchestrator
from arb.pricing import VarQuote

M = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", fill_usd=1000, min_hedge_usd=20, entry_mode="maker")
Q = VarQuote(67.00, 67.10, 15, ts=0)


class HL:
    def __init__(self):
        self.placed, self.cancelled, self.modified = [], [], []
        self.oid = 100
        self.place_error = None
        self.modify_error = None
        self.cancel_ok = True

    async def place_alo(self, coin, is_buy, sz, px):
        if self.place_error:
            return {"oid": None, "filled_sz": 0.0, "avg_px": None, "error": self.place_error}
        self.oid += 1
        self.placed.append((is_buy, sz, px, self.oid))
        return {"oid": self.oid, "filled_sz": 0.0, "avg_px": None, "error": None}

    async def modify(self, coin, oid, is_buy, sz, px):
        if self.modify_error:
            return {"oid": None, "error": self.modify_error}
        self.oid += 1
        self.modified.append((oid, is_buy, sz, px, self.oid))
        return {"oid": self.oid, "filled_sz": 0.0, "avg_px": None, "error": None}

    async def cancel(self, coin, oid):
        self.cancelled.append(oid)
        return self.cancel_ok


class J:
    def __init__(self):
        self.arbs, self.incidents = [], []

    def record_arb(self, *a):
        self.arbs.append(a)

    def record_incident(self, *a):
        self.incidents.append(a)


def mk(hl=None, g=None, m=M):
    e = MarketEngine(m, g or GlobalCfg(), hl or HL(), None, J(), HedgeGate(), sz_decimals=2, fee_bps=0.3)
    t = {"v": 1000.0}
    e._mono = lambda: t["v"]
    return e, t


def shifted(q, bps):
    f = 1 + bps / 1e4
    return VarQuote(q.bid * f, q.ask * f, q.qty, ts=0)


# -- C1a : repricing asymétrique ------------------------------------------------------------

async def test_c1a_passive_drift_below_threshold_not_repriced():
    e, t = mk()
    await e.requote(Q, 0.0, None, None, 1.0, None)
    assert len(e.hl.placed) == 2
    t["v"] += 60
    # Var monte de 2 pb : l'achat au repos devient plus passif de ~2 pb (< 3 pb) -> rien.
    await e.requote(shifted(Q, 2), 0.0, None, None, 1.0, None)
    assert e.hl.modified == [] and e.hl.cancelled == [] and len(e.hl.placed) == 2


async def test_c1a_passive_drift_waits_min_interval_then_modifies():
    e, t = mk()
    await e.requote(Q, 0.0, None, None, 1.0, None)
    buy_oid, sell_oid = e.orders["buy"]["oid"], e.orders["sell"]["oid"]
    t["v"] += 2   # < min_reprice_interval_s (5 s)
    await e.requote(shifted(Q, 5), 0.0, None, None, 1.0, None)
    # Var +5 pb : la vente au repos n'a plus que ~2 pb d'écart (< min_edge_bps) -> reposée tout
    # de suite ; l'achat, devenu plus passif de 5 pb, attend l'intervalle minimal.
    assert [m[0] for m in e.hl.modified] == [sell_oid]
    t["v"] += 4   # 6 s depuis le placement de l'achat
    await e.requote(shifted(Q, 5), 0.0, None, None, 1.0, None)
    assert [m[0] for m in e.hl.modified] == [sell_oid, buy_oid]
    assert e.hl.cancelled == [] and len(e.hl.placed) == 2      # modify, jamais cancel+place
    assert e.orders["buy"]["oid"] == e.hl.modified[1][4]
    assert e.hl.modified[1][4] in e._my_oids


async def test_c1a_too_aggressive_repriced_immediately():
    e, t = mk()
    await e.requote(Q, 0.0, None, None, 1.0, None)
    sell_oid = e.orders["sell"]["oid"]
    # Var monte de 20 pb, pas de délai écoulé : la vente au repos a maintenant un écart négatif
    # contre la cotation Var -> repositionnement immédiat.
    await e.requote(shifted(Q, 20), 0.0, None, None, 1.0, None)
    assert sell_oid in [m[0] for m in e.hl.modified]


async def test_c1a_reducing_side_at_target_does_not_churn():
    # position longue au max : le côté vente (réduction) vise exit_edge_bps (0) < min_edge_bps ;
    # posé à la cible, il ne doit pas être considéré "trop agressif" à chaque tick.
    e, t = mk()
    e.pos_hl = 3000 / 67.05
    await e.requote(Q, 0.0, None, None, 1.0, None)
    n = len(e.hl.placed)
    await e.requote(Q, 0.0, None, None, 1.0, None)
    await e.requote(Q, 0.0, None, None, 1.0, None)
    assert len(e.hl.placed) == n and e.hl.modified == [] and e.hl.cancelled == []


# -- C1b : modify puis repli cancel+place -------------------------------------------------

async def test_c1b_modify_failure_falls_back_to_cancel_and_place():
    hl = HL()
    e, t = mk(hl)
    await e.requote(Q, 0.0, None, None, 1.0, None)
    sell_oid = e.orders["sell"]["oid"]
    hl.modify_error = "Cannot modify canceled or filled order"
    await e.requote(shifted(Q, 20), 0.0, None, None, 1.0, None)
    assert sell_oid in hl.cancelled
    assert len(hl.placed) == 3 and e.orders["sell"]["oid"] == hl.placed[-1][3]


# -- C1c : backoff par côté ------------------------------------------------------------------

async def test_c1c_backoff_doubles_and_resets():
    hl = HL()
    hl.place_error = "Post only order would have immediately matched"
    e, t = mk(hl)
    await e.requote(Q, 0.0, None, None, 1.0, None)
    assert e._backoff_s == {"buy": 1.0, "sell": 1.0}
    t["v"] += 0.5
    await e.requote(Q, 0.0, None, None, 1.0, None)   # encore en backoff : aucune tentative
    assert e._backoff_s == {"buy": 1.0, "sell": 1.0}
    t["v"] += 0.6
    await e.requote(Q, 0.0, None, None, 1.0, None)   # nouvelle tentative, nouvel échec
    assert e._backoff_s == {"buy": 2.0, "sell": 2.0}
    for _ in range(10):
        t["v"] += 60
        await e.requote(Q, 0.0, None, None, 1.0, None)
    assert e._backoff_s["buy"] == 30.0
    hl.place_error = None
    t["v"] += 31
    await e.requote(Q, 0.0, None, None, 1.0, None)
    assert e.orders["buy"] and e.orders["sell"]
    assert e._backoff_s == {"buy": 0.0, "sell": 0.0}


async def test_c1c_place_exception_counts_as_error():
    class Boom(HL):
        async def place_alo(self, *a):
            raise RuntimeError("réseau")

    e, t = mk(Boom())
    await e.requote(Q, 0.0, None, None, 1.0, None)
    assert e._backoff_s["buy"] == 1.0 and e.orders["buy"] is None


# -- C1d : mode annulation seule -------------------------------------------------------------

async def test_c1d_cancel_only_no_place_no_modify_but_cancels_dangerous_order():
    hl = HL()
    e, t = mk(hl)
    await e.requote(Q, 0.0, None, None, 1.0, None)
    sell_oid = e.orders["sell"]["oid"]
    e.cancel_only_fn = lambda: True
    t["v"] += 60
    await e.requote(shifted(Q, 20), 0.0, None, None, 1.0, None)
    assert hl.modified == [] and len(hl.placed) == 2
    assert sell_oid in hl.cancelled and e.orders["sell"] is None   # trop agressive -> retirée
    assert e.orders["buy"] is not None                              # l'autre reste au repos
    assert e.last_reason == CANCEL_ONLY_REASON
    await e.requote(Q, 0.0, None, None, 1.0, None)
    assert len(hl.placed) == 2                                      # jamais de nouvel ordre


def test_parse_rate_limit_fields():
    rl = parse_rate_limit({"cumVlm": "1500.5", "nRequestsUsed": 9000, "nRequestsCap": 11500,
                           "nRequestsSurplus": 100})
    assert rl == {"used": 9000, "cap": 11500, "surplus": 100, "cum_vlm": 1500.5, "headroom": 2600}


class HLC:
    paper = True

    def __init__(self, headroom=50_000):
        self.headroom = headroom
        self.rate_calls = 0

    def load_meta(self):
        from arb.hl_client import CoinMeta
        return {"xyz:SILVER": CoinMeta(2, 25, True)}

    def maker_fee_bps(self, coin):
        return 0.3

    def mids(self):
        return {"xyz:SILVER": 67.05}

    def positions(self):
        return {}

    def positions_detail(self):
        return {}

    def margin_ratio(self):
        return 0.1

    def rate_limit(self):
        self.rate_calls += 1
        return {"used": 0, "cap": self.headroom, "surplus": 0, "cum_vlm": 0.0, "headroom": self.headroom}

    def set_cross_leverage(self, coin, lev):
        pass


class VC:
    def __init__(self):
        self.limiter = type("L", (), {"strikes_recent": lambda s: 0, "quotes_suspended": lambda s: False})()

    async def supported_assets(self):
        return {"XAGS": [{"price": "67.08", "trading_sessions": []}]}

    async def set_leverage(self, a, l):
        pass

    async def positions(self):
        return {}

    async def portfolio(self):
        return {"balance": 1.0}


class OJ(J):
    def daily_pnl(self, day=None):
        return 0.0


async def test_c1d_orchestrator_reads_rate_limit_at_setup_and_reconcile_and_toggles():
    h = HLC(headroom=50_000)
    o = Orchestrator(GlobalCfg(), [M], h, VC(), OJ(), clock=lambda: 0)
    await o.setup()
    assert h.rate_calls == 1 and o.cancel_only() is False
    assert o.engines["XAGS"].cancel_only_fn() is False
    h.headroom = 1500
    await o.reconcile()
    assert h.rate_calls == 2
    assert o.cancel_only() is True and o.engines["XAGS"].cancel_only_fn() is True
    st = o.status()
    assert st["hl_cancel_only"] is True and st["hl_rate"]["headroom"] == 1500
    assert any("annulation seule" in i[2] for i in o.journal.incidents)
    h.headroom = 5000
    await o.reconcile()
    assert o.cancel_only() is False


# -- I2 : annulation non confirmée ------------------------------------------------------------

async def test_i2_unconfirmed_cancel_keeps_order_and_retries():
    hl = HL()
    e, t = mk(hl)
    await e.requote(Q, 0.0, None, None, 1.0, None)
    hl.cancel_ok = False
    await e.pause("test")
    assert e.state == PAUSED
    assert e.orders["buy"] is not None and e.orders["sell"] is not None
    assert e._cancel_failed == {"buy", "sell"}
    n_inc = len(e.journal.incidents)
    t["v"] += 60                               # résiduel N1 : nouvel essai après le backoff
    await e.retry_pending_cancels()            # toujours refusé : pas de nouvel incident
    assert len(e.journal.incidents) == n_inc
    hl.cancel_ok = True
    t["v"] += 60
    await e.retry_pending_cancels()
    assert e.orders == {"buy": None, "sell": None} and e._cancel_failed == set()


async def test_i2_orchestrator_tick_retries_pending_cancels_even_when_paused():
    from tests.test_orchestrator import FakeHLFeed, HLC as OHLC, VC as OVC, J as OJ2
    o = Orchestrator(GlobalCfg(), [M], OHLC(), OVC(), OJ2(), clock=lambda: 0)
    await o.setup()
    eng = o.engines["XAGS"]
    eng.state = PAUSED
    eng.orders["buy"] = {"oid": 7, "px": 66.0, "sz": 1.0}
    eng._cancel_failed.add("buy")
    o.hl_feed = FakeHLFeed()
    await o.tick()
    assert eng.orders["buy"] is None


def _client(exchange=None, post=None):
    c = HLClient.__new__(HLClient)
    c.account, c.paper, c.exchange, c.meta = "0xabc", False, exchange, {}
    c._user_add_rate = c._user_cross_rate = None
    c._ex_lock = threading.Lock()
    if post is not None:
        c._post = post
    return c


def test_parse_cancel_status():
    ok = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
    gone = {"status": "ok", "response": {"data": {"statuses": [
        {"error": "Order was never placed, already canceled, or filled. asset=110000"}]}}}
    other = {"status": "ok", "response": {"data": {"statuses": [{"error": "Too many requests"}]}}}
    assert parse_cancel_status(ok) and parse_cancel_status(gone)
    assert not parse_cancel_status(other)
    assert not parse_cancel_status({"status": "err", "response": "rate limited"})


def test_i2_hl_cancel_all_raises_when_orders_remain():
    class Ex:
        def __init__(self):
            self.cancelled = []

        def cancel(self, coin, oid):
            self.cancelled.append(oid)
            return {"status": "ok"}

    reads = {"n": 0}

    def post(payload):
        assert payload["type"] == "frontendOpenOrders"
        reads["n"] += 1
        return [{"coin": "xyz:SILVER", "oid": 5}, {"coin": "xyz:GOLD", "oid": 6}, {"coin": "other", "oid": 7}]

    ex = Ex()
    c = _client(ex, post)
    with pytest.raises(RuntimeError):
        c.cancel_all(["xyz:SILVER", "xyz:GOLD"])
    assert ex.cancelled == [5, 6] and reads["n"] == 2

    c2 = _client(Ex(), lambda p: [{"coin": "other", "oid": 7}])
    c2.cancel_all(["xyz:SILVER"])    # rien ne reste sur nos coins : pas d'erreur


# -- I4 : requote interrompu ---------------------------------------------------------------

async def test_i4_state_change_during_place_cancels_new_order():
    class Slow(HL):
        def __init__(self):
            super().__init__()
            self.engine = None

        async def place_alo(self, coin, is_buy, sz, px):
            res = await super().place_alo(coin, is_buy, sz, px)
            self.engine.state = BLOCKED       # un blocage arrive pendant le placement
            return res

    hl = Slow()
    e, t = mk(hl)
    hl.engine = e
    await e.requote(Q, 0.0, None, None, 1.0, None)
    assert len(hl.placed) == 1                # le second côté n'est jamais posé
    assert hl.cancelled == [hl.placed[0][3]] and e.orders == {"buy": None, "sell": None}


async def test_i4_kill_during_place_cancels_new_order():
    killed = {"v": False}

    class Slow(HL):
        async def place_alo(self, coin, is_buy, sz, px):
            res = await super().place_alo(coin, is_buy, sz, px)
            killed["v"] = True
            return res

    hl = Slow()
    e, t = mk(hl)
    e.kill_fn = lambda: killed["v"]
    await e.requote(Q, 0.0, None, None, 1.0, None)
    assert hl.cancelled == [hl.placed[0][3]] and e.orders["buy"] is None and len(hl.placed) == 1


# -- R1 : une action d'échange à la fois --------------------------------------------------------

def test_r1_exchange_actions_are_serialized():
    state = {"in": 0, "max": 0}

    class Ex:
        def _act(self):
            state["in"] += 1
            state["max"] = max(state["max"], state["in"])
            time.sleep(0.02)
            state["in"] -= 1
            return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}

        def order(self, *a, **k):
            return self._act()

        def cancel(self, *a):
            return self._act()

        def modify_order(self, *a, **k):
            return self._act()

        def schedule_cancel(self, t):
            return self._act()

    c = _client(Ex())
    threads = [threading.Thread(target=f) for f in (
        lambda: c.place_alo("x", True, 1, 1), lambda: c.cancel("x", 1),
        lambda: c.modify_alo("x", 1, True, 1, 1), lambda: c.ioc("x", True, 1, 1),
        lambda: c.schedule_cancel(123))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert state["max"] == 1


# -- I3b / I9 / C1b côté HLClient ----------------------------------------------------------------

def test_hl_client_ioc_reduce_only_modify_and_schedule_cancel():
    calls = []

    class Ex:
        def order(self, name, is_buy, sz, px, order_type, reduce_only=False):
            calls.append(("order", order_type, reduce_only))
            return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 3, "totalSz": "1", "avgPx": "2"}}]}}}

        def modify_order(self, oid, name, is_buy, sz, px, order_type):
            calls.append(("modify", oid, order_type))
            return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 9}}]}}}

        def schedule_cancel(self, t):
            calls.append(("sched", t))
            return {"status": "err", "response": "Cannot set scheduled cancel time until enough volume traded"}

    c = _client(Ex())
    assert c.ioc("xyz:SILVER", False, 1.0, 2.0, reduce_only=True)["filled_sz"] == 1.0
    assert calls[0] == ("order", {"limit": {"tif": "Ioc"}}, True)
    assert c.modify_alo("xyz:SILVER", 5, True, 1.0, 2.0)["oid"] == 9
    assert calls[1] == ("modify", 5, {"limit": {"tif": "Alo"}})
    with pytest.raises(RuntimeError):
        c.schedule_cancel(1_000)
    assert c.rate_limit.__doc__


def test_hl_client_rate_limit_reads_user_rate_limit():
    seen = []

    def post(p):
        seen.append(p)
        return {"cumVlm": "0.0", "nRequestsUsed": 10, "nRequestsCap": 10000, "nRequestsSurplus": 0}

    c = _client(None, post)
    assert c.rate_limit()["headroom"] == 9990
    assert seen == [{"type": "userRateLimit", "user": "0xabc"}]
