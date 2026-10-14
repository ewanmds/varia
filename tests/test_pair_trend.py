import math
from dataclasses import replace

import pytest

from arb.config import PairCfg
from arb.pair_signal import TrendSignal
from arb.pricing import VarQuote
from tests.test_pair_orchestrator import make, PX, FakeVar


def feed(signal, slope=0.5, n=31, start=0):
    for i in range(n):
        signal.add(start + i * 60, 90 * math.exp(slope * i / 1e4), 100)


@pytest.mark.parametrize("slope,side", [(0.5, "B"), (-0.5, "A")])
def test_ratio_trend_direction_and_scale(slope, side):
    signal = TrendSignal()
    feed(signal, slope)
    result = signal.status()
    assert result["ready"] and result["side"] == side
    assert result["slope_bps_min"] == pytest.approx(slope)
    assert result["r2"] == pytest.approx(1)


def test_warmup_and_flat_ratio_never_choose_arbitrary_direction():
    signal = TrendSignal()
    feed(signal, n=10)
    assert signal.status()["side"] is None and not signal.status()["ready"]
    signal.reset()
    feed(signal, slope=0)
    assert signal.status()["ready"] and signal.status()["side"] is None


def test_unclosed_minute_cannot_flip_confirmed_signal():
    signal = TrendSignal()
    feed(signal)
    signal.add(30 * 60 + 55, 60, 100)
    assert signal.status()["side"] == "B"


def test_discontinuous_or_nonfinite_history_cannot_trigger_orders():
    signal = TrendSignal()
    feed(signal, n=15)
    feed(signal, n=15, start=18 * 60)
    assert signal.status()["side"] is None
    old = len(signal._pts)
    signal.add(2000, float("nan"), 100)
    signal.add(2000, 90, 0)
    signal.add(float("inf"), 90, 100)
    assert len(signal._pts) == old


def test_recent_reversal_vetoes_longer_uptrend():
    signal = TrendSignal()
    for i in range(31):
        value = i if i < 24 else 24 - 0.5 * (i - 24)
        signal.add(i * 60, 90 * math.exp(value / 1e4), 100)
    status = signal.status()
    assert status["ready"] and status["slope_bps_min"] > 0
    assert status["confirm_slope_bps_min"] < 0 and status["side"] is None


class Decision:
    def __init__(self, side="B", slope=1.0, ready=True):
        self.side, self.slope, self.ready = side, slope, ready

    def add(self, *a):
        pass

    def status(self):
        return {"side": self.side, "slope_bps_min": self.slope, "ready": self.ready,
                "reason": "test", "r2": 1.0, "move_bps": 30 * self.slope}


async def trend_bot(decision=None):
    cfg = PairCfg(enabled=True, signal_mode="trend_hold", leg_usd=1000, tranche_usd=500, order_gap_s=0)
    o, var, clock = await make(cfg=cfg)
    if decision:
        o.trend = decision
    return o, var, clock


async def test_trend_warmup_never_opens_and_exposes_wait_reason():
    o, var, _ = await trend_bot()
    await o.tick()
    assert var.orders == [] and o.status()["pair"]["state"] == "ATTENTE"
    assert "préchauffage tendance" in o.status()["pair"]["reason"]


async def test_explicit_initial_direction_opens_without_fabricating_trend():
    cfg = PairCfg(enabled=True, signal_mode="trend_hold", warmup_entry_side="B",
                  leg_usd=1000, tranche_usd=500, order_gap_s=0, trend_slip_reserve_bps=0)
    o, var, clock = await make(cfg=cfg)
    await o.tick()
    assert len(var.orders) == 4 and var.pos["USOILP"] > 0 > var.pos["UKOILP"]
    assert o.status()["pair"]["trend"]["ready"] is False
    assert o.status()["pair"]["projected_move_bps"] is None
    assert o.status()["pair"]["next_cycle_in_s"] == pytest.approx(600)
    clock.t += 601
    await o.tick()
    assert len(var.orders) == 4  # pas de recyclage du sens initial


async def test_immediate_entry_still_respects_quote_cost_budget():
    cfg = PairCfg(enabled=True, signal_mode="trend_hold", warmup_entry_side="B",
                  leg_usd=1000, tranche_usd=500, order_gap_s=0)
    o, var, _ = await make(cfg=cfg)
    await o.tick()
    assert not var.orders
    assert "budget CPM" in o.status()["pair"]["reason"]


async def test_session_indicator_distinguishes_unknown_from_disconnected():
    from types import SimpleNamespace
    o, var, _ = await trend_bot()
    assert o.status()["session_ok"] is None
    var._health_ok = True
    assert o.status()["session_ok"] is True
    var.transport = SimpleNamespace(bridge=SimpleNamespace(connected=False))
    assert o.status()["session_ok"] is False


async def test_uptrend_opens_long_wti_short_brent_and_holds_without_churn():
    o, var, clock = await trend_bot(Decision())
    await o.tick()
    assert var.pos["USOILP"] > 0 > var.pos["UKOILP"] and len(var.orders) == 4
    count, volume = o.cycles, o.status()["pair"]["volume_session_usd"]
    clock.t += 601
    await o.tick()
    assert len(var.orders) == 4 and o.cycles == count
    assert o.status()["pair"]["volume_session_usd"] == volume
    assert "paire conservée" in o.status()["pair"]["reason"]
    assert set(var.others) == {"COPPER", "COPPP"}


async def volume_bot():
    cfg = PairCfg(enabled=True, signal_mode="trend_hold", leg_usd=1000, tranche_usd=500,
                  cycle_min=1, volume_cycle_min=5, order_gap_s=0, trend_slip_reserve_bps=0)
    o, var, clock = await make(cfg=cfg)
    o.trend = Decision()
    await o.tick()
    return o, var, clock


async def test_volume_cycle_waits_five_minutes_then_closes_and_reopens_same_side():
    o, var, clock = await volume_bot()
    initial_volume = o.status()["pair"]["volume_session_usd"]
    clock.t += 299
    await o.tick()
    assert len(var.orders) == 4 and o._cycle_retry_at == pytest.approx(clock.t + 1)
    clock.t += 1
    await o.tick()
    assert len(var.orders) == 12 and o.cycles == 2
    assert var.pos["USOILP"] > 0 > var.pos["UKOILP"]
    assert o.status()["pair"]["volume_session_usd"] - initial_volume == pytest.approx(4000, abs=1)
    assert o.status()["pair"]["next_volume_cycle_in_s"] == pytest.approx(300)
    assert all(x[0] in ("USOILP", "UKOILP") for x in var.orders)


async def test_neutral_signal_recycles_existing_side_but_cannot_open_flat():
    o, var, clock = await volume_bot()
    o.trend.side = None
    clock.t += 300
    await o.tick()
    assert len(var.orders) == 12 and o.status()["pair"]["side"] == "B"
    cfg = replace(o.cfg)
    flat, var, _ = await make(cfg=cfg)
    flat.trend = Decision(side=None)
    await flat.tick()
    assert not var.orders


async def test_volume_due_confirmed_reversal_flips_without_extra_recycling():
    o, var, clock = await volume_bot()
    o.trend.side, o.trend.slope = "A", -1
    clock.t += 300
    await o.tick()
    assert len(var.orders) == 12 and o.status()["pair"]["side"] == "A"


async def test_expensive_volume_cycle_is_deferred_without_closing_pair():
    o, var, clock = await volume_bot()
    async def expensive(asset, kind, qty, **kwargs):
        return VarQuote(PX[asset] * 0.999, PX[asset] * 1.001, qty, clock.t, "q")
    var.quote = expensive
    clock.t += 300
    await o.tick()
    assert len(var.orders) == 4 and o.status()["pair"]["side"] == "B"
    assert "cycle volume différé" in o.status()["pair"]["reason"]


@pytest.mark.parametrize("slope,expected", [(0.1, "B"), (-0.1, "A"), (0.0, "B")])
async def test_required_volume_opens_despite_weak_or_neutral_trend(slope, expected):
    cfg = PairCfg(enabled=True, signal_mode="trend_hold", leg_usd=1000, tranche_usd=500,
                  cycle_min=1, volume_cycle_min=5, volume_cycle_required=True, order_gap_s=0)
    o, var, _ = await make(cfg=cfg)
    o.trend = Decision(side=None, slope=slope)
    await o.tick()
    assert len(var.orders) == 4 and o.status()["pair"]["side"] == expected
    assert "sans confirmation" in o.status()["pair"]["reason"]


async def test_required_volume_does_not_wait_for_trend_or_cpm_at_five_minutes():
    o, var, clock = await volume_bot()
    o.cfg.volume_cycle_required = True
    o.trend.side, o.trend.slope = None, 0.0
    async def no_prediction_gate(**kwargs):
        raise AssertionError("Mandatory cycle must not use the trend/CPM prediction gate")
    o._trend_cost_gate = no_prediction_gate
    clock.t += 299
    await o.tick()
    assert len(var.orders) == 4
    clock.t += 1
    await o.tick()
    assert len(var.orders) == 12 and o.status()["pair"]["side"] == "B"
    assert "obligatoire" in o.status()["pair"]["reason"]


async def test_required_volume_warmup_and_daily_guard_remain_mandatory():
    cfg = PairCfg(enabled=True, signal_mode="trend_hold", leg_usd=1000, tranche_usd=500,
                  cycle_min=1, volume_cycle_min=5, volume_cycle_required=True, order_gap_s=0)
    o, var, _ = await make(cfg=cfg)
    o.trend = Decision(side=None, slope=None, ready=False)
    await o.tick()
    assert not var.orders
    o.trend = Decision(side="B", slope=1.0)
    o.journal._inner.daily = -201
    await o.tick()
    assert o.killed and not var.orders


async def test_confirmed_reversal_flips_only_the_two_oil_assets():
    decision = Decision()
    o, var, clock = await trend_bot(decision)
    await o.tick()
    decision.side, decision.slope = "A", -1
    clock.t += 601
    await o.tick()
    assert var.pos["USOILP"] < 0 < var.pos["UKOILP"]
    assert len(var.orders) == 12 and o.cycles == 2
    assert all(order[0] in ("USOILP", "UKOILP") for order in var.orders)


async def test_adopted_partial_pair_is_topped_up_without_closing_first():
    cfg = PairCfg(enabled=True, signal_mode="trend_hold", leg_usd=1000, tranche_usd=500, order_gap_s=0)
    var = FakeVar(pos={"USOILP": 600 / 90, "UKOILP": -600 / 100})
    o, var, clock = await make(var=var, cfg=cfg)
    o.trend = Decision()
    clock.t += 601
    await o.tick()
    assert len(var.orders) == 2 and o.cycles == 1
    assert var.pos["USOILP"] * 90 == pytest.approx(1000, abs=1)
    assert var.pos["UKOILP"] * 100 == pytest.approx(-1000, abs=1)


async def test_cost_gate_vetoes_expensive_quote_and_does_not_loop_every_second():
    o, var, clock = await trend_bot(Decision())
    quote_calls = []

    async def expensive(asset, kind, qty, **kwargs):
        quote_calls.append(asset)
        return VarQuote(PX[asset] * 0.999, PX[asset] * 1.001, qty, clock.t, "q")

    var.quote = expensive
    await o.tick()
    assert var.orders == [] and len(quote_calls) == 2
    assert "spread coté" in o.status()["pair"]["reason"]
    clock.t += 1
    await o.tick()
    assert len(quote_calls) == 2


async def test_neutral_does_not_open_or_reopen_existing_pair():
    decision = Decision()
    o, var, clock = await trend_bot(decision)
    decision.side = None
    await o.tick()
    assert not var.orders
    decision.side = "B"
    clock.t += 601
    await o.tick()
    decision.side = None
    clock.t += 601
    await o.tick()
    assert len(var.orders) == 4 and o.status()["pair"]["side"] == "B"


async def test_loss_guard_still_closes_when_trend_or_quotes_would_veto_entry():
    decision = Decision()
    o, var, clock = await trend_bot(decision)
    await o.tick()
    o.journal._inner.daily = -201
    decision.side = None
    await o.tick()
    assert o.killed and var.pos["USOILP"] == 0 and var.pos["UKOILP"] == 0
    assert len(var.orders) == 8


@pytest.mark.parametrize("changes", [{"signal_mode": "bad"}, {"trend_window_min": 2},
                                    {"trend_min_r2": float("nan")}, {"trend_cost_multiplier": 0}])
def test_invalid_strategy_config_rejected(changes):
    with pytest.raises(ValueError):
        replace(PairCfg(), **changes)
