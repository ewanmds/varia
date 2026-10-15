"""Awaited position/RFQ reads must not commit an obsolete or cancelled cycle."""
import asyncio
import math

import pytest

from arb.config import PairCfg
from tests.test_pair_orchestrator import T0, make


def settings(**changes):
    return PairCfg(enabled=True, signal_mode="trend_hold", trend_source="tradingview",
                   trend_window_min=5, trend_confirm_min=2, leg_usd=1000,
                   tranche_usd=500, cycle_min=1, order_gap_s=0, **changes)


def load_bars(o, now, direction=1):
    minute = math.floor(now / 60) * 60
    stamps = [minute - (5 - i) * 60 for i in range(6)]
    o.tv.closes = ({t: 90 * math.exp(direction * i / 1e4) for i, t in enumerate(stamps)},
                   {t: 100 for t in stamps})
    o.tv.last_poll, o.tv.last_error = now, None


async def test_completed_tv_poll_during_position_read_replaces_old_direction():
    o, var, clock = await make(cfg=settings(volume_cycle_min=5, volume_cycle_required=True))
    load_bars(o, clock.t, direction=1)
    original = var.positions
    armed = True

    async def positions(**kwargs):
        nonlocal armed
        if armed:
            armed = False
            await asyncio.sleep(0)
            load_bars(o, clock.t, direction=-1)
        return await original(**kwargs)

    var.positions = positions
    await o.tick()
    assert o.status()["pair"]["side"] == "A"
    assert o.status()["pair"]["trend"]["side"] == "A"
    assert len(var.orders) == 4


async def test_stop_during_position_read_prevents_new_cycle_orders():
    o, var, clock = await make(cfg=settings(volume_cycle_min=5, volume_cycle_required=True))
    load_bars(o, clock.t)
    original = var.positions
    kill_tasks = []
    armed = True

    async def positions(**kwargs):
        nonlocal armed
        if armed:
            armed = False
            kill_tasks.append(asyncio.create_task(o.kill("arrêt pendant la lecture")))
            await asyncio.sleep(0)
        return await original(**kwargs)

    var.positions = positions
    await o.tick()
    await asyncio.gather(*kill_tasks)
    assert o.killed and not var.orders and not o._has_pos()


@pytest.mark.parametrize("change", ["session", "feed", "pause"])
async def test_guards_changed_during_position_read_prevent_cycle(change):
    o, var, clock = await make(cfg=settings(volume_cycle_min=5, volume_cycle_required=True))
    load_bars(o, clock.t)
    original = var.positions
    armed = True

    async def positions(**kwargs):
        nonlocal armed
        if armed:
            armed = False
            await asyncio.sleep(0)
            if change == "session":
                clock.t = T0 + 6 * 3600 - 9 * 60
            elif change == "feed":
                o.stream.muted_at = clock.t - 61
            else:
                o._set_state("PAUSE", "pause pendant la lecture")
        return await original(**kwargs)

    var.positions = positions
    await o.tick()
    assert not var.orders and o.cycles == 0


@pytest.mark.parametrize("change", ["stale_tv", "direction", "session", "stop"])
async def test_cost_quote_reads_cannot_commit_changed_signal_or_guards(change):
    o, var, clock = await make(cfg=settings())
    load_bars(o, clock.t)
    original = var.quote
    quote_calls = []
    kill_tasks = []

    async def quote(*args, **kwargs):
        quote_calls.append(args[0])
        if len(quote_calls) == 1:
            await asyncio.sleep(0)
            if change == "stale_tv":
                o.tv.last_poll = clock.t - o.cfg.tv_stale_s - 1
            elif change == "direction":
                load_bars(o, clock.t, direction=-1)
            elif change == "session":
                clock.t = T0 + 6 * 3600 - 9 * 60
            else:
                kill_tasks.append(asyncio.create_task(o.kill("arrêt pendant les cotations")))
                await asyncio.sleep(0)
        return await original(*args, **kwargs)

    var.quote = quote
    await o.tick()
    if kill_tasks:
        await asyncio.gather(*kill_tasks)
    assert not var.orders and o.cycles == 0
    assert len(quote_calls) == 2  # No execution or extra RFQ pass after the obsolete plan.
    if change == "direction":
        assert "signal modifié" in o.status()["pair"]["reason"]
        clock.t += 1
        await o.tick()
        assert len(var.orders) == 4 and o.status()["pair"]["side"] == "A"


async def test_explicit_manual_entry_keeps_tv_bypass_with_normal_risk_guards():
    o, var, _ = await make(cfg=settings())
    assert not o.tv.snapshot()["ready"]
    o.request_entry("B")
    await o.tick()
    assert len(var.orders) == 4 and o.status()["pair"]["side"] == "B"
    assert o._manual_entry_executed == "B" and o._manual_entry_side is None


async def test_manual_request_remains_pending_if_position_read_crosses_session_close():
    o, var, clock = await make(cfg=settings())
    original = var.positions
    armed = True

    async def positions(**kwargs):
        nonlocal armed
        if armed:
            armed = False
            clock.t = T0 + 6 * 3600 - 9 * 60
        return await original(**kwargs)

    var.positions = positions
    o.request_entry("B")
    await o.tick()
    assert not var.orders and o._manual_entry_side == "B"


@pytest.mark.parametrize("boundary", ["executor_positions", "opening_quote"])
@pytest.mark.parametrize("change", ["direction", "stop"])
async def test_final_opening_guard_catches_last_await_race_without_pausing(boundary, change):
    o, var, clock = await make(cfg=settings(volume_cycle_min=5, volume_cycle_required=True))
    load_bars(o, clock.t)
    positions_original, quote_original = var.positions, var.quote
    position_calls = 0
    quote_calls = 0
    changed = False
    kill_tasks = []

    async def change_inputs():
        nonlocal changed
        changed = True
        if change == "direction":
            load_bars(o, clock.t, direction=-1)
        else:
            kill_tasks.append(asyncio.create_task(o.kill("arrêt avant le premier market")))
            await asyncio.sleep(0)

    async def positions(**kwargs):
        nonlocal position_calls
        position_calls += 1
        if not changed and boundary == "executor_positions" and position_calls == 2:
            await change_inputs()
        return await positions_original(**kwargs)

    async def quote(*args, **kwargs):
        nonlocal quote_calls
        quote_calls += 1
        result = await quote_original(*args, **kwargs)
        if not changed and boundary == "opening_quote":
            await change_inputs()
        return result

    var.positions, var.quote = positions, quote
    await o.tick()
    if kill_tasks:
        await asyncio.gather(*kill_tasks)
    assert changed and not var.orders and o.cycles == 0
    assert o._last_cycle_end is None and o.last_cycle_at is None
    assert quote_calls == 1
    assert o.state == "ACTIF"  # Deferral is not an execution failure or a PAUSE.
    if change == "direction":
        assert o._cycle_retry_at == pytest.approx(clock.t + 1)
        clock.t += 1
        await o.tick()
        assert len(var.orders) == 4 and o.status()["pair"]["side"] == "A"


async def test_deferred_reopen_accounts_real_closes_and_retries_fresh_direction():
    o, var, clock = await make(cfg=settings(volume_cycle_min=5, volume_cycle_required=True))
    load_bars(o, clock.t)
    await o.tick()
    assert len(var.orders) == 4 and o.cycles == 1
    clock.t += 300
    load_bars(o, clock.t)
    original = var.quote
    quote_calls = 0

    async def quote(*args, **kwargs):
        nonlocal quote_calls
        quote_calls += 1
        result = await original(*args, **kwargs)
        if quote_calls == 5:  # Four real reduce-only closes preceded the first opening RFQ.
            load_bars(o, clock.t, direction=-1)
        return result

    var.quote = quote
    before = o._volume
    await o.tick()
    assert len(var.orders) == 8 and all(order[2] for order in var.orders[4:])
    assert not any(var.pos.values()) and not o._has_pos()
    assert o._volume > before and o.cycles == 1 and o.state == "ACTIF"
    assert o._last_cycle_end == clock.t
    clock.t += 1
    await o.tick()
    assert len(var.orders) == 12 and o.status()["pair"]["side"] == "A"
    assert o.cycles == 2  # One completed scheduled cycle, without extra orders.


async def test_final_opening_guard_restores_unsent_explicit_manual_request():
    o, var, clock = await make(cfg=settings())
    original = var.quote

    async def quote(*args, **kwargs):
        result = await original(*args, **kwargs)
        o.stream.muted_at = clock.t - 61
        return result

    var.quote = quote
    o.request_entry("B")
    await o.tick()
    assert not var.orders and o._manual_entry_side == "B"
    assert o._manual_entry_executed is None and o._last_cycle_end is None
    assert o.state == "ACTIF" and o._cycle_retry_at == pytest.approx(clock.t + 30)


async def test_risk_close_does_not_receive_entry_guard_when_feed_is_unavailable():
    o, var, clock = await make(cfg=settings(volume_cycle_min=5, volume_cycle_required=True))
    load_bars(o, clock.t)
    await o.tick()
    original = o.executor.run_steps
    close_kwargs = []

    async def run_steps(steps, prices, **kwargs):
        close_kwargs.append(kwargs)
        return await original(steps, prices, **kwargs)

    o.executor.run_steps = run_steps
    o.tv.last_error = "flux indisponible"
    o.stream.muted_at = clock.t - 61
    await o.kill("arrêt sans données de signal")
    assert close_kwargs == [{}]
    assert not any(var.pos.values()) and len(var.orders) == 8
