import math

import pytest

from arb.config import PairCfg
from tools.pair_trend_replay import replay, read_rows


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
