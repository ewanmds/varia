import asyncio
import pytest
from arb.config import GlobalCfg, MarketCfg
from arb.engine import MarketEngine, HedgeGate, QUOTING, PAUSED, BLOCKED, HEDGING, EXITING
from arb.pricing import VarQuote
from arb.var_client import VarRateLimited, VarRejected

G = GlobalCfg(hedge_retry_s=1.0)
M = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", fill_usd=1000, min_hedge_usd=20, entry_mode="maker")


class FakeHL:
    def __init__(self, ioc_fill_ratio=1.0):
        self.placed = []; self.cancelled = []; self.iocs = []; self.oid = 100
        self.ioc_fill_ratio = ioc_fill_ratio
        self.pos = {}            # vague finale I3a : positions HL relues par flatten()
        self.reduce_only = []

    async def positions_detail(self):
        return {c: {"szi": v, "entry": None} for c, v in self.pos.items()}

    async def place_alo(self, coin, is_buy, sz, px):
        self.oid += 1; self.placed.append((coin, is_buy, sz, px, self.oid))
        return {"oid": self.oid, "filled_sz": 0.0, "avg_px": None, "error": None}

    async def cancel(self, coin, oid): self.cancelled.append(oid); return True

    async def ioc(self, coin, is_buy, sz, px, reduce_only=False):
        self.iocs.append((coin, is_buy, sz, px))
        self.reduce_only.append(reduce_only)
        filled = sz * self.ioc_fill_ratio
        return {"oid": 1, "filled_sz": filled, "avg_px": px, "error": None}


class FakeVar:
    def __init__(self, fail=0, exc=None, positions=None):
        self.fail = fail; self.exc = exc; self.orders = []
        self.market_calls = 0
        self._positions = positions if positions is not None else {}

    async def quote(self, asset, kind, qty, prio=2, **kw):
        return VarQuote(67.05, 67.10, qty, ts=0, quote_id="q")

    async def market(self, quote_id, side, max_slippage, reduce_only=False, prio=0, **kw):
        self.market_calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise self.exc or VarRejected(422, "no")
        self.orders.append((side, reduce_only)); return "r1"

    async def order_status(self, rfq_id, prio=3, **kw):
        return {"status": "cleared", "price": "67.05", "qty": "15"}

    async def positions(self, **kw):
        return self._positions


class FakeJournal:
    def __init__(self): self.arbs = []; self.incidents = []
    def record_arb(self, *a): self.arbs.append(a); return 1
    def record_incident(self, *a): self.incidents.append(a)


def eng(var=None, hl=None, m=M, g=G):
    return MarketEngine(m, g, hl or FakeHL(), var or FakeVar(), FakeJournal(), HedgeGate(), sz_decimals=2, fee_bps=0.3)


Q = VarQuote(67.00, 67.10, 15, ts=0)


async def test_requote_places_both_sides():
    e = eng()
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    assert len(e.hl.placed) == 2 and e.orders["buy"] and e.orders["sell"]
    # même cotation : pas de repositionnement
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    assert len(e.hl.placed) == 2


async def test_block_reason_cancels_orders():
    e = eng()
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, "cotation Var périmée")
    assert len(e.hl.cancelled) == 2 and e.orders["buy"] is None and e.last_reason == "cotation Var périmée"


async def test_fill_triggers_hedge():
    e = eng()
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    buy_oid = e.orders["buy"]["oid"]
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0, "oid": buy_oid, "fee": 0.01, "tid": 1, "time": 1})
    assert e.var.orders == [("sell", False)]
    assert e.pos_hl == 15.0 and e.pos_var == -15.0 and e.pending_qty == 0
    assert e.state == QUOTING
    assert e.journal.arbs[0][-1] == "hedged"


async def test_unknown_oid_ignored():
    e = eng()
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0, "oid": 999, "fee": 0, "tid": 1, "time": 1})
    assert e.pos_hl == 0 and e.var.orders == []


async def test_hedge_retries_then_succeeds():
    e = eng(FakeVar(fail=1))
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0, "oid": e.orders["buy"]["oid"], "fee": 0, "tid": 1, "time": 1})
    assert e.var.orders == [("sell", False)] and e.state == QUOTING


async def test_hedge_failure_unwinds_hl_and_pauses():
    e = eng(FakeVar(fail=1000))
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0, "oid": e.orders["buy"]["oid"], "fee": 0, "tid": 1, "time": 1})
    assert e.hl.iocs and e.hl.iocs[0][1] is False     # vente IOC pour déboucler
    # Fix round 3 (spec §4.5) : un débouclage HL complet et sans erreur IOC finit en PAUSE (pas
    # BLOQUÉ, réservé au débouclage partiel/en erreur — voir test_i2_partial_unwind_...).
    assert e.pos_hl == 0 and e.state == PAUSED
    assert e.journal.arbs[0][-1] == "unwound" and e.journal.incidents


async def test_small_partial_fill_accumulates():
    e = eng()
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    oid = e.orders["buy"]["oid"]
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 0.1, "oid": oid, "fee": 0, "tid": 1, "time": 1})
    assert e.var.orders == [] and e.pending_qty == pytest.approx(0.1)


async def test_flatten_taker_closes_both_legs():
    e = eng()
    # vague finale I3a : flatten() relit les positions réelles (plus les valeurs en cache).
    e.hl.pos = {"xyz:SILVER": 15.0}
    e.var._positions = {"XAGS": {"qty": -15.0}}
    await e.flatten(True, 66.9, 67.2)
    assert e.hl.iocs[-1][1] is False and e.hl.iocs[-1][2] == 15.0   # vente IOC
    assert e.var.orders[-1] == ("buy", True)                          # rachat Var reduce-only
    assert e.pos_hl == 0 and e.pos_var == 0


# ---------------------------------------------------------------------------
# Fix round 1 : findings task-10-findings-r1.md
# ---------------------------------------------------------------------------

class FlakyStatusVar(FakeVar):
    """order_status renvoie None N fois puis "cleared"."""
    def __init__(self, none_count):
        super().__init__()
        self.none_count = none_count
        self.status_calls = 0

    async def order_status(self, rfq_id, prio=3, **kw):
        self.status_calls += 1
        if self.none_count > 0:
            self.none_count -= 1
            return None
        return {"status": "cleared", "price": "67.05", "qty": "15"}


async def test_c1_order_status_none_then_cleared_single_market_call():
    e = eng(FlakyStatusVar(none_count=2), g=GlobalCfg(hedge_retry_s=3.0))
    e._confirm_window_s = 10.0   # ne pas interférer : ce test cible uniquement le statut None
    e._status_poll_s = 0.5       # vague finale I6 : sondage à 1 s par défaut, 3 sondages ici
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                     "oid": e.orders["buy"]["oid"], "fee": 0, "tid": 1, "time": 1})
    assert e.var.market_calls == 1
    assert e.var.orders == [("sell", False)]
    assert e.state == QUOTING


class UnknownStatusPositionsConfirmVar(FakeVar):
    """order_status ne se résout jamais ; positions() montre le remplissage attendu."""
    def __init__(self):
        super().__init__(positions={"XAGS": {"qty": 0.0}})
        self._confirmed = False

    async def order_status(self, rfq_id, prio=3, **kw):
        return {"status": "pending"}

    async def positions(self, **kw):
        if self.market_calls:
            self._positions = {"XAGS": {"qty": -15.0}}
        return self._positions


async def test_c1_unknown_status_confirmed_via_positions_no_unwind():
    e = eng(UnknownStatusPositionsConfirmVar())
    e._confirm_window_s = 0.1
    e.g = GlobalCfg(hedge_retry_s=1.0)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                     "oid": e.orders["buy"]["oid"], "fee": 0, "tid": 1, "time": 1})
    assert e.var.market_calls == 1
    assert not e.hl.iocs           # aucun débouclage
    assert e.pos_var == -15.0 and e.pending_qty == 0
    assert e.state == QUOTING
    assert e.journal.arbs[0][-1] == "hedged_px_quote"


class GatedVar(FakeVar):
    """market() attend un asyncio.Event avant de répondre, pour simuler une couverture longue
    pendant laquelle un deuxième fill peut arriver."""
    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def market(self, quote_id, side, max_slippage, reduce_only=False, prio=0, **kw):
        self.entered.set()
        await self.gate.wait()
        return await super().market(quote_id, side, max_slippage, reduce_only=reduce_only, prio=prio)


async def test_c2_concurrent_fill_during_hedge_both_covered():
    var = GatedVar()
    e = eng(var)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    oid = e.orders["buy"]["oid"]

    async def first_fill():
        await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                         "oid": oid, "fee": 0, "tid": 1, "time": 1})

    t1 = asyncio.create_task(first_fill())
    await var.entered.wait()   # la 1ère couverture tient le verrou, en attente dans market()

    # 2e fill traité en tâche concurrente pendant que hedge() du 1er tient le verrou (C2) : on ne
    # l'attend pas directement (son propre hedge() va bloquer sur le même verrou), on le laisse
    # juste démarrer et se mettre en attente du verrou avant de libérer la 1ère couverture.
    t2 = asyncio.create_task(e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.96, "sz": 5.0,
                                        "oid": oid, "fee": 0, "tid": 2, "time": 2}))
    await asyncio.sleep(0)
    var.gate.set()              # libérer la 1ère couverture
    await asyncio.gather(t1, t2)

    assert e.pos_hl == 20.0
    assert e.pos_var == -20.0
    assert e.pending_qty == 0
    assert var.market_calls == 2          # une couverture par lot de fill, aucune perte


async def test_c3_fill_on_cancelled_order_still_covered():
    e = eng()
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    oid = e.orders["buy"]["oid"]
    await e._cancel_side("buy")
    assert e.orders["buy"] is None
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                     "oid": oid, "fee": 0, "tid": 1, "time": 1})
    assert e.pos_hl == 15.0
    assert e.var.orders == [("sell", False)]
    assert e.pending_qty == 0


async def test_c4_unwind_rounds_price_and_size():
    m = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", fill_usd=1000, min_hedge_usd=20, entry_mode="maker")
    hl = FakeHL()
    e = eng(FakeVar(fail=1000), hl=hl, m=m)
    e.book = (66.987654, 67.223456)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.951234, "sz": 15.0,
                     "oid": e.orders["buy"]["oid"], "fee": 0, "tid": 1, "time": 1})
    assert hl.iocs
    _, is_buy, sz, px = hl.iocs[0]
    assert is_buy is False
    # sz_decimals=2 -> au plus 2 décimales pour la taille
    assert round(sz, 2) == sz
    # au plus 6 - sz_decimals = 4 décimales pour le prix (et 5 chiffres significatifs)
    assert round(px, 4) == px


async def test_i2_partial_unwind_keeps_residual_and_blocks():
    hl = FakeHL(ioc_fill_ratio=0.4)   # l'IOC ne remplit que 40% de la taille demandée
    e = eng(FakeVar(fail=1000), hl=hl)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                     "oid": e.orders["buy"]["oid"], "fee": 0, "tid": 1, "time": 1})
    # Fix round 2 (I2) : un débouclage partiel appelle block(), pas pause().
    assert e.state == BLOCKED
    assert e.pending_qty == pytest.approx(9.0)   # 15 - 0.4*15 = 9 non débouclé
    assert e.journal.incidents


class AlwaysRaisesQuoteVar(FakeVar):
    async def quote(self, asset, kind, qty, prio=2, **kw):
        raise RuntimeError("boom playwright")


async def test_c5_non_var_error_in_quote_unwinds_and_clears_inflight():
    e = eng(AlwaysRaisesQuoteVar())
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                     "oid": e.orders["buy"]["oid"], "fee": 0, "tid": 1, "time": 1})
    assert e.hl.iocs
    # Fix round 3 (spec §4.5) : débouclage complet sans erreur IOC -> PAUSE.
    assert e.state == PAUSED
    assert e.hedge_inflight is False


async def test_i1_pause_cancels_hl_orders():
    e = eng()
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    assert e.orders["buy"] and e.orders["sell"]
    await e.pause("test manuel")
    assert e.state == PAUSED and e.last_reason == "test manuel"
    assert len(e.hl.cancelled) == 2
    assert e.orders["buy"] is None and e.orders["sell"] is None


async def test_i1_fill_during_pause_hedged_but_state_stays_paused():
    e = eng()
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    oid = e.orders["buy"]["oid"]
    await e.pause("maintenance")
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                     "oid": oid, "fee": 0, "tid": 1, "time": 1})
    assert e.pos_var == -15.0 and e.pending_qty == 0   # couvert malgré la pause
    assert e.state == PAUSED                            # jamais remis à QUOTING automatiquement


async def test_i4_flatten_waits_for_inflight_hedge():
    var = GatedVar()
    e = eng(var)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    oid = e.orders["buy"]["oid"]
    order = []

    async def do_hedge():
        await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                         "oid": oid, "fee": 0, "tid": 1, "time": 1})
        order.append("hedge_done")

    async def do_flatten():
        await var.entered.wait()   # la couverture est en vol, verrou pris
        await asyncio.sleep(0.05)
        assert "hedge_done" not in order   # la couverture n'est pas encore terminée
        var.gate.set()              # laisser la couverture se terminer
        await e.flatten(False, 66.9, 67.2)   # doit attendre la libération du verrou
        order.append("flatten_started")

    t1 = asyncio.create_task(do_hedge())
    t2 = asyncio.create_task(do_flatten())
    await asyncio.gather(t1, t2)
    assert order[0] == "hedge_done"
    assert order[-1] == "flatten_started"


# ---------------------------------------------------------------------------
# Fix round 2 : findings task-10-findings-r2.md
# ---------------------------------------------------------------------------

class FinalReadOnlyConfirmVar(FakeVar):
    """order_status ne se résout jamais ; positions() ne montre le remplissage qu'à la lecture
    finale (post-boucle), pas pendant la fenêtre de confirmation en boucle."""
    def __init__(self):
        super().__init__()
        self.positions_calls = 0

    async def order_status(self, rfq_id, prio=3, **kw):
        return {"status": "pending"}

    async def positions(self, **kw):
        self.positions_calls += 1
        if self.positions_calls == 1:
            return {"XAGS": {"qty": 0.0}}     # baseline, lue juste avant market()
        return {"XAGS": {"qty": -15.0}}       # lecture finale : le fill est bien passé


async def test_c1a_final_positions_read_confirms_hedge_no_unwind():
    e = eng(FinalReadOnlyConfirmVar(), g=GlobalCfg(hedge_retry_s=1.5))
    e._confirm_window_s = 100.0     # ne jamais confirmer PENDANT la boucle : seule la lecture
                                     # finale (après réservation du budget) doit le faire (C1a)
    e._final_read_reserve_s = 0.3
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                     "oid": e.orders["buy"]["oid"], "fee": 0, "tid": 1, "time": 1})
    assert not e.hl.iocs                # aucun débouclage
    assert e.pos_var == -15.0 and e.pending_qty == 0
    assert e.state == QUOTING
    assert e.journal.arbs[0][-1] == "hedged_px_quote"
    assert e.var.market_calls == 1


class FinalReadFailsVar(FakeVar):
    """baseline OK, mais toute lecture ultérieure des positions échoue (y compris la finale)."""
    def __init__(self):
        super().__init__()
        self.positions_calls = 0

    async def order_status(self, rfq_id, prio=3, **kw):
        return {"status": "pending"}

    async def positions(self, **kw):
        self.positions_calls += 1
        if self.positions_calls == 1:
            return {"XAGS": {"qty": 0.0}}
        raise RuntimeError("positions API down")


async def test_c1a_final_read_failure_blocks_without_unwind():
    e = eng(FinalReadFailsVar(), g=GlobalCfg(hedge_retry_s=1.5))
    e._confirm_window_s = 100.0
    e._final_read_reserve_s = 0.3
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                     "oid": e.orders["buy"]["oid"], "fee": 0, "tid": 1, "time": 1})
    assert not e.hl.iocs                 # pas de débouclage à l'aveugle, incertitude != échec
    assert e.state == BLOCKED
    assert e.pending_qty == 15.0          # ni couvert ni débouclé, laissé pour la main/réconciliation
    assert e.journal.incidents and "incertaine" in e.journal.incidents[-1][2]


class BaselineAlwaysFailsVar(FakeVar):
    async def positions(self, **kw):
        raise RuntimeError("positions API down")


async def test_c1b_baseline_read_fails_twice_no_market_unwind():
    e = eng(BaselineAlwaysFailsVar(), g=G)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                     "oid": e.orders["buy"]["oid"], "fee": 0, "tid": 1, "time": 1})
    assert e.var.market_calls == 0        # jamais envoyé sans base pour confirmer plus tard
    assert e.hl.iocs and e.hl.iocs[0][1] is False
    assert e.pos_hl == 0
    # Fix round 3 (spec §4.5) : débouclage complet sans erreur IOC -> PAUSE.
    assert e.state == PAUSED


class RaisingCancelHL(FakeHL):
    def __init__(self):
        super().__init__()
        self.fail_oid = None

    async def cancel(self, coin, oid):
        self.cancelled.append(oid)
        if oid == self.fail_oid:
            raise RuntimeError("boom cancel")
        return True


async def test_n2_cancel_all_raises_still_hedges_and_clears_inflight():
    hl = RaisingCancelHL()
    e = eng(hl=hl)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    buy_oid = e.orders["buy"]["oid"]
    hl.fail_oid = buy_oid   # l'ordre buy reste actif après un fill partiel : son annulation,
                            # au tout début de hedge(), va lever.
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 5.0,
                     "oid": buy_oid, "fee": 0, "tid": 1, "time": 1})
    assert e.pos_var == -5.0
    assert e.pending_qty == 0
    assert e.hedge_inflight is False
    assert e.journal.arbs[-1][-1] == "hedged"
    assert any("annulation HL avant couverture échouée" in i[2] for i in e.journal.incidents)


async def test_n3_fill_during_exiting_state_preserved():
    e = eng()
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    oid = e.orders["buy"]["oid"]
    e.state = EXITING
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                     "oid": oid, "fee": 0, "tid": 1, "time": 1})
    assert e.pos_var == -15.0 and e.pending_qty == 0   # couvert malgré l'état
    assert e.state == EXITING                           # jamais remis à QUOTING


async def test_orphan_fill_replayed_when_oid_registered():
    e = eng()
    # fill qui arrive AVANT que l'oid soit connu du moteur (websocket plus rapide que la réponse
    # HTTP de place_alo()).
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                     "oid": 555, "fee": 0, "tid": 1, "time": 1})
    assert e.pos_hl == 0   # mis en tampon, pas encore appliqué
    e.hl.oid = 554         # le prochain oid généré par FakeHL sera 555
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    assert 555 in e._my_oids
    # le fill (15) dépasse la taille de l'ordre placé (~14.91) : il a été rejoué et a soldé
    # l'ordre buy (orders["buy"] redevient None, comme pour tout fill qui l'épuise).
    assert e.orders["buy"] is None
    assert e.pos_hl == 15.0          # le fill orphelin a été rejoué dès l'enregistrement de l'oid
    assert e.var.orders == [("sell", False)]
    assert e.pending_qty == 0


class GatedVar2(FakeVar):
    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def market(self, quote_id, side, max_slippage, reduce_only=False, prio=0, **kw):
        self.entered.set()
        await self.gate.wait()
        return await super().market(quote_id, side, max_slippage, reduce_only=reduce_only, prio=prio)


async def test_n4_residual_fill_during_hedge_keeps_own_price():
    var = GatedVar2()
    e = eng(var)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    oid = e.orders["buy"]["oid"]

    async def first_fill():
        await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                         "oid": oid, "fee": 0, "tid": 1, "time": 1})

    t1 = asyncio.create_task(first_fill())
    await var.entered.wait()   # le lot de 15 @ 66.95 est capturé par hedge(), verrou tenu

    t2 = asyncio.create_task(e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 70.0, "sz": 5.0,
                                        "oid": oid, "fee": 0, "tid": 2, "time": 2}))
    await asyncio.sleep(0)      # laisser t2 tourner jusqu'à son blocage sur le verrou de hedge()
    # le résidu (5 @ 70.0) doit garder SON PROPRE prix, pas une moyenne avec le lot déjà capturé.
    assert e.pending_qty == pytest.approx(20.0)
    assert e._pending_px == pytest.approx(70.0)
    var.gate.set()
    await asyncio.gather(t1, t2)
    assert e.pending_qty == 0


class RateLimitedThenClearVar(FakeVar):
    def __init__(self):
        super().__init__()
        self.status_calls = 0

    async def order_status(self, rfq_id, prio=3, **kw):
        self.status_calls += 1
        if self.status_calls == 1:
            raise VarRateLimited(0.05)
        return {"status": "cleared", "price": "67.05"}


async def test_n5_flatten_survives_rate_limit_during_confirmation():
    var = RateLimitedThenClearVar()
    e = eng(var)
    e.hl.pos = {"xyz:SILVER": 15.0}                 # vague finale I3a
    var._positions = {"XAGS": {"qty": -15.0}}
    await e.flatten(True, 66.9, 67.2)
    assert e.pos_var == 0.0
    assert var.status_calls >= 2


# ---------------------------------------------------------------------------
# Fix round 3 : findings task-10-findings-r3.md
# ---------------------------------------------------------------------------

async def test_r3_1_clean_full_unwind_ends_in_pause():
    # Débouclage HL complet (FakeHL remplit tout) et sans erreur IOC : doit finir en PAUSE
    # (spec §4.5), pas BLOQUÉ (réservé au débouclage partiel ou en erreur).
    e = eng(FakeVar(fail=1000))
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                     "oid": e.orders["buy"]["oid"], "fee": 0, "tid": 1, "time": 1})
    assert e.hl.iocs and e.hl.iocs[0][1] is False
    assert e.pos_hl == 0
    assert e.state == PAUSED
    assert e.journal.arbs[0][-1] == "unwound"
    # un seul incident (pas de doublon "couverture échouée" + "bloqué") puisque pause() ne
    # journalise pas d'incident : la trace de l'unwind reste unique.
    assert len(e.journal.incidents) == 1


class RaisingCancelOtherHL(FakeHL):
    def __init__(self):
        super().__init__()
        self.fail_oid = None

    async def cancel(self, coin, oid):
        self.cancelled.append(oid)
        if oid == self.fail_oid:
            raise RuntimeError("boom cancel other side")
        return True


async def test_r3_3_cancel_other_side_raises_hedge_still_happens():
    hl = RaisingCancelOtherHL()
    e = eng(hl=hl)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    buy_oid = e.orders["buy"]["oid"]
    sell_oid = e.orders["sell"]["oid"]
    hl.fail_oid = sell_oid   # l'annulation du côté "sell", déclenchée par le fill buy, lève
    await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                     "oid": buy_oid, "fee": 0, "tid": 1, "time": 1})
    assert e.pos_var == -15.0
    assert e.pending_qty == 0
    assert e.state == QUOTING
    assert any("annulation de l'autre côté HL échouée" in i[2] for i in e.journal.incidents)


async def test_r3_4_external_pause_during_inflight_hedge_preserved():
    var = GatedVar()
    e = eng(var)
    await e.requote(Q, 0.0, 66.9, 67.2, 1.0, None)
    oid = e.orders["buy"]["oid"]

    async def do_hedge():
        await e.on_fill({"coin": "xyz:SILVER", "side": "B", "px": 66.95, "sz": 15.0,
                         "oid": oid, "fee": 0, "tid": 1, "time": 1})

    t1 = asyncio.create_task(do_hedge())
    await var.entered.wait()   # la couverture est en vol (verrou tenu, market() suspendu)
    await e.pause("intervention externe")
    assert e.state == PAUSED
    var.gate.set()             # laisser la couverture se terminer (avec succès)
    await t1

    assert e.pos_var == -15.0 and e.pending_qty == 0   # la couverture a bien eu lieu
    assert e.state == PAUSED             # ... mais l'état externe n'a pas été écrasé au succès
    assert e.last_reason == "intervention externe"


class RaisesNonVarErrorVar(FakeVar):
    async def quote(self, asset, kind, qty, prio=2, **kw):
        raise RuntimeError("boom playwright pendant la sortie")


async def test_r3_5_flatten_non_var_error_incident_not_raised():
    e = eng(RaisesNonVarErrorVar())
    e.hl.pos = {"xyz:SILVER": 15.0}                 # vague finale I3a
    e.var._positions = {"XAGS": {"qty": -15.0}}
    await e.flatten(True, 66.9, 67.2)   # ne doit lever aucune exception
    assert any("sortie Var échouée" in i[2] for i in e.journal.incidents)
    # vague finale I3c : Var se ferme d'abord ; sa sortie ayant échoué, la jambe HL couverte
    # n'est PAS fermée (on ne laisse jamais Var nu).
    assert e.pos_hl == 15.0 and e.hl.iocs == []
