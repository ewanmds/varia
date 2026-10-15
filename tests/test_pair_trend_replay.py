import math
from dataclasses import replace

import pytest

from arb.config import PairCfg
from tools.pair_trend_replay import compare_fixed_cadence, replay, read_rows


def rows(slope=0):
    return [(i * 60, 90 * math.exp(slope * i / 1e4), 100) for i in range(181)]


def test_flat_market_avoids_volume_churn_but_does_not_claim_positive_profit():
    cfg = PairCfg(leg_usd=1000, tranche_usd=500)
    baseline = replay(rows(), cfg, 0.3, 0.2, "mean_reversion")
    trend = replay(rows(), cfg, 0.3, 0.2, "trend_hold")
    assert baseline["pnl_usd"] < 0 and baseline["volume_usd"] > 0
    assert trend["pnl_usd"] == 0 and trend["volume_usd"] == 0
    assert trend["cost_per_m"] is None  # absence de volume != CPM nul


def test_synthetic_sustained_uptrend_uses_long_ratio_and_pays_costs():
    cfg = PairCfg(leg_usd=1000, tranche_usd=500)
    trend = replay(rows(1), cfg, 0.3, 0.2, "trend_hold")
    assert trend["pnl_usd"] > 0
    assert trend["transaction_cost_usd"] > 0 and trend["volume_usd"] > 3900


def test_decision_only_executes_on_following_point():
    cfg = PairCfg(leg_usd=1000, tranche_usd=500)
    result = replay([(0, 90, 100)], cfg, 0.3, 0.2, "mean_reversion")
    assert result["estimated_orders"] == 0 and result["cost_per_m"] is None


@pytest.mark.parametrize("cost", [-1, float("nan"), float("inf")])
def test_invalid_cost_model_rejected(cost):
    with pytest.raises(ValueError):
        replay(rows(), PairCfg(), cost, 0, "trend_hold")


def test_csv_requires_ordered_synchronized_prices(tmp_path):
    path = tmp_path / "prices.csv"
    path.write_text("ts,wti,brent\n60,90,100\n0,90,100\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_rows(path)


def volume_cfg(**kwargs):
    return PairCfg(leg_usd=1000, tranche_usd=500, cycle_min=1,
                   volume_cycle_min=5, volume_cycle_required=True,
                   trend_window_min=5, trend_confirm_min=2, **kwargs)


def test_single_hold_counts_planned_close_tranches_despite_price_drift():
    result = replay(rows(1), PairCfg(leg_usd=1000, tranche_usd=500), 0.3, 0.2,
                    "trend_hold", include_events=True)
    assert result["cycles"] == 1
    assert result["estimated_orders"] == 8  # 4 opening, 4 quantity-based closing
    assert [event["orders"] for event in result["events"]] == [4, 4]


def test_required_volume_still_trades_flat_market_after_warmup():
    result = replay(rows(), volume_cfg(trend_context_min=30), 0.3, 0.2,
                    "trend_hold", include_events=True)
    assert result["cycles"] > 1 and result["recycles"] > 0
    assert result["pnl_usd"] < 0 and result["cost_per_m"] > 0
    assert result["events"][0]["decision_ts"] == 30 * 60
    assert result["events"][0]["execution_ts"] == 31 * 60


def test_volume_due_is_measured_from_last_execution_in_full_replay():
    result = replay(rows(), volume_cfg(), 0.3, 0.2, "trend_hold", include_events=True)
    events = [event for event in result["events"] if event["side"] is not None]
    assert events[1]["decision_ts"] == events[0]["execution_ts"] + 5 * 60
    assert events[1]["execution_ts"] == events[1]["decision_ts"] + 60
    assert result["cadence_clock"] == "last_execution_clock"


def reversal_rows():
    return [(i * 60, 90 * math.exp((i if i < 90 else 180 - i) / 1e4), 100)
            for i in range(181)]


@pytest.mark.parametrize("prices", [rows(), rows(1), rows(-1), reversal_rows()])
def test_fixed_cadence_comparison_preserves_counts_and_causal_timing(prices):
    result = compare_fixed_cadence(prices, volume_cfg(trend_context_min=30), 0.3, 0.2,
                                  include_events=True)
    assert result["simulation_only"] and not result["market_validation"]
    assert result["same_order_count"] and result["same_cycle_count"]
    baseline, candidate = result["comparison"]
    for policy in (baseline, candidate):
        events = [event for event in policy["events"] if event["side"] is not None]
        assert policy["cadence_clock"] == "fixed_decision_clock"
        assert policy["estimated_orders"] == policy["cycles"] * 8
        assert all(event["execution_ts"] - event["decision_ts"] == 60 for event in events)
        assert all(b["execution_ts"] - a["execution_ts"] == 5 * 60 for a, b in zip(events, events[1:]))
    assert baseline["events"][0]["execution_ts"] == candidate["events"][0]["execution_ts"]


def test_fixed_comparison_legacy_retains_short_while_contextual_policy_follows_uptrend():
    # A brief weak pullback at the common start makes legacy enter A; weak
    # subsequent rises cannot confirm a fast reversal, preserving that old A.
    values = [i if i < 25 else (25 - .1 * (i - 25) if i <= 30 else 24.5 + .1 * (i - 30))
              for i in range(121)]
    prices = [(i * 60, 90 * math.exp(value / 1e4), 100) for i, value in enumerate(values)]
    result = compare_fixed_cadence(prices, volume_cfg(trend_context_min=30), 0.3, 0.2,
                                  include_events=True)
    baseline, candidate = result["comparison"]
    assert baseline["events"][0]["side"] == "A"
    assert candidate["events"][0]["side"] == "B"
    assert result["same_order_count"]
    assert candidate["pnl_usd"] > baseline["pnl_usd"]


def test_projection_uses_horizon_independent_from_check_cadence():
    cfg = replace(volume_cfg(), volume_cycle_min=0, volume_cycle_required=False,
                  trend_projection_min=10)
    result = replay(rows(1), cfg, 0.3, 0.2, "trend_hold")
    assert result["cycles"] == 1  # 10bp projection > cost, although 1bp check interval is not
    too_short = replay(rows(1), replace(cfg, trend_projection_min=1), 0.3, 0.2, "trend_hold")
    assert too_short["cycles"] == 0


def test_optional_scheduled_context_flip_uses_cpm_cap(monkeypatch):
    class ContextFlip:
        def __init__(self, *args, **kwargs):
            self.ts = 0

        def add(self, ts, *prices):
            self.ts = ts

        def status(self):
            return {"ready": True, "side": "A" if self.ts < 120 else None,
                    "volume_side": "A" if self.ts < 120 else "B",
                    "slope_bps_min": -1 if self.ts < 120 else 1}

    monkeypatch.setattr("tools.pair_trend_replay.TrendSignal", ContextFlip)
    cfg = replace(volume_cfg(), volume_cycle_required=False, cost_alert_per_m=79)
    blocked = replay(rows()[:15], cfg, 0.3, 0.2, "trend_hold", include_events=True)
    permitted = replay(rows()[:15], replace(cfg, cost_alert_per_m=81), 0.3, 0.2,
                       "trend_hold", include_events=True)
    assert blocked["cycles"] == 1 and blocked["flips"] == 0
    assert permitted["flips"] == 1
    assert [event["side"] for event in permitted["events"] if event["side"]][:2] == ["A", "B"]


def test_independent_closed_signal_cannot_see_future_bar_or_execute_at_signal_price():
    execution = rows(-1)[:61]
    signals = rows(1)[:61]
    cfg = volume_cfg()
    first = replay(execution, cfg, 0.3, 0.2, "trend_hold", signal_rows=signals, include_events=True)
    signals[-1] = (signals[-1][0], 9000, 100)  # closes after execution history has ended
    second = replay(execution, cfg, 0.3, 0.2, "trend_hold", signal_rows=signals, include_events=True)
    assert first == second
    assert first["independent_signal_prices"]
    assert first["events"][0]["decision_ts"] >= cfg.trend_window_min * 60
    assert all(event["side"] == "B" for event in first["events"] if event["side"])
    assert first["pnl_usd"] < 0  # long ratio loses on the declining execution series


def test_stale_independent_signal_blocks_new_cycles():
    result = replay(rows(1)[:31], volume_cfg(), 0.3, 0.2, "trend_hold",
                    signal_rows=rows(1)[:5], include_events=True)
    assert result["cycles"] == 1


@pytest.mark.parametrize("prices", [[], [(0, 90, 100), (0, 90, 100)],
                                    [(60, 90, 100), (0, 90, 100)],
                                    [(0, 90, 100), (180, 90, 100)],
                                    [(0, float("nan"), 100)], [(0, 0, 100)]])
def test_direct_replay_validates_data_as_strictly_as_csv(prices):
    with pytest.raises(ValueError):
        replay(prices, PairCfg(), 0.3, 0.2, "trend_hold")


def test_fixed_comparison_rejects_different_sizes_or_cadences():
    cfg = volume_cfg(trend_context_min=30)
    with pytest.raises(ValueError):
        compare_fixed_cadence(rows(), cfg, 0.3, 0.2, baseline_cfg=replace(cfg, leg_usd=2000))
