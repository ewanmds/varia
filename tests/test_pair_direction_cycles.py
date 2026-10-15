"""Direction fixes must preserve the mandatory volume cadence and risk exits."""
import math
from dataclasses import replace

import pytest

from arb.config import PairCfg
from tests.test_pair_orchestrator import FakeVar, make
from tests.test_pair_trend import Decision


def cfg(**changes):
    return PairCfg(enabled=True, signal_mode="trend_hold", leg_usd=1000,
                   tranche_usd=500, cycle_min=1, volume_cycle_min=5,
                   volume_cycle_required=True, order_gap_s=0, **changes)


@pytest.mark.parametrize("old,slope,expected", [("A", 0.1, "B"), ("B", -0.1, "A")])
async def test_mandatory_cycle_follows_fresh_weak_slope_before_existing_side(old, slope, expected):
    direction = 1 if old == "B" else -1
    var = FakeVar(pos={"USOILP": direction * 1000 / 90,
                       "UKOILP": -direction * 1000 / 100})
    o, var, clock = await make(cfg=cfg(), var=var)
    o.trend = Decision(side=None, slope=slope)
    clock.t += 300
    await o.tick()
    assert o.status()["pair"]["side"] == expected
    assert len(var.orders) == 8
    assert o.status()["pair"]["next_volume_cycle_in_s"] == pytest.approx(300)


async def test_flat_reentry_uses_fresh_slope_before_previous_short():
    o, var, _ = await make(cfg=cfg())
    o.pref_side = "A"
    o.trend = Decision(side=None, slope=0.1)
    await o.tick()
    assert len(var.orders) == 4 and o.status()["pair"]["side"] == "B"


def load_pullback(o, now, direction=1):
    minute = math.floor(now / 60) * 60
    stamps = [minute - (30 - i) * 60 for i in range(31)]
    # Twenty-five rising closes, then a local five-minute decline.
    values = [i if i < 25 else 24 - 2 * (i - 24) for i in range(31)]
    o.tv.closes = ({t: 90 * math.exp(direction * v / 1e4) for t, v in zip(stamps, values)},
                   {t: 100 for t in stamps})
    o.tv.last_poll, o.tv.last_error = now, None


@pytest.mark.parametrize("direction,expected", [(1, "B"), (-1, "A")])
async def test_context_vetoes_local_countertrend_without_reducing_scheduled_orders(direction, expected):
    o, var, clock = await make(cfg=cfg(trend_source="tradingview", trend_window_min=5,
                                     trend_confirm_min=2, trend_context_min=30))
    assert o.tv.window_min == 30
    load_pullback(o, clock.t, direction)
    await o.tick()
    trend = o.status()["pair"]["trend"]
    assert trend["context"]["side"] == expected and trend["side"] is None
    assert o.status()["pair"]["side"] == expected and len(var.orders) == 4
    clock.t += 299
    load_pullback(o, clock.t, direction)
    await o.tick()
    assert len(var.orders) == 4
    clock.t += 1
    load_pullback(o, clock.t, direction)
    await o.tick()
    assert o.status()["pair"]["side"] == expected and len(var.orders) == 12
    assert o.status()["pair"]["volume_session_usd"] == pytest.approx(6000, abs=1)
    # Even with a context veto and unavailable signal data, risk closure wins.
    o.tv.last_error = "flux indisponible"
    o.journal._inner.daily = -201
    await o.tick()
    assert o.killed and not any(var.pos.values())


async def test_missing_context_never_recycles_an_existing_pair():
    var = FakeVar(pos={"USOILP": -1000 / 90, "UKOILP": 1000 / 100})
    o, var, clock = await make(cfg=cfg(trend_context_min=30, trend_window_min=5,
                                     trend_confirm_min=2), var=var)
    # Fast data alone is insufficient when a context window is required.
    for i in range(6):
        o.trend.add(clock.t - (6 - i) * 60, 90 * math.exp(i / 1e4), 100)
    clock.t += 300
    await o.tick()
    assert not var.orders


@pytest.mark.parametrize("cap,expected_orders", [(79, 0), (81, 8)])
async def test_optional_context_flip_uses_cycle_cost_budget_not_opposing_fast_projection(cap, expected_orders):
    var = FakeVar(pos={"USOILP": -1000 / 90, "UKOILP": 1000 / 100})
    settings = replace(cfg(trend_source="tradingview", trend_window_min=5,
                           trend_confirm_min=2, trend_context_min=30),
                       volume_cycle_required=False, trend_cost_multiplier=100,
                       cost_alert_per_m=cap)
    o, var, clock = await make(cfg=settings, var=var)
    # Full spreads: 0.6667 + 0.6 pb, plus this reserve = 3.2 pb = 80 $/M.
    o.cfg.trend_slip_reserve_bps = 3.2 - (0.006 / 90 + 0.006 / 100) * 1e4
    clock.t += 300
    load_pullback(o, clock.t)
    await o.tick()
    assert len(var.orders) == expected_orders
    assert o.status()["pair"]["side"] == ("B" if expected_orders else "A")
