import math
from pathlib import Path
import pytest
from arb.config import PairCfg, load_pair_config
from arb.pair_signal import RatioSignal

def test_pair_cfg_defaults_and_toml(tmp_path):
    c = PairCfg()
    assert (c.leg_usd, c.tranche_usd, c.cycle_min, c.daily_loss_usd) == (5000.0, 2500.0, 10.0, 200.0)
    p = tmp_path / "c.toml"
    p.write_text('[pair]\nenabled = true\nleg_usd = 3000\n')
    c = load_pair_config(p)
    assert c.enabled and c.leg_usd == 3000.0
    p.write_text('[pair]\nfoo = 1\n')
    with pytest.raises(ValueError):
        load_pair_config(p)
    p.write_text('[global]\n')
    assert load_pair_config(p).enabled is False

def _feed(sig, n, wti_fn, t0=0.0):
    for i in range(n):
        sig.add(t0 + i, wti_fn(i), 100.0)

def test_not_ready_keeps_previous_side():
    s = RatioSignal(window_min=60)
    _feed(s, 600, lambda i: 90.0)
    assert not s.ready() and s.z() is None and s.side("B") == "B"

def test_rich_wti_gives_side_A_and_cheap_gives_B():
    s = RatioSignal(window_min=60)
    _feed(s, 3600, lambda i: 90.0 + 0.01 * math.sin(i / 50))
    s.add(3600, 90.5, 100.0)          # WTI cher face au Brent
    assert s.ready() and s.z() > 0 and s.side("B") == "A"
    s2 = RatioSignal(window_min=60)
    _feed(s2, 3600, lambda i: 90.0 + 0.01 * math.sin(i / 50))
    s2.add(3600, 89.5, 100.0)
    assert s2.z() < 0 and s2.side("A") == "B"

def test_one_point_per_second_and_purge():
    s = RatioSignal(window_min=1)
    for k in range(10):
        s.add(5.2, 90.0, 100.0)       # même seconde : un seul point
    assert len(s._pts) == 1
    _feed(s, 200, lambda i: 90.0, t0=10.0)
    assert s._pts[0][0] >= 209 - 60


def test_reset_forces_new_warmup():
    sig = RatioSignal(1)
    for t in range(70):
        sig.add(float(t), 90.0 + t * 0.01, 100.0)
    assert sig.ready()
    sig.reset()
    assert not sig.ready() and sig.z() is None and sig.last_ts() is None
