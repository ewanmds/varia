"""An uneven adopted pair must not open into the remaining old direction."""
import pytest

from arb.config import PairCfg
from arb.pair_exec import PairExecutor, plan_cycle, target_usd
from tests.test_pair_exec import FakeVar, J, PRICES, usd_pos, nosleep


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("reopen", [True, False])
def test_unequal_plans_close_both_legs_before_opening(direction, reopen):
    current = {"wti": -500 * direction, "brent": 1000 * direction}
    target = target_usd("A" if reopen == (direction == 1) else "B", 1000)
    steps = plan_cycle(current, target, 500, reopen=reopen)
    assert len(steps) == 7  # Three closes and four opens, unchanged count.
    assert [s.reduce_only for s in steps] == [True] * 3 + [False] * 4
    remaining = dict(current)
    for index, step in enumerate(steps):
        if not step.reduce_only and all(s.reduce_only for s in steps[:index]):
            assert remaining == {"wti": 0, "brent": 0}
        remaining[step.leg] += step.usd if step.side == "buy" else -step.usd
    assert remaining == target


@pytest.mark.parametrize("direction", [1, -1])
async def test_unequal_actual_quantities_are_flat_before_first_flip_open(direction):
    class ObservedVar(FakeVar):
        before = None

        async def market(self, quote_id, side, max_slippage, reduce_only=False, prio=0, deadline=None):
            if not reduce_only and self.before is None:
                self.before = usd_pos(self)
            return await super().market(quote_id, side, max_slippage, reduce_only, prio, deadline)

    held = {"wti": -749 * direction, "brent": 1248 * direction}
    var = ObservedVar(held)
    target = target_usd("B" if direction == 1 else "A", 1000)
    # The live orchestrator rounds starting notionals to whole tranches;
    # the executor still closes the actual quantities exactly.
    rounded = {"wti": -500 * direction, "brent": 1000 * direction}
    steps = plan_cycle(rounded, target, 500, reopen=False)
    result = await PairExecutor(var, PairCfg(leg_usd=1000, tranche_usd=500, order_gap_s=0),
                                J(), sleep=nosleep).run_steps(steps, PRICES)
    assert result.ok and len(var.orders) == 7
    assert var.before == {"wti": 0, "brent": 0}
    assert usd_pos(var) == pytest.approx(target, abs=1)
