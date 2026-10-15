"""A last opening guard must prevent stale entries without breaking a sent hedge."""
import asyncio

import pytest

from arb.config import PairCfg
from arb.pair_exec import PairExecutor, plan_cycle
from arb.var_client import VarRejected
from tests.test_pair_exec import A, B, FLIP, PRICES, FakeVar, J, nosleep


def executor(var):
    return PairExecutor(var, PairCfg(order_gap_s=0), J(), sleep=nosleep)


def opening_steps():
    return plan_cycle({"wti": 0, "brent": 0}, B, 2500, reopen=False)


@pytest.mark.parametrize("during", ["positions", "quote"])
async def test_guard_rechecks_changes_during_awaited_initial_reads_and_quote(during):
    state = {"killed": False}

    class Changed(FakeVar):
        positions_calls = quote_calls = 0

        async def positions(self, *args, **kwargs):
            self.positions_calls += 1
            result = await super().positions(*args, **kwargs)
            if during == "positions" and self.positions_calls == 1:
                await asyncio.sleep(0)
                state["killed"] = True
            return result

        async def quote(self, *args, **kwargs):
            self.quote_calls += 1
            result = await super().quote(*args, **kwargs)
            if during == "quote":
                await asyncio.sleep(0)
                state["killed"] = True
            return result

    var = Changed()
    ex = executor(var)

    async def no_rebalance(*args):
        raise AssertionError("veto must not rebalance or submit any order")

    ex._rebalance = no_rebalance
    result = await ex.run_steps(opening_steps(), PRICES,
                                entry_guard=lambda: "killed before opening" if state["killed"] else None)
    assert not result.ok and result.deferred and result.reason == "killed before opening"
    assert not result.filled and not var.orders and var.calls == 0
    assert var.quote_calls == 1  # only the already required first-order quote
    assert all(qty == 0 for qty in var.pos.values())


async def test_flip_closes_both_legs_before_opening_veto_and_remains_flat():
    var = FakeVar(A)
    seen = []

    def veto():
        seen.append(dict(var.pos))
        return "direction changed"

    result = await executor(var).run_steps(FLIP, PRICES, entry_guard=veto)
    assert not result.ok and result.deferred and result.reason == "direction changed"
    assert len(result.filled) == len(var.orders) == 4
    assert all(order[2] for order in var.orders)
    assert len(seen) == 1 and all(qty == 0 for qty in seen[0].values())
    assert all(qty == 0 for qty in var.pos.values())


async def test_reduce_only_close_never_calls_opening_guard():
    var = FakeVar(A)

    def forbidden():
        raise AssertionError("risk exit must never run the opening guard")

    result = await executor(var).run_steps(FLIP[:4], PRICES, entry_guard=forbidden)
    assert result.ok and not result.deferred
    assert len(var.orders) == 4 and all(order[2] for order in var.orders)
    assert all(qty == 0 for qty in var.pos.values())


@pytest.mark.parametrize("first_behavior", [None, "market_5xx_applied"])
async def test_sent_opening_completes_remaining_hedge_even_if_guard_would_now_veto(first_behavior):
    state = {"killed": False, "checks": 0}

    class KillAfterSend(FakeVar):
        async def market(self, *args, **kwargs):
            try:
                return await super().market(*args, **kwargs)
            finally:
                state["killed"] = True

    var = KillAfterSend(script={0: first_behavior})

    def guard():
        state["checks"] += 1
        return "killed" if state["killed"] else None

    result = await executor(var).run_steps(opening_steps(), PRICES, entry_guard=guard)
    assert result.ok and not result.deferred
    assert len(var.orders) == 4 and state["checks"] == 1
    assert var.pos["USOILP"] > 0 > var.pos["UKOILP"]


async def test_explicit_first_market_refusal_rechecks_guard_before_safe_retry():
    state = {"veto": False, "checks": 0}

    class RefusedThenChanged(FakeVar):
        async def market(self, *args, **kwargs):
            try:
                return await super().market(*args, **kwargs)
            except VarRejected:
                state["veto"] = True
                raise

    var = RefusedThenChanged(script={0: "reject"})

    def guard():
        state["checks"] += 1
        return "changed after refusal" if state["veto"] else None

    result = await executor(var).run_steps(opening_steps(), PRICES, entry_guard=guard)
    assert not result.ok and result.deferred and result.reason == "changed after refusal"
    assert state["checks"] == 2 and var.calls == 1
    assert len(var._quotes) == 2  # normal retry fetched a fresh quote; no extra quote added
    assert not result.filled and not var.orders


async def test_guard_error_defers_before_market_without_retry():
    var = FakeVar()

    def broken():
        raise RuntimeError("decision temporarily unavailable")

    result = await executor(var).run_steps(opening_steps(), PRICES, entry_guard=broken)
    assert not result.ok and result.deferred and "entry guard unavailable" in result.reason
    assert var.calls == 0 and len(var._quotes) == 1


async def test_later_order_failure_still_rebalances_after_opening_began_without_guarding_exit():
    var = FakeVar(script={1: "reject", 2: "reject"})
    checks = []

    def guard():
        checks.append(True)
        return None if len(checks) == 1 else "must not interrupt hedge or rebalance"

    result = await executor(var).run_steps(opening_steps(), PRICES, entry_guard=guard)
    assert not result.ok and not result.deferred and len(checks) == 1
    assert any(order[2] for order in var.orders)
    assert all(qty == 0 for qty in var.pos.values())
