"""Correctifs de revue des leviers L1 à L5 (2026-09-23) : taille IOC bornée au carnet et
reliquats, position maximale, demi-spread Var frais, âge RFQ, horodatage serveur, IOC en erreur."""
import dataclasses
import time

from arb.config import GlobalCfg, MarketCfg
from arb.engine import BLOCKED
from arb.pricing import VarQuote, round_sz
from arb.risk import STALE_QUOTE_REASON
from tests.test_lag_taker import FAIR, M, SZ, UP, mk, rq
from tests import test_chase as tc


# -- 1. taille IOC bornée au carnet, reliquats sous min_hedge_usd ----------------------------------

async def test_ioc_size_capped_at_visible_best_size():
    e, t = mk()
    await rq(e, 90.60, 90.62, lag=dict(UP, book_sizes=(100.0, 2.0)))
    assert e.hl.iocs == [(True, 2.0, 90.62, False)]


async def test_no_take_when_visible_size_under_min_hedge():
    e, t = mk()
    await rq(e, 90.60, 90.62, lag=dict(UP, book_sizes=(100.0, 0.1)))    # 9 $ < 20 $
    await rq(e, 90.60, 90.62, lag=dict(UP, book_sizes=(None, None)))
    assert e.hl.iocs == [] and e.lag_stats["skipped_size"] == 2


async def test_partial_fill_dust_closed_reduce_only_right_away():
    e, t = mk()
    e.paper = True
    e.hl.res = {"oid": None, "filled_sz": 0.1, "avg_px": 90.62, "error": None}   # 9 $ < 20 $
    await rq(e, 90.60, 90.62)
    assert e.hl.iocs[-1][0] is False and e.hl.iocs[-1][1] == 0.1 and e.hl.iocs[-1][3] is True
    assert e.pending_qty == 0 and abs(e.pos_hl) < 1e-12 and e.var.orders == []
    assert any("reliquat HL" in i[2] and i[1] == "warn" for i in e.journal.incidents)


async def test_late_dust_from_user_fills_settled_on_next_tick():
    e, t = mk()
    e.hl.res = {"oid": 777, "filled_sz": 0.1, "avg_px": 90.62, "error": None}
    await rq(e, 90.60, 90.62)
    await e.on_fill({"coin": "xyz:CL", "side": "B", "px": 90.62, "sz": 0.1, "oid": 777, "fee": 0.0,
                     "tid": 1, "time": 1})
    assert e.pending_qty == 0.1
    e._pending_t = time.time() - 2.0
    e.hl.res = None
    await rq(e, 90.60, 90.68)
    assert e.pending_qty == 0 and e.hl.iocs[-1][3] is True


async def test_dust_residual_never_left_stuck_on_ioc_error():
    e, t = mk()
    e.pos_hl, e.pending_qty, e._pending_px, e._pending_t = 0.1, 0.1, 90.62, time.time() - 2

    async def boom(*a, **k):
        raise TimeoutError("délai")

    e.hl.ioc = boom
    await e._settle_lag_dust()
    assert e.pending_qty == 0
    assert any(i[1] == "error" and "remis à 0" in i[2] for i in e.journal.incidents)


# -- 2. position maximale respectée ------------------------------------------------------------------

async def test_lag_loading_take_refused_past_max_position():
    e, t = mk()
    e.pos_hl = 1.0                       # 90,7 $ long ; + 5,51 x 90,72 = 590 $ > 500 $
    await rq(e, 90.60, 90.62)
    assert e.hl.iocs == []


async def test_chase_loading_post_refused_past_max_position():
    # max 3 000 $ ; 30 x 67,05 = 2 012 $ long + 14,91 x 67,05 = 1 000 $ -> 3 011 $ > 3 000 $
    e, t = tc.mk()
    e.pos_hl = 30.0
    await tc.rq(e, 66.50, 67.40)
    assert [p[0] for p in e.hl.placed] == [False]            # vente (réduit) seulement
    # 20 x 67,05 = 1 341 $ + 1 000 $ = 2 341 $ <= 3 000 $ : l'achat est permis
    e2, t2 = tc.mk()
    e2.pos_hl = 20.0
    await tc.rq(e2, 66.50, 67.40)
    assert True in [p[0] for p in e2.hl.placed]


# -- 3. demi-spread Var frais avec marge ----------------------------------------------------------------

async def test_no_take_without_fresh_rfq_spread():
    e, t = mk()
    await rq(e, 90.60, 90.62, lag=dict(UP, spread_ok=False))
    assert e.hl.iocs == [] and e.lag_stats["skipped_spread"] == 1


async def test_spread_buffer_raises_threshold():
    e, t = mk()
    # seuil = 3 + 0,9 + 6 + 2 = 11,9 pb > écart 11,0 pb
    await rq(e, 90.60, 90.62, lag=dict(UP, hs_bps=6.0))
    assert e.hl.iocs == []


async def test_orchestrator_lag_spread_is_buffered_median():
    from tests.test_levers_var_ref import mk_orch, ws_upd
    lagm = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", entry_mode="lag_taker")
    o, eng, calls = await mk_orch(lagm)
    now = time.time()
    for i, (b, a) in enumerate(((67.0, 67.2), (67.0, 67.2), (67.04, 67.06))):
        o.feed._quotes["XAGS"] = VarQuote(b, a, 15, ts=now - 3 + i, quote_id="q")
        o._on_var_ws("XAGS", ws_upd(67.05, now - 0.1))
        await o.tick()
    # dernier 1,5 pb, médiane 14,9 pb -> 14,9 x 1,5
    hs_med = 0.1 / 67.1 * 1e4
    lag = calls[-1]["lag"]
    assert abs(lag["hs_bps"] - hs_med * 1.5) < 1e-6 and lag["spread_ok"] is True


# -- 4. âge RFQ ---------------------------------------------------------------------------------------

async def test_stale_block_uses_max_of_ws_and_rfq_age():
    from tests.test_levers_var_ref import mk_orch, ws_upd
    o, eng, calls = await mk_orch(g=GlobalCfg(quote_max_age_s=3.0))
    now = time.time()
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 15, ts=now - 4.0, quote_id="q1")
    o._on_var_ws("XAGS", ws_upd(67.08, now - 0.2))
    await o.tick()
    assert calls[-1]["q"].quote_id == "ws" and calls[-1]["reason"] == STALE_QUOTE_REASON


async def test_chase_f3_age_counts_rfq_age():
    from tests.test_levers_var_ref import WSQ, mk_eng
    e, t, reqs = mk_eng()
    await e.requote(WSQ(999.9), 0.0, 66.95, 67.25, 1.0, None, rfq=VarQuote(67.0, 67.1, 15, ts=998.0))
    t["v"] = 1001.0
    # RFQ plus récente que la détection mais de 2 s : au-delà de chase_max_quote_age_s (1,5)
    await e.requote(WSQ(1000.9), 0.0, 66.95, 67.25, 1.0, None, rfq=VarQuote(67.0, 67.1, 15, ts=999.0))
    assert e.hl.placed == []
    await e.requote(WSQ(1000.9), 0.0, 66.95, 67.25, 1.0, None, rfq=VarQuote(67.0, 67.1, 15, ts=1000.5))
    assert len(e.hl.placed) == 1


# -- 5. horodatage serveur du prix websocket ------------------------------------------------------------

async def test_take_refused_on_slow_transport_or_catch_up_jump():
    e, t = mk()
    await rq(e, 90.60, 90.62, lag=dict(UP, ws_transport_s=1.2))
    await rq(e, 90.60, 90.62, lag=dict(UP, ws_prev_gap_s=2.5))
    await rq(e, 90.60, 90.62, lag=dict(UP, ws_transport_s=None))
    assert e.hl.iocs == [] and e.lag_stats["skipped_ws_stale"] == 3


async def test_orchestrator_passes_transport_and_prev_gap():
    from tests.test_levers_var_ref import mk_orch
    lagm = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", entry_mode="lag_taker")
    o, eng, calls = await mk_orch(lagm)
    now = time.time()
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 15, ts=now - 1.0, quote_id="q1")
    for recv, srv in ((now - 3.2, now - 3.5), (now - 0.2, now - 0.5)):
        o._on_var_ws("XAGS", {"mark": 67.05, "ts_recv_ms": int(recv * 1000), "ts_server_ms": int(srv * 1000)})
    await o.tick()
    lag = calls[-1]["lag"]
    assert abs(lag["ws_transport_s"] - 0.3) < 0.01 and abs(lag["ws_prev_gap_s"] - 3.0) < 0.01


# -- 6. IOC en erreur ---------------------------------------------------------------------------------

def _raising(e, positions):
    async def boom(*a, **k):
        raise TimeoutError("délai dépassé")

    async def pos():
        if isinstance(positions, Exception):
            raise positions
        return positions

    e.hl.ioc = boom
    e.hl.positions = pos


async def test_ioc_exception_executed_is_hedged_from_position_read():
    e, t = mk()
    _raising(e, {"xyz:CL": SZ})
    await rq(e, 90.60, 90.62)
    assert e.var.orders == [("sell", False)] and abs(e.pos_hl - SZ) < 1e-12
    assert e.lag_stats["errors"] == 1 and e.lag_stats["hedged"] == 1
    # un fill userFills tardif de cet IOC (oid inconnu) reste orphelin, jamais compté deux fois
    await e.on_fill({"coin": "xyz:CL", "side": "B", "px": 90.62, "sz": SZ, "oid": 888, "fee": 0.0,
                     "tid": 1, "time": 1})
    assert 888 in e._orphan_fills and abs(e.pos_hl - SZ) < 1e-12


async def test_ioc_exception_not_executed_no_hedge():
    e, t = mk()
    _raising(e, {})
    await rq(e, 90.60, 90.62)
    assert e.var.orders == [] and e.pending_qty == 0
    assert any("aucune exécution" in i[2] for i in e.journal.incidents)


async def test_ioc_exception_and_unreadable_position_blocks():
    e, t = mk()
    _raising(e, RuntimeError("réseau"))
    await rq(e, 90.60, 90.62)
    assert e.state == BLOCKED


async def test_dust_settle_never_waits_for_busy_hedge_lock():
    e, t = mk()
    e.pos_hl, e.pending_qty, e._pending_px, e._pending_t = 0.1, 0.1, 90.62, time.time() - 2
    await e.gate.lock.acquire()
    try:
        await e._settle_lag_dust()          # ne bloque pas
        assert e.pending_qty == 0.1 and e.hl.iocs == []
    finally:
        e.gate.lock.release()
    await e._settle_lag_dust()
    assert e.pending_qty == 0
