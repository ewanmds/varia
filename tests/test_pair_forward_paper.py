"""Prospective simulation must use received data and cannot reach an account."""
import math

import pytest

from tools.pair_forward_paper import ASSETS, Capture, ForwardPaper, SyntheticQuoteSource


class Clock:
    t = 6000.0

    def __call__(self):
        return self.t


def observed(source, now, wti=90, brent=100):
    for asset, price in zip(ASSETS.values(), (wti, brent)):
        source.observe(asset, {"mark": price, "ts_recv_ms": now * 1000})


def publish(engine, now, slope=0):
    minute = math.floor(now / 60) * 60
    points = [(minute - (30 - i) * 60, 90 * math.exp(slope * i / 1e4), 100) for i in range(30)]
    engine.observe_tv({"ready": True, "points": points}, now)


def make(capture=None):
    clock = Clock()
    source = SyntheticQuoteSource(0.3, 0.2, clock=clock)

    async def sleep(seconds):
        clock.t += seconds
        observed(source, clock.t)

    engine = ForwardPaper(source, clock=clock, sleep=sleep, capture=capture)
    observed(source, clock.t)
    publish(engine, clock.t)
    return engine, source, clock


async def next_observation(engine, source, clock):
    clock.t += 1
    observed(source, clock.t)
    return await engine.advance()


async def test_local_simulation_has_no_account_or_real_order_capability(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline engine must never construct a network feed")

    monkeypatch.setattr("tools.pair_forward_paper.VarPriceStream", forbidden)
    monkeypatch.setattr("tools.pair_forward_paper.TradingViewCandles", forbidden)
    engine, source, clock = make()
    assert not hasattr(source, "market") and not hasattr(source, "positions")
    await engine.advance()
    assert await next_observation(engine, source, clock)
    summary = engine.status()
    assert summary["real_orders_sent"] == 0 and summary["account_connection"] is False
    assert all(a["orders"] == 4 for a in summary["policies"].values())


async def test_elapsed_time_without_new_two_leg_observation_cannot_execute():
    engine, source, clock = make()
    await engine.advance()
    clock.t += 1
    assert not await engine.advance()
    source.observe("USOILP", {"mark": 90, "ts_recv_ms": clock.t * 1000})
    assert not await engine.advance()
    source.observe("UKOILP", {"mark": 100, "ts_recv_ms": clock.t * 1000})
    assert await engine.advance()


async def test_fixed_common_cadence_counts_and_costs_survive_paper_flatten():
    engine, source, clock = make()
    await engine.advance()
    await next_observation(engine, source, clock)
    clock.t = engine.next_cycle_at - 1
    observed(source, clock.t)
    publish(engine, clock.t)
    assert not await engine.advance()
    assert all(a["orders"] == 4 for a in engine.status()["policies"].values())
    clock.t += 1
    observed(source, clock.t)
    assert not await engine.advance()  # New decision, before any new fill.
    assert await next_observation(engine, source, clock)
    assert all(a["orders"] == 12 for a in engine.status()["policies"].values())
    results = await engine.flatten()
    assert all(r["ok"] for r in results.values())
    summary = engine.status()
    assert summary["same_order_count"] and summary["same_cycle_count"]
    for arm in summary["policies"].values():
        assert arm["orders"] == 16 and arm["cycles"] == 2
        assert not any(arm["positions_qty"].values())
        assert arm["net_paper_pnl_usd"] < 0 and arm["assumed_transaction_cost_usd"] > 0
        assert arm["paper_cost_per_m"] == pytest.approx(50, abs=0.01)


async def test_stale_or_unclosed_publications_cannot_create_paper_orders():
    engine, source, clock = make()
    clock.t += 91
    observed(source, clock.t)
    assert not await engine.advance() and engine.pending is None
    engine.observe_tv({"ready": True, "points": [(clock.t, 90, 100)]}, clock.t)
    assert not await engine.advance() and engine.pending is None
    assert all(a["orders"] == 0 for a in engine.status()["policies"].values())


async def test_new_direction_before_execution_defers_old_paper_plan():
    engine, source, clock = make()
    publish(engine, clock.t, slope=1)
    await engine.advance()
    clock.t += 1
    observed(source, clock.t)
    publish(engine, clock.t, slope=-1)
    assert not await engine.advance() and engine.pending is None
    assert all(a["orders"] == 0 for a in engine.status()["policies"].values())


async def test_missing_fresh_var_marks_blocks_entries_and_does_not_invent_flatten_price():
    engine, source, clock = make()
    await engine.advance()
    await next_observation(engine, source, clock)
    clock.t += 6
    assert not await engine.advance()
    result = await engine.flatten()
    assert all(not r["ok"] for r in result.values())
    assert any(arm["positions_qty"]["wti"] for arm in engine.status()["policies"].values())


async def test_capture_records_observed_time_and_simulation_provenance(tmp_path):
    capture = Capture(tmp_path)
    engine, source, clock = make(capture)
    await engine.advance()
    await next_observation(engine, source, clock)
    capture.document("status.json", engine.status())
    capture.close()
    import json
    signal = json.loads((tmp_path / "signals.jsonl").read_text().splitlines()[0])
    assert signal["observed_at"] == 6000
    assert max(b["bar_open_ts"] for b in signal["bars"]) + 60 <= signal["observed_at"]
    fills = [json.loads(line) for line in (tmp_path / "fills.jsonl").read_text().splitlines()]
    assert len(fills) == 8
    assert all(f["scope"] == "local_simulation_only" and not f["execution_cost_verified"] for f in fills)


@pytest.mark.parametrize("spread,slip", [(-1, 0), (float("nan"), 0), (1, float("inf")), (5000, 5000)])
def test_invalid_cost_assumptions_rejected(spread, slip):
    with pytest.raises(ValueError):
        SyntheticQuoteSource(spread, slip)
