"""Cotation Var fraîche pour le chase (docs/superpowers/fresh-quote-2026-09-23.md) : F1
confirmation par cotation fraîche, F2 marchés chauds, F3 âge maximal de cotation, F4 mesure du
glissement, F5 écart du journal contre var_px + basis."""
import dataclasses

import pytest

from arb.config import GlobalCfg, MarketCfg
from arb.engine import HedgeGate, MarketEngine
from arb.pricing import VarQuote, edge_bps
from tests.test_chase import HL, J, Var

G = GlobalCfg(hedge_retry_s=1.0)
# seuil total = 15 + 0,3 + 2 = 17,3 pb ; plancher vente 67.2161 face à ask 67.10
M = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", fill_usd=1000,
              min_edge_bps=15, max_position_usd=3000, min_hedge_usd=20, entry_mode="chase")


def mk(m=M, hl=None, var=None):
    e = MarketEngine(m, G, hl or HL(), var or Var(), J(), HedgeGate(), sz_decimals=2, fee_bps=0.3)
    t = {"v": 1000.0}
    e._mono = lambda: t["v"]
    e._wall = lambda: t["v"]            # même horloge : q.ts s'exprime dans cette échelle
    reqs = []
    e.fresh_quote_fn = lambda: reqs.append(t["v"])
    return e, t, reqs


def Q(ts, bid=67.00, ask=67.10):
    return VarQuote(bid, ask, 15, ts=ts, quote_id="q")


async def rq(e, bid, ask, q, basis=0.0):
    await e.requote(q, basis, bid, ask, 1.0, None)


# -- configuration -------------------------------------------------------------------------------

def test_config_defaults():
    assert M.chase_fresh_confirm is True and M.chase_max_quote_age_s == 1.5
    oil = MarketCfg(var_asset="UKOILP", var_kind="commodity", hl_coin="xyz:BRENTOIL", basis_mode="ema")
    assert oil.chase_max_quote_age_s == 1.0
    assert MarketCfg(var_asset="A", var_kind="index", hl_coin="xyz:A",
                     chase_max_quote_age_s=2.0).chase_max_quote_age_s == 2.0
    g = GlobalCfg()
    assert g.hot_quote_interval_s == 1.0 and g.hot_window_s == 10.0


# -- F1 : confirmation par cotation fraîche ------------------------------------------------------

async def test_detection_requests_fresh_quote_and_does_not_place():
    e, t, reqs = mk()
    await rq(e, 66.95, 67.25, Q(999.8))
    assert e.hl.placed == [] and reqs == [1000.0]
    assert e.chase_stats["detections"] == 1 and e.chase_stats["fresh_requests"] == 1
    # même cotation au tick suivant : toujours pas de pose, pas de nouvelle demande
    t["v"] += 0.5
    await rq(e, 66.95, 67.25, Q(999.8))
    assert e.hl.placed == [] and reqs == [1000.0]


async def test_fresh_quote_confirms_then_places_with_floor_from_fresh_quote():
    e, t, reqs = mk()
    await rq(e, 66.95, 67.25, Q(999.8))
    t["v"] += 0.4
    await rq(e, 66.95, 67.25, Q(1000.3, 67.02, 67.12))
    assert len(e.hl.placed) == 1 and e.hl.placed[0][2] == 67.25
    assert e.chase_stats["placed"] == 1


async def test_fresh_quote_invalidates_detection_no_order():
    e, t, reqs = mk()
    await rq(e, 66.95, 67.25, Q(999.8))
    t["v"] += 0.4
    # la cotation fraîche a bougé : plancher 67.18 * 1,00173 = 67.296 > 67.25
    await rq(e, 66.95, 67.25, Q(1000.3, 67.10, 67.18))
    assert e.hl.placed == [] and e.chase_stats["not_confirmed"] == 1


async def test_no_second_request_within_cooldown_window():
    e, t, reqs = mk()
    await rq(e, 66.95, 67.25, Q(999.8))
    t["v"] += 0.4
    await rq(e, 66.95, 67.25, Q(1000.3, 67.10, 67.18))     # non confirmée
    t["v"] += 0.5
    await rq(e, 66.95, 67.25, Q(1000.3))                   # nouvelle détection dans la fenêtre
    assert reqs == [1000.0]
    assert e.hl.placed == []
    t["v"] = 1003.5                                        # fenêtre (3 s) écoulée
    await rq(e, 66.95, 67.25, Q(1000.3))
    assert e.hl.placed == []                               # cotation déjà vue à la détection
    t["v"] += 0.1
    await rq(e, 66.95, 67.25, Q(1003.55))                  # plus récente que la détection
    assert len(e.hl.placed) == 1


async def test_after_cancel_new_pose_needs_new_confirmation():
    e, t, reqs = mk()
    await rq(e, 66.95, 67.25, Q(999.9))
    t["v"] += 0.2
    await rq(e, 66.95, 67.25, Q(1000.1))
    assert len(e.hl.placed) == 1
    t["v"] += 0.2
    await rq(e, 66.95, 67.15, Q(1000.3))                   # écart refermé : annulation
    assert e.orders["sell"] is None
    t["v"] = 1003.5
    await rq(e, 66.95, 67.25, Q(1003.4))                   # nouvelle détection
    assert len(e.hl.placed) == 1
    t["v"] += 0.3
    await rq(e, 66.95, 67.25, Q(1003.7))
    assert len(e.hl.placed) == 2


async def test_confirmation_can_be_disabled():
    e, t, reqs = mk(m=dataclasses.replace(M, chase_fresh_confirm=False))
    await rq(e, 66.95, 67.25, Q(999.8))
    assert len(e.hl.placed) == 1


# -- F3 : âge maximal de cotation ----------------------------------------------------------------

async def test_no_new_pose_on_quote_older_than_max_age():
    e, t, reqs = mk()
    await rq(e, 66.95, 67.25, Q(998.0))
    t["v"] += 0.1
    await rq(e, 66.95, 67.25, Q(998.2))                    # plus récente mais 1,9 s d'âge
    assert e.hl.placed == []
    t["v"] += 0.1
    await rq(e, 66.95, 67.25, Q(1000.0))                   # 0,2 s
    assert len(e.hl.placed) == 1


async def test_oil_max_age_is_one_second():
    m = dataclasses.replace(M, basis_mode="ema", chase_max_quote_age_s=None)
    m.__post_init__()
    e, t, reqs = mk(m=m)
    await rq(e, 66.95, 67.25, Q(999.0))
    t["v"] += 0.1
    await rq(e, 66.95, 67.25, Q(999.05))                   # 1,05 s
    assert e.hl.placed == []


async def _resting(e, t):
    await rq(e, 66.95, 67.25, Q(999.9))
    t["v"] += 0.2
    await rq(e, 66.95, 67.25, Q(1000.1))
    assert e.orders["sell"] is not None


async def test_resting_order_kept_while_hot_tracking_may_still_refresh():
    e, t, reqs = mk()
    await _resting(e, t)
    t["v"] = 1002.0                                        # âge 1,9 s > 1,5 mais < 1,5 + 1
    await rq(e, 66.95, 67.30, Q(1000.1))                   # best a bougé : pas de modify sur vieille cotation
    assert e.orders["sell"] is not None and e.hl.modified == [] and e.hl.cancelled == []


async def test_resting_order_cancelled_when_refresh_failed():
    e, t, reqs = mk()
    await _resting(e, t)
    e.quote_refresh_failed_fn = lambda: True
    t["v"] = 1001.7                                        # âge 1,6 s
    await rq(e, 66.95, 67.25, Q(1000.1))
    assert e.orders["sell"] is None and e.chase_stats["stale_cancels"] == 1


async def test_resting_order_cancelled_when_hot_tracking_did_not_refresh():
    e, t, reqs = mk()
    await _resting(e, t)
    t["v"] = 1002.7                                        # âge 2,6 s > 1,5 + 1
    await rq(e, 66.95, 67.25, Q(1000.1))
    assert e.orders["sell"] is None and e.chase_stats["stale_cancels"] == 1


async def test_refresh_failed_alone_does_not_cancel_fresh_quote():
    e, t, reqs = mk()
    await _resting(e, t)
    e.quote_refresh_failed_fn = lambda: True
    t["v"] += 0.5
    await rq(e, 66.95, 67.25, Q(1000.1))                   # âge 0,6 s
    assert e.orders["sell"] is not None


# -- F2 : marché chaud ---------------------------------------------------------------------------

async def test_hot_while_order_rests_or_detection_within_window():
    e, t, reqs = mk()
    assert e.chase_hot() is False
    await rq(e, 66.95, 67.25, Q(999.8))                    # détection
    assert e.chase_hot() is True
    t["v"] += 0.2
    await rq(e, 66.95, 67.15, Q(1000.1))                   # plus d'inefficience
    t["v"] += 9.0
    assert e.chase_hot() is True
    t["v"] += 1.0
    assert e.chase_hot() is False
    await rq(e, 66.95, 67.25, Q(1010.1))
    t["v"] += 0.2
    await rq(e, 66.95, 67.25, Q(1010.25))
    assert e.orders["sell"] is not None
    t["v"] += 20.0
    assert e.chase_hot() is True                           # ordre au repos


def test_maker_never_hot():
    e, t, reqs = mk(m=dataclasses.replace(M, entry_mode="maker"))
    e._chase_seen_at = t["v"]
    assert e.chase_hot() is False


# -- F4 / F5 : mesure du glissement, écart contre var_px + basis ---------------------------------

class VarAt(Var):
    def __init__(self, px):
        super().__init__()
        self.px = px

    async def order_status(self, rfq_id, prio=3, **kw):
        return {"status": "cleared", "price": str(self.px)}


async def test_hedge_logs_slippage_incident_and_edge_uses_entry_basis():
    # pétrole : basis EMA +0,30 (HL au-dessus de Var) ; la décision voit ask Var 97.30 + 0.30
    e, t, reqs = mk(var=VarAt(97.42))
    q0 = VarQuote(97.20, 97.30, 5, ts=999.9)
    await rq(e, 97.0, 97.80, q0, basis=0.30)
    t["v"] += 0.3
    q1 = VarQuote(97.20, 97.30, 5, ts=1000.2)
    await rq(e, 97.0, 97.80, q1, basis=0.30)
    o = e.orders["sell"]
    assert o is not None and o["px"] == 97.80
    t["v"] += 0.1
    await e.on_fill({"coin": "xyz:SILVER", "side": "A", "px": 97.80, "sz": 5.0, "oid": o["oid"],
                     "fee": 0, "tid": 1, "time": 1})
    arb = e.journal.arbs[-1] if hasattr(e.journal, "arbs") else e.journal._inner.arbs[-1]
    edge, pnl = arb[8], arb[9]
    expect = edge_bps(False, 97.80, 97.42 + 0.30, 0.3)
    assert edge == pytest.approx(expect) and pnl == pytest.approx(expect / 1e4 * 5.0 * 97.80)
    incs = [i for i in e.journal._inner.incidents if i[1] == "info"]
    assert len(incs) == 1
    msg = incs[0][2]
    assert "âge cotation 0.10 s" in msg
    assert "glissement Var +12.3 pb" in msg                # (97.42 - 97.30) / 97.30
    assert "latence fill-couverture" in msg
    assert f"écart brut {edge_bps(False, 97.80, 97.42, 0.3):+.1f} pb" in msg
    assert "basis +0.3000" in msg


async def test_buy_side_slippage_sign():
    e, t, reqs = mk(var=VarAt(66.95))
    await rq(e, 66.85, 67.30, Q(999.9))
    t["v"] += 0.2
    await rq(e, 66.85, 67.30, Q(1000.1))
    o = e.orders["buy"]
    assert o is not None
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.85, "sz": 5.0, "oid": o["oid"],
                     "fee": 0, "tid": 1, "time": 1})
    msg = [i for i in e.journal._inner.incidents if i[1] == "info"][0][2]
    assert "glissement Var +7.5 pb" in msg                 # vente Var 66.95 contre bid 67.00


async def test_maker_hedge_unchanged_no_info_incident():
    m = dataclasses.replace(M, entry_mode="maker")
    e, t, reqs = mk(m=m, var=VarAt(67.10))
    await rq(e, 66.9, 67.2, Q(1000.0), basis=0.5)
    oid = e.orders["sell"]["oid"]
    await e.on_fill({"coin": "xyz:SILVER", "side": "A", "px": e.orders["sell"]["px"], "sz": 5.0,
                     "oid": oid, "fee": 0, "tid": 1, "time": 1})
    arb = e.journal._inner.arbs[-1]
    assert arb[8] == pytest.approx(edge_bps(False, arb[2], 67.10, 0.3))
    assert [i for i in e.journal._inner.incidents if i[1] == "info"] == []
