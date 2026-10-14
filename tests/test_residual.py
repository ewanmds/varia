"""Correctifs résiduels : N1 à N5, D3, README."""
import time
from pathlib import Path

import pytest

from arb.config import GlobalCfg
from arb.hl_client import HLRefused
from arb.orchestrator import Orchestrator
from arb.pricing import VarQuote
from tests.test_final_ratelimit import HL, Q, mk, shifted
from tests.test_orchestrator import HLC, HLC2, J, M, M2, VC, VC2, FakeHLFeed

ROOT = Path(__file__).resolve().parents[1]
NOON = 1790078400.0  # 2026-09-22 12:00 UTC


class HLO(HL):
    """HL de moteur avec carnet d'ordres ouverts lisible."""

    def __init__(self):
        super().__init__()
        self.open = []
        self.open_reads = 0

    async def open_orders(self):
        self.open_reads += 1
        return list(self.open)


# -- N1 -----------------------------------------------------------------------------------------

async def test_n1_cancel_retry_backoff_doubles():
    hl = HLO()
    e, t = mk(hl)
    await e.requote(Q, 0.0, None, None, 1.0, None)
    hl.cancel_ok = False
    await e.pause("x")                       # 1er échec par côté : backoff 1 s
    n = len(hl.cancelled)
    await e.retry_pending_cancels()          # backoff pas écoulé : aucune tentative
    assert len(hl.cancelled) == n
    t["v"] += 1.01
    await e.retry_pending_cancels()          # 2e échec : backoff 2 s
    assert len(hl.cancelled) == n + 2 and e._cancel_backoff_s["buy"] == 2.0
    t["v"] += 1.5
    await e.retry_pending_cancels()
    assert len(hl.cancelled) == n + 2
    for _ in range(10):
        t["v"] += 100
        e._cancel_fails = {"buy": 0, "sell": 0}   # rester sous le seuil de relecture du carnet
        await e.retry_pending_cancels()
    assert e._cancel_backoff_s["buy"] == 30.0


async def test_n1_after_three_failures_slot_freed_if_order_gone():
    hl = HLO()
    e, t = mk(hl)
    await e.requote(Q, 0.0, None, None, 1.0, None)
    buy, sell = e.orders["buy"]["oid"], e.orders["sell"]["oid"]
    hl.cancel_ok = False
    await e.pause("x")                       # échec 1
    for _ in range(2):                       # échecs 2 et 3
        t["v"] += 100
        await e.retry_pending_cancels()
    assert e._cancel_fails["buy"] == 3 and hl.open_reads == 0
    hl.open = [{"coin": "xyz:SILVER", "oid": sell}]   # l'achat n'est plus au carnet
    t["v"] += 100
    await e.retry_pending_cancels()
    assert hl.open_reads >= 1
    assert e.orders["buy"] is None and "buy" not in e._cancel_failed
    assert e.orders["sell"] is not None and "sell" in e._cancel_failed


# -- N4 -----------------------------------------------------------------------------------------

async def test_n4_modify_without_oid_cancels_untracked_then_replaces():
    class NoOid(HLO):
        async def modify(self, coin, oid, is_buy, sz, px):
            return {"oid": None, "filled_sz": 0.0, "avg_px": None, "error": None}

    hl = NoOid()
    e, t = mk(hl)
    await e.requote(Q, 0.0, None, None, 1.0, None)
    buy, sell = e.orders["buy"]["oid"], e.orders["sell"]["oid"]
    hl.open = [{"coin": "xyz:SILVER", "oid": buy}, {"coin": "xyz:SILVER", "oid": sell},
               {"coin": "xyz:SILVER", "oid": 999}, {"coin": "xyz:GOLD", "oid": 777}]
    await e.requote(shifted(Q, 20), 0.0, None, None, 1.0, None)   # vente trop agressive -> modify
    assert 999 in hl.cancelled and 777 not in hl.cancelled        # ordre non suivi du coin annulé
    assert 999 in e._my_oids
    assert sell in hl.cancelled and e.orders["sell"]["oid"] == hl.placed[-1][3]   # reposé normalement
    assert any("modify HL sans oid" in i[2] for i in e.journal.incidents)


async def test_n4_modify_raises_is_treated_as_unknown():
    class Raises(HLO):
        async def modify(self, *a):
            raise TimeoutError("délai dépassé")

    hl = Raises()
    e, t = mk(hl)
    await e.requote(Q, 0.0, None, None, 1.0, None)
    hl.open = [{"coin": "xyz:SILVER", "oid": 4242}]
    await e.requote(shifted(Q, 20), 0.0, None, None, 1.0, None)
    assert hl.open_reads == 1 and 4242 in hl.cancelled


async def test_n4_modify_new_oid_replaces_tracked_oid():
    hl = HLO()
    e, t = mk(hl)
    await e.requote(Q, 0.0, None, None, 1.0, None)
    old = e.orders["sell"]["oid"]
    await e.requote(shifted(Q, 20), 0.0, None, None, 1.0, None)
    new = e.orders["sell"]["oid"]
    assert new != old and new == hl.modified[-1][4]
    assert new in e._my_oids and old in e._my_oids
    assert hl.open_reads == 0


async def test_n4_modify_explicit_error_plain_fallback():
    hl = HLO()
    e, t = mk(hl)
    await e.requote(Q, 0.0, None, None, 1.0, None)
    hl.modify_error = "Post only order would have immediately matched"
    await e.requote(shifted(Q, 20), 0.0, None, None, 1.0, None)
    assert hl.open_reads == 0 and len(hl.placed) == 3


# -- N5 -----------------------------------------------------------------------------------------

async def test_n5_placed_clears_pending_cancel_state():
    hl = HLO()
    e, t = mk(hl)
    await e.requote(Q, 0.0, None, None, 1.0, None)
    e._note_cancel_failure("buy")
    assert "buy" in e._cancel_failed
    e._placed("buy", 5555, 66.0, 1.0)
    assert "buy" not in e._cancel_failed and e._cancel_fails["buy"] == 0
    assert e._cancel_retry_at["buy"] == 0.0


# -- N2 -----------------------------------------------------------------------------------------

def test_n2_config_defaults():
    g = GlobalCfg()
    assert g.deadman_renew_s == 60 and g.deadman_window_s == 120
    from arb.config import load_config
    g2, _ = load_config(ROOT / "config.toml")
    assert g2.deadman_renew_s == 60 and g2.deadman_window_s == 120


async def _orch_with_order(h):
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: 0)
    await o.setup()
    o.hl_feed = FakeHLFeed()
    seat(o)
    return o


def seat(o):
    """Ordre au repos (la phase fermée du faux l'annule à chaque tick : on le repose)."""
    o.engines["XAGS"].orders["buy"] = {"oid": 5, "px": 66.0, "sz": 1.0}


async def test_n2_exception_retries_next_cadence_not_ten_minutes():
    class Flaky(HLC):
        def __init__(self):
            super().__init__()
            self.n = 0

        def schedule_cancel(self, when_ms):
            self.n += 1
            if self.n == 1:
                raise ConnectionError("réseau")
            return super().schedule_cancel(when_ms)

    h = Flaky()
    o = await _orch_with_order(h)
    await o.tick()
    assert o._deadman_refused is False
    assert o._next_deadman - time.monotonic() == pytest.approx(60.0, abs=1.0)
    assert not any("indisponible" in i[2] for i in o.journal.inc)
    o._next_deadman = 0.0
    seat(o)
    await o.tick()
    assert h.n == 2 and h.schedule_calls


async def test_n2_refused_flag_reset_after_success():
    class RefuseOnce(HLC):
        def __init__(self):
            super().__init__()
            self.n = 0

        def schedule_cancel(self, when_ms):
            self.n += 1
            if self.n == 1:
                raise HLRefused("pas éligible")
            return super().schedule_cancel(when_ms)

    h = RefuseOnce()
    o = await _orch_with_order(h)
    await o.tick()
    assert o._deadman_refused is True
    o._next_deadman = 0.0
    seat(o)
    await o.tick()
    assert o._deadman_refused is False


async def test_n2_shutdown_always_tries_disarm_even_after_refusal():
    import asyncio
    from unittest.mock import patch

    class Feed:
        def start(self): pass
        def stop(self): pass
        def book(self, c): return (66.9, 67.2)
        def mid(self, c): return 67.05
        def fills_alive(self, max_age_s=30.0): return True
        def last_any_msg(self): return time.time()
        def book_age(self, c): return 0.0

    h = HLC()
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: 0)
    await o.setup()
    o._deadman_refused = True
    stop = asyncio.Event()

    async def driver():
        await asyncio.sleep(0.05)
        stop.set()

    with patch("arb.orchestrator.HLFeed", return_value=Feed()):
        await asyncio.gather(o.run(stop, "0xabc"), driver())
    assert h.schedule_calls[-1] is None


# -- N3 -----------------------------------------------------------------------------------------

async def test_n3_no_renewal_without_orders_or_in_cancel_only():
    h = HLC()
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: 0)
    await o.setup()
    o.hl_feed = FakeHLFeed()
    await o.tick()
    assert getattr(h, "schedule_calls", []) == []          # aucun ordre au repos
    e = o.engines["XAGS"]
    e._cancel_failed.add("buy")
    e.orders["buy"] = {"oid": 5, "px": 66.0, "sz": 1.0}
    e._cancel_retry_at["buy"] = time.monotonic() + 1000     # pas de nouvel essai pendant ce tick
    e._mono = time.monotonic
    await o.tick()
    assert len(h.schedule_calls) == 1                        # annulation non confirmée : renouvelé
    o._next_deadman = 0.0
    o._cancel_only = True
    await o.tick()
    assert len(h.schedule_calls) == 1                        # annulation seule : pas de renouvellement


async def test_n3_entering_cancel_only_cancels_everything():
    class Low(HLC):
        headroom = 100000

        def rate_limit(self):
            return {"used": 0, "cap": self.headroom, "surplus": 0, "cum_vlm": 0.0, "headroom": self.headroom}

    h = Low()
    o = Orchestrator(GlobalCfg(), [M], h, VC(), J(), clock=lambda: 0)
    await o.setup()
    e = o.engines["XAGS"]
    e.orders["buy"] = {"oid": 5, "px": 66.0, "sz": 1.0}
    h.headroom = 100
    await o.refresh_rate_limit()
    assert o.cancel_only() and h.cancel_all_calls == [["xyz:SILVER"]]
    assert e.orders == {"buy": None, "sell": None}
    await o.refresh_rate_limit()                             # déjà en annulation seule : pas de doublon
    assert len(h.cancel_all_calls) == 1


# -- D3 -----------------------------------------------------------------------------------------

async def test_d3_margin_reduction_waits_for_fresh_margin_read():
    o = Orchestrator(GlobalCfg(), [M, M2], HLC2(), VC2(), J(), clock=lambda: NOON)
    await o.setup()
    e1, e2 = o.engines["XAGS"], o.engines["GOLD"]
    e1.pos_hl, e1.pos_var = 3.0, -3.0
    e2.pos_hl, e2.pos_var = 1.0, -1.0
    o.hl_margin = 0.9
    o._margin_read_at = time.monotonic()
    o.hl_feed = FakeHLFeed(mid={"xyz:SILVER": 67.05, "xyz:GOLD": 2000.5})
    calls = []

    def spy(name, eng):
        async def f(taker, b, a, var_mid=None):
            calls.append(name)
            eng.pos_hl = eng.pos_var = 0.0          # la réduction a vidé GOLD
        return f

    e1.flatten, e2.flatten = spy("XAGS", e1), spy("GOLD", e2)
    await o.tick()
    assert calls == ["GOLD"]
    await o.tick()                                  # marge encore 0,9 mais lue AVANT la réduction
    assert calls == ["GOLD"]
    o._margin_read_at = time.monotonic()            # nouvelle lecture de marge, toujours tendue
    await o.tick()
    assert calls == ["GOLD", "XAGS"]


# -- README ---------------------------------------------------------------------------------------

def test_readme_first_contact_modify_and_schedule_cancel_checks():
    sec = (ROOT / "README.md").read_text().split("## Limites connues", 1)[1]
    assert "statuses[0]" in sec and "modify" in sec
    assert "nRequestsUsed" in sec and "scheduleCancel" in sec
