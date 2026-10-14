"""L4 (docs/superpowers/levers-2026-09-23.md) : mode d'entrée « lag_taker », désactivé par défaut.
Prise en IOC sur HL quand le prix websocket Var a bougé et que le meilleur prix HL n'a pas suivi."""
import dataclasses
from pathlib import Path

from arb.config import GlobalCfg, MarketCfg, load_config
from arb.engine import HedgeGate, MarketEngine
from arb.pricing import VarQuote, edge_bps, round_sz

G = GlobalCfg(hedge_retry_s=1.0)
# seuil côté qui charge, à plat : 3 (min_edge) + 0,9 (taker) + 2 (demi-spread Var) + 2 (réserve)
# = 7,9 pb ; côté qui réduit : 0 + 0,9 + 2 + 2 = 4,9 pb.
M = MarketCfg(var_asset="USOILP", var_kind="commodity", hl_coin="xyz:CL", fill_usd=500,
              min_edge_bps=3, max_position_usd=500, min_hedge_usd=20, entry_mode="lag_taker",
              basis_mode="ema")
MARK = 91.10
HS = 2e-4
BASIS = -0.38                    # basis = HL - Var ; prix juste HL = 91.10 - 0.38 = 90.72
FAIR = MARK + BASIS
REF = VarQuote(MARK * (1 - HS), MARK * (1 + HS), 5, ts=0, quote_id="ws")
UP = {"src": "ws", "ws_age": 0.3, "bbo_age": 0.2, "move_bps": 8.0, "ws_transport_s": 0.3,
      "ws_prev_gap_s": 1.0, "rfq_age": 1.0, "spread_ok": True, "hs_bps": 2.0,
      "book_sizes": (100.0, 100.0), "basis_ready": True}
DOWN = dict(UP, move_bps=-8.0)


class HL:
    def __init__(self):
        self.iocs, self.placed, self.cancelled = [], [], []
        self.res = None
        self.before = None

    async def ioc(self, coin, is_buy, sz, px, reduce_only=False):
        self.iocs.append((is_buy, sz, px, reduce_only))
        if self.before is not None:
            await self.before(is_buy, sz, px)
        if self.res is not None:
            return dict(self.res)
        return {"oid": 777, "filled_sz": sz, "avg_px": px, "error": None}

    async def place_alo(self, coin, is_buy, sz, px):
        self.placed.append((is_buy, sz, px))
        return {"oid": 5, "error": None}

    async def modify(self, *a):
        return {"oid": None, "error": "non"}

    async def cancel(self, coin, oid):
        self.cancelled.append(oid)
        return True


class Var:
    def __init__(self, price="91.08"):
        self.orders = []
        self.price = price

    async def quote(self, asset, kind, qty, prio=2, **kw):
        return VarQuote(91.08, 91.12, qty, ts=0, quote_id="q")

    async def market(self, quote_id, side, max_slippage, reduce_only=False, prio=0, **kw):
        self.orders.append((side, reduce_only))
        return "r1"

    async def order_status(self, rfq_id, prio=3, **kw):
        return {"status": "cleared", "price": self.price}

    async def positions(self, **kw):
        return {}


class J:
    def __init__(self):
        self.arbs, self.incidents = [], []

    def record_arb(self, *a):
        self.arbs.append(a)

    def record_incident(self, *a):
        self.incidents.append(a)


def mk(m=M, hl=None, var=None):
    e = MarketEngine(m, G, hl or HL(), var or Var(), J(), HedgeGate(), sz_decimals=2, fee_bps=0.3,
                     taker_fee_bps=0.9)
    t = {"v": 1000.0}
    e._mono = lambda: t["v"]
    e._wall = lambda: t["v"]
    return e, t


async def rq(e, bid, ask, lag=UP, reason=None, basis=BASIS, q=REF):
    await e.requote(q, basis, bid, ask, 1.0, reason, lag=lag)


SZ = round_sz(500 / FAIR, 2)


# -- décision ------------------------------------------------------------------------------------

async def test_no_take_below_threshold():
    e, t = mk()
    await rq(e, 90.60, 90.68)          # écart achat (90.72 - 90.68) / 90.68 = 4,4 pb < 7,9
    assert e.hl.iocs == [] and e.lag_stats["taken"] == 0


async def test_take_buy_ioc_at_best_ask_when_hl_lags():
    e, t = mk()
    await rq(e, 90.60, 90.62)          # (90.72 - 90.62) / 90.62 = 11,0 pb > 7,9
    assert e.hl.iocs == [(True, SZ, 90.62, False)]
    assert e.lag_stats["signals"] == 1 and e.lag_stats["taken"] == 1 and e.lag_stats["filled"] == 1
    info = [i for i in e.journal.incidents if "prise sur retard" in i[2]]
    assert len(info) == 1 and info[0][1] == "info"
    assert e.hl.placed == []            # jamais d'ordre ALO en lag_taker


async def test_take_sell_symmetric():
    e, t = mk()
    await rq(e, 90.82, 90.84, lag=DOWN)   # (90.82 - 90.72) / 90.72 = 11,0 pb
    assert e.hl.iocs == [(False, SZ, 90.82, False)]


async def test_var_must_have_moved_in_the_trade_direction():
    e, t = mk()
    await rq(e, 90.60, 90.62, lag=dict(UP, move_bps=0.5))    # mouvement trop faible
    await rq(e, 90.60, 90.62, lag=DOWN)                      # mouvement dans l'autre sens
    assert e.hl.iocs == []
    assert e.lag_stats["skipped_move"] == 2


async def test_no_take_until_fast_basis_is_warm():
    e, t = mk()
    await rq(e, 90.60, 90.62, lag=dict(UP, basis_ready=False))
    assert e.hl.iocs == [] and e.lag_stats["skipped_basis"] == 1


def test_basis_ema_warm_duration():
    from arb.pricing import BasisEma
    b = BasisEma(60.0)
    assert b.warm_s() == 0.0
    b.update(1.0, 0.9, 100.0)
    b.update(1.0, 0.9, 130.0)
    assert b.warm_s() == 30.0


async def test_freshness_guards():
    e, t = mk()
    await rq(e, 90.60, 90.62, lag=dict(UP, ws_age=1.6))
    await rq(e, 90.60, 90.62, lag=dict(UP, bbo_age=1.2))
    await rq(e, 90.60, 90.62, lag=dict(UP, src="rfq"))
    await rq(e, 90.60, 90.62, lag=None)
    assert e.hl.iocs == []
    assert e.lag_stats["skipped_age"] == 4


async def test_at_most_one_take_per_interval():
    e, t = mk(hl=HL())
    e.hl.res = {"oid": 777, "filled_sz": 0.0, "avg_px": None, "error": None}   # IOC non rempli
    await rq(e, 90.60, 90.62)
    t["v"] += 4.9
    await rq(e, 90.60, 90.62)
    assert len(e.hl.iocs) == 1 and e.lag_stats["skipped_interval"] == 1
    assert e.lag_stats["no_fill"] == 1
    t["v"] += 0.2
    await rq(e, 90.60, 90.62)
    assert len(e.hl.iocs) == 2


async def test_block_reason_cancel_only_and_halt_prevent_take():
    e, t = mk()
    await rq(e, 90.60, 90.62, reason="websocket HL muet")
    assert e.last_reason == "websocket HL muet"
    e.cancel_only_fn = lambda: True
    await rq(e, 90.60, 90.62)
    e.cancel_only_fn = lambda: False
    e.halt_fn = lambda: True
    await rq(e, 90.60, 90.62)
    assert e.hl.iocs == []


async def test_no_take_while_fill_waits_for_hedge():
    e, t = mk()
    e.pending_qty = 1.0
    await rq(e, 90.60, 90.62)
    assert e.hl.iocs == []


async def test_inventory_bias_and_reducing_side_at_exit_edge():
    e, t = mk()
    e.pos_hl = 5.52                     # long à la position maximale : plus d'achat
    await rq(e, 90.60, 90.62)
    assert e.hl.iocs == []
    # côté qui réduit : seuil 4,9 pb ; (90.77 - 90.72) / 90.72 = 5,5 pb
    await rq(e, 90.77, 90.79, lag=dict(UP, move_bps=-2.0))
    assert e.hl.iocs == [(False, SZ, 90.77, False)]


async def test_reducing_side_never_flips_position():
    e, t = mk()
    e.pos_hl = 1.0
    await rq(e, 90.77, 90.79, lag=dict(UP, move_bps=-2.0))
    assert e.hl.iocs == [(False, 1.0, 90.77, False)]


async def test_leftover_resting_orders_cancelled_in_lag_mode():
    e, t = mk()
    e.orders["buy"] = {"oid": 42, "px": 90.5, "sz": 1.0}
    e._my_oids.add(42)
    await rq(e, 90.60, 90.68)
    assert e.hl.cancelled == [42] and e.orders["buy"] is None


# -- exécution et couverture -----------------------------------------------------------------------

async def test_live_fill_via_user_fills_is_hedged_with_taker_fee_and_basis():
    e, t = mk()
    await rq(e, 90.60, 90.62)
    assert e.pending_qty == 0 and e.hl.iocs
    await e.on_fill({"coin": "xyz:CL", "side": "B", "px": 90.62, "sz": SZ, "oid": 777, "fee": 0.0,
                     "tid": 1, "time": 1})
    assert e.var.orders == [("sell", False)]
    arb = e.journal.arbs[-1]
    assert arb[-1] == "hedged"
    # écart contre var_px + basis (91.08 - 0.38), net des frais TAKER (0,9 pb)
    assert abs(arb[8] - edge_bps(True, 90.62, 91.08 + BASIS, 0.9)) < 1e-9
    assert abs(arb[4] - 0.9e-4 * SZ * 90.62) < 1e-12
    assert e.lag_stats["hedged"] == 1
    slip = [i for i in e.journal.incidents if "glissement couverture" in i[2]]
    assert len(slip) == 1 and "écart attendu" in slip[0][2]


async def test_fill_arriving_before_ioc_response_is_replayed():
    e, t = mk()

    async def early(is_buy, sz, px):
        await e.on_fill({"coin": "xyz:CL", "side": "B", "px": px, "sz": sz, "oid": 777, "fee": 0.0,
                         "tid": 1, "time": 1})

    e.hl.before = early
    await rq(e, 90.60, 90.62)
    assert e.var.orders == [("sell", False)] and e.pending_qty == 0


async def test_paper_ioc_simulates_fill_and_hedge():
    e, t = mk()
    e.paper = True
    e.hl.res = {"oid": None, "filled_sz": SZ, "avg_px": 90.62, "error": None}
    await rq(e, 90.60, 90.62)
    assert e.var.orders == [("sell", False)]
    assert abs(e.pos_hl - SZ) < 1e-12 and abs(e.pos_var + SZ) < 1e-12


async def test_live_ioc_without_oid_is_flagged_not_hedged():
    e, t = mk()
    e.hl.res = {"oid": None, "filled_sz": SZ, "avg_px": 90.62, "error": None}
    await rq(e, 90.60, 90.62)
    assert e.var.orders == []
    assert any(i[1] == "error" and "sans oid" in i[2] for i in e.journal.incidents)


async def test_ioc_error_backs_off():
    e, t = mk()
    e.hl.res = {"oid": None, "filled_sz": 0.0, "avg_px": None, "error": "Insufficient margin"}
    await rq(e, 90.60, 90.62)
    assert e.lag_stats["errors"] == 1
    assert any("IOC" in i[2] and i[1] == "warn" for i in e.journal.incidents)


# -- mode et configuration -------------------------------------------------------------------------

async def test_set_entry_mode_cancels_orders_and_validates():
    e, t = mk(m=dataclasses.replace(M, entry_mode="chase"))
    e.orders["sell"] = {"oid": 9, "px": 91.0, "sz": 1.0}
    await e.set_entry_mode("lag_taker")
    assert e.cfg.entry_mode == "lag_taker" and e.hl.cancelled == [9]
    try:
        await e.set_entry_mode("yolo")
        assert False
    except ValueError:
        pass


def test_lag_taker_disabled_by_default():
    assert MarketCfg(var_asset="A", var_kind="index", hl_coin="xyz:A").entry_mode == "chase"
    g, markets = load_config(Path(__file__).resolve().parents[1] / "config.toml")
    # config.toml du 23/09 : seuls les pétroles passent en lag_taker (décision de l'utilisateur, analyse 2 h).
    assert markets and all(m.entry_mode == ("lag_taker" if m.var_asset in ("USOILP", "UKOILP") else "chase")
                           for m in markets)
    m = MarketCfg(var_asset="A", var_kind="index", hl_coin="xyz:A", entry_mode="lag_taker")
    assert m.lag_var_max_age_s == 1.5 and m.lag_hl_max_age_s == 1.0 and m.lag_min_interval_s == 5.0


# -- orchestrateur : informations de retard transmises au moteur ------------------------------------

async def test_orchestrator_passes_lag_info_and_paper_flag():
    import time
    from tests.test_levers_var_ref import NOON, ws_upd
    from tests.test_orchestrator import FakeHLFeed, HLC, J as OJ, VC
    from arb.orchestrator import Orchestrator

    lagm = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", entry_mode="lag_taker")
    o = Orchestrator(GlobalCfg(), [lagm], HLC(), VC(), OJ(), clock=lambda: NOON)
    await o.setup()
    o.hl_feed = FakeHLFeed(book=(66.9, 67.2), mid=67.05, book_age=0.3)
    eng = o.engines["XAGS"]
    assert eng.paper is True                    # HLC.paper = True
    calls = []

    async def requote(q, basis, hl_bid, hl_ask, wide, reason, hold=False, rfq=None, lag=None):
        calls.append({"q": q, "basis": basis, "lag": lag})

    eng.requote = requote
    now = time.time()
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 15, ts=now - 1.0, quote_id="q1")
    o._on_var_ws("XAGS", ws_upd(67.00, now - 1.2))
    o._on_var_ws("XAGS", ws_upd(67.10, now - 0.2))
    assert o._wake.is_set()                     # réveil immédiat de la boucle de tick
    await o.tick()
    lag = calls[-1]["lag"]
    assert lag["basis_ready"] is False          # basis rapide pas encore chauffé (60 s)
    assert lag["src"] == "ws" and abs(lag["ws_age"] - 0.2) < 0.05 and lag["bbo_age"] == 0.3
    assert abs(lag["move_bps"] - (67.10 / 67.00 - 1) * 1e4) < 1e-6
    # basis rapide (mid HL 67.05 - Var) utilisé en lag_taker même hors basis_mode "ema"
    assert calls[-1]["basis"] == o.basis_fast["XAGS"].value
    st = o.status()["markets"][0]
    assert st["entry_mode"] == "lag_taker" and "lag" in st and st["lag"]["taken"] == 0
