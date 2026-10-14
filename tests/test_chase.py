"""Mode d'entrée « chase » (docs/superpowers/chase-mode-2026-09-23.md) : aucun ordre sans
inefficience, ordre ALO au meilleur prix du carnet HL, suivi par modify (au plus 1 par seconde),
plancher strict face à la cotation Var courante, annulation dès que l'inefficience disparaît."""
import dataclasses

import pytest

from arb.config import GlobalCfg, MarketCfg
from arb.engine import CANCEL_ONLY_REASON, HedgeGate, MarketEngine, QUOTING
from arb.pricing import VarQuote, chase_margins_bps

G = GlobalCfg(hedge_retry_s=1.0)
# seuil total = 15 + 0,3 (frais maker) + 2 (réserve) = 17,3 pb
M = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", fill_usd=1000,
              min_edge_bps=15, max_position_usd=3000, min_hedge_usd=20, entry_mode="chase",
              chase_fresh_confirm=False)   # F1 testée dans tests/test_fresh_quote.py
Q = VarQuote(67.00, 67.10, 15, ts=0)
SELL_FLOOR = 67.10 * (1 + 17.3e-4)   # 67.2161
BUY_FLOOR = 67.00 * (1 - 17.3e-4)    # 66.8841


class HL:
    def __init__(self):
        self.placed, self.cancelled, self.modified = [], [], []
        self.oid = 100
        self.place_error = None
        self.modify_error = None

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
        return True

    async def ioc(self, coin, is_buy, sz, px, reduce_only=False):
        return {"oid": 1, "filled_sz": sz, "avg_px": px, "error": None}


class Var:
    def __init__(self):
        self.orders = []

    async def quote(self, asset, kind, qty, prio=2, **kw):
        return VarQuote(67.05, 67.10, qty, ts=0, quote_id="q")

    async def market(self, quote_id, side, max_slippage, reduce_only=False, prio=0, **kw):
        self.orders.append((side, reduce_only))
        return "r1"

    async def order_status(self, rfq_id, prio=3, **kw):
        return {"status": "cleared", "price": "67.10"}

    async def positions(self, **kw):
        return {}


class J:
    def __init__(self):
        self.arbs, self.incidents = [], []

    def record_arb(self, *a):
        self.arbs.append(a)

    def record_incident(self, *a):
        self.incidents.append(a)


def mk(m=M, hl=None):
    e = MarketEngine(m, G, hl or HL(), Var(), J(), HedgeGate(), sz_decimals=2, fee_bps=0.3)
    t = {"v": 1000.0}
    e._mono = lambda: t["v"]
    e._wall = lambda: 0.0                # F3 : cotations d'âge nul ici (tests/test_fresh_quote.py)
    return e, t


async def rq(e, bid, ask, q=Q, reason=None, hold=False, basis=0.0, wide=1.0):
    await e.requote(q, basis, bid, ask, wide, reason, hold=hold)


# -- configuration -------------------------------------------------------------------------------

def test_config_defaults_and_validation():
    m = MarketCfg(var_asset="A", var_kind="index", hl_coin="xyz:A")
    assert m.entry_mode == "chase" and m.chase_ttl_s == 30.0 and m.chase_min_modify_interval_s == 1.0
    with pytest.raises(ValueError):
        MarketCfg(var_asset="A", var_kind="index", hl_coin="xyz:A", entry_mode="taker")


# -- détection -----------------------------------------------------------------------------------

async def test_no_order_while_edge_below_threshold():
    e, _ = mk()
    await rq(e, 66.95, 67.15)
    await rq(e, 66.90, 67.21)            # 67.21 < plancher 67.2161
    assert e.hl.placed == [] and e.orders == {"buy": None, "sell": None}
    assert e.chase_stats["detections"] == 0


async def test_order_placed_at_best_as_soon_as_edge_reached():
    e, _ = mk()
    await rq(e, 66.95, 67.25)
    assert len(e.hl.placed) == 1
    is_buy, sz, px, _ = e.hl.placed[0]
    assert is_buy is False and px == 67.25 and sz == 14.91   # 1000 / 67,05
    assert e.orders["sell"]["px"] == 67.25 and e.orders["buy"] is None
    assert e.chase_stats["detections"] == 1 and e.chase_stats["placed"] == 1
    # même carnet : rien de plus
    await rq(e, 66.95, 67.25)
    assert len(e.hl.placed) == 1 and e.hl.modified == [] and e.hl.cancelled == []


async def test_buy_side_is_symmetric():
    e, _ = mk()
    await rq(e, 66.89, 67.15)            # 66.89 > plancher achat 66.8841
    assert e.hl.placed == []
    await rq(e, 66.88, 67.15)
    assert len(e.hl.placed) == 1
    is_buy, sz, px, _ = e.hl.placed[0]
    assert is_buy is True and px == 66.88 and e.orders["sell"] is None


# -- suivi ---------------------------------------------------------------------------------------

async def test_chase_follows_best_at_most_one_modify_per_second():
    e, t = mk()
    await rq(e, 66.95, 67.30)
    oid0 = e.orders["sell"]["oid"]
    t["v"] += 0.5
    await rq(e, 66.95, 67.28)            # le best bouge, mais moins d'1 s depuis la pose
    assert e.hl.modified == []
    t["v"] += 0.6
    await rq(e, 66.95, 67.28)
    assert len(e.hl.modified) == 1 and e.hl.modified[0][0] == oid0 and e.hl.modified[0][3] == 67.28
    assert e.orders["sell"]["px"] == 67.28 and e.chase_stats["modified"] == 1
    t["v"] += 0.3
    await rq(e, 66.95, 67.26)
    assert len(e.hl.modified) == 1       # encore trop tôt
    t["v"] += 0.8
    await rq(e, 66.95, 67.26)
    assert len(e.hl.modified) == 2 and e.orders["sell"]["px"] == 67.26
    assert e.hl.cancelled == []


async def test_modify_failure_falls_back_to_cancel_and_place():
    hl = HL()
    e, t = mk(hl=hl)
    await rq(e, 66.95, 67.30)
    hl.modify_error = "Cannot modify canceled or filled order"
    t["v"] += 1.5
    await rq(e, 66.95, 67.27)
    assert len(hl.cancelled) == 1 and len(hl.placed) == 2 and hl.placed[1][2] == 67.27


async def test_floor_never_crossed():
    e, t = mk()
    await rq(e, 66.95, 67.30)
    # le best descend sous le plancher : on ne le suit pas, on annule
    t["v"] += 2.0
    await rq(e, 66.95, 67.21)
    assert e.orders["sell"] is None and len(e.hl.cancelled) == 1 and e.hl.modified == []
    # la cotation Var monte pendant qu'un ordre est au repos et qu'un modify est encore interdit :
    # l'ordre (désormais sous le plancher) est annulé, pas gardé.
    t["v"] += 3.5
    await rq(e, 66.95, 67.25)
    assert e.orders["sell"]["px"] == 67.25
    t["v"] += 0.2
    q2 = VarQuote(67.05, 67.15, 15, ts=1)          # plancher vente 67.2662
    await rq(e, 66.95, 67.30, q=q2)
    assert e.orders["sell"] is None
    # aucun prix posé ou modifié ne donne un écart sous le seuil face à la cotation du moment
    for _, _, px, _ in e.hl.placed:
        assert px >= SELL_FLOOR
    for _, _, _, px, _ in e.hl.modified:
        assert px >= SELL_FLOOR


# -- annulations ---------------------------------------------------------------------------------

async def test_cancel_when_edge_falls():
    e, t = mk()
    await rq(e, 66.95, 67.25)
    t["v"] += 0.1
    await rq(e, 66.95, 67.20)
    assert e.orders["sell"] is None and len(e.hl.cancelled) == 1
    assert e.chase_stats["cancelled"] == 1


async def test_cancel_when_var_quote_stale_even_with_hold():
    e, _ = mk()
    await rq(e, 66.95, 67.25)
    await rq(e, 66.95, 67.25, reason="cotation Var périmée", hold=True)
    assert e.orders["sell"] is None and len(e.hl.cancelled) == 1
    assert e.last_reason == "cotation Var périmée"


async def test_cancel_on_any_block_reason():
    e, _ = mk()
    await rq(e, 66.88, 67.25)
    assert e.orders["buy"] and e.orders["sell"]
    await rq(e, 66.88, 67.25, reason="carnet HL périmé")
    assert e.orders == {"buy": None, "sell": None} and len(e.hl.cancelled) == 2


async def test_cancel_when_ttl_expires():
    e, t = mk()
    await rq(e, 66.95, 67.25)
    t["v"] += 29.0
    await rq(e, 66.95, 67.25)
    assert e.orders["sell"] is not None
    t["v"] += 1.5
    await rq(e, 66.95, 67.25)
    assert e.orders["sell"] is None and len(e.hl.cancelled) == 1


async def test_ttl_not_reset_by_modify():
    e, t = mk()
    await rq(e, 66.95, 67.30)
    t["v"] += 20.0
    await rq(e, 66.95, 67.28)
    assert len(e.hl.modified) == 1
    t["v"] += 11.0
    await rq(e, 66.95, 67.28)
    assert e.orders["sell"] is None


async def test_cancel_only_keeps_valid_order_but_never_places():
    e, t = mk()
    e.cancel_only_fn = lambda: True
    await rq(e, 66.95, 67.25)
    assert e.hl.placed == [] and e.last_reason == CANCEL_ONLY_REASON
    e.cancel_only_fn = lambda: False
    await rq(e, 66.95, 67.25)
    e.cancel_only_fn = lambda: True
    t["v"] += 2.0
    await rq(e, 66.95, 67.24)
    assert e.hl.modified == [] and e.orders["sell"]["px"] == 67.25
    await rq(e, 66.95, 67.20)
    assert e.orders["sell"] is None


async def test_halt_blocks_placement():
    e, _ = mk()
    e.halt_fn = lambda: True
    await rq(e, 66.95, 67.25)
    assert e.hl.placed == []


# -- fills ---------------------------------------------------------------------------------------

async def test_partial_fill_hedged_then_chase_resumes():
    e, t = mk()
    await rq(e, 66.95, 67.25)
    oid = e.orders["sell"]["oid"]
    await e.on_fill({"coin": "xyz:SILVER", "side": "A", "px": 67.25, "sz": 5.0, "oid": oid, "fee": 0, "tid": 1, "time": 1})
    assert e.var.orders == [("buy", False)] and e.pos_hl == -5.0 and e.pos_var == 5.0
    assert e.state == QUOTING and e.pending_qty == 0
    # la couverture a retiré le reste ; l'inefficience dure : le chase reprend après le délai
    # avant repose (bid 66.99 : au-dessus du plancher de sortie côté achat, 66.9846, pour isoler
    # la vente)
    t["v"] += 0.5
    await rq(e, 66.99, 67.25)
    assert e.orders["sell"] is None
    t["v"] += 3.0
    await rq(e, 66.99, 67.25)
    assert e.orders["sell"] is not None and len(e.hl.placed) == 2 and e.orders["buy"] is None


async def test_small_partial_fill_keeps_chasing_with_remaining_size():
    e, t = mk()
    await rq(e, 66.95, 67.30)
    oid = e.orders["sell"]["oid"]
    await e.on_fill({"coin": "xyz:SILVER", "side": "A", "px": 67.30, "sz": 0.2, "oid": oid, "fee": 0, "tid": 1, "time": 1})
    assert e.var.orders == []            # 13 $ < min_hedge_usd : pas encore de couverture
    assert abs(e.orders["sell"]["sz"] - 14.71) < 1e-9
    t["v"] += 1.5
    await rq(e, 66.95, 67.28)
    assert len(e.hl.modified) == 1 and abs(e.hl.modified[0][2] - 14.71) < 1e-9


# -- inventaire ----------------------------------------------------------------------------------

def test_chase_margins_reducing_side_uses_exit_edge():
    m_buy, m_sell = chase_margins_bps(M, 0.3, 0.0, 1.0)
    assert m_buy == pytest.approx(17.3) and m_sell == pytest.approx(17.3)
    m_buy, m_sell = chase_margins_bps(M, 0.3, 1 / 3, 1.0)       # long HL : la vente réduit
    assert m_sell == pytest.approx(2.3)                          # exit 0 + 0,3 + 2
    assert m_buy == pytest.approx(17.3 + 15 / 3)                 # biais d'inventaire du mode maker
    m_buy, m_sell = chase_margins_bps(M, 0.3, -1.0, 1.0)         # short HL au max
    assert m_buy == pytest.approx(2.3) and m_sell is None


async def test_reducing_side_enters_at_exit_edge():
    e, _ = mk()
    e.pos_hl = 1000 / 67.05              # long 1000 $ (ratio 1/3)
    # 67.15 : écart vente 7,5 pb brut, sous 17,3 mais au-dessus du seuil de sortie 2,3
    await rq(e, 66.95, 67.15)
    assert len(e.hl.placed) == 1 and e.hl.placed[0][0] is False and e.hl.placed[0][2] == 67.15
    # sans position, la même situation ne pose rien
    e2, _ = mk()
    await rq(e2, 66.95, 67.15)
    assert e2.hl.placed == []


async def test_position_limit_blocks_loading_side():
    e, _ = mk()
    e.pos_hl = -3000 / 67.05             # short au maximum
    await rq(e, 66.95, 67.40)            # grosse inefficience côté vente : on ne charge pas plus
    assert all(p[0] is True for p in e.hl.placed)


# -- affichage -----------------------------------------------------------------------------------

async def test_edge_now_bps_reported():
    e, _ = mk()
    await rq(e, 66.95, 67.25)
    # vente : (67.25 - 67.10) / 67.10 = 22,35 pb brut, moins 0,3 et 2 = 20,05
    assert e.edge_now_bps == pytest.approx((67.25 - 67.10) / 67.10 * 1e4 - 2.3)
    assert e.edge_now_side == "sell"


async def test_maker_mode_still_available():
    m = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", fill_usd=1000,
                  min_hedge_usd=20, entry_mode="maker")
    e, _ = mk(m=m)
    await rq(e, 66.9, 67.2)
    assert len(e.hl.placed) == 2          # le mode maker cote les deux côtés sans inefficience


# -- orchestrateur -------------------------------------------------------------------------------

async def _orch_chase(age, book=(66.95, 67.25)):
    import time as _t
    from arb.orchestrator import Orchestrator
    from tests.test_final_exits import NOON, VCX
    from tests.test_live_fixes_stale import HLCRec
    from tests.test_orchestrator import FakeHLFeed, J as OJ
    m = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", min_edge_bps=15)
    h = HLCRec()
    o = Orchestrator(GlobalCfg(), [m], h, VCX(), OJ(), clock=lambda: NOON)
    await o.setup()
    o.hl_feed = FakeHLFeed(book=book, mid=sum(book) / 2)
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 14.9, ts=_t.time() - age, quote_id="q")
    return o, o.engines["XAGS"], h


async def _confirm(o):
    """F1 : la 1re détection demande une cotation fraîche ; on simule son arrivée."""
    import time as _t
    assert o.feed._urgent == ["XAGS"]
    o.feed._urgent.clear()
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 14.9, ts=_t.time() - 0.1, quote_id="q2")
    await o.tick()


async def test_orchestrator_chase_places_then_cancels_on_stale_quote():
    o, e, h = await _orch_chase(0.5)
    await o.tick()
    assert h.placed == []
    await _confirm(o)
    assert len(h.placed) == 1 and e.orders["sell"] is not None
    # cotation périmée mais plus jeune que quote_cancel_age_s : le mode maker garderait l'ordre
    # (L8), le mode chase l'annule.
    import time as _t
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 14.9, ts=_t.time() - 6.0, quote_id="q")
    await o.tick()
    assert e.orders["sell"] is None and len(h.cancels) == 1


async def test_orchestrator_status_exposes_chase_fields():
    o, e, h = await _orch_chase(0.5)
    await o.tick()
    await _confirm(o)
    st = o.status()
    mk_ = st["markets"][0]
    assert mk_["entry_mode"] == "chase" and mk_["min_edge_bps"] == 15
    assert mk_["edge_now_bps"] == pytest.approx((67.25 - 67.10) / 67.10 * 1e4 - 2.3)
    assert mk_["chase"]["placed"] == 1
    assert st["min_edge_bps"] == 15


async def test_orchestrator_status_global_min_edge_none_when_markets_differ():
    from arb.orchestrator import Orchestrator
    from tests.test_orchestrator import HLC2, J as OJ, VC2
    ms = [MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", min_edge_bps=15),
          MarketCfg(var_asset="GOLD", var_kind="commodity", hl_coin="xyz:GOLD", min_edge_bps=15)]
    o = Orchestrator(GlobalCfg(), ms, HLC2(), VC2(), OJ(), clock=lambda: 0)
    await o.setup()
    assert o.status()["min_edge_bps"] == 15
    ms[1].min_edge_bps = 12
    assert o.status()["min_edge_bps"] is None


# -- correctifs de revue ---------------------------------------------------------------------

async def test_r1_reducing_side_size_capped_at_position():
    e, _ = mk()
    e.pos_hl = 3.0                       # long 201 $ : la vente réduit
    await rq(e, 66.99, 67.15)
    assert e.hl.placed[0][0] is False and e.hl.placed[0][1] == 3.0   # jamais retournée
    e2, _ = mk()
    e2.pos_hl = 0.004                    # sous le pas de taille : rien à poser
    await rq(e2, 66.99, 67.15)
    assert e2.hl.placed == []


async def test_r2_replace_cooldown_after_any_cancel():
    e, t = mk()
    assert M.chase_replace_cooldown_s == 3.0
    await rq(e, 66.95, 67.25)
    t["v"] += 0.5
    await rq(e, 66.95, 67.20)            # l'écart retombe : annulation
    assert e.orders["sell"] is None
    t["v"] += 0.5
    await rq(e, 66.95, 67.25)            # l'écart revient aussitôt : pas de repose
    t["v"] += 1.0
    await rq(e, 66.95, 67.25)
    assert len(e.hl.placed) == 1
    t["v"] += 1.6                        # 3,1 s après l'annulation
    await rq(e, 66.95, 67.25)
    assert len(e.hl.placed) == 2


async def test_r2_ttl_cancel_not_followed_by_immediate_replace():
    e, t = mk()
    await rq(e, 66.95, 67.25)
    t["v"] += 30.5
    await rq(e, 66.95, 67.25)
    assert e.orders["sell"] is None
    t["v"] += 0.5
    await rq(e, 66.95, 67.25)
    assert len(e.hl.placed) == 1
    t["v"] += 3.0
    await rq(e, 66.95, 67.25)
    assert len(e.hl.placed) == 2


async def test_r2_stale_quote_cancel_also_cools_down():
    e, t = mk()
    await rq(e, 66.95, 67.25)
    await rq(e, 66.95, 67.25, reason="cotation Var périmée")
    t["v"] += 1.0
    await rq(e, 66.95, 67.25)
    assert len(e.hl.placed) == 1


async def test_r3_hedge_cancels_chase_orders_before_waiting_for_the_lock():
    import asyncio
    e, _ = mk()
    await rq(e, 66.88, 67.30)
    assert e.orders["buy"] and e.orders["sell"]
    sell_oid = e.orders["sell"]["oid"]
    await e.gate.lock.acquire()          # une autre couverture tient le verrou
    task = asyncio.create_task(e.on_fill({"coin": "xyz:SILVER", "side": "A", "px": 67.30, "sz": 5.0,
                                          "oid": sell_oid, "fee": 0, "tid": 1, "time": 1}))
    for _ in range(5):
        await asyncio.sleep(0)
    assert e.orders == {"buy": None, "sell": None}   # retirés avant même d'avoir le verrou
    e.gate.lock.release()
    await task
    assert e.var.orders == [("buy", False)]


async def test_r5_fallback_replace_resets_ttl():
    hl = HL()
    e, t = mk(hl=hl)
    await rq(e, 66.95, 67.30)
    t["v"] += 25.0
    hl.modify_error = "Cannot modify canceled or filled order"
    await rq(e, 66.95, 67.27)            # repli annulation puis pose : nouvel ordre
    assert len(hl.placed) == 2
    hl.modify_error = None
    t["v"] += 10.0                       # 35 s depuis la 1re pose, 10 s depuis la 2e
    await rq(e, 66.95, 67.27)
    assert e.orders["sell"] is not None


class FillDuringModifyHL(HL):
    """Un fill partiel arrive sur l'ancien ordre pendant le modify."""

    def __init__(self):
        super().__init__()
        self.engine = None
        self.fill_sz = 4.0

    async def modify(self, coin, oid, is_buy, sz, px):
        if self.fill_sz:
            f, self.fill_sz = self.fill_sz, 0
            await self.engine.on_fill({"coin": coin, "side": "B" if is_buy else "A", "px": 67.3, "sz": f,
                                       "oid": oid, "fee": 0, "tid": 9, "time": 1})
        return await super().modify(coin, oid, is_buy, sz, px)


async def test_r6_fill_during_modify_keeps_remaining_size():
    hl = FillDuringModifyHL()
    e, t = mk(hl=hl)
    hl.engine = e
    e.cfg = dataclasses.replace(M, min_hedge_usd=1e9)   # pas de couverture ici
    await rq(e, 66.95, 67.30)
    t["v"] += 1.5
    await rq(e, 66.95, 67.28)
    o = e.orders["sell"]
    assert o is not None and abs(o["sz"] - 10.91) < 1e-9          # 14,91 - 4
    assert abs(hl.modified[-1][2] - 10.91) < 1e-9                  # carnet réaligné


async def test_r6_order_filled_out_during_modify_is_cancelled():
    hl = FillDuringModifyHL()
    hl.fill_sz = 14.91
    e, t = mk(hl=hl)
    hl.engine = e
    e.cfg = dataclasses.replace(M, min_hedge_usd=1e9)
    await rq(e, 66.95, 67.30)
    t["v"] += 1.5
    await rq(e, 66.95, 67.28)
    assert e.orders["sell"] is None and hl.modified and hl.modified[-1][4] in hl.cancelled


async def test_orchestrator_wires_fresh_quote_hot_market_and_refresh_failure():
    o, e, h = await _orch_chase(0.5)
    assert o.feed.hot_interval_s == o.g.hot_quote_interval_s
    assert o.feed.hot_fn("XAGS") is False
    await o.tick()                                   # détection
    assert o.feed._urgent == ["XAGS"] and o.feed.hot_fn("XAGS") is True
    assert o.status()["markets"][0]["hot"] is True
    o.feed._failed["XAGS"] = True
    assert e.quote_refresh_failed_fn() is True
    assert o.feed.hot_fn("ZZZ") is False
