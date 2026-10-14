"""L2 (prix Var temps réel par websocket) et L3 (basis rapide des pétroles),
docs/superpowers/levers-2026-09-23.md."""
import asyncio
import dataclasses
import time
from datetime import datetime, timezone
from unittest.mock import patch

from arb.config import GlobalCfg, MarketCfg
from arb.engine import HedgeGate, MarketEngine
from arb.orchestrator import Orchestrator
from arb.pricing import VarQuote, ws_reference
from arb.risk import STALE_QUOTE_REASON
from tests.test_chase import HL, J as EJ, Var
from tests.test_orchestrator import HLC, VC, J, FakeHLFeed

NOON = datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp()
CHASE = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", entry_mode="chase")
OIL = dataclasses.replace(CHASE, basis_mode="ema")
MAKER = dataclasses.replace(CHASE, entry_mode="maker")


def ws_upd(mark, recv_s):
    return {"bid": None, "ask": None, "mark": mark, "underlying": mark,
            "ts_recv_ms": int(recv_s * 1000), "ts_server_ms": None}


async def mk_orch(m=CHASE, g=None, feed=None):
    o = Orchestrator(g or GlobalCfg(), [dataclasses.replace(m)], HLC(), VC(), J(), clock=lambda: NOON)
    await o.setup()
    o.hl_feed = feed or FakeHLFeed(book=(66.9, 67.2), mid=67.05)
    eng = o.engines["XAGS"]
    calls = []

    async def requote(q, basis, hl_bid, hl_ask, wide, reason, hold=False, rfq=None, lag=None):
        calls.append({"q": q, "basis": basis, "reason": reason, "rfq": rfq, "lag": lag})

    eng.requote = requote
    return o, eng, calls


# -- L2 : référence Var --------------------------------------------------------------------------

def test_ws_reference_uses_rfq_relative_half_spread():
    rfq = VarQuote(67.0, 67.1, 15, ts=10.0, quote_id="q")
    ref = ws_reference(rfq, 67.2, 12.5)
    hs = 0.05 / 67.05
    assert abs(ref.bid - 67.2 * (1 - hs)) < 1e-12 and abs(ref.ask - 67.2 * (1 + hs)) < 1e-12
    assert abs(ref.mid - 67.2) < 1e-12
    assert ref.ts == 12.5 and ref.qty == 15 and ref.quote_id == "ws"


async def test_chase_reference_is_ws_price_when_fresh():
    o, eng, calls = await mk_orch()
    now = time.time()
    rfq = VarQuote(67.0, 67.1, 15, ts=now - 1.0, quote_id="q1")
    o.feed._quotes["XAGS"] = rfq
    o._on_var_ws("XAGS", ws_upd(67.2, now - 0.3))
    await o.tick()
    c = calls[-1]
    assert c["q"].quote_id == "ws" and abs(c["q"].mid - 67.2) < 1e-9
    assert c["rfq"] is rfq
    assert o.status()["markets"][0]["var_ref"] == "ws"


async def test_reference_falls_back_to_rfq_when_ws_stale():
    o, eng, calls = await mk_orch()
    now = time.time()
    rfq = VarQuote(67.0, 67.1, 15, ts=now - 1.0, quote_id="q1")
    o.feed._quotes["XAGS"] = rfq
    o._on_var_ws("XAGS", ws_upd(67.2, now - 2.0))      # > var_ws_max_age_s (1,5 s)
    await o.tick()
    assert calls[-1]["q"] is rfq
    assert o.status()["markets"][0]["var_ref"] == "rfq"


async def test_maker_mode_keeps_rfq_reference():
    o, eng, calls = await mk_orch(MAKER)
    now = time.time()
    rfq = VarQuote(67.0, 67.1, 15, ts=now - 1.0, quote_id="q1")
    o.feed._quotes["XAGS"] = rfq
    o._on_var_ws("XAGS", ws_upd(67.2, now - 0.2))
    await o.tick()
    assert calls[-1]["q"] is rfq


async def test_ws_reference_with_rfq_under_5s():
    """Revue : le demi-spread vient d'une cotation RFQ d'au plus 5 s (var_ws_spread_max_age_s)."""
    o, eng, calls = await mk_orch()
    now = time.time()
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 15, ts=now - 4.0, quote_id="q1")
    o._on_var_ws("XAGS", ws_upd(67.08, now - 0.2))
    await o.tick()
    assert calls[-1]["reason"] is None and calls[-1]["q"].quote_id == "ws"


async def test_rfq_over_5s_falls_back_to_rfq_and_stale_block():
    """Revue : une cotation RFQ de 6 s ne donne plus de demi-spread : repli sur la cotation RFQ,
    bloquée comme périmée (garde-fou sur l'âge RFQ, pas sur l'âge websocket)."""
    o, eng, calls = await mk_orch()
    now = time.time()
    rfq = VarQuote(67.0, 67.1, 15, ts=now - 6.0, quote_id="q1")
    o.feed._quotes["XAGS"] = rfq
    o._on_var_ws("XAGS", ws_upd(67.08, now - 0.2))
    await o.tick()
    assert calls[-1]["q"] is rfq and calls[-1]["reason"] == STALE_QUOTE_REASON


async def test_rfq_too_old_for_spread_falls_back_with_current_age_rules():
    o, eng, calls = await mk_orch()
    now = time.time()
    rfq = VarQuote(67.0, 67.1, 15, ts=now - 40.0, quote_id="q1")   # > var_ws_spread_max_age_s
    o.feed._quotes["XAGS"] = rfq
    o._on_var_ws("XAGS", ws_upd(67.08, now - 0.2))
    await o.tick()
    assert calls[-1]["q"] is rfq and calls[-1]["reason"] == STALE_QUOTE_REASON


async def test_ws_silence_alert_once_then_recovery():
    o, eng, calls = await mk_orch()
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 15, ts=time.time(), quote_id="q1")
    o._var_ws_started_at = time.time() - 10.0            # flux lancé, aucun prix depuis 10 s
    await o.tick()
    await o.tick()
    muted = [i for i in o.journal.inc if "flux de prix Var muet" in i[2]]
    assert len(muted) == 1 and muted[0][1] == "warn"
    o._on_var_ws("XAGS", ws_upd(67.05, time.time()))
    await o.tick()
    assert any("flux de prix Var rétabli" in i[2] for i in o.journal.inc)


async def test_ws_silence_not_alerted_when_ws_not_started():
    o, eng, calls = await mk_orch()
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 15, ts=time.time(), quote_id="q1")
    await o.tick()
    assert not any("flux de prix Var muet" in i[2] for i in o.journal.inc)


async def test_run_starts_var_price_stream_and_stops_it():
    o = Orchestrator(GlobalCfg(), [dataclasses.replace(CHASE)], HLC(), VC(), J(), clock=lambda: NOON)
    await o.setup()
    started = []

    class FakeWS:
        async def run(self, stop):
            started.append(True)
            await stop.wait()

    o.var_ws = FakeWS()

    class Feed(FakeHLFeed):
        def start(self): pass
        def stop(self): pass

    stop = asyncio.Event()

    async def driver():
        await asyncio.sleep(0.05)
        stop.set()

    with patch("arb.orchestrator.HLFeed", return_value=Feed()):
        await asyncio.wait_for(asyncio.gather(o.run(stop, "0xabc"), driver()), 20)
    assert started == [True]


def test_config_defaults_l2_l3():
    g = GlobalCfg()
    assert g.var_ws_enabled is True and g.var_ws_max_age_s == 1.5
    assert g.var_ws_silence_s == 5.0 and g.var_ws_spread_max_age_s == 5.0
    assert CHASE.basis_fast_tau_s == 60.0


# -- L2 côté moteur : la confirmation F1 reste portée par la cotation RFQ ------------------------

G = GlobalCfg(hedge_retry_s=1.0)
EM = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", fill_usd=1000,
               min_edge_bps=15, max_position_usd=3000, min_hedge_usd=20, entry_mode="chase")


def mk_eng():
    e = MarketEngine(EM, G, HL(), Var(), EJ(), HedgeGate(), sz_decimals=2, fee_bps=0.3)
    t = {"v": 1000.0}
    e._mono = lambda: t["v"]
    e._wall = lambda: t["v"]
    reqs = []
    e.fresh_quote_fn = lambda: reqs.append(t["v"])
    return e, t, reqs


def WSQ(ts, bid=67.00, ask=67.10):
    return VarQuote(bid, ask, 15, ts=ts, quote_id="ws")


async def test_ws_reference_advancing_does_not_confirm_without_new_rfq():
    e, t, reqs = mk_eng()
    rfq = VarQuote(67.00, 67.10, 15, ts=999.0, quote_id="q")
    await e.requote(WSQ(999.9), 0.0, 66.95, 67.25, 1.0, None, rfq=rfq)
    t["v"] += 1.0
    # nouveau prix websocket, mais aucune cotation RFQ plus récente que la détection
    await e.requote(WSQ(1000.9), 0.0, 66.95, 67.25, 1.0, None, rfq=rfq)
    assert e.hl.placed == [] and reqs == [1000.0]


async def test_new_rfq_confirms_pose_on_ws_reference():
    e, t, reqs = mk_eng()
    await e.requote(WSQ(999.9), 0.0, 66.95, 67.25, 1.0, None, rfq=VarQuote(67.0, 67.1, 15, ts=999.0))
    t["v"] += 0.5
    await e.requote(WSQ(1000.4), 0.0, 66.95, 67.25, 1.0, None, rfq=VarQuote(67.0, 67.1, 15, ts=1000.3))
    assert len(e.hl.placed) == 1 and e.hl.placed[0][2] == 67.25


async def test_fresh_rfq_must_also_show_the_edge():
    """La référence websocket voit l'écart, mais la cotation fraîche F1 non : pas de pose."""
    e, t, reqs = mk_eng()
    await e.requote(WSQ(999.9), 0.0, 66.95, 67.25, 1.0, None, rfq=VarQuote(67.0, 67.1, 15, ts=999.0))
    t["v"] += 0.5
    # cotation fraîche : ask 67.18, plancher 67.18 x 1,00173 = 67.296 > 67.25
    await e.requote(WSQ(1000.4), 0.0, 66.95, 67.25, 1.0, None, rfq=VarQuote(67.1, 67.18, 15, ts=1000.3))
    assert e.hl.placed == [] and e.chase_stats["not_confirmed"] == 1


# -- L3 : basis rapide ---------------------------------------------------------------------------

async def test_fast_basis_updated_on_ws_price_with_hl_bbo_mid():
    o, eng, calls = await mk_orch(OIL)
    o._on_var_ws("XAGS", ws_upd(67.0, time.time()))
    assert abs(o.basis_fast["XAGS"].value - 0.05) < 1e-9
    assert o.basis_fast["XAGS"].tau_s == 60.0


async def test_fast_basis_not_updated_on_stale_hl_book():
    o, eng, calls = await mk_orch(OIL, feed=FakeHLFeed(book=(66.9, 67.2), mid=67.05, book_age=3.0))
    o._on_var_ws("XAGS", ws_upd(67.0, time.time()))
    assert o.basis_fast["XAGS"].value is None


async def _oil_setup(slow_value, ws_age, warm=True):
    o, eng, calls = await mk_orch(OIL)
    now = time.time()
    rfq = VarQuote(67.0, 67.1, 15, ts=now - 0.5, quote_id="q1")
    o.feed._quotes["XAGS"] = rfq
    o.basis["XAGS"].value, o.basis["XAGS"]._ts = slow_value, rfq.ts
    o._last_basis_ts["XAGS"] = rfq.ts                     # pas de mise à jour de l'EMA lente
    if warm:
        o._on_var_ws("XAGS", ws_upd(67.0, now - ws_age - 61.0))   # chauffe (>= 60 s)
    o._on_var_ws("XAGS", ws_upd(67.0, now - ws_age))      # basis rapide = 67.05 - 67.0
    await o.tick()
    return o, calls


async def test_oil_uses_fast_basis_when_ws_fresh():
    o, calls = await _oil_setup(0.10, 0.2)
    assert abs(calls[-1]["basis"] - 0.05) < 1e-9
    assert abs(o.status()["markets"][0]["basis_fast"] - 0.05) < 1e-9


async def test_oil_keeps_slow_basis_until_fast_basis_is_warm():
    o, calls = await _oil_setup(0.10, 0.2, warm=False)
    assert abs(calls[-1]["basis"] - 0.10) < 1e-9


async def test_oil_falls_back_to_slow_basis_when_ws_stale():
    o, calls = await _oil_setup(0.10, 2.0)
    assert abs(calls[-1]["basis"] - 0.10) < 1e-9


async def test_basis_guard_still_on_slow_ema():
    # EMA lente à 1,0 : écart au basis |(67.05 - 67.05) - 1,0| / 67.05 = 149 pb > 30 pb
    o, calls = await _oil_setup(1.0, 0.2)
    assert calls[-1]["reason"] == "basis hors moyenne"


async def test_raw_market_chase_keeps_zero_basis():
    o, eng, calls = await mk_orch(CHASE)
    now = time.time()
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 15, ts=now - 0.5, quote_id="q1")
    o._on_var_ws("XAGS", ws_upd(67.0, now - 0.2))
    await o.tick()
    assert calls[-1]["basis"] == 0.0


async def test_force_reconnect_closes_current_socket():
    from arb.var_ws import VarPriceStream

    class WS:
        closed = False

        async def close(self):
            WS.closed = True

    s = VarPriceStream({"USOILP": "commodity"})
    s.force_reconnect()                     # pas de connexion : sans effet
    s._ws = WS()
    s.force_reconnect()
    await asyncio.sleep(0)
    assert WS.closed is True


async def test_ws_callback_never_raises():
    o, eng, calls = await mk_orch()

    class Boom(FakeHLFeed):
        def mid(self, coin):
            raise RuntimeError("boum")

    o.hl_feed = Boom()
    o._on_var_ws("XAGS", ws_upd(67.0, time.time()))     # ne lève pas
    assert any("prix websocket Var ignoré" in i[2] for i in o.journal.inc)
