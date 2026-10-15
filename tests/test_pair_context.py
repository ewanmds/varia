import math
from dataclasses import replace

import pytest

from arb.config import PairCfg, load_pair_config
from arb.pair_signal import TrendSignal, select_volume_side


def closed_history(signal, moves, timestamps=None):
    timestamps = list(range(0, len(moves) * 60, 60)) if timestamps is None else timestamps
    points = [(ts, 90 * math.exp(move / 1e4), 100) for ts, move in zip(timestamps, moves)]
    signal.replace_closed(points, timestamps[-1] + 60)


@pytest.mark.parametrize("direction,context_side", [(1, "B"), (-1, "A")])
def test_context_vetoes_confirmed_local_pullback_without_changing_volume_cadence(direction, context_side):
    # The last five closes point down within a clear 30-minute rise (and vice versa).
    moves = [direction * (i if i < 25 else 24 - 0.8 * (i - 24)) for i in range(30)]
    fast = TrendSignal(5, 2)
    closed_history(fast, moves)
    local_side = "A" if direction > 0 else "B"
    assert fast.status()["side"] == local_side

    signal = TrendSignal(5, 2, context_min=30)
    closed_history(signal, moves)
    decision = signal.status()
    assert decision["ready"] and decision["context"]["ready"]
    assert decision["context"]["side"] == context_side
    assert decision["side"] is None
    assert decision["volume_side"] == context_side
    assert "opposée au contexte" in decision["reason"]
    assert select_volume_side(decision, current=local_side, previous=local_side) == context_side


def test_sustained_broad_reversal_changes_context_and_volume_direction():
    signal = TrendSignal(5, 2, context_min=30)
    for i in range(31):
        signal.add(i * 60, 90 * math.exp(i / 1e4), 100)
    assert signal.status()["context"]["side"] == "B"
    for i in range(31, 61):
        signal.add(i * 60, 90 * math.exp((30 - 2 * (i - 30)) / 1e4), 100)
    decision = signal.status()
    assert decision["ready"]
    assert decision["side"] == decision["context"]["side"] == decision["volume_side"] == "A"


@pytest.mark.parametrize("count", [5, 15, 29])
def test_enabled_context_requires_all_closed_minutes(count):
    signal = TrendSignal(5, 2, context_min=30)
    closed_history(signal, list(range(count)))
    decision = signal.status()
    assert not decision["ready"] and not decision["context"]["ready"]
    assert decision["side"] is None and decision["volume_side"] is None
    assert decision["reason"] == "préchauffage contexte"


def test_missing_context_minute_blocks_decision_even_with_ready_fast_window():
    signal = TrendSignal(5, 2, context_min=30)
    timestamps = [i * 60 for i in range(31) if i != 15]
    closed_history(signal, list(range(30)), timestamps)
    decision = signal.status()
    assert not decision["ready"] and not decision["context"]["ready"]
    assert decision["side"] is None and decision["volume_side"] is None
    assert decision["reason"] == "historique contexte discontinu"


def test_single_missing_fast_minute_is_rejected_without_context():
    signal = TrendSignal(5, 2)
    closed_history(signal, [0, 1, 3, 4, 5], [0, 60, 180, 240, 300])
    decision = signal.status()
    assert not decision["ready"] and decision["side"] is None
    assert decision["reason"] == "historique tendance discontinu"


def test_unclosed_bar_cannot_reverse_fast_or_context_signal():
    signal = TrendSignal(5, 2, context_min=30)
    closed_history(signal, list(range(30)))
    signal.add(30 * 60 + 55, 1, 100)
    decision = signal.status()
    assert decision["side"] == decision["context"]["side"] == decision["volume_side"] == "B"


def test_weak_current_slope_takes_priority_over_previous_short_without_context():
    signal = TrendSignal(5, 2)
    closed_history(signal, [i * 0.1 for i in range(5)])
    decision = signal.status()
    assert decision["ready"] and decision["side"] is None
    assert decision["volume_side"] == "B"
    assert select_volume_side(decision, current="A", previous="A") == "B"


@pytest.mark.parametrize("decision,current,previous,expected", [
    ({"volume_side": "B", "side": "A", "slope_bps_min": -1}, "A", "A", "B"),
    ({"side": "A", "slope_bps_min": 1}, "B", "B", "A"),
    ({"slope_bps_min": 0.1}, "A", "A", "B"),
    ({"slope_bps_min": -0.1}, "B", "B", "A"),
    ({"slope_bps_min": 0}, "A", "B", "A"),
    ({"slope_bps_min": float("nan")}, None, "A", "A"),
    ({"slope_bps_min": float("inf")}, None, None, "B"),
    ({}, "invalid", "A", "A"),
])
def test_scheduled_direction_evidence_precedence(decision, current, previous, expected):
    assert select_volume_side(decision, current, previous) == expected


@pytest.mark.parametrize("context", [-1, 5, 30, 121, 30.5, True, float("nan")])
def test_invalid_context_config_is_rejected(context):
    with pytest.raises(ValueError, match="contexte"):
        replace(PairCfg(), trend_context_min=context)


def test_context_defaults_disabled_and_loads_explicit_longer_window(tmp_path):
    assert PairCfg().trend_context_min == 0
    path = tmp_path / "context.toml"
    path.write_text("[pair]\ntrend_window_min = 5\ntrend_confirm_min = 2\ntrend_context_min = 30\n")
    cfg = load_pair_config(path)
    assert cfg.trend_context_min == 30
