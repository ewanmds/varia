"""Vague finale : sorties sûres (I3), kill (I4), démarrage (I5), PnL complet (I8),
dead-man switch (I9), réduction forcée (S2)."""
import asyncio
import time
from datetime import datetime, timezone

import pytest

from arb.config import GlobalCfg, MarketCfg
from arb.engine import BLOCKED, EXITING, HedgeGate, IDLE, MarketEngine, QUOTING
from arb.journal import Journal
from arb.orchestrator import Orchestrator, STARTUP_DELTA_REASON
from arb.pricing import VarQuote
from tests.test_orchestrator import HLC, HLC2, J, M, M2, VC, VC2, FakeHLFeed

MK = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", fill_usd=1000, min_hedge_usd=20, entry_mode="maker")
NOON = datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp()


# -- faux pour le moteur ------------------------------------------------------------------------

class EHL:
    def __init__(self, pos=None, entry=None, fill_ratio=1.0):
        self.pos = pos or {}
        self.entry = entry
        self.fill_ratio = fill_ratio
        self.iocs, self.calls = [], []
        self.fail_read = False

    async def positions_detail(self):
        if self.fail_read:
            raise RuntimeError("HL indisponible")
        return {c: {"szi": v, "entry": self.entry} for c, v in self.pos.items()}

    async def cancel(self, coin, oid):
        return True

    async def ioc(self, coin, is_buy, sz, px, reduce_only=False):
        self.calls.append("hl_ioc")
        self.iocs.append({"is_buy": is_buy, "sz": sz, "px": px, "reduce_only": reduce_only})
        f = sz * self.fill_ratio
        return {"oid": 1, "filled_sz": f, "avg_px": px, "error": None}


class EVar:
    def __init__(self, qty=0.0, entry=None, status="cleared", calls=None):
        self.qty, self.entry, self.status = qty, entry, status
        self.calls = calls if calls is not None else []
        self.orders = []

    async def quote(self, asset, kind, qty, prio=2, **kw):
        return VarQuote(67.00, 67.10, qty, ts=0, quote_id="q")

    async def market(self, quote_id, side, max_slippage, reduce_only=False, prio=0, **kw):
        self.calls.append("var_market")
        self.orders.append((side, reduce_only))
        return "r1"

    async def order_status(self, rfq, prio=3, **kw):
        if self.status == "cleared":
            return {"status": "cleared", "price": "67.08"}
        return {"status": self.status}

    async def positions(self, **kw):
        return {"XAGS": {"qty": self.qty, "entry": self.entry}} if self.qty else {}


class EJ:
    def __init__(self):
        self.arbs, self.incidents = [], []

    def record_arb(self, *a):
        self.arbs.append(a)

    def record_incident(self, *a):
        self.incidents.append(a)


def eng(hl, var, taker_fee=0.0):
    e = MarketEngine(MK, GlobalCfg(), hl, var, EJ(), HedgeGate(), 2, 0.3, taker_fee_bps=taker_fee)
    e._confirm_window_s = 0.2
    e._status_poll_s = 0.05
    return e


# -- I3a ----------------------------------------------------------------------------------------

async def test_i3a_flatten_sizes_on_reread_positions_not_cache():
    hl = EHL(pos={"xyz:SILVER": 10.0})
    var = EVar(qty=-10.0)
    e = eng(hl, var)
    e.pos_hl, e.pos_var = 99.0, -99.0          # cache périmé
    await e.flatten(True, 66.9, 67.2)
    assert hl.iocs[0]["sz"] == 10.0
    assert e.pos_hl == 0 and e.pos_var == 0


async def test_i3a_flatten_postponed_when_positions_unreadable():
    hl = EHL(pos={"xyz:SILVER": 10.0})
    hl.fail_read = True
    e = eng(hl, EVar(qty=-10.0))
    await e.flatten(True, 66.9, 67.2)
    assert hl.iocs == [] and e.var.orders == [] and e.state == EXITING
    assert any("sortie reportée" in i[2] for i in e.journal.incidents)


# -- I3b / I3c ----------------------------------------------------------------------------------

async def test_i3bc_var_closed_first_then_hl_reduce_only():
    calls = []
    hl = EHL(pos={"xyz:SILVER": 15.0})
    hl.calls = calls
    var = EVar(qty=-15.0, calls=calls)
    e = eng(hl, var)
    await e.flatten(True, 66.9, 67.2)
    assert calls == ["var_market", "hl_ioc"]
    assert var.orders == [("buy", True)]
    assert hl.iocs[0]["reduce_only"] is True and hl.iocs[0]["is_buy"] is False


async def test_i3c_var_unconfirmed_hl_closes_only_uncovered_excess():
    hl = EHL(pos={"xyz:SILVER": 20.0})
    var = EVar(qty=-15.0, status="pending")      # sortie Var jamais confirmée, position inchangée
    e = eng(hl, var)
    await e.flatten(True, 66.9, 67.2)
    assert [o["sz"] for o in hl.iocs] == [5.0]    # seul l'excédent HL non couvert
    assert e.pos_hl == 15.0 and e.pos_var == -15.0
    assert any("sortie Var non confirmée" in i[2] for i in e.journal.incidents)


async def test_i3c_empty_hl_book_uses_var_mid_and_logs_incident():
    hl = EHL(pos={"xyz:SILVER": 15.0})
    e = eng(hl, EVar(qty=-15.0))
    await e.flatten(True, None, None, var_mid=67.05)
    assert hl.iocs and hl.iocs[0]["px"] == pytest.approx(67.05 * 0.995, rel=1e-3)
    assert any("carnet HL vide" in i[2] for i in e.journal.incidents)


async def test_i3b_hedge_unwind_is_not_reduce_only():
    # le débouclage d'une couverture ratée peut rouvrir de l'inventaire (fill qui réduisait une
    # position existante) : il ne doit donc pas être en reduce-only.
    class FailVar(EVar):
        async def quote(self, *a, **k):
            raise RuntimeError("down")

    hl = EHL()

    async def place_alo(coin, is_buy, sz, px):
        return {"oid": 7, "filled_sz": 0.0, "avg_px": None, "error": None}

    hl.place_alo = place_alo
    e = MarketEngine(MK, GlobalCfg(hedge_retry_s=0.3), hl, FailVar(), EJ(), HedgeGate(), 2, 0.3)
    await e.requote(VarQuote(67.0, 67.1, 15, ts=0), 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0, "oid": 7, "fee": 0,
                     "tid": 1, "time": 1})
    assert hl.iocs and hl.iocs[0]["reduce_only"] is False


# -- I8 -----------------------------------------------------------------------------------------

async def test_i8_flatten_records_exit_with_realized_pnl():
    hl = EHL(pos={"xyz:SILVER": 15.0})
    e = eng(hl, EVar(qty=-15.0), taker_fee=1.0)
    await e.flatten(True, 67.0, 67.2)
    row = e.journal.arbs[-1]
    assert row[-1] == "exit"
    hl_px = hl.iocs[0]["px"]
    expected = (hl_px - 67.08) * 15.0 - 1.0 / 1e4 * 15.0 * hl_px
    assert row[9] == pytest.approx(expected)


async def test_i8_exit_and_correction_count_in_daily_pnl(tmp_path):
    j = Journal(tmp_path / "j.db")
    hl = EHL(pos={"xyz:SILVER": 15.0})
    e = MarketEngine(MK, GlobalCfg(), hl, EVar(qty=-15.0), j, HedgeGate(), 2, 0.3, taker_fee_bps=0.0)
    e._confirm_window_s, e._status_poll_s = 0.2, 0.05
    await e.flatten(True, 67.0, 67.2)
    rows = j.recent_arbs()
    assert rows[0]["status"] == "exit"
    assert j.daily_pnl() == pytest.approx(rows[0]["pnl_usd"]) and j.daily_pnl() < 0


async def test_i8_i3b_i3e_correction_reduce_only_journaled_and_positions_reread():
    class HLCE(HLC):
        def positions_detail(self):
            return {c: {"szi": v, "entry": 67.5} for c, v in self.pos.items()}

        def ioc(self, coin, is_buy, sz, px, reduce_only=False):
            res = super().ioc(coin, is_buy, sz, px, reduce_only=reduce_only)
            self.pos = {"xyz:SILVER": 15.0}       # la correction a ramené HL à 15
            return res

    h = HLCE()
    h.pos = {"xyz:SILVER": 20.0}
    v = VC()
    v.pos = {"XAGS": {"qty": -15.0, "entry": 67, "liq": None, "value": -1005}}
    o = Orchestrator(GlobalCfg(), [M], h, v, J(), clock=lambda: 0)
    await o.setup()
    eng_ = o.engines["XAGS"]
    eng_.resume()
    eng_.book = (66.9, 67.2)
    o.hl_feed = FakeHLFeed(book=(66.9, 67.2), mid=67.0)
    await o.reconcile()
    await o.reconcile()
    assert h.reduce_only == [True]
    assert eng_.pos_hl == 15.0 and eng_.pos_var == -15.0


async def test_i8_correction_row_recorded():
    rows = []

    class JR(J):
        def record_arb(self, *a):
            rows.append(a)
            return 1

    h = HLC()
    h.pos = {"xyz:SILVER": 20.0}
    v = VC()
    v.pos = {"XAGS": {"qty": -15.0, "entry": 67, "liq": None, "value": -1005}}
    o = Orchestrator(GlobalCfg(), [M], h, v, JR(), clock=lambda: 0)
    await o.setup()
    o.engines["XAGS"].resume()
    o.engines["XAGS"].book = (66.9, 67.2)
    o.hl_feed = FakeHLFeed(book=(66.9, 67.2), mid=67.0)
    await o.reconcile()
    await o.reconcile()
    assert rows and rows[-1][-1] == "correction"


# -- I3d ----------------------------------------------------------------------------------------

class VCX(VC):
    """VC avec order_status (sortie Var confirmée) et positions mutables."""

    async def order_status(self, rfq, prio=3, **kw):
        return {"status": "cleared", "price": "67.05"}


async def test_i3d_partial_exit_after_kill_retried_by_tick():
    class HLCPartial(HLC):
        def ioc(self, coin, is_buy, sz, px, reduce_only=False):
            self.iocs.append((coin, is_buy, sz, px))
            half = round(sz / 2, 2)
            self.pos = {coin: self.pos.get(coin, 0) - half}
            return {"oid": 2, "filled_sz": half, "avg_px": px, "error": None}

    h = HLCPartial()
    h.pos = {"xyz:SILVER": 4.0}
    v = VCX()
    o = Orchestrator(GlobalCfg(), [M], h, v, J(), clock=lambda: 0)
    await o.setup()
    o.hl_feed = FakeHLFeed()
    await o.kill("test")
    e = o.engines["XAGS"]
    assert len(h.iocs) == 1 and e.pos_hl == 2.0 and e.state == IDLE
    await o.tick()                           # backoff pas écoulé : pas de nouvel essai
    assert len(h.iocs) == 1
    o._next_taker_ts["XAGS"] = 0.0
    # L3 (23/09) : pas de nouvel essai de sortie avant une relecture des positions.
    await o.tick()
    assert len(h.iocs) == 1
    await o.reconcile()
    await o.tick()
    assert len(h.iocs) == 2 and e.pos_hl == 1.0 and e.state == IDLE


async def test_i3d_open_position_at_close_incident_once_per_session():
    t = {"v": datetime(2026, 9, 22, 21, 1, tzinfo=timezone.utc).timestamp()}   # CLOSED
    o = Orchestrator(GlobalCfg(), [M], HLC(), VC(), J(), clock=lambda: t["v"])
    await o.setup()
    e = o.engines["XAGS"]
    e.pos_hl, e.pos_var = 3.0, -3.0
    o.hl_feed = FakeHLFeed()
    await o.tick()
    await o.tick()
    msgs = [i for i in o.journal.inc if "position ouverte à la fermeture" in i[2]]
    assert len(msgs) == 1


# -- I4 -----------------------------------------------------------------------------------------

async def test_i4_kill_waits_for_running_tick_then_final_cancel_all():
    order = []
    h = HLC()
    o = Orchestrator(GlobalCfg(), [M], h, VCX(), J(), clock=lambda: NOON)
    await o.setup()
    e = o.engines["XAGS"]
    o.hl_feed = FakeHLFeed(mid=67.05)
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 15, ts=time.time(), quote_id="q")
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def slow_requote(*a, **k):
        entered.set()
        await gate.wait()
        order.append("tick_done")

    e.requote = slow_requote
    orig = h.cancel_all

    def cancel_all(coins):
        order.append("cancel_all")
        return orig(coins)

    h.cancel_all = cancel_all
    t = asyncio.create_task(o.tick())
    await entered.wait()
    k = asyncio.create_task(o.kill("test"))
    await asyncio.sleep(0.05)
    assert "tick_done" not in order
    gate.set()
    await asyncio.gather(t, k)
    assert order.index("tick_done") < len(order) - 1
    assert order[-1] == "cancel_all"


async def test_i4_kill_from_inside_tick_does_not_deadlock():
    class JLoss(J):
        def daily_pnl(self, day=None):
            return -1000.0

    o = Orchestrator(GlobalCfg(), [M], HLC(), VCX(), JLoss(), clock=lambda: NOON)
    await o.setup()
    o.hl_feed = FakeHLFeed()
    await asyncio.wait_for(o.tick(), timeout=5)
    assert o.killed and o.kill_reason == "perte journalière max"


# -- I5 -----------------------------------------------------------------------------------------

async def test_i5_startup_delta_blocks_market():
    h = HLC()
    h.pos = {"xyz:SILVER": 5.0}          # 5 x 67 = 335 $ >= 0,2 x 1000
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: 0)
    await o.setup()
    e = o.engines["XAGS"]
    assert e.state == BLOCKED and STARTUP_DELTA_REASON in e.last_reason


async def test_i5_small_startup_delta_not_blocked():
    h = HLC()
    h.pos = {"xyz:SILVER": 2.0}          # 134 $ < 200 $
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: 0)
    await o.setup()
    assert o.engines["XAGS"].state == QUOTING


# -- I9 -----------------------------------------------------------------------------------------

async def test_i9_deadman_rearmed_every_60s_with_120s_horizon():
    # résiduel N2/N3 : cadence 60 s, échéance 120 s, seulement avec un ordre au repos.
    h = HLC()
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: 0)
    await o.setup()
    o.hl_feed = FakeHLFeed()
    o.engines["XAGS"].orders["buy"] = {"oid": 5, "px": 66.0, "sz": 1.0}
    t0 = int(time.time() * 1000)
    await o.tick()
    await o.tick()
    assert len(h.schedule_calls) == 1
    assert 119_000 <= h.schedule_calls[0] - t0 <= 121_000
    assert o._next_deadman - time.monotonic() == pytest.approx(60.0, abs=1.0)
    o._next_deadman = 0.0
    o.engines["XAGS"].orders["buy"] = {"oid": 5, "px": 66.0, "sz": 1.0}
    await o.tick()
    assert len(h.schedule_calls) == 2


async def test_i9_deadman_refused_single_incident_and_bot_continues():
    from arb.hl_client import HLRefused

    class HLCRefuse(HLC):
        def schedule_cancel(self, when_ms):
            raise HLRefused("Cannot set scheduled cancel time until enough volume traded")

    o = Orchestrator(GlobalCfg(), [M], HLCRefuse(), VC(), J(), clock=lambda: 0)
    await o.setup()
    o.hl_feed = FakeHLFeed()
    o.engines["XAGS"].orders["buy"] = {"oid": 5, "px": 66.0, "sz": 1.0}
    for _ in range(3):
        o._next_deadman = 0.0
        o.engines["XAGS"].orders["buy"] = {"oid": 5, "px": 66.0, "sz": 1.0}
        await o.tick()
    msgs = [i for i in o.journal.inc if "scheduleCancel indisponible" in i[2]]
    assert len(msgs) == 1 and not o.killed
    assert o._next_deadman - time.monotonic() > 500     # pause de 10 min après un refus HL


async def test_i9_run_shutdown_disarms_deadman():
    from unittest.mock import patch

    class Feed:
        def start(self):
            pass

        def stop(self):
            pass

        def book(self, c):
            return (66.9, 67.2)

        def mid(self, c):
            return 67.05

        def fills_alive(self, max_age_s=30.0):
            return True

        def last_any_msg(self):
            return time.time()

        def book_age(self, c):
            return 0.0

    h = HLC()
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: 0)
    await o.setup()
    stop = asyncio.Event()

    async def driver():
        await asyncio.sleep(0.05)
        stop.set()

    with patch("arb.orchestrator.HLFeed", return_value=Feed()):
        await asyncio.gather(o.run(stop, "0xabc"), driver())
    assert h.schedule_calls[-1] is None


# -- S2 -----------------------------------------------------------------------------------------

async def test_s2_var_liquidation_close_forces_flatten_of_that_market():
    o = Orchestrator(GlobalCfg(), [M], HLC(), VCX(), J(), clock=lambda: NOON)
    await o.setup()
    e = o.engines["XAGS"]
    e.pos_hl, e.pos_var = 3.0, -3.0
    o.var_liq["XAGS"] = 0.02             # < var_liq_distance_min
    o.hl_feed = FakeHLFeed()
    calls = []

    async def fake_flatten(taker, b, a, var_mid=None):
        calls.append(taker)

    e.flatten = fake_flatten
    await o.tick()
    assert calls == [True]
    assert any("réduction forcée" in i[2] for i in o.journal.inc)
    await o.tick()                        # backoff : pas de seconde tentative immédiate
    assert calls == [True]


async def test_s2_hl_margin_tight_flattens_largest_market_only():
    o = Orchestrator(GlobalCfg(), [M, M2], HLC2(), VC2(), J(), clock=lambda: NOON)
    await o.setup()
    e1, e2 = o.engines["XAGS"], o.engines["GOLD"]
    e1.pos_hl, e1.pos_var = 3.0, -3.0         # ~200 $
    e2.pos_hl, e2.pos_var = 1.0, -1.0         # ~2000 $
    o.hl_margin = 0.9
    o.hl_feed = FakeHLFeed(mid={"xyz:SILVER": 67.05, "xyz:GOLD": 2000.5})
    calls = []

    def spy(name):
        async def f(taker, b, a, var_mid=None):
            calls.append(name)
        return f

    e1.flatten, e2.flatten = spy("XAGS"), spy("GOLD")
    await o.tick()
    assert calls == ["GOLD"]


async def test_s2_forced_reduction_keeps_paused_state():
    from arb.engine import PAUSED
    h = HLC()
    o = Orchestrator(GlobalCfg(), [M], h, VCX(), J(), clock=lambda: NOON)
    await o.setup()
    e = o.engines["XAGS"]
    h.pos = {"xyz:SILVER": 3.0}
    o.var.pos = {"XAGS": {"qty": -3.0, "entry": 67.0}}
    e.pos_hl, e.pos_var = 3.0, -3.0
    e.state = PAUSED
    o.var_liq["XAGS"] = 0.02
    o.hl_feed = FakeHLFeed()
    await o.tick()
    assert h.iocs and e.state == PAUSED
