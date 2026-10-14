import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from arb.config import GlobalCfg, MarketCfg
from arb.engine import BLOCKED, EXITING, IDLE, PAUSED, QUOTING
from arb.orchestrator import Orchestrator
from arb.pricing import VarQuote, round_px, round_sz
from arb.hl_client import CoinMeta

M = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER", entry_mode="maker")
M2 = MarketCfg(var_asset="GOLD", var_kind="commodity", hl_coin="xyz:GOLD", entry_mode="maker")


class HLC:
    paper = True

    def __init__(self):
        self.meta = {}
        self.pos = {}
        self.iocs = []
        self.cancel_all_calls = []

    def load_meta(self):
        self.meta = {"xyz:SILVER": CoinMeta(2, 25, True)}
        return self.meta

    def maker_fee_bps(self, coin):
        return 0.3

    def mids(self):
        return {"xyz:SILVER": 67.05}

    def positions(self):
        return dict(self.pos)

    def positions_detail(self):
        return {c: {"szi": v, "entry": None} for c, v in self.pos.items()}

    def margin_ratio(self):
        return 0.1

    def rate_limit(self):
        return {"used": 0, "cap": 100000, "surplus": 0, "cum_vlm": 0.0, "headroom": 100000}

    def schedule_cancel(self, when_ms):
        self.schedule_calls = getattr(self, "schedule_calls", []) + [when_ms]
        return True

    def place_alo(self, *a):
        return {"oid": 1, "filled_sz": 0, "avg_px": None, "error": None}

    def cancel(self, *a):
        return True

    def cancel_all(self, coins):
        self.cancel_all_calls.append(list(coins))

    def ioc(self, coin, is_buy, sz, px, reduce_only=False):
        self.iocs.append((coin, is_buy, sz, px))
        self.reduce_only = getattr(self, "reduce_only", []) + [reduce_only]
        return {"oid": 2, "filled_sz": sz, "avg_px": px, "error": None}

    def set_cross_leverage(self, coin, lev):
        pass


class VC:
    def __init__(self):
        self.pos = {}
        self.lev = []
        self.market_calls = []
        self.limiter = type("L", (), {"strikes_recent": lambda s: 0, "quotes_suspended": lambda s: False})()

    async def supported_assets(self):
        return {"XAGS": [{"price": "67.08", "trading_sessions": [
            {"open": "2026-09-21T22:00:00Z", "close": "2026-09-22T21:00:00Z"}]}]}

    async def set_leverage(self, a, l):
        self.lev.append((a, l))

    async def positions(self):
        return dict(self.pos)

    async def portfolio(self):
        return {"balance": 500.0, "upnl": 0.0}

    async def health(self):
        return True

    async def quote(self, *a, **k):
        return VarQuote(67.0, 67.1, 15, ts=0, quote_id="q")

    async def market(self, *a, **k):
        self.market_calls.append((a, k))
        return "r1"


class HLC2(HLC):
    """Variante à deux coins (XAGS/xyz:SILVER + GOLD/xyz:GOLD)."""

    def load_meta(self):
        self.meta = {"xyz:SILVER": CoinMeta(2, 25, True), "xyz:GOLD": CoinMeta(2, 25, True)}
        return self.meta

    def mids(self):
        return {"xyz:SILVER": 67.05, "xyz:GOLD": 2000.5}


class VC2(VC):
    """Variante à deux marchés (XAGS + GOLD)."""

    async def supported_assets(self):
        sess = [{"open": "2026-09-21T22:00:00Z", "close": "2026-09-22T21:00:00Z"}]
        return {"XAGS": [{"price": "67.08", "trading_sessions": sess}],
                "GOLD": [{"price": "2000.5", "trading_sessions": sess}]}


class J:
    def __init__(self):
        self.inc = []

    def record_incident(self, *a):
        self.inc.append(a)

    def record_arb(self, *a):
        return 1

    def daily_pnl(self, day=None):
        return 0.0


class FakeHLFeed:
    """Simule le carnet HL fourni par HLFeed : bid/ask fixes pour un coin donné.

    `stalled` (rétro-compat, amendement t14 fix) simule le silence GLOBAL : traduit en un
    `last_any_msg()` très ancien (silence global détecté quel que soit `hl_ws_stall_s`) au lieu
    d'un horodatage récent. `book_age` simule l'âge du carnet PAR MARCHÉ, indépendant du silence
    global, pour le nouveau garde-fou "carnet HL périmé".
    """

    def __init__(self, book=(66.9, 67.2), mid=67.05, stalled=False, fills_alive=True, book_age=0.0):
        self._book = book
        self._mid = mid
        self._stalled = stalled
        self._fills_alive = fills_alive
        self._book_age = book_age

    def book(self, coin):
        return self._book

    def mid(self, coin):
        if isinstance(self._mid, dict):
            return self._mid.get(coin, 0.0)
        return self._mid

    def fills_alive(self, max_age_s=30.0):
        return self._fills_alive

    def last_any_msg(self):
        return time.time() - (1e6 if self._stalled else 0.0)

    def book_age(self, coin):
        if isinstance(self._book_age, dict):
            return self._book_age.get(coin, 0.0)
        return self._book_age


async def test_setup_sets_leverage_and_refs():
    o = Orchestrator(GlobalCfg(), [M], HLC(), VC(), J(),
                     clock=lambda: datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp())
    await o.setup()
    assert o.var.lev == [("XAGS", 5)]   # L1 : levier Var par défaut 5 (borné au maximum de l'actif)
    assert o.engines["XAGS"].sz_decimals == 2 and o.engines["XAGS"].fee_bps == 0.3
    assert o.feed.qty_for("XAGS") is not None


async def test_unit_mismatch_disables_market():
    h = HLC()
    h.mids = lambda: {"xyz:SILVER": 670.5}
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: 0)
    await o.setup()
    assert o.engines["XAGS"].state == BLOCKED


async def test_setup_loads_existing_positions():
    h = HLC()
    h.pos = {"xyz:SILVER": 15.0}
    v = VC()
    v.pos = {"XAGS": {"qty": -15.0, "entry": 67, "liq": None, "value": -1005}}
    o = Orchestrator(GlobalCfg(), [M], h, v, J(), clock=lambda: 0)
    await o.setup()
    assert o.engines["XAGS"].pos_hl == 15.0 and o.engines["XAGS"].pos_var == -15.0


# -- amendement 8 : tests supplémentaires ------------------------------------------

async def test_tick_updates_engine_book():
    o = Orchestrator(GlobalCfg(), [M], HLC(), VC(), J(), clock=lambda: 0)
    await o.setup()
    o.hl_feed = FakeHLFeed(book=(66.9, 67.2))
    # marché fermé (aucune session à clock()=0) : tick() doit tout de même poser eng.book avant
    # de traiter la phase.
    await o.tick()
    assert o.engines["XAGS"].book == (66.9, 67.2)


async def test_reconcile_does_not_overwrite_positions_while_hedge_inflight():
    h = HLC()
    h.pos = {"xyz:SILVER": 999.0}
    v = VC()
    v.pos = {"XAGS": {"qty": -999.0, "entry": 67, "liq": None, "value": 0}}
    o = Orchestrator(GlobalCfg(), [M], h, v, J(), clock=lambda: 0)
    await o.setup()
    eng = o.engines["XAGS"]
    eng.pos_hl, eng.pos_var = 5.0, -5.0
    eng.hedge_inflight = True
    o.hl_feed = FakeHLFeed()
    await o.reconcile()
    assert eng.pos_hl == 5.0 and eng.pos_var == -5.0


async def test_reconcile_hl_correction_sends_rounded_price_and_size():
    h = HLC()
    h.pos = {"xyz:SILVER": 20.0}
    v = VC()
    v.pos = {"XAGS": {"qty": -15.0, "entry": 67, "liq": None, "value": -1005}}
    o = Orchestrator(GlobalCfg(), [M], h, v, J(), clock=lambda: 0)
    await o.setup()
    eng = o.engines["XAGS"]
    # vague finale I5 : ce delta existant au démarrage bloque le marché ; l'opérateur débloque.
    assert eng.state == BLOCKED
    eng.resume()
    eng.book = (66.9, 67.2)
    o.hl_feed = FakeHLFeed(book=(66.9, 67.2), mid=67.0)
    # premier appel : delta observé, encore "wait" (il faut deux lectures cohérentes) ;
    # deuxième appel : "correct" -> correction HL (jambe HL, 20, plus grande en valeur absolue
    # que la jambe Var, 15).
    await o.reconcile()
    await o.reconcile()
    assert len(h.iocs) == 1
    coin, is_buy, sz, px = h.iocs[0]
    assert coin == "xyz:SILVER"
    # real_hl = 20 > 0 -> on vend pour réduire -> is_buy = False côté IOC.
    assert is_buy is False
    expected_sz = round_sz(abs(20.0 - 15.0), eng.sz_decimals)
    expected_px = round_px(66.9 * 0.995, eng.sz_decimals, False)
    assert sz == expected_sz
    assert px == expected_px


async def test_kill_flattens_every_market_and_stops_engines():
    h = HLC()
    h.pos = {"xyz:SILVER": 3.0}
    v = VC()
    v.pos = {"XAGS": {"qty": -3.0, "entry": 67, "liq": None, "value": -201}}
    o = Orchestrator(GlobalCfg(), [M], h, v, J(), clock=lambda: 0)
    await o.setup()
    eng = o.engines["XAGS"]
    eng.flatten = AsyncMock(wraps=eng.flatten)
    await o.kill("test manuel")
    eng.flatten.assert_awaited_once()
    args, kwargs = eng.flatten.call_args
    assert args[0] is True  # taker=True
    assert o.killed is True
    assert o.kill_reason == "test manuel"
    assert eng.state == IDLE


# -- fix round 1 : tests exigés par task-12-findings-r1.md -----------------------------

async def test_hl_silence_triggers_cancel_all_once_and_clears_orders():
    h = HLC()
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: 0)
    await o.setup()
    eng = o.engines["XAGS"]
    eng.orders["buy"] = {"oid": 5, "px": 66.0, "sz": 1.0}
    o.hl_feed = FakeHLFeed(stalled=True)
    await o.tick()
    await o.tick()
    assert len(h.cancel_all_calls) == 1
    assert eng.orders == {"buy": None, "sell": None}
    assert o._need_reconcile is True


async def test_idle_market_stays_idle_through_closing():
    t = {"v": datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp()}
    o = Orchestrator(GlobalCfg(), [M], HLC(), VC(), J(), clock=lambda: t["v"])
    await o.setup()
    eng = o.engines["XAGS"]
    eng.state = IDLE
    o.hl_feed = FakeHLFeed()
    t["v"] = datetime(2026, 9, 22, 20, 57, tzinfo=timezone.utc).timestamp()  # FLAT
    await o.tick()
    assert eng.state == IDLE
    t["v"] = datetime(2026, 9, 22, 21, 1, tzinfo=timezone.utc).timestamp()  # CLOSED (après clôture)
    await o.tick()
    assert eng.state == IDLE


async def test_paused_market_restored_after_closing():
    t = {"v": datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp()}
    o = Orchestrator(GlobalCfg(), [M], HLC(), VC(), J(), clock=lambda: t["v"])
    await o.setup()
    eng = o.engines["XAGS"]
    eng.state = PAUSED
    o.hl_feed = FakeHLFeed()
    t["v"] = datetime(2026, 9, 22, 20, 57, tzinfo=timezone.utc).timestamp()  # FLAT -> flatten taker
    await o.tick()
    assert eng.state == EXITING
    t["v"] = datetime(2026, 9, 22, 21, 1, tzinfo=timezone.utc).timestamp()  # CLOSED -> restauration
    await o.tick()
    assert eng.state == PAUSED


async def test_session_dead_no_taker_flatten_in_closing_taker():
    h = HLC()
    now_ts = datetime(2026, 9, 22, 20, 53, tzinfo=timezone.utc).timestamp()  # CLOSING_TAKER (7 min avant clôture)
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: now_ts)
    await o.setup()
    eng = o.engines["XAGS"]
    o.session_ok = False
    o.hl_feed = FakeHLFeed()
    await o.tick()
    assert h.iocs == []
    assert eng.state == QUOTING
    assert any(inc[0] == "XAGS" and "session Var" in inc[2] for inc in o.journal.inc)


async def test_kill_continues_after_one_market_flatten_raises():
    h = HLC2()
    o = Orchestrator(GlobalCfg(), [M, M2], h, VC2(), J(), clock=lambda: 0)
    await o.setup()
    e1, e2 = o.engines["XAGS"], o.engines["GOLD"]

    async def boom(*a, **k):
        raise RuntimeError("boom")

    e1.flatten = boom
    e2.flatten = AsyncMock(wraps=e2.flatten)
    await o.kill("test")
    e2.flatten.assert_awaited_once()
    assert e1.state == IDLE
    assert e2.state == IDLE
    assert o.killed is True


async def test_exception_on_one_market_does_not_block_others():
    h = HLC2()
    o = Orchestrator(GlobalCfg(), [M, M2], h, VC2(), J(),
                     clock=lambda: datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp())
    await o.setup()
    e1, e2 = o.engines["XAGS"], o.engines["GOLD"]
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 15, ts=1, quote_id="q1")
    o.feed._quotes["GOLD"] = VarQuote(2000.0, 2001.0, 1, ts=1, quote_id="q2")
    o.hl_feed = FakeHLFeed(mid=67.05)
    e1.requote = AsyncMock(side_effect=RuntimeError("boom"))
    e2.requote = AsyncMock(wraps=e2.requote)
    await o.tick()
    e2.requote.assert_awaited_once()
    assert any(inc[0] == "XAGS" and "tick" in inc[2] for inc in o.journal.inc)


async def test_reconcile_skips_correction_for_blocked_market():
    h = HLC()
    h.pos = {"xyz:SILVER": 20.0}
    v = VC()
    v.pos = {"XAGS": {"qty": -15.0, "entry": 67, "liq": None, "value": -1005}}
    o = Orchestrator(GlobalCfg(), [M], h, v, J(), clock=lambda: 0)
    await o.setup()
    eng = o.engines["XAGS"]
    eng.book = (66.9, 67.2)
    eng.state = BLOCKED
    o.hl_feed = FakeHLFeed(book=(66.9, 67.2), mid=67.0)
    await o.reconcile()
    await o.reconcile()
    assert h.iocs == []
    assert eng.state == BLOCKED


async def test_health_check_exception_marks_session_not_ok():
    class VCBoom(VC):
        async def health(self):
            raise RuntimeError("down")

    o = Orchestrator(GlobalCfg(), [M], HLC(), VCBoom(), J(), clock=lambda: 0)
    await o.setup()
    assert o.session_ok is True
    await o._health_once()
    assert o.session_ok is False
    assert any(inc[1] == "warn" for inc in o.journal.inc)


# -- fix round 2 : tests exigés par task-12-findings-r2.md -----------------------------

async def test_silence_cancel_all_failure_retries_next_tick():
    class HLCBoom(HLC):
        def __init__(self):
            super().__init__()
            self.cancel_all_attempts = 0

        def cancel_all(self, coins):
            self.cancel_all_attempts += 1
            raise RuntimeError("exchange indisponible")

    h = HLCBoom()
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: 0)
    await o.setup()
    eng = o.engines["XAGS"]
    o.hl_feed = FakeHLFeed(stalled=True)
    o._global_cancel_retry_s = 0.0

    await o.tick()
    # l'annulation globale a échoué -> épisode NON marqué, pas de recotation suspendue à tort,
    # les ordres du moteur ne sont pas videts sur la base d'une annulation qui n'a pas eu lieu.
    assert o._hl_silence_active is False
    assert o._need_reconcile is False
    # L7 (23/09) : 1 essai + 3 nouveaux essais immédiats dans le même tick.
    assert h.cancel_all_attempts == 4

    await o.tick()
    # nouvel essai au tick suivant, tant que le silence persiste.
    assert h.cancel_all_attempts == 8
    assert eng.state != BLOCKED  # pas d'effet de bord : juste des tentatives d'annulation


async def test_reconcile_started_before_silence_end_does_not_clear_flag():
    h = HLC()
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: 0)
    await o.setup()
    o.hl_feed = FakeHLFeed(stalled=True)
    await o.tick()
    assert o._need_reconcile is True
    assert o._hl_silence_active is True

    # réconciliation qui se déroule PENDANT le silence : ne doit jamais lever le drapeau, même si
    # elle réussit (les positions lues sont considérées non fiables tant que le silence dure).
    await o.reconcile()
    assert o._need_reconcile is True

    # le flux redevient vivant : la fin de CET épisode est mémorisée (_silence_ended_at).
    o.hl_feed = FakeHLFeed(stalled=False, fills_alive=True)
    await o.tick()
    assert o._hl_silence_active is False

    # nouvelle réconciliation, commencée après la fin du silence : lève le drapeau.
    await o.reconcile()
    assert o._need_reconcile is False


async def test_run_shutdown_waits_for_fill_task_before_stopping_feed():
    class FakeFeed:
        def __init__(self):
            self.stop_calls = 0

        def start(self):
            pass

        def stop(self):
            self.stop_calls += 1

        def book(self, coin):
            return (66.9, 67.2)

        def mid(self, coin):
            return 67.05

        def fills_alive(self, max_age_s=30.0):
            return True

        def last_any_msg(self):
            return time.time()

        def book_age(self, coin):
            return 0.0

    h = HLC()
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: 0)
    await o.setup()
    fake_feed = FakeFeed()
    stop_event = asyncio.Event()
    fill_done = {"v": False}

    async def slow_fill_task():
        await asyncio.sleep(0.05)
        fill_done["v"] = True

    async def driver():
        await asyncio.sleep(0.01)
        t = asyncio.get_running_loop().create_task(slow_fill_task())
        o._fill_tasks.add(t)
        t.add_done_callback(o._fill_tasks.discard)
        await asyncio.sleep(0.01)
        stop_event.set()

    with patch("arb.orchestrator.HLFeed", return_value=fake_feed):
        await asyncio.gather(o.run(stop_event, "0xabc"), driver())

    assert fill_done["v"] is True
    assert fake_feed.stop_calls == 1
    assert len(o._fill_tasks) == 0



# -- fix task-14 : silence HL global (durée + toutes sources) vs carnet périmé par marché --------

async def test_single_coin_book_stale_blocks_only_that_market_no_global_cancel():
    o = Orchestrator(GlobalCfg(), [M], HLC(), VC(), J(),
                     clock=lambda: datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp())
    await o.setup()
    eng = o.engines["XAGS"]
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 15, ts=time.time(), quote_id="q1")
    # flux globalement vivant (pas de silence), mais le carnet de CE coin n'a pas été rafraîchi
    # depuis 35 s (> hl_book_max_age_s, 10 s depuis L1) : ce marché seul doit cesser de coter.
    o.hl_feed = FakeHLFeed(mid=67.05, book_age=35.0, stalled=False, fills_alive=True)
    await o.tick()
    assert o.hl_client.cancel_all_calls == []
    assert o._hl_silence_active is False
    assert o._need_reconcile is False
    assert eng.last_reason == "carnet HL périmé"


async def test_single_coin_book_stale_below_threshold_does_not_block():
    o = Orchestrator(GlobalCfg(), [M], HLC(), VC(), J(),
                     clock=lambda: datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp())
    await o.setup()
    eng = o.engines["XAGS"]
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 15, ts=time.time(), quote_id="q1")
    # muet depuis 7 s seulement : sous le seuil de 10 s (L1, flux bbo ; 30 s sur l2Book), ne
    # doit pas bloquer ce marché.
    o.hl_feed = FakeHLFeed(mid=67.05, book_age=7.0, stalled=False, fills_alive=True)
    await o.tick()
    assert o.hl_client.cancel_all_calls == []
    assert eng.last_reason != "carnet HL périmé"


async def test_two_markets_one_stale_book_does_not_block_the_other():
    h = HLC2()
    o = Orchestrator(GlobalCfg(), [M, M2], h, VC2(), J(),
                     clock=lambda: datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp())
    await o.setup()
    e1, e2 = o.engines["XAGS"], o.engines["GOLD"]
    o.feed._quotes["XAGS"] = VarQuote(67.0, 67.1, 15, ts=time.time(), quote_id="q1")
    o.feed._quotes["GOLD"] = VarQuote(2000.0, 2001.0, 1, ts=time.time(), quote_id="q2")
    o.hl_feed = FakeHLFeed(mid={"xyz:SILVER": 67.05, "xyz:GOLD": 2000.5},
                           book_age={"xyz:SILVER": 35.0, "xyz:GOLD": 0.0},
                           stalled=False, fills_alive=True)
    await o.tick()
    assert h.cancel_all_calls == []
    assert e1.last_reason == "carnet HL périmé"
    assert e2.last_reason is None


async def test_paused_restored_even_if_closed_cancel_all_raises():
    t = {"v": datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp()}
    o = Orchestrator(GlobalCfg(), [M], HLC(), VC(), J(), clock=lambda: t["v"])
    await o.setup()
    eng = o.engines["XAGS"]
    eng.state = PAUSED
    o.hl_feed = FakeHLFeed()
    t["v"] = datetime(2026, 9, 22, 20, 57, tzinfo=timezone.utc).timestamp()  # FLAT -> flatten taker
    await o.tick()
    assert eng.state == EXITING

    async def boom():
        raise RuntimeError("annulation HL indisponible")

    eng.cancel_all = boom
    t["v"] = datetime(2026, 9, 22, 21, 1, tzinfo=timezone.utc).timestamp()  # CLOSED (réouverture)
    await o.tick()
    # l'état est restauré à PAUSED même si cancel_all() a levé (l'exception est isolée par marché).
    assert eng.state == PAUSED


async def test_setup_cancels_stale_orders_from_previous_run():
    h = HLC()
    h.open_orders = lambda: [{"coin": "xyz:SILVER", "oid": 7}, {"coin": "BTC", "oid": 8}]
    cancelled = []
    h.cancel_all = lambda coins: cancelled.append(list(coins))
    j = J()
    o = Orchestrator(GlobalCfg(), [M], h, VC(), j, clock=lambda: 0)
    await o.setup()
    assert cancelled == [["xyz:SILVER"]]
    assert any("run précédent" in str(i) for i in j.inc)
